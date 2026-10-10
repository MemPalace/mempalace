"""Gather-before-dequantize rewrite of quantized token lookups (#2681).

The models here are hand-encoded ONNX protobufs, built with the same wire
helpers the rewrite uses, so the tests need neither the ``onnx`` package
nor a downloaded model.
"""

import logging
import os
import re
import struct
from types import SimpleNamespace

import numpy as np
import pytest

ort = pytest.importorskip("onnxruntime")

from mempalace import embedding  # noqa: E402
from mempalace.onnx_graph import (  # noqa: E402
    _GRAPH_NODE,
    _MODEL_GRAPH,
    _Node,
    _encode_varint,
    _fields,
    _len_field,
    _string_field,
    gather_before_dequantize,
)

FLOAT, INT8, INT64 = 1, 3, 7
VOCAB, DIM = 50, 8

# ONNX Runtime < 1.21 ignores the external-data folder option for a model
# loaded from memory and looks in the working directory instead.
_FOLDER_OPTION_HONOURED = embedding._ort_honours_external_data_folder(ort)
needs_external_data_folder = pytest.mark.skipif(
    not _FOLDER_OPTION_HONOURED,
    reason=f"onnxruntime {ort.__version__} ignores the external-data folder option",
)
# ONNX Runtime rejects external data that resolves outside the model's
# directory from 1.24 on; 1.19-1.23 load it.
_ORT_MINOR = tuple(int(x) for x in re.match(r"(\d+)\.(\d+)", ort.__version__).groups())
_VALIDATES_EXTERNAL_DATA_PATHS = _ORT_MINOR >= (1, 24)


def _varint_field(number, value):
    return _encode_varint(number << 3) + _encode_varint(value)


def _tensor(name, dtype, dims, raw=None, location=None):
    payload = b"".join(_varint_field(1, d) for d in dims) + _varint_field(2, dtype)
    payload += _string_field(8, name)
    if location is None:
        payload += _len_field(9, raw)
    else:
        entries = (("location", location), ("offset", "0"), ("length", str(len(raw))))
        for key, value in entries:
            payload += _len_field(13, _string_field(1, key) + _string_field(2, value))
        payload += _varint_field(14, 1)  # data_location = EXTERNAL
    return _len_field(5, payload)


def _value_info(number, name, dtype, dims):
    shape = b""
    for d in dims:
        dim = _string_field(2, d) if isinstance(d, str) else _varint_field(1, d)
        shape += _len_field(1, dim)
    tensor_type = _varint_field(1, dtype) + _len_field(2, shape)
    return _len_field(number, _string_field(1, name) + _len_field(2, _len_field(1, tensor_type)))


def _node(op, inputs, outputs, name, attrs=()):
    payload = b"".join(_string_field(1, x) for x in inputs)
    payload += b"".join(_string_field(2, x) for x in outputs)
    payload += _string_field(3, name) + _string_field(4, op)
    for attr_name, value in attrs:
        payload += _len_field(
            5, _string_field(1, attr_name) + _varint_field(3, value) + _varint_field(20, 2)
        )
    return _len_field(1, payload)


def _table():
    rng = np.random.default_rng(7)
    return rng.integers(-128, 128, size=(VOCAB, DIM), dtype=np.int8)


