"""Tests for mempalace.source_identity.

The question this answers is "is this the directory the file was mined from",
and the answer is the directory's inode. A path is only a name: mount
something at it and the name resolves to the root of what was mounted, which
is a different inode, and unmounting brings the original back. The tests that
matter here drive real mounts rather than simulating them, because nothing
else produces the states #2320 is about.

One precondition is handled two ways on purpose. A filesystem that reports no
inode of its own is a skip for the two tests whose assertions would degenerate
into ``None == None`` there and pass having proven nothing, and a loud failure
for the rest, whose assertions stay meaningful and would rather say so than go
quiet. Skipping everywhere would let the whole module go green on a filesystem
it never tested.
"""

import functools
import os
import subprocess
import sys
import textwrap

import pytest

from mempalace import source_identity as si


def _inodes_or_skip(*paths):
    """The paths' inodes, or a skip where the filesystem reports none.

    A filesystem that answers zero has no identity to give, which
    ``directory_identity`` reports as ``None`` on purpose. Asserting the
    recorded answer equals ``str(st_ino)`` there would compare ``None`` to
    ``"0"`` and fail for a reason that is not a defect; asserting two of them
    differ would compare ``None`` to ``None`` and pass having proven nothing.
    ``test_a_zero_inode_is_not_an_identity`` covers that filesystem instead.
    """
    inodes = [os.stat(p).st_ino for p in paths]
    if not all(inodes):
        pytest.skip("filesystem reports no inode of its own; see the zero-inode test")
    return inodes


def test_a_directory_answers_with_its_inode(tmp_path):
    (ino,) = _inodes_or_skip(tmp_path)

    assert si.directory_identity(tmp_path) == str(ino)


