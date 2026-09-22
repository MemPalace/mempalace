"""Conversation freshness must describe the source bytes actually mined."""

import os

import pytest

from mempalace import convo_miner
from mempalace import normalize as normalize_module
from mempalace._source_state import source_fingerprint
from mempalace.palace import (
    NORMALIZE_VERSION,
    file_already_mined,
    get_collection,
    prefetch_mined_set,
)


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
    """File freshness can change while sync's source directory stays the same."""
    source = tmp_path / "session.txt"
    initial = _INITIAL + "\n> Shipping plan?\nOLD_REPLACE_MARKER: keep transport local.\n"
    revised = initial.replace("OLD_REPLACE_MARKER", "NEW_REPLACE_MARKER")
    source.write_text(initial, encoding="utf-8")
    palace = tmp_path / "palace"
    _mine(source, palace)
    original_stat = source.stat()
    original_rows = get_collection(str(palace)).get(
        where={"source_file": str(source)}, include=["metadatas"]
    )
    assert original_rows["ids"]
    original_fingerprints = {m["source_fingerprint"] for m in original_rows["metadatas"]}
    directory_inode = source.parent.stat().st_ino
    directory_identity = str(directory_inode) if directory_inode else None
    assert all(m.get("source_dir_ino") == directory_identity for m in original_rows["metadatas"])

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
    replaced_rows = get_collection(str(palace)).get(
        where={"source_file": str(source)}, include=["metadatas"]
    )
    assert replaced_rows["ids"]
    assert all(
        m["source_fingerprint"] not in original_fingerprints
        and m.get("source_dir_ino") == directory_identity
        for m in replaced_rows["metadatas"]
    )


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


def test_scoped_prefetch_preserves_fingerprints_and_legacy_metadata(collection, tmp_path):
    source = tmp_path / "verified.txt"
    legacy = tmp_path / "legacy.txt"
    source.write_text("verified conversation", encoding="utf-8")
    legacy.write_text("legacy conversation", encoding="utf-8")
    # Keep legacy float metadata exactly representable through the backend.
    os.utime(source, (1_700_000_000, 1_700_000_000))
    os.utime(legacy, (1_700_000_000, 1_700_000_000))
    fingerprint = source_fingerprint(source.stat())
    common = {"normalize_version": NORMALIZE_VERSION, "source_mtime": source.stat().st_mtime}
    collection.add(
        ids=["verified", "legacy", "general", "sweep", "outside"],
        documents=["stored conversation"] * 5,
        metadatas=[
            {
                **common,
                "source_file": str(source),
                "extract_mode": "exchange",
                "source_fingerprint": fingerprint,
            },
            {
                **common,
                "source_file": str(legacy),
                "source_mtime": legacy.stat().st_mtime,
                "ingest_mode": "convos",
            },
            {
                **common,
                "source_file": str(source),
                "extract_mode": "general",
                "source_fingerprint": "other-mode-snapshot",
            },
            {**common, "source_file": "sweep.txt", "ingest_mode": "sweep"},
            {**common, "source_file": "outside.txt", "extract_mode": "exchange"},
        ],
    )
    sources = [str(source), str(legacy), "sweep.txt"]

    assert prefetch_mined_set(
        collection, extract_mode="exchange", source_files=sources, source_fingerprints=True
    ) == {str(source): fingerprint, str(legacy): None}
    assert prefetch_mined_set(
        collection, extract_mode="general", source_files=sources, source_fingerprints=True
    ) == {str(source): "other-mode-snapshot"}
    assert prefetch_mined_set(collection, extract_mode="exchange", source_files=sources) == {
        str(source): source.stat().st_mtime,
        str(legacy): legacy.stat().st_mtime,
    }
    assert file_already_mined(
        collection, str(source), extract_mode="exchange", check_source_fingerprint=True
    )
    assert not file_already_mined(
        collection, str(legacy), extract_mode="exchange", check_source_fingerprint=True
    )
    assert not file_already_mined(
        collection, str(source), extract_mode="general", check_source_fingerprint=True
    )
    assert file_already_mined(collection, str(legacy), extract_mode="exchange", check_mtime=True)


