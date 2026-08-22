"""Non-regular files must never wedge an ingest command.

``os.walk``/``rglob`` list a FIFO, a socket and a device node as ordinary
filenames, and MemPalace decides what to read by extension. Opening a FIFO
for reading parks in the kernel until a writer appears, so a named pipe
called ``notes.md`` sitting in a mined directory used to hang ``mine``,
``sweep`` and ``init`` forever — no output, no error, no progress.

Every check here is wrapped in :func:`hard_timeout`. A regression must turn
this file red; it must not hang the suite (an unbounded blocking open would
otherwise stall pytest itself, which reports as "still running", not as a
failure).
"""

import argparse
import errno
import hashlib
import os
import signal
import socket
import stat as stat_module
import threading
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from mempalace.cli import (
    _ensure_mempalace_files_gitignored,
    _gather_origin_samples,
    cmd_compress,
    cmd_init,
)
from mempalace.convo_miner import _is_regular_source_file, scan_convos
from mempalace.entity_detector import detect_entities
from mempalace.format_miner import ExtractionStatus, extract_text
from mempalace.hook_shell import count_human_messages
from mempalace.llm_refine import collect_corpus_text
from mempalace.miner import _read_text_no_follow, load_config, mine, scan_project
from mempalace.normalize import _read_transcript_file
from mempalace.project_scanner import _collect_manifest_names, _parse_gradle
from mempalace.repair import _copy_file_no_follow, _open_regular_file_no_follow
from mempalace.room_detector_local import detect_rooms_local
from mempalace.split_mega_files import main as split_main
from mempalace.split_mega_files import split_file
from mempalace.sweeper import parse_claude_jsonl, sweep_directory

# ``os.mkfifo`` and ``SIGALRM`` are both POSIX-only. Windows has no FIFO in
# the filesystem namespace at all (its named pipes live under \\.\pipe\ and
# no directory walk can reach them), so there is nothing to reproduce there.
posix_only = pytest.mark.skipif(
    not hasattr(os, "mkfifo") or not hasattr(signal, "SIGALRM"),
    reason="requires POSIX FIFOs and SIGALRM",
)

# Root holds CAP_DAC_OVERRIDE and walks straight into a directory with no
# ``x`` bit, so the file each test walls off stays readable and the assertion
# below breaks: the state these tests need cannot be built as root, they do
# not merely pass vacuously there. ``tests/test_backups.py`` gates the same
# way and additionally excludes Windows, which it has to because it carries
# no ``posix_only``; every use here already sits under ``posix_only``.
needs_unprivileged_posix = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="directory permission bits do not gate root",
)

TIMEOUT_SECONDS = 10.0


class Blocked(BaseException):
    """Raised when a call under test blocks past the deadline.

    Deliberately derived from ``BaseException`` rather than ``Exception``.
    Two separate handlers would otherwise eat the deadline and leave a
    reverted fix looking green while it blocked for the full timeout:

    * ``except OSError`` guards every read site here, and ``TimeoutError``
      *is* an ``OSError`` — that one alone cost four vacuous passes.
    * ``except Exception`` guards the paths the end-to-end tests cross,
      including ``sweeper.sweep_directory`` and four sites inside
      ``miner._mine_impl``, so a plain ``Exception`` subclass would be
      swallowed there just as thoroughly.
    """


