# ChatGPT Custom GPT Integration

Give a ChatGPT Custom GPT live read and write access to a MemPalace instance running on your machine. The GPT can search your palace, read full drawers, and file new memories — all through a permanent HTTPS URL via a Cloudflare Tunnel.

## What You'll Build

- `palace_api.py` — a FastAPI HTTP gateway wrapping your palace
- A Cloudflare named tunnel — permanent HTTPS URL (e.g. `palace.yourdomain.com`)
- A Custom GPT Action schema — connects ChatGPT to the palace API

## Prerequisites

- MemPalace installed and populated (`mempalace init`, `mempalace mine`)
- Python 3.9+, `fastapi`, `uvicorn`, `chromadb`
- A Cloudflare account (free tier works) with a domain managed by Cloudflare
- `cloudflared` CLI installed (`brew install cloudflare/cloudflare/cloudflared`)
- A ChatGPT Plus/Team account (Custom GPTs require a paid plan)

---

## Step 1 — palace_api.py

Create `palace_api.py` in your project directory. This is the HTTP gateway that sits between ChatGPT and your palace.

```python
#!/usr/bin/env python3
import hashlib, os, sys
from datetime import datetime, timezone
from typing import Optional

sys.path.insert(0, os.path.expanduser("~/Library/Python/3.9/lib/python/site-packages"))

from fastapi import FastAPI, Query, HTTPException, Security
from fastapi.security.api_key import APIKeyHeader
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import chromadb
from mempalace.config import MempalaceConfig

# API key — store in ~/.mempalace/api_key or set PALACE_API_KEY env var
_KEY_FILE = os.path.expanduser("~/.mempalace/api_key")
def _load_key():
    if os.environ.get("PALACE_API_KEY"):
        return os.environ["PALACE_API_KEY"]
    if os.path.exists(_KEY_FILE):
        return open(_KEY_FILE).read().strip()
    raise RuntimeError("No API key found.")

PALACE_API_KEY = _load_key()
_api_key_header = APIKeyHeader(name="X-Palace-Key", auto_error=False)

def require_key(key: str = Security(_api_key_header)):
    if key != PALACE_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

app = FastAPI(title="MemPalace API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://chat.openai.com", "https://chatgpt.com"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

_config = MempalaceConfig()
_client = chromadb.PersistentClient(path=_config.palace_path)
_col = _client.get_collection("mempalace_drawers")

class DrawerIn(BaseModel):
    wing: str
    room: str
    content: str
    source_file: Optional[str] = None
    added_by: str = "ChatGPT"

def _build_where(wing, room):
    if wing and room: return {"$and": [{"wing": wing}, {"room": room}]}
    if wing: return {"wing": wing}
    if room: return {"room": room}
    return None

@app.get("/status")
def status(_ = Security(require_key)):
    all_meta = _col.get(include=["metadatas"])["metadatas"]
    wings, rooms = {}, {}
    for m in all_meta:
        w, r = m.get("wing", "unknown"), m.get("room", "unknown")
        wings[w] = wings.get(w, 0) + 1
        rooms[r] = rooms.get(r, 0) + 1
    return {"total_drawers": len(all_meta), "wings": wings, "rooms": rooms}

@app.get("/search")
def search(
    q: str = Query(...),
    wing: Optional[str] = Query(None),
    room: Optional[str] = Query(None),
    limit: int = Query(5, ge=1, le=20),
    _ = Security(require_key),
):
    where = _build_where(wing, room)
    kwargs = {"query_texts": [q], "n_results": limit,
              "include": ["documents", "metadatas", "distances"]}
    if where: kwargs["where"] = where
    raw = _col.query(**kwargs)
    return {"query": q, "results": [
        {"drawer_id": did, "wing": m.get("wing"), "room": m.get("room"),
         "filed_at": m.get("filed_at", ""), "snippet": doc[:600], "distance": round(d, 4)}
        for did, doc, m, d in zip(
            raw["ids"][0], raw["documents"][0], raw["metadatas"][0], raw["distances"][0])
    ]}

@app.get("/drawer/{drawer_id}")
def get_drawer(drawer_id: str, _ = Security(require_key)):
    result = _col.get(ids=[drawer_id], include=["metadatas", "documents"])
    if not result["documents"]:
        raise HTTPException(status_code=404, detail=f"Drawer not found: {drawer_id}")
    meta = result["metadatas"][0]
    return {"drawer_id": drawer_id, "wing": meta.get("wing"), "room": meta.get("room"),
            "filed_at": meta.get("filed_at"), "content": result["documents"][0]}

@app.post("/drawer")
def add_drawer(body: DrawerIn, _ = Security(require_key)):
    drawer_id = "drawer_" + body.wing + "_" + body.room + "_" + hashlib.md5(body.content.encode()).hexdigest()
    if _col.get(ids=[drawer_id], include=["documents"])["documents"]:
        return {"status": "duplicate", "drawer_id": drawer_id}
    _col.add(ids=[drawer_id], documents=[body.content], metadatas=[{
        "wing": body.wing, "room": body.room, "source_file": body.source_file or "",
        "added_by": body.added_by, "filed_at": datetime.now(timezone.utc).isoformat(),
        "ingest_mode": "api", "normalize_version": 2,
    }])
    return {"status": "filed", "drawer_id": drawer_id}

@app.get("/file")
def file_drawer(
    wing: str = Query(...),
    room: str = Query(...),
    content: str = Query(...),
    source_file: Optional[str] = Query(None),
    added_by: str = Query("ChatGPT"),
    _ = Security(require_key),
):
    """Write via GET — required workaround for Cloudflare blocking POST from OpenAI IPs."""
    drawer_id = "drawer_" + wing + "_" + room + "_" + hashlib.md5(content.encode()).hexdigest()
    if _col.get(ids=[drawer_id], include=["documents"])["documents"]:
        return {"status": "duplicate", "drawer_id": drawer_id}
    _col.add(ids=[drawer_id], documents=[content], metadatas=[{
        "wing": wing, "room": room, "source_file": source_file or "",
        "added_by": added_by, "filed_at": datetime.now(timezone.utc).isoformat(),
        "ingest_mode": "api", "normalize_version": 2,
    }])
    return {"status": "filed", "drawer_id": drawer_id}
```

