"""Independent real-SQLite check of an existing PR comment about split retries."""

import json
import subprocess
import sys
from argparse import Namespace
from collections import Counter
from pathlib import Path

import pytest

from mempalace.backends import PalaceRef
from mempalace.backends.sqlite_exact import SQLiteExactBackend
from mempalace.config import MempalaceConfig
from mempalace.wing_split import plan_split, save_split_plan, split_pending_path


@pytest.mark.parametrize("change_target", [True, False])
@pytest.mark.parametrize("wing", ["convos", "-Users-fixture-Documents-project"])
def test_interrupted_split_keeps_original_plan(tmp_path, monkeypatch, change_target, wing):
    import mempalace.cli as cli

    path = str(tmp_path)
    cfg = MempalaceConfig(palace_path=path)
    backend = SQLiteExactBackend()
    ref = PalaceRef(id=path, local_path=path)
    drawers = backend.get_collection(palace=ref, collection_name="mempalace_drawers", create=True)
    closets = backend.get_collection(palace=ref, collection_name="mempalace_closets", create=True)
    meta = {
        "wing": wing,
        "room": "technical",
        "source_file": "/Users/test/.claude/projects/p--acme-portal/session.jsonl",
    }
    ids = [f"d-{i:04d}" for i in range(501)]
    drawers.add(
        ids=ids,
        documents=["exact original"] * 501,
        metadatas=[meta] * 501,
        embeddings=[[1.0, 0.0]] * 501,
    )
    closets.add(ids=["k-1"], documents=["index text"], metadatas=[meta], embeddings=[[1.0, 0.0]])
    plan = plan_split(drawers, wing, ["portal"])
    assert len(plan["projects"]) == 1
    plan_path = Path(save_split_plan(cfg, plan))
    monkeypatch.setattr("mempalace.palace.get_collection", lambda *a, **k: drawers)
    monkeypatch.setattr("mempalace.palace.get_closets_collection", lambda *a, **k: closets)
    actual_update = drawers.update
    calls = 0

    def interrupt(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected stop before second batch")
        return actual_update(**kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(drawers, "update", interrupt)
        with pytest.raises(RuntimeError, match="second batch"):
            cli.cmd_wings(Namespace(wings_action="split", palace=path, wing=wing, yes=True))
    assert calls == 2
    marker = Path(split_pending_path(cfg, wing))
    assert marker.is_file()
    if wing.startswith("-"):
        # An earlier/manual operation may already have safe raw-name state.
        # Resume the pair in place rather than silently abandoning its marker.
        plan_path.rename(tmp_path / "wings" / f"split-{wing}.json")
        marker.rename(tmp_path / "wings" / f"split-{wing}.pending")
    before = drawers.get(include=["metadatas"])
    assert Counter(m["wing"] for m in before.metadatas) == {"portal": 500, wing: 1}
    if change_target:
        next(iter(plan["projects"].values()))["target"] = "portal_new"
        save_split_plan(cfg, plan)
    backend.close()
    result = json.loads(
        subprocess.check_output(
            [
                sys.executable,
                "-B",
                "-c",
                r"""
import contextlib, io, json, os, sys
from argparse import Namespace
from collections import Counter
from unittest.mock import patch
import mempalace.cli as cli
from mempalace.backends import PalaceRef
from mempalace.backends.sqlite_exact import SQLiteExactBackend
from mempalace.config import MempalaceConfig
from mempalace.wing_split import split_pending_path
path=sys.argv[1]
wing=sys.argv[2]
b=SQLiteExactBackend()
p=PalaceRef(id=path,local_path=path)
d=b.get_collection(palace=p,collection_name='mempalace_drawers')
k=b.get_collection(palace=p,collection_name='mempalace_closets')
error=None
with patch('mempalace.palace.get_collection',lambda *a,**kw:d),patch('mempalace.palace.get_closets_collection',lambda *a,**kw:k),contextlib.redirect_stdout(io.StringIO()):
    try:
        cli.cmd_wings(Namespace(wings_action='split',palace=path,wing=wing,yes=True))
    except (ValueError,RuntimeError,SystemExit) as e:
        error=repr(e)
b.close()
# Read the result through a newly opened backend, after the retry committed.
b=SQLiteExactBackend()
d=b.get_collection(palace=p,collection_name='mempalace_drawers').get(include=['documents','metadatas','embeddings'])
k=b.get_collection(palace=p,collection_name='mempalace_closets').get(include=['documents','metadatas','embeddings'])
out={'error':error,'drawers_by_wing':dict(Counter(m['wing'] for m in d.metadatas)),'closet_wing':k.metadatas[0]['wing'],'marker_exists':os.path.exists(split_pending_path(MempalaceConfig(palace_path=path),wing)),'content_preserved':all(s=='exact original' for s in d.documents) and k.documents==['index text'],'vectors_preserved':all(list(v)==[1.,0.] for v in d.embeddings)}
b.close()
print(json.dumps(out))
""",
                path,
                wing,
            ],
            text=True,
        )
    )
    print("FRESH_PROCESS_RESULT", json.dumps(result))
    assert result["content_preserved"] and result["vectors_preserved"]
    if change_target:
        assert result["error"] is not None, "Edited pending plan was accepted: " + json.dumps(
            result
        )
        assert result["drawers_by_wing"] == {"portal": 500, wing: 1}
        assert result["closet_wing"] == wing and result["marker_exists"]
    else:
        assert result["error"] is None
        assert result["drawers_by_wing"] == {"portal": 501}
        assert result["closet_wing"] == "portal" and not result["marker_exists"]