@contextmanager
def hard_timeout(seconds: float, what: str):
    """Fail the test instead of blocking forever.

    ``signal.setitimer`` fires SIGALRM even while the interpreter sits in a
    blocking ``open(2)``; the handler raises, and PEP 475 propagates that
    exception rather than restarting the syscall. Without this, reverting
    the fix would hang pytest rather than fail it.
    """

    def _fire(signum, frame):
        raise Blocked(f"{what} blocked for more than {seconds}s")

    previous = signal.signal(signal.SIGALRM, _fire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def make_fifo(directory: Path, name: str) -> Path:
    path = directory / name
    os.mkfifo(path)
    return path


def write_regular(directory: Path, name: str, content: str) -> Path:
    path = directory / name
    path.write_text(content, encoding="utf-8")
    return path


# ─────────────────────────────────────────────────────────────────────────
# The four os.open read sites: the type check must be reachable
# ─────────────────────────────────────────────────────────────────────────


@posix_only
def test_read_text_no_follow_rejects_fifo(tmp_path):
    fifo = make_fifo(tmp_path, "notes.md")
    with hard_timeout(TIMEOUT_SECONDS, "_read_text_no_follow on a FIFO"):
        assert _read_text_no_follow(fifo, tmp_path) is None


@posix_only
def test_is_regular_source_file_rejects_fifo(tmp_path):
    fifo = make_fifo(tmp_path, "session.jsonl")
    with hard_timeout(TIMEOUT_SECONDS, "_is_regular_source_file on a FIFO"):
        assert _is_regular_source_file(fifo, tmp_path) is False


@posix_only
def test_read_transcript_file_rejects_fifo(tmp_path):
    fifo = make_fifo(tmp_path, "session.jsonl")
    with hard_timeout(TIMEOUT_SECONDS, "_read_transcript_file on a FIFO"):
        with pytest.raises(IOError) as excinfo:
            _read_transcript_file(str(fifo))
    message = str(excinfo.value)
    assert "not a regular file" in message
    # The path belongs in the message exactly once — the raise inside the
    # try block is prefix-free so the wrapper composes it.
    assert message.count(str(fifo)) == 1


@posix_only
def test_open_regular_file_no_follow_rejects_fifo(tmp_path):
    fifo = make_fifo(tmp_path, "chroma.sqlite3")
    with hard_timeout(TIMEOUT_SECONDS, "_open_regular_file_no_follow on a FIFO"):
        with pytest.raises(RuntimeError, match="Refusing non-regular file"):
            _open_regular_file_no_follow(str(fifo))


@posix_only
def test_copy_file_no_follow_rejects_fifo_source(tmp_path):
    fifo = make_fifo(tmp_path, "chroma.sqlite3")
    with hard_timeout(TIMEOUT_SECONDS, "_copy_file_no_follow from a FIFO"):
        with pytest.raises(RuntimeError, match="Refusing non-regular file"):
            _copy_file_no_follow(str(fifo), str(tmp_path / "backup.sqlite3"))
    assert not (tmp_path / "backup.sqlite3").exists()


@posix_only
def test_read_text_no_follow_rejects_fifo_that_has_a_live_writer(tmp_path):
    """The verdict comes from the file type, not from "nobody is writing".

    With a writer attached the open succeeds even without ``O_NONBLOCK``, so
    this is not the hang regression — it pins the gate itself. Anyone who
    "fixes" the hang by swallowing an errno instead of checking ``S_ISREG``
    turns this red, and a pipe whose content would otherwise be mined as a
    verbatim drawer stays out of the palace.
    """
    fifo = make_fifo(tmp_path, "notes.md")
    writer_attached = threading.Event()
    release_writer = threading.Event()
    failures = []

    def _hold_write_end():
        try:
            fd = os.open(fifo, os.O_WRONLY)  # blocks until a reader opens
        except OSError as exc:  # pragma: no cover - only on a broken setup
            failures.append(exc)
            writer_attached.set()
            return
        writer_attached.set()
        release_writer.wait(TIMEOUT_SECONDS)
        os.close(fd)

    thread = threading.Thread(target=_hold_write_end, daemon=True)
    thread.start()
    # Opening our own read end is what lets the writer's open(2) return.
    reader_fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    try:
        assert writer_attached.wait(TIMEOUT_SECONDS), "writer never attached"
        assert not failures, failures
        with hard_timeout(TIMEOUT_SECONDS, "_read_text_no_follow on a written FIFO"):
            assert _read_text_no_follow(fifo, tmp_path) is None
    finally:
        release_writer.set()
        os.close(reader_fd)
        thread.join(TIMEOUT_SECONDS)


# ─────────────────────────────────────────────────────────────────────────
# O_NONBLOCK must not change what a regular file reads back
# ─────────────────────────────────────────────────────────────────────────


@posix_only
def test_read_text_no_follow_still_reads_a_large_regular_file_whole(tmp_path):
    """POSIX and Linux open(2) both say O_NONBLOCK has no effect on regular
    files. This pins that: 2 MB is many buffered reads, and a short read or
    an ``EAGAIN`` would truncate a drawer silently.
    """
    payload = "the quick brown fox jumps over the lazy dog\n" * 50_000
    regular = write_regular(tmp_path, "big.md", payload)
    with hard_timeout(TIMEOUT_SECONDS, "_read_text_no_follow on a 2 MB file"):
        result = _read_text_no_follow(regular, tmp_path)
    assert result is not None
    content, mtime = result
    assert content == payload
    assert mtime == os.path.getmtime(regular)


@posix_only
def test_copy_file_no_follow_still_copies_a_large_regular_file_byte_for_byte(tmp_path):
    """The palace backup path reads through the same non-blocking fd."""
    payload = os.urandom(2 * 1024 * 1024)
    src = tmp_path / "chroma.sqlite3"
    src.write_bytes(payload)
    dst = tmp_path / "chroma.sqlite3.backup"
    with hard_timeout(TIMEOUT_SECONDS, "_copy_file_no_follow of a 2 MB file"):
        _copy_file_no_follow(str(src), str(dst))
    assert hashlib.sha256(dst.read_bytes()).hexdigest() == hashlib.sha256(payload).hexdigest()


# ─────────────────────────────────────────────────────────────────────────
# Discovery walks must not hand a non-regular file to a reader
# ─────────────────────────────────────────────────────────────────────────


@posix_only
def test_scan_project_skips_fifo_and_keeps_regular_files(tmp_path, capsys):
    make_fifo(tmp_path, "notes.md")
    write_regular(tmp_path, "real.md", "# Real\n")
    with hard_timeout(TIMEOUT_SECONDS, "scan_project over a FIFO"):
        found = scan_project(str(tmp_path))
    assert [path.name for path in found] == ["real.md"]
    assert "SKIP: notes.md (not a regular file)" in capsys.readouterr().err


@posix_only
def test_scan_project_skips_unix_socket(tmp_path, capsys):
    """Sockets fail the open with ENXIO rather than blocking, but they are
    not readable either — the walk drops them at the same gate.
    """
    # Bind through a short-lived cwd rather than the absolute path: AF_UNIX
    # caps sun_path at 104 bytes on macOS (108 on Linux), and pytest's
    # tmp_path under the macOS runner's /var/folders/... TMPDIR overruns it.
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    previous_cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        sock.bind("mcp.md")
        write_regular(tmp_path, "real.md", "# Real\n")
        with hard_timeout(TIMEOUT_SECONDS, "scan_project over a socket"):
            found = scan_project(str(tmp_path))
    finally:
        os.chdir(previous_cwd)
        sock.close()
    assert (tmp_path / "mcp.md").is_socket()
    assert [path.name for path in found] == ["real.md"]
    assert "SKIP: mcp.md (not a regular file)" in capsys.readouterr().err


@posix_only
def test_scan_convos_skips_fifo_and_keeps_regular_files(tmp_path, capsys):
    make_fifo(tmp_path, "piped.jsonl")
    write_regular(tmp_path, "real.jsonl", '{"type": "user"}\n')
    with hard_timeout(TIMEOUT_SECONDS, "scan_convos over a FIFO"):
        found = scan_convos(str(tmp_path))
    assert [path.name for path in found] == ["real.jsonl"]
    assert "SKIP: piped.jsonl (not a regular file)" in capsys.readouterr().err


@posix_only
def test_parse_claude_jsonl_refuses_fifo(tmp_path):
    fifo = make_fifo(tmp_path, "session.jsonl")
    with hard_timeout(TIMEOUT_SECONDS, "parse_claude_jsonl on a FIFO"):
        with pytest.raises(OSError, match="Refusing non-regular file"):
            list(parse_claude_jsonl(str(fifo)))


def test_parse_claude_jsonl_still_raises_for_a_missing_path(tmp_path):
    """The type gate stats first, so a missing path must keep failing the
    way the plain ``open`` used to.
    """
    with pytest.raises(FileNotFoundError):
        list(parse_claude_jsonl(str(tmp_path / "nope.jsonl")))


def test_parse_claude_jsonl_still_reads_a_regular_transcript(tmp_path):
    path = write_regular(
        tmp_path,
        "session.jsonl",
        '{"type": "user", "sessionId": "s1", "uuid": "u1", '
        '"timestamp": "2026-01-01T00:00:00Z", '
        '"message": {"role": "user", "content": "hello"}}\n',
    )
    records = list(parse_claude_jsonl(str(path)))
    assert [record["content"] for record in records] == ["hello"]


@posix_only
def test_detect_entities_ignores_a_fifo_candidate(tmp_path):
    """A FIFO must be invisible: same result as if it were never there, and
    it must not consume the ``max_files`` budget.
    """
    regular = write_regular(
        tmp_path,
        "people.md",
        "Met Sarah Connor and John Connor at the office today.\n" * 5,
    )
    fifo = make_fifo(tmp_path, "notes.md")
    baseline = detect_entities([regular])
    with hard_timeout(TIMEOUT_SECONDS, "detect_entities over a FIFO"):
        with_fifo = detect_entities([fifo, regular])
    assert with_fifo == baseline


@posix_only
def test_gather_origin_samples_ignores_a_fifo_candidate(tmp_path):
    """``init``'s corpus-origin pass reads the same candidate list as
    ``detect_entities`` and must drop a FIFO at the same gate.
    """
    write_regular(tmp_path, "real.md", "# Real\n\nSome prose about the project.\n")
    make_fifo(tmp_path, "notes.md")
    with hard_timeout(TIMEOUT_SECONDS, "_gather_origin_samples over a FIFO"):
        samples = _gather_origin_samples(str(tmp_path))
    assert len(samples) == 1
    assert "Some prose about the project." in samples[0]


@posix_only
def test_split_mega_files_skips_fifo(tmp_path, capsys, monkeypatch):
    make_fifo(tmp_path, "piped.txt")
    # Two detectable sessions, so the regular file is reported rather than
    # dropped for having nothing to split. Shape copied from
    # tests/test_split_mega_files.py::test_find_session_boundaries_two_sessions.
    session = "Claude Code v1.0\ncontent\n" + "\n" * 5
    write_regular(tmp_path, "real.txt", session * 2)
    monkeypatch.setattr("sys.argv", ["mempalace split", "--source", str(tmp_path), "--dry-run"])
    with hard_timeout(TIMEOUT_SECONDS, "split_mega_files.main over a FIFO"):
        split_main()
    out = capsys.readouterr().out
    assert "SKIP: piped.txt (not a regular file)" in out
    # The gate must drop the pipe and nothing else: a version that skipped
    # every entry would satisfy the SKIP assertion above on its own.
    assert "SKIP: real.txt" not in out
    assert "real.txt" in out


@posix_only
def test_split_file_skips_a_fifo_at_its_own_output_name(tmp_path, capsys):
    """The walk gate covers the source; the output name is built here.

    ``split_file`` synthesises each per-session filename from the transcript
    and writes it into the source directory, so nothing has vetted that path.
    A pre-existing FIFO sitting at one of those names turned the write into a
    blocking open — the same hang, in the one path the discovery gate cannot
    reach.
    """
    session = "Claude Code v1.0\n" + "content line\n" * 14 + "\n" * 5
    source = write_regular(tmp_path, "real.txt", session * 2)
    planned = split_file(str(source), None, dry_run=True)
    assert len(planned) >= 2, "fixture must produce at least two output files"
    blocked = Path(planned[0])
    os.mkfifo(blocked)

    with hard_timeout(TIMEOUT_SECONDS, "split_file writing over a FIFO output"):
        written = split_file(str(source), None, dry_run=False)

    out = capsys.readouterr().out
    assert f"SKIP: {blocked.name} (not a regular file)" in out
    assert blocked not in written
    # The pipe must cost only its own chunk: every other session still lands.
    assert len(written) == len(planned) - 1
    assert all(path.is_file() for path in written)


@posix_only
def test_split_file_skips_a_dangling_symlink_at_its_own_output_name(tmp_path, capsys):
    """A broken link at an output name must not redirect the write.

    ``os.path.exists`` follows the link and answers False for a dangling one,
    so the type gate would wave it through — and ``write_text`` then CREATES
    the target, landing a chunk wherever the link points instead of in the
    output directory. The gate has to ask about the link itself.
    """
    session = "Claude Code v1.0\n" + "content line\n" * 14 + "\n" * 5
    source = write_regular(tmp_path, "real.txt", session * 2)
    planned = split_file(str(source), None, dry_run=True)
    assert len(planned) >= 2, "fixture must produce at least two output files"
    blocked = Path(planned[0])
    outside = tmp_path / "outside" / "victim.txt"
    outside.parent.mkdir()
    os.symlink(outside, blocked)
    assert not outside.exists(), "the link must dangle before the run"

    with hard_timeout(TIMEOUT_SECONDS, "split_file writing over a dangling symlink"):
        written = split_file(str(source), None, dry_run=False)

    out = capsys.readouterr().out
    assert f"SKIP: {blocked.name} (not a regular file)" in out
    assert blocked not in written
    assert not outside.exists(), "a chunk was written through the link, outside the output dir"
    assert len(written) == len(planned) - 1


@posix_only
@needs_unprivileged_posix
def test_collect_manifest_names_survives_an_unreadable_directory(tmp_path):
    """The type gate must not turn a skipped manifest into a crash.

    ``os.walk`` lists the children of a directory with ``r`` but no ``x``,
    and stating one of them raises ``PermissionError``. Each parser already
    swallowed that through its own ``except OSError``, so the gate in front
    of them has to swallow it too — otherwise ``mempalace init`` gains a
    traceback where it used to report no manifest name.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package.json").write_text('{"name": "inner"}', encoding="utf-8")
    os.chmod(repo, 0o444)
    try:
        with hard_timeout(TIMEOUT_SECONDS, "_collect_manifest_names over an unreadable dir"):
            found = _collect_manifest_names(repo)
    finally:
        os.chmod(repo, 0o755)
    assert found == []


@posix_only
@needs_unprivileged_posix
def test_parse_gradle_survives_an_unreadable_directory(tmp_path):
    """The sibling ``settings.gradle`` is stat'd inside the parser's own try."""
    repo = tmp_path / "repo"
    repo.mkdir()
    build = repo / "build.gradle"
    build.write_text("plugins { id 'java' }\n", encoding="utf-8")
    os.chmod(repo, 0o444)
    try:
        with hard_timeout(TIMEOUT_SECONDS, "_parse_gradle over an unreadable dir"):
            name = _parse_gradle(build)
    finally:
        os.chmod(repo, 0o755)
    # Falls back to the directory name, exactly as it did before the gate.
    assert name == "repo"


@posix_only
def test_format_miner_extract_text_does_not_block_on_fifo(tmp_path):
    """``mine --mode extract`` was already immune — its zero-size gate fires
    first, because a FIFO stats as 0 bytes. Pinned so a future reshuffle of
    those checks cannot reintroduce the hang here.
    """
    fifo = make_fifo(tmp_path, "doc.pdf")
    with hard_timeout(TIMEOUT_SECONDS, "extract_text on a FIFO"):
        text, status = extract_text(fifo)
    assert text is None
    assert status is ExtractionStatus.SKIP_EMPTY


@posix_only
def test_collect_manifest_names_ignores_a_fifo_manifest(tmp_path):
    real_repo = tmp_path / "real"
    real_repo.mkdir()
    (real_repo / "package.json").write_text('{"name": "real-project"}', encoding="utf-8")
    piped = tmp_path / "piped"
    piped.mkdir()
    os.mkfifo(piped / "package.json")

    with hard_timeout(TIMEOUT_SECONDS, "_collect_manifest_names over a FIFO manifest"):
        found = _collect_manifest_names(tmp_path)
    assert [entry[1] for entry in found] == ["real-project"]


# ─────────────────────────────────────────────────────────────────────────
# Fixed-name reads: `exists()` is not a type check
#
# The sites above are fed by a directory walk. These are read by a name the
# code already knows, behind an `exists()` guard — which is true for a FIFO,
# so the open right after it blocks anyway.
# ─────────────────────────────────────────────────────────────────────────


@posix_only
def test_load_config_treats_a_fifo_yaml_as_absent(tmp_path):
    """`mempalace.yaml` is the first thing `mine` reads."""
    make_fifo(tmp_path, "mempalace.yaml")
    with hard_timeout(TIMEOUT_SECONDS, "load_config on a FIFO mempalace.yaml"):
        config = load_config(str(tmp_path))
    assert [room["name"] for room in config["rooms"]] == ["general"]


@posix_only
def test_load_config_still_reads_a_regular_yaml(tmp_path):
    (tmp_path / "mempalace.yaml").write_text(
        yaml.dump({"wing": "realwing", "rooms": [{"name": "docs", "description": "d"}]}),
        encoding="utf-8",
    )
    config = load_config(str(tmp_path))
    assert config["wing"] == "realwing"
    assert [room["name"] for room in config["rooms"]] == ["docs"]


@posix_only
def test_ensure_gitignore_leaves_a_fifo_gitignore_alone(tmp_path):
    (tmp_path / ".git").mkdir()
    make_fifo(tmp_path, ".gitignore")
    with hard_timeout(TIMEOUT_SECONDS, "_ensure_mempalace_files_gitignored on a FIFO"):
        assert _ensure_mempalace_files_gitignored(str(tmp_path)) is False


def test_ensure_gitignore_still_appends_to_a_regular_gitignore(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text("*.pyc\n", encoding="utf-8")
    assert _ensure_mempalace_files_gitignored(str(tmp_path)) is True
    written = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert "mempalace.yaml" in written and "entities.json" in written


@posix_only
def test_cmd_compress_ignores_a_fifo_entities_json(tmp_path, monkeypatch, capsys):
    """`compress` picks up `./entities.json` when no --config is given."""
    monkeypatch.chdir(tmp_path)
    make_fifo(tmp_path, "entities.json")
    args = argparse.Namespace(palace=None, wing=None, dry_run=False, config=None)
    with patch("mempalace.cli.MempalaceConfig") as mock_config_cls:
        mock_config_cls.return_value.palace_path = str(tmp_path / "nonexistent")
        with hard_timeout(TIMEOUT_SECONDS, "cmd_compress with a FIFO entities.json"):
            with pytest.raises(SystemExit):
                cmd_compress(args)
    out = capsys.readouterr().out
    assert "No palace found" in out
    assert "Loaded entity config" not in out


@posix_only
def test_cmd_compress_fifo_entities_json_does_not_shadow_the_palace_copy(
    tmp_path, monkeypatch, capsys
):
    """The candidate loop must not stop at a pipe.

    An `entities.json` FIFO in the cwd would otherwise be picked as the
    config and then rejected by the load guard, hiding a perfectly good
    `<palace>/entities.json` behind it.
    """
    monkeypatch.chdir(tmp_path)
    make_fifo(tmp_path, "entities.json")
    palace = tmp_path / "palace"
    palace.mkdir()
    (palace / "entities.json").write_text('{"entities": {"Alice": "ALC"}}', encoding="utf-8")
    args = argparse.Namespace(palace=None, wing=None, dry_run=False, config=None)
    with patch("mempalace.cli.MempalaceConfig") as mock_config_cls:
        mock_config_cls.return_value.palace_path = str(palace)
        with hard_timeout(TIMEOUT_SECONDS, "cmd_compress candidate loop"):
            with pytest.raises(SystemExit):
                cmd_compress(args)
    assert f"Loaded entity config: {palace / 'entities.json'}" in capsys.readouterr().out


@posix_only
def test_parse_gradle_ignores_a_fifo_sibling_settings_file(tmp_path):
    """`build.gradle` is a regular file and clears the manifest gate; the
    parser then reads the SIBLING `settings.gradle`, which no walk vetted.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "build.gradle").write_text("plugins { id 'java' }\n", encoding="utf-8")
    make_fifo(repo, "settings.gradle")
    with hard_timeout(TIMEOUT_SECONDS, "_collect_manifest_names with a FIFO settings.gradle"):
        found = _collect_manifest_names(tmp_path)
    assert [entry[1] for entry in found] == ["repo"]


