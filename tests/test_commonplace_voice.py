"""Dictation through the real gateway; no optional models or downloads in tests."""

import http.client
import importlib
import json
import logging
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from commonplace import plans, sessions, voice
from commonplace.control_db import ControlDB
from commonplace.social import accounts
from commonplace.social.providers import Profile
from tests._commonplace_helpers import Hub
from tests.test_commonplace_web import login, request


class FakeEngine:
    def __init__(self):
        self.present = True
        self.calls = []
        self.text = "Café  “yes”\n日本語 <script> & ik zei nee."
        self.error = False

    def available(self):
        return self.present

    def transcribe(self, language, pcm):
        self.calls.append((language, pcm))
        if self.error:
            raise RuntimeError(self.text + repr(pcm))
        return self.text


@pytest.fixture
def app(tmp_path):
    hub = Hub(tmp_path / "data", None)
    hub.gateway.rate_limits = False
    hub.gateway.voice_engine = FakeEngine()
    cookie = login(hub)[0]
    try:
        yield hub, cookie
    finally:
        hub.close()


def call(app, method="GET", path="/api/voice", body=None, headers=None):
    hub, cookie = app
    if isinstance(body, dict):
        body = json.dumps(body).encode()
    media = "application/octet-stream" if "/transcribe" in path else "application/json"
    status, _headers, raw = request(
        hub,
        path,
        method,
        body,
        {**cookie, "Content-Type": media, **(headers or {})},
    )
    return status, json.loads(raw)


def enable(app):
    assert call(app, "PATCH", body={"enabled": True}) == (
        200,
        {"available": True, "enabled": True, "languages": ["en", "nl"], "owner": True},
    )


def fresh_voice_user(hub, email):
    user = accounts.resolve(hub.db, "synthetic", Profile(email, "New user", (email,)), signup=True)
    assert not hub.db.user(user)["is_admin"]
    workspace = next(row["id"] for row in hub.db.workspaces() if row["personal_of"] == user)
    token = sessions.create_login_link(hub.db, email, workspace)
    status, headers, _ = request(hub, f"/app/login?t={token}")
    assert status == 302
    user_app = hub, {"Cookie": headers["Set-Cookie"].split(";", 1)[0]}
    assert call(user_app, path="/api/me")[1]["workspace"] == workspace
    enable(user_app)
    return user_app, user


@pytest.fixture
def limited_voice(app, monkeypatch):
    monkeypatch.setenv("COMMONPLACE_PLANS", "on")
    monkeypatch.setitem(plans.PLANS, "free", replace(plans.FREE, transcription_seconds=1))
    return fresh_voice_user(app[0], "fresh@example.com")


@pytest.mark.parametrize("samples,expected", [(15999, 200), (16000, 200), (16001, 429)])
def test_fresh_user_transcription_quota_uses_received_audio(limited_voice, samples, expected):
    app, user = limited_voice
    hub, _ = app
    assert plans.get_plan(hub.db, user).transcription_seconds == 1
    pcm = b"\0\0" * samples
    status, result = call(
        app,
        "POST",
        "/api/voice/transcribe?lang=en&duration=0&seconds=0",
        pcm,
        {"X-Audio-Duration": "0"},
    )
    assert status == expected
    if expected == 200:
        assert result == {"text": hub.gateway.voice_engine.text}
        assert hub.gateway.voice_engine.calls == [("en", pcm)]
        assert plans.transcription_used(hub.db, user) == 1
        # A successful replay is another clip, and cannot bypass the quota.
        assert call(app, "POST", "/api/voice/transcribe?lang=en", pcm)[0] == 429
        assert len(hub.gateway.voice_engine.calls) == 1
    else:
        assert result == {
            "error": plans.transcription_message(plans.get_plan(hub.db, user)),
            "limit": "transcription",
        }
        assert "1 second" in result["error"] and "00:00 UTC" in result["error"]
        assert hub.gateway.voice_engine.calls == []
        assert plans.transcription_used(hub.db, user) == 0
        assert call(app, "POST", "/api/voice/transcribe?lang=en", b"\0\0" * 16000)[0] == 200
    assert plans.transcription_used(hub.db, hub.admin) == 0
    status, payload = call(app, path="/api/plan")
    assert status == 200 and payload["transcription"] == {"used_today": 1, "limit": 1}


