"""End-to-end integration: ``mempalace init --llm-provider copilot`` with a
faked Copilot SDK.

Unlike ``tests/test_copilot_provider.py`` (which unit-tests the provider) and
``tests/test_copilot_refine_dataset.py`` (which drives ``refine_entities``
directly), this suite runs the REAL :func:`mempalace.cli.cmd_init` against a
REAL project directory (manifest + git authors + prose), through the REAL
:class:`mempalace.copilot_provider.CopilotProvider`, with only the
``github-copilot-sdk`` faked at the single seam
(:func:`mempalace.copilot_provider._ensure_sdk`).

It proves the whole wiring end to end: provider acquisition, the external-egress
consent bypass, Pass 0 corpus-origin detection, Pass 1 entity discovery + LLM
refinement, the tool-denied ``auto`` session contract, ``entities.json``
persistence, palace initialization, and prompt teardown of the runtime.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import mempalace.copilot_provider as cp
from mempalace.cli import cmd_init

GIT = shutil.which("git")


def _require_git():
    if GIT is None:
        pytest.skip("git executable not available")


def _init_repo(path: Path, name: str, email: str) -> None:
    _require_git()
    env = {
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": name,
        "GIT_COMMITTER_EMAIL": email,
    }
    subprocess.run([GIT, "init", "-q"], cwd=path, check=True)
    subprocess.run([GIT, "config", "user.name", name], cwd=path, check=True)
    subprocess.run([GIT, "config", "user.email", email], cwd=path, check=True)
    subprocess.run([GIT, "config", "commit.gpgsign", "false"], cwd=path, check=True)
    (path / "README.md").write_text("hello", encoding="utf-8")
    subprocess.run([GIT, "add", "README.md"], cwd=path, check=True, env=env)
    subprocess.run([GIT, "commit", "-q", "-m", "initial"], cwd=path, check=True, env=env)


# ── Faked SDK: an oracle Copilot runtime ─────────────────────────────────────


class _Msg:
    def __init__(self, content):
        self.content = content


class _Idle:
    pass


class _Err:
    def __init__(self, message):
        self.message = message


class _Event:
    def __init__(self, data):
        self.data = data


class _Reject:
    def __init__(self, feedback=None):
        self.feedback = feedback


class _Runtime:
    @staticmethod
    def for_uri(uri):
        return ("uri", uri)


class _ModelInfo:
    def __init__(self, id, supported=None, default=None):
        self.id = id
        self.supported_reasoning_efforts = supported
        self.default_reasoning_effort = default


class _OracleSession:
    def __init__(self, client, name_to_label):
        self._client = client
        self._map = name_to_label

    async def send_and_wait(self, prompt, timeout=60.0):
        self._client.prompts.append(prompt)
        if "CANDIDATES:" in prompt:
            classifications = [
                {"name": name, "label": label, "reason": "oracle"}
                for name, label in self._map.items()
                if f". {name}  (currently:" in prompt
            ]
            return _Event(_Msg(json.dumps({"classifications": classifications})))
        # Pass-0 corpus-origin (or any non-refine) prompt → benign, non-dialogue.
        return _Event(_Msg(json.dumps({"likely_ai_dialogue": False})))

    async def disconnect(self):
        return None


def _build_oracle_sdk(name_to_label):
    class OracleClient:
        instances: list = []

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.create_calls: list = []
            self.prompts: list = []
            self.started = False
            self.stopped = False
            OracleClient.instances.append(self)

        async def start(self):
            self.started = True

        async def stop(self):
            self.stopped = True

        async def list_models(self):
            return [_ModelInfo("auto", None, None)]

        async def create_session(self, **kwargs):
            self.create_calls.append(kwargs)
            return _OracleSession(self, name_to_label)

    OracleClient.instances = []
    sdk = cp._Sdk(
        CopilotClient=OracleClient,
        RuntimeConnection=_Runtime,
        PermissionDecisionReject=_Reject,
        AssistantMessageData=_Msg,
        SessionIdleData=_Idle,
        SessionErrorData=_Err,
    )
    return sdk, OracleClient


def _init_namespace(project_dir: Path, palace_dir: Path):
    import argparse

    return argparse.Namespace(
        dir=str(project_dir),
        palace=str(palace_dir),
        lang="en",
        no_llm=False,
        llm_provider="copilot",
        llm_model=None,
        llm_endpoint=None,
        llm_api_key=None,
        accept_external_llm=True,
        yes=True,
        auto_mine=False,
        backend=None,
    )


@pytest.fixture
def project(tmp_path):
    """A realistic corpus: manifest project + git author + prose candidates."""
    proj = tmp_path / "realproj"
    proj.mkdir()
    (proj / "package.json").write_text(json.dumps({"name": "realproj"}), encoding="utf-8")
    _init_repo(proj, "Dana Scully", "dana@example.com")
    (proj / "notes.md").write_text(
        "Alice reviewed the change. Alice merged it. Alice approved the release.\n"
        "We deploy with Terraform. Terraform runs nightly. Terraform scales well.\n"
        "Notes from the Meeting. Another Meeting today. Final Meeting adjourned.\n",
        encoding="utf-8",
    )
    return proj


def test_init_copilot_end_to_end_builds_palace_and_entities(project, tmp_path, monkeypatch):
    palace = tmp_path / "palace"
    monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(palace))

    sdk, client_cls = _build_oracle_sdk(
        {"Alice": "PERSON", "Terraform": "PROJECT", "Meeting": "COMMON_WORD"}
    )
    monkeypatch.setattr(cp, "_ensure_sdk", lambda: sdk)
    # Keep the integration scoped to init: don't prompt for / run a mine.
    monkeypatch.setattr("mempalace.cli._maybe_run_mine_after_init", lambda *a, **k: None)

    cmd_init(_init_namespace(project, palace))

    # entities.json written and valid.
    entities_path = project / "entities.json"
    assert entities_path.exists()
    entities = json.loads(entities_path.read_text(encoding="utf-8"))

    people = set(entities.get("people", []))
    projects = set(entities.get("projects", []))
    all_names = people | projects | set(entities.get("topics", []))

    # Deterministic source-backed signals.
    assert "realproj" in projects  # package.json manifest
    assert "Dana Scully" in people  # git author (authoritative)
    # LLM-classified prose candidate routed by the oracle.
    assert "Alice" in people
    # COMMON_WORD is dropped, never persisted.
    assert "Meeting" not in all_names

    # The palace was initialized and Pass 0 persisted origin.json.
    assert (palace / ".mempalace" / "origin.json").exists()

    # The runtime was actually driven, tool-denied, on the auto model, and a
    # refine (CANDIDATES) prompt was sent.
    assert client_cls.instances, "the Copilot runtime client was never constructed"
    client = client_cls.instances[-1]
    assert client.started is True
    assert client.create_calls, "no Copilot session was created"
    for call in client.create_calls:
        assert call["available_tools"] == []  # tool-denied contract
        assert call["model"] == "auto"
        assert "reasoning_effort" not in call  # auto omits effort
    assert any("CANDIDATES:" in p for p in client.prompts), "no refine prompt reached the runtime"

    # The provider was torn down (subprocess + loop thread reclaimed).
    assert client.stopped is True


def test_init_copilot_no_llm_stays_local(project, tmp_path, monkeypatch):
    """--no-llm must never construct the Copilot runtime (fully local init)."""
    palace = tmp_path / "palace"
    monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(palace))

    sdk, client_cls = _build_oracle_sdk({})
    monkeypatch.setattr(cp, "_ensure_sdk", lambda: sdk)
    monkeypatch.setattr("mempalace.cli._maybe_run_mine_after_init", lambda *a, **k: None)

    ns = _init_namespace(project, palace)
    ns.no_llm = True
    cmd_init(ns)

    assert client_cls.instances == [], "no_llm must not touch the Copilot SDK"
    # Source-backed entities still detected without any LLM.
    entities = json.loads((project / "entities.json").read_text(encoding="utf-8"))
    assert "realproj" in set(entities.get("projects", []))
    assert "Dana Scully" in set(entities.get("people", []))