@posix_only
def test_count_human_messages_ignores_a_fifo_transcript(tmp_path):
    fifo = make_fifo(tmp_path, "transcript.jsonl")
    with hard_timeout(TIMEOUT_SECONDS, "count_human_messages on a FIFO"):
        assert count_human_messages(str(fifo)) == 0


def test_count_human_messages_still_raises_for_a_missing_path(tmp_path):
    """The type gate is narrowed to paths that exist, so a missing transcript
    keeps failing the way the plain ``open`` did.
    """
    with pytest.raises(FileNotFoundError):
        count_human_messages(str(tmp_path / "nope.jsonl"))


def test_count_human_messages_still_counts_a_regular_transcript(tmp_path):
    path = write_regular(
        tmp_path,
        "transcript.jsonl",
        '{"message": {"role": "user", "content": "one"}}\n'
        '{"message": {"role": "assistant", "content": "two"}}\n'
        '{"message": {"role": "user", "content": "three"}}\n',
    )
    assert count_human_messages(str(path)) == 2


@posix_only
def test_cmd_init_refuses_to_write_entities_over_a_fifo(tmp_path, capsys):
    """`init` writes `<project>/entities.json`; opening a pre-existing FIFO
    for writing blocks until a reader appears.
    """
    make_fifo(tmp_path, "entities.json")
    args = argparse.Namespace(dir=str(tmp_path), yes=True, no_llm=True)
    detected = {"people": [{"name": "Alice"}], "projects": [], "topics": [], "uncertain": []}
    confirmed = {"people": ["Alice"], "projects": [], "topics": []}
    with (
        patch("mempalace.cli.MempalaceConfig"),
        patch("mempalace.project_scanner.discover_entities", return_value=detected),
        patch("mempalace.entity_detector.confirm_entities", return_value=confirmed),
        patch("mempalace.room_detector_local.detect_rooms_local"),
        patch("mempalace.cli._run_pass_zero", return_value=None),
        patch("mempalace.cli._maybe_run_mine_after_init"),
    ):
        with hard_timeout(TIMEOUT_SECONDS, "cmd_init with a FIFO entities.json"):
            cmd_init(args)
    captured = capsys.readouterr()
    assert "is not a regular file" in captured.err
    assert "Entities saved" not in captured.out