@pytest.mark.parametrize(
    "path,body,headers,expected",
    [
        ("?lang=en", b"", {}, 400),
        ("?lang=en", b"\0", {}, 400),
        ("?lang=de", b"\0\0", {}, 400),
        ("?lang=en", b"\0\0", {"Content-Type": "text/plain"}, 403),
        ("?lang=en", b"\0\0", {"Content-Length": "-2"}, 400),
        ("?lang=en", b"", {"Content-Length": str(voice.MAX_PCM_BYTES + 2)}, 413),
    ],
)
def test_invalid_audio_does_not_charge(limited_voice, path, body, headers, expected):
    app, user = limited_voice
    hub, _ = app
    assert call(app, "POST", "/api/voice/transcribe" + path, body, headers)[0] == expected
    assert plans.transcription_used(hub.db, user) == 0
    assert hub.gateway.voice_engine.calls == []
    assert call(app, "POST", "/api/voice/transcribe?lang=en", b"\0\0" * 16000)[0] == 200


def test_incomplete_audio_does_not_charge(limited_voice):
    import socket

    app, user = limited_voice
    hub, cookie = app
    conn = http.client.HTTPConnection("127.0.0.1", hub.port, timeout=5)
    try:
        conn.request(
            "POST",
            "/api/voice/transcribe?lang=en",
            b"\0\0",
            {**cookie, "Content-Type": "application/octet-stream", "Content-Length": "32000"},
        )
        conn.sock.shutdown(socket.SHUT_WR)
        response = conn.getresponse()
        assert response.status == 400
        assert json.loads(response.read()) == {"error": "Incomplete PCM body"}
    finally:
        conn.close()
    assert plans.transcription_used(hub.db, user) == 0
    assert hub.gateway.voice_engine.calls == []


def test_off_unavailable_and_failed_model_do_not_charge(limited_voice):
    app, user = limited_voice
    hub, _ = app
    engine = hub.gateway.voice_engine
    assert call(app, "PATCH", body={"enabled": False})[0] == 200
    assert call(app, "POST", "/api/voice/transcribe?lang=en", b"\0\0")[0] == 403
    enable(app)
    engine.present = False
    assert call(app, "POST", "/api/voice/transcribe?lang=en", b"\0\0")[0] == 503
    engine.present, engine.error = True, True
    pcm = b"\0\0" * 16000
    for _ in range(2):
        assert call(app, "POST", "/api/voice/transcribe?lang=en", pcm)[0] == 503
        assert plans.transcription_used(hub.db, user) == 0
    engine.error = False
    assert call(app, "POST", "/api/voice/transcribe?lang=en", pcm)[0] == 200
    assert plans.transcription_used(hub.db, user) == 1


def test_inflight_quota_is_atomic_and_other_users_are_independent(limited_voice, monkeypatch):
    app, user = limited_voice
    hub, _ = app
    other_app, other_user = fresh_voice_user(hub, "independent@example.com")
    entered, release = threading.Event(), threading.Event()
    pcm, other_pcm = b"\0\0" * 16000, b"\1\0" * 16000

    def recognize(language, audio):
        if audio == pcm:
            entered.set()
            assert release.wait(5)
        return "verbatim"

    monkeypatch.setattr(hub.gateway.voice_engine, "transcribe", recognize)
    with ThreadPoolExecutor(max_workers=5) as pool:
        first = pool.submit(call, app, "POST", "/api/voice/transcribe?lang=en", pcm)
        try:
            assert entered.wait(5)
            assert plans.transcription_used(hub.db, user) == 1
            denied = [
                pool.submit(call, app, "POST", "/api/voice/transcribe?lang=en", other_pcm)
                for _ in range(3)
            ]
            independent = pool.submit(
                call, other_app, "POST", "/api/voice/transcribe?lang=nl", other_pcm
            )
            assert [future.result(5)[0] for future in denied] == [429, 429, 429]
            assert independent.result(5) == (200, {"text": "verbatim"})
            assert plans.transcription_used(hub.db, other_user) == 1
        finally:
            release.set()
        assert first.result(5) == (200, {"text": "verbatim"})
    assert plans.transcription_used(hub.db, user) == 1


