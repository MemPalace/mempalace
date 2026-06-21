# Behavior Spec: Qdrant REST Backend

Source: `mempalace/backends/qdrant.py`

## Purpose & Scope

This is an opt-in external-service storage backend for MemPalace that talks to a Qdrant vector database over its REST/HTTP API. It only runs when the user explicitly selects `qdrant` via config, env, or CLI/MCP flag; Chroma is the default. Embeddings are produced locally and only the resulting vectors are sent to Qdrant (`mempalace/backends/qdrant.py:L1-L7`). The public surface is the two exported classes `QdrantBackend` and `QdrantCollection` (`mempalace/backends/qdrant.py:L1386-L1386`).

## Constants & Contracts

- Default Qdrant base URL is `http://localhost:6333` (`mempalace/backends/qdrant.py:L49-L49`).
- The on-disk marker file is named `qdrant_backend.json` and is stored in the palace local path (`mempalace/backends/qdrant.py:L50-L50`, `mempalace/backends/qdrant.py:L1104-L1106`).
- Qdrant point payloads use three reserved keys: `mempalace_id` (the caller-facing document id), `document` (verbatim text), and `metadata` (a JSON object) (`mempalace/backends/qdrant.py:L51-L53`). Upsert payloads additionally include an `updated_at` ISO-8601 UTC timestamp (`mempalace/backends/qdrant.py:L801-L806`).
- Point ids stored in Qdrant are deterministic UUIDv5 values derived from the namespace UUID `c06c3fc7-5c14-4dc4-84c2-24a5f72d8dc1` and the string document id (`mempalace/backends/qdrant.py:L54-L54`, `mempalace/backends/qdrant.py:L248-L249`). The same input id always maps to the same point id.
- Supported metadata filter operators are exactly: `$eq`, `$ne`, `$in`, `$nin`, `$and`, `$or`, `$contains`, `$gt`, `$gte`, `$lt`, `$lte` (`mempalace/backends/qdrant.py:L56-L58`).

## Configuration Resolution

Configuration is resolved from (in priority order) an explicit options dict, then environment variables, then a `MempalaceConfig` object, then defaults (`mempalace/backends/qdrant.py:L310-L352`). The fields and their env vars are: `url` ← `MEMPALACE_QDRANT_URL` (default `http://localhost:6333`); `api_key` ← `MEMPALACE_QDRANT_API_KEY`; `namespace` ← `MEMPALACE_QDRANT_NAMESPACE`; `timeout` ← `MEMPALACE_QDRANT_TIMEOUT` (default 10.0) (`mempalace/backends/qdrant.py:L319-L340`). The URL has trailing slashes stripped and falls back to the default if empty (`mempalace/backends/qdrant.py:L347-L348`). Timeout is coerced to float; non-numeric or non-positive values become 10.0 (`mempalace/backends/qdrant.py:L341-L346`). Namespace is stripped of whitespace and becomes null if empty (`mempalace/backends/qdrant.py:L351-L351`). If config import fails, resolution still proceeds using env/options/defaults (`mempalace/backends/qdrant.py:L313-L318`).

## HTTP Contract (REST Client)

All requests are JSON over HTTP. The request URL is `{base_url}{path}` optionally with a URL-encoded query string; headers always include `Content-Type: application/json`, and `api-key: {api_key}` when an api key is configured; the request body, when present, is UTF-8 JSON (`mempalace/backends/qdrant.py:L366-L376`). Requests use the configured timeout (`mempalace/backends/qdrant.py:L378-L378`).

Error handling: an HTTP error response raises `_QdrantHTTPError` carrying the numeric status code and a decoded detail string, with message format `Qdrant HTTP {status}: {detail}` (`mempalace/backends/qdrant.py:L296-L300`, `mempalace/backends/qdrant.py:L380-L383`). A connection/URL-level failure raises `BackendError` with message `Qdrant request failed: {reason}` (`mempalace/backends/qdrant.py:L384-L385`). An empty response body returns an empty object; a non-empty body that is not valid JSON raises `BackendError("Qdrant returned invalid JSON")` (`mempalace/backends/qdrant.py:L386-L391`).

