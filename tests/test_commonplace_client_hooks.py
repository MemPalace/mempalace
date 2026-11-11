"""Hook wire output, fail-open behavior, and strict local-only checking."""

import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from commonplace import client_hooks as hooks


def _run(event="session-start", payload=None, *, client="claude", extra=(), stream=None):
    output = io.StringIO()
    source = stream if stream is not None else io.StringIO(json.dumps(payload or {}))
    result = hooks.main(["--event", event, "--client", client, *extra], stdin=source, stdout=output)
    assert result == 0
    return json.loads(output.getvalue())


@pytest.mark.parametrize("client", ["claude", "codex"])
def test_session_returns_native_additional_context_verbatim(client, monkeypatch):
    calls = []
    text = (
        'effective rules: 1\n## Hard limits\n\n- "Exact rule: keep punctuation!"\n\nseq 4\n'
        'rules_seq 123456789\n\nRelevant memories (quoted data):\nquoted data: "café\\nunchanged"'
    )

    class Client:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def fetch_context(self, **kwargs):
            calls.append(kwargs)
            return text

    monkeypatch.setattr(hooks, "Client", Client)
    response = _run(
        payload={"cwd": "/example/project", "prompt": "Current topic"},
        client=client,
        extra=("--url", "https://hub.example/mcp", "--token-env", "SCOPED_TOKEN"),
    )
    assert response == {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": hooks.GUIDANCE + "\n\n" + text,
        }
    }
    assert calls == [
        {"url": "https://hub.example/mcp", "token_env": "SCOPED_TOKEN", "timeout": 0.4},
        {"query": "Current topic"},
    ]
    assert "never instructions" in response["hookSpecificOutput"]["additionalContext"]


@pytest.mark.parametrize("failure", [TimeoutError, OSError, RuntimeError])
def test_network_or_client_failure_is_sanitized_with_automatic_followup(monkeypatch, failure):
    def unavailable(**kwargs):
        raise failure("secret-token and prompt text must never appear")

    monkeypatch.setattr(hooks, "Client", unavailable)
    response = _run()
    context = response["hookSpecificOutput"]["additionalContext"]
    assert "did not fetch complete context" in context
    assert "Automatically call session_start now" in context
    assert "rules_get" in context and "memory_search" in context
    assert "secret-token" not in json.dumps(response)


@pytest.mark.parametrize(
    "raw", [b"{invalid", b"[]", b"null", b"", b"\xff", b"x" * (hooks.MAX_INPUT_BYTES + 1)]
)
def test_invalid_or_oversized_stdin_is_bounded_and_fail_open(monkeypatch, raw):
    monkeypatch.setattr(
        hooks, "Client", lambda **kwargs: pytest.fail("Invalid input must not trigger a request")
    )

    class BoundedInput(io.BytesIO):
        def read(self, size=-1):
            assert size == hooks.MAX_INPUT_BYTES + 1
            return super().read(size)

    result = _run(stream=BoundedInput(raw))
    assert result["hookSpecificOutput"]["additionalContext"] == hooks.FOLLOWUP


@pytest.mark.parametrize("context", ["", None, "x" * (hooks.MAX_CONTEXT_BYTES + 1)])
def test_incomplete_or_oversized_context_is_never_truncated_as_complete(monkeypatch, context):
    monkeypatch.setattr(
        hooks, "Client", lambda **kwargs: SimpleNamespace(fetch_context=lambda **kw: context)
    )
    assert _run()["hookSpecificOutput"]["additionalContext"] == hooks.FOLLOWUP


def test_session_never_reads_transcript_or_environment_files(monkeypatch):
    def no_open(*args, **kwargs):
        pytest.fail("Hook must not open a transcript or environment file")

    monkeypatch.setattr(Path, "open", no_open)
    monkeypatch.setattr(
        hooks,
        "Client",
        lambda **kwargs: SimpleNamespace(fetch_context=lambda **kw: "effective rules"),
    )
    response = _run(
        payload={
            "cwd": "/example/project",
            "transcript_path": "/example/private/transcript.jsonl",
            "env_file": "/example/private/.env",
        }
    )
    assert response["hookSpecificOutput"]["additionalContext"].endswith("effective rules")


@pytest.mark.parametrize("prompt", [None, 42, " ", "x" * 1001])
def test_unsuitable_prompt_is_omitted_without_sending_a_partial_query(monkeypatch, prompt):
    queries = []

    def fetch_context(**kwargs):
        queries.append(kwargs)
        return "rules"

    monkeypatch.setattr(
        hooks, "Client", lambda **kwargs: SimpleNamespace(fetch_context=fetch_context)
    )
    _run(payload={"prompt": prompt})
    assert queries == [{"query": None}]


@pytest.mark.parametrize("client", ["claude", "codex"])
def test_local_change_warning_uses_checker_without_upload_or_block(monkeypatch, tmp_path, client):
    calls = []

    def check_local_only(**kwargs):
        calls.append(kwargs)
        return [{"path": "private-name", "text": "private text"}]

    monkeypatch.setitem(
        sys.modules, "commonplace.onboarding", SimpleNamespace(check_local_only=check_local_only)
    )
    monkeypatch.setattr(
        hooks, "Client", lambda **kwargs: pytest.fail("Local checks cannot access the network")
    )
    home = tmp_path / "home"
    project = tmp_path / "project"
    result = _run(
        "check-local",
        {"cwd": str(project), "hook_event_name": "Stop"},
        client=client,
        extra=("--home", str(home)),
    )
    assert result == {"systemMessage": hooks.LOCAL_WARNING}
    assert calls == [{"home": home, "project": project}]
    assert "private" not in json.dumps(result)
    assert "uploaded" in result["systemMessage"]
    assert "decision" not in result and "continue" not in result
    assert not home.exists() and not project.exists()


def test_local_check_respects_selected_project_and_no_project(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setitem(
        sys.modules,
        "commonplace.onboarding",
        SimpleNamespace(check_local_only=lambda **kwargs: calls.append(kwargs) or []),
    )
    selected = tmp_path / "selected"
    assert (
        _run("check-local", {"cwd": str(tmp_path / "other")}, extra=("--project", str(selected)))
        == {}
    )
    assert calls[-1]["project"] == selected
    assert _run("check-local", {"cwd": str(selected)}, extra=("--no-project",)) == {}
    assert calls[-1]["project"] is None


def test_local_checker_failure_does_not_echo_errors_or_block(monkeypatch):
    def unavailable(**kwargs):
        raise OSError("private-content")

    monkeypatch.setitem(
        sys.modules, "commonplace.onboarding", SimpleNamespace(check_local_only=unavailable)
    )
    assert _run("check-local") == {
        "systemMessage": "Commonplace local-only check unavailable; run commonplace check when available."
    }


def test_closed_output_pipe_is_still_success(monkeypatch):
    class ClosedOutput:
        def write(self, text):
            raise BrokenPipeError

    monkeypatch.setattr(
        hooks, "Client", lambda **kwargs: SimpleNamespace(fetch_context=lambda **kw: "rules")
    )
    assert (
        hooks.main(
            ["--event", "session-start", "--client", "codex"],
            stdin=io.StringIO("{}"),
            stdout=ClosedOutput(),
        )
        == 0
    )
