
import pytest
import os
import shutil
from mempalace.searcher import search_memories, SearchError
from mempalace.palace import get_collection
from mempalace.backends.chroma import ChromaBackend

def test_none_metadata_resilience(tmp_path):
    # 1. Setup a fresh temporary palace
    palace_path = str(tmp_path / "palace")
    os.makedirs(palace_path, exist_ok=True)
    
    # Create a collection manually to ensure we can control it
    client = ChromaBackend.make_client(palace_path)
    col = client.get_or_create_collection(
        "mempalace_drawers", 
        metadata={"hnsw:space": "cosine", "hnsw:num_threads": 1}
    )
    
    # Add one item with valid metadata
    col.add(
        ids=["test_1"],
        documents=["This is a test document"],
        metadatas=[{"wing": "test_wing", "room": "test_room"}]
    )
    
    # 2. Simulate the bug: bypass the client and delete metadata directly via SQLite
    db_path = os.path.join(palace_path, "chroma.sqlite3")
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM embedding_metadata WHERE id = (SELECT id FROM embeddings LIMIT 1)")
    conn.commit()
    conn.close()
    
    # 3. Run search and verify no AttributeError occurs
    # search_memories should return a result dict, not crash
    try:
        result = search_memories("test", palace_path)
        assert "results" in result
        # Verify the result has a fallback metadata dict instead of None
        for hit in result["results"]:
            assert isinstance(hit["metadata"], dict)
    except AttributeError as e:
        pytest.fail(f"AttributeError detected: {e}")
    except Exception as e:
        pytest.fail(f"Unexpected error: {e}")

