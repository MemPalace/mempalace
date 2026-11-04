"""Recorder checks and envelopes, with upstream console units faked."""

import secrets
from types import SimpleNamespace
from unittest.mock import Mock, PropertyMock

import pytest

from commonplace.console import recorder
from commonplace.gateway import Mediator, WorkerUnavailable
from commonplace.rules import checks


@pytest.fixture
def recording(monkeypatch):
    calls = Mock()
    calls.attach_mock(Mock(wraps=checks.is_internal_call), "internal")
    calls.attach_mock(Mock(return_value=True), "recording")
    calls.attach_mock(Mock(return_value=object()), "extension")
    calls.attach_mock(Mock(side_effect=lambda name: (name, [])), "redact")
    calls.attach_mock(Mock(return_value=10.125), "clock")
    activity = Mock()
    calls.attach_mock(Mock(return_value=activity), "open")
    calls.attach_mock(activity.append, "append")
    monkeypatch.setattr(checks, "is_internal_call", calls.internal)
    monkeypatch.setattr(recorder.settings, "recording", calls.recording)
    monkeypatch.setattr(recorder.extensions, "get", calls.extension)
    monkeypatch.setattr(recorder.redact, "redact", calls.redact)
    monkeypatch.setattr(recorder, "monotonic", calls.clock)
    monkeypatch.setattr(recorder.store, "open_activity", calls.open)
    principal = Mock(connection_id="cid", workspace_id="ws")
    person = PropertyMock(return_value=False)
    runner = PropertyMock(return_value="agent")
    type(principal).person = person
    type(principal).runner = runner
    calls.attach_mock(person, "person")
    calls.attach_mock(runner, "runner")
    return SimpleNamespace(
        calls=calls,
        principal=principal,
        mediator=Mediator(object(), Mock()),
        request={"method": "tools/call", "params": {"name": "task_list", "arguments": {}}},
        response={"result": {"content": []}},
    )


def observe(env, *, response=...):
    return recorder.observe(
        env.mediator,
        env.principal,
        env.request,
        env.response if response is ... else response,
        10.0,
    )


def test_checks_and_append_run_in_order(recording):
    h = recording
    assert observe(h) is None
    assert [entry[0] for entry in h.calls.mock_calls] == [
        "internal",
        "person",
        "runner",
        "recording",
        "extension",
        "redact",
        "clock",
        "open",
        "append",
    ]
    h.calls.recording.assert_called_once_with(h.mediator.db, "cid")
    h.calls.open.assert_called_once_with(h.mediator.gateway, "ws")
    h.calls.append.assert_called_once_with("cid", "task_list", "ok", 125)


@pytest.mark.parametrize(
    ("skip", "expected"),
    [
        ("internal", ["internal"]),
        ("person", ["internal", "person"]),
        ("machine", ["internal", "person", "runner"]),
        ("off", ["internal", "person", "runner", "recording"]),
    ],
)
def test_skipped_calls_do_not_reach_later_checks_or_append(recording, skip, expected):
    h = recording
    if skip == "internal":
        h.calls.internal.return_value = True
    elif skip == "person":
        h.calls.person.return_value = True
    elif skip == "machine":
        h.calls.runner.return_value = "machine"
    else:
        h.calls.recording.return_value = False
    assert observe(h) is None
    assert [entry[0] for entry in h.calls.mock_calls] == expected
    h.calls.append.assert_not_called()


def test_internal_mediator_handle_does_not_append_with_positive_control(recording, monkeypatch):
    h = recording
    tool_call = Mock(return_value=h.response)
    monkeypatch.setattr(h.mediator, "_tools_call", tool_call)
    with checks.internal_call():
        assert h.mediator.handle(h.principal, h.request) is h.response
    tool_call.assert_called_once_with(h.principal, h.request)
    h.calls.recording.assert_not_called()
    h.calls.append.assert_not_called()
    assert h.mediator.handle(h.principal, h.request) is h.response
    h.calls.append.assert_called_once()


