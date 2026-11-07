# Loaded into mempalace.hooks_cli via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.hooks_cli":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.hooks_cli")


def _get_mine_targets() -> list[tuple[str, str]]:
    """Return the list of ``(dir, mode)`` targets for auto-ingest.

    MEMPAL_DIR (when set and resolvable) contributes a ``"projects"``
    target. Transcript ingestion is handled separately by
    ``_ingest_transcript`` — emitting it here too would double-mine the
    same JSONL into a different wing on every hook fire (#1231 review).

    An empty list means no MEMPAL_DIR ingest should run.
    """
    targets: list[tuple[str, str]] = []
    mempal_dir = os.environ.get("MEMPAL_DIR", "")
    if mempal_dir:
        resolved = Path(mempal_dir).expanduser().resolve()
        if resolved.is_dir():
            targets.append((str(resolved), "projects"))
    return targets


# Per-target PID guard.
#
# Hook fires ingest mines in the background. If a previous fire's child is
# still running for the *same* target (same source dir, mode, wing), the new
# fire should skip rather than pile up — multiple concurrent mines against the
# same source corrupt the HNSW index and exhaust disk via duplicate upserts
# (#1212, #1206). But mines targeting *different* sources / modes must remain
# independent so the user can have e.g. project-mining and transcript-ingest
# running in parallel.
#
# The single ``mine.pid`` global file used previously failed both ways: the
# guard was rebuilt every spawn (so two near-simultaneous fires both passed
# the check before either wrote), and the file was unconditionally overwritten
# (so the second spawn lost the first PID, orphaning it). The replacement is
# a directory of per-target slots, claimed via ``O_CREAT | O_EXCL`` so the
# claim is atomic and per-target.
_MINE_PID_DIR = STATE_DIR / "mine_pids"

# The per-process PID file path is communicated to the mine subprocess via
# this env var so the child's cleanup hook (in miner.py) can remove its
# own slot on exit without scanning the whole directory.
_MINE_PID_FILE_ENV = "MEMPALACE_MINE_PID_FILE"

# Maximum wall-clock hours a mine subprocess is allowed to run before its
# PID slot is treated as stale (even if the process is still alive).  A
# wedged mine — e.g. one that is blocking indefinitely on ChromaDB
# cold-init under concurrent Windows load (#1552) — would otherwise hold
# its slot forever.  Set MEMPALACE_MINE_TIMEOUT_HOURS=0 to disable the
# timeout (slots are reclaimed only when the PID is dead).
_MINE_TIMEOUT_HOURS_ENV = "MEMPALACE_MINE_TIMEOUT_HOURS"
_MINE_TIMEOUT_HOURS_DEFAULT = 2.0


def _mine_slot_timeout_secs() -> float:
    """Return the configured mine-slot timeout in seconds.

    Reads ``MEMPALACE_MINE_TIMEOUT_HOURS`` from the environment (float).
    Returns 0 if the env var is set to 0 or is not parseable.
    """
    raw = os.environ.get(_MINE_TIMEOUT_HOURS_ENV, "")
    if raw:
        try:
            hours = float(raw)
            return max(0.0, hours) * 3600
        except ValueError:
            return 0.0
    return _MINE_TIMEOUT_HOURS_DEFAULT * 3600


def _pid_file_for_cmd(cmd: list[str]) -> Path:
    """Return the per-target PID file path for a mine subcommand.

    The key is derived from the mine arguments (everything after ``mine``)
    so different (dir, mode, wing) combinations get independent slots.
    Two fires with the same arguments collapse to the same slot — which is
    exactly the dedup we want.
    """
    try:
        idx = cmd.index("mine")
        key = " ".join(cmd[idx:])
    except ValueError:
        key = " ".join(cmd)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return _MINE_PID_DIR / f"mine_{digest}.pid"


