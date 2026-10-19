from pathlib import Path

from mempalace_graph.sync_state import SyncState


def test_sync_state_schema_and_forbidden_columns(tmp_path: Path) -> None:
    state = SyncState(tmp_path / "sync.sqlite3")
    try:
        assert state.forbidden_columns() == []
        run_id = state.start_sync_run()
        state.finish_sync_run(run_id, "success")
        state.replace_records_for_source("source", {"loc": "mem"})
        assert state.get_records_for_source("source") == {"loc": "mem"}
    finally:
        state.close()
