"""Metrics: timing extraction, VRAM measurement, embedding similarity, percentiles."""
from __future__ import annotations

import json
import re
import statistics
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


# ── Thinking-token stripping ─────────────────────────────────────────────
#
# Some models (Qwen 3 reasoning variants, DeepSeek-R1, QwQ) emit
# <think>...</think> blocks before their actual answer, or use Ollama's
# `thinking` / `reasoning` response fields. The benchmark scores against
# the final answer only, so we strip these.
#
# This is a local copy of the same logic that lives in
# `mempalace.local_model.strip_thinking_tokens`. We duplicate it here so
# the harness can land on develop independently of the model-router PR.
# When that PR merges, replace this with: from mempalace.local_model
# import strip_thinking_tokens

_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def strip_thinking_tokens(text: str, raw: dict) -> str:
    """Remove <think>...</think> blocks from text. Falls back to the raw
    response's `thinking` or `reasoning` fields if the main text is empty
    after stripping."""
    cleaned = _THINK_BLOCK_RE.sub("", text or "").strip()
    if cleaned:
        return cleaned
    msg = raw.get("message") if isinstance(raw, dict) else None
    if isinstance(msg, dict):
        for field_name in ("content", "thinking", "reasoning"):
            v = msg.get(field_name)
            if isinstance(v, str) and v.strip():
                return _THINK_BLOCK_RE.sub("", v).strip()
    for field_name in ("thinking", "reasoning"):
        v = raw.get(field_name) if isinstance(raw, dict) else None
        if isinstance(v, str) and v.strip():
            return _THINK_BLOCK_RE.sub("", v).strip()
    return cleaned


# ── Timing extraction from Ollama response ───────────────────────────────


@dataclass
class TimingSample:
    """Per-request timing extracted from Ollama's response payload."""

    e2e_ms: float
    ttft_ms: float
    tps: float
    eval_tokens: int
    prompt_tokens: int


def extract_timing(raw: dict, wall_clock_seconds: float) -> TimingSample:
    """Pull timing numbers out of an Ollama /api/chat response.

    Ollama returns durations in nanoseconds. Wall-clock is the Python-side
    measurement of the full request; we use it as the e2e source of truth
    since `total_duration` excludes some HTTP overhead.

    TTFT is approximated as load + prompt_eval, which is the time before
    the first output token would have streamed. Close enough for
    benchmarking without implementing streaming.
    """
    e2e_ms = wall_clock_seconds * 1000.0
    load_ns = raw.get("load_duration", 0) or 0
    prompt_eval_ns = raw.get("prompt_eval_duration", 0) or 0
    eval_ns = raw.get("eval_duration", 0) or 0
    eval_count = raw.get("eval_count", 0) or 0
    prompt_count = raw.get("prompt_eval_count", 0) or 0

    ttft_ms = (load_ns + prompt_eval_ns) / 1e6
    tps = (eval_count / (eval_ns / 1e9)) if eval_ns > 0 else 0.0

    return TimingSample(
        e2e_ms=e2e_ms,
        ttft_ms=ttft_ms,
        tps=tps,
        eval_tokens=eval_count,
        prompt_tokens=prompt_count,
    )


# ── Percentile aggregation ───────────────────────────────────────────────


@dataclass
class TimingAggregate:
    e2e_p50_ms: float
    e2e_p95_ms: float
    ttft_p50_ms: float
    ttft_p95_ms: float
    tps_p50: float
    tps_p95: float
    n: int


def aggregate_timings(samples: list[TimingSample]) -> TimingAggregate:
    if not samples:
        return TimingAggregate(0, 0, 0, 0, 0, 0, 0)
    e2e = sorted(s.e2e_ms for s in samples)
    ttft = sorted(s.ttft_ms for s in samples)
    tps = sorted(s.tps for s in samples)
    return TimingAggregate(
        e2e_p50_ms=_p(e2e, 50),
        e2e_p95_ms=_p(e2e, 95),
        ttft_p50_ms=_p(ttft, 50),
        ttft_p95_ms=_p(ttft, 95),
        tps_p50=_p(tps, 50),
        tps_p95=_p(tps, 95),
        n=len(samples),
    )


def _p(sorted_values: list[float], pct: int) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    # Nearest-rank: matches what most production tools (statsd, Datadog) report
    rank = max(0, min(len(sorted_values) - 1, int(round((pct / 100.0) * (len(sorted_values) - 1)))))
    return sorted_values[rank]


# ── VRAM measurement ─────────────────────────────────────────────────────


def vram_resident_mb(
    model_tag: str, endpoint: str = "http://localhost:11434", timeout: int = 5
) -> Optional[int]:
    """Resident VRAM for a loaded model, read from Ollama's /api/ps endpoint.

    Returns None if the model isn't loaded or the endpoint is unreachable.
    Uses the HTTP API rather than `ollama ps` since the CLI's --format flag
    is missing in older Ollama versions (verified against 0.23.2).
    """
    try:
        with urlopen(f"{endpoint}/api/ps", timeout=timeout) as resp:
            data = json.loads(resp.read())
    except (URLError, HTTPError, OSError, json.JSONDecodeError):
        return None
    for model in data.get("models", []) or []:
        if not isinstance(model, dict):
            continue
        name = model.get("name") or model.get("model") or ""
        if name == model_tag:
            size = model.get("size_vram") or model.get("size") or 0
            return int(size / (1024 * 1024)) if size else None
    return None


