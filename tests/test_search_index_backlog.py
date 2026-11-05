"""Metadata-only search indexing selection, shared by both store formats."""

import pytest

from commonplace.handoff.models import ThreadMessage
from commonplace.handoff.thread_store import ThreadStore, ThreadStoreV2


@pytest.fixture(params=[ThreadStore, ThreadStoreV2], ids=["v1", "v2"])
def store(request):
    value = request.param(":memory:")
    yield value
    value.close()


def messages(count=2, *, text="synthetic words", kind="message"):
    return [ThreadMessage(seq, "user", kind, text, "{}") for seq in range(count)]


def put(store, source, *, workspace="workspace", **kwargs):
    return store.store(workspace, source, messages(**kwargs), actor="actor")


def complete(store, thread):
    through = thread.message_count - 1
    store.begin_mirror(thread.id, through)
    store.finish_mirror(thread.id, through)


def ids(store, **kwargs):
    return [thread.id for thread in store.index_backlog("workspace", **kwargs)]


def test_u1_a_import_namespaces_and_progress(store):
    acl = put(store, "import:acl")
    private = put(store, "import-private:user:snapshot")
    lagging = put(store, "import:lagging")
    store.begin_mirror(lagging.id, 0)
    store.finish_mirror(lagging.id, 0)
    done = put(store, "import:done")
    complete(store, done)
    for source in ("capture:old", "handoff", "imports:other", "Import:other", "import-private"):
        put(store, source)
    put(store, "import:foreign", workspace="other")

    assert ids(store) == sorted([acl.id, private.id, lagging.id])
    assert store.index_counts("workspace") == {"threads_indexed": 1, "threads_pending": 3}
    assert store.index_counts("missing") == {"threads_indexed": 0, "threads_pending": 0}


def test_u1_b_enrolled_threads_and_append_resume(store):
    capture = put(store, "capture:active")
    handoff = put(store, "handoff:retry")
    store.begin_mirror(capture.id, -1, bootstrap=-1)
    store.begin_mirror(handoff.id, 1)
    assert ids(store) == sorted([capture.id, handoff.id])
    complete(store, capture)
    complete(store, handoff)
    assert ids(store) == []
    store.append(capture.id, messages(3))
    assert ids(store) == [capture.id]
    assert store.index_counts("workspace") == {"threads_indexed": 1, "threads_pending": 1}


def test_u1_c_enrolled_import_group_holders_and_forks(store):
    holder = put(store, "capture:holder")
    fork = put(store, "capture-fork:fork")
    assert ids(store) == []
    for thread in (holder, fork):
        store.begin_mirror(thread.id, -1, bootstrap=-1)
    assert ids(store) == sorted([holder.id, fork.id])
    complete(store, holder)
    assert ids(store) == [fork.id]


def test_u1_d_cursor_marker_only_and_metadata_reads(store, monkeypatch):
    threads = [put(store, f"import:item-{i}") for i in range(4)]
    marker = put(store, "import:marker", count=1, text="", kind="capture.base")
    expected = sorted(thread.id for thread in [*threads, marker])

    def no_words(*args, **kwargs):
        pytest.fail("backlog and counts must not read transcript content")

    monkeypatch.setattr(store, "messages", no_words)
    monkeypatch.setattr(store, "chunks", no_words)
    assert ids(store, limit=2) == expected[:2]
    assert ids(store, after_id=expected[1], limit=2) == expected[2:4]
    assert ids(store, after_id=expected[3], limit=2) == expected[4:]
    assert ids(store, after_id=expected[-1], limit=2) == []
    assert ids(store, limit=0) == []
    assert ids(store, limit=-1) == []
    assert store.index_counts("workspace") == {"threads_indexed": 0, "threads_pending": 5}
    complete(store, marker)
    assert marker.id not in ids(store)
    assert store.index_counts("workspace") == {"threads_indexed": 1, "threads_pending": 4}
