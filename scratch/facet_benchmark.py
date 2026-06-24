import random
import time
import uuid

from mempalace.backends.qdrant import QdrantBackend
from mempalace.backends.base import PalaceRef

N = 100_000

backend = QdrantBackend()
# Using a shared local path so data persists if you switch branches
palace = PalaceRef(id="benchmark", local_path="/tmp/benchmark")

collection = backend.get_collection(
    palace=palace,
    collection_name="benchmark",
    create=True,
)


# ---------------------------------------------------------------------
# 1. Setup Keyword Indexes (Required for Qdrant Faceting)
# ---------------------------------------------------------------------
# We explicitly create payload indexes for BOTH naming conventions
# to ensure the server-side engine works perfectly regardless of the branch logic.
def ensure_indexes():
    for field in ["wing", "metadata.wing", "room", "metadata.room"]:
        try:
            collection._client.request(
                "PUT",
                f"/collections/{collection._remote_collection}/index",
                body={"field_name": field, "field_schema": "keyword"},
            )
        except Exception:
            pass


ensure_indexes()

# ---------------------------------------------------------------------
# 2. Populate Data Once
# ---------------------------------------------------------------------
if collection.count() == 0:
    print(f"Inserting {N:,} points...")

    ids = [str(uuid.uuid4()) for _ in range(N)]
    docs = ["doc"] * N
    embeddings = [[1.0, 0.0]] * N

    wings = ["engineering", "research", "personal", "knowledge", "archive"]

    # We provide both nested and flat metadata patterns so that
    # no matter how your branch extracts the "wing" key, it finds it.
    metadata = [
        {
            "wing": choice,
            "room": f"room-{random.randint(1, 25)}",
            "metadata": {
                "wing": choice,
                "room": f"room-{random.randint(1, 25)}",
            },
        }
        for _ in range(N)
        for choice in [random.choice(wings)]
    ]

    collection.upsert(
        ids=ids,
        documents=docs,
        metadatas=metadata,
        embeddings=embeddings,
    )
    print("Finished inserting.\n")
else:
    print(f"Using existing {collection.count():,} points.\n")


# ---------------------------------------------------------------------
# 3. Dynamic Baseline Benchmark (Works on both branches)
# ---------------------------------------------------------------------
print("--- BENCHMARK START ---")
start = time.perf_counter()

counts = {}

# Dynamic inspection: Uses main branch generator if available, falls back to direct get
if hasattr(collection, "get_all_metadata"):
    metadata_iterator = collection.get_all_metadata()
elif hasattr(collection, "get"):
    metadata_iterator = collection.get(include=["metadatas"]).metadatas
else:
    metadata_iterator = []

for meta in metadata_iterator:
    if not meta:
        continue
    # Safe lookup: handles both flat metadata and nested metadata architectures
    wing = meta.get("wing") or meta.get("metadata", {}).get("wing", "unknown")
    counts[wing] = counts.get(wing, 0) + 1

elapsed_baseline = time.perf_counter() - start
print(f"Client-side counting (Baseline) : {elapsed_baseline:.3f} sec")


# ---------------------------------------------------------------------
# 4. Dynamic Optimized Benchmark (Gracefully handles branch absence)
# ---------------------------------------------------------------------
if hasattr(collection, "facet_counts"):
    start = time.perf_counter()

    # Tries both namespace variations dynamically based on what your branch expects
    try:
        facet_results = collection.facet_counts("wing")
    except Exception:
        facet_results = collection.facet_counts("metadata.wing")

    elapsed_facet = time.perf_counter() - start
    print(f"Server-side facets (Optimized)  : {elapsed_facet:.3f} sec")

    # Calculate performance gains automatically
    speedup = elapsed_baseline / max(elapsed_facet, 1e-9)
    print(f"\n🚀 Speedup Factor: {speedup:.2f}x faster")
else:
    print("Server-side facets (Optimized)  : Not available on this branch (Main Baseline)")
