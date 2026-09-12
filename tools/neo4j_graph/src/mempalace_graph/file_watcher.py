from __future__ import annotations

import logging
import time
from pathlib import Path
from threading import Lock, Timer

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .config import Config
from .sync_engine import sync_once

logger = logging.getLogger(__name__)


class DebouncedSyncHandler(FileSystemEventHandler):
    def __init__(self, config: Config, create_schema: bool = False) -> None:
        self.config = config
        self.create_schema = create_schema
        self.timer: Timer | None = None
        self.lock = Lock()
        self.watch_names = {
            "knowledge_graph.sqlite3",
            "chroma.sqlite3",
            "knowledge_graph.sqlite3-wal",
            "knowledge_graph.sqlite3-shm",
            "write_log.jsonl",
            "config.json",
        }

    def on_any_event(self, event: FileSystemEvent) -> None:
        path = Path(event.src_path)
        if path.suffix in {".bin", ".pickle"} or path.name.startswith("."):
            return
        if path.name not in self.watch_names:
            return
        with self.lock:
            if self.timer:
                self.timer.cancel()
            self.timer = Timer(self.config.watch_debounce_seconds, self._run_sync)
            self.timer.daemon = True
            self.timer.start()

    def _run_sync(self) -> None:
        try:
            result = sync_once(self.config, create_schema=self.create_schema)
            print(f"MemPalace sync complete: {result.records_upserted} records upserted, {len(result.errors)} warnings")
        except Exception as exc:
            logger.error("Sync failed: %s", exc)


def watch(config: Config, create_schema: bool = False) -> None:
    observer = Observer()
    handler = DebouncedSyncHandler(config, create_schema=create_schema)
    for path in {config.mempalace_palace_dir, config.mempalace_home / "wal"}:
        if path.exists():
            observer.schedule(handler, str(path), recursive=True)
    observer.start()
    print("Watching MemPalace files. Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()