def test_scoped_prefetch_keeps_paginated_snapshot_groups_separate(collection, tmp_path):
    source = tmp_path / "session.txt"
    source.write_text("first source snapshot", encoding="utf-8")
    before = source.stat()
    old_fingerprint = source_fingerprint(before)
    source.write_text("first source snapshot, with an appended turn", encoding="utf-8")
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
    fingerprint = source_fingerprint(source.stat())
    assert source.stat().st_mtime_ns == before.st_mtime_ns
    assert fingerprint != old_fingerprint

    common = {
        "normalize_version": NORMALIZE_VERSION,
        "source_file": str(source),
        "source_mtime": before.st_mtime,
        "extract_mode": "exchange",
        "chunk_total": 1001,
    }
    collection.add(
        ids=[f"chunk-{i}" for i in range(1001)],
        documents=["stored exchange"] * 1001,
        metadatas=[
            {**common, "source_fingerprint": old_fingerprint if i < 500 else fingerprint}
            for i in range(1001)
        ],
    )

    def prefetched():
        return prefetch_mined_set(
            collection,
            extract_mode="exchange",
            source_files=[str(source)],
            source_fingerprints=True,
        )

    assert prefetched() == {}, "partial generations must not combine into a complete source"
    assert not file_already_mined(
        collection, str(source), extract_mode="exchange", check_source_fingerprint=True
    )

    collection.add(
        ids=[f"remaining-{i}" for i in range(500)],
        documents=["recovered exchange"] * 500,
        metadatas=[{**common, "source_fingerprint": fingerprint}] * 500,
    )
    assert prefetched() == {str(source): fingerprint}
    assert file_already_mined(
        collection, str(source), extract_mode="exchange", check_source_fingerprint=True
    )


@pytest.mark.parametrize("source_fingerprints", [False, True], ids=["mtime", "fingerprint"])
def test_scoped_prefetch_retry_does_not_double_count_partial_page(
    collection, tmp_path, source_fingerprints
):
    source = tmp_path / "interrupted.txt"
    source.write_text("source with a partially filed generation", encoding="utf-8")
    # This test checks retry accounting, independent of float round-trip precision.
    os.utime(source, (1_700_000_000, 1_700_000_000))
    fingerprint = source_fingerprint(source.stat())
    common = {
        "normalize_version": NORMALIZE_VERSION,
        "extract_mode": "exchange",
        "source_mtime": source.stat().st_mtime,
        "source_fingerprint": fingerprint,
    }
    collection.add(
        ids=[f"partial-{i}" for i in range(1000)] + ["unrelated"],
        documents=["stored exchange"] * 1001,
        metadatas=[{**common, "source_file": str(source), "chunk_total": 1500}] * 1000
        + [{**common, "source_file": "unrelated.txt"}],
    )
    rejected_pages = []

    class RejectSecondScopedPage:
        def count(self):
            return collection.count()

        def get(self, **kwargs):
            where = kwargs.get("where", {})
            if isinstance(where.get("source_file"), dict) and kwargs.get("offset", 0):
                rejected_pages.append(kwargs["offset"])
                raise RuntimeError("scoped pagination temporarily unavailable")
            return collection.get(**kwargs)

    mined = prefetch_mined_set(
        RejectSecondScopedPage(),
        extract_mode="exchange",
        source_files=[str(source)],
        source_fingerprints=source_fingerprints,
    )
    assert rejected_pages, "the failure must occur after a real first scoped page"
    expected = fingerprint if source_fingerprints else source.stat().st_mtime
    assert mined == {"unrelated.txt": expected}, (
        "retrying a full scan must not count the first scoped page twice"
    )
    assert not file_already_mined(
        collection,
        str(source),
        extract_mode="exchange",
        check_mtime=True,
        check_source_fingerprint=source_fingerprints,
    )


_REWRITE_INITIAL = (
    "> Which plan?\nDECISION_OLD: keep the implementation small.\n\n"
    "> What is the next step?\nCheck the stored conversation after mining completes.\n"
)