def _model(
    path,
    *,
    scale_dims=(1,),
    extra_consumer=False,
    dequantized_is_output=False,
    external=None,
    with_value_info=True,
):
    """Write a model computing Relu(Gather(DequantizeLinear(table, scale, zp), ids))."""
    table = _table()
    scale_count = int(np.prod(scale_dims)) if scale_dims else 1
    scale = struct.pack(f"<{scale_count}f", *([0.0123] * scale_count))
    zero_point = bytes([3] * scale_count)
    initializers = [
        _tensor("table_q", INT8, (VOCAB, DIM), table.tobytes(), location=external),
        _tensor("scale", FLOAT, scale_dims, scale),
        _tensor("zp", INT8, scale_dims, zero_point),
    ]
    nodes = [
        _node(
            "DequantizeLinear",
            ["table_q", "scale", "zp"],
            ["table_f"],
            "dq",
            [("axis", 0)] if scale_dims == (VOCAB,) else (),
        ),
        _node("Gather", ["table_f", "ids"], ["emb"], "gather"),
        _node("Relu", ["emb"], ["out"], "relu"),
    ]
    if extra_consumer:
        nodes.append(_node("Identity", ["table_f"], ["table_copy"], "copy"))
    outputs = [_value_info(12, "out", FLOAT, ["n", DIM]), _value_info(12, "emb", FLOAT, ["n", DIM])]
    if dequantized_is_output:
        outputs.append(_value_info(12, "table_f", FLOAT, [VOCAB, DIM]))
    if extra_consumer:
        outputs.append(_value_info(12, "table_copy", FLOAT, [VOCAB, DIM]))
    value_info = [_value_info(13, "table_f", FLOAT, [VOCAB, DIM])] if with_value_info else []
    graph = b"".join(nodes) + _string_field(2, "g") + b"".join(initializers)
    graph += _value_info(11, "ids", INT64, ["n"]) + b"".join(outputs) + b"".join(value_info)
    model = _varint_field(1, 8) + _string_field(2, "unit-test-producer")
    model += _len_field(7, graph) + _len_field(8, _string_field(1, "") + _varint_field(2, 13))
    path.write_bytes(model)
    if external is not None:
        return table.tobytes()
    return None


def _run(model, ids, folder=None):
    so = ort.SessionOptions()
    if folder is not None:
        so.add_session_config_entry("session.model_external_initializers_file_folder_path", folder)
    session = ort.InferenceSession(model, sess_options=so, providers=["CPUExecutionProvider"])
    return session.run(None, {"ids": ids})


IDS = np.array([0, 7, 7, 49, 23], dtype=np.int64)


def _node_ops(model_bytes):
    graph = next(v for n, _w, v, _r in _fields(model_bytes) if n == _MODEL_GRAPH)
    return [_Node.parse(r, v) for n, _w, v, r in _fields(graph) if n == _GRAPH_NODE]


def test_rewrite_gives_bitwise_identical_outputs(tmp_path):
    path = tmp_path / "m.onnx"
    _model(path)
    patch = gather_before_dequantize(str(path))
    assert patch is not None and patch.rewritten == 1
    assert patch.external_data_dir is None

    before = _run(str(path), IDS)
    after = _run(patch.model_bytes, IDS)
    for x, y in zip(before, after):
        assert x.dtype == y.dtype and np.array_equal(x, y)


def test_rewrite_gathers_the_quantized_table_then_dequantizes_the_rows(tmp_path):
    path = tmp_path / "m.onnx"
    _model(path)
    nodes = _node_ops(gather_before_dequantize(str(path)).model_bytes)

    assert [n.op_type for n in nodes] == ["Gather", "DequantizeLinear", "Relu"]
    gather, dq, _relu = nodes
    assert gather.inputs == ["table_q", "ids"]
    assert dq.inputs == [gather.outputs[0], "scale", "zp"]
    assert dq.outputs == ["emb"]


def test_rewrite_drops_value_info_for_the_full_dequantized_table(tmp_path):
    path = tmp_path / "m.onnx"
    _model(path)
    patched = gather_before_dequantize(str(path)).model_bytes
    assert b"table_f" not in patched


def test_rewrite_keeps_unrelated_fields(tmp_path):
    path = tmp_path / "m.onnx"
    _model(path)
    patched = gather_before_dequantize(str(path)).model_bytes
    assert b"unit-test-producer" in patched
    assert len(patched) < len(path.read_bytes())  # only the value_info went away


@pytest.mark.parametrize(
    "kwargs",
    [
        {"scale_dims": (VOCAB,)},  # per-row scale: dequantize does not commute
        {"extra_consumer": True},  # the full table is needed elsewhere
        {"dequantized_is_output": True},
    ],
    ids=["per-axis-scale", "second-consumer", "graph-output"],
)
def test_ineligible_lookups_are_left_alone(tmp_path, kwargs):
    path = tmp_path / "m.onnx"
    _model(path, **kwargs)
    assert gather_before_dequantize(str(path)) is None


def test_scalar_scale_without_dims_is_eligible(tmp_path):
    path = tmp_path / "m.onnx"
    _model(path, scale_dims=())
    patch = gather_before_dequantize(str(path))
    assert patch is not None
    for x, y in zip(_run(str(path), IDS), _run(patch.model_bytes, IDS)):
        assert np.array_equal(x, y)


