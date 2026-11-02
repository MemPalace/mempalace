# Skills and public decks

Skills are exact UTF-8 Markdown documents in a workspace. Each edit appends a new `.md` file and version record; older versions remain readable. Reads verify the document's SHA-256 hash. Nothing converts a skill into an accepted rule, startup instruction, executable script, or external API request.

Signed-in workspace owners manage skills through REST. Connected agents on any device use the same workspace through MCP with an explicit `skills:read` scope and an allowed tool/profile. This scope grants access to the workspace's skills, independently of memory wing permissions. Existing memory-only connections do not gain skill access. Private skill IDs cannot be read or edited from another workspace.

| Route or tool | Contract |
| --- | --- |
| `POST /api/skills` | Owner creates `{name, markdown, source?}`. Source accepts `url`, `commit`, `license`, and `deck` strings. URLs are provenance only and are never fetched. |
| `PATCH /api/skills/{id}` | Owner appends `{version, name, markdown, source?}`; version must match the current version. |
| `GET /api/skills` / `skills_list` | List latest version metadata. |
| `GET /api/skills/{id}?version=1` / `skills_get` | Read exact Markdown; MCP takes `skill_id` and optional `version`. Text is JSON-quoted data. |
| `GET /api/skills/{id}/history` | Read every version, including exact Markdown. |
| `skills_select` | `{task, limit?}` ranks up to 200 current skills by local sentence-embedding cosine similarity. Returns quoted documents with scores and heuristic safety findings. |
| `POST /api/skill-decks` | Owner publishes `{title, guide, skills: [{skill_id, version}], confirm_public: true}`. Requires explicit confirmation, pins 1–20 exact versions, and returns public preview and guide links. |
| `GET /skills/decks/{id}` | Public full-text preview, provenance, safety findings, install confirmation, and report form. `/guide` explains sharing; `/data` returns the same public snapshot as JSON. |
| `POST /api/skill-decks/{id}/preview` | Signed-in target-workspace owner receives full preview and a 15-minute receipt bound to that person, workspace, deck, and content hash. |
| `POST /api/skill-decks/{id}/install` | Owner submits `{receipt, confirm: true}` to copy precisely the previewed Markdown into that workspace. Retries with the same receipt are idempotent. Agents cannot confirm installation. |
| `POST /api/skill-decks/{id}/report` | A signed-in person submits `{reason}`. The report is stored for the publisher and its reference is returned. |
| `GET /api/skill-decks/{id}/reports` | Publishing workspace owner reads reports. Reporting does not automatically remove a deck. |
| `DELETE /api/skill-decks/{id}` | Publishing workspace owner withdraws the public snapshot and prevents further installs. Existing installed copies stay private in their destination workspaces. |

Public sharing is an explicit owner action. A deck exposes only its pinned documents, provenance, title, and guide; later private edits are not published automatically. Imported text is displayed literally, including HTML comments and instruction-looking text. The heuristic scan flags instruction overrides, hidden markup, network references, credentials/environment reads, shell execution, and obfuscation. It is not a security verdict. No imported text is rendered as HTML or executed. Reports go to the publisher; this is not an independent moderation service.

Selection uses the locally installed Chroma MiniLM ONNX model (`all-MiniLM-L6-v2`) on CPU. Missing model files or runtime errors produce a clear refusal. Selection never downloads a model, calls an external embedding API, or falls back to lexical matching. Provision the model separately before using selection. MiniLM uses a limited token window, and selection sends only the first 8,000 characters of each name/document to the tokenizer. Ranking is a relevance aid; it makes no guarantee of exhaustive recall or multilingual quality. Verbatim storage and reads are unaffected.

Repository/URL fetching, helper-file bundles, on-device filesystem sync, and a session-start skills index are separate work. This API does not install documents into an agent's instruction directories or activate them automatically.