REST operations map to Qdrant endpoints:
- `collection_exists`: `GET /collections/{name}`; a 404 returns false, other errors propagate (`mempalace/backends/qdrant.py:L393-L400`).
- `create_collection`: `PUT /collections/{name}` with body `{"vectors": {"size": dimension, "distance": "Cosine"}}` — collections are created with cosine distance (`mempalace/backends/qdrant.py:L405-L410`).
- `create_payload_index`: `PUT /collections/{name}/index?wait=true`; status 400 or 409 is treated as success/skip, other errors propagate (`mempalace/backends/qdrant.py:L412-L424`).
- `upsert_points`: `PUT /collections/{name}/points?wait=true` with `{"points": [...]}` (`mempalace/backends/qdrant.py:L426-L432`).
- `query_points`: first tries `POST /collections/{name}/points/query` with body keyed `query`; on status 404 or 405 it retries `POST /collections/{name}/points/search` with body keyed `vector`. Both request `with_payload=true` and conditional `with_vector`. The result list is read from `result` (if a list) or `result.points` (`mempalace/backends/qdrant.py:L434-L476`).
- `scroll_points`: `POST /collections/{name}/points/scroll` returning a tuple of (points, next_page_offset) (`mempalace/backends/qdrant.py:L478-L502`).
- `delete_points`: `POST /collections/{name}/points/delete?wait=true` with either a `{"points": [...]}` selector or a `{"filter": ...}` selector (`mempalace/backends/qdrant.py:L504-L521`).
- `count_points`: `POST /collections/{name}/points/count` with `{"exact": true}`, returns the integer `result.count` (`mempalace/backends/qdrant.py:L523-L530`).
- `delete_collection`: `DELETE /collections/{name}` (`mempalace/backends/qdrant.py:L532-L533`).

## Remote Collection Naming & Isolation

Remote Qdrant collection names are derived deterministically. The prefix is `mempalace`, optionally followed by a slugged namespace, followed by a 16-hex-char SHA-256 hash of the palace id, joined by `_` (`mempalace/backends/qdrant.py:L1108-L1117`). The full remote collection name appends `_{slug(collection_name)}` to that prefix (`mempalace/backends/qdrant.py:L1214-L1228`). A `palace.namespace` overrides the config namespace when building the remote name (`mempalace/backends/qdrant.py:L1221-L1226`). Slugging replaces any run of non-`[A-Za-z0-9_-]` characters with `_`, strips leading/trailing underscores, falls back to a default when empty, and when longer than 64 chars truncates to 51 chars plus `_` plus a 12-hex-char SHA-256 digest of the original (`mempalace/backends/qdrant.py:L252-L258`).

## Marker File (Mismatch Protection)

The marker file `qdrant_backend.json` is written into the palace local path with permissions `0o600` and the parent directory chmod'd to `0o700` when possible (`mempalace/backends/qdrant.py:L1164-L1185`). Its JSON shape is: `backend` = `"qdrant"`, `schema_version` = `1`, `created_at` = ISO-8601 UTC timestamp, `palace_id`, and a `qdrant` object containing `url`, `namespace`, `palace_hash` (16-hex SHA-256 of palace id), and `remote_prefix` (`mempalace/backends/qdrant.py:L1119-L1125`, `mempalace/backends/qdrant.py:L1172-L1181`). When `palace.local_path` is falsy, no marker is written (`mempalace/backends/qdrant.py:L1165-L1166`).

The marker presence is the signal that a palace is initialized. Marker existence is checked by `os.path.isfile` on the marker path (`mempalace/backends/qdrant.py:L1127-L1128`). Reading an unreadable or invalid-JSON marker raises `BackendMismatchError("qdrant marker is unreadable: {path}")` (`mempalace/backends/qdrant.py:L1130-L1141`). Validation requires `backend == "qdrant"` else `BackendMismatchError`; requires a `qdrant` dict else `BackendMismatchError`; and compares each expected target field (`url`, `namespace`, `palace_hash`, `remote_prefix`) against the stored value, raising `BackendMismatchError` listing mismatched keys if any differ — directing the user to keep `MEMPALACE_QDRANT_URL` and namespace consistent or use a fresh palace (`mempalace/backends/qdrant.py:L1143-L1162`).

## Embedder Identity Sidecar

