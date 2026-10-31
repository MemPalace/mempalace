"""EmbeddingGemma must load when the HF cache shards its blobs apart (#2601).

huggingface_hub >= 1.32 stores each cached file as ``hub/blobs/<2 hex>/<sha>``
and puts a symlink to it in the snapshot directory. The ONNX model and its
external-data sibling land in two different shards, so the snapshot's two
symlinks resolve apart, and onnxruntime >= 1.30 rejects the model with
"External data path escapes model directory".
"""

import errno
import hashlib
import os
import sys
import threading

import pytest

from mempalace.embedding import (
    _EXTERNAL_DATA_DIRNAME,
    _co_locate_external_data,
    _hf_hub_root,
)

_MODEL = "model_quantized.onnx"
_DATA = "model_quantized.onnx_data"


def _symlink_or_skip(target, link_name):
    try:
        os.symlink(target, link_name)
    except (OSError, NotImplementedError) as exc:  # Windows without privilege
        pytest.skip(f"os.symlink unavailable here: {exc}")


def _sharded_hub(tmp_path):
    """Build a hub whose two model files live in different blob shards."""
    hub = tmp_path / "hub"
    (hub / "blobs" / "6a").mkdir(parents=True)
    (hub / "blobs" / "2d").mkdir(parents=True)
    (hub / "blobs" / "6a" / "aabbcc").write_bytes(b"graph-bytes")
    (hub / "blobs" / "2d" / "ddeeff").write_bytes(b"weight-bytes-weight-bytes")
    snapshot = (
        hub / "models--onnx-community--embeddinggemma-300m-ONNX" / "snapshots" / "rev1" / "onnx"
    )
    snapshot.mkdir(parents=True)
    _symlink_or_skip(hub / "blobs" / "6a" / "aabbcc", snapshot / _MODEL)
    _symlink_or_skip(hub / "blobs" / "2d" / "ddeeff", snapshot / _DATA)
    return hub, snapshot


def test_hf_hub_root_walks_up_from_a_sharded_blob(tmp_path):
    blob = str(tmp_path / "hub" / "blobs" / "6a" / "aabbcc")
    assert _hf_hub_root(blob) == str(tmp_path / "hub")


def test_hf_hub_root_returns_none_for_an_unsharded_path(tmp_path):
    assert _hf_hub_root(str(tmp_path / "somewhere" / "model.onnx")) is None


def test_split_shards_are_colocated(tmp_path):
    hub, snapshot = _sharded_hub(tmp_path)

    loaded = _co_locate_external_data(str(snapshot / _MODEL), str(snapshot / _DATA))

    assert os.path.dirname(loaded) != str(snapshot)
    with open(loaded, "rb") as handle:
        assert handle.read() == b"graph-bytes"
    # The protobuf names its external data; the co-located directory has to
    # carry both files under exactly those names or ORT still cannot find them.
    assert sorted(os.listdir(os.path.dirname(loaded))) == sorted([_MODEL, _DATA])
    with open(os.path.join(os.path.dirname(loaded), _DATA), "rb") as handle:
        assert handle.read() == b"weight-bytes-weight-bytes"
    assert os.path.dirname(loaded).startswith(str(hub / _EXTERNAL_DATA_DIRNAME))


def test_blobs_are_hardlinked_not_copied(tmp_path):
    hub, snapshot = _sharded_hub(tmp_path)

    loaded = _co_locate_external_data(str(snapshot / _MODEL), str(snapshot / _DATA))

    blob = os.path.realpath(str(snapshot / _MODEL))
    assert os.stat(loaded).st_nlink == 2
    assert os.stat(blob).st_ino == os.stat(loaded).st_ino
    assert os.stat(blob).st_size == os.path.getsize(loaded)
    assert hub.exists()


def test_colocated_input_is_returned_untouched(tmp_path):
    plain = tmp_path / "flat"
    plain.mkdir()
    (plain / _MODEL).write_bytes(b"graph-bytes")
    (plain / _DATA).write_bytes(b"weight-bytes")
    model_path = str(plain / _MODEL)

    assert _co_locate_external_data(model_path, str(plain / _DATA)) == model_path
    assert not (tmp_path / _EXTERNAL_DATA_DIRNAME).exists()


def test_flat_blob_store_is_left_alone(tmp_path):
    """huggingface_hub < 1.32 keeps both blobs in one directory: nothing to do.

    The snapshot symlinks resolve apart from each other but into the same real
    directory, which is the layout ORT accepts. The early return has to fire
    even though the path *is* inside a hub, or every load would rebuild a
    private copy of a model that already works.
    """
    hub = tmp_path / "hub"
    (hub / "blobs").mkdir(parents=True)
    (hub / "blobs" / "aabbcc").write_bytes(b"graph-bytes")
    (hub / "blobs" / "ddeeff").write_bytes(b"weight-bytes")
    snapshot = (
        hub / "models--onnx-community--embeddinggemma-300m-ONNX" / "snapshots" / "rev1" / "onnx"
    )
    snapshot.mkdir(parents=True)
    _symlink_or_skip(hub / "blobs" / "aabbcc", snapshot / _MODEL)
    _symlink_or_skip(hub / "blobs" / "ddeeff", snapshot / _DATA)
    model_path = str(snapshot / _MODEL)

    assert _co_locate_external_data(model_path, str(snapshot / _DATA)) == model_path
    assert not (hub / _EXTERNAL_DATA_DIRNAME).exists()