@posix_only
def test_detect_rooms_local_refuses_a_fifo_config(tmp_path):
    """`init` writes `mempalace.yaml`; a write open on a pipe blocks until a
    reader appears, which parked `init` with no output at all.
    """
    write_regular(tmp_path, "README.md", "note\n")
    make_fifo(tmp_path, "mempalace.yaml")
    with hard_timeout(TIMEOUT_SECONDS, "detect_rooms_local over a FIFO config"):
        with pytest.raises(OSError, match="not a regular file"):
            detect_rooms_local(project_dir=str(tmp_path), yes=True)


@posix_only
def test_collect_corpus_text_ignores_a_fifo(tmp_path):
    """LLM refinement walks prose by suffix and stats only for mtime."""
    write_regular(tmp_path, "a.md", "real prose\n")
    make_fifo(tmp_path, "notes.md")
    with hard_timeout(TIMEOUT_SECONDS, "collect_corpus_text over a FIFO"):
        text = collect_corpus_text(str(tmp_path))
    assert text == "real prose\n"


@posix_only
def test_sweep_directory_skips_a_fifo_without_booking_a_failure(tmp_path, capsys):
    """A pipe is nothing to sweep, not a sweep failure.

    Booking it as a failure flips the command's exit status to 2 through
    ``cli.cmd_sweep``, which breaks any script gating on it — and the same
    input on a directory walk is a benign ``SKIP`` in ``mine``.
    """
    convos = tmp_path / "convos"
    convos.mkdir()
    write_regular(
        convos,
        "real.jsonl",
        '{"type": "user", "sessionId": "s1", "uuid": "u1", '
        '"timestamp": "2026-01-01T00:00:00Z", '
        '"message": {"role": "user", "content": "hello"}}\n',
    )
    make_fifo(convos, "piped.jsonl")
    with hard_timeout(TIMEOUT_SECONDS, "sweep_directory over a FIFO"):
        result = sweep_directory(str(convos), str(tmp_path / "palace"))
    # ``failures`` is what ``cli.cmd_sweep`` turns into ``sys.exit(2)``.
    assert result["failures"] == []
    # ``files_attempted`` counts discovery, per its docstring, so the pipe
    # stays in it; ``files_succeeded`` counts what was actually swept.
    assert result["files_attempted"] == 2
    assert result["files_succeeded"] == 1
    assert result["drawers_added"] == 1
    assert "SKIP: piped.jsonl (not a regular file)" in capsys.readouterr().err


