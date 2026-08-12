#!/usr/bin/env python3
"""Repair legacy Windows mojibake in a MemPalace collection."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from mempalace.config import MempalaceConfig
from mempalace.encoding_repair import (
    repair_collection,
    restore_collection,
)
from mempalace.palace import (
    get_collection,
    mine_palace_lock,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Conservatively repair high-confidence UTF-8 mojibake "
            "in legacy MemPalace drawers. Dry-run is the default."
        )
    )
    parser.add_argument(
        "--palace",
        help=("Palace path; defaults to the configured palace."),
    )
    parser.add_argument(
        "--collection",
        help=("Collection name; defaults to the configured collection."),
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=500,
        help=("Rows scanned or restored per page (default: 500)."),
    )
    parser.add_argument(
        "--preview-chars",
        type=int,
        default=180,
        help=("Maximum characters shown in each before/after preview."),
    )

    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Write repairs after creating a private JSONL backup. "
            "Without this flag, the command is read-only."
        ),
    )
    action.add_argument(
        "--restore-backup",
        metavar="PATH",
        help=("Restore original documents from a prior repair backup."),
    )

    parser.add_argument(
        "--backup",
        metavar="PATH",
        help=(
            "Backup destination used with --apply. Defaults to a "
            "timestamped file beside the palace. Existing files "
            "are never overwritten."
        ),
    )
    return parser


def _default_backup_path(
    palace_path: str,
) -> Path:
    palace = Path(palace_path).expanduser().resolve()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    return palace.parent / (f"{palace.name}.encoding-repair-{timestamp}.jsonl")


def _preview(
    text: str,
    limit: int,
) -> str:
    compact = text.replace(
        "\r",
        "\\r",
    ).replace(
        "\n",
        "\\n",
    )

    if len(compact) <= limit:
        return compact

    return (
        compact[
            : max(
                0,
                limit - 1,
            )
        ]
        + "…"
    )


def _print_change(
    drawer_id: str,
    before: str,
    after: str,
    *,
    preview_chars: int,
) -> None:
    print()
    print(f"Drawer: {drawer_id}")
    print(
        "  before: "
        + _preview(
            before,
            preview_chars,
        )
    )
    print(
        "  after:  "
        + _preview(
            after,
            preview_chars,
        )
    )


def _reconfigure_stdio_utf8_on_windows() -> None:
    """Decode stdio as UTF-8 on Windows for the encoding-repair CLI.

    Thin wrapper around the shared helper in ``mempalace._stdio``, matching
    ``cli.py`` and ``fact_checker.py``. stdout/stderr override to ``replace``
    because every proposed change prints a before/after preview of verbatim
    drawer text -- under the legacy console codepage this tool is written for,
    ``strict`` raises on the mojibake lead bytes themselves and aborts the run
    before a single drawer is repaired.
    """
    from mempalace._stdio import reconfigure_stdio_utf8_on_windows

    reconfigure_stdio_utf8_on_windows(stdout_errors="replace", stderr_errors="replace")


def main() -> int:
    _reconfigure_stdio_utf8_on_windows()

    parser = build_parser()
    args = parser.parse_args()

    if args.page_size < 1:
        parser.error("--page-size must be at least 1")

    if args.preview_chars < 40:
        parser.error("--preview-chars must be at least 40")

    if args.backup and not args.apply:
        parser.error("--backup requires --apply")

    config = MempalaceConfig()
    palace_path = args.palace or config.palace_path
    collection_name = args.collection or getattr(
        config,
        "collection_name",
        "mempalace_drawers",
    )

    if args.restore_backup:
        with mine_palace_lock(palace_path):
            collection = get_collection(
                palace_path,
                collection_name=(collection_name),
                create=False,
            )
            report = restore_collection(
                collection,
                args.restore_backup,
                batch_size=(args.page_size),
            )

        print("Mode: RESTORE")
        print(f"Backup records validated: {report['validated']}")
        print(f"Documents restored: {report['restored']}")
        return 0

    backup_path = None

    if args.apply:
        backup_path = Path(args.backup) if args.backup else _default_backup_path(palace_path)

    def show_change(
        drawer_id: str,
        before: str,
        after: str,
    ) -> None:
        _print_change(
            drawer_id,
            before,
            after,
            preview_chars=(args.preview_chars),
        )

    try:
        if args.apply:
            with mine_palace_lock(palace_path):
                collection = get_collection(
                    palace_path,
                    collection_name=(collection_name),
                    create=False,
                )
                report = repair_collection(
                    collection,
                    apply=True,
                    page_size=(args.page_size),
                    backup_path=(backup_path),
                    on_change=show_change,
                )
        else:
            collection = get_collection(
                palace_path,
                collection_name=(collection_name),
                create=False,
            )
            report = repair_collection(
                collection,
                apply=False,
                page_size=(args.page_size),
                on_change=show_change,
            )
    except FileExistsError as exc:
        print(
            f"ERROR: backup file already exists; refusing to overwrite it: {exc.filename}",
            file=sys.stderr,
        )
        return 2

    print()
    print("Mode: " + ("APPLY" if args.apply else "DRY RUN"))
    print(f"Rows scanned: {report['scanned']}")
    print(f"Documents needing repair: {report['changed']}")
    print(f"Documents updated: {report['updated']}")

    if report["backup_path"]:
        print(f"Original-document backup: {report['backup_path']}")

    if not args.apply and report["changed"]:
        print()
        print(
            "Review every change above, then run again with "
            "--apply. A private, non-overwriting backup will "
            "be written before any document is updated."
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