Embedder identity is stored in a separate sidecar file (filename `EMBEDDER_SIDECAR_FILENAME`) in the palace local path, NOT in the marker — this lets a brand-new palace record identity before the marker (which signals "initialized") exists (`mempalace/backends/qdrant.py:L1187-L1202`, `mempalace/backends/qdrant.py:L665-L671`). `get_stored_embedder_identity` reads it and `set_embedder_identity` writes it, both keyed by collection name (`mempalace/backends/qdrant.py:L665-L671`, `mempalace/backends/qdrant.py:L1198-L1202`). When there is no local path, the sidecar path is null (`mempalace/backends/qdrant.py:L1193-L1196`).

## Vector Handling & Dimensions

Each embedding must be a non-empty 1-D vector; otherwise `ValueError("embedding must be a non-empty 1D vector")` is raised (`mempalace/backends/qdrant.py:L221-L225`). A write batch's embeddings must all share the same dimension, else `DimensionMismatchError` listing the mixed sizes (`mempalace/backends/qdrant.py:L228-L237`). A remote collection is created lazily on first upsert with the discovered dimension; the dimension must be positive (`mempalace/backends/qdrant.py:L691-L716`). If the remote collection already exists with a different vector size than the incoming batch, `DimensionMismatchError` is raised naming both expected and actual (`mempalace/backends/qdrant.py:L710-L715`). The known dimension is cached per collection and re-checked on subsequent operations (`mempalace/backends/qdrant.py:L696-L702`, `mempalace/backends/qdrant.py:L941-L947`).

## Filter Translation

Filters are validated before use: any `$`-prefixed key not in the supported operator set raises `UnsupportedFilterError("operator {key} not supported by qdrant")`, walking the whole nested structure (`mempalace/backends/qdrant.py:L107-L122`). Filters that cannot be pushed to Qdrant — those containing `$or`, `$contains`, or any `where_document` — are flagged as requiring local (in-process) filtering (`mempalace/backends/qdrant.py:L566-L585`). Server-pushable filters translate `where` into Qdrant `must`/`must_not` conditions keyed on `metadata.{field}`: `$eq`→match value, `$ne`→must_not match, `$in`→match any, `$nin`→must_not match any, `$gt/$gte/$lt/$lte`→range (`mempalace/backends/qdrant.py:L536-L563`). A top-level `$and` recurses; a top-level `$or` or unrecognized `$`-key causes the entire filter to be dropped (returns null, forcing full scan) (`mempalace/backends/qdrant.py:L588-L615`).

Local filter evaluation: `_matches_where` evaluates `$and` (all), `$or` (any), and per-field comparison; bool values are coerced to int for comparison; ordering comparisons that raise type errors evaluate false; `$contains` checks substring containment after string coercion (`mempalace/backends/qdrant.py:L124-L180`). `_matches_where_document` supports `$contains` (substring of the document), `$and`, `$or`; any other operator raises `UnsupportedFilterError` (`mempalace/backends/qdrant.py:L183-L202`).

## QdrantCollection — Write Operations

`add(documents, ids, metadatas?, embeddings?)`: validates batch lengths (documents/metadatas/embeddings must each match ids length, else `ValueError`) (`mempalace/backends/qdrant.py:L205-L218`, `mempalace/backends/qdrant.py:L767-L773`); requires explicit embeddings else `ValueError("qdrant requires explicit embeddings")` (`mempalace/backends/qdrant.py:L774-L775`); requires unique ids else `ValueError("add ids must be unique")` (`mempalace/backends/qdrant.py:L776-L777`); requires that none of the ids already exist, else `ValueError` listing the colliding ids (`mempalace/backends/qdrant.py:L778-L780`); then upserts (`mempalace/backends/qdrant.py:L781-L781`).

`upsert(documents, ids, metadatas?, embeddings?)`: same length validation and explicit-embeddings requirement (`mempalace/backends/qdrant.py:L783-L791`); normalizes vectors, ensures the remote collection exists at the right dimension (`mempalace/backends/qdrant.py:L792-L793`); missing metadata defaults to empty objects (`mempalace/backends/qdrant.py:L794-L794`); builds one point per row with the deterministic point id, vector, and payload (`mempalace_id`, `document`, `metadata`, `updated_at`) and upserts them with `wait=true` (`mempalace/backends/qdrant.py:L795-L809`). After a successful upsert it writes the marker file (palace initialization side effect) (`mempalace/backends/qdrant.py:L810-L810`). Metadata is round-tripped through JSON; non-JSON-serializable metadata silently becomes an empty object (`mempalace/backends/qdrant.py:L240-L245`).

