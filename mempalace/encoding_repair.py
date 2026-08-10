"""Safely repair high-confidence UTF-8 mojibake in MemPalace drawers."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Callable, Iterator, Optional, TextIO, Union

_BACKUP_FORMAT = "mempalace-encoding-repair"
_BACKUP_VERSION = 1
_UNDEFINED_CP1252_BYTES = frozenset(
    {
        0x81,
        0x8D,
        0x8F,
        0x90,
        0x9D,
    }
)


def _cp1252_character(
    byte_value: int,
) -> str:
    """Map a legacy byte to the character stored in old palaces."""
    if byte_value in _UNDEFINED_CP1252_BYTES:
        # Pre-3.1 Windows reads could preserve these five undefined
        # Windows-1252 byte values as their corresponding invisible
        # C1 control code points.
        return chr(byte_value)

    return bytes([byte_value]).decode("cp1252")


def _encode_mojibake_candidate(
    text: str,
) -> bytes:
    """Recover original bytes, including undefined CP1252 values."""
    raw = bytearray()

    for character in text:
        codepoint = ord(character)

        if codepoint in _UNDEFINED_CP1252_BYTES:
            raw.append(codepoint)
        else:
            raw.extend(character.encode("cp1252"))

    return bytes(raw)


_CONTINUATION_CHARS = "".join(
    _cp1252_character(byte_value)
    for byte_value in range(
        0x80,
        0xC0,
    )
)
_CONTINUATION_CLASS = re.escape(_CONTINUATION_CHARS)

# These are the characteristic visible lead characters produced by
# common UTF-8-as-Windows-1252 corruption:
#
# C2/C3 -> Â/Ã
# E2    -> â
# F0    -> ð
# EF    -> ï
#
# C4/C5 -> Ä/Å are deliberately excluded because strings such as Å²
# can be legitimate scientific text.
_HIGH_CONFIDENCE_RUN = re.compile(
    rf"(?:"
    rf"[ÂÃ][{_CONTINUATION_CLASS}]"
    rf"|â[{_CONTINUATION_CLASS}]{{2}}"
    rf"|ð[{_CONTINUATION_CLASS}]{{3}}"
    rf"|ï[{_CONTINUATION_CLASS}]{{2}}"
    rf")+"
)


def _decode_high_confidence_run(
    match: re.Match,
) -> str:
    candidate = match.group(0)

    try:
        return _encode_mojibake_candidate(candidate).decode("utf-8")
    except (
        UnicodeEncodeError,
        UnicodeDecodeError,
    ):
        return candidate


def repair_mojibake_once(text: str) -> str:
    """Repair one layer of high-confidence UTF-8-as-CP1252 mojibake."""
    return _HIGH_CONFIDENCE_RUN.sub(
        _decode_high_confidence_run,
        text,
    )


def repair_mojibake(
    text: str,
    *,
    max_passes: int = 3,
) -> str:
    """Repair repeated high-confidence mojibake layers until stable."""
    if max_passes < 1:
        raise ValueError("max_passes must be at least 1")

    current = text

    for _ in range(max_passes):
        repaired = repair_mojibake_once(current)

        if repaired == current:
            break

        current = repaired

    return current


def _result_field(result, name: str):
    if isinstance(result, dict):
        return result.get(name)

    return getattr(result, name, None)


def _collection_name(
    collection,
) -> Optional[str]:
    """Resolve a collection name through MemPalace backend wrappers."""

    def resolve_name(candidate) -> Optional[str]:
        name = getattr(
            candidate,
            "name",
            None,
        )

        if callable(name):
            try:
                name = name()
            except TypeError:
                name = None

        return str(name) if name else None

    direct_name = resolve_name(collection)
    if direct_name:
        return direct_name

    # ChromaCollection already provides this resolver for its wrapped
    # chromadb collection.
    resolver = getattr(
        collection,
        "_collection_name",
        None,
    )

    if callable(resolver):
        try:
            resolved = resolver()
        except (
            AttributeError,
            TypeError,
        ):
            resolved = None

        if resolved:
            return str(resolved)

    # Defensive fallback for thin wrappers that expose only their inner
    # collection object.
    inner = getattr(
        collection,
        "_collection",
        None,
    )

    if inner is not None and inner is not collection:
        return resolve_name(inner)

    return None


def _open_private_backup(
    path: Path,
) -> TextIO:
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    descriptor = os.open(
        str(path),
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )

    try:
        try:
            os.chmod(path, 0o600)
        except (
            OSError,
            NotImplementedError,
        ):
            pass

        return os.fdopen(
            descriptor,
            "w",
            encoding="utf-8",
            newline="\n",
        )
    except Exception:
        os.close(descriptor)
        raise


def _write_json_line(
    handle: TextIO,
    value: dict,
) -> None:
    handle.write(
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
        )
        + "\n"
    )


def _write_backup_header(
    handle: TextIO,
    collection,
) -> None:
    _write_json_line(
        handle,
        {
            "format": _BACKUP_FORMAT,
            "version": _BACKUP_VERSION,
            "collection": _collection_name(collection),
        },
    )


def _read_backup_header(
    path: Path,
) -> dict:
    try:
        with path.open(
            "r",
            encoding="utf-8",
        ) as handle:
            header = json.loads(handle.readline())
    except OSError as exc:
        raise ValueError(f"could not read repair backup: {path}") from exc
    except (
        json.JSONDecodeError,
        TypeError,
    ) as exc:
        raise ValueError("repair backup has an invalid header") from exc

    if not isinstance(
        header,
        dict,
    ) or (header.get("format") != _BACKUP_FORMAT or header.get("version") != _BACKUP_VERSION):
        raise ValueError("unsupported repair backup format")

    return header


def _iter_backup_records(
    path: Path,
) -> Iterator[tuple[str, str]]:
    _read_backup_header(path)

    with path.open(
        "r",
        encoding="utf-8",
    ) as handle:
        # Skip the validated header.
        handle.readline()

        for line_number, line in enumerate(
            handle,
            start=2,
        ):
            if not line.strip():
                continue

            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid backup JSON at line {line_number}") from exc

            drawer_id = record.get("id") if isinstance(record, dict) else None
            document = record.get("original_document") if isinstance(record, dict) else None

            if not isinstance(
                drawer_id,
                str,
            ) or not isinstance(
                document,
                str,
            ):
                raise ValueError(f"invalid repair backup record at line {line_number}")

            yield drawer_id, document


def repair_collection(
    collection,
    *,
    apply: bool = False,
    page_size: int = 500,
    backup_path: Optional[Union[str, Path]] = None,
    on_change: Optional[Callable[[str, str, str], None]] = None,
) -> dict:
    """Scan a collection and optionally repair high-confidence mojibake."""
    if page_size < 1:
        raise ValueError("page_size must be at least 1")

    if apply and backup_path is None:
        raise ValueError("backup_path is required when apply=True")

    backup = Path(backup_path) if backup_path is not None else None
    backup_handle: Optional[TextIO] = None
    backup_used: Optional[str] = None

    scanned = 0
    changed = 0
    updated = 0
    offset = 0

    try:
        while True:
            page = collection.get(
                limit=page_size,
                offset=offset,
                include=["documents"],
            )
            ids = list(
                _result_field(
                    page,
                    "ids",
                )
                or []
            )
            documents = list(
                _result_field(
                    page,
                    "documents",
                )
                or []
            )

            if len(ids) != len(documents):
                raise RuntimeError("collection returned misaligned ids and documents")

            if not ids:
                break

            page_changes = []

            for drawer_id, document in zip(
                ids,
                documents,
            ):
                scanned += 1

                if not isinstance(
                    document,
                    str,
                ):
                    continue

                repaired = repair_mojibake(document)

                if repaired == document:
                    continue

                item = (
                    str(drawer_id),
                    document,
                    repaired,
                )
                page_changes.append(item)
                changed += 1

                if on_change is not None:
                    on_change(*item)

            if apply and page_changes:
                if backup_handle is None:
                    assert backup is not None

                    backup_handle = _open_private_backup(backup)
                    _write_backup_header(
                        backup_handle,
                        collection,
                    )
                    backup_used = str(backup)

                for (
                    drawer_id,
                    original,
                    _repaired,
                ) in page_changes:
                    _write_json_line(
                        backup_handle,
                        {
                            "id": drawer_id,
                            "original_document": (original),
                        },
                    )

                # Originals must reach durable storage before their
                # live collection rows are overwritten.
                backup_handle.flush()
                os.fsync(backup_handle.fileno())

                collection.update(
                    ids=[item[0] for item in page_changes],
                    documents=[item[2] for item in page_changes],
                )
                updated += len(page_changes)

            offset += len(ids)

            if len(ids) < page_size:
                break
    finally:
        if backup_handle is not None:
            backup_handle.close()

    return {
        "scanned": scanned,
        "changed": changed,
        "updated": updated,
        "backup_path": backup_used,
    }


def restore_collection(
    collection,
    backup_path: Union[str, Path],
    *,
    batch_size: int = 500,
) -> dict:
    """Restore original documents from an encoding-repair backup."""
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")

    path = Path(backup_path)
    header = _read_backup_header(path)

    backup_collection = header.get("collection")
    target_collection = _collection_name(collection)

    if backup_collection and target_collection and backup_collection != target_collection:
        raise ValueError(
            f"backup belongs to collection {backup_collection!r}, not {target_collection!r}"
        )

    # Validate the complete file before performing the first restore write.
    validated = sum(1 for _record in _iter_backup_records(path))

    restored = 0
    batch_ids = []
    batch_documents = []

    for (
        drawer_id,
        document,
    ) in _iter_backup_records(path):
        batch_ids.append(drawer_id)
        batch_documents.append(document)

        if len(batch_ids) < batch_size:
            continue

        collection.update(
            ids=batch_ids,
            documents=batch_documents,
        )
        restored += len(batch_ids)
        batch_ids = []
        batch_documents = []

    if batch_ids:
        collection.update(
            ids=batch_ids,
            documents=batch_documents,
        )
        restored += len(batch_ids)

    return {
        "validated": validated,
        "restored": restored,
    }