Generate an API key and save it:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))" > ~/.mempalace/api_key
chmod 600 ~/.mempalace/api_key
```

Start the server:

```bash
python3 -m uvicorn palace_api:app --host 0.0.0.0 --port 8765
```

Test it:

```bash
curl http://localhost:8765/status -H "X-Palace-Key: $(cat ~/.mempalace/api_key)"
```

---

## Step 2 — Cloudflare Named Tunnel

A named tunnel gives ChatGPT a permanent HTTPS URL that survives restarts.

```bash
# Authenticate
cloudflared tunnel login

# Create the tunnel
cloudflared tunnel create my-palace

# Route your domain
cloudflared tunnel route dns my-palace palace.yourdomain.com
```

Create `~/.cloudflared/config.yml`:

```yaml
tunnel: <tunnel-id-from-above>
credentials-file: ~/.cloudflared/<tunnel-id>.json

ingress:
  - hostname: palace.yourdomain.com
    service: http://localhost:8765
  - service: http_status:404
```

Start the tunnel (add to login items or run in background):

```bash
cloudflared tunnel run my-palace
```

Verify:

```bash
curl https://palace.yourdomain.com/status -H "X-Palace-Key: $(cat ~/.mempalace/api_key)"
```

---

## Step 3 — Custom GPT Action Schema

In ChatGPT, create or edit a Custom GPT → **Configure** → **Actions** → **Add action**. Paste this schema:

```json
{
  "openapi": "3.1.0",
  "info": {
    "title": "MemPalace API",
    "version": "1.0.0"
  },
  "servers": [{ "url": "https://palace.yourdomain.com" }],
  "paths": {
    "/status": {
      "get": {
        "operationId": "getStatus",
        "summary": "Palace overview — total drawers by wing and room",
        "responses": { "200": { "description": "Palace status" } }
      }
    },
    "/search": {
      "get": {
        "operationId": "searchPalace",
        "summary": "Semantic search across all palace drawers",
        "parameters": [
          { "name": "q", "in": "query", "required": true, "schema": { "type": "string" } },
          { "name": "wing", "in": "query", "required": false, "schema": { "type": "string" } },
          { "name": "room", "in": "query", "required": false, "schema": { "type": "string" } },
          { "name": "limit", "in": "query", "required": false, "schema": { "type": "integer", "default": 5 } }
        ],
        "responses": { "200": { "description": "Search results" } }
      }
    },
    "/drawer/{drawer_id}": {
      "get": {
        "operationId": "getDrawer",
        "summary": "Read a full drawer by ID",
        "parameters": [
          { "name": "drawer_id", "in": "path", "required": true, "schema": { "type": "string" } }
        ],
        "responses": { "200": { "description": "Full drawer content" } }
      }
    },
    "/file": {
      "get": {
        "operationId": "fileDrawer",
        "summary": "File new content into the palace",
        "description": "Use GET /file (not POST /drawer) to write — Cloudflare blocks POST from OpenAI IPs. See known issues below.",
        "parameters": [
          { "name": "wing", "in": "query", "required": true, "schema": { "type": "string" } },
          { "name": "room", "in": "query", "required": true, "schema": { "type": "string" } },
          { "name": "content", "in": "query", "required": true, "schema": { "type": "string" } },
          { "name": "added_by", "in": "query", "required": false, "schema": { "type": "string", "default": "ChatGPT" } }
        ],
        "responses": { "200": { "description": "Filed or duplicate" } }
      }
    }
  }
}
```

**Auth setup:** In the Action auth section, choose **API Key** → **Custom header** → header name: `X-Palace-Key` → paste your key.

---

## Step 4 — Test the Connection

Ask your GPT:

> "Call getStatus and tell me how many drawers are in the palace."

If it returns a drawer count, the connection is live. Then try:

> "Search the palace for [something you know is in there]."

Then try a write:

> "File this to wing_test, room general: 'Hello from ChatGPT — connection confirmed.'"

---

## Known Issues

### Cloudflare blocks POST requests from OpenAI's IPs

**Symptom:** `getStatus` and `searchPalace` work. `fileDrawer` (or any write) times out or returns a Cloudflare error. Palace API logs show zero POST requests from OpenAI IPs (`57.151.x.x`).

**Why:** Cloudflare's automated security treats OpenAI's infrastructure as bot traffic for POST requests. GET requests pass through because they can be cached at the edge; POSTs must reach the origin server and get challenged.

**Fix:** Use `GET /file` instead of `POST /drawer` for writes. The palace_api.py and schema above already implement this workaround — `fileDrawer` is a GET that accepts content as a query parameter. Cloudflare never blocks GETs from OpenAI IPs.

**Optional belt-and-suspenders:** Lower Cloudflare's Security Level to `essentially_off` via the Cloudflare API. This reduces IP reputation challenges without disabling specific CVE rules.

```bash
curl -X PATCH "https://api.cloudflare.com/client/v4/zones/{zone_id}/settings/security_level" \
  -H "Authorization: Bearer {your_token}" \
  -H "Content-Type: application/json" \
  -d '{"value":"essentially_off"}'
