"""Compact readers keep decompression contexts within their SQLite snapshot."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from mempalace.backends import compact


@pytest.mark.parametrize("trained", [False, True], ids=["bootstrap", "dictionary"])
def test_scoped_and_vector_reads_do_not_share_decompressors(tmp_path, monkeypatch, trained):
    backend = compact.CompactBackend()
    try:
        col = backend.create_collection(str(tmp_path), "drawers")
        if trained:
            samples = [f"sample {i} " + (f"topic {i % 7} details " * 100) for i in range(200)]
            col.add(
                ids=[f"sample-{i}" for i in range(len(samples))],
                documents=samples,
                embeddings=[[0, 1]] * len(samples),
            )
            assert col._handle.conn.execute("SELECT count(*) FROM dictionaries").fetchone()[0] == 1
        text = "needle exact\0 café \udc80 " * 100
        col.add(ids=["needle"], documents=[text], embeddings=[[1, 0]])

        # Hold vector hydration inside its context while scoped lexical hydration
        # enters. Detect forbidden context reuse before it can corrupt native state.
        factory = compact.zstd.ZstdDecompressor
        entered = threading.Event()
        release = threading.Event()
        caller = threading.get_ident()
        overlaps = []

        class CheckedDecompressor:
            def __init__(self, *args, **kwargs):
                self.inner = factory(*args, **kwargs)
                self.active = threading.Lock()

            def decompress(self, blob):
                if not self.active.acquire(blocking=False):
                    overlaps.append(threading.get_ident())
                    raise AssertionError("overlapping Zstandard context reuse")
                try:
                    if threading.get_ident() != caller:
                        entered.set()
                        assert release.wait(5), "lexical read did not finish"
                    return self.inner.decompress(blob)
                finally:
                    self.active.release()

        monkeypatch.setattr(compact.zstd, "ZstdDecompressor", CheckedDecompressor)
        with ThreadPoolExecutor(max_workers=1) as executor:
            vector = executor.submit(col.query, query_embeddings=[[1, 0]], n_results=1)
            try:
                assert entered.wait(5), "vector hydration did not start"
                lexical = col.union_lexical_search(query="needle", n_results=1)
                assert [(hit.id, hit.document) for hit in lexical.hits] == [("needle", text)]
            finally:
                release.set()
            assert vector.result(timeout=5).documents == [[text]]
            assert not overlaps
    finally:
        backend.close()


def test_scoped_dictionary_reads_stay_in_their_workspace(tmp_path):
    backend = compact.CompactBackend()
    try:
        collections = []
        # Distinct palace paths own distinct handles, even with the same dictionary ID.
        for workspace in ("first", "second"):
            col = backend.create_collection(str(tmp_path / workspace), "drawers")
            samples = [f"{workspace} sample {i} " * 100 for i in range(200)]
            col.add(
                ids=[f"sample-{i}" for i in range(len(samples))],
                documents=samples,
                embeddings=[[0, 1]] * len(samples),
            )
            assert col._handle.conn.execute("SELECT id FROM dictionaries").fetchone()[0] == 1
            text = f"needle {workspace}\0 café \udc80 " * 100
            col.add(ids=["needle"], documents=[text], embeddings=[[1, 0]])
            collections.append((col, text))
        for _ in range(2):
            for col, text in collections:
                hits = col.union_lexical_search(query="needle", n_results=1).hits
                assert [(hit.id, hit.document) for hit in hits] == [("needle", text)]
    finally:
        backend.close()