@needs_external_data_folder
def test_external_data_loads_from_memory(tmp_path):
    path = tmp_path / "m.onnx"
    raw = _model(path, external="weights.bin")
    (tmp_path / "weights.bin").write_bytes(raw)
    patch = gather_before_dequantize(str(path))

    assert patch.external_data_dir == os.path.realpath(tmp_path)
    for x, y in zip(_run(str(path), IDS), _run(patch.model_bytes, IDS, patch.external_data_dir)):
        assert np.array_equal(x, y)


def test_symlinked_external_data_points_at_the_real_file(tmp_path):
    """huggingface_hub links snapshot files to content-addressed blobs; the
    rewritten model names the blob, in the blob's directory."""
    blobs = tmp_path / "blobs"
    snapshot = tmp_path / "snapshot"
    blobs.mkdir()
    snapshot.mkdir()
    path = snapshot / "m.onnx"
    raw = _model(path, external="weights.bin")
    (blobs / "9f2c").write_bytes(raw)
    try:
        os.symlink(blobs / "9f2c", snapshot / "weights.bin")
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"os.symlink unavailable here: {exc}")

    patch = gather_before_dequantize(str(path))
    assert patch.external_data_dir == os.path.realpath(blobs)
    assert b"9f2c" in patch.model_bytes
    # The model file and its data resolve into different real directories,
    # which ONNX Runtime >= 1.24 refuses when loading from the path (#2601);
    # the patched model, loaded from memory, is not subject to that.
    if _VALIDATES_EXTERNAL_DATA_PATHS:
        with pytest.raises(Exception, match="escapes"):
            _run(str(path), IDS)
    if not _FOLDER_OPTION_HONOURED:
        return  # the in-memory load below needs onnxruntime >= 1.21
    plain = tmp_path / "plain"
    plain.mkdir()
    _model(plain / "m.onnx", external="weights.bin")
    (plain / "weights.bin").write_bytes(raw)
    reference = _run(str(plain / "m.onnx"), IDS)
    for x, y in zip(reference, _run(patch.model_bytes, IDS, patch.external_data_dir)):
        assert np.array_equal(x, y)


def test_session_falls_back_to_the_file_when_the_patch_is_rejected(tmp_path, monkeypatch):
    path = tmp_path / "m.onnx"
    _model(path)
    patch = gather_before_dequantize(str(path))
    real = ort.InferenceSession
    loaded = []

    def refuse_bytes(model, *args, **kwargs):
        if isinstance(model, bytes):
            raise RuntimeError("injected rejection")
        loaded.append(model)
        return real(model, *args, **kwargs)

    monkeypatch.setattr(ort, "InferenceSession", refuse_bytes)
    session = embedding._new_embeddinggemma_session(
        ort, str(path), patch, 0, ["CPUExecutionProvider"]
    )
    assert loaded == [str(path)]
    assert session.run(None, {"ids": IDS})


@pytest.mark.parametrize(
    "version, honoured",
    [
        ("1.19.2", False),
        ("1.20.1", False),
        ("1.21.0", True),
        ("1.22.0.dev20250101", True),
        ("1.30.0", True),
        ("2.0.0", True),
        ("", False),
        ("unknown", False),
    ],
)
def test_external_data_folder_needs_onnxruntime_1_21(version, honoured):
    assert (
        embedding._ort_honours_external_data_folder(SimpleNamespace(__version__=version))
        is honoured
    )


def _recording_ort(version):
    """An ``onnxruntime`` stand-in that records what each session loads from."""
    loads = []

    class Options:
        def __init__(self):
            self.config = {}

        def add_session_config_entry(self, key, value):
            self.config[key] = value

    def session(model, sess_options=None, providers=None):
        loads.append((model, getattr(sess_options, "config", {})))
        return object()

    return SimpleNamespace(
        __version__=version, SessionOptions=Options, InferenceSession=session
    ), loads


def test_old_onnxruntime_loads_an_external_data_patch_from_the_path(tmp_path, caplog):
    path = tmp_path / "m.onnx"
    raw = _model(path, external="weights.bin")
    (tmp_path / "weights.bin").write_bytes(raw)
    patch = gather_before_dequantize(str(path))
    fake, loads = _recording_ort("1.20.1")

    with caplog.at_level(logging.WARNING, logger=embedding.logger.name):
        embedding._new_embeddinggemma_session(fake, str(path), patch, 0, ["CPUExecutionProvider"])
    assert [model for model, _config in loads] == [str(path)]
    assert not caplog.records


