"""Offline, non-destructive SQLite salvage for an unreadable Chroma palace.

SQLite recovery is best effort, not proof that the original words are complete
or unchanged. This produces a reviewable artifact in a new directory; it never
activates that directory, opens Chroma, or infers text from orphaned vectors.
"""

import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import time
from contextlib import closing


class SQLiteSalvageError(RuntimeError):
    """Salvage failed; any forensic staging directory is retained."""


_REQUIRED_COLUMNS = {
    "collections": {"id", "name"},
    "segments": {"id", "collection", "scope"},
    "embeddings": {"id", "segment_id", "embedding_id"},
    "embedding_metadata": {"id", "key", "string_value", "int_value", "float_value"},
}
_SOURCE_FILES = ("chroma.sqlite3", "chroma.sqlite3-wal", "chroma.sqlite3-shm")
_LIMITATIONS = (
    "SQLite salvage can omit, resurrect or alter records. Integrity checks do not prove "
    "verbatim completeness; compare against backups and original transcripts before use.",
    "HNSW vectors cannot reconstruct missing document text. HNSW files, knowledge graph "
    "and media files are not recovered or copied by this operation.",
    "Queued writes and unassigned recovered rows require separate review before rebuilding.",
)


def _signature(st):
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _same_file_content(path_sig, fd_sig):
    """Compare a path ``lstat`` signature with an open handle's ``fstat`` one.

    On Windows ``lstat`` and ``fstat`` fill ``st_dev``/``st_ino``/``st_ctime``
    from different APIs, so the two never compare equal for the same
    untouched file. Size and modification time are reliable on every
    platform; identity fields are compared only where they agree (POSIX).
    """
    if os.name == "nt":
        return path_sig[2:4] == fd_sig[2:4]
    return path_sig == fd_sig


def _source_inventory(source):
    inventory = {}
    for name in _SOURCE_FILES:
        path = source / name
        try:
            st = path.lstat()
        except FileNotFoundError:
            if name == "chroma.sqlite3":
                raise SQLiteSalvageError(f"Source has no chroma.sqlite3: {source}") from None
            continue
        if not stat.S_ISREG(st.st_mode):
            raise SQLiteSalvageError(f"Source {name} must be a regular file, not a link or device")
        if name == "chroma.sqlite3" and st.st_size == 0:
            raise SQLiteSalvageError(
                "Source chroma.sqlite3 is empty; there are no pages to salvage"
            )
        inventory[name] = _signature(st)
    return inventory


def _paths(source_palace, dest_palace):
    if not source_palace or not dest_palace:
        raise SQLiteSalvageError("Salvage requires explicit --source and --palace paths")
    source_raw = Path(os.path.abspath(os.path.expanduser(source_palace)))
    dest_raw = Path(os.path.abspath(os.path.expanduser(dest_palace)))
    # Reject supplied directory links and resolve ancestors before overlap
    # checks (system ancestors such as /var on macOS are themselves links).
    for path in (source_raw, dest_raw):
        if path.is_symlink():
            raise SQLiteSalvageError(f"Salvage source/destination must not be symlinks: {path}")
    source, dest = source_raw.resolve(), dest_raw.resolve()
    if not source.is_dir():
        raise SQLiteSalvageError(f"Source palace is not a directory: {source}")
    if source == dest or source in dest.parents or dest in source.parents:
        raise SQLiteSalvageError("Source and destination must be separate, non-nested directories")
    if os.path.lexists(dest):
        raise SQLiteSalvageError(f"Destination must be fresh and absent: {dest}")
    if not dest.parent.is_dir():
        raise SQLiteSalvageError(f"Destination parent directory does not exist: {dest.parent}")
    _source_inventory(source)
    return source, dest


