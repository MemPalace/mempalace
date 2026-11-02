"""Portable review policy: blind inputs, immutable diff and fail-closed preflight."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("fcntl", reason="the optional review client targets POSIX hosts")

SOURCE = Path(__file__).parents[1] / "tools" / "review_gate.py"


@pytest.fixture
def gate(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("review_gate_test", SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for key, value in {
        "CFG": tmp_path / "private",
        "PENDING_DIR": tmp_path / "private" / "pending",
        "LOCK_FILE": tmp_path / "private" / "lock",
        "USAGE_LOG": tmp_path / "private" / "usage.jsonl",
        "VERDICT_DIR": tmp_path / "private" / "verdicts",
    }.items():
        monkeypatch.setattr(module, key, str(value))
    return module


def test_blind_prompt_excludes_every_builder_field(gate):
    sentinel = "BUILDER_EXPLANATION_MUST_NOT_REACH_REVIEW"
    prompt = gate.build_review_prompt(
        "blind",
        "Trusted review policy",
        "+ actual code",
        problem="Original user problem",
        pr_text=sentinel,
        commits=sentinel,
    )
    assert sentinel not in prompt
    data = json.loads(prompt.split("<review_data>\n", 1)[1].split("\n</review_data>", 1)[0])
    assert data == {"problem": "Original user problem", "diff": "+ actual code"}
    with pytest.raises(SystemExit):
        gate.build_review_prompt("blind", "", "diff", problem="problem", context=sentinel)
    with pytest.raises(SystemExit):
        gate.build_review_prompt("blind", "", "diff")


def test_plan_retains_context_and_commit_excerpts(gate):
    prompt = gate.build_review_prompt(
        "plan",
        "plan policy",
        "future edits",
        pr_text="plan identity",
        commits="source commit",
        context="Current source context",
    )
    assert all(
        value in prompt
        for value in ["Current source context", "source commit", "plan identity", "future edits"]
    )


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.fixture
def commits(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Synthetic contributor")
    git(repo, "config", "user.email", "contributor@example.test")
    (repo / "code.py").write_text("value = 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "initial")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "code.py").write_text("value = 2\n")
    git(repo, "commit", "-qam", "feature")
    return repo, base, git(repo, "rev-parse", "HEAD")


def test_review_diff_comes_from_the_same_pinned_head(gate, commits, monkeypatch):
    repo, base, head = commits
    calls = []
    original_run = gate.run

    def fake_run(argv):
        calls.append(argv)
        if argv[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(
                argv,
                0,
                json.dumps(
                    {"headRefOid": head, "baseRefOid": base, "title": "BUILDER", "body": "BUILDER"}
                ),
                "",
            )
        if argv[:3] == ["gh", "repo", "view"]:
            return subprocess.CompletedProcess(argv, 0, "PUBLIC", "")
        assert argv[0] == "git", "Mutable PR diff or commit fetch must never run"
        return original_run(argv)

    monkeypatch.setattr(gate, "run", fake_run)
    meta, diff, public, messages = gate.gather_pr("owner/project", 1, head, repo_path=repo)
    expected = subprocess.check_output(
        [
            "git",
            "--no-replace-objects",
            "-C",
            str(repo),
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--binary",
            "--full-index",
            base,
            head,
            "--",
        ],
        text=True,
    )
    assert diff == expected and public and messages == ""
    assert meta["regressionBaseOid"] == base
    assert not any(argv[:3] == ["gh", "pr", "diff"] for argv in calls)
    for wrong in [head[:8], "a" * 40, None]:
        with pytest.raises(SystemExit):
            gate.gather_pr("owner/project", 1, wrong, repo_path=repo)


def test_pinned_diff_does_not_execute_external_diff(gate, commits, tmp_path):
    repo, base, head = commits
    marker = tmp_path / "external-ran"
    external = tmp_path / "external-diff"
    external.write_text(
        f"#!{Path(sys.executable).resolve()}\n"
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('invoked')\n"
        "print('unrelated external diff output')\n"
    )
    external.chmod(0o700)
    git(repo, "config", "diff.external", str(external))

    diff, actual_base = gate.pinned_diff(repo, base, head)

    assert not marker.exists(), "review must not execute a configured external diff"
    assert actual_base == base
    assert "+value = 2\n" in diff and "-value = 1\n" in diff


def test_pinned_diff_ignores_replacement_objects(gate, commits, monkeypatch):
    repo, base, head = commits
    (repo / "code.py").write_text("value = 9\n")
    git(repo, "add", "code.py")
    tree = git(repo, "write-tree")
    replacement = git(repo, "commit-tree", tree, "-p", base, "-m", "synthetic replacement")
    git(repo, "replace", head, replacement)
    calls = []
    original_run = gate.run

    def traced_run(argv):
        calls.append(argv)
        return original_run(argv)

    monkeypatch.setattr(gate, "run", traced_run)
    diff, actual_base = gate.pinned_diff(repo, base, head)

    assert actual_base == base
    assert "+value = 2\n" in diff and "+value = 9\n" not in diff
    assert all(argv[:2] == ["git", "--no-replace-objects"] for argv in calls)
    diff_call = next(argv for argv in calls if "diff" in argv)
    assert "--no-ext-diff" in diff_call and "--no-textconv" in diff_call


@pytest.mark.parametrize(
    "args",
    [
        ["--mode", "plan", "--purpose", "gate"],
        ["--purpose", "review", "--context", "builder-plan"],
        ["--purpose", "review", "--change-kind", "feature"],
    ],
)
def test_invalid_requests_never_load_credentials(gate, tmp_path, monkeypatch, args):
    diff = tmp_path / "change.diff"
    diff.write_text("+ safe synthetic change\n")
    monkeypatch.setattr(gate, "load_key", lambda: pytest.fail("credential loader called"))
    monkeypatch.setattr("sys.argv", ["review-gate", "--diff", str(diff), *args])
    with pytest.raises(SystemExit):
        gate.main()


def test_bugfix_without_runner_blocks_before_credentials(gate, commits, tmp_path, monkeypatch):
    repo, base, head = commits
    diff, _ = gate.pinned_diff(repo, base, head)
    path = tmp_path / "change.diff"
    path.write_text(diff)
    problem = tmp_path / "problem.txt"
    problem.write_text("Correct the original defect")
    monkeypatch.setattr(gate, "load_key", lambda: pytest.fail("credential loader called"))
    monkeypatch.setattr(
        "sys.argv",
        [
            "review-gate",
            "--purpose",
            "gate",
            "--repo-path",
            str(repo),
            "--base",
            base,
            "--head",
            head,
            "--diff",
            str(path),
            "--problem",
            str(problem),
        ],
    )
    with pytest.raises(SystemExit, match="2"):
        gate.main()


def test_supplied_local_diff_must_match_pinned_objects(gate, commits, tmp_path, monkeypatch):
    repo, base, head = commits
    diff = tmp_path / "wrong.diff"
    diff.write_text("+ a different unproven change\n")
    monkeypatch.setattr(gate, "load_key", lambda: pytest.fail("credential loader called"))
    monkeypatch.setattr(
        "sys.argv",
        [
            "review-gate",
            "--purpose",
            "gate",
            "--diff",
            str(diff),
            "--repo-path",
            str(repo),
            "--base",
            base,
            "--head",
            head,
        ],
    )
    with pytest.raises(SystemExit):
        gate.main()


def test_only_final_json_block_is_a_verdict(gate):
    valid = '```json\n{"verdict":"PASS","summary":"ok","findings":[]}\n```'
    assert gate.valid_verdict(gate.extract_json(valid))
    assert gate.extract_json(valid + "\n```json\n{invalid}\n```") is None
    assert gate.extract_json(valid + "\n```json\n{") is None
    assert not gate.valid_verdict({"verdict": "PASS", "summary": "x", "findings": "major"})
    assert not gate.valid_verdict(
        {
            "verdict": "PASS",
            "summary": "x",
            "findings": [{"severity": "unknown", "issue": "x", "fix": "x"}],
        }
    )


def test_credit_policy_is_explicit_and_bounded(gate, tmp_path):
    path = tmp_path / "policy.json"
    for policy in [
        {},
        {"cap_usd": 10, "free_credit_usd": 5, "expires": "2030-01-01"},
        {"cap_usd": float("nan"), "free_credit_usd": 5, "expires": "2030-01-01"},
    ]:
        path.write_text(json.dumps(policy))
        with pytest.raises(SystemExit):
            gate.configure_policy(path)
    path.write_text(json.dumps({"cap_usd": 5, "free_credit_usd": 10, "expires": "2030-01-01"}))
    gate.configure_policy(path)
    assert gate.BUDGET_CAP_USD == 5


def api_fixture(gate, tmp_path, monkeypatch, *, cap=10, session=None):
    diff = tmp_path / "input.diff"
    diff.write_text("+ actual feature\n")
    problem = tmp_path / "problem.txt"
    problem.write_text("Original independent problem")
    policy = tmp_path / "policy.json"
    values = {"cap_usd": cap, "free_credit_usd": max(10, cap), "expires": "2099-01-01"}
    if session is not None:
        values["session"] = session
    policy.write_text(json.dumps(values))
    args = [
        "review-gate",
        "--purpose",
        "review",
        "--change-kind",
        "feature",
        "--diff",
        str(diff),
        "--problem",
        str(problem),
        "--policy",
        str(policy),
    ]
    monkeypatch.setattr("sys.argv", args)
    return diff, problem, args


def response(kind="feature"):
    result = {"verdict": "PASS", "summary": "Synthetic review", "findings": [], "change_kind": kind}
    return {
        "content": [
            {"type": "server_tool_use", "name": "advisor"},
            {"type": "text", "text": "```json\n" + json.dumps(result) + "\n```"},
        ],
        "usage": {},
        "stop_reason": "end_turn",
    }


def test_actual_request_uses_frozen_prepared_payload(gate, tmp_path, monkeypatch):
    diff, problem, _ = api_fixture(gate, tmp_path, monkeypatch)
    prepared = gate.prepare_review(gate.parse_args())
    diff.write_text("BUILDER_CHANGED_INPUT_AFTER_PREFLIGHT")
    problem.write_text("BUILDER_CHANGED_PROBLEM_AFTER_PREFLIGHT")
    captured = []
    monkeypatch.setattr(gate, "load_key", lambda: "synthetic-not-a-real-key")
    monkeypatch.setattr(
        gate,
        "api_call",
        lambda _key, body: captured.append(json.loads(json.dumps(body))) or response(),
    )
    gate.execute_review(prepared)
    assert captured[0]["messages"] == [{"role": "user", "content": prepared.user}]
    assert "BUILDER_CHANGED" not in captured[0]["messages"][0]["content"]
    assert "Original independent problem" in captured[0]["messages"][0]["content"]
    assert (
        prepared.metadata["diff_sha256"] == gate.hashlib.sha256(b"+ actual feature\n").hexdigest()
    )


def test_actual_api_request_has_no_pr_builder_metadata(gate, tmp_path, monkeypatch):
    _, _, args = api_fixture(gate, tmp_path, monkeypatch)
    sentinel = "BUILDER_SENTINEL"
    meta = {
        "headRefOid": "a" * 40,
        "baseRefOid": "b" * 40,
        "regressionBaseOid": "b" * 40,
        "title": sentinel,
        "body": sentinel,
        "baseRefName": sentinel,
        "headRefName": sentinel,
        "additions": 1,
        "deletions": 0,
        "changedFiles": 1,
    }
    monkeypatch.setattr(
        gate, "gather_pr", lambda *args, **kwargs: (meta, "+ actual feature\n", False, sentinel)
    )
    # PR source replaces local --diff, but none of the fetched builder metadata
    # may enter the real request serialized for the API.
    i = args.index("--diff")
    del args[i : i + 2]
    args += [
        "--pr",
        "1",
        "--repo",
        "owner/project",
        "--head",
        "a" * 40,
        "--repo-path",
        str(tmp_path),
    ]
    captured = []
    monkeypatch.setattr(gate, "load_key", lambda: "synthetic-not-a-real-key")
    monkeypatch.setattr(
        gate,
        "api_call",
        lambda _key, body: captured.append(json.loads(json.dumps(body))) or response(),
    )
    assert gate.main() == 0
    assert sentinel not in json.dumps(captured)
    artifact = json.loads(next(Path(gate.VERDICT_DIR).glob("*.json")).read_text())
    assert artifact["head"] == "a" * 40 and artifact["mode"] == "blind"
    assert next(Path(gate.VERDICT_DIR).glob("*.json")).stat().st_mode & 0o777 == 0o600


def test_usage_caller_uses_generic_user_label(gate, tmp_path, monkeypatch):
    api_fixture(gate, tmp_path, monkeypatch)
    real_get = gate.os.environ.get

    def environment_get(key, default=None):
        if key == "USER":
            return "synthetic-user"
        if key.endswith("_THREAD_ID"):
            return "private-run-must-not-be-caller"
        return real_get(key, default)

    monkeypatch.setattr(gate.os.environ, "get", environment_get)
    monkeypatch.setattr(gate, "load_key", lambda: "synthetic-not-a-real-key")
    monkeypatch.setattr(gate, "api_call", lambda *_: response())

    assert gate.main() == 0
    record = json.loads(Path(gate.USAGE_LOG).read_text())
    assert record["caller"] == "synthetic-user"


@pytest.mark.parametrize("scope", ["global", "session", "window"])
def test_credit_refusal_precedes_credential_load(gate, tmp_path, monkeypatch, scope):
    session = None
    if scope != "global":
        session = {
            "start": "2000-01-01T00:00:00+00:00",
            "end": "2099-01-01T00:00:00+00:00",
            "cap_usd": 0.000001,
            "baseline_usd": 0,
        }
        if scope == "window":
            session["end"] = "2001-01-01T00:00:00+00:00"
    api_fixture(
        gate, tmp_path, monkeypatch, cap=0.000001 if scope == "global" else 10, session=session
    )
    monkeypatch.setattr(gate, "load_key", lambda: pytest.fail("blocked credit read credentials"))
    with pytest.raises(SystemExit) as error:
        gate.main()
    assert error.value.code == 3
    assert not list(Path(gate.PENDING_DIR).iterdir())


def test_execution_failure_releases_only_own_reservation(gate, tmp_path, monkeypatch):
    api_fixture(gate, tmp_path, monkeypatch)
    monkeypatch.setattr(
        gate,
        "execute_review",
        lambda prepared: (_ for _ in ()).throw(RuntimeError("synthetic execution failure")),
    )
    with pytest.raises(RuntimeError):
        gate.main()
    assert not list(Path(gate.PENDING_DIR).iterdir())
    assert Path(gate.VERDICT_DIR).stat().st_mode & 0o777 == 0o700
    assert not list(Path(gate.VERDICT_DIR).iterdir()), "Execution failure must leave no verdict"


def test_feature_declaration_cannot_override_blind_bugfix_classification(
    gate, tmp_path, monkeypatch
):
    api_fixture(gate, tmp_path, monkeypatch)
    monkeypatch.setattr(gate, "load_key", lambda: "synthetic-not-a-real-key")
    monkeypatch.setattr(gate, "api_call", lambda _key, body: response("bugfix"))
    assert gate.main() == 1
    artifact = json.loads(next(Path(gate.VERDICT_DIR).glob("*.json")).read_text())
    assert (
        artifact["verdict"] == "FAIL" and artifact["parsed"]["findings"][-1]["severity"] == "major"
    )


def failing_response(line="8-10"):
    data = response()
    verdict = {
        "verdict": "FAIL",
        "change_kind": "feature",
        "summary": "Synthetic failure",
        "findings": [
            {
                "severity": "major",
                "file": "example.py",
                "line": line,
                "checklist": "write handling",
                "issue": "  Preserve this exact failure.  ",
                "fix": "Keep the write blocked.",
            }
        ],
    }
    data["content"][-1]["text"] = "```json\n" + json.dumps(verdict) + "\n```"
    return data


def test_generated_verdicts_feed_actual_offline_audit(gate, tmp_path, monkeypatch):
    from tools.review_support.audit import audit

    _, _, args = api_fixture(gate, tmp_path, monkeypatch)
    i = args.index("--diff")
    del args[i : i + 2]
    args += [
        "--pr",
        "1",
        "--repo",
        "owner/project",
        "--head",
        "a" * 40,
        "--repo-path",
        str(tmp_path),
    ]

    def metadata(_repo, _pr, head, **_kwargs):
        return (
            {
                "headRefOid": head,
                "baseRefOid": "c" * 40,
                "regressionBaseOid": "c" * 40,
                "title": "Synthetic feature",
                "body": "",
                "baseRefName": "base",
                "headRefName": "head",
                "additions": 1,
                "deletions": 0,
                "changedFiles": 1,
            },
            "+ actual feature\n",
            False,
            "",
        )

    monkeypatch.setattr(gate, "gather_pr", metadata)
    monkeypatch.setattr(gate, "load_key", lambda: "synthetic-not-a-real-key")
    monkeypatch.setattr(gate, "api_call", lambda _key, _body: failing_response())
    for head in ["a" * 40, "b" * 40]:
        args[args.index("--head") + 1] = head
        assert gate.main() == 1
    result = audit(Path(gate.VERDICT_DIR))
    assert len(result["proposals"]) == 1
    assert "  Preserve this exact failure.  " in result["proposals"][0]["body"]
    assert len(list(Path(gate.VERDICT_DIR).glob("*.json"))) == 2


def test_verdict_timestamp_is_utc_even_on_non_utc_host(gate, tmp_path, monkeypatch):
    import time

    api_fixture(gate, tmp_path, monkeypatch)
    monkeypatch.setattr(gate, "load_key", lambda: "synthetic-not-a-real-key")
    monkeypatch.setattr(gate, "api_call", lambda _key, _body: response())
    try:
        with monkeypatch.context() as local:
            local.setenv("TZ", "Etc/GMT-1")
            time.tzset()
            assert gate.main() == 0
            artifact = json.loads(next(Path(gate.VERDICT_DIR).glob("*.json")).read_text())
            assert artifact["ts"].endswith("+00:00")
    finally:
        time.tzset()


def test_nonprivate_output_directory_refused_before_credentials(gate, tmp_path, monkeypatch):
    api_fixture(gate, tmp_path, monkeypatch)
    Path(gate.VERDICT_DIR).mkdir(parents=True, mode=0o755)
    Path(gate.VERDICT_DIR).chmod(0o755)
    monkeypatch.setattr(gate, "load_key", lambda: pytest.fail("unsafe output reached credentials"))
    with pytest.raises(SystemExit):
        gate.main()


def test_default_regression_evidence_has_private_parent_and_fresh_name(
    gate, commits, tmp_path, monkeypatch
):
    from tools.review_support import regression

    repo, base, head = commits
    diff, _ = gate.pinned_diff(repo, base, head)
    path = tmp_path / "code.diff"
    path.write_text(diff)
    problem = tmp_path / "problem.txt"
    problem.write_text("Fix original defect")
    args = gate.parse_args(
        [
            "--purpose",
            "gate",
            "--repo-path",
            str(repo),
            "--base",
            base,
            "--head",
            head,
            "--diff",
            str(path),
            "--problem",
            str(problem),
            "--regression-config",
            str(tmp_path / "operator.json"),
            "--regression-test",
            "tests/test_fix.py",
            "--allow-regression-execution",
        ]
    )
    paths = []

    def verify(*values, **kwargs):
        destination = Path(values[5])
        assert destination.parent.is_dir()
        assert destination.parent.stat().st_mode & 0o777 == 0o700
        assert not destination.exists() and destination not in paths
        paths.append(destination)
        destination.write_text("{}")
        destination.chmod(0o600)
        return {"status": "PASS"}

    monkeypatch.setattr(regression, "verify_regression", verify)
    gate.prepare_review(args)
    gate.prepare_review(args)
    assert len(set(paths)) == 2


def test_synthesized_regression_finding_has_valid_audit_location(gate, tmp_path, monkeypatch):
    from tools.review_support.audit import _record

    api_fixture(gate, tmp_path, monkeypatch)
    monkeypatch.setattr(gate, "load_key", lambda: "synthetic-not-a-real-key")
    monkeypatch.setattr(gate, "api_call", lambda _key, _body: response("bugfix"))
    assert gate.main() == 1
    artifact = json.loads(next(Path(gate.VERDICT_DIR).glob("*.json")).read_text())
    # Local unpinned reviews intentionally lack a code identity; supply only a
    # synthetic identity to isolate the generated finding's real field schema.
    artifact.update(repository="example/project", head="a" * 40)
    assert artifact["parsed"]["findings"][-1]["file"] == "regression evidence"
    assert len(_record(artifact)[2]) == 1