def test_failed_reservation_refunds_original_day_after_plan_change(limited_voice, monkeypatch):
    app, user = limited_voice
    hub, _ = app
    now = [datetime(2026, 1, 1, 23, 59, 59, tzinfo=timezone.utc)]
    monkeypatch.setattr(plans, "_now", lambda: now[0])

    def fail(language, pcm):
        assert plans.transcription_used(hub.db, user) == 1
        now[0] += timedelta(seconds=1)
        plans.use_transcription(hub.db, user, 1)
        plans.set_plan(hub.db, user, "paid", actor=hub.admin)
        raise RuntimeError("unavailable")

    monkeypatch.setattr(hub.gateway.voice_engine, "transcribe", fail)
    assert call(app, "POST", "/api/voice/transcribe?lang=en", b"\0\0")[0] == 503
    with hub.db._lock:
        rows = hub.db._conn.execute(
            "SELECT day, seconds FROM transcription_usage WHERE user_id=? ORDER BY day", (user,)
        ).fetchall()
    assert [tuple(row) for row in rows] == [("2026-01-01", 0), ("2026-01-02", 1)]


@pytest.mark.parametrize("switch,name", [("off", "free"), ("on", "paid"), ("on", "none")])
def test_unmetered_voice_does_not_record_usage(limited_voice, monkeypatch, switch, name):
    app, user = limited_voice
    hub, _ = app
    monkeypatch.setenv("COMMONPLACE_PLANS", switch)
    monkeypatch.setattr(plans, "load_ratio", lambda: 0)
    plans.set_plan(hub.db, user, name, actor=hub.admin)
    for error, expected in [(False, 200), (True, 503)]:
        hub.gateway.voice_engine.error = error
        assert call(app, "POST", "/api/voice/transcribe?lang=en", b"\0\0" * 32000)[0] == expected
    assert plans.transcription_used(hub.db, user) == 0
    with hub.db._lock:
        assert hub.db._conn.execute("SELECT * FROM transcription_usage").fetchall() == []


def test_default_owner_toggle_persists_and_workspace_isolated(app):
    hub, _cookie = app
    assert call(app) == (
        200,
        {"available": True, "enabled": False, "languages": ["en", "nl"], "owner": True},
    )
    assert hub.gateway.voice_engine.calls == []
    enable(app)
    assert call(app)[1]["enabled"] is True
    db = ControlDB(hub.db.path)
    try:
        assert voice.enabled(db, hub.workspace)
        other = db.create_workspace("another", hub.admin)
        assert not voice.enabled(db, other)
    finally:
        db.close()
    assert call(app, "PATCH", body={"enabled": False})[1]["enabled"] is False
    assert not voice.enabled(hub.db, hub.workspace)


def test_people_only_and_owner_only(app):
    hub, _cookie = app
    for method, path, body in [
        ("GET", "/api/voice", None),
        ("PATCH", "/api/voice", b'{"enabled":true}'),
        ("POST", "/api/voice/transcribe?lang=en", b"\0\0"),
    ]:
        assert request(hub, path, method, body)[0] == 401
        token = hub.owner_token(name=f"agent-{method}")
        assert request(hub, path, method, body, {"Authorization": f"Bearer {token}"})[0] == 403
    member = hub.db.create_user("member@example.com")
    hub.db.add_member(hub.workspace, member, "member", actor=hub.admin)
    session = sessions.redeem_login_link(
        hub.db, sessions.create_login_link(hub.db, "member@example.com", hub.workspace)
    )
    member_app = hub, {"Cookie": f"{sessions.COOKIE}={session}"}
    assert call(member_app) == (
        200,
        {"available": True, "enabled": False, "languages": ["en", "nl"], "owner": False},
    )
    assert call(member_app, "PATCH", body={"enabled": True})[0] == 403
    enable(app)
    assert call(member_app, "POST", "/api/voice/transcribe?lang=nl", b"\0\0")[0] == 200