def _rows(palace):
    rows = get_collection(str(palace)).get(include=["documents", "metadatas"])
    return dict(zip(rows["ids"], zip(rows["documents"], rows["metadatas"])))


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX change-time semantics")
def test_inplace_rewrite_with_same_size_and_restored_mtime_is_remined(tmp_path, monkeypatch):
    source = tmp_path / "session.txt"
    source.write_text(_REWRITE_INITIAL, encoding="utf-8")
    palace = tmp_path / "palace"
    _mine(source, palace)
    original_stat = source.stat()

    revised = _REWRITE_INITIAL.replace("DECISION_OLD", "DECISION_NEW")
    source.write_text(revised, encoding="utf-8")
    os.utime(source, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    changed_stat = source.stat()
    assert changed_stat.st_dev == original_stat.st_dev
    assert changed_stat.st_ino == original_stat.st_ino
    assert changed_stat.st_size == original_stat.st_size
    assert changed_stat.st_mtime_ns == original_stat.st_mtime_ns
    if changed_stat.st_ctime_ns == original_stat.st_ctime_ns:
        pytest.skip("the filesystem does not expose a distinct inode change time")

    _mine(source, palace)
    docs = [document for document, _ in _rows(palace).values()]
    assert any("DECISION_NEW" in doc for doc in docs), (
        "a same-size rewrite with restored mtime was skipped despite changed source bytes"
    )
    assert not any("DECISION_OLD" in doc for doc in docs)

    # Reading alone must not change freshness or force another normalization.
    source.read_text(encoding="utf-8")

    def unexpected_normalization(*args, **kwargs):
        pytest.fail("a stable source was normalized again after a read-only access")

    monkeypatch.setattr(convo_miner, "normalize_conversations", unexpected_normalization)
    _mine(source, palace)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX change-time semantics")
def test_restored_mtime_rewrite_during_read_preserves_prior_drawers(tmp_path, monkeypatch):
    source = tmp_path / "session.txt"
    source.write_text(_REWRITE_INITIAL, encoding="utf-8")
    palace = tmp_path / "palace"
    _mine(source, palace)
    before = _rows(palace)

    # Make a new generation eligible, then rewrite already-consumed bytes
    # while retaining that generation's inode, length and modification time.
    pending = _REWRITE_INITIAL.replace("stored conversation", "latest conversation")
    revised = pending.replace("DECISION_OLD", "DECISION_NEW")
    source.write_text(pending, encoding="utf-8")
    pending_stat = source.stat()
    os.utime(
        source,
        ns=(pending_stat.st_atime_ns, pending_stat.st_mtime_ns + 60_000_000_000),
    )
    original_stat = source.stat()
    original_fdopen = normalize_module.os.fdopen
    changed = False
    changed_stat = None

    class RewritingReader:
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
            nonlocal changed, changed_stat
            if size != -1 or changed:
                return self.stream.read(size)
            prefix = self.stream.read(32)
            assert "DECISION_OLD" in prefix
            source.write_text(revised, encoding="utf-8")
            os.utime(source, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
            changed_stat = source.stat()
            changed = True
            return prefix + self.stream.read()

    def rewriting_fdopen(fd, *args, **kwargs):
        file_stat = os.fstat(fd)
        stream = original_fdopen(fd, *args, **kwargs)
        if (file_stat.st_dev, file_stat.st_ino) == (
            original_stat.st_dev,
            original_stat.st_ino,
        ):
            return RewritingReader(stream)
        return stream

    with monkeypatch.context() as race:
        race.setattr(normalize_module.os, "fdopen", rewriting_fdopen)
        _mine(source, palace)

    assert changed, "the source rewrite was not interleaved during the read"
    assert changed_stat.st_dev == original_stat.st_dev
    assert changed_stat.st_ino == original_stat.st_ino
    assert changed_stat.st_size == original_stat.st_size
    assert changed_stat.st_mtime_ns == original_stat.st_mtime_ns
    if changed_stat.st_ctime_ns == original_stat.st_ctime_ns:
        pytest.skip("the filesystem does not expose a distinct inode change time")
    assert source.read_text(encoding="utf-8") == revised
    assert _rows(palace) == before, (
        "a timestamp-preserving rewrite during the read changed the previously filed snapshot"
    )

    _mine(source, palace)
    docs = [document for document, _ in _rows(palace).values()]
    assert any("DECISION_NEW" in doc for doc in docs)
    assert not any("DECISION_OLD" in doc for doc in docs)