@posix_only
def test_sweep_directory_still_books_a_stat_failure_as_a_failure(tmp_path, capsys):
    """A pipe is nothing to sweep; a stat that FAILS is a real error.

    The type gate has to tell those apart. A dangling symlink, a symlink loop
    and a file unlinked between ``rglob`` and the gate all raise from
    ``stat`` — and every one of them used to reach ``open`` inside ``sweep``
    and be booked. Swallowing them would flip ``mempalace sweep`` from exit 2
    to exit 0 on a transcript it could not read.
    """
    convos = tmp_path / "convos"
    convos.mkdir()
    write_regular(
        convos,
        "real.jsonl",
        '{"type": "user", "sessionId": "s1", "uuid": "u1", '
        '"timestamp": "2026-01-01T00:00:00Z", '
        '"message": {"role": "user", "content": "hello"}}\n',
    )
    os.symlink(convos / "gone.jsonl", convos / "dangling.jsonl")
    with hard_timeout(TIMEOUT_SECONDS, "sweep_directory over a dangling symlink"):
        result = sweep_directory(str(convos), str(tmp_path / "palace"))
    # ``cli.cmd_sweep`` turns a non-empty ``failures`` into ``sys.exit(2)``.
    assert [Path(entry["file"]).name for entry in result["failures"]] == ["dangling.jsonl"]
    assert result["files_succeeded"] == 1
    assert "stat failed" in capsys.readouterr().err


