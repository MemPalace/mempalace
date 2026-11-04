# Wing access control for `mempalace serve`

Opt-in, off by default. With no grants file the server behaves exactly as before: one
bearer token, full access. A grants file gives each token its own set of wings.

This is **not encryption**. It filters what the server returns. Anyone with filesystem
access to the palace, the WAL or the database sees everything (see Boundary).

## Turning it on

```bash
mempalace serve --token "$MEMPALACE_MCP_HTTP_TOKEN" --grants-file /srv/mempalace/grants.yaml
# or: MEMPALACE_MCP_GRANTS_FILE=/srv/mempalace/grants.yaml
```

The file must be mode 0600 (any group or other bit refuses to start; skipped on Windows).
The server also refuses to start with a grants file when:

- the server token is empty, or the server would run in `local_only_tokenless` mode
  (`--allow-insecure` included). The legacy token stays an all-wings admin identity, so
  it has to exist;
- the file sets a reserved key (below);
- the file is empty, unreadable, malformed, or has an unknown key, a duplicate token
  hash or key name, two spellings of one wing, or a hash equal to the server token.

## Grants file

Tokens are stored as sha256 hex, never in plaintext: `printf %s "$TOKEN" | sha256sum`.
Wing names are normalized with the same rule the miners use (`Alpha-Team` is
`alpha_team`), at load and on every request. A sibling spelling such as `alpha-team` next
to `alpha_team` is a different wing and is not granted implicitly.

```yaml
levels: [public, staff, director]      # reserved, see below
default_wing_mode: locked              # the only value enforced today
keys:
  - name: alice                        # unique; shown nowhere to clients
    token_sha256: 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08
    wings: [alpha, beta]               # read and write
    read_wings: [gamma]                # read only (no effect until reads are enabled)
  - name: old-bot
    token_sha256: 978ed3f09b9a46afec3d258976c9611ee08fafcc6d5a33afd4f18211e36f8988
    revoked: true                      # fails exactly like an unknown token
```

The grants file is read once, at startup. Revoking or changing a key by editing the file
needs a server restart; SIGHUP does not reload it.

A key with no wings can use no palace data. A wing that no key lists is invisible to
every key but the server token.

### Reserved keys

Parsed and validated now so the file format does not change later, but **not enforced
yet**. The server refuses to start if they are set, so the file never looks more locked
than it is:

| Key | Meaning when enforced |
|---|---|
| `keys[].clearance` (with top-level `levels`) | highest drawer level this key reads inside a wing |
| `keys[].compartments` | extra named parts of the palace this key may enter |
| `wings.<name>.mode: open` and `default_wing_mode: open` | wings any valid key may read |

`levels` on its own, and `mode: locked`, are accepted.

## What is enforced in this release

- Every request needs a valid token: unknown or revoked is `401` with no detail.
- `/statusz`, `/sync/*`, `/logstream/stream` and `/mcp/light` are admin only (`403` for a
  locked token).
  `/healthz` stays open.
- A locked token sees and may call **three tools**; every other tool answers exactly like
  a tool that does not exist (`-32601 Unknown tool`, before argument validation) and is
  missing from `tools/list`:

  | Tool | Rule |
  |---|---|
  | `mempalace_add_drawer` | `wing` required and in the key's `wings` |
  | `mempalace_diary_write` | `wing` required (the default wing is outside any grant) and in `wings` |
  | `mempalace_get_aaak_spec` | static text, no palace data |

  A wing that is missing, not granted, or does not exist all give the same
  `-32602 Invalid params: wing not available`, decided before the database is touched.
  The wing passed to the handler is the normalized one.

Every tool is classified in `ACL_POLICY` (`mempalace/tool_capabilities.py`); a test fails
when a tool is missing, and a missing tool is denied.

### Classified but denied until later

These are allowed in the final design but cannot be made leak-free by checks at dispatch
alone, so they stay denied (`ACL_PENDING`). Each leaks across wings today, for example by
expanding a wing to sibling spellings, counting every wing, or fetching by id:

- enabled by the wrapper filter (next PR): `list_wings`, `list_rooms`, `search`,
  `get_drawer`, `get_drawers`, `list_drawers`, `diary_read`
- enabled by per-group knowledge graphs (later): `kg_query`, `kg_add`, `kg_invalidate`,
  `kg_supersede`, `kg_timeline`, `kg_stats`
- denied by design for locked tokens: `status`, `get_taxonomy`, graph and tunnel tools,
  `check_duplicate`, `checkpoint`, delete, update, `mine`, `sync`, `reconnect`, hook and
  coordination tools, artifacts

## Trusted doors

Access control is a feature of the HTTP server. **stdio, the light server, the daemon and
hub clients are owner-trusted and run as admin.** A hub client authenticates with the
server token, so a session that goes through a hub is admin and cannot carry a locked
token. Setting `MEMPALACE_MCP_GRANTS_FILE` for a stdio server has no effect. The HTTP
server's `/mcp/light` endpoint resolves wing names before the access check runs, so it is
admin only while a grants file is loaded.

## Boundary

Not encryption. The filter protects what the server returns, not the files: the Chroma
and SQLite stores, `write_log.jsonl` and `drawer_revisions.jsonl` hold every wing in the
clear. A locked token cannot reach them, and anyone who can read the host's files can.
Server errors can carry server-side text such as paths on a failed write. Timing of the
single request lock is not hidden.
