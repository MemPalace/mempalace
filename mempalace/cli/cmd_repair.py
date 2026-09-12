# Loaded into mempalace.cli via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.cli":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.cli")


def cmd_palace_set_embedder(args):
    """Record (or force-override) a palace's embedder identity (RFC 001).

    Resolves the ``unknown`` state for a legacy palace, or records a specific
    model with ``--model``. It records identity on the palace only; it does not
    change the configured model — when the two differ it prints how to align
    ``MEMPALACE_EMBEDDING_MODEL``. ``--force`` overwrites an existing,
    differently-named identity.
    """
    from ..backends.base import EmbedderIdentityMismatchError
    from ..palace import set_palace_embedder_identity

    config = MempalaceConfig()
    palace_path = os.path.abspath(
        os.path.expanduser(args.palace) if args.palace else config.palace_path
    )
    model = getattr(args, "model", None)
    try:
        old, new = set_palace_embedder_identity(
            palace_path,
            model=model,
            force=getattr(args, "force", False),
            backend=_backend_arg(args),
        )
    except EmbedderIdentityMismatchError as exc:
        print(f"  ✗ {exc}")
        raise SystemExit(2) from exc
    if old is None:
        print(f"  ✓ recorded embedder identity: {new.model_name} (dim={new.dimension})")
    elif old.model_name == new.model_name:
        print(f"  ✓ embedder identity unchanged: {new.model_name} (dim={new.dimension})")
    else:
        print(
            f"  ✓ embedder identity changed: {old.model_name} → {new.model_name} "
            f"(dim={new.dimension})"
        )
    # set-embedder records the palace's identity; it does not change the
    # configured model. If they differ, the next normal open would mismatch —
    # tell the user how to align them.
    configured = config.embedding_model
    if new.model_name and configured and new.model_name != configured:
        print(
            f"  ⚠ configured model is {configured!r}; set MEMPALACE_EMBEDDING_MODEL="
            f"{new.model_name} (or run onboarding) so normal opens of this palace match."
        )


def cmd_repair_status(args):
    """Read-only HNSW capacity health check (#1222)."""
    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    if not _maintenance_requires_chroma(palace_path, "repair-status"):
        raise SystemExit(2)
    from ..repair import status as repair_status

    repair_status(palace_path=palace_path)


