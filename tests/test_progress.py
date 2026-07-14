from mempalace.progress import ProgressReporter


def test_progress_reporter_merges_state_and_throttles(monkeypatch):
    now = {"value": 10.0}
    monkeypatch.setattr("mempalace.progress.time.monotonic", lambda: now["value"])
    events = []
    reporter = ProgressReporter(events.append, min_interval=1.0)

    reporter.emit(force=True, phase="scanning", files_total=2)
    reporter.emit(files_processed=1)
    now["value"] = 11.1
    reporter.emit(files_processed=2, files_changed=1)

    assert events == [
        {"phase": "scanning", "files_total": 2},
        {
            "phase": "scanning",
            "files_total": 2,
            "files_processed": 2,
            "files_changed": 1,
        },
    ]


def test_progress_reporter_never_breaks_mining_when_callback_fails():
    reporter = ProgressReporter(lambda _event: (_ for _ in ()).throw(RuntimeError("boom")))
    reporter.emit(force=True, phase="scanning")
