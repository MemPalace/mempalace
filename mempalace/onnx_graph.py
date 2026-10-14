"""Load-time rewrites of ONNX graphs, done on the protobuf wire format.

Kept dependency-free on purpose: the rewrite below touches a handful of
fields, so it reads and writes them with a small varint codec instead of
pulling in the ``onnx`` package. Every field it does not rewrite is copied
through byte for byte, so the result differs from the input only where it
means to.

The rewrite (#2681): a q8 model that stores its token table quantized looks
tokens up as ``Gather(DequantizeLinear(table_q, scale, zero_point), ids)``.
ONNX Runtime evaluates that literally, so every ``run()``, even for a
two-word query, first dequantizes the whole table into a float32 copy
(768 MiB for EmbeddingGemma's 262,144 x 768 table), and the CPU arena keeps
that high-water mark. With a per-tensor scale and zero point, dequantizing
is elementwise, so it commutes with the row lookup:
``DequantizeLinear(Gather(table_q, ids), scale, zero_point)`` produces the
same floats, bit for bit, while touching only the looked-up rows.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

# Field numbers from onnx.proto.
_MODEL_GRAPH = 7
_GRAPH_NODE = 1
_GRAPH_INITIALIZER = 5
_GRAPH_OUTPUT = 12
_GRAPH_VALUE_INFO = 13
_NODE_INPUT = 1
_NODE_OUTPUT = 2
_NODE_OP_TYPE = 4
_NODE_ATTRIBUTE = 5
_NODE_DOMAIN = 7
_ATTRIBUTE_NAME = 1
_TENSOR_DIMS = 1
_TENSOR_NAME = 8
_TENSOR_EXTERNAL_DATA = 13
_ENTRY_KEY = 1
_ENTRY_VALUE = 2
_VALUE_INFO_NAME = 1

_VARINT = 0
_LEN = 2


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if byte < 0x80:
            return result, pos
        shift += 7


def _encode_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _fields(buf: bytes):
    """Yield ``(field_number, wire_type, value, raw)`` for each field of a message.

    ``value`` is the int for a varint and the payload bytes for a
    length-delimited field. ``raw`` is the field's exact encoding, key
    included, for copying it through unchanged.
    """
    pos = 0
    while pos < len(buf):
        start = pos
        key, pos = _read_varint(buf, pos)
        number, wire_type = key >> 3, key & 7
        if wire_type == _VARINT:
            value, pos = _read_varint(buf, pos)
        elif wire_type == _LEN:
            length, pos = _read_varint(buf, pos)
            value = buf[pos : pos + length]
            pos += length
        elif wire_type == 1:  # 64-bit
            value = buf[pos : pos + 8]
            pos += 8
        elif wire_type == 5:  # 32-bit
            value = buf[pos : pos + 4]
            pos += 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire_type}")
        if pos > len(buf):
            raise ValueError("truncated protobuf message")
        yield number, wire_type, value, buf[start:pos]


def _len_field(number: int, payload: bytes) -> bytes:
    return _encode_varint(number << 3 | _LEN) + _encode_varint(len(payload)) + payload


def _string_field(number: int, text: str) -> bytes:
    return _len_field(number, text.encode("utf-8"))


@dataclass
class _Node:
    raw: bytes
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    op_type: str = ""
    domain: str = ""
    attributes: list[str] = field(default_factory=list)
    # Every field other than inputs and outputs, in order, for re-encoding.
    rest: list[bytes] = field(default_factory=list)

    @classmethod
    def parse(cls, raw: bytes, payload: bytes) -> "_Node":
        node = cls(raw=raw)
        for number, _wt, value, field_raw in _fields(payload):
            if number == _NODE_INPUT:
                node.inputs.append(value.decode("utf-8"))
                continue
            if number == _NODE_OUTPUT:
                node.outputs.append(value.decode("utf-8"))
                continue
            if number == _NODE_OP_TYPE:
                node.op_type = value.decode("utf-8")
            elif number == _NODE_DOMAIN:
                node.domain = value.decode("utf-8")
            elif number == _NODE_ATTRIBUTE:
                for anum, _awt, avalue, _araw in _fields(value):
                    if anum == _ATTRIBUTE_NAME:
                        node.attributes.append(avalue.decode("utf-8"))
            node.rest.append(field_raw)
        return node

    def encode(self, inputs: list[str], outputs: list[str]) -> bytes:
        payload = b"".join(
            [_string_field(_NODE_INPUT, name) for name in inputs]
            + [_string_field(_NODE_OUTPUT, name) for name in outputs]
            + self.rest
        )
        return _len_field(_GRAPH_NODE, payload)

    @property
    def is_default_domain(self) -> bool:
        return self.domain in ("", "ai.onnx")


def _tensor_name_and_dims(payload: bytes) -> tuple[str, list[int]]:
    name, dims = "", []
    for number, wire_type, value, _raw in _fields(payload):
        if number == _TENSOR_NAME:
            name = value.decode("utf-8")
        elif number == _TENSOR_DIMS:
            if wire_type == _VARINT:
                dims.append(value)
            else:  # packed
                pos = 0
                while pos < len(value):
                    dim, pos = _read_varint(value, pos)
                    dims.append(dim)
    return name, dims


def _external_location(payload: bytes) -> Optional[str]:
    for number, _wt, value, _raw in _fields(payload):
        if number != _TENSOR_EXTERNAL_DATA:
            continue
        entry = {n: v.decode("utf-8") for n, _w, v, _r in _fields(value)}
        if entry.get(_ENTRY_KEY) == "location":
            return entry.get(_ENTRY_VALUE)
    return None


def _relocate_external_data(payload: bytes, location: str) -> bytes:
    """Return a TensorProto payload whose external ``location`` is ``location``."""
    out = []
    for number, _wt, value, raw in _fields(payload):
        if number == _TENSOR_EXTERNAL_DATA:
            entry = {n: v.decode("utf-8") for n, _w, v, _r in _fields(value)}
            if entry.get(_ENTRY_KEY) == "location":
                raw = _len_field(
                    _TENSOR_EXTERNAL_DATA,
                    _string_field(_ENTRY_KEY, "location") + _string_field(_ENTRY_VALUE, location),
                )
        out.append(raw)
    return b"".join(out)


def _is_scalar(dims: Optional[list[int]]) -> bool:
    return dims is not None and (dims == [] or dims == [1])


@dataclass
class GatherPatch:
    """A rewritten model, ready for ``ort.InferenceSession(model_bytes, ...)``.

    ``external_data_dir`` is the real directory holding the model's external
    data, for the ``session.model_external_initializers_file_folder_path``
    session option; ``None`` when the model has no external data.
    """

    model_bytes: bytes
    external_data_dir: Optional[str]
    rewritten: int


@dataclass
class _Graph:
    """The parts of a GraphProto the rewrite reads."""

    nodes: list[_Node] = field(default_factory=list)
    initializer_dims: dict[str, list[int]] = field(default_factory=dict)
    outputs: set[str] = field(default_factory=set)
    # initializer name -> external data location, for external initializers
    external: dict[str, str] = field(default_factory=dict)

    @classmethod
    def parse(cls, payload: bytes) -> "_Graph":
        graph = cls()
        for number, wire_type, value, raw in _fields(payload):
            if wire_type != _LEN:
                continue
            if number == _GRAPH_NODE:
                graph.nodes.append(_Node.parse(raw, value))
            elif number == _GRAPH_INITIALIZER:
                name, dims = _tensor_name_and_dims(value)
                graph.initializer_dims[name] = dims
                location = _external_location(value)
                if location is not None:
                    graph.external[name] = location
            elif number == _GRAPH_OUTPUT:
                graph.outputs.add(_value_info_name(value))
        return graph


def _value_info_name(payload: bytes) -> Optional[str]:
    return next(
        (v.decode("utf-8") for n, _w, v, _r in _fields(payload) if n == _VALUE_INFO_NAME),
        None,
    )


def _is_per_tensor_dequantize(node: _Node, graph: _Graph) -> bool:
    if node.op_type != "DequantizeLinear" or not node.is_default_domain:
        return False
    if len(node.outputs) != 1 or not 2 <= len(node.inputs) <= 3:
        return False
    if "block_size" in node.attributes or node.inputs[0] not in graph.initializer_dims:
        return False
    params = [name for name in node.inputs[1:] if name]
    return all(_is_scalar(graph.initializer_dims.get(name)) for name in params)


def _reads_as_data(gather: _Node, tensor: str) -> bool:
    return (
        gather.op_type == "Gather"
        and gather.is_default_domain
        and len(gather.inputs) == 2
        and len(gather.outputs) == 1
        and gather.inputs[0] == tensor
        and gather.inputs[1] != tensor
    )


def _eligible_pairs(graph: _Graph) -> dict[int, int]:
    """Map each eligible DequantizeLinear's node index to its Gather's."""
    consumers: dict[str, list[int]] = {}
    for index, node in enumerate(graph.nodes):
        for name in node.inputs:
            consumers.setdefault(name, []).append(index)
    pairs: dict[int, int] = {}
    for index, node in enumerate(graph.nodes):
        if not _is_per_tensor_dequantize(node, graph):
            continue
        dequantized = node.outputs[0]
        users = consumers.get(dequantized, [])
        if dequantized in graph.outputs or len(users) != 1:
            continue
        if _reads_as_data(graph.nodes[users[0]], dequantized):
            pairs[index] = users[0]
    return pairs


def _real_external_locations(model_path: str, graph: _Graph):
    """``(location -> real file name, real directory)`` for the external data.

    ``None`` when the files resolve into more than one real directory: a
    session loaded from memory can be pointed at only one folder.
    """
    model_dir = os.path.dirname(os.path.abspath(model_path))
    names: dict[str, str] = {}
    real_dir = None
    for location in set(graph.external.values()):
        real = os.path.realpath(os.path.join(model_dir, location))
        if real_dir is None:
            real_dir = os.path.dirname(real)
        elif os.path.dirname(real) != real_dir:
            return None
        names[location] = os.path.basename(real)
    return names, real_dir


def _swapped_nodes(graph: _Graph, pairs: dict[int, int]) -> tuple[dict[int, bytes], set[str]]:
    """Encoded replacement nodes by index, and the dequantized tensors that go away."""
    taken = {name for node in graph.nodes for name in node.outputs}
    replacement: dict[int, bytes] = {}
    removed: set[str] = set()
    for dq_index, gather_index in pairs.items():
        dq, gather = graph.nodes[dq_index], graph.nodes[gather_index]
        rows = gather.outputs[0] + "/quantized_rows"
        while rows in taken:
            rows += "_"
        taken.add(rows)
        # The Gather moves to the DequantizeLinear's slot and the
        # DequantizeLinear to the Gather's, which keeps the node list in
        # topological order: the new Gather reads only an initializer and
        # the ids, and everything that read the Gather's output still comes
        # after the DequantizeLinear that now produces it.
        replacement[dq_index] = gather.encode([dq.inputs[0], gather.inputs[1]], [rows])
        replacement[gather_index] = dq.encode([rows, *dq.inputs[1:]], [gather.outputs[0]])
        removed.add(dq.outputs[0])
    return replacement, removed


def _rewrite_graph(payload, replacement, removed, real_names) -> bytes:
    out = []
    node_index = 0
    for number, wire_type, value, raw in _fields(payload):
        if wire_type != _LEN:
            out.append(raw)
            continue
        if number == _GRAPH_NODE:
            raw = replacement.get(node_index, raw)
            node_index += 1
        elif number == _GRAPH_VALUE_INFO and _value_info_name(value) in removed:
            # Describes the full dequantized table, which no longer exists.
            continue
        elif number == _GRAPH_INITIALIZER:
            location = _external_location(value)
            if location is not None and real_names[location] != location:
                raw = _len_field(
                    _GRAPH_INITIALIZER, _relocate_external_data(value, real_names[location])
                )
        out.append(raw)
    return b"".join(out)


def gather_before_dequantize(model_path: str) -> Optional[GatherPatch]:
    """Swap every eligible ``Gather(DequantizeLinear(...))`` into ``DequantizeLinear(Gather(...))``.

    A pair is eligible when the dequantized tensor is an initializer with a
    per-tensor (scalar) scale and zero point, no block quantization, and
    the Gather that reads it as its data input is its only consumer.
    Returns ``None`` when no pair is eligible, so the caller loads the
    model from its path as before, or when the external data cannot be
    pointed at from memory (files spread over several real directories).

    The model is returned as bytes because it is loaded from memory: its
    external-data locations are rewritten to the files' real names in
    their real directory, which also keeps a symlinked cache (huggingface_hub
    links snapshot files to content-addressed blobs) inside the directory
    ONNX Runtime validates external data against.
    """
    with open(model_path, "rb") as fh:
        model = fh.read()

    graph_payload = None
    for number, wire_type, value, _raw in _fields(model):
        if number == _MODEL_GRAPH and wire_type == _LEN:
            graph_payload = value
    if graph_payload is None:
        return None

    graph = _Graph.parse(graph_payload)
    pairs = _eligible_pairs(graph)
    if not pairs:
        return None
    located = _real_external_locations(model_path, graph)
    if located is None:
        return None
    real_names, external_dir = located

    replacement, removed = _swapped_nodes(graph, pairs)
    new_graph = _rewrite_graph(graph_payload, replacement, removed, real_names)
    model_out = []
    for number, wire_type, _value, raw in _fields(model):
        if number == _MODEL_GRAPH and wire_type == _LEN:
            raw = _len_field(_MODEL_GRAPH, new_graph)
        model_out.append(raw)
    return GatherPatch(b"".join(model_out), external_dir, len(pairs))
