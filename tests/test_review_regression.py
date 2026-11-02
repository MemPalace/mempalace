"""Small real Git snapshots and a trusted adapter that executes Python unittest."""

import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.review_support import regression

IDENTITY = {
    "GIT_AUTHOR_NAME": "Synthetic contributor",
    "GIT_AUTHOR_EMAIL": "contributor@example.test",
    "GIT_COMMITTER_NAME": "Synthetic contributor",
    "GIT_COMMITTER_EMAIL": "contributor@example.test",
}

ADAPTER = r"""
import importlib.util
import json
import os
import sys
import time
import unittest
from pathlib import Path

request = json.load(sys.stdin)
root = Path(request["snapshot_cwd"])
assert root == Path.cwd()
assert set(request) == {"schema_version", "snapshot_cwd", "commit_sha", "selected_tests", "report_path", "suite"}
assert request["schema_version"] == 1
assert not any(name in os.environ for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GITHUB_TOKEN", "PYTHONPATH", "GIT_DIR"))
assert Path(os.environ["HOME"]) != root
sys.path.insert(0, str(root))
mode = "MODE"
if mode == "timeout":
    time.sleep(5)
if mode == "signal":
    os.kill(os.getpid(), 15)
if mode == "infra":
    sys.exit(3)

def phase(original, name):
    def call(test, *args):
        test.regression_phase = name
        return original(test, *args)
    return call

for method, name in (("_callSetUp", "setup"), ("_callTestMethod", "test"), ("_callTearDown", "teardown"), ("_callCleanup", "cleanup")):
    setattr(unittest.TestCase, method, phase(getattr(unittest.TestCase, method), name))

class Result(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.collected = []
        self.failed = []
        self.diagnostics = []

    def startTest(self, test):
        super().startTest(test)
        self.collected.append(self.canonical(test))

    def canonical(self, test):
        return f"{current}::{test.id()}"

    def addFailure(self, test, err):
        super().addFailure(test, err)
        node = self.canonical(test)
        self.failed.append(node)
        kind = "assertion" if test.regression_phase == "test" else test.regression_phase
        self.diagnostics.append({"id": node, "kind": kind, "message": self._exc_info_to_string(err, test)})

result = Result()
for current in request["selected_tests"]:
    if not Path(current).name.startswith("test_") or not current.endswith(".py"):
        continue  # Explicit fixtures are overlaid but are not executable tests.
    spec = importlib.util.spec_from_file_location("selected_test", root / current)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
        unittest.defaultTestLoader.loadTestsFromModule(module).run(result)
    except Exception:
        sys.exit(2)  # Collection/import/setup cannot become assertion proof.

counts = {
    "collected": len(result.collected),
    "passed": result.testsRun - len(result.failures) - len(result.errors) - len(result.skipped),
    "failed": len(result.failures),
    "skipped": len(result.skipped),
    "errors": len(result.errors),
}
report = {
    "schema_version": 1,
    "commit_sha": request["commit_sha"],
    "suite": request["suite"],
    "selected_tests": request["selected_tests"],
    "collected_ids": result.collected,
    "counts": counts,
    "failed_ids": result.failed,
    "failures": result.diagnostics,
}
if mode == "stale":
    report["commit_sha"] = "f" * 40
if mode == "suite":
    report["suite"] = {"profile": "other", "version": "1"}
if mode == "bool-count":
    report["counts"]["failed"] = True
if mode == "inconsistent":
    report["counts"]["passed"] += 1
if mode == "setup-kind" and result.failed:
    report["failures"][0]["kind"] = "setup"
if mode == "duplicate-id":
    report["collected_ids"] *= 2
if mode == "unselected-id":
    report["collected_ids"] = ["production.py::unrelated"]
if mode == "missing-diagnostic":
    report["failures"] = []
if mode == "different-ids" and not result.failed:
    report["collected_ids"] = [current + "::other_test"]
if mode == "selected-mismatch":
    report["selected_tests"] = []
if mode == "extra-key":
    report["unexpected"] = "forged"
if mode == "empty":
    report["collected_ids"] = []
    report["counts"] = dict.fromkeys(counts, 0)
    report["failed_ids"] = []
    report["failures"] = []
if mode == "mutate":
    (root / "production.py").write_text("def answer(): return 42\n")
output = Path(request["report_path"])
if mode == "symlink-report":
    output.unlink()
    output.symlink_to(root / "production.py")
elif mode == "oversized":
    output.write_text(" " * (1024 * 1024 + 1))
elif mode == "invalid-json":
    output.write_text("not json")
else:
    output.write_text(json.dumps(report))
sys.exit(0 if mode == "wrong-exit" else (1 if result.failures or result.errors else 0))
"""

