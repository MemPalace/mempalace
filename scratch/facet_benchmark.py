import time
import uuid
import numpy as np

from mempalace.backends.qdrant import QdrantBackend
from mempalace.backends.base import PalaceRef


# -----------------------------
# CONFIG
# -----------------------------
NUM_POINTS = 100_000
DIM = 2


def make_collection():
    backend = QdrantBackend()
    palace = PalaceRef(id="benchmark_palace", local_path="/tmp")
    col = backend.get_collection(
        palace=palace,
        collection_name="facet_benchmark",
        create=True,
    )
    return backend, col


def create_payload_indexes(col):
    print("Creating payload indexes...")

    def create(field):
        col._client.request(
            "PUT",
            f"/collections/{col._remote_collection}/index",
            body={
                "field_name": field,
                "field_schema": "keyword",
            },
        )

    try:
        create("metadata.wing")
        create("metadata.room")
        print("Indexes created.")
    except Exception as e:
        print("Index creation skipped (may already exist):", e)


def insert_data(col):
    print("Inserting points...")

    wings = ["alpha", "beta", "gamma", "delta"]
    rooms = ["r1", "r2", "r3"]

    ids = []
    docs = []
    metas = []
    vectors = []

    for i in range(NUM_POINTS):
        ids.append(str(uuid.uuid4()))
        docs.append(f"doc-{i}")
        metas.append(
            {
                "wing": wings[i % len(wings)],
                "room": rooms[i % len(rooms)],
            }
        )
        vectors.append(np.random.rand(DIM).tolist())

    col.upsert(
        ids=ids,
        documents=docs,
        metadatas=metas,
        embeddings=vectors,
    )

    print("Inserted.")


def client_side_count(col):
    """
    O(n) baseline
    """
    t0 = time.time()

    all_meta = col.get(include=["metadatas"])
    counter = {}

    for m in all_meta.metadatas:
        if not m:
            continue
        w = m.get("wing")
        if w:
            counter[w] = counter.get(w, 0) + 1

    t1 = time.time()
    return counter, t1 - t0


def facet_count(col):
    """
    O(1-ish server aggregation via Qdrant facet API
    """
    t0 = time.time()

    wings = col.facet_counts("metadata.wing")

    t1 = time.time()
    return wings, t1 - t0


def main():
    backend, col = make_collection()

    insert_data(col)

    print("\n--- BENCHMARK START ---")

    client_counts, t_client = client_side_count(col)
    print(f"Client-side counting: {t_client:.3f}s")

    try:
        facet_counts, t_facet = facet_count(col)
        print(f"Facet counting     : {t_facet:.3f}s")

        print("\nSpeedup:", round(t_client / max(t_facet, 1e-9), 2), "x")

    except Exception as e:
        print("\nFacet failed (this is expected on develop branch):")
        print(e)


if __name__ == "__main__":
    main()
