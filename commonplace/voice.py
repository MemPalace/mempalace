"""Opt-in, server-local dictation. PCM and transcripts live only in request memory."""

from __future__ import annotations

import ctypes
import gc
import importlib.util
import logging
import os
import re
import threading
import time
from dataclasses import dataclass

from commonplace import plans
from commonplace.http_util import query, read_json, require_principal, send_json

PCM_BYTES_PER_SECOND = 16000 * 2
MAX_PCM_BYTES = 60 * PCM_BYTES_PER_SECOND
LANGUAGES = ("en", "nl")
# The existing models, kept as the defaults so current installs change nothing.
# English runs Moonshine (only "base" is wired up); every other language runs
# faster-whisper, whose model may be a size name, a Hugging Face repo id or the
# absolute path of a local CTranslate2 directory.
DEFAULT_MODELS = {"en": "base", "nl": "small"}
MOONSHINE_MODELS = ("base",)
_MODEL_ID = re.compile(r"^/?[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$")
logger = logging.getLogger(__name__)


class VoiceConfigError(ValueError):
    """Voice settings are missing or invalid. Raised instead of falling back."""


@dataclass(frozen=True)
class VoiceConfig:
    enabled: bool
    models: dict


def _model_setting(language, environ):
    name = f"COMMONPLACE_VOICE_MODEL_{language.upper()}"
    if name not in environ:
        return DEFAULT_MODELS[language]
    model = environ[name].strip()
    if not model:
        raise VoiceConfigError(f"{name} is empty; set a model or unset it to use the default")
    if language == "en":
        if model not in MOONSHINE_MODELS:
            raise VoiceConfigError(
                f"{name}={model!r} is not supported; English uses one of {', '.join(MOONSHINE_MODELS)}"
            )
        return model
    parts = model.lstrip("/").split("/")
    relative_ok = model.startswith("/") or len(parts) <= 2
    if (
        len(model) > 200
        or not _MODEL_ID.match(model)
        or not relative_ok
        or any(part in (".", "..") for part in parts)
    ):
        raise VoiceConfigError(
            f"{name} must be a faster-whisper model size, a Hugging Face repo id "
            "(owner/name) or an absolute path to a local model directory"
        )
    if model.startswith("/") and not os.path.isdir(model):
        raise VoiceConfigError(f"{name}={model!r} is not a directory on this server")
    return model


def load_config(environ=None):
    """Read the instance-level voice settings.

    COMMONPLACE_VOICE is ``on`` (default) or ``off``. COMMONPLACE_VOICE_MODEL_EN and
    COMMONPLACE_VOICE_MODEL_NL pick each language's model. Anything invalid raises
    VoiceConfigError, so a typo never silently selects another model.
    """
    environ = os.environ if environ is None else environ
    switch = environ.get("COMMONPLACE_VOICE", "on")
    if switch not in ("on", "off"):
        raise VoiceConfigError(f"COMMONPLACE_VOICE must be 'on' or 'off', not {switch!r}")
    if switch == "off":
        return VoiceConfig(False, dict(DEFAULT_MODELS))
    return VoiceConfig(True, {lang: _model_setting(lang, environ) for lang in LANGUAGES})


def available():
    # Inspect the extra without loading its native libraries during sign-in.
    try:
        return all(
            importlib.util.find_spec(name) is not None
            for name in ("moonshine_onnx", "faster_whisper")
        )
    except (ImportError, ValueError):
        return False


def _trim():
    """Return freed model allocations to the OS when glibc supports it."""
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):
        pass