def _snapshot(source, target):
    """Copy regular files only, detecting changes across the entire snapshot."""
    before = _source_inventory(source)
    hashes = {}
    for name, expected in before.items():
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        fd = os.open(source / name, flags)
        with os.fdopen(fd, "rb") as src:
            opened = _signature(os.fstat(src.fileno()))
            if not _same_file_content(expected, opened):
                raise SQLiteSalvageError(f"Source {name} changed before the snapshot")
            out_fd = os.open(target / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            digest = hashlib.sha256()
            with os.fdopen(out_fd, "wb") as out:
                for chunk in iter(lambda: src.read(1024 * 1024), b""):
                    out.write(chunk)
                    digest.update(chunk)
                out.flush()
                os.fsync(out.fileno())
            if _signature(os.fstat(src.fileno())) != opened:
                raise SQLiteSalvageError(f"Source {name} changed during the snapshot")
        hashes[name] = {"sha256": digest.hexdigest(), "bytes": expected[2]}
    if _source_inventory(source) != before:
        raise SQLiteSalvageError("Source SQLite/sidecar files changed during the snapshot")
    return hashes


def _run_sqlite(command, *, deadline, stdin=None, stdout=None, error_file):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SQLiteSalvageError("SQLite salvage timeout expired")
    with open(error_file, "wb") as err:
        try:
            result = subprocess.run(
                command,
                stdin=stdin,
                stdout=stdout,
                stderr=err,
                timeout=remaining,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise SQLiteSalvageError(
                "SQLite salvage timed out; original files are unchanged"
            ) from exc
        except OSError as exc:
            raise SQLiteSalvageError(f"Could not start local sqlite3: {exc}") from exc
    if result.returncode:
        with open(error_file, "rb") as err:
            detail = err.read(1500).decode("utf-8", errors="replace").strip()
        raise SQLiteSalvageError(
            f"sqlite3 failed (exit {result.returncode}): {detail or 'no diagnostic'}; "
            "a sqlite3 CLI with .recover and -safe support is required "
            "(including the sqlite_dbpage recovery extension)"
        )


# PRAGMAs that ``.recover`` emits to rebuild a database. Anything else in the
# recovered script is refused rather than run.
_RECOVERY_PRAGMAS = frozenset(
    {
        "application_id",
        "auto_vacuum",
        "encoding",
        "foreign_keys",
        "page_size",
        "user_version",
        "writable_schema",
    }
)
# Functions that could reach outside the private stage database.
_DENIED_FUNCTIONS = frozenset({"load_extension", "readfile", "writefile", "edit", "fts3_tokenizer"})


def _recovery_authorizer(action, arg1, arg2, _db, _trigger):
    if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH):
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_PRAGMA and (arg1 or "").lower() not in _RECOVERY_PRAGMAS:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in _DENIED_FUNCTIONS:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _apply_recovery_sql(sql_path, recovered, *, deadline, error_file):
    """Run ``.recover``'s script into the empty private stage database.

    ``.recover`` restores virtual tables (Chroma's FTS5
    ``embedding_fulltext_search``) by inserting their ``sqlite_schema`` row
    under ``PRAGMA writable_schema``, which ``sqlite3 -safe`` forbids, so every
    real Chroma palace failed to apply. The script is applied here instead,
    with the same intent as ``-safe``: no ATTACH/DETACH, no extension loading
    or file functions, only the PRAGMAs ``.recover`` writes, untrusted schema,
    and the overall deadline. The source snapshot is never opened here.
    """
    if time.monotonic() >= deadline:
        raise SQLiteSalvageError("SQLite salvage timed out; original files are unchanged")
    script = Path(sql_path).read_bytes().decode("utf-8", errors="surrogateescape")
    # sqlite3 3.48+ starts the script with a shell dot-command so the CLI can
    # write sqlite_schema. A Python connection is not in defensive mode, so it
    # is dropped; any other dot-command is a syntax error here (fails closed).
    for directive in (".dbconfig defensive off\n", ".dbconfig defensive off\r\n"):
        if script.startswith(directive):
            script = script[len(directive) :]
            break
    try:
        with closing(sqlite3.connect(str(recovered), isolation_level=None)) as conn:
            conn.execute("PRAGMA trusted_schema=OFF")
            conn.set_authorizer(_recovery_authorizer)
            conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            conn.executescript(script)
    except (sqlite3.Error, UnicodeEncodeError) as exc:
        Path(error_file).write_text(f"{type(exc).__name__}: {exc}\n", encoding="utf-8")
        if time.monotonic() >= deadline:
            raise SQLiteSalvageError(
                "SQLite salvage timed out; original files are unchanged"
            ) from exc
        raise SQLiteSalvageError(f"Applying the recovered SQL failed: {exc}") from exc


def _census(db_path, *, deadline):
    """Validate the artifact, then count stored text without opening Chroma."""
    uri = db_path.as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        conn.execute("PRAGMA trusted_schema=OFF")
        verdict = [row[0] for row in conn.execute("PRAGMA integrity_check")]
        if verdict != ["ok"]:
            raise SQLiteSalvageError(f"Recovered SQLite failed integrity_check: {verdict[:5]}")
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        for table, required in _REQUIRED_COLUMNS.items():
            if table not in tables:
                raise SQLiteSalvageError(f"Recovered database lacks Chroma table {table!r}")
            columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
            missing = required - columns
            if missing:
                raise SQLiteSalvageError(
                    f"Recovered Chroma table {table!r} lacks columns {sorted(missing)}"
                )
        # Count only records still assignable to a named Chroma collection and
        # carrying actual stored text, never treating an orphaned vector as text.
        collections = []
        for name, cid in conn.execute("SELECT name, id FROM collections ORDER BY name, id"):
            if not isinstance(name, str) or not name:
                raise SQLiteSalvageError("Recovered collection has no valid name")
            rows, texts = conn.execute(
                """
                SELECT COUNT(DISTINCT e.id), COUNT(DISTINCT CASE
                    WHEN em.key='chroma:document' AND typeof(em.string_value)='text'
                    THEN e.id END)
                FROM segments s LEFT JOIN embeddings e ON e.segment_id=s.id
                LEFT JOIN embedding_metadata em ON em.id=e.id
                WHERE s.collection=? AND s.scope='METADATA'
                """,
                (cid,),
            ).fetchone()
            collections.append({"name": name, "embedding_rows": rows, "document_rows": texts})
        text_count = sum(c["document_rows"] for c in collections)
        if not text_count:
            raise SQLiteSalvageError(
                "No stored Chroma document text was recovered; refusing an empty recovery success"
            )
        orphan_embeddings = conn.execute(
            "SELECT COUNT(*) FROM embeddings e LEFT JOIN segments s ON s.id=e.segment_id "
            "LEFT JOIN collections c ON c.id=s.collection WHERE c.id IS NULL"
        ).fetchone()[0]
        orphan_metadata = conn.execute(
            "SELECT COUNT(*) FROM embedding_metadata m LEFT JOIN embeddings e ON e.id=m.id "
            "WHERE e.id IS NULL"
        ).fetchone()[0]
        pending = (
            conn.execute("SELECT COUNT(*) FROM embeddings_queue").fetchone()[0]
            if "embeddings_queue" in tables
            else 0
        )
        lost = (
            conn.execute("SELECT COUNT(*) FROM lost_and_found").fetchone()[0]
            if "lost_and_found" in tables
            else 0
        )
    return {
        "integrity_check": verdict,
        "collections": collections,
        "document_rows": text_count,
        "orphan_embedding_rows": orphan_embeddings,
        "orphan_metadata_rows": orphan_metadata,
        "pending_queue_rows": pending,
        "lost_and_found_rows": lost,
    }


def _file_hash(path):
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def salvage_sqlite(source_palace, dest_palace, *, dry_run=False, timeout=300, progress=print):
    """Salvage SQLite pages into a fresh review directory, preserving originals.

    Requires a local ``sqlite3`` CLI with ``.recover`` and ``-safe`` support.
    ``.recover`` runs in a ``-safe`` subprocess against a private snapshot; its
    script is applied in-process to an empty stage database under an authorizer
    with the same intent (see :func:`_apply_recovery_sql`). The original palace's existing writer lease is respected;
    uncoordinated external writers are detected through snapshot file identity,
    size and timestamp checks, rather than promised to be fenced by that lease.
    """
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise SQLiteSalvageError("SQLite salvage timeout must be a positive finite number")
    if not math.isfinite(timeout) or timeout <= 0:
        raise SQLiteSalvageError("SQLite salvage timeout must be a positive finite number")
    source, dest = _paths(source_palace, dest_palace)
    if dry_run:
        progress(f"DRY RUN: salvage SQLite from {source} into fresh directory {dest}")
        progress("Original files stay untouched. The result requires review before rebuilding.")
        return {"dry_run": True, "source": str(source), "destination": str(dest)}
    binary = shutil.which("sqlite3")
    if binary is None:
        raise SQLiteSalvageError(
            "Local sqlite3 CLI was not found; salvage needs sqlite3 with .recover support. "
            "No files were copied or changed."
        )
    from .palace import MineAlreadyRunning, mine_palace_lock

    stage = None
    try:
        with mine_palace_lock(str(source)):
            # Recheck after the lease, before making any output. Do not copy an
            # online palace whose cooperating owner holds its writer lease.
            source, dest = _paths(str(source), str(dest))
            stage = Path(tempfile.mkdtemp(prefix=f".{dest.name}.salvage-", dir=dest.parent))
            snapshot = stage / "source-snapshot"
            snapshot.mkdir(mode=0o700)
            inputs = _snapshot(source, snapshot)
        deadline = time.monotonic() + timeout
        base = [binary, "-safe", "-batch", "-bail", "-init", os.devnull]
        version_path = stage / "sqlite-version.txt"
        with open(version_path, "wb") as version:
            _run_sqlite(
                [binary, "-version"],
                deadline=deadline,
                stdout=version,
                error_file=stage / "version-error.txt",
            )
        sql_path = stage / "recovery.sql"
        with open(sql_path, "wb") as sql:
            _run_sqlite(
                [
                    *base,
                    "-readonly",
                    str(snapshot / "chroma.sqlite3"),
                    ".recover --ignore-freelist",
                ],
                deadline=deadline,
                stdout=sql,
                error_file=stage / "recover-error.txt",
            )
        recovered = stage / "chroma.sqlite3"
        fd = os.open(recovered, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        _apply_recovery_sql(
            sql_path, recovered, deadline=deadline, error_file=stage / "apply-error.txt"
        )
        census = _census(recovered, deadline=deadline)
        with open(version_path, "rb") as version:
            cli_version = version.read(2000).decode("utf-8", errors="replace").strip()
        manifest = {
            "format_version": 1,
            "status": "salvaged-requires-content-review",
            "source": str(source),
            "source_files": inputs,
            "sqlite_cli": cli_version,
            "recovered_sqlite_sha256": _file_hash(recovered),
            "recovery_sql_sha256": _file_hash(sql_path),
            "census": census,
            "limitations": list(_LIMITATIONS),
        }
        (stage / "salvage-manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        # mkdir is exclusive even on Unix, where rename could otherwise replace
        # an empty directory created by another actor after the initial check.
        dest.mkdir(mode=0o700)
        try:
            for entry in stage.iterdir():
                entry.rename(dest / entry.name)
            stage.rmdir()
        except BaseException:
            # Keep both partial publication and staging. Never remove evidence.
            raise SQLiteSalvageError(
                f"Salvage publication was interrupted. Keep {dest} and {stage}; "
                "the original palace is unchanged."
            ) from None
        progress(f"Recovered {census['document_rows']} stored document row(s) into {dest}")
        progress(f"Review {dest / 'salvage-manifest.json'}; completeness is not proven.")
        progress(
            "After reviewing text, queued writes and assets, rebuild into another fresh palace "
            "with: mempalace repair --mode from-sqlite --source <salvage-dir> "
            "--palace <new-palace>"
        )
        return manifest
    except MineAlreadyRunning as exc:
        raise SQLiteSalvageError(
            f"Source has a live writer: {exc}. Stop it or use an offline archived copy; "
            "no salvage copy was started."
        ) from exc
    except Exception as exc:
        evidence = f" Forensic files retained at {stage}." if stage is not None else ""
        if isinstance(exc, SQLiteSalvageError):
            raise SQLiteSalvageError(f"{exc}{evidence}") from exc
        raise SQLiteSalvageError(f"SQLite salvage failed: {exc}.{evidence}") from exc
