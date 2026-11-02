"""Local sentence embeddings only. Missing model files never trigger a download."""

import math
import threading
from functools import cached_property, lru_cache

from commonplace.extensions import ToolError

_lock = threading.Lock()
MODEL = "all-MiniLM-L6-v2"


@lru_cache(maxsize=1)
def local_model():
    from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2

    class OfflineMiniLM(ONNXMiniLM_L6_V2):
        def _download_model_if_not_exists(self):
            directory = self.DOWNLOAD_PATH / self.EXTRACTED_FOLDER_NAME
            if not all((directory / name).is_file() for name in ("model.onnx", "tokenizer.json")):
                raise ToolError("no: local skill selection model is not installed")

        @cached_property
        def model(self):
            options = self.ort.SessionOptions()
            options.intra_op_num_threads = 2
            options.inter_op_num_threads = 1
            options.log_severity_level = 3
            return self.ort.InferenceSession(
                str(self.DOWNLOAD_PATH / self.EXTRACTED_FOLDER_NAME / "model.onnx"),
                providers=["CPUExecutionProvider"],
                sess_options=options,
            )

    return OfflineMiniLM(preferred_providers=["CPUExecutionProvider"])


def encode(texts):
    try:
        with _lock:
            return local_model()(input=texts)
    except ToolError:
        raise
    except Exception:
        raise ToolError("no: local skill selection model is unavailable") from None


def rank(task, records, limit):
    if len(records) > 200:
        raise ToolError("no: local skill selection supports at most 200 skills")
    if not records:
        return []
    # MiniLM truncates to its token window. Selection is a relevance aid, not a recall guarantee.
    texts = [task] + [(row["name"] + "\n" + row["markdown"])[:8000] for row in records]
    vectors = encode(texts)

    def cosine(left, right):
        norm = math.sqrt(sum(float(x) ** 2 for x in left) * sum(float(x) ** 2 for x in right))
        return sum(float(x) * float(y) for x, y in zip(left, right)) / norm if norm else 0.0

    found = [
        {**row, "score": round(cosine(vectors[0], vector), 6)}
        for row, vector in zip(records, vectors[1:])
    ]
    return sorted(found, key=lambda row: (-row["score"], row["skill_id"]))[:limit]
