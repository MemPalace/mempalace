"""NDJSON source adapter (RFC 002) — ingest a JSON-Lines spool of pre-extracted records.

For any pipeline that has already extracted and chunked content elsewhere and wants
to hand MemPalace ready-to-file records: each line of the spool is one record, filed
as one drawer. The adapter is byte-preserving — it stores ``content`` verbatim and
declares no transformations (the foundational promise; see ``CLAUDE.md`` "Verbatim
always"). It carries no opinion about *where* the records came from.

One NDJSON line is one record::

    {"content": "...", "source_file": "...", "metadata": {...}, "chunk_index": 0}

``content`` and ``source_file`` are required strings; ``metadata`` (flat scalars) and
``chunk_index`` (int) are optional. ``metadata`` passes through verbatim — producers
may attach any flat-scalar fields, opaque to this adapter. When ``chunk_index`` is
absent the adapter assigns a running per-``source_file`` ordinal so two records that
share a ``source_file`` never collide on the deterministic drawer id
(``sha256(source_file)_chunk``).

``SourceRef.local_path`` points at the spool file. The optional ``--wing`` routing
flag (``SourceRef.options['wing']``) fills the ``wing`` metadata key only where a
record left it unset (RFC 002 §2.5 — the record's own routing wins).
"""

from __future__ import annotations

import json
import os
from typing import Iterator

from .base import (
    AdapterSchema,
    BaseSourceAdapter,
    DrawerRecord,
    IngestResult,
    SourceAdapterError,
    SourceNotFoundError,
    SourceRef,
)


class NdjsonSourceAdapter(BaseSourceAdapter):
    """File a newline-delimited JSON spool of pre-extracted records into verbatim drawers.

    First-party, in-tree — registered manually (RFC 002 §3.2), not via the
    third-party entry-point group.
    """

    name = "ndjson"
    adapter_version = "0.1.0"
    capabilities = frozenset({"byte_preserving"})
    supported_modes = frozenset({"whole_record"})
    declared_transformations = frozenset()  # verbatim — the producer pre-chunked
    default_privacy_class = "pii_potential"

    def ingest(
        self,
        *,
        source: SourceRef,
        palace: "object",  # PalaceContext — broad to avoid the import cycle
    ) -> Iterator[IngestResult]:
        path = source.local_path
        if not path or not os.path.isfile(path):
            raise SourceNotFoundError(
                f"ndjson: spool file not found: {path!r} (expected a newline-delimited JSON file)"
            )

        # Honor the --wing routing precedence (§2.5): a record's own wing wins;
        # the flag fills in only where the producer left routing unannotated.
        fallback_wing = source.options.get("wing") if source.options else None

        # Per-source_file ordinal so absent chunk_index values never collide on
        # the deterministic drawer id. Provided chunk_index values pass through.
        ordinals: dict[str, int] = {}

        with open(path, encoding="utf-8") as fh:
            for line_no, raw in enumerate(fh, start=1):
                line = raw.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise SourceAdapterError(
                        f"ndjson: malformed JSON at {path}:{line_no}: {exc}"
                    ) from exc
                if not isinstance(record, dict):
                    raise SourceAdapterError(f"ndjson: line {path}:{line_no} is not a JSON object")

                content = record.get("content")
                source_file = record.get("source_file")
                if not isinstance(content, str) or not isinstance(source_file, str):
                    raise SourceAdapterError(
                        f"ndjson: line {path}:{line_no} lacks string 'content' and 'source_file'"
                    )

                metadata = dict(record.get("metadata") or {})
                if fallback_wing and "wing" not in metadata:
                    metadata["wing"] = fallback_wing

                provided = record.get("chunk_index")
                if isinstance(provided, int):
                    chunk_index = provided
                else:
                    chunk_index = ordinals.get(source_file, 0)
                ordinals[source_file] = chunk_index + 1

                yield DrawerRecord(
                    content=content,
                    source_file=source_file,
                    chunk_index=chunk_index,
                    metadata=metadata,
                )

    def describe_schema(self) -> AdapterSchema:
        # No fixed structured schema: metadata is producer-defined and flows
        # through verbatim (upsert_drawer does not reject undeclared fields).
        return AdapterSchema(version="1.0", fields={})
