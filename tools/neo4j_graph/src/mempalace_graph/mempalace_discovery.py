from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import Config

UUID_DIR = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


@dataclass(frozen=True)
class FileInfo:
    path: Path
    exists: bool
    size_bytes: int | None
    modified_at: str | None
    sha256: str | None


@dataclass(frozen=True)
class DiscoveryReport:
    home: Path
    palace_dir: Path
    knowledge_graph_db: FileInfo
    chroma_db: FileInfo
    write_log: FileInfo
    config_json: FileInfo
    ignored_internal_paths: list[Path]


def file_info(path: Path, hash_file: bool = True, max_hash_bytes: int = 100 * 1024 * 1024) -> FileInfo:
    if not path.exists():
        return FileInfo(path=path, exists=False, size_bytes=None, modified_at=None, sha256=None)
    stat = path.stat()
    modified_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
    digest = None
    if hash_file and path.is_file() and stat.st_size <= max_hash_bytes:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        digest = h.hexdigest()
    return FileInfo(path=path, exists=True, size_bytes=stat.st_size, modified_at=modified_at, sha256=digest)


def discover_mempalace(config: Config, hash_sqlite: bool = True) -> DiscoveryReport:
    write_log = config.mempalace_write_log
    fallback_write_log = config.mempalace_home / "wal" / "write_log.jsonl"
    if not write_log.exists() and fallback_write_log.exists():
        write_log = fallback_write_log
    config_json = config.mempalace_config
    fallback_config = config.mempalace_home / "config.json"
    if not config_json.exists() and fallback_config.exists():
        config_json = fallback_config

    ignored: list[Path] = []
    if config.mempalace_palace_dir.exists():
        for child in config.mempalace_palace_dir.iterdir():
            if child.is_dir() and UUID_DIR.match(child.name):
                ignored.append(child)
            elif child.suffix in {".bin", ".pickle"}:
                ignored.append(child)

    return DiscoveryReport(
        home=config.mempalace_home,
        palace_dir=config.mempalace_palace_dir,
        knowledge_graph_db=file_info(config.mempalace_knowledge_graph_db, hash_file=hash_sqlite),
        chroma_db=file_info(config.mempalace_chroma_db, hash_file=hash_sqlite),
        write_log=file_info(write_log, hash_file=True),
        config_json=file_info(config_json, hash_file=True),
        ignored_internal_paths=ignored,
    )
