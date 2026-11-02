"""Pinned regression execution through a trusted external adapter (see REGRESSION.md)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

_HEX = re.compile(r"[0-9a-fA-F]{40}\Z")
_MAX_REPORT = 1024 * 1024
_MAX_FILES = 50_000
_MAX_BLOB = 32 * 1024 * 1024
_MAX_TREE = 256 * 1024 * 1024


class RegressionBlocked(Exception):
    """No regression proof was established; evidence is private when available."""

    def __init__(self, message, *, evidence=None):
        super().__init__(message)
        self.evidence = evidence


def _require(condition, message):
    if not condition:
        raise RegressionBlocked(message)


def _relative(value):
    _require(isinstance(value, str) and 0 < len(value) <= 4096, "invalid relative path")
    parts = PurePosixPath(value).parts
    _require(
        parts
        and not value.startswith("/")
        and PurePosixPath(value).as_posix() == value
        and all(p not in (".", "..") and p.casefold() != ".git" for p in parts)
        and "\\" not in value
        and ":" not in value
        and value.isprintable(),
        "unsafe relative path",
    )
    return value


def _absolute(value):
    path = Path(value).absolute()
    _require(path == path.resolve(), "symlink or noncanonical external path")
    return path


def _outside_checkout(path):
    for parent in (path, *path.parents):
        _require(
            not parent.name.startswith(".regression-"),
            "external path cannot be inside a regression workspace",
        )
        _require(
            not (parent / ".git").exists()
            and not (parent / ".git").is_symlink()
            and not (
                (parent / "HEAD").is_file()
                and (parent / "objects").is_dir()
                and (parent / "refs").is_dir()
            ),
            "external path must be outside every Git checkout/repository",
        )


def _regular(path, *, trusted=False):
    info = path.lstat()
    _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "expected regular file")
    if trusted:
        _require(not info.st_mode & 0o022, "trusted file is group/world writable")
        if hasattr(os, "getuid"):
            _require(info.st_uid in (0, os.getuid()), "trusted file has another owner")
    return info


def _read_json(path, limit, *, trusted=False):
    info = _regular(path, trusted=trusted)
    _require(0 < info.st_size <= limit, "JSON artifact is empty or oversized")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as source:
        opened = os.fstat(source.fileno())
        _require(
            (opened.st_dev, opened.st_ino) == (info.st_dev, info.st_ino)
            and stat.S_ISREG(opened.st_mode)
            and opened.st_nlink == 1,
            "JSON artifact changed during read",
        )
        raw = source.read(limit + 1)
    _require(len(raw) <= limit, "JSON artifact is oversized")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result, "duplicate JSON key")
            result[key] = value
        return result

    value = json.loads(raw, object_pairs_hook=unique)
    _require(isinstance(value, dict), "JSON artifact must be an object")
    return value, hashlib.sha256(raw).hexdigest()


def _keys(value, keys, description):
    _require(isinstance(value, dict) and set(value) == set(keys), f"invalid {description} schema")


def _text(value, maximum=512):
    return isinstance(value, str) and 0 < len(value) <= maximum and value.isprintable()


def _config(path):
    _outside_checkout(path)
    cfg, digest = _read_json(path, 64 * 1024, trusted=True)
    _keys(
        cfg,
        (
            "schema_version",
            "runner_argv",
            "allowed_test_roots",
            "suite",
            "isolation",
            "timeout_seconds",
        ),
        "runner config",
    )
    _require(
        type(cfg["schema_version"]) is int and cfg["schema_version"] == 1,
        "unsupported config version",
    )
    argv = cfg["runner_argv"]
    _require(
        isinstance(argv, list) and 1 <= len(argv) <= 32 and all(_text(a, 4096) for a in argv),
        "invalid runner argv",
    )
    _require(Path(argv[0]).is_absolute(), "runner executable must be absolute")
    # Version 1 accepts an executable plus external file operands, not inline
    # commands, module names or paths interpreted relative to the snapshot.
    for item in argv:
        _require(Path(item).is_absolute(), "runner argv must contain absolute external files")
        external = _absolute(item)
        _outside_checkout(external)
        _regular(external, trusted=True)
    _require(os.access(argv[0], os.X_OK), "runner is not executable")
    roots = cfg["allowed_test_roots"]
    _require(isinstance(roots, list) and 1 <= len(roots) <= 32, "invalid allowed test roots")
    for root in roots:
        _relative(root)
    _require(len(set(roots)) == len(roots), "duplicate allowed test root")
    _keys(cfg["suite"], ("profile", "version"), "suite")
    _require(all(_text(v) for v in cfg["suite"].values()), "invalid suite identity")
    _keys(cfg["isolation"], ("filesystem", "network", "processes"), "isolation declaration")
    _require(
        all(_text(v, 2048) for v in cfg["isolation"].values()), "missing isolation declaration"
    )
    _require(
        type(cfg["timeout_seconds"]) is int and 1 <= cfg["timeout_seconds"] <= 300,
        "invalid runner timeout",
    )
    return cfg, digest


def _environment(home=None, temporary=None):
    env = {
        "PATH": os.defpath,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if home is not None:
        env["HOME"] = str(home)
        env["TMPDIR"] = str(temporary)
        env["TMP"] = str(temporary)
        env["TEMP"] = str(temporary)
    return env


def _git(repo, *args, data=None):
    executable = shutil.which("git", path=os.defpath)
    _require(executable is not None, "Git is unavailable")
    result = subprocess.run(
        [executable, "--no-replace-objects", "-C", str(repo), *args],
        input=data,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=_environment(),
        timeout=30,
        check=False,
    )
    _require(result.returncode == 0, "Git object read failed")
    return result.stdout


def _repository(path, base, head):
    repo = _absolute(path)
    _require(repo.is_dir(), "repository does not exist")
    _require(
        _git(repo, "rev-parse", "--is-bare-repository").strip() == b"false",
        "bare repository unsupported",
    )
    _require(
        Path(os.fsdecode(_git(repo, "rev-parse", "--show-toplevel").strip())) == repo,
        "repo_path must be the checkout root",
    )
    for sha in (base, head):
        _require(isinstance(sha, str) and _HEX.fullmatch(sha), "commits require full 40-hex SHAs")
        _require(
            _git(repo, "cat-file", "-t", sha).strip() == b"commit", "SHA must name a commit object"
        )
        _require(
            _git(repo, "rev-parse", "--verify", f"{sha}^{{commit}}").strip().decode()
            == sha.lower(),
            "commit resolution mismatch",
        )
    return repo


def _tree(repo, sha):
    files = {}
    total = 0
    records = _git(repo, "ls-tree", "-r", "-l", "-z", "--full-tree", sha).split(b"\0")
    _require(len(records) <= _MAX_FILES + 1, "snapshot has too many files")
    for record in records:
        if not record:
            continue
        header, name = record.split(b"\t", 1)
        mode, kind, oid, size = header.split()
        _require(
            mode in (b"100644", b"100755") and kind == b"blob",
            "symlink/submodule or unsupported Git entry",
        )
        name = _relative(name.decode("utf-8"))
        length = int(size)
        total += length
        _require(length <= _MAX_BLOB and total <= _MAX_TREE, "snapshot size limit exceeded")
        _require(name not in files, "duplicate snapshot path")
        files[name] = (mode.decode(), oid.decode(), length)
    return files


def _select(selected, roots, base, head):
    _require(
        isinstance(selected, (list, tuple)) and 1 <= len(selected) <= 256,
        "explicit selected test paths required",
    )
    for name in selected:
        _relative(name)
        _require(
            any(name.startswith(root + "/") for root in roots),
            "selected path outside allowed test roots",
        )
        _require(name in head, "selected path is not a head regular file")
        _require(base.get(name) != head[name], "selected path is unchanged from base")
    _require(len(set(selected)) == len(selected), "duplicate selected path")
    return list(selected)


def _snapshot(repo, files, destination):
    destination.mkdir(mode=0o700)
    entries = list(files.items())
    raw = _git(
        repo, "cat-file", "--batch", data=b"".join(info[1].encode() + b"\n" for _, info in entries)
    )
    offset = 0
    hashes = {}
    for name, (mode, oid, size) in entries:
        end = raw.index(b"\n", offset)
        _require(raw[offset:end] == f"{oid} blob {size}".encode(), "Git blob header mismatch")
        offset = end + 1
        content = raw[offset : offset + size]
        offset += size
        _require(len(content) == size and raw[offset : offset + 1] == b"\n", "truncated Git blob")
        offset += 1
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        target.chmod(0o755 if mode == "100755" else 0o644)
        hashes[name] = hashlib.sha256(content).hexdigest()
    _require(offset == len(raw), "unexpected Git blob output")
    return hashes


def _unchanged(snapshot, files, hashes):
    for name, (mode, _, size) in files.items():
        path = snapshot / name
        # Resolve every parent as well as the file, preventing a redirected read.
        _require(path.resolve() == path, "snapshot path mutation")
        info = _regular(path)
        _require(
            info.st_size == size and bool(info.st_mode & 0o111) == (mode == "100755"),
            "snapshot file mutation",
        )
        _require(
            hashlib.sha256(path.read_bytes()).hexdigest() == hashes[name],
            "snapshot content mutation",
        )


def _report(path, sha, selected, suite):
    report, _ = _read_json(path, _MAX_REPORT)
    _keys(
        report,
        (
            "schema_version",
            "commit_sha",
            "suite",
            "selected_tests",
            "collected_ids",
            "counts",
            "failed_ids",
            "failures",
        ),
        "runner report",
    )
    _require(
        type(report["schema_version"]) is int and report["schema_version"] == 1,
        "unsupported report version",
    )
    _require(
        report["commit_sha"] == sha and report["suite"] == suite, "stale report revision or suite"
    )
    _require(report["selected_tests"] == selected, "report selected paths mismatch")
    ids = report["collected_ids"]
    _require(isinstance(ids, list) and 1 <= len(ids) <= 10_000, "empty/oversized collection")
    _require(
        all(
            _text(i, 2048) and "::" in i and i.split("::", 1)[0] in selected and i.split("::", 1)[1]
            for i in ids
        ),
        "invalid/unselected collected ID",
    )
    _require(len(set(ids)) == len(ids), "duplicate collected ID")
    counts = report["counts"]
    _keys(counts, ("collected", "passed", "failed", "skipped", "errors"), "counts")
    _require(all(type(n) is int and 0 <= n <= 10_000 for n in counts.values()), "invalid counts")
    _require(
        counts["collected"]
        == len(ids)
        == sum(counts[k] for k in ("passed", "failed", "skipped", "errors")),
        "inconsistent counts",
    )
    failed = report["failed_ids"]
    _require(
        isinstance(failed, list) and all(isinstance(i, str) for i in failed), "invalid failed IDs"
    )
    _require(
        len(set(failed)) == len(failed) == counts["failed"] and set(failed) <= set(ids),
        "inconsistent failed IDs",
    )
    failures = report["failures"]
    _require(isinstance(failures, list) and len(failures) <= 10_000, "invalid diagnostics")
    for failure in failures:
        _keys(failure, ("id", "kind", "message"), "failure")
        _require(
            failure["id"] in failed
            and failure["kind"] == "assertion"
            and isinstance(failure["message"], str)
            and 0 < len(failure["message"]) <= 16_384,
            "non-assertion or invalid failure diagnostic",
        )
    _require(
        len(failures) == len(failed) and {f["id"] for f in failures} == set(failed),
        "missing failure diagnostics",
    )
    _require(counts["skipped"] == counts["errors"] == 0, "skip/error cannot prove a regression")
    return report


def _run(cfg, snapshot, sha, selected, report_path, home, temporary):
    report_path.touch(mode=0o600, exist_ok=False)
    request = {
        "schema_version": 1,
        "snapshot_cwd": str(snapshot),
        "commit_sha": sha,
        "selected_tests": selected,
        "report_path": str(report_path),
        "suite": cfg["suite"],
    }
    process = subprocess.Popen(
        cfg["runner_argv"],
        cwd=snapshot,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=_environment(home, temporary),
        start_new_session=True,
    )
    try:
        process.communicate(json.dumps(request).encode(), timeout=cfg["timeout_seconds"])
    except BaseException:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
        process.wait()
        raise
    _require(process.returncode in (0, 1), "runner infrastructure failure/signal")
    return {
        "exit_code": process.returncode,
        "report": _report(report_path, sha, selected, cfg["suite"]),
    }


def _output(path):
    destination = _absolute(path)
    _outside_checkout(destination)
    _require(destination.parent.is_dir(), "evidence parent must exist")
    _require(
        not destination.exists() and not destination.is_symlink(),
        "evidence destination already exists",
    )
    parent = destination.parent.stat()
    _require(not parent.st_mode & 0o022, "unsafe evidence directory permissions")
    if hasattr(os, "getuid"):
        _require(parent.st_uid in (0, os.getuid()), "evidence directory has another owner")
    return destination


def _write_evidence(destination, evidence):
    fd, temporary = tempfile.mkstemp(prefix=".regression-evidence-", dir=destination.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(evidence, output, sort_keys=True, indent=2, allow_nan=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        # Atomic publication without replacing any existing caller-written file.
        os.link(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


def verify_regression(
    repo_path,
    base_sha,
    head_sha,
    selected_tests,
    config_path,
    evidence_path,
    *,
    allow_execution=False,
):
    """Execute pinned BASE/HEAD tests or raise RegressionBlocked; never load credentials.

    BASE must already be the caller's pinned merge-base. The adapter contract,
    strict result schema and isolation trust boundary are in REGRESSION.md.
    """
    evidence = {"schema_version": 1, "status": "BLOCKED"}
    destination = None
    try:
        destination = _output(evidence_path)
        _require(allow_execution is True, "runner execution requires explicit per-run opt-in")
        repo = _repository(repo_path, base_sha, head_sha)
        base_sha, head_sha = base_sha.lower(), head_sha.lower()
        cfg, digest = _config(_absolute(config_path))
        evidence.update(
            repo_path=str(repo),
            base_sha=base_sha,
            head_sha=head_sha,
            config_sha256=digest,
            suite=cfg["suite"],
            isolation=cfg["isolation"],
        )
        base = _tree(repo, base_sha)
        head = _tree(repo, head_sha)
        selected = _select(selected_tests, cfg["allowed_test_roots"], base, head)
        overlay = dict(base)
        overlay.update({name: head[name] for name in selected})
        evidence["selected_tests"] = selected
        evidence["trees"] = {
            "base": _git(repo, "rev-parse", f"{base_sha}^{{tree}}").strip().decode(),
            "head": _git(repo, "rev-parse", f"{head_sha}^{{tree}}").strip().decode(),
        }
        with tempfile.TemporaryDirectory(
            prefix=".regression-", dir=destination.parent
        ) as directory:
            work = Path(directory)
            for label, sha, files in (("base", base_sha, overlay), ("head", head_sha, head)):
                home, temporary = work / f"{label}-home", work / f"{label}-tmp"
                home.mkdir(mode=0o700)
                temporary.mkdir(mode=0o700)
                snapshot = work / label
                hashes = _snapshot(repo, files, snapshot)
                evidence.setdefault("snapshots", {})[label] = {
                    "manifest_sha256": hashlib.sha256(
                        json.dumps(
                            {name: [files[name][0], value] for name, value in hashes.items()},
                            sort_keys=True,
                        ).encode()
                    ).hexdigest(),
                    "selected_sha256": {name: hashes[name] for name in selected},
                }
                result = _run(
                    cfg,
                    snapshot,
                    sha,
                    selected,
                    work / f"{label}.json",
                    home,
                    temporary,
                )
                evidence.setdefault("runs", {})[label] = result
                _unchanged(snapshot, files, hashes)
            old, new = (evidence["runs"][label] for label in ("base", "head"))
            _require(
                set(old["report"]["collected_ids"]) == set(new["report"]["collected_ids"]),
                "base/head collected IDs differ",
            )
            _require(
                old["exit_code"] == 1 and old["report"]["counts"]["failed"] > 0,
                "BASE must have real assertion failures; both-pass is blocked",
            )
            _require(
                new["exit_code"] == 0
                and new["report"]["counts"]["failed"] == 0
                and new["report"]["counts"]["passed"] == new["report"]["counts"]["collected"],
                "HEAD must pass every selected test",
            )
        evidence["status"] = "PASS"
        evidence["evidence_path"] = str(destination)
        _write_evidence(destination, evidence)
        return evidence
    except (
        RegressionBlocked,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as error:
        evidence["status"] = "BLOCKED"
        evidence["reason"] = str(error)[:4096]
        if destination is not None:
            try:
                _write_evidence(destination, evidence)
            except OSError:
                evidence["reason"] += "; private evidence could not be published"
        raise RegressionBlocked(evidence["reason"], evidence=evidence) from error