class VRAMPoller:
    """Poll nvidia-smi for peak GPU memory during a code block.

    Usage:
        poller = VRAMPoller()
        poller.start()
        # ... run inference ...
        peak_mb = poller.stop()

    Returns None if nvidia-smi is unavailable. Multi-GPU: tracks GPU 0 only.
    Single-GPU is the assumed deployment for this benchmark.
    """

    def __init__(self, interval_s: float = 0.5):
        # 500ms balances peak-capture coverage against jitter from nvidia-smi
        # subprocess spawns. Inference VRAM is mostly steady-state during a
        # request, so missing the absolute peak by a few-percent margin is
        # acceptable and worth the reduced overhead on the run itself.
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._peak_mb: int = 0
        self._available = self._check_nvidia_smi()

    @staticmethod
    def _check_nvidia_smi() -> bool:
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def _poll(self):
        while not self._stop.is_set():
            try:
                result = subprocess.run(
                    ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
                if result.returncode == 0:
                    first_line = result.stdout.strip().splitlines()[0]
                    mb = int(first_line.strip())
                    if mb > self._peak_mb:
                        self._peak_mb = mb
            except (subprocess.TimeoutExpired, ValueError, IndexError):
                pass
            self._stop.wait(self.interval_s)

    def start(self):
        if not self._available:
            return
        self._stop.clear()
        self._peak_mb = 0
        self._thread = threading.Thread(target=self._poll, daemon=True)
        self._thread.start()

    def stop(self) -> Optional[int]:
        if not self._available or self._thread is None:
            return None
        self._stop.set()
        self._thread.join(timeout=2)
        return self._peak_mb if self._peak_mb > 0 else None


# ── Embedding similarity (open-set scoring) ──────────────────────────────


def embed_text(
    text: str,
    model: str = "nomic-embed-text",
    endpoint: str = "http://localhost:11434",
    timeout: int = 30,
) -> Optional[list[float]]:
    """Get an embedding from Ollama's /api/embeddings. Returns None on failure."""
    body = json.dumps({"model": model, "prompt": text}).encode("utf-8")
    req = Request(
        f"{endpoint}/api/embeddings",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except (URLError, HTTPError, OSError, json.JSONDecodeError):
        return None
    return data.get("embedding")


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def label_similarity(
    predicted: str,
    target: str,
    embed_model: str = "nomic-embed-text",
    endpoint: str = "http://localhost:11434",
) -> float:
    """Cosine similarity between embeddings of two label strings.

    Used for open-set scoring where the model invents a label and we need
    to compare against a hand-chosen preferred label without requiring
    exact-match wording.
    """
    if predicted.strip().lower() == target.strip().lower():
        return 1.0
    emb_p = embed_text(predicted, model=embed_model, endpoint=endpoint)
    emb_t = embed_text(target, model=embed_model, endpoint=endpoint)
    if emb_p is None or emb_t is None:
        return 0.0
    return cosine_similarity(emb_p, emb_t)


# ── Hardware reporting ──────────────────────────────────────────────────


@dataclass
class HostInfo:
    cpu: str = ""
    cores: int = 0
    ram_gb: float = 0.0
    gpu: str = ""
    gpu_vram_mb: int = 0
    ollama_version: str = ""
    os: str = ""
    hostname: str = ""


def gather_host_info() -> HostInfo:
    """Best-effort introspection of the test machine. Failures degrade
    silently rather than aborting the benchmark."""
    info = HostInfo()

    info.hostname = (_run(["hostname"]) or "").strip()

    cpuinfo = _read_file("/proc/cpuinfo") or ""
    for line in cpuinfo.splitlines():
        if line.startswith("model name"):
            info.cpu = line.split(":", 1)[1].strip()
            break

    nproc = _run(["nproc"])
    if nproc:
        try:
            info.cores = int(nproc.strip())
        except ValueError:
            pass

    meminfo = _read_file("/proc/meminfo") or ""
    for line in meminfo.splitlines():
        if line.startswith("MemTotal:"):
            kb = int(line.split()[1])
            info.ram_gb = round(kb / (1024 * 1024), 1)
            break

    nvidia = _run([
        "nvidia-smi",
        "--query-gpu=name,memory.total",
        "--format=csv,noheader,nounits",
    ])
    if nvidia:
        first = nvidia.strip().splitlines()[0]
        if "," in first:
            name, vram = first.split(",", 1)
            info.gpu = name.strip()
            try:
                info.gpu_vram_mb = int(vram.strip())
            except ValueError:
                pass

    info.ollama_version = (_run(["ollama", "--version"]) or "").strip()

    info.os = (_read_file("/etc/os-release") or "").splitlines()[0] if _read_file("/etc/os-release") else ""
    if info.os.startswith("PRETTY_NAME="):
        info.os = info.os.split("=", 1)[1].strip('"')

    return info


def _run(cmd: list[str]) -> Optional[str]:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            return result.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def _read_file(path: str) -> Optional[str]:
    try:
        with open(path, "r") as f:
            return f.read()
    except OSError:
        return None