TEST = """\
import unittest
from production import answer

class Regression(unittest.TestCase):
    def test_answer(self):
        self.assertEqual(answer(), 42)
"""


@unittest.skipUnless(os.name == "posix", "optional tooling targets POSIX hosts")
class RegressionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.repo = self.root / "checkout"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "commit.gpgsign", "false")
        self.write("production.py", "def answer(): return 0\n")
        self.write("tests/test_existing.py", "# existing fixture\n")
        self.base = self.commit()
        self.write("production.py", "def answer(): return 42\n")
        self.write("tests/test_regression.py", TEST)
        self.head = self.commit()
        self.selected = ["tests/test_regression.py"]
        self.adapter = self.root / "adapter.py"
        self.config = self.root / "runner.json"
        self.evidence = self.root / "evidence.json"
        self.cfg = {
            "schema_version": 1,
            "runner_argv": [str(self.adapter)],
            "allowed_test_roots": ["tests"],
            "suite": {"profile": "unittest", "version": "1"},
            "isolation": {
                "filesystem": "Controlled synthetic fixtures in disposable directories; no production sandbox.",
                "network": "Controlled test fixtures perform no network operations.",
                "processes": "Harness runs and waits for one unittest interpreter.",
            },
            "timeout_seconds": 3,
        }
        self.set_adapter()
        self.save_config()

    def git(self, *args):
        return (
            subprocess.check_output(
                ["git", "-C", str(self.repo), *args],
                env={
                    **os.environ,
                    **IDENTITY,
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CONFIG_GLOBAL": os.devnull,
                },
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
        )

    def write(self, relative, content):
        path = self.repo / relative
        path.parent.mkdir(exist_ok=True, parents=True)
        path.write_text(content)

    def commit(self):
        self.git("add", "--all")
        self.git("commit", "-qm", "test: synthetic regression fixture")
        return self.git("rev-parse", "HEAD")

    def save_config(self):
        self.config.write_text(json.dumps(self.cfg))
        self.config.chmod(0o600)

    def set_adapter(self, mode="normal"):
        self.adapter.write_text(
            f"#!{Path(sys.executable).resolve()}\n"
            + textwrap.dedent(ADAPTER).lstrip("\n").replace('mode = "MODE"', f"mode = {mode!r}")
        )
        self.adapter.chmod(0o700)

    def verify(self, **overrides):
        kwargs = {
            "repo_path": self.repo,
            "base_sha": self.base,
            "head_sha": self.head,
            "selected_tests": self.selected,
            "config_path": self.config,
            "evidence_path": self.evidence,
            "allow_execution": True,
        }
        kwargs.update(overrides)
        return regression.verify_regression(**kwargs)

    def blocked(self, **overrides):
        with self.assertRaises(regression.RegressionBlocked) as caught:
            self.verify(**overrides)
        self.assertEqual(caught.exception.evidence["status"], "BLOCKED")
        self.assertFalse(list(self.root.glob(".regression-*")), "temporary snapshots leaked")
        return caught.exception

    def test_new_test_really_fails_base_passes_head_and_checkout_is_untouched(self):
        self.write("production.py", "uncommitted caller content\n")
        self.write("untracked.txt", "caller scratch\n")
        before = self.git("status", "--porcelain=v1")
        with patch.dict(
            os.environ,
            {
                "ANTHROPIC_API_KEY": "must-not-inherit",
                "OPENAI_API_KEY": "must-not-inherit",
                "GITHUB_TOKEN": "must-not-inherit",
                "PYTHONPATH": "must-not-inherit",
            },
        ):
            evidence = self.verify()
        self.assertEqual(evidence["status"], "PASS")
        self.assertEqual(evidence["runs"]["base"]["report"]["counts"]["failed"], 1)
        self.assertEqual(evidence["runs"]["head"]["report"]["counts"]["passed"], 1)
        self.assertIn("0 != 42", evidence["runs"]["base"]["report"]["failures"][0]["message"])
        self.assertEqual(
            evidence["snapshots"]["base"]["selected_sha256"][self.selected[0]],
            hashlib.sha256(TEST.encode()).hexdigest(),
        )
        self.assertEqual(
            evidence["config_sha256"], hashlib.sha256(self.config.read_bytes()).hexdigest()
        )
        self.assertEqual(self.git("status", "--porcelain=v1"), before)
        self.assertEqual((self.repo / "production.py").read_text(), "uncommitted caller content\n")
        self.assertEqual(json.loads(self.evidence.read_text()), evidence)
        self.assertEqual(stat.S_IMODE(self.evidence.stat().st_mode), 0o600)
        self.assertFalse(list(self.root.glob(".regression-*")))

    def test_modified_test_and_added_fixture_are_overlaid_together(self):
        self.write(
            "tests/test_existing.py", TEST.replace("42)", "int(open('tests/expected.txt').read()))")
        )
        self.write("tests/expected.txt", "42")
        self.head = self.commit()
        self.selected = ["tests/test_existing.py", "tests/expected.txt"]
        self.assertEqual(self.verify()["status"], "PASS")

    def test_both_pass_is_not_a_regression(self):
        self.write(self.selected[0], TEST.replace("42)", "answer())"))
        self.head = self.commit()
        self.assertIn("both-pass", str(self.blocked()))

    def test_head_failure_refused(self):
        self.write("production.py", "def answer(): return 7\n")
        self.head = self.commit()
        self.assertIn("HEAD", str(self.blocked()))

    def test_base_import_error_is_not_assertion_proof(self):
        self.write(
            self.selected[0],
            TEST.replace(
                "from production import answer", "from production import answer, only_head"
            ),
        )
        self.write("production.py", "def answer(): return 42\nonly_head = True\n")
        self.head = self.commit()
        self.assertIn("infrastructure", str(self.blocked()))

    def test_base_test_error_is_not_assertion_proof(self):
        self.write(
            self.selected[0],
            TEST.replace("self.assertEqual(answer(), 42)", "self.assertEqual(42 / answer(), 1)"),
        )
        self.head = self.commit()
        self.assertIn("skip/error", str(self.blocked()))

    def test_setup_failure_is_not_assertion_proof(self):
        self.write(
            self.selected[0],
            TEST.replace(
                "    def test_answer(self):",
                "    def setUp(self):\n        raise RuntimeError('setup error')\n\n    def test_answer(self):",
            ),
        )
        self.head = self.commit()
        self.blocked()

    def test_skip_refused(self):
        self.write(
            self.selected[0],
            TEST.replace(
                "    def test_answer(self):",
                "    @unittest.skip('unsupported')\n    def test_answer(self):",
            ),
        )
        self.head = self.commit()
        self.assertIn("skip/error", str(self.blocked()))

    def test_setup_and_teardown_assertions_are_not_regression_proof(self):
        for method in ("setUp", "tearDown"):
            with self.subTest(method=method):
                self.evidence.unlink(missing_ok=True)
                source = TEST.replace("    def test_answer(self):", f"    def {method}(self):")
                source += "\n    def test_answer(self):\n        self.assertTrue(True)\n"
                self.write(self.selected[0], source)
                self.head = self.commit()
                self.assertIn("non-assertion", str(self.blocked()))

    def test_forged_stale_infra_or_inconsistent_reports_refused(self):
        for mode, reason in {
            "stale": "stale report",
            "suite": "stale report",
            "bool-count": "invalid counts",
            "inconsistent": "inconsistent counts",
            "setup-kind": "non-assertion",
            "duplicate-id": "duplicate collected ID",
            "unselected-id": "invalid/unselected",
            "missing-diagnostic": "missing failure diagnostics",
            "different-ids": "collected IDs differ",
            "selected-mismatch": "selected paths mismatch",
            "extra-key": "report schema",
            "empty": "empty/oversized collection",
            "wrong-exit": "BASE must",
            "infra": "infrastructure",
            "signal": "infrastructure",
            "mutate": "snapshot file mutation",
            "symlink-report": "regular file",
            "oversized": "oversized",
            "invalid-json": "Expecting value",
        }.items():
            with self.subTest(mode=mode):
                self.evidence.unlink(missing_ok=True)
                self.set_adapter(mode)
                self.assertIn(reason, str(self.blocked()))

    def test_timeout_cleans_snapshots_and_records_private_block(self):
        self.set_adapter("timeout")
        self.cfg["timeout_seconds"] = 1
        self.save_config()
        self.assertIn("timed out", str(self.blocked()))
        self.assertEqual(json.loads(self.evidence.read_text())["status"], "BLOCKED")
        self.assertEqual(stat.S_IMODE(self.evidence.stat().st_mode), 0o600)

    def test_bad_selections_never_execute_adapter(self):
        self.set_adapter("timeout")
        for selected in (
            [],
            ["production.py"],
            ["../test.py"],
            ["/tests/test.py"],
            ["tests/../production.py"],
            ["tests/.git/config"],
            ["tests\\test.py"],
            ["tests/test_existing.py"],
            ["tests/missing.py"],
            self.selected * 2,
        ):
            with self.subTest(selected=selected):
                self.evidence.unlink(missing_ok=True)
                self.blocked(selected_tests=selected)

    def test_helper_outside_allowed_roots_refused(self):
        self.write("helpers/fixture.py", "expected = 42\n")
        self.head = self.commit()
        self.blocked(selected_tests=self.selected + ["helpers/fixture.py"])

    def test_deleted_file_refused(self):
        (self.repo / "tests/test_existing.py").unlink()
        self.head = self.commit()
        self.blocked(selected_tests=["tests/test_existing.py"])

    def test_symlink_and_submodule_entries_refused(self):
        (self.repo / "tests/link.py").symlink_to("../production.py")
        self.head = self.commit()
        self.blocked(selected_tests=["tests/link.py"])
        self.evidence.unlink()
        (self.repo / "tests/link.py").unlink()
        self.git("add", "--all")
        self.git("update-index", "--add", "--cacheinfo", f"160000,{self.base},tests/submodule")
        self.git("commit", "-qm", "test: synthetic gitlink")
        self.head = self.git("rev-parse", "HEAD")
        self.blocked(selected_tests=["tests/submodule"])

    def test_unpinned_noncommit_and_unknown_shas_refused(self):
        for sha in (self.base[:12], "HEAD", "0" * 40, self.git("rev-parse", "HEAD^{tree}")):
            with self.subTest(sha=sha):
                self.evidence.unlink(missing_ok=True)
                self.blocked(base_sha=sha)

    def test_opt_in_and_trusted_configuration_required(self):
        self.blocked(allow_execution=False)
        for key, value in (
            ("schema_version", 2),
            ("isolation", {}),
            ("isolation", {"filesystem": "", "network": "off", "processes": "isolated"}),
            ("runner_argv", ["python", str(self.adapter)]),
            ("runner_argv", [str(self.adapter), "tests/test_regression.py"]),
            ("timeout_seconds", True),
            ("allowed_test_roots", ["."]),
        ):
            with self.subTest(key=key, value=value):
                self.evidence.unlink(missing_ok=True)
                original = self.cfg[key]
                self.cfg[key] = value
                self.save_config()
                self.blocked()
                self.cfg[key] = original
        self.save_config()
        self.evidence.unlink()
        self.config.chmod(0o666)
        self.blocked()

    def test_config_and_adapter_inside_any_checkout_refused(self):
        for kind in ("config", "adapter"):
            with self.subTest(kind=kind):
                self.evidence.unlink(missing_ok=True)
                self.write("private/runner.json", self.config.read_text())
                if kind == "config":
                    self.blocked(config_path=self.repo / "private/runner.json")
                else:
                    self.write("private/adapter.py", self.adapter.read_text())
                    self.cfg["runner_argv"][0] = str(self.repo / "private/adapter.py")
                    self.save_config()
                    self.blocked()

    def test_group_writable_executable_adapter_is_refused(self):
        self.adapter.chmod(0o777)
        self.assertIn("group/world writable", str(self.blocked()))

    def test_evidence_inside_checkout_snapshot_or_symlink_refused(self):
        self.blocked(evidence_path=self.repo / "evidence.json")
        other = self.root / "other_checkout"
        other.mkdir()
        (other / ".git").mkdir()
        self.blocked(evidence_path=other / "evidence.json")
        link = self.root / "linked"
        link.symlink_to(self.repo, target_is_directory=True)
        self.blocked(evidence_path=link / "evidence.json")
        previous = self.root / "unrelated" / ".regression-previous" / "base"
        previous.mkdir(parents=True)
        self.assertIn(
            "regression workspace", str(self.blocked(evidence_path=previous / "evidence.json"))
        )
        # Temp workspaces are created below the evidence parent, so a caller
        # cannot select their fresh snapshot path as its evidence destination.
        with patch.object(regression, "_run") as run:

            def reject_snapshot(cfg, snapshot, *args):
                self.assertNotIn(snapshot, self.evidence.parents)
                raise regression.RegressionBlocked("synthetic adapter refusal")

            run.side_effect = reject_snapshot
            self.blocked()
        self.assertFalse((self.repo / "evidence.json").exists())

    def test_artifact_symlink_swap_cannot_read_an_external_file(self):
        original = regression._regular
        secret = self.root / "must-not-read.json"
        secret.write_text('{"must_not_read": true}')

        def swap(path, **kwargs):
            info = original(path, **kwargs)
            if path.name == "base.json":
                path.unlink()
                path.symlink_to(secret)
            return info

        with patch.object(regression, "_regular", side_effect=swap):
            self.assertIn("symbolic", str(self.blocked()).lower())

    def test_caller_written_evidence_is_never_accepted_or_overwritten(self):
        self.evidence.write_text('{"status": "PASS"}')
        self.blocked()
        self.assertEqual(self.evidence.read_text(), '{"status": "PASS"}')

    def test_symlinked_config_and_unsafe_evidence_directory_refused(self):
        linked = self.root / "linked.json"
        linked.symlink_to(self.config)
        self.blocked(config_path=linked)
        directory = self.root / "shared"
        directory.mkdir(mode=0o777)
        directory.chmod(0o777)
        self.blocked(evidence_path=directory / "evidence.json")

    def test_duplicate_json_keys_and_oversized_config_refused(self):
        self.config.write_text(
            self.config.read_text().replace(
                '"schema_version": 1', '"schema_version": 2, "schema_version": 1'
            )
        )
        self.blocked()
        self.evidence.unlink()
        self.config.write_text(" " * (64 * 1024 + 1))
        self.blocked()


if __name__ == "__main__":
    unittest.main()