def _load_model(language, threads, model_name):
    if not model_name:
        raise VoiceConfigError(f"No voice model is configured for {language!r}")
    import numpy as np

    if language == "en":
        import onnxruntime as ort
        from moonshine_onnx import MoonshineOnnxModel, load_tokenizer

        # Moonshine's constructor has no SessionOptions seam. Initialize its
        # base-model sessions explicitly, without patching ORT process-wide.
        model = MoonshineOnnxModel.__new__(MoonshineOnnxModel)
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        paths = model._load_weights_from_hf_hub(model_name, "float")
        model.encoder, model.decoder = [
            ort.InferenceSession(path, sess_options=options, providers=["CPUExecutionProvider"])
            for path in paths
        ]
        model.encoder_input_names = [v.name for v in model.encoder.get_inputs()]
        model.decoder_input_names = [v.name for v in model.decoder.get_inputs()]
        model.num_layers, model.num_key_value_heads, model.head_dim = 8, 8, 52
        model.decoder_start_token_id, model.eos_token_id = 1, 2
        tokenizer = load_tokenizer()

        def transcribe(pcm):
            audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
            return tokenizer.decode_batch(
                model.generate(audio[None, :], max_len=max(192, int(audio.size / 16000 * 8)))
            )[0]

    else:
        from faster_whisper import WhisperModel

        model = WhisperModel(model_name, device="cpu", compute_type="int8", cpu_threads=threads)
        # faster-whisper's debug logging can include decoded segments. Give
        # this model a private, silent logger, including under root DEBUG.
        model.logger = logging.Logger("commonplace.voice.model", level=logging.CRITICAL + 1)
        model.logger.propagate = False

        def transcribe(pcm):
            audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
            segments, _ = model.transcribe(
                audio,
                language="nl",
                beam_size=1,
                vad_filter=False,
                condition_on_previous_text=False,
            )
            return "".join(segment.text for segment in segments)

    return transcribe


class VoiceEngine:
    """One process-wide queue, lazy per-language models, and an idle timer.

    The loader and monotonic clock are seams for offline, deterministic tests.
    The short-idle environment override is also useful for deployment smoke tests.
    """

    def __init__(
        self, loader=_load_model, clock=time.monotonic, idle_seconds=None, trim=_trim, models=None
    ):
        self.loader, self.clock, self.trim = loader, clock, trim
        self.models = dict(DEFAULT_MODELS if models is None else models)
        self.idle_seconds = (
            float(os.environ.get("COMMONPLACE_VOICE_IDLE_SECONDS", "600"))
            if idle_seconds is None
            else idle_seconds
        )
        self.threads = int(os.environ.get("COMMONPLACE_VOICE_THREADS", "4"))
        if self.threads < 1 or self.idle_seconds <= 0:
            raise ValueError("Voice threads and idle seconds must be positive")
        self._lock = threading.Lock()
        self._models, self._used = {}, {}
        self._timer = None

    available = staticmethod(available)

    def _schedule(self):
        if self._timer:
            self._timer.cancel()
        self._timer = None
        if self._used:
            delay = max(0.01, min(self._used.values()) + self.idle_seconds - self.clock())
            self._timer = threading.Timer(delay, self.unload_idle)
            self._timer.daemon = True
            self._timer.start()

    def unload_idle(self):
        with self._lock:
            expired = [
                lang
                for lang, used in self._used.items()
                if self.clock() - used >= self.idle_seconds
            ]
            for lang in expired:
                del self._models[lang], self._used[lang]
            if expired:
                gc.collect()
                self.trim()
            self._schedule()

    def transcribe(self, language, pcm):
        with self._lock:
            if self._timer:
                self._timer.cancel()
            try:
                if language not in self._models:
                    self._models[language] = self.loader(
                        language, self.threads, self.models.get(language)
                    )
                return self._models[language](pcm).strip()
            finally:
                if language in self._models:
                    self._used[language] = self.clock()
                self._schedule()

    def close(self):
        with self._lock:
            if self._timer:
                self._timer.cancel()
            self._timer = None
            loaded = bool(self._models)
            self._models.clear()
            self._used.clear()
            if loaded:
                gc.collect()
                self.trim()


_engine = None
_engine_lock = threading.Lock()


def engine(config=None):
    global _engine
    models = (config or load_config()).models
    with _engine_lock:
        # A new configuration replaces the engine, so settings never go stale.
        # close() takes the old engine's lock, so it waits for a running transcription.
        if _engine is not None and _engine.models != models:
            _engine.close()
            _engine = None
        if _engine is None:
            _engine = VoiceEngine(models=models)
        return _engine


def enabled(db, workspace):
    with db._lock:
        row = db._conn.execute(
            "SELECT voice_enabled FROM workspace_voice WHERE workspace_id=?", (workspace,)
        ).fetchone()
    return bool(row and row[0])


def _refusal(config, voice_engine):
    """Why voice cannot run on this instance right now, or None."""
    if not config.enabled:
        return "Voice is switched off on this instance"
    return None if voice_engine.available() else "Voice is unavailable"


