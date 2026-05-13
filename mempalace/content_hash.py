import hashlib
import gzip
import io
import math
import os
import pickle
import sqlite3
from pathlib import Path


# Allowed modules/classes for safe unpickling
_SAFE_CLASSES = {
    ("builtins", "list"),
    ("builtins", "dict"),
    ("builtins", "set"),
    ("builtins", "int"),
    ("builtins", "float"),
    ("builtins", "bool"),
    ("builtins", "str"),
    ("builtins", "tuple"),
    ("builtins", "bytearray"),
}


class SafeUnpickler(pickle.Unpickler):
    """Restricted unpickler that only allows safe built-in types."""

    def find_class(self, module, name):
        if (module, name) in _SAFE_CLASSES:
            return super().find_class(module, name)
        raise pickle.UnpicklingError(
            f"Potentially unsafe pickle: {module}.{name} is not allowed"
        )


def _compute_blob_hash(data: bytes) -> str:
    """Compute SHA256 hash of data for integrity verification."""
    return hashlib.sha256(data).hexdigest()


class BloomFilter:
    """Simple bloom filter for fast duplicate checking."""

    def __init__(self, capacity: int = 100000, false_positive_rate: float = 0.01):
        self.size = self._optimal_size(capacity, false_positive_rate)
        self.hash_count = self._optimal_hash_count(capacity, self.size)
        self.array = [False] * self.size

    def _optimal_size(self, n: int, p: float) -> int:
        return int(-n * math.log(p) / (math.log(2) ** 2))

    def _optimal_hash_count(self, n: int, m: int) -> int:
        return max(1, int((m / n) * math.log(2)))

    def _hashes(self, item: str) -> list:
        result = []
        for i in range(self.hash_count):
            h = hashlib.md5((item + str(i)).encode()).hexdigest()
            result.append(int(h, 16) % self.size)
        return result

    def add(self, item: str):
        for idx in self._hashes(item):
            self.array[idx] = True

    def __contains__(self, item: str) -> bool:
        return all(self.array[idx] for idx in self._hashes(item))

    def save(self, path: str):
        """Save bloom filter as compressed, hashed pickle."""
        data = {
            "array_size": self.size,
            "hash_count": self.hash_count,
            "array": self.array,
        }
        # Pickle the data
        pickled = pickle.dumps(data, protocol=pickle.HIGHEST_PROTOCOL)
        # Compute integrity hash
        integrity_hash = _compute_blob_hash(pickled)
        # Prepend the hash (64 hex chars) to the pickled data
        payload = integrity_hash.encode("ascii") + pickled
        # Compress with gzip
        compressed = gzip.compress(payload, compresslevel=6)
        with open(path, "wb") as f:
            f.write(compressed)

    @classmethod
    def load(cls, path: str) -> "BloomFilter":
        """Load bloom filter from compressed, hashed pickle. Safe against code execution."""
        if not os.path.exists(path):
            return cls()
        try:
            with open(path, "rb") as f:
                compressed = f.read()
            # Decompress
            payload = gzip.decompress(compressed)
            # Extract integrity hash (first 64 bytes = hex SHA256)
            stored_hash = payload[:64].decode("ascii")
            pickled = payload[64:]
            # Verify integrity hash
            computed_hash = _compute_blob_hash(pickled)
            if not stored_hash == computed_hash:
                raise ValueError(
                    f"Bloom filter integrity check failed: stored={stored_hash[:8]}... "
                    f"computed={computed_hash[:8]}..."
                )
            # Safe unpickle — only allows basic built-in types
            data = SafeUnpickler(io.BytesIO(pickled)).load()
            bf = cls.__new__(cls)
            bf.size = data["array_size"]
            bf.hash_count = data["hash_count"]
            bf.array = data["array"]
            return bf
        except (pickle.UnpicklingError, ValueError, KeyError, EOFError, gzip.BadGzipFile) as e:
            raise ValueError(f"Failed to load bloom filter from {path}: {e}")


class ContentHashDB:
    """Persistent hash database for file content using SQLite."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.bloom_path = db_path + ".bloom"
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._hash_set = set()
        self.bloom = BloomFilter()
        self._initialize()
        self._load()

    def _initialize(self):
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS content_hashes (
                filepath TEXT PRIMARY KEY,
                content_hash TEXT NOT NULL
            )
        """)
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_content_hash ON content_hashes(content_hash)
        """)
        self._conn.commit()

    def _load(self):
        cursor = self._conn.execute("SELECT content_hash FROM content_hashes")
        self._hash_set = {row[0] for row in cursor.fetchall()}
        if self._hash_set and os.path.exists(self.bloom_path):
            self.bloom = BloomFilter.load(self.bloom_path)
        else:
            self.bloom = BloomFilter(capacity=max(1000, len(self._hash_set) * 2))
            for h in self._hash_set:
                self.bloom.add(h)

    def flush(self):
        """Persist bloom filter to disk. Call after batch operations."""
        self._conn.commit()
        self.bloom.save(self.bloom_path)

    def compute_hash(self, filepath: Path) -> str:
        """Compute SHA256 hash of file content."""
        h = hashlib.sha256()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                h.update(chunk)
        return h.hexdigest()

    def check_and_add(self, filepath: Path) -> bool:
        """Check if file content hash exists, add if not. Returns True if duplicate."""
        try:
            content_hash = self.compute_hash(filepath)
        except (OSError, IOError):
            return False

        filepath_str = str(filepath)

        if content_hash in self.bloom:
            if content_hash in self._hash_set:
                return True
            self._hash_set.add(content_hash)
            try:
                self._conn.execute(
                    "INSERT INTO content_hashes (filepath, content_hash) VALUES (?, ?)",
                    (filepath_str, content_hash),
                )
            except sqlite3.IntegrityError:
                return True
            return False
        else:
            self._hash_set.add(content_hash)
            try:
                self._conn.execute(
                    "INSERT INTO content_hashes (filepath, content_hash) VALUES (?, ?)",
                    (filepath_str, content_hash),
                )
            except sqlite3.IntegrityError:
                self._hash_set.discard(content_hash)
                return True
            self.bloom.add(content_hash)
            return False

    def record(self, filepath: Path):
        """Record a file without checking (for fallback after storage check)."""
        try:
            content_hash = self.compute_hash(filepath)
        except (OSError, IOError):
            return
        filepath_str = str(filepath)
        self._hash_set.add(content_hash)
        self._conn.execute(
            "INSERT OR REPLACE INTO content_hashes (filepath, content_hash) VALUES (?, ?)",
            (filepath_str, content_hash),
        )
        self.bloom.add(content_hash)

    def _get_hashes(self) -> dict:
        """Get all hashes as a dict (filepath -> content_hash)."""
        cursor = self._conn.execute("SELECT filepath, content_hash FROM content_hashes")
        return {row[0]: row[1] for row in cursor.fetchall()}

    @property
    def hashes(self) -> dict:
        return self._get_hashes()

    def clear(self):
        self._hash_set = set()
        self.bloom = BloomFilter()
        self._conn.execute("DELETE FROM content_hashes")
        self._conn.commit()
        if os.path.exists(self.bloom_path):
            os.remove(self.bloom_path)

    def close(self):
        self._conn.close()