`update(ids, documents?, metadatas?, embeddings?)`: requires at least one of documents/metadatas/embeddings else `ValueError` (`mempalace/backends/qdrant.py:L812-L814`); each provided list must match ids length else `ValueError` (`mempalace/backends/qdrant.py:L815-L822`); fetches existing rows, and for each id that exists, merges — documents/embeddings replaced when provided else kept; metadata is a shallow merge of previous metadata updated with provided keys (`mempalace/backends/qdrant.py:L823-L843`). Ids not currently present are silently skipped (`mempalace/backends/qdrant.py:L833-L835`). The merged rows are upserted (`mempalace/backends/qdrant.py:L844-L850`).

## QdrantCollection — Read Operations

`get(ids?, where?, where_document?, limit?, offset?, include?)`: returns a `GetResult` with `ids`, and `documents`/`metadatas`/`embeddings` populated only when requested via `include` (`mempalace/backends/qdrant.py:L972-L1001`). When `ids` is given, results are returned in the requested id order, omitting absent ids (`mempalace/backends/qdrant.py:L989-L991`). `offset` then `limit` are applied as a slice in that order (`mempalace/backends/qdrant.py:L992-L995`). Rows are fetched by scrolling the remote collection (page size 256) with server filters where possible and local filters applied afterward (`mempalace/backends/qdrant.py:L718-L765`).

`query(query_embeddings, n_results=10, where?, where_document?, include?)`: rejects `query_texts` with `ValueError` (callers must pass embeddings) (`mempalace/backends/qdrant.py:L898-L909`); requires non-null, non-empty `query_embeddings` else `ValueError` (`mempalace/backends/qdrant.py:L910-L913`). When the filter requires local evaluation, it falls back to an exact local cosine-distance query: scroll all rows with vectors, locally filter, compute distance per query vector, sort ascending by distance, and take top `n_results` (`mempalace/backends/qdrant.py:L916-L923`, `mempalace/backends/qdrant.py:L852-L896`). Otherwise it pushes the query to Qdrant per query vector, enforcing the dimension check, and returns server-ranked results (`mempalace/backends/qdrant.py:L932-L970`). Distances are derived as `1 - clamp(score, -1, 1)` from Qdrant scores or `1 - clamp(cosine, -1, 1)` locally; cosine of zero-norm vectors yields distance 1.0 (`mempalace/backends/qdrant.py:L278-L293`). The result is a `QueryResult` with nested per-query lists; documents/metadatas/distances/embeddings are populated per the include spec (distances default on) (`mempalace/backends/qdrant.py:L890-L896`, `mempalace/backends/qdrant.py:L964-L970`).

`count()`: returns the exact remote point count (`mempalace/backends/qdrant.py:L1026-L1032`).

`lexical_search(query, n_results=10, where?)`: validates `where`; attempts a server-side `text_any` token filter on the `document` field, falling back to a full scroll if that filter errors; then computes BM25 scores (k1=1.5, b=0.75) over the documents, keeps only hits with score > 0, sorts descending by score, and returns the top `n_results` as a `LexicalResult` (`mempalace/backends/qdrant.py:L1034-L1067`, `mempalace/backends/qdrant.py:L71-L104`, `mempalace/backends/qdrant.py:L627-L631`). Tokenization lowercases and matches Unicode word runs of length ≥ 2 (`mempalace/backends/qdrant.py:L55-L55`, `mempalace/backends/qdrant.py:L65-L68`).

## QdrantCollection — Delete

`delete(ids?, where?)`: validates `where` (`mempalace/backends/qdrant.py:L1003-L1004`). If ids only, deletes by deterministic point ids (`mempalace/backends/qdrant.py:L1009-L1014`). If `where` only and it is server-pushable, deletes by filter (`mempalace/backends/qdrant.py:L1015-L1018`). Otherwise it fetches matching rows locally and deletes by their point ids (`mempalace/backends/qdrant.py:L1019-L1024`).

## Initialization & Not-Found Semantics

For read/query/count/delete: if the remote collection does not exist but the marker exists, `CollectionNotInitializedError(collection_name)` is raised; if neither exists, an empty result (empty rows / empty `QueryResult` / count 0) is returned and delete is a no-op (`mempalace/backends/qdrant.py:L724-L728`, `mempalace/backends/qdrant.py:L924-L930`, `mempalace/backends/qdrant.py:L1005-L1008`, `mempalace/backends/qdrant.py:L1028-L1031`).

