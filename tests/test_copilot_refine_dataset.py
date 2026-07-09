"""Data-driven pipeline test: Copilot provider -> llm_refine.refine_entities.

This is the deterministic half of the dataset contract. An *oracle* Copilot SDK
echoes each candidate's ``expected_label`` from
``tests/data/copilot_refine_dataset.json``, and the real
:class:`mempalace.copilot_provider.CopilotProvider` is driven end-to-end through
:func:`mempalace.llm_refine.refine_entities`. The assertions prove the wiring —
prompt construction, JSON extraction (including ```json fences, the
claude-sonnet-4.5 quirk observed live), and bucket routing — carries every label
into the correct destination bucket.

The env-gated live suite (``tests/test_copilot_live_e2e.py``) replays the SAME
dataset against the real, authenticated Copilot CLI and asserts accuracy stays
at or above ``min_live_accuracy``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import mempalace.copilot_provider as cp
from mempalace.llm_refine import refine_entities

DATASET_PATH = Path(__file__).parent / "data" / "copilot_refine_dataset.json"


def _load_dataset() -> dict:
    return json.loads(DATASET_PATH.read_text(encoding="utf-8"))


# ── Self-contained oracle SDK (mirrors the surface the provider touches) ─────


class _OracleAssistantMessageData:
    def __init__(self, content: str):
        self.content = content


class _OracleSessionIdleData:
    pass


class _OracleSessionErrorData:
    def __init__(self, message: str):
        self.message = message


class _OracleEvent:
    def __init__(self, data):
        self.data = data


class _OracleReject:
    def __init__(self, feedback=None):
        self.feedback = feedback


class _OracleRuntimeConnection:
    @staticmethod
    def for_uri(uri):
        return ("uri", uri)


class _OracleModelInfo:
    def __init__(self, id, supported=None, default=None):
        self.id = id
        self.supported_reasoning_efforts = supported
        self.default_reasoning_effort = default


class _OracleSession:
    """Reads candidate names from the prompt and returns their expected labels.

    The provider forwards the refine ``user`` prompt verbatim; each candidate
    appears as ``N. <name>  (currently: <type>)`` (see
    ``llm_refine._build_user_prompt``), so a substring match is exact and
    order-independent, and it works whether refine sends one batch or several.
    """

    def __init__(self, name_to_label: dict[str, str], *, fence: bool):
        self._map = name_to_label
        self._fence = fence

    async def send_and_wait(self, prompt: str, timeout: float = 60.0):
        classifications = [
            {"name": name, "label": label, "reason": "oracle"}
            for name, label in self._map.items()
            if f". {name}  (currently:" in prompt
        ]
        payload = json.dumps({"classifications": classifications})
        if self._fence:
            payload = f"Here you go:\n```json\n{payload}\n```"
        return _OracleEvent(_OracleAssistantMessageData(payload))

    async def disconnect(self):
        return None


def _build_oracle_sdk(name_to_label: dict[str, str], *, fence: bool):
    class OracleClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def start(self):
            return None

        async def stop(self):
            return None

        async def list_models(self):
            return [_OracleModelInfo("auto", None, None)]

        async def create_session(self, **kwargs):
            return _OracleSession(name_to_label, fence=fence)

    return cp._Sdk(
        CopilotClient=OracleClient,
        RuntimeConnection=_OracleRuntimeConnection,
        PermissionDecisionReject=_OracleReject,
        AssistantMessageData=_OracleAssistantMessageData,
        SessionIdleData=_OracleSessionIdleData,
        SessionErrorData=_OracleSessionErrorData,
    )


# ── Helpers ─────────────────────────────────────────────────────────────────


def _detected_from_cases(cases: list[dict]) -> dict:
    """Group dataset cases into a detected-entities dict by input bucket.

    Signals are deliberately benign (``["prose"]``) so no candidate is treated
    as an authoritative git/manifest entity and every one is sent to the LLM.
    """
    detected: dict[str, list[dict]] = {
        "people": [],
        "projects": [],
        "topics": [],
        "uncertain": [],
    }
    type_for_bucket = {"people": "person", "projects": "project", "uncertain": "uncertain"}
    for case in cases:
        bucket = case["input_bucket"]
        detected[bucket].append(
            {
                "name": case["name"],
                "type": type_for_bucket[bucket],
                "signals": ["prose"],
            }
        )
    return detected


def _bucket_of(merged: dict, name: str) -> str | None:
    """Return the bucket a name landed in, or ``None`` if dropped entirely."""
    for bucket, items in merged.items():
        for entry in items:
            if entry["name"] == name:
                return bucket
    return None


# ── Tests ────────────────────────────────────────────────────────────────────


@pytest.fixture
def dataset() -> dict:
    return _load_dataset()


def test_dataset_is_well_formed(dataset):
    """Guard the fixture: unique names, valid labels/buckets, sane threshold."""
    cases = dataset["cases"]
    assert cases, "dataset must contain cases"
    names = [c["name"] for c in cases]
    assert len(names) == len(set(names)), "candidate names must be unique"
    label_to_bucket = dataset["label_to_bucket"]
    for case in cases:
        assert case["input_bucket"] in ("people", "projects", "uncertain")
        assert case["expected_label"] in label_to_bucket
        assert case["expected_bucket"] == label_to_bucket[case["expected_label"]]
    assert 0.0 < dataset["min_live_accuracy"] <= 1.0
    # The dataset must exercise every routable label at least once.
    labels = {c["expected_label"] for c in cases}
    assert {"PERSON", "PROJECT", "TOPIC", "COMMON_WORD"} <= labels


@pytest.mark.parametrize("fence", [False, True], ids=["plain-json", "fenced-json"])
def test_refine_routes_every_case_to_expected_bucket(dataset, monkeypatch, fence):
    """Every dataset case must land in its expected bucket through the real
    provider + refine pipeline, for both plain and ```json-fenced responses."""
    cases = dataset["cases"]
    name_to_label = {c["name"]: c["expected_label"] for c in cases}
    corpus_text = "\n".join(c["context"] for c in cases)

    sdk = _build_oracle_sdk(name_to_label, fence=fence)
    monkeypatch.setattr(cp, "_ensure_sdk", lambda: sdk)

    provider = cp.CopilotProvider(model="auto", timeout=30)
    try:
        result = refine_entities(
            _detected_from_cases(cases),
            corpus_text,
            provider,
            show_progress=False,
        )
    finally:
        provider.close()

    assert result.errors == []
    assert not result.cancelled

    for case in cases:
        landed = _bucket_of(result.merged, case["name"])
        if case["expected_bucket"] == "__dropped__":
            assert landed is None, f"{case['name']} should have been dropped, found in {landed}"
        else:
            assert landed == case["expected_bucket"], (
                f"{case['name']}: expected bucket {case['expected_bucket']}, got {landed}"
            )

    # Counts derived from the dataset — no brittle literals.
    expected_dropped = sum(1 for c in cases if c["expected_bucket"] == "__dropped__")
    expected_reclassified = sum(
        1
        for c in cases
        if c["expected_bucket"] != "__dropped__" and c["expected_bucket"] != c["input_bucket"]
    )
    assert result.dropped == expected_dropped
    assert result.reclassified == expected_reclassified


def test_refine_records_batch_error_without_aborting(dataset, monkeypatch):
    """A transport failure surfaces in errors and leaves candidates untouched,
    proving refine's per-batch resilience holds with the real provider."""
    cases = dataset["cases"]
    detected = _detected_from_cases(cases)

    sdk = _build_oracle_sdk({}, fence=False)
    monkeypatch.setattr(cp, "_ensure_sdk", lambda: sdk)

    provider = cp.CopilotProvider(model="auto", timeout=30)

    def _boom(*_args, **_kwargs):
        from mempalace.llm_client import LLMError

        raise LLMError("simulated transport failure")

    monkeypatch.setattr(provider, "classify", _boom)
    try:
        result = refine_entities(detected, "", provider, show_progress=False)
    finally:
        provider.close()

    assert result.errors, "batch failure must be recorded"
    assert result.reclassified == 0
    assert result.dropped == 0
    # Nothing lost: every input candidate is still present somewhere.
    all_names = {e["name"] for items in result.merged.values() for e in items}
    for case in cases:
        assert case["name"] in all_names
