"""Conversation freshness must describe the source bytes actually mined."""

import os

import pytest

from mempalace import convo_miner
from mempalace import normalize as normalize_module
from mempalace.palace import get_collection


_INITIAL = (
    "> What is the plan?\nStart with the schema, then the API.\n\n"
    "> Any risks?\nMigration ordering is the main one.\n"
)
_APPEND = "\n> Did we settle the issue?\nAPPENDED_TURN_MARKER: preserve every live turn.\n"


def _mine(source, palace):
    convo_miner.mine_convos(str(source), str(palace), wing="snapshot")


def _documents(palace):
    # Mining may close cached backend handles; reopen after each completed run.
    rows = get_collection(str(palace)).get(include=["documents", "metadatas"])
    print("Stored documents:", rows["documents"])
    print("Stored source mtimes:", [m.get("source_mtime") for m in rows["metadatas"]])
    return rows["documents"]


@pytest.mark.parametrize("initial", [_INITIAL, "hi", ""], ids=["drawers", "short", "empty"])
def test_append_after_read_is_recovered_on_next_mine(tmp_path, monkeypatch, initial):
    source = tmp_path / "session.txt"
    source.write_text(initial, encoding="utf-8")
    original_stat = source.stat()
    palace = tmp_path / "palace"
    original_normalize = convo_miner.normalize_conversations
    appended = False

    def normalize_then_append(filepath, *args, **kwargs):
        nonlocal appended
        result = original_normalize(filepath, *args, **kwargs)
        with source.open("a", encoding="utf-8") as stream:
            stream.write(_APPEND)
        os.utime(
            source,
            ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns + 60_000_000_000),
        )
        appended = True
        return result

    with monkeypatch.context() as race:
        race.setattr(convo_miner, "normalize_conversations", normalize_then_append)
        _mine(source, palace)

    assert appended, "the source append was not interleaved after normalization"
    print("Source after append:", source.read_text(encoding="utf-8"))
    _documents(palace)
    _mine(source, palace)
    assert any("APPENDED_TURN_MARKER" in doc for doc in _documents(palace)), (
        "A stable re-mine skipped the appended turn after an older read was filed"
    )


def test_append_between_runs_with_submillisecond_mtime_is_recovered(tmp_path):
    """This is a completed-run freshness error, with no concurrent read/write."""
    source = tmp_path / "session.txt"
    source.write_text(_INITIAL, encoding="utf-8")
    palace = tmp_path / "palace"
    _mine(source, palace)
    original_stat = source.stat()

    with source.open("a", encoding="utf-8") as stream:
        stream.write(_APPEND)
    os.utime(
        source,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns + 100_000),
    )
    changed_stat = source.stat()
    delta_ns = changed_stat.st_mtime_ns - original_stat.st_mtime_ns
    if not 0 < delta_ns < 1_000_000:
        pytest.skip("the filesystem cannot preserve a submillisecond mtime change")
    assert changed_stat.st_size > original_stat.st_size
    print("Actual mtime delta (ns):", delta_ns)
    print("Source after append:", source.read_text(encoding="utf-8"))

    _mine(source, palace)
    assert any("APPENDED_TURN_MARKER" in doc for doc in _documents(palace)), (
        "A real submillisecond mtime change was treated as an unchanged transcript"
    )


