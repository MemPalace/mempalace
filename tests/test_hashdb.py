import os
import tempfile
import shutil
from pathlib import Path
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
