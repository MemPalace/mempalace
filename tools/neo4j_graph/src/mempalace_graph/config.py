from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv(_path: str | Path | None = None) -> bool:
        return False


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    neo4j_uri: str
    neo4j_username: str
    neo4j_password: str
    neo4j_database: str
    mempalace_home: Path
    mempalace_palace_dir: Path
    mempalace_knowledge_graph_db: Path
    mempalace_chroma_db: Path
    mempalace_write_log: Path
    mempalace_config: Path
    sync_state_path: Path
    sync_mode: Literal["soft_delete", "hard_delete", "ignore"]
    watch_debounce_seconds: float
    store_content: bool
    store_snippet: bool
    snippet_chars: int

    def validate_mempalace_paths(self, require_write_log: bool = False) -> None:
        config_exists = self.mempalace_config.exists() or (self.mempalace_home / "config.json").exists()
        required = [
            ("MemPalace home", self.mempalace_home),
            ("MemPalace palace directory", self.mempalace_palace_dir),
            ("knowledge_graph.sqlite3", self.mempalace_knowledge_graph_db),
            ("chroma.sqlite3", self.mempalace_chroma_db),
        ]
        if require_write_log:
            required.append(("write_log.jsonl", self.mempalace_write_log))
        missing = [f"{label} not found at {path}" for label, path in required if not path.exists()]
        if not config_exists:
            missing.append(f"config.json not found at {self.mempalace_config} or {self.mempalace_home / 'config.json'}")
        if missing:
            raise ConfigError("Error: " + "; ".join(missing))


def expand_path(value: str | Path) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(value)))).resolve()


def parse_bool(value: str | bool | None, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_config(args: argparse.Namespace | None = None, env_file: str | Path | None = ".env") -> Config:
    if env_file:
        load_dotenv(env_file)

    def arg(name: str, env: str, default: str | None = None) -> str | None:
        value = getattr(args, name, None) if args is not None else None
        return value if value is not None else os.getenv(env, default)

    home = expand_path(arg("mempalace_home", "MEMPALACE_HOME", "~/.mempalace"))
    palace_dir = expand_path(arg("mempalace_palace_dir", "MEMPALACE_PALACE_DIR", str(home / "palace")))
    kg_db = expand_path(arg("mempalace_knowledge_graph_db", "MEMPALACE_KNOWLEDGE_GRAPH_DB", str(palace_dir / "knowledge_graph.sqlite3")))
    chroma_db = expand_path(arg("mempalace_chroma_db", "MEMPALACE_CHROMA_DB", str(palace_dir / "chroma.sqlite3")))
    write_log = expand_path(arg("mempalace_write_log", "MEMPALACE_WRITE_LOG", str(palace_dir / "wal" / "write_log.jsonl")))
    config_json = expand_path(arg("mempalace_config", "MEMPALACE_CONFIG", str(palace_dir / "config.json")))
    sync_state = expand_path(arg("sync_state", "MEMPALACE_SYNC_STATE_PATH", ".sync/mempalace_sync.sqlite3"))
    sync_mode = arg("delete_mode", "MEMPALACE_SYNC_MODE", "soft_delete") or "soft_delete"
    if sync_mode not in {"soft_delete", "hard_delete", "ignore"}:
        raise ConfigError(f"Error: unsupported delete mode {sync_mode!r}")

    return Config(
        neo4j_uri=arg("neo4j_uri", "NEO4J_URI", "bolt://localhost:7687") or "bolt://localhost:7687",
        neo4j_username=arg("neo4j_username", "NEO4J_USERNAME", "neo4j") or "neo4j",
        neo4j_password=arg("neo4j_password", "NEO4J_PASSWORD", "mempalace_password") or "mempalace_password",
        neo4j_database=arg("neo4j_database", "NEO4J_DATABASE", "neo4j") or "neo4j",
        mempalace_home=home,
        mempalace_palace_dir=palace_dir,
        mempalace_knowledge_graph_db=kg_db,
        mempalace_chroma_db=chroma_db,
        mempalace_write_log=write_log,
        mempalace_config=config_json,
        sync_state_path=sync_state,
        sync_mode=sync_mode,  # type: ignore[arg-type]
        watch_debounce_seconds=float(arg("watch_debounce_seconds", "MEMPALACE_WATCH_DEBOUNCE_SECONDS", "1.5") or "1.5"),
        store_content=parse_bool(arg("store_content", "MEMPALACE_STORE_CONTENT", "false")),
        store_snippet=parse_bool(arg("store_snippet", "MEMPALACE_STORE_SNIPPET", "true"), True),
        snippet_chars=int(arg("snippet_chars", "MEMPALACE_SNIPPET_CHARS", "240") or "240"),
    )


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--mempalace-home")
    parser.add_argument("--mempalace-palace-dir")
    parser.add_argument("--mempalace-knowledge-graph-db")
    parser.add_argument("--mempalace-chroma-db")
    parser.add_argument("--mempalace-write-log")
    parser.add_argument("--mempalace-config")
    parser.add_argument("--sync-state")
    parser.add_argument("--delete-mode", choices=["soft_delete", "hard_delete", "ignore"])
    parser.add_argument("--neo4j-uri")
    parser.add_argument("--neo4j-username")
    parser.add_argument("--neo4j-password")
    parser.add_argument("--neo4j-database")
    parser.add_argument("--store-content", action="store_true")
    parser.add_argument("--snippet-chars", type=int)
