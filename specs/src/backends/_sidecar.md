# Behavior Spec: Embedder-Identity Sidecar (`mempalace/backends/_sidecar.py`)

## Purpose

This module manages a shared on-disk record of embedder identity per collection, called the "embedder sidecar". The sidecar is a small JSON file in the palace directory, keyed by collection name, recording which embedder produced a collection's vectors (`model_name` and `dimension`) (mempalace/backends/_sidecar.py:L1-L10).

The sidecar is deliberately separate from a backend's mismatch marker. A marker's presence signals "palace initialized"; recording identity on first empty open must therefore not create a marker. The sidecar is unguarded, so a brand-new palace can record identity immediately (mempalace/backends/_sidecar.py:L3-L9).

## On-Disk Contract

The canonical filename for the sidecar is `mempalace_embedder.json` (mempalace/backends/_sidecar.py:L16-L16). Note: callers supply the full `path` to the file; this module does not itself join the filename to a directory, but the constant defines the expected name.

The file format is a JSON object (dictionary) whose keys are collection names and whose values are per-collection entry objects. Each entry object has two fields: `model_name` (string) and `dimension` (integer) (mempalace/backends/_sidecar.py:L62-L65). The file is written with 2-space indentation and non-ASCII characters preserved literally (not escaped) (mempalace/backends/_sidecar.py:L68-L68).

After every successful write the file's permissions are set to owner-read/write only (octal `0600`) (mempalace/backends/_sidecar.py:L69-L69).

## Data Model: EmbedderIdentity

The returned/consumed identity value has two attributes: `model_name` (string, the stable embedder identity) and `dimension` (integer, the vector width; `0` denotes unknown/not-probed) (mempalace/backends/base.py:L138-L146). This module constructs and consumes that value type (mempalace/backends/_sidecar.py:L25-L25, L39-L42).

## Public Surface

### `read_embedder_sidecar(path, collection_name) -> EmbedderIdentity | None`

Inputs: `path` (optional string path to the sidecar file), `collection_name` (optional string). Returns the recorded `EmbedderIdentity` for the given collection, or `None` (mempalace/backends/_sidecar.py:L19-L24).

Returns `None` (degrades to the "unknown" state, never raises) in all of the following cases:
- `path` is empty/missing, or `collection_name` is empty/missing, or the path does not point to an existing regular file (mempalace/backends/_sidecar.py:L27-L28).
- The file cannot be read, or its contents are not valid JSON (mempalace/backends/_sidecar.py:L29-L33).
- The parsed top-level JSON value is not an object/dictionary (mempalace/backends/_sidecar.py:L34-L35).
- There is no entry for `collection_name`, or the entry is not an object, or the entry has no truthy `model_name` field (mempalace/backends/_sidecar.py:L36-L38).

On success it returns an identity whose `model_name` is the entry's `model_name` coerced to string, and whose `dimension` is the entry's `dimension` coerced to integer, defaulting to `0` when the field is missing or falsy (mempalace/backends/_sidecar.py:L39-L42). The file is opened/read as UTF-8 (mempalace/backends/_sidecar.py:L30-L30).

This function is read-only: it has no filesystem side effects beyond reading.

### `write_embedder_sidecar(path, collection_name, identity) -> None`

Inputs: `path` (optional string path to the sidecar file), `collection_name` (optional string), `identity` (an object exposing `model_name` and `dimension`). Returns nothing (mempalace/backends/_sidecar.py:L45-L50).

The function is a no-op (returns without touching the filesystem) when any of the following hold: `path` is empty/missing, `collection_name` is empty/missing, `identity` is absent/falsy, or `identity` has no truthy `model_name` (mempalace/backends/_sidecar.py:L51-L52).

Otherwise it performs a read-modify-write that preserves other collections' entries: it starts from an empty object, and if the target file already exists it loads the existing JSON; the loaded value is adopted only if it is an object/dictionary, otherwise the starting state remains an empty object. If the existing file is unreadable or contains invalid JSON, it is treated as an empty object (existing content is effectively discarded for the rewrite) (mempalace/backends/_sidecar.py:L53-L61).

It then sets (creating or overwriting) the entry for `collection_name` to `{ "model_name": <identity.model_name as string>, "dimension": <identity.dimension as integer, or 0 when falsy> }` (mempalace/backends/_sidecar.py:L62-L65).

The merged object is written back to `path` (UTF-8, 2-space indent, non-ASCII preserved), creating the file if it did not exist, and the file permissions are then set to `0600` (mempalace/backends/_sidecar.py:L66-L69).

The write path never raises on I/O failure or on an unsupported permission-change operation: both are silently swallowed (mempalace/backends/_sidecar.py:L66-L71).

## Invariants and Guarantees

- Identity recording never creates a backend mismatch marker; the sidecar is the only artifact this module writes (mempalace/backends/_sidecar.py:L3-L9, L66-L69).
- Writes are additive per collection: writing one collection's identity preserves all other collections' entries present in a valid existing file (mempalace/backends/_sidecar.py:L53-L65).
- Both read and write degrade gracefully and never raise on missing/malformed input or I/O errors; the read path returns `None` and the write path returns silently (mempalace/backends/_sidecar.py:L27-L38, L66-L71).
- A `dimension` of `0` is the stored representation of "unknown / not probed" (mempalace/backends/_sidecar.py:L41-L42, L64-L64; mempalace/backends/base.py:L143-L145).