def cmd_repair(args):
    """Rebuild palace vector index from SQLite metadata.

    On success the palace SQLite file is VACUUMed and the FTS5 index is
    rebuilt, so the next repair's integrity preflight reads a consistent
    database (#1747).
    """
    config = MempalaceConfig()
    collection_name = config.collection_name
    palace_path = os.path.abspath(
        os.path.expanduser(args.palace) if args.palace else config.palace_path
    )
    if not _maintenance_requires_chroma(palace_path, "repair"):
        raise SystemExit(2)

    import shutil
    from ..backends.chroma import ChromaBackend
    from ..backups import copy_palace_dir
    from ..migrate import confirm_destructive_action, contains_palace_database
    from ..repair import (
        RebuildCollectionError,
        TruncationDetected,
        _close_chroma_handles,
        _extract_drawers,
        _post_rebuild_cleanup,
        _preview_legacy_repair,
        _promote_temp_collection,
        _rebuild_collection_via_temp,
        check_extraction_safety,
        index_read_recovery_guidance,
        maybe_repair_poisoned_max_seq_id_before_rebuild,
        print_sqlite_integrity_abort,
        resolve_repair_preflight_errors,
        sqlite_integrity_errors,
    )

    if getattr(args, "repair_action", None) == "rebuild-index":
        args.mode = "from-sqlite"
        args.archive_existing = True

    if getattr(args, "mode", "legacy") == "max-seq-id":
        from ..repair import repair_max_seq_id

        repair_max_seq_id(
            palace_path,
            segment=getattr(args, "segment", None),
            from_sidecar=getattr(args, "from_sidecar", None),
            backup=getattr(args, "backup", True),
            dry_run=getattr(args, "dry_run", False),
            assume_yes=getattr(args, "yes", False),
        )
        return

    if getattr(args, "mode", "legacy") == "from-sqlite":
        from ..migrate import confirm_destructive_action
        from ..repair import RebuildCleanupError, RebuildPartialError, rebuild_from_sqlite

        source_path = getattr(args, "source", None)
        source_path = (
            os.path.abspath(os.path.expanduser(source_path)) if source_path else palace_path
        )
        archive_existing = getattr(args, "archive_existing", False)

        # Gate any path that touches the user's existing palace dir
        # behind confirm_destructive_action. The legacy mode already
        # gates; from-sqlite needs the same protection because:
        # (a) --archive-existing renames the existing palace,
        # (b) --source PATH writes into --palace dir which the user
        #     may not realize is also a palace.
        # No prompt when source != dest AND dest does not exist (pure
        # extract-into-fresh-dir case is non-destructive to existing
        # palaces).
        # A --dry-run only reads the source SQLite and prints a plan — it
        # never archives, creates, or writes — so it must not trip the
        # destructive-action confirmation (#2095, #2133).
        dry_run = getattr(args, "dry_run", False)
        is_destructive_to_dest = source_path == palace_path or os.path.exists(palace_path)
        if (
            not dry_run
            and is_destructive_to_dest
            and not confirm_destructive_action(
                "Rebuild from SQLite", palace_path, assume_yes=getattr(args, "yes", False)
            )
        ):
            return

        try:
            counts = rebuild_from_sqlite(
                source_palace=source_path,
                dest_palace=palace_path,
                archive_existing_dest=archive_existing,
                dry_run=dry_run,
            )
        except RebuildPartialError as exc:
            # The error itself was already printed by rebuild_from_sqlite
            # with recovery instructions; surface a non-zero exit so
            # scripts and CI gates see the failure.
            print(
                "\n  Rebuild partial — see message above. "
                f"Failed in collection: {exc.failed_collection}"
            )
            sys.exit(1)
        except RebuildCleanupError:
            # All rows may have landed, but rebuild_from_sqlite deliberately
            # withholds success until FTS5 rebuild, VACUUM, and quick_check are
            # clean. Its exception already includes the retained destination
            # and archive/source recovery paths.
            print("\n  Rebuild cleanup failed -- see recovery details above.")
            sys.exit(1)
        # An empty counts dict is rebuild_from_sqlite's documented signal
        # for a validation refusal (missing source, existing dest,
        # in-place without --archive-existing). The library already
        # printed an actionable message; exit non-zero so unattended
        # scripts/CI distinguish "invalid inputs" from a successful
        # rebuild that legitimately found zero rows (which still returns
        # a populated dict with 0-valued counts).
        if not counts:
            sys.exit(1)
        return

    db_path = os.path.join(palace_path, "chroma.sqlite3")

    if not os.path.isdir(palace_path):
        print(f"\n  No palace found at {palace_path}")
        return
    if not contains_palace_database(palace_path):
        print(f"\n No palace database found at {db_path}")
        return

    # Run the SQLite integrity preflight before any chromadb client open.
    # ChromaDB's rust binding raises pyo3_runtime.PanicException on a
    # malformed page, which is not a regular Exception subclass and
    # propagates past the try/except below — the user gets a 30-line
    # stack trace instead of the friendly abort message. Run quick_check
    # here so we can surface the clear recovery instructions and exit
    # cleanly before chromadb's compactor touches the disk.
    dry_run = getattr(args, "dry_run", False)
    # The FTS5 autoheal inside this call is a write, so a --dry-run predicts
    # its outcome instead of performing it (#1596 is auto-healable and must
    # not surface as an abort in a preview).
    sqlite_errors = resolve_repair_preflight_errors(
        palace_path, sqlite_integrity_errors(palace_path), dry_run=dry_run
    )
    if sqlite_errors:
        print_sqlite_integrity_abort(palace_path, sqlite_errors)
        sys.exit(1)

    preflight = maybe_repair_poisoned_max_seq_id_before_rebuild(
        palace_path,
        backup=getattr(args, "backup", True),
        dry_run=dry_run,
        assume_yes=getattr(args, "yes", False),
    )
    if preflight is not None:
        return

    print(f"\n{'=' * 55}")
    print(" MemPalace Repair")
    print(f"{'=' * 55}\n")
    print(f"  Palace: {palace_path}")

    if dry_run:
        # Return before the backend is used at all: the chromadb client this
        # path opens is itself a write to chroma.sqlite3 (measured — the file
        # hash changes on get_collection alone, before count()), so a preview
        # that reached it could not be inert. Staying off the chromadb layer
        # also keeps a dry run clear of the layer repair is separately reported
        # to segfault in on a large palace (#2113). Exit non-zero on an
        # unreadable count for parity with the from-sqlite preview above, so
        # `--dry-run && repair --yes` cannot walk into the destructive run
        # after a failed preview (#2095, #2133).
        if not _preview_legacy_repair(
            palace_path=palace_path,
            collection_name=collection_name,
            confirm_truncation_ok=getattr(args, "confirm_truncation_ok", False),
        ):
            sys.exit(1)
        return

    backend = ChromaBackend()

    # Try to read existing drawers
    try:
        col = backend.get_collection(palace_path, collection_name)
        total = col.count()
        print(f"  Drawers found: {total}")
    except Exception as e:
        print(f"  Error reading palace: {e}")
        print(index_read_recovery_guidance())
        return

    if total == 0:
        print("  Nothing to repair.")
        return

    if not confirm_destructive_action(
        "Repair", palace_path, assume_yes=getattr(args, "yes", False)
    ):
        return

    # Extract all drawers in batches
    print("\n  Extracting drawers...")
    batch_size = 5000
    all_ids, all_docs, all_metas = _extract_drawers(col, total, batch_size)
    print(f"  Extracted {len(all_ids)} drawers")

    # ── #1208 guard ──────────────────────────────────────────────────
    # Cross-check against the SQLite ground truth before doing anything
    # destructive. Catches the user-reported case where chromadb's
    # collection-layer get() silently caps at 10,000 rows even on much
    # larger palaces (e.g. after manual HNSW quarantine). Override with
    # --confirm-truncation-ok only after independently verifying the
    # extraction count is real.
    try:
        check_extraction_safety(
            palace_path,
            len(all_ids),
            confirm_truncation_ok=getattr(args, "confirm_truncation_ok", False),
            collection_name=collection_name,
        )
    except TruncationDetected as e:
        print(e.message)
        return

    palace_path = os.path.normpath(palace_path)
    backup_path = palace_path + ".backup"
    if os.path.exists(backup_path):
        if not contains_palace_database(backup_path):
            print(
                "  Backup validation failed: backup path exists but does not contain chroma.sqlite3. "
                f"Please remove or rename: {backup_path}"
            )
            return
        shutil.rmtree(backup_path)
    print(f"  Backing up to {backup_path}...")
    copy_palace_dir(palace_path, backup_path, log=print)

    try:
        filed = _rebuild_collection_via_temp(
            backend,
            palace_path,
            all_ids,
            all_docs,
            all_metas,
            batch_size,
            collection_name=collection_name,
            progress=print,
        )
    except RebuildCollectionError as e:
        print(f"  Repair failed: {e}")
        if getattr(e, "live_replaced", False):
            temp_name = f"{collection_name}__repair_tmp"
            print(f"  Attempting recovery: promoting verified copy from '{temp_name}'...")
            try:
                _close_chroma_handles(palace_path, backend=backend)
                _promote_temp_collection(
                    backend,
                    palace_path,
                    temp_name,
                    collection_name,
                    len(all_ids),
                    batch_size,
                    progress=print,
                )
                print("  Recovery succeeded: live collection restored from the verified temp copy.")
            except Exception as promote_error:
                print(f"  Automatic recovery failed: {promote_error}")
                print(
                    f"  The verified pre-swap copy still survives under '{temp_name}' -- do NOT "
                    f"delete it. Recover manually by promoting it, or restore the full-directory "
                    f"backup at: {backup_path}"
                )
        sys.exit(1)

    # The bulk delete + re-upsert cycle above leaves the FTS5 inverted index
    # inconsistent, which fails the next repair's integrity preflight (#1747).
    _post_rebuild_cleanup(palace_path, backend=backend, progress=print)

    print(f"\n  Repair complete. {filed} drawers rebuilt.")
    print(f"  Backup saved at {backup_path}")
    print(f"\n{'=' * 55}\n")