def test_disabled_unavailable_and_model_failures(app, caplog):
    hub, _cookie = app
    engine = hub.gateway.voice_engine
    assert call(app, "POST", "/api/voice/transcribe?lang=en", b"\0\0")[0] == 403
    engine.present = False
    assert call(app)[1]["available"] is False
    assert call(app, "PATCH", body={"enabled": True})[0] == 503
    engine.present = True
    enable(app)
    engine.present = False
    assert call(app, "POST", "/api/voice/transcribe?lang=en", b"\0\0")[0] == 503
    engine.present, engine.error = True, True
    caplog.set_level(logging.DEBUG)
    status, result = call(app, "POST", "/api/voice/transcribe?lang=en", b"PRIVATE_AUDIO_")
    assert status == 503
    assert engine.text not in json.dumps(result, ensure_ascii=False) + caplog.text
    assert "PRIVATE_AUDIO" not in caplog.text
    assert repr(b"PRIVATE_AUDIO_") not in caplog.text
    assert "voice transcription failed: RuntimeError (en)" in caplog.text
    assert caplog.records[-1].args == ("RuntimeError", "en")


@pytest.mark.parametrize(
    "body",
    [{}, {"enabled": 1}, {"enabled": "yes"}, {"enabled": None}, {"enabled": True, "extra": 1}],
)
def test_toggle_validation(app, body):
    assert call(app, "PATCH", body=body)[0] == 400


@pytest.mark.parametrize("language", ["", "de", "EN", "en%00"])
def test_language_validation(app, language):
    enable(app)
    assert call(app, "POST", f"/api/voice/transcribe?lang={language}", b"\0\0")[0] == 400
    assert app[0].gateway.voice_engine.calls == []


@pytest.mark.parametrize("body", [b"", b"\0", b"abc"])
def test_pcm_validation(app, body):
    enable(app)
    assert call(app, "POST", "/api/voice/transcribe?lang=en", body)[0] == 400