def install(gateway):
    with gateway.db._lock:
        gateway.db._conn.execute("""CREATE TABLE IF NOT EXISTS workspace_voice (
            workspace_id TEXT PRIMARY KEY REFERENCES workspaces(id),
            voice_enabled INTEGER NOT NULL DEFAULT 0 CHECK (voice_enabled IN (0, 1))
        )""")
    # A bad setting stops startup with a clear message instead of picking a model.
    gateway.voice_config = config = load_config()
    # Tests may substitute a fake without importing the optional extra.
    gateway.voice_engine = engine(config)

    def person(h):
        principal = require_principal(gateway, h)
        if principal is not None and not principal.person:
            send_json(h, 403, {"error": "Person access required"})
            return None
        return principal

    def refusal():
        return _refusal(config, gateway.voice_engine)

    def get(h):
        principal = person(h)
        if principal is not None:
            send_json(
                h,
                200,
                {
                    "available": refusal() is None,
                    "enabled": enabled(gateway.db, principal.workspace_id),
                    "languages": list(LANGUAGES),
                    "owner": principal.role == "owner",
                },
            )

    def patch(h):
        principal = person(h)
        if principal is None:
            return
        if principal.role != "owner":
            send_json(h, 403, {"error": "Owner access required"})
            return
        try:
            body = read_json(h)
        except ValueError:
            return
        if set(body) != {"enabled"} or type(body["enabled"]) is not bool:
            send_json(h, 400, {"error": "enabled must be a boolean"})
            return
        if reason := refusal():
            send_json(h, 503, {"error": reason})
            return
        with gateway.db._lock:
            gateway.db._conn.execute(
                "INSERT INTO workspace_voice(workspace_id, voice_enabled) VALUES (?, ?) "
                "ON CONFLICT(workspace_id) DO UPDATE SET voice_enabled=excluded.voice_enabled",
                (principal.workspace_id, int(body["enabled"])),
            )
        send_json(
            h,
            200,
            {
                "available": True,
                "enabled": body["enabled"],
                "languages": list(LANGUAGES),
                "owner": principal.role == "owner",
            },
        )

    def transcribe(h):
        principal = person(h)
        if principal is None:
            return
        if not enabled(gateway.db, principal.workspace_id):
            send_json(h, 403, {"error": "Voice is off"})
            return
        if reason := refusal():
            send_json(h, 503, {"error": reason})
            return
        language = query(h).get("lang")
        if language not in LANGUAGES:
            send_json(h, 400, {"error": "Choose EN or NL"})
            return
        media = h.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if media != "application/octet-stream":
            send_json(h, 415, {"error": "Send raw PCM as application/octet-stream"})
            return
        length = int(h.headers.get("Content-Length", "0"))
        if length > MAX_PCM_BYTES:
            send_json(h, 413, {"error": "Recording exceeds 60 seconds"})
            return
        if not length or length % 2:
            send_json(h, 400, {"error": "Send nonempty 16 kHz mono int16 PCM"})
            return
        pcm = h.rfile.read(length)
        if len(pcm) != length:
            send_json(h, 400, {"error": "Incomplete PCM body"})
            return
        # The wire format fixes sample rate/channels/width. Bill received samples,
        # rounding up to the meter's whole seconds, never a client duration claim.
        seconds = (len(pcm) + PCM_BYTES_PER_SECOND - 1) // PCM_BYTES_PER_SECOND
        try:
            day = plans.use_transcription(gateway.db, principal.user_id, seconds)
        except plans.LimitReached as exc:
            send_json(h, 429, {"error": str(exc), "limit": "transcription"})
            return
        try:
            text = gateway.voice_engine.transcribe(language, pcm)
        except VoiceConfigError as exc:
            plans.refund_transcription(gateway.db, principal.user_id, seconds, day)
            # Setting names only, never user content: the operator needs this.
            logger.error("voice model setting is unusable: %s", exc)
            send_json(h, 503, {"error": "Voice is not configured correctly on this server."})
            return
        except Exception as exc:
            plans.refund_transcription(gateway.db, principal.user_id, seconds, day)
            # Exception messages and args may contain user content.
            logger.warning("voice transcription failed: %s (%s)", type(exc).__name__, language)
            send_json(h, 503, {"error": "Could not transcribe. Voice model is unavailable."})
            return
        send_json(h, 200, {"text": text})

    gateway.add_route("GET", "/api/voice", get)
    gateway.add_route("PATCH", "/api/voice", patch)
    gateway.add_route("POST", "/api/voice/transcribe", transcribe)