def test_old_onnxruntime_still_uses_a_patch_without_external_data(tmp_path):
    path = tmp_path / "m.onnx"
    _model(path)
    patch = gather_before_dequantize(str(path))
    fake, loads = _recording_ort("1.19.2")

    embedding._new_embeddinggemma_session(fake, str(path), patch, 0, ["CPUExecutionProvider"])
    assert [model for model, _config in loads] == [patch.model_bytes]


def test_new_onnxruntime_points_the_patch_at_its_external_data(tmp_path):
    path = tmp_path / "m.onnx"
    raw = _model(path, external="weights.bin")
    (tmp_path / "weights.bin").write_bytes(raw)
    patch = gather_before_dequantize(str(path))
    fake, loads = _recording_ort("1.21.0")

    embedding._new_embeddinggemma_session(fake, str(path), patch, 0, ["CPUExecutionProvider"])
    assert loads == [
        (
            patch.model_bytes,
            {"session.model_external_initializers_file_folder_path": patch.external_data_dir},
        )
    ]


def test_external_data_session_reads_the_model_weights_not_the_working_directory(
    tmp_path, monkeypatch, caplog
):
    """On the installed onnxruntime, whichever way the session loads, it reads
    the model's own weights: a same-named file in the working directory, which
    onnxruntime < 1.21 would pick up for an in-memory model, is never used."""
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    path = model_dir / "m.onnx"
    raw = _model(path, external="weights.bin")
    (model_dir / "weights.bin").write_bytes(raw)
    expected = _run(str(path), IDS)
    patch = gather_before_dequantize(str(path))

    decoy = tmp_path / "cwd"
    decoy.mkdir()
    (decoy / "weights.bin").write_bytes(bytes(len(raw)))
    monkeypatch.chdir(decoy)
    with caplog.at_level(logging.WARNING, logger=embedding.logger.name):
        session = embedding._new_embeddinggemma_session(
            ort, str(path), patch, 0, ["CPUExecutionProvider"]
        )
    for x, y in zip(expected, session.run(None, {"ids": IDS})):
        assert x.dtype == y.dtype and np.array_equal(x, y)
    assert not caplog.records


def test_patch_is_skipped_quietly_for_a_missing_file(tmp_path, caplog):
    assert embedding._embeddinggemma_gather_patch(str(tmp_path / "absent.onnx")) is None
    assert not caplog.records


def test_unreadable_graph_falls_back_with_a_warning(tmp_path, caplog):
    path = tmp_path / "m.onnx"
    path.write_bytes(b"\x3a\xff\xff\xff\xff\x0f")  # graph field claiming 4 GiB
    assert embedding._embeddinggemma_gather_patch(str(path)) is None
    assert "Could not rewrite" in caplog.text


# The test session redirects HOME, so the HF cache is out of reach; point this
# at a downloaded model_quantized.onnx to check the real model.
_REAL_MODEL = os.environ.get("MEMPALACE_TEST_EMBEDDINGGEMMA_ONNX", "")


@pytest.mark.skipif(
    not os.path.isfile(_REAL_MODEL), reason="MEMPALACE_TEST_EMBEDDINGGEMMA_ONNX not set"
)
@needs_external_data_folder
def test_real_embeddinggemma_vectors_are_bitwise_identical():
    model_path = _REAL_MODEL
    patch = gather_before_dequantize(model_path)
    assert patch is not None and patch.rewritten == 1
    ids = np.array([[2, 9259, 236787, 8433, 26132, 1], [2, 1437, 1, 0, 0, 0]], dtype=np.int64)
    mask = (ids != 0).astype(np.int64)
    feed = {"input_ids": ids, "attention_mask": mask}
    before = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"]).run(None, feed)
    after = _run_feed(patch, feed)
    for x, y in zip(before, after):
        assert np.array_equal(x, y)


def _run_feed(patch, feed):
    so = ort.SessionOptions()
    so.add_session_config_entry(
        "session.model_external_initializers_file_folder_path", patch.external_data_dir
    )
    session = ort.InferenceSession(
        patch.model_bytes, sess_options=so, providers=["CPUExecutionProvider"]
    )
    return session.run(None, feed)