def test_change_during_read_is_recovered_on_next_stable_mine(tmp_path, monkeypatch):
    source = tmp_path / "session.txt"
    initial = (
        "> Which plan?\nDECISION_OLD: keep the implementation small.\n\n"
        "> What is the next step?\nCheck the stored conversation after mining completes.\n"
    )
    source.write_text(initial, encoding="utf-8")
    palace = tmp_path / "palace"
    _mine(source, palace)
    stored = get_collection(str(palace)).get(include=["documents", "metadatas"])
    before = dict(zip(stored["ids"], zip(stored["documents"], stored["metadatas"])))

    # Make a new generation eligible for mining before interrupting its read.
    pending = initial.replace(
        "Check the stored conversation", "REVIEW_PENDING: inspect the conversation"
    )
    revised = pending.replace("DECISION_OLD", "DECISION_NEW")
    source.write_text(pending, encoding="utf-8")
    original_stat = source.stat()
    original_fdopen = normalize_module.os.fdopen
    changed = False

    class ChangingReader:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def read(self, size=-1):
            nonlocal changed
            if size != -1 or changed:
                return self.stream.read(size)
            prefix = self.stream.read(32)
            assert "DECISION_OLD" in prefix
            # Rewrite bytes the reader has already consumed, before it reads
            # the rest. Both reads and the intervening write use the real file.
            source.write_text(revised, encoding="utf-8")
            os.utime(
                source,
                ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns + 60_000_000_000),
            )
            changed = True
            return prefix + self.stream.read()

    def changing_fdopen(fd, *args, **kwargs):
        file_stat = os.fstat(fd)
        stream = original_fdopen(fd, *args, **kwargs)
        if (file_stat.st_dev, file_stat.st_ino) == (
            original_stat.st_dev,
            original_stat.st_ino,
        ):
            return ChangingReader(stream)
        return stream

    with monkeypatch.context() as race:
        race.setattr(normalize_module.os, "fdopen", changing_fdopen)
        _mine(source, palace)

    assert changed, "the source rewrite was not interleaved during the read"
    assert source.read_text(encoding="utf-8") == revised
    print("Source after rewrite:", revised)
    stored = get_collection(str(palace)).get(include=["documents", "metadatas"])
    after = dict(zip(stored["ids"], zip(stored["documents"], stored["metadatas"])))
    assert after == before, (
        "an unstable read must preserve prior drawers and add no registry sentinel"
    )
    _mine(source, palace)
    assert any("DECISION_NEW" in doc for doc in _documents(palace)), (
        "A changed-during-read transcript was marked current and skipped on retry"
    )


def test_atomic_replacement_with_same_size_and_mtime_is_remined(tmp_path):
    source = tmp_path / "session.txt"
    initial = _INITIAL + "\n> Shipping plan?\nOLD_REPLACE_MARKER: keep transport local.\n"
    revised = initial.replace("OLD_REPLACE_MARKER", "NEW_REPLACE_MARKER")
    source.write_text(initial, encoding="utf-8")
    palace = tmp_path / "palace"
    _mine(source, palace)
    original_stat = source.stat()

    replacement = tmp_path / "replacement.txt"
    replacement.write_text(revised, encoding="utf-8")
    os.utime(replacement, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    os.replace(replacement, source)
    replaced_stat = source.stat()
    if not replaced_stat.st_ino or (replaced_stat.st_dev, replaced_stat.st_ino) == (
        original_stat.st_dev,
        original_stat.st_ino,
    ):
        pytest.skip("the filesystem does not expose distinct replacement inodes")
    assert replaced_stat.st_size == original_stat.st_size
    assert replaced_stat.st_mtime_ns == original_stat.st_mtime_ns

    _mine(source, palace)
    docs = _documents(palace)
    assert any("NEW_REPLACE_MARKER" in doc for doc in docs)
    assert not any("OLD_REPLACE_MARKER" in doc for doc in docs)


def test_legacy_mtime_only_rows_upgrade_once_then_skip(tmp_path, monkeypatch):
    source = tmp_path / "session.txt"
    source.write_text(_INITIAL, encoding="utf-8")
    palace = tmp_path / "palace"
    _mine(source, palace)
    collection = get_collection(str(palace))
    rows = collection.get(include=["metadatas"])
    assert rows["ids"]
    collection.update(
        ids=rows["ids"],
        metadatas=[{"source_fingerprint": None} for _ in rows["ids"]],
    )
    legacy_rows = collection.get(include=["metadatas"])
    assert all("source_fingerprint" not in meta for meta in legacy_rows["metadatas"])
    assert all("source_mtime" in meta for meta in legacy_rows["metadatas"])

    original_normalize = convo_miner.normalize_conversations
    normalizations = 0

    def counted_normalize(*args, **kwargs):
        nonlocal normalizations
        normalizations += 1
        return original_normalize(*args, **kwargs)

    monkeypatch.setattr(convo_miner, "normalize_conversations", counted_normalize)
    _mine(source, palace)
    assert normalizations == 1, "legacy mtime-only rows must be verified once"
    upgraded = get_collection(str(palace)).get(include=["metadatas"])
    assert all(meta.get("source_fingerprint") for meta in upgraded["metadatas"])

    _mine(source, palace)
    assert normalizations == 1, "an unchanged verified source should skip subsequent normalization"
