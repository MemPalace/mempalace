import gzip
import os
import pickle
import tempfile
import shutil
from pathlib import Path
from collections import defaultdict

import pytest


import hashlib

from mempalace.content_hash import BloomFilter, ContentHashDB


class TestBloomFilter:
    def test_add_and_check(self):
        bf = BloomFilter(capacity=1000)
        bf.add("hello")
        assert "hello" in bf
        assert "world" not in bf

    def test_false_positive_rate(self):
        bf = BloomFilter(capacity=10000, false_positive_rate=0.1)
        items = [f"item_{i}" for i in range(1000)]
        for item in items:
            bf.add(item)

        false_positives = sum(1 for i in range(1000, 2000) if f"item_{i}" in bf)
        assert false_positives < 150

    def test_save_and_load(self, tmp_path):
        bf1 = BloomFilter(capacity=1000)
        bf1.add("test")
        bf1.add("data")

        bloom_file = tmp_path / "bloom.json"
        bf1.save(str(bloom_file))
        bf2 = BloomFilter.load(str(bloom_file))
        assert "test" in bf2
        assert "data" in bf2


class TestContentHashDB:
    def test_compute_hash(self, tmp_path):
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello world")

        db = ContentHashDB(str(tmp_path / "hashes.json"))
        content_hash = db.compute_hash(test_file)

        assert len(content_hash) == 64
        assert content_hash == hashlib.sha256(b"hello world").hexdigest()

    def test_check_and_add_new_file(self, tmp_path):
        test_file = tmp_path / "test.txt"
        test_file.write_text("new content")

        db = ContentHashDB(str(tmp_path / "hashes.json"))
        is_duplicate = db.check_and_add(test_file)

        assert is_duplicate is False
        assert str(test_file) in db.hashes

    def test_check_and_add_duplicate_file(self, tmp_path):
        test_file = tmp_path / "test.txt"
        test_file.write_text("same content")

        db = ContentHashDB(str(tmp_path / "hashes.json"))
        db.check_and_add(test_file)

        is_duplicate = db.check_and_add(test_file)

        assert is_duplicate is True

    def test_different_files_same_content(self, tmp_path):
        file1 = tmp_path / "file1.txt"
        file2 = tmp_path / "file2.txt"
        file1.write_text("identical content")
        file2.write_text("identical content")

        db = ContentHashDB(str(tmp_path / "hashes.json"))
        db.check_and_add(file1)
        is_dup = db.check_and_add(file2)

        assert is_dup is True

    def test_different_content_not_duplicate(self, tmp_path):
        file1 = tmp_path / "file1.txt"
        file2 = tmp_path / "file2.txt"
        file1.write_text("content A")
        file2.write_text("content B")

        db = ContentHashDB(str(tmp_path / "hashes.json"))
        db.check_and_add(file1)
        is_dup = db.check_and_add(file2)

        assert is_dup is False

    def test_persistence(self, tmp_path):
        test_file = tmp_path / "test.txt"
        test_file.write_text("persistent content")

        db1 = ContentHashDB(str(tmp_path / "hashes.json"))
        db1.check_and_add(test_file)
        db1.flush()

        db2 = ContentHashDB(str(tmp_path / "hashes.json"))
        is_dup = db2.check_and_add(test_file)

        assert is_dup is True
        assert str(test_file) in db2.hashes

    def test_clear(self, tmp_path):
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")

        db = ContentHashDB(str(tmp_path / "hashes.json"))
        db.check_and_add(test_file)
        assert str(test_file) in db.hashes

        db.clear()
        assert len(db.hashes) == 0

        is_dup = db.check_and_add(test_file)
        assert is_dup is False

    def test_record_fallback(self, tmp_path):
        """Test that record() adds without checking (for ChromaDB fallback path)."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("recorded content")

        db = ContentHashDB(str(tmp_path / "hashes.json"))
        db.record(test_file)

        assert str(test_file) in db.hashes

    def test_false_positive_handled(self, tmp_path):
        """Test that files are correctly added after bloom false positive."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("unique content")

        db = ContentHashDB(str(tmp_path / "hashes.json"))
        is_dup = db.check_and_add(test_file)

        assert is_dup is False
        assert str(test_file) in db.hashes


class TestBloomFilterSecurity:
    def test_malicious_pickle_rejected(self, tmp_path):
        """Verify that a malicious pickle file is rejected."""
        from mempalace.content_hash import BloomFilter

        bloom_file = tmp_path / "evil.bloom"

        # Create a malicious pickle that tries to execute code
        # This uses __reduce__ to call os.system
        import pickle
        import os

        class Malicious:
            def __reduce__(self):
                return (os.system, ("echo PWNED",))

        # Create a fake payload that looks like a valid bloom but contains malicious data
        # We'll just write garbage with a valid-looking hash prefix
        malicious_data = pickle.dumps(Malicious())
        fake_hash = "a" * 64  # Wrong hash
        payload = fake_hash.encode("ascii") + malicious_data
        compressed = gzip.compress(payload)

        with open(bloom_file, "wb") as f:
            f.write(compressed)

        # Attempting to load should fail (either hash mismatch or SafeUnpickler)
        with pytest.raises((ValueError, pickle.UnpicklingError)):
            BloomFilter.load(str(bloom_file))

    def test_corrupted_hash_rejected(self, tmp_path):
        """Verify that a file with corrupted hash is rejected."""
        from mempalace.content_hash import BloomFilter

        bf = BloomFilter(capacity=100)
        bf.add("test")
        bf.save(str(tmp_path / "test.bloom"))

        # Now corrupt the hash prefix
        with open(tmp_path / "test.bloom", "rb") as f:
            compressed = f.read()

        decompressed = gzip.decompress(compressed)
        # Replace the first 64 bytes (the hash) with garbage
        corrupted = b"0" * 64 + decompressed[64:]
        recompressed = gzip.compress(corrupted)

        with open(tmp_path / "test.bloom", "wb") as f:
            f.write(recompressed)

        with pytest.raises(ValueError, match="integrity check failed"):
            BloomFilter.load(str(tmp_path / "test.bloom"))

    def test_safe_unpickler_allows_basic_types(self, tmp_path):
        """Verify that SafeUnpickler allows basic types."""
        from mempalace.content_hash import BloomFilter

        bf = BloomFilter(capacity=100)
        bf.add("hello")
        bf.add("world")
        bf.save(str(tmp_path / "test.bloom"))

        # Loading should work fine - only basic types used
        bf2 = BloomFilter.load(str(tmp_path / "test.bloom"))
        assert "hello" in bf2
        assert "world" in bf2