def test_unrecognised_layout_is_not_guessed_at(tmp_path):
    left = tmp_path / "one"
    right = tmp_path / "two"
    left.mkdir()
    right.mkdir()
    (left / _MODEL).write_bytes(b"graph-bytes")
    (right / _DATA).write_bytes(b"weight-bytes")
    model_path = str(left / _MODEL)

    assert _co_locate_external_data(model_path, str(right / _DATA)) == model_path


def test_second_load_reuses_the_same_directory(tmp_path):
    hub, snapshot = _sharded_hub(tmp_path)
    first = _co_locate_external_data(str(snapshot / _MODEL), str(snapshot / _DATA))
    entries_before = os.listdir(hub / _EXTERNAL_DATA_DIRNAME)

    second = _co_locate_external_data(str(snapshot / _MODEL), str(snapshot / _DATA))

    assert second == first
    assert os.listdir(hub / _EXTERNAL_DATA_DIRNAME) == entries_before


def test_copy_fallback_when_the_filesystem_refuses_to_link(tmp_path, monkeypatch):
    hub, snapshot = _sharded_hub(tmp_path)

    def refuse(src, dst):
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr(os, "link", refuse)

    loaded = _co_locate_external_data(str(snapshot / _MODEL), str(snapshot / _DATA))

    # The fallback has to produce the co-located directory, not just survive:
    # returning the snapshot path would read the same bytes and look identical.
    assert loaded == os.path.join(
        str(hub),
        _EXTERNAL_DATA_DIRNAME,
        hashlib.sha1(
            f"{os.path.realpath(str(snapshot / _MODEL))}\n"
            f"{os.path.realpath(str(snapshot / _DATA))}".encode()
        ).hexdigest()[:16],
        _MODEL,
    )
    with open(loaded, "rb") as handle:
        assert handle.read() == b"graph-bytes"
    with open(os.path.join(os.path.dirname(loaded), _DATA), "rb") as handle:
        assert handle.read() == b"weight-bytes-weight-bytes"
    # A copy, not a link: the blob keeps its original single reference.
    assert os.stat(loaded).st_nlink == 1


def test_stale_target_of_a_different_size_is_replaced(tmp_path):
    hub, snapshot = _sharded_hub(tmp_path)
    model_real = os.path.realpath(str(snapshot / _MODEL))
    data_real = os.path.realpath(str(snapshot / _DATA))
    stale_dir = os.path.join(
        str(hub),
        _EXTERNAL_DATA_DIRNAME,
        hashlib.sha1(f"{model_real}\n{data_real}".encode()).hexdigest()[:16],
    )
    os.makedirs(stale_dir)
    with open(os.path.join(stale_dir, _MODEL), "wb") as handle:
        handle.write(b"truncated")

    loaded = _co_locate_external_data(str(snapshot / _MODEL), str(snapshot / _DATA))

    assert loaded == os.path.join(stale_dir, _MODEL)
    with open(loaded, "rb") as handle:
        assert handle.read() == b"graph-bytes"


def test_unwritable_hub_falls_back_to_the_snapshot_path(tmp_path, monkeypatch, caplog):
    hub, snapshot = _sharded_hub(tmp_path)
    real_makedirs = os.makedirs

    def refuse(path, *args, **kwargs):
        if _EXTERNAL_DATA_DIRNAME in str(path):
            raise OSError(errno.EACCES, "permission denied")
        return real_makedirs(path, *args, **kwargs)

    monkeypatch.setattr(os, "makedirs", refuse)

    assert _co_locate_external_data(str(snapshot / _MODEL), str(snapshot / _DATA)) == str(
        snapshot / _MODEL
    )
    assert "co-locate" in caplog.text.lower()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink layout")
def test_lazy_load_builds_the_session_from_the_colocated_path(tmp_path, monkeypatch):
    """_lazy_load must hand ORT the co-located path, not the snapshot symlink."""
    import mempalace.embedding as embedding

    hub, snapshot = _sharded_hub(tmp_path)
    seen = {}

    class _FakeSession:
        def __init__(self, path, **kwargs):
            seen["path"] = path

        def get_outputs(self):
            return [type("Output", (), {"name": "sentence_embedding"})()]

    class _FakeTokenizer:
        @classmethod
        def from_file(cls, path):
            return cls()

        def enable_padding(self):
            return None

        def enable_truncation(self, max_length=None):
            return None

    class _FakeHub:
        @staticmethod
        def hf_hub_download(repo, filename=None, subfolder=None):
            return str(snapshot / filename)

    monkeypatch.setitem(
        sys.modules, "onnxruntime", type("ort", (), {"InferenceSession": _FakeSession})
    )
    monkeypatch.setitem(sys.modules, "huggingface_hub", _FakeHub)
    monkeypatch.setitem(sys.modules, "tokenizers", type("tok", (), {"Tokenizer": _FakeTokenizer}))
    monkeypatch.setitem(sys.modules, "numpy", pytest.importorskip("numpy"))

    instance = embedding.EmbeddinggemmaONNX.__new__(embedding.EmbeddinggemmaONNX)
    instance._session = None
    instance._load_lock = threading.Lock()
    instance._intra_op_num_threads = 0
    instance._providers = ["CPUExecutionProvider"]

    instance._lazy_load()

    assert os.path.dirname(seen["path"]) != str(snapshot)
    assert seen["path"].startswith(str(hub / _EXTERNAL_DATA_DIRNAME))
    assert os.path.basename(seen["path"]) == _MODEL
    assert instance._session is not None