@pytest.mark.parametrize(
    ("response", "outcome"),
    [
        (None, "error"),
        ({"error": {"code": -32601}}, "denied"),
        ({"error": {"code": -32601}, "result": {"isError": True}}, "denied"),
        ({"error": {"code": -32602}}, "error"),
        ({"error": {}}, "error"),
        ({"error": None}, "error"),
        ({"result": {"isError": True}}, "error"),
        ({"result": {"isError": False}}, "ok"),
        ({"result": {"content": [{"text": '{"error":"body only"}'}]}}, "ok"),
        ({"result": {"error": "body only"}}, "ok"),
        ({}, "ok"),
    ],
)
def test_outcome_comes_only_from_envelope(recording, response, outcome):
    assert observe(recording, response=response) is None
    assert recording.calls.append.call_args.args[2] == outcome


@pytest.mark.parametrize("failure", [WorkerUnavailable, RuntimeError])
def test_mediator_records_raised_call_as_error(recording, monkeypatch, failure):
    h = recording
    monkeypatch.setattr(h.mediator, "_tools_call", Mock(side_effect=failure))
    if failure is WorkerUnavailable:
        response = h.mediator.handle(h.principal, h.request)
        assert response["error"]["code"] == -32603
    else:
        with pytest.raises(RuntimeError):
            h.mediator.handle(h.principal, h.request)
    assert h.calls.append.call_args.args[2] == "error"


@pytest.mark.parametrize("name", ["no_such_tool", "synthetic-secret-tool-name", None, 42, []])
def test_unrecognised_or_secret_name_becomes_unknown(recording, name):
    h = recording
    h.calls.extension.return_value = None
    h.request["params"]["name"] = name
    observe(h)
    h.calls.redact.assert_called_once_with("unknown")
    assert h.calls.append.call_args.args[1] == "unknown"
    if not isinstance(name, str):
        h.calls.extension.assert_not_called()


def test_secret_shaped_tool_name_never_reaches_redaction_or_append(recording):
    h = recording
    h.calls.extension.return_value = None
    h.request["params"]["name"] = "cmmn_" + secrets.token_urlsafe(32)
    observe(h)
    h.calls.redact.assert_called_once_with("unknown")
    assert h.calls.append.call_args.args[1] == "unknown"


@pytest.mark.parametrize("params", [None, [], {}, {"arguments": {}}])
def test_missing_or_malformed_params_record_unknown(recording, params):
    recording.request["params"] = params
    observe(recording)
    assert recording.calls.append.call_args.args[1] == "unknown"


def test_policy_tool_is_allowlisted_without_extension(recording):
    h = recording
    name = "mempalace_search"
    assert name in recorder.policy.TOOL_RULES
    h.calls.extension.return_value = None
    h.request["params"]["name"] = name
    observe(h)
    h.calls.redact.assert_called_once_with(name)
    assert h.calls.append.call_args.args[1] == name


def test_redaction_precedes_name_length_limit(recording):
    h = recording
    name = "registered_" + "x" * 100
    h.request["params"]["name"] = name
    h.calls.redact.side_effect = None
    h.calls.redact.return_value = ("[redacted:test]" + "y" * 100, ["test"])
    observe(h)
    h.calls.redact.assert_called_once_with(name)
    assert h.calls.append.call_args.args[1] == ("[redacted:test]" + "y" * 100)[:64]


@pytest.mark.parametrize(("now", "expected"), [(9, 0), (10.125, 125), (20_000, 10_000_000)])
def test_elapsed_milliseconds_are_clamped(recording, now, expected):
    recording.calls.clock.return_value = now
    observe(recording)
    assert recording.calls.append.call_args.args[3] == expected


@pytest.mark.parametrize(
    "stage",
    ["internal", "person", "runner", "recording", "extension", "redact", "clock", "open", "append"],
)
def test_observation_failures_never_raise_or_log_call_data(recording, caplog, stage):
    getattr(recording.calls, stage).side_effect = RuntimeError("synthetic-private-call-data")
    assert observe(recording) is None
    assert "synthetic-private-call-data" not in caplog.text
    if stage != "append":
        recording.calls.append.assert_not_called()


def test_append_failure_does_not_change_mediator_response(recording, monkeypatch):
    h = recording
    h.calls.append.side_effect = RuntimeError
    monkeypatch.setattr(h.mediator, "_tools_call", Mock(return_value=h.response))
    assert h.mediator.handle(h.principal, h.request) is h.response
    h.calls.append.assert_called_once()