## Closing Semantics

A closed collection or closed backend causes operations gated by `_ensure_open` to raise `BackendClosedError("QdrantCollection has been closed")` (`mempalace/backends/qdrant.py:L655-L657`). `close()` on a collection marks it closed (`mempalace/backends/qdrant.py:L1069-L1070`).

## QdrantBackend — Class Surface

`name` is `"qdrant"` and `capabilities` is the fixed set: `requires_explicit_embeddings`, `supports_embeddings_in`, `supports_embeddings_passthrough`, `supports_embeddings_out`, `supports_metadata_filters`, `supports_lexical_search`, `supports_namespace_isolation`, `server_mode` (`mempalace/backends/qdrant.py:L1083-L1096`).

`get_collection(...)` accepts either keyword `palace=PalaceRef, collection_name, create?, options?` or positional `palace_path, collection_name, create?` or keyword `palace_path=...`; a non-`PalaceRef` `palace=` raises `TypeError`, missing `collection_name` raises `TypeError`, and any extra args raise `TypeError` (`mempalace/backends/qdrant.py:L1282-L1325`). It resolves config (with `palace.namespace` overriding config namespace when they differ) (`mempalace/backends/qdrant.py:L1235-L1243`). When a local path is present and the marker file exists, it validates the marker target; when the marker is absent and `create` is false, it raises `PalaceNotFoundError(marker_path)` (`mempalace/backends/qdrant.py:L1245-L1250`). When there is NO local path (pure-remote mode), it raises `BackendError` stating that a local palace path is required to anchor mismatch protection — pure-remote palaces are not supported (`mempalace/backends/qdrant.py:L1251-L1262`). When `create` is false and the remote collection does not exist, it raises `CollectionNotInitializedError(collection_name)` (`mempalace/backends/qdrant.py:L1268-L1269`). On success it constructs a `QdrantCollection` and registers it under the palace id (`mempalace/backends/qdrant.py:L1270-L1280`).

`create_collection(palace_path, collection_name)` and `get_or_create_collection(palace_path, collection_name)` both delegate to `get_collection(..., create=True)` (`mempalace/backends/qdrant.py:L1367-L1371`).

`delete_collection(palace_path, collection_name)`: computes the remote collection name and deletes it from Qdrant only if it exists (`mempalace/backends/qdrant.py:L1373-L1383`).

`close_palace(palace)`: removes and closes all collections registered for that palace id (`mempalace/backends/qdrant.py:L1327-L1332`). `close()`: closes all collections across all palaces, clears clients, and marks the backend closed (`mempalace/backends/qdrant.py:L1334-L1345`). After close, `_client` raises `BackendClosedError("QdrantBackend has been closed")` (`mempalace/backends/qdrant.py:L1204-L1206`).

`health(palace?)`: returns unhealthy if the backend is closed; pings `GET /collections` and returns unhealthy with the exception string on failure; if a palace with a local path is given but its marker file is absent, returns unhealthy "qdrant marker not found"; otherwise healthy (`mempalace/backends/qdrant.py:L1347-L1361`). `QdrantCollection.health()` returns unhealthy if closed, unhealthy if the remote collection is not found (or on error), else healthy (`mempalace/backends/qdrant.py:L1072-L1080`).

`detect(path)`: returns true iff a `qdrant_backend.json` file exists in the given path — this is how the backend type is auto-detected for an existing palace (`mempalace/backends/qdrant.py:L1363-L1365`).

## Concurrency & Side Effects

Both backend and collection use re-entrant locks to guard mutable shared state (client cache, registered collections, known dimension) (`mempalace/backends/qdrant.py:L651-L651`, `mempalace/backends/qdrant.py:L1101-L1101`, `mempalace/backends/qdrant.py:L1207-L1212`, `mempalace/backends/qdrant.py:L694-L694`). REST clients are cached and reused per config (`mempalace/backends/qdrant.py:L1204-L1212`). Observable side effects: network calls to the configured Qdrant URL; reads of environment variables `MEMPALACE_QDRANT_*`; filesystem writes of the marker file (`0o600`) and parent dir chmod (`0o700`) inside the palace local path; and embedder sidecar reads/writes in the palace local path (`mempalace/backends/qdrant.py:L1164-L1185`, `mempalace/backends/qdrant.py:L1192-L1202`, `mempalace/backends/qdrant.py:L319-L340`).