def test_two_directories_answer_differently(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _inodes_or_skip(a, b)

    assert si.directory_identity(a) != si.directory_identity(b)


def test_the_answer_survives_the_files_inside_it_changing(tmp_path):
    """An editor that saves through a temporary file gives every file a new
    inode. The directory's own does not move, which is why the directory is
    what gets recorded."""
    before = si.directory_identity(tmp_path)
    (tmp_path / "a.md").write_text("first")
    (tmp_path / "a.md").unlink()
    (tmp_path / "a.md").write_text("second")

    assert si.directory_identity(tmp_path) == before


def test_a_directory_that_is_not_there_answers_none(tmp_path):
    assert si.directory_identity(tmp_path / "gone") is None


def test_a_zero_inode_is_not_an_identity(tmp_path, monkeypatch):
    """Some filesystems report zero rather than a number of their own. Taken
    at face value it would make every directory on such a filesystem
    corroborate every other, and nothing in the report would say so."""
    real_stat = os.stat

    class ZeroInode:
        st_ino = 0

    monkeypatch.setattr(
        os,
        "stat",
        # Everything else in the process keeps the real call, arguments and
        # all: dropping them here would answer a ``follow_symlinks=False``
        # ask with a followed stat for the length of the test.
        lambda p, *a, **k: ZeroInode() if str(p) == str(tmp_path) else real_stat(p, *a, **k),
    )

    assert si.directory_identity(tmp_path) is None
    assert si.identity_metadata(tmp_path / "file.md") == {}


@pytest.mark.skipif(
    os.name == "nt", reason="symlink creation requires elevated privileges on Windows"
)
def test_a_symlinked_directory_answers_with_its_target(tmp_path):
    """``mine`` resolves its argument, and a link is not a filesystem: what
    matters is the directory the file was really read from."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)

    assert si.directory_identity(link) == si.directory_identity(real)


def test_identity_metadata_is_mergeable(tmp_path):
    meta = {"wing": "demo"}
    meta.update(si.identity_metadata(tmp_path / "file.md"))

    expected = si.directory_identity(tmp_path)
    assert expected is not None, "the filesystem reports no inode to record"
    assert meta["source_dir_ino"] == expected


def test_identity_metadata_is_empty_rather_than_none(tmp_path):
    """A directory with no identity to record leaves the key out rather than
    storing ``None`` under it. A stored ``None`` would be a third state for
    ``sync`` to read where two already cover it, and chroma drops such a value
    on the way in anyway, so the two spellings would not even round-trip
    alike."""
    assert si.identity_metadata(tmp_path / "gone" / "file.md") == {}


class TestEveryWritePathRecordsIt:
    """One test per site that files a drawer naming a source file.

    ``sync`` decides every such drawer by the same rule, so a site that files
    one without the identity leaves that drawer at ``develop``'s behaviour
    with nothing in any report to say so. The sites reached through a public
    entry point are here; the ones inside a mining run are pinned where that
    run is already exercised, in ``test_miner``, ``test_convo_miner_unit``,
    ``test_format_miner``, ``test_sweeper``, ``test_closets`` and
    ``test_mcp_server``.
    """

    class FakeCollection:
        def __init__(self):
            self.metadatas: list = []

        def upsert(self, ids=None, documents=None, metadatas=None, **kwargs):
            self.metadatas.extend(metadatas or [])

        def add(self, ids=None, documents=None, metadatas=None, **kwargs):
            self.upsert(ids=ids, documents=documents, metadatas=metadatas)

        def get(self, *args, **kwargs):
            return {"ids": [], "metadatas": [], "documents": []}

        def query(self, *args, **kwargs):
            return {"ids": [[]], "metadatas": [[]], "documents": [[]]}

    def _source(self, tmp_path, name="a.md", text="hello"):
        path = tmp_path / name
        path.write_text(text)
        return str(path)

    def test_a_live_exchange(self, tmp_path):
        from mempalace.convo_miner import file_conversation_exchange

        col = self.FakeCollection()
        source = self._source(tmp_path, "chat.jsonl")
        file_conversation_exchange(
            col,
            wing="wing_general",
            room="conversations",
            text="hello",
            source_file=source,
            agent="agent",
        )

        expected = si.directory_identity(tmp_path)
        assert expected is not None, "the filesystem reports no inode to record"
        assert col.metadatas
        assert all(m.get("source_dir_ino") == expected for m in col.metadatas), col.metadatas

    def test_the_sentinel_a_zero_chunk_extract_leaves(self, tmp_path):
        from mempalace.format_miner import _register_file

        col = self.FakeCollection()
        source = self._source(tmp_path, "empty.pdf")
        _register_file(col, source, "wing_general", "agent")

        expected = si.directory_identity(tmp_path)
        assert expected is not None, "the filesystem reports no inode to record"
        assert col.metadatas
        assert col.metadatas[0].get("source_dir_ino") == expected

    def test_the_public_add_drawer(self, tmp_path):
        from mempalace.miner import add_drawer

        col = self.FakeCollection()
        source = self._source(tmp_path)
        add_drawer(col, "wing_general", "notes", "hello", source, 0, "agent")

        expected = si.directory_identity(tmp_path)
        assert expected is not None, "the filesystem reports no inode to record"
        assert col.metadatas
        assert col.metadatas[0].get("source_dir_ino") == expected

    def test_a_drawer_an_adapter_files(self, tmp_path):
        from mempalace.sources.base import DrawerRecord
        from mempalace.sources.context import PalaceContext

        col = self.FakeCollection()
        source = self._source(tmp_path)
        ctx = PalaceContext(
            drawer_collection=col,
            knowledge_graph=None,
            palace_path=str(tmp_path),
            adapter_name="demo",
            adapter_version="1",
        )
        ctx.upsert_drawer(
            DrawerRecord(
                content="hello",
                source_file=source,
                chunk_index=0,
                metadata={},
            )
        )

        expected = si.directory_identity(tmp_path)
        assert expected is not None, "the filesystem reports no inode to record"
        assert col.metadatas
        assert col.metadatas[0].get("source_dir_ino") == expected

    def test_an_adapter_filing_two_directories_records_each_one(self, tmp_path):
        """One adapter run files many drawers, and an adapter can file for as
        long as it likes, so the identity is read for each drawer rather than
        held for the run. Anything held across it would hand the first
        directory's inode to every file after it.
        """
        from mempalace.sources.base import DrawerRecord
        from mempalace.sources.context import PalaceContext

        one = tmp_path / "one"
        two = tmp_path / "two"
        one.mkdir()
        two.mkdir()
        first = one / "a.md"
        second = two / "b.md"
        first.write_text("hello\n")
        second.write_text("hello\n")
        assert si.directory_identity(one) != si.directory_identity(two)

        col = self.FakeCollection()
        ctx = PalaceContext(
            drawer_collection=col,
            knowledge_graph=None,
            palace_path=str(tmp_path),
            adapter_name="demo",
            adapter_version="1",
        )
        for source in (first, second, first):
            ctx.upsert_drawer(
                DrawerRecord(content="hello", source_file=str(source), chunk_index=0, metadata={})
            )

        recorded = [m.get("source_dir_ino") for m in col.metadatas]
        assert recorded == [
            si.directory_identity(one),
            si.directory_identity(two),
            si.directory_identity(one),
        ], recorded

    def test_a_source_that_is_not_a_path_records_nothing(self, tmp_path):
        """An adapter's source can be a URL or an id. It stats to nothing, the
        key is left off, and the drawer is decided the way one filed before
        this existed is."""
        from mempalace.sources.base import DrawerRecord
        from mempalace.sources.context import PalaceContext

        col = self.FakeCollection()
        ctx = PalaceContext(
            drawer_collection=col,
            knowledge_graph=None,
            palace_path=str(tmp_path),
            adapter_name="demo",
            adapter_version="1",
        )
        ctx.upsert_drawer(
            DrawerRecord(
                content="hello",
                source_file="https://example.invalid/thread/1",
                chunk_index=0,
                metadata={},
            )
        )

        assert col.metadatas
        assert "source_dir_ino" not in col.metadatas[0], col.metadatas

    def test_an_adapters_own_source_file_is_the_one_stamped(self, tmp_path):
        """``upsert_drawer`` fills ``source_file`` with ``setdefault``, so an
        adapter that set one of its own keeps it. The identity has to come
        from that value: ``sync`` looks the drawer up by what lands in the
        row, and an inode read from the record's path instead would name a
        directory the drawer never mentions. Nothing could then match it, and
        the drawer would be unremovable for as long as it exists.
        """
        from mempalace.sources.base import DrawerRecord
        from mempalace.sources.context import PalaceContext

        named = tmp_path / "named"
        passed = tmp_path / "passed"
        named.mkdir()
        passed.mkdir()
        (named / "x.md").write_text("hello\n")
        (passed / "x.md").write_text("hello\n")
        assert si.directory_identity(named) != si.directory_identity(passed)

        col = self.FakeCollection()
        ctx = PalaceContext(
            drawer_collection=col,
            knowledge_graph=None,
            palace_path=str(tmp_path),
            adapter_name="demo",
            adapter_version="1",
        )
        ctx.upsert_drawer(
            DrawerRecord(
                content="hello",
                source_file=str(passed / "x.md"),
                chunk_index=0,
                metadata={"source_file": str(named / "x.md")},
            )
        )

        assert col.metadatas
        stamped = col.metadatas[0]
        assert stamped["source_file"] == str(named / "x.md")
        assert stamped.get("source_dir_ino") == si.directory_identity(named), stamped

    def test_an_adapter_cannot_supply_the_identity_itself(self, tmp_path):
        """The other stamps are an adapter's to set; this one is not. It is a
        reading of this machine's filesystem, so a value from anywhere else
        is a number ``sync`` would weigh a removal against and no directory
        could ever answer. Core overwrites it where it read one, and drops it
        where it did not, which leaves the drawer decided by corroboration
        alone rather than by something an adapter invented.
        """
        from mempalace.sources.base import DrawerRecord
        from mempalace.sources.context import PalaceContext

        col = self.FakeCollection()
        source = self._source(tmp_path)
        ctx = PalaceContext(
            drawer_collection=col,
            knowledge_graph=None,
            palace_path=str(tmp_path),
            adapter_name="demo",
            adapter_version="1",
        )
        ctx.upsert_drawer(
            DrawerRecord(
                content="hello",
                source_file=source,
                chunk_index=0,
                metadata={"source_dir_ino": "111111111"},
            )
        )
        ctx.upsert_drawer(
            DrawerRecord(
                content="hello",
                source_file="https://example.invalid/thread/1",
                chunk_index=1,
                metadata={"source_dir_ino": "111111111"},
            )
        )

        here = si.directory_identity(tmp_path)
        assert here is not None and here != "111111111"
        assert col.metadatas[0].get("source_dir_ino") == here, col.metadatas[0]
        assert "source_dir_ino" not in col.metadatas[1], col.metadatas[1]


def test_nothing_is_written_to_the_directory(tmp_path):
    """The project's README promises that mining never writes to the source,
    and its container recipes mount sources read-only. Reading an inode is
    what keeps that true."""
    (tmp_path / "a.md").write_text("content")
    before = sorted(p.name for p in tmp_path.iterdir())

    si.directory_identity(tmp_path)
    si.identity_metadata(tmp_path / "a.md")

    assert sorted(p.name for p in tmp_path.iterdir()) == before


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_a_directory_this_process_may_not_open_still_answers(tmp_path):
    """A read-only volume records an identity like any other, and so does a
    directory this process cannot read at all.

    ``os.stat`` asks the parent for the entry and needs no permission on the
    target itself, which is what lets mining record an identity on a source
    mounted ``:ro``, as both of the README's container recipes do. Mode
    ``0o000`` is the strong form of that: an implementation that reached for
    ``listdir`` or opened the directory would answer ``None`` here.
    """
    directory = tmp_path / "unreadable"
    directory.mkdir()
    expected = os.stat(directory).st_ino
    if not expected:
        pytest.skip("filesystem reports no inode of its own")
    os.chmod(directory, 0o000)
    try:
        assert si.directory_identity(directory) == str(expected)
        assert si.identity_metadata(directory / "a.md") == {"source_dir_ino": str(expected)}
    finally:
        os.chmod(directory, 0o700)


# ── the part that needs a real mount ────────────────────────────────────
#
# Mount states are what this module exists for, and nothing but a real mount
# produces one. This runs the probe inside `unshare --mount --map-root-user`,
# which needs unprivileged user namespaces; where those are off, the test says
# so rather than pretending to cover it.

_MOUNT_PROBE = textwrap.dedent(
    """
    import os, shutil, subprocess, sys, tempfile
    sys.path.insert(0, sys.argv[1])
    from mempalace import source_identity as si

    root = tempfile.mkdtemp()
    try:
        proj = os.path.join(root, "proj")
        data = os.path.join(proj, "data")
        other = os.path.join(root, "other")
        os.makedirs(data)
        os.makedirs(other)

        before = si.directory_identity(data)
        subprocess.run(["mount", "-t", "tmpfs", "tmpfs", data], check=True)
        on_volume = si.directory_identity(data)
        subprocess.run(["umount", data], check=True)
        after = si.directory_identity(data)

        subprocess.run(["mount", "--bind", other, data], check=True)
        on_bind = si.directory_identity(data)
        subprocess.run(["umount", data], check=True)

        print(f"before={before}")
        print(f"on_volume={on_volume}")
        print(f"after={after}")
        print(f"on_bind={on_bind}")
        print(f"other={si.directory_identity(other)}")
    finally:
        # The namespace goes away with this process, the directory does not:
        # it is on the filesystem the runner shares with everything else, so
        # a mount that fails part way must not leave it behind either.
        subprocess.run(["umount", os.path.join(root, "proj", "data")], check=False)
        shutil.rmtree(root, ignore_errors=True)
    """
)


@functools.lru_cache(maxsize=1)
def _unshare_available() -> bool:
    if os.name == "nt" or sys.platform == "darwin":
        return False
    # The body mounts, binds and unmounts, so the gate has to establish that
    # and not merely that a namespace can be entered: a kernel that hands out
    # user namespaces while refusing the mount would turn a skip into a
    # failure with nothing about this code in it.
    try:
        probe = subprocess.run(
            [
                "unshare",
                "--mount",
                "--map-root-user",
                "sh",
                "-c",
                'd=$(mktemp -d) && mount -t tmpfs tmpfs "$d" && umount "$d" && rmdir "$d"',
            ],
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0


@pytest.mark.skipif(sys.platform != "linux", reason="reads Linux's own mount table")
def test_a_mount_point_answers_with_the_root_of_what_is_mounted_on_it():
    """Half the claim, on a mount somebody else made, so it cannot be skipped.

    The mount test below needs unprivileged user namespaces, which a CI
    runner may withhold; a suite that only skips there would go green
    having proven nothing. Linux mounts ``/proc`` on every boot, so the
    question can be put to that instead: a path with a filesystem mounted
    at it is stat'ed on that filesystem, and the identity recorded for it
    is that filesystem's root rather than any number the directory
    underneath could hold.

    What this cannot show is the directory underneath coming back, since
    seeing it means unmounting; that half needs the namespace and lives in
    the test below.
    """
    with open("/proc/mounts", encoding="utf-8") as f:
        mounted = {line.split()[1] for line in f if len(line.split()) > 1}
    assert "/proc" in mounted, "this test reads /proc, which is expected to be mounted"

    # Stat lands on the mounted filesystem, not on the directory the name
    # belongs to: a different device answers.
    assert os.stat("/proc").st_dev != os.stat("/").st_dev

    # And the identity is that filesystem's root. `1` is not a number the
    # root filesystem hands out to a directory — on ext4 it is the
    # bad-blocks inode and `2` is the root — so this answer can only have
    # come from the procfs mounted at the name.
    assert si.directory_identity("/proc") == "1"
    assert si.directory_identity("/") == str(os.stat("/").st_ino)
    assert si.directory_identity("/proc") != si.directory_identity("/")


@pytest.mark.skipif(not _unshare_available(), reason="needs unprivileged mount namespaces")
def test_a_mounted_volume_answers_differently_and_the_directory_comes_back():
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(si.__file__)))
    result = subprocess.run(
        ["unshare", "--mount", "--map-root-user", sys.executable, "-c", _MOUNT_PROBE, repo_root],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    answers = dict(line.split("=", 1) for line in result.stdout.strip().splitlines())

    # A volume mounted over the directory answers with its own root, and the
    # directory underneath answers again once the volume goes away.
    assert answers["before"] != "None"
    assert answers["on_volume"] != answers["before"]
    assert answers["after"] == answers["before"]

    # A bind mount is the container shape: another directory, same filesystem,
    # so `st_dev` cannot see it and the inode can.
    assert answers["other"] != "None"
    assert answers["on_bind"] == answers["other"]
    assert answers["on_bind"] != answers["before"]