def _pid_alive(pid: int) -> bool:
    """Cross-platform existence check for a PID.

    On POSIX, ``os.kill(pid, 0)`` is the well-known no-op existence probe.
    On Windows, ``os.kill`` maps to ``TerminateProcess(handle, sig)`` and
    would *terminate* the target process with exit code ``sig`` — using
    it here would kill our own mine child (or worse, the caller itself).
    Use ``OpenProcess`` + ``GetExitCodeProcess`` via ctypes instead.
    """
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def _mine_already_running(cmd: list[str]) -> bool:
    """Return True if a previous mine for ``cmd``'s target is still alive.

    The PID file format is ``{pid} {unix_timestamp}`` (timestamp added in
    #1552 to detect wedged subprocesses).  Old-format files (bare ``{pid}``)
    use the PID file's mtime as the approximate start time so a still-running
    pre-upgrade mine is not immediately misclassified as stale.

    A process is considered stale (and this function returns False) when:
    - the PID is dead, OR
    - the configured mine timeout is > 0 AND the process has been running
      longer than the timeout.
    """
    pid_file = _pid_file_for_cmd(cmd)
    try:
        recorded = pid_file.read_text().strip()
    except OSError:
        return False
    if not recorded:
        return False
    parts = recorded.split(None, 1)
    if not parts[0].isdigit():
        return False
    pid = int(parts[0])
    if not _pid_alive(pid):
        return False
    timeout_secs = _mine_slot_timeout_secs()
    if timeout_secs > 0:
        if len(parts) > 1 and parts[1]:
            try:
                start_ts = float(parts[1])
            except ValueError:
                return False
        else:
            try:
                start_ts = pid_file.stat().st_mtime
            except OSError:
                return True
        if time.time() - start_ts > timeout_secs:
            return False
    return True


def _create_mine_slot_with_placeholder(pid_file: Path) -> Path:
    """Atomically create a mine PID slot and write this hook PID into it.

    The slot body is ``{pid} {unix_timestamp}`` so that stale-by-age
    detection in ``_mine_already_running`` can determine how long the
    recorded process has been running (#1552).
    """
    fd = os.open(str(pid_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write(f"{os.getpid()} {int(time.time())}")
    except OSError:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            pid_file.unlink()
        except OSError:
            pass
        raise
    return pid_file


def _claim_mine_slot(cmd: list[str]) -> Optional[Path]:
    """Atomically reserve the per-target PID slot for ``cmd``.

    Returns the slot path on success, or ``None`` if the target is
    already being mined by a live process. The reservation is done via
    ``O_CREAT | O_EXCL`` so two simultaneous hook fires can never both
    pass the check; one wins, the other returns None.

    A stale slot (file exists but the recorded PID is dead) is reclaimed
    transparently — orphan miners that crashed without cleanup do not
    block future hook fires forever.
    """
    pid_file = _pid_file_for_cmd(cmd)
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        return _create_mine_slot_with_placeholder(pid_file)
    except FileExistsError:
        pass

    # Slot exists. If the holder is alive, defer.
    if _mine_already_running(cmd):
        return None

    # Stale entry; reclaim. The unlink+create is racy against another hook
    # firing right now, but the second create's O_EXCL will fail and that
    # caller will see the live PID via the next round.
    try:
        pid_file.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        return None

    try:
        return _create_mine_slot_with_placeholder(pid_file)
    except FileExistsError:
        return None


def _spawn_mine(cmd: list) -> None:
    """Spawn a mine subprocess if no live mine is already targeting it.

    The PID slot is claimed atomically *before* the spawn, so two near-
    simultaneous hook fires can't both proceed — the second sees the
    claimed slot and silently skips. The spawned process inherits a
    ``MEMPALACE_MINE_PID_FILE`` env var so its cleanup hook can remove
    the slot on exit without scanning the directory.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    log_path = STATE_DIR / "hook.log"
    pid_file = _claim_mine_slot(cmd)
    if pid_file is None:
        _log(f"Skipping mine: target already running ({' '.join(cmd[-3:])})")
        return
    child_env = os.environ.copy()
    child_env[_MINE_PID_FILE_ENV] = str(pid_file)
    with open(log_path, "a") as log_f:
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=log_f,
                stderr=log_f,
                env=child_env,
                **_detached_popen_kwargs(),
            )
        except OSError:
            # Spawn failed; release the slot we just claimed so the next
            # hook fire can try again rather than skipping forever.
            try:
                pid_file.unlink()
            except OSError:
                pass
            raise
    try:
        pid_file.write_text(f"{proc.pid} {int(time.time())}")
    except OSError:
        pass