```

The API token needs **Zone Settings: Edit** permission. Your zone ID is visible in the Cloudflare dashboard sidebar.

### Cloudflare free plan WAF Custom Rules are not editable

The toggle appears but cannot be clicked on the free plan. The GET /file workaround makes this a non-issue for writes.

### GPT-5.5 token budgets

If you're using gpt-5.5 (a reasoning model) in the classroom or via API, set `max_completion_tokens` to at least `16000`. Reasoning tokens count against this limit and a low budget will produce empty responses silently.

---

## API Key Security

- The key lives in `~/.mempalace/api_key` (chmod 600). Never commit it.
- Every request requires the `X-Palace-Key` header — without it, the API returns 401.
- If the key crosses through a chat session (e.g. you paste it to debug), rotate it:
  ```bash
  python3 -c "import secrets; print(secrets.token_urlsafe(32))" > ~/.mempalace/api_key
  ```
  Then update your Custom GPT action auth and any env files.

---

## Keeping the Tunnel Running

Add to your shell profile:

```bash
# Start palace API and tunnel (add to ~/.zshrc or login items)
alias start-palace='python3 -m uvicorn palace_api:app --host 0.0.0.0 --port 8765 &'
alias start-tunnel='cloudflared tunnel run my-palace &'
```

Or create a launchd plist on macOS to start both on login.

---

## Troubleshooting Checklist

| Symptom | Check |
|---|---|
| All actions fail | Is palace_api.py running? Is the tunnel running? |
| "Invalid or missing API key" | Is X-Palace-Key header set correctly in action auth? |
| Reads work, writes time out | Use GET /file (not POST /drawer) — see known issues |
| "Something went wrong" from ChatGPT | Cloudflare returned HTML (challenge page) instead of JSON |
| Empty response from reasoning model | Raise max_completion_tokens to 16000+ |
| Tunnel URL changes each restart | Use a named tunnel — see Step 2 |

---

*Built by the Campfire Curriculum circle. The palace holds what conversations lose.*
