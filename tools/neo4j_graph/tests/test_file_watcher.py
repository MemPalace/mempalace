import sys
from pathlib import Path
from types import SimpleNamespace

from mempalace_graph.config import Config


class FakeTimer:
    def __init__(self, interval, function) -> None:
        self.interval = interval
        self.function = function
        self.cancelled = False
        self.daemon = False
        self.started = False

    def cancel(self) -> None:
        self.cancelled = True

    def start(self) -> None:
        self.started = True


def make_config(tmp_path: Path) -> Config:
    return Config(
        neo4j_uri="bolt://localhost:7687",
        neo4j_username="neo4j",
        neo4j_password="password",
        neo4j_database="neo4j",
        mempalace_home=tmp_path,
        mempalace_palace_dir=tmp_path / "palace",
        mempalace_knowledge_graph_db=tmp_path / "palace" / "knowledge_graph.sqlite3",
        mempalace_chroma_db=tmp_path / "palace" / "chroma.sqlite3",
        mempalace_write_log=tmp_path / "palace" / "wal" / "write_log.jsonl",
        mempalace_config=tmp_path / "palace" / "config.json",
        sync_state_path=tmp_path / ".sync" / "state.sqlite3",
        sync_mode="soft_delete",
        watch_debounce_seconds=0.1,
        store_content=False,
        store_snippet=True,
        snippet_chars=240,
    )


def test_debounce_replaces_pending_timer_under_lock(tmp_path: Path, monkeypatch) -> None:
    sys.modules.setdefault(
        "watchdog.events",
        SimpleNamespace(FileSystemEvent=object, FileSystemEventHandler=object),
    )
    sys.modules.setdefault("watchdog.observers", SimpleNamespace(Observer=object))
    from mempalace_graph.file_watcher import DebouncedSyncHandler

    monkeypatch.setattr("mempalace_graph.file_watcher.Timer", FakeTimer)
    handler = DebouncedSyncHandler(make_config(tmp_path))
    event = SimpleNamespace(src_path=str(tmp_path / "knowledge_graph.sqlite3"))

    handler.on_any_event(event)
    first_timer = handler.timer
    handler.on_any_event(event)

    assert first_timer is not None
    assert first_timer.cancelled
    assert handler.timer is not first_timer
    assert isinstance(handler.timer, FakeTimer)
    assert handler.timer.started
