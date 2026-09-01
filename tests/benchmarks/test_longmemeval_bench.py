import importlib.util
import json
import sys
import types
from pathlib import Path


def _load_longmemeval_bench(monkeypatch):
    module_name = "tests._longmemeval_bench_under_test"
    module_path = Path(__file__).resolve().parents[2] / "benchmarks" / "longmemeval_bench.py"

    class _DummyEphemeralClient:
        def delete_collection(self, name):
            return None

        def create_collection(self, name, embedding_function=None):
            return object()

    fake_chromadb = types.ModuleType("chromadb")
    fake_chromadb.EphemeralClient = lambda: _DummyEphemeralClient()
    monkeypatch.setitem(sys.modules, "chromadb", fake_chromadb)

    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _force_cp1252_default(monkeypatch, target_path):
    original_open = open
    target_path = Path(target_path)

    def fake_open(file, mode="r", *args, **kwargs):
        if Path(file) == target_path and "b" not in mode and "encoding" not in kwargs:
            kwargs["encoding"] = "cp1252"
        return original_open(file, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", fake_open)


def test_load_or_create_split_reads_utf8_when_windows_default_is_cp1252(tmp_path, monkeypatch):
    bench = _load_longmemeval_bench(monkeypatch)
    split_path = tmp_path / "lme_split.json"
    expected = {"dev": ["frage-ä"], "held_out": ["sess-😀"], "seed": 42, "dev_size": 1}
    split_path.write_text(json.dumps(expected, ensure_ascii=False), encoding="utf-8")

    _force_cp1252_default(monkeypatch, split_path)

    split = bench._load_or_create_split(str(split_path), data=[])

    assert split == expected


def test_run_benchmark_reads_utf8_questions_when_windows_default_is_cp1252(
    tmp_path, monkeypatch
):
    bench = _load_longmemeval_bench(monkeypatch)
    question = "Wann hat Zoë das Café 😀 erwähnt?"
    answer = "Beim Frühstück im Café 😀."
    data = [
        {
            "question_id": "q1",
            "question_type": "single-session-user",
            "question": question,
            "answer": answer,
            "answer_session_ids": ["sess_1"],
            "haystack_sessions": [[{"role": "user", "content": "stub memory"}]],
            "haystack_session_ids": ["sess_1"],
            "haystack_dates": ["2024-01-01"],
        }
    ]
    data_path = tmp_path / "longmemeval.json"
    data_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    captured = {}

    def fake_build(entry, granularity="session", n_results=50):
        captured["question"] = entry["question"]
        captured["answer"] = entry["answer"]
        return [0], ["stub memory"], ["sess_1"], ["2024-01-01"]

    monkeypatch.setattr(bench, "build_palace_and_retrieve", fake_build)
    _force_cp1252_default(monkeypatch, data_path)

    bench.run_benchmark(str(data_path), limit=1, out_file=None)

    assert captured["question"] == question
    assert captured["answer"] == answer