# ─────────────────────────────────────────────────────────────────────────
# O_NONBLOCK must not drop a regular file the blocking open would have read
# ─────────────────────────────────────────────────────────────────────────


@posix_only
def test_read_text_no_follow_retries_when_a_lease_break_returns_eagain(tmp_path, monkeypatch):
    """A write lease is the one case where the flag changes `open` itself.

    Breaking a lease with ``O_NONBLOCK`` fails ``EAGAIN`` immediately, where
    a blocking open waits out ``lease-break-time`` and succeeds. Left alone
    that turns into a silently dropped file. The kernel grants leases on
    regular files only, so the retry is authorised by the file *type*; the
    errno only decides whether to look again.

    ``EAGAIN`` is injected rather than staged with a real lease so the test
    costs milliseconds instead of the 45 s default lease-break-time.
    """
    payload = "PAYLOAD THAT MUST STILL BE MINED\n" * 20
    target = write_regular(tmp_path, "notes.md", payload)
    real_open = os.open
    calls = {"n": 0}

    def _fake_open(path, flags, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            assert flags & os.O_NONBLOCK, "first attempt should carry the flag"
            raise OSError(errno.EAGAIN, os.strerror(errno.EAGAIN), str(path))
        assert not flags & os.O_NONBLOCK, "retry should drop the flag"
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr("mempalace.miner.os.open", _fake_open)
    with hard_timeout(TIMEOUT_SECONDS, "_read_text_no_follow under an injected EAGAIN"):
        result = _read_text_no_follow(target, tmp_path)
    assert result is not None
    content, mtime = result
    assert content == payload
    assert mtime == os.path.getmtime(target)
    assert calls["n"] == 2


@posix_only
def test_read_text_no_follow_does_not_retry_eagain_on_a_fifo(tmp_path, monkeypatch):
    """The retry is gated on the type, so a pipe never gets a blocking open.

    The stand-in raises if it is ever called without the flag, so a retry
    that trusted the errno alone would fail this test rather than hang it.
    """
    fifo = make_fifo(tmp_path, "notes.md")

    def _fake_open(path, flags, *args, **kwargs):
        if flags & os.O_NONBLOCK:
            raise OSError(errno.EAGAIN, os.strerror(errno.EAGAIN), str(path))
        raise AssertionError("must not retry a blocking open on a FIFO")

    monkeypatch.setattr("mempalace.miner.os.open", _fake_open)
    with hard_timeout(TIMEOUT_SECONDS, "_read_text_no_follow EAGAIN on a FIFO"):
        assert _read_text_no_follow(fifo, tmp_path) is None


@posix_only
@needs_unprivileged_posix
def test_gather_origin_samples_survives_an_unreadable_directory(tmp_path):
    """The type gate must not turn a skipped file into a crash.

    ``Path.is_file()`` raises ``PermissionError`` on a directory without
    ``x``; the ``open`` it replaced was already inside the ``try`` that
    absorbs exactly that, so the gate has to sit there too.
    """
    write_regular(tmp_path, "real.md", "# Real\n\nsome prose here\n")
    walled = tmp_path / "walled"
    walled.mkdir()
    write_regular(walled, "notes.md", "y" * 200)
    os.chmod(walled, 0o444)
    try:
        with hard_timeout(TIMEOUT_SECONDS, "_gather_origin_samples over an unreadable dir"):
            samples = _gather_origin_samples(str(tmp_path))
    finally:
        os.chmod(walled, 0o755)
    assert len(samples) == 1
    assert "some prose here" in samples[0]


def test_read_transcript_file_size_message_names_the_path_once(tmp_path):
    """Both refusal branches compose the path through the same wrapper."""
    big = write_regular(tmp_path, "huge.jsonl", "not actually huge")

    class _HugeStat:
        st_mode = stat_module.S_IFREG | 0o644
        st_size = 600 * 1024 * 1024

    with patch("mempalace.normalize.os.fstat", return_value=_HugeStat()):
        with pytest.raises(IOError) as excinfo:
            _read_transcript_file(str(big))
    message = str(excinfo.value)
    assert "too large" in message.lower()
    assert message.count(str(big)) == 1


# ─────────────────────────────────────────────────────────────────────────
# End to end through the real miner
# ─────────────────────────────────────────────────────────────────────────


@posix_only
def test_mine_completes_with_a_fifo_in_the_corpus(tmp_path):
    """The original report: ``mempalace mine <dir>`` never returned."""
    project_root = tmp_path / "corpus"
    project_root.mkdir()
    (project_root / "mempalace.yaml").write_text(
        yaml.dump(
            {
                "wing": "fifo_repro",
                "rooms": [{"name": "general", "description": "General"}],
            }
        ),
        encoding="utf-8",
    )
    write_regular(
        project_root,
        "real.md",
        "# Real note\n\n" + "The quick brown fox jumps over the lazy dog. " * 40,
    )
    make_fifo(project_root, "notes.md")

    palace_path = tmp_path / "palace"
    with hard_timeout(TIMEOUT_SECONDS, "mine() over a corpus holding a FIFO"):
        mine(str(project_root), str(palace_path))

    import chromadb

    collection = chromadb.PersistentClient(path=str(palace_path)).get_collection(
        "mempalace_drawers"
    )
    stored = collection.get(include=["metadatas"])
    sources = {Path(meta["source_file"]).name for meta in stored["metadatas"]}
    assert sources == {"real.md"}
