"""Unauthorized import requests must be refused before reading their bodies."""

from types import SimpleNamespace

import pytest

from commonplace import imports
from commonplace.handoff.api import _RequestError
from commonplace.policy import Policy


def test_fix9_acl_refusal_reads_no_body(monkeypatch):
    read = []
    monkeypatch.setattr(
        imports,
        "_read_json",
        lambda handler, limit: read.append(limit) or {"source": "claude-code", "threads": []},
    )
    principal = SimpleNamespace(
        person=False,
        policy=Policy.build(scopes=[], read_wings=[], write_wing="inbox", trust="untrusted"),
    )
    with pytest.raises(_RequestError) as error:
        imports._import_envelope(SimpleNamespace(), principal, SimpleNamespace())
    assert error.value.status == 403
    assert read == []


def test_fix2_eligible_person_can_select_private_without_acl(monkeypatch):
    body = {"source": "claude-code", "threads": [], "private": True}
    reads = []
    monkeypatch.setattr(imports, "_read_json", lambda *args: reads.append(1) or body)
    monkeypatch.setattr(imports.import_grant, "eligible", lambda *args: True)
    principal = SimpleNamespace(
        person=True,
        user_id="synthetic-user",
        workspace_id="synthetic-workspace",
        policy=Policy.build(scopes=[], read_wings=[], write_wing="inbox", trust="untrusted"),
    )
    try:
        result = imports._import_envelope(
            SimpleNamespace(control_db=None), principal, SimpleNamespace()
        )
    except _RequestError as exc:
        raise AssertionError("an eligible person must be able to select the private path") from exc
    assert result == (body, "grant") and reads == [1]