def test_pcm_cap_is_exact_and_path_specific(app):
    enable(app)
    audio = b"\0\0" * (voice.MAX_PCM_BYTES // 2)
    assert call(app, "POST", "/api/voice/transcribe?lang=en", audio)[0] == 200
    for path, length in [
        ("/api/voice/transcribe?lang=en", voice.MAX_PCM_BYTES + 1),
        ("/api/voice/transcribe/", 1024 * 1024 + 1),
        ("/api/voice", 1024 * 1024 + 1),
        ("/api/boards", 1024 * 1024 + 1),
    ]:
        assert call(app, "POST", path, b"", {"Content-Length": str(length)})[0] == 413
    # The unchanged MCP cap remains larger; it gets as far as JSON validation.
    assert call(app, "POST", "/mcp", b" " * (1024 * 1024 + 1))[0] == 400


def test_pcm_origin_checks_and_media_type_still_apply(app):
    enable(app)
    for origin in ["https://evil.example", "null", "http://127.0.0.1:1"]:
        assert (
            call(app, "POST", "/api/voice/transcribe?lang=en", b"\0\0", {"Origin": origin})[0]
            == 403
        )
    assert (
        call(
            app,
            "POST",
            "/api/voice/transcribe?lang=en",
            b"\0\0",
            {"Origin": f"http://127.0.0.1:{app[0].port}"},
        )[0]
        == 200
    )
    for media in ["text/plain", "application/json"]:
        assert (
            call(app, "POST", "/api/voice/transcribe?lang=en", b"\0\0", {"Content-Type": media})[0]
            == 403
        )
    assert (
        call(app, "PATCH", body={"enabled": False}, headers={"Origin": "https://evil.example"})[0]
        == 403
    )
    assert (
        call(
            app,
            "PATCH",
            body={"enabled": False},
            headers={"Content-Type": "application/octet-stream"},
        )[0]
        == 403
    )
    # Reject duplicate headers, including on the PCM exception.
    hub, cookie = app
    conn = http.client.HTTPConnection("127.0.0.1", hub.port, timeout=5)
    try:
        conn.putrequest("POST", "/api/voice/transcribe?lang=en")
        for key, value in {**cookie, "Content-Length": "2"}.items():
            conn.putheader(key, value)
        conn.putheader("Content-Type", "application/octet-stream")
        conn.putheader("Content-Type", "application/octet-stream")
        conn.endheaders(b"\0\0")
        response = conn.getresponse()
        assert response.status == 403
        response.read()
    finally:
        conn.close()


def snapshot(directory):
    return {
        str(path.relative_to(directory)): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
    }


def test_verbatim_no_audio_or_transcript_on_disk_or_in_logs(app, tmp_path, monkeypatch, caplog):
    hub, _cookie = app
    enable(app)
    workspace = Path(hub.gateway.workspace_dir(hub.workspace))
    temp = tmp_path / "temp"
    temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    monkeypatch.setenv("TMPDIR", str(temp))
    before = snapshot(workspace), snapshot(temp)
    with hub.db._lock:
        audit = list(hub.db._conn.execute("SELECT * FROM audit_log"))
    caplog.set_level(logging.DEBUG)
    pcm = b"PRIVATE_AUDIO_1234" * 2
    for lang in ("en", "nl"):
        assert call(app, "POST", f"/api/voice/transcribe?lang={lang}", pcm) == (
            200,
            {"text": hub.gateway.voice_engine.text},
        )
    assert hub.gateway.voice_engine.calls == [("en", pcm), ("nl", pcm)]
    assert before == (snapshot(workspace), snapshot(temp))
    with hub.db._lock:
        assert list(hub.db._conn.execute("SELECT * FROM audit_log")) == audit
    assert hub.gateway.voice_engine.text not in caplog.text
    assert "PRIVATE_AUDIO" not in caplog.text


def test_engine_lazy_verbatim_threads_idle_per_language(monkeypatch):
    monkeypatch.setenv("COMMONPLACE_VOICE_THREADS", "2")
    now, loaded = [100.0], []

    def loader(lang, threads):
        loaded.append((lang, threads))
        return lambda pcm: " \n Café  “yes”\n日本語 <script> \t"

    engine = voice.VoiceEngine(loader, lambda: now[0])
    try:
        assert loaded == []
        assert engine.transcribe("en", b"\0\0") == "Café  “yes”\n日本語 <script>"
        assert loaded == [("en", 2)]
        now[0] += 100
        engine.transcribe("en", b"\0\0")
        engine.transcribe("nl", b"\0\0")
        now[0] += 500
        engine.transcribe("nl", b"\0\0")
        now[0] += 100
        engine.unload_idle()
        assert list(engine._models) == ["nl"]
        engine.transcribe("en", b"\0\0")
        assert loaded == [("en", 2), ("nl", 2), ("en", 2)]
        now[0] += 600
        engine.unload_idle()
        assert not engine._models and engine._timer is None
    finally:
        engine.close()


def test_engine_serializes_loading_and_transcription():
    entered, release = threading.Event(), threading.Event()
    loaded, calls = [], []

    def loader(lang, threads):
        loaded.append(lang)

        def transcribe(pcm):
            calls.append(pcm)
            entered.set()
            assert release.wait(5)
            return "yes"

        return transcribe

    engine = voice.VoiceEngine(loader)
    try:
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(engine.transcribe, "en", b"first")
            assert entered.wait(5)
            second = pool.submit(engine.transcribe, "en", b"second")
            assert calls == [b"first"] and loaded == ["en"]
            release.set()
            assert first.result(5) == second.result(5) == "yes"
            assert calls == [b"first", b"second"] and loaded == ["en"]
    finally:
        engine.close()


@pytest.mark.parametrize("missing", ["moonshine_onnx", "faster_whisper", None])
def test_extra_unavailable_without_importing_models(monkeypatch, missing):
    calls = []

    def find(name):
        calls.append(name)
        return None if name == missing else object()

    def unexpected_import(name):
        pytest.fail(f"availability imported {name}")

    monkeypatch.setattr(voice.importlib.util, "find_spec", find)
    monkeypatch.setattr(importlib, "import_module", unexpected_import)
    assert voice.available() is (missing is None)
    assert calls == (
        ["moonshine_onnx"] if missing == "moonshine_onnx" else ["moonshine_onnx", "faster_whisper"]
    )


def test_engine_trims_only_after_unloading_models():
    now, trimmed = [0.0], []

    def trim():
        assert engine._lock.locked()
        assert not engine._models
        trimmed.append(now[0])

    engine = voice.VoiceEngine(lambda lang, threads: lambda pcm: "yes", lambda: now[0], trim=trim)
    try:
        engine.unload_idle()
        engine.close()
        assert trimmed == []
        engine.transcribe("en", b"\0\0")
        now[0] = 599
        engine.unload_idle()
        assert trimmed == []
        now[0] = 600
        engine.unload_idle()
        assert trimmed == [600]
        engine.unload_idle()
        engine.close()
        assert trimmed == [600]
        engine.transcribe("nl", b"\0\0")
        engine.close()
        assert trimmed == [600, 600]
        engine.close()
        assert trimmed == [600, 600]
    finally:
        engine.close()


@pytest.mark.parametrize("failure", [OSError, AttributeError])
def test_trim_is_optional_without_glibc(monkeypatch, failure):
    def unavailable(name):
        raise failure("unavailable")

    monkeypatch.setattr(voice.ctypes, "CDLL", unavailable)
    voice._trim()


def test_installed_moonshine_model_exposes_weight_loader():
    if importlib.util.find_spec("moonshine_onnx") is None:
        pytest.skip("voice extra is not installed")
    from moonshine_onnx import MoonshineOnnxModel

    assert callable(getattr(MoonshineOnnxModel, "_load_weights_from_hf_hub", None)), (
        "Moonshine adapter requires MoonshineOnnxModel._load_weights_from_hf_hub; "
        "update the adapter before changing the supported version range"
    )


def test_static_worklet_uses_existing_csp(app):
    status, headers, body = request(app[0], "/app/voice-worklet.js")
    assert status == 200 and headers["Content-Type"].startswith("text/javascript")
    assert "script-src 'self'" in headers["Content-Security-Policy"]
    assert b'registerProcessor("commonplace-voice"' in body


def test_real_loaders_use_fixed_local_models_and_no_global_ort_patch(monkeypatch, caplog):
    import sys

    import numpy as np

    options, sessions_created, whisper_options = [], [], []

    class SessionOptions:
        def __init__(self):
            options.append(self)

    class Session:
        def __init__(self, path, **kwargs):
            sessions_created.append((path, kwargs))

        def get_inputs(self):
            return [SimpleNamespace(name="input_values")]

    class Moonshine:
        def _load_weights_from_hf_hub(self, name, precision):
            assert (name, precision) == ("base", "float")
            return ["encoder", "decoder"]

        def generate(self, samples, max_len):
            assert max_len == 192
            assert samples.dtype == np.float32 and samples.shape == (1, 2)
            assert samples[0].tolist() == [-1.0, 0.5]
            return [[1, 2]]

    class Whisper:
        def __init__(self, *args, **kwargs):
            whisper_options.append((args, kwargs))

        def transcribe(self, samples, **kwargs):
            assert kwargs == {
                "language": "nl",
                "beam_size": 1,
                "vad_filter": False,
                "condition_on_previous_text": False,
            }
            assert samples.tolist() == [-1.0, 0.5]
            self.logger.debug("PRIVATE_TRANSCRIPT")
            return iter([SimpleNamespace(text=" Ik"), SimpleNamespace(text="  zei nee. ")]), None

    ort = SimpleNamespace(SessionOptions=SessionOptions, InferenceSession=Session)
    monkeypatch.setitem(sys.modules, "onnxruntime", ort)
    monkeypatch.setitem(
        sys.modules,
        "moonshine_onnx",
        SimpleNamespace(
            MoonshineOnnxModel=Moonshine,
            load_tokenizer=lambda: SimpleNamespace(decode_batch=lambda tokens: [" yes  "]),
        ),
    )
    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=Whisper))
    pcm = b"\0\x80\0\x40"
    caplog.set_level(logging.DEBUG)
    assert voice._load_model("en", 3)(pcm) == " yes  "
    assert ort.InferenceSession is Session
    assert options[0].intra_op_num_threads == 3 and options[0].inter_op_num_threads == 1
    assert len(sessions_created) == 2
    assert all(kw["providers"] == ["CPUExecutionProvider"] for _, kw in sessions_created)
    assert voice._load_model("nl", 3)(pcm) == " Ik  zei nee. "
    assert whisper_options == [
        (("small",), {"device": "cpu", "compute_type": "int8", "cpu_threads": 3})
    ]
    assert "PRIVATE_TRANSCRIPT" not in caplog.text
