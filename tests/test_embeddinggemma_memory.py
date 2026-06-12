import numpy as np

from mempalace.embedding import (
    EmbeddinggemmaONNX,
    _EMBEDDINGGEMMA_DIM,
    _cpu_mem_arena_enabled,
)


class _Encoding:
    ids = [1, 2, 3]
    attention_mask = [1, 1, 1]


class _Tokenizer:
    def encode_batch(self, texts):
        return [_Encoding() for _ in texts]


class _Session:
    def __init__(self):
        self.batch_sizes = []

    def run(self, _names, feed):
        batch_size = int(feed["input_ids"].shape[0])
        self.batch_sizes.append(batch_size)
        return [np.ones((batch_size, _EMBEDDINGGEMMA_DIM + 8), dtype=np.float32)]


def test_embeddinggemma_batches_internal_session_runs(monkeypatch):
    ef = EmbeddinggemmaONNX()
    session = _Session()

    ef._session = session
    ef._tokenizer = _Tokenizer()
    ef._np = np
    ef._output_idx = 0

    monkeypatch.setenv("MEMPALACE_EMBEDDINGGEMMA_BATCH_SIZE", "2")

    vectors = ef(["a", "b", "c", "d", "e"])

    assert len(vectors) == 5
    assert session.batch_sizes == [2, 2, 1]
    assert all(len(vector) == _EMBEDDINGGEMMA_DIM for vector in vectors)


def test_cpu_only_embeddinggemma_disables_onnx_cpu_arena_by_default(monkeypatch):
    monkeypatch.delenv("MEMPALACE_ONNX_CPU_MEM_ARENA", raising=False)

    assert _cpu_mem_arena_enabled(["CPUExecutionProvider"]) is False
    assert _cpu_mem_arena_enabled(["CUDAExecutionProvider", "CPUExecutionProvider"]) is True


def test_cpu_arena_env_override(monkeypatch):
    monkeypatch.setenv("MEMPALACE_ONNX_CPU_MEM_ARENA", "1")

    assert _cpu_mem_arena_enabled(["CPUExecutionProvider"]) is True


def test_embeddinggemma_single_string_is_one_input(monkeypatch):
    ef = EmbeddinggemmaONNX()
    session = _Session()

    ef._session = session
    ef._tokenizer = _Tokenizer()
    ef._np = np
    ef._output_idx = 0

    monkeypatch.setenv("MEMPALACE_EMBEDDINGGEMMA_BATCH_SIZE", "32")

    vectors = ef("hello")

    assert len(vectors) == 1
    assert session.batch_sizes == [1]


def test_env_bool_parses_empty_and_garbage_as_safe_false(monkeypatch):
    from mempalace.embedding import _env_bool

    monkeypatch.setenv("MEMPALACE_ONNX_CPU_MEM_ARENA", "")
    assert _env_bool("MEMPALACE_ONNX_CPU_MEM_ARENA", default=True) is False

    monkeypatch.setenv("MEMPALACE_ONNX_CPU_MEM_ARENA", "definitely")
    assert _env_bool("MEMPALACE_ONNX_CPU_MEM_ARENA", default=True) is False

    monkeypatch.setenv("MEMPALACE_ONNX_CPU_MEM_ARENA", "yes")
    assert _env_bool("MEMPALACE_ONNX_CPU_MEM_ARENA", default=False) is True


def test_env_int_invalid_value_returns_safe_zero(monkeypatch):
    from mempalace.embedding import _env_int

    monkeypatch.setenv("MEMPALACE_EMBEDDINGGEMMA_BATCH_SIZE", "banana")

    assert _env_int("MEMPALACE_EMBEDDINGGEMMA_BATCH_SIZE", default=32) == 0
