# ChatGPT Remote MCP

MemPalace can be exposed to ChatGPT Developer Mode as a remote MCP app
over Streamable HTTP.

## Install

```bash
pip install "mempalace[mcp-http]"
```

## Start the remote server

```bash
mempalace-mcp-http --host 0.0.0.0 --port 8000
```

Default paths:

- Streamable HTTP: `http://HOST:8000/mcp`
- SSE: `http://HOST:8000/sse`

For ChatGPT, place this behind a public HTTPS hostname. A tunnel works
for testing; a named tunnel or your own domain is better for long-term use.

## OAuth

Create an operator secret:

```bash
python3 - <<'PY' > ~/.mempalace/mcp_http_auth_secret
import secrets
print(secrets.token_urlsafe(32))
PY
chmod 600 ~/.mempalace/mcp_http_auth_secret
```

Start the server with a public issuer URL:

```bash
mempalace-mcp-http \
  --host 0.0.0.0 \
  --port 8000 \
  --oauth-issuer-url https://mempalace.example.com
```

OAuth state is persisted at `~/.mempalace/mcp_http_oauth.json`.

## Add the app in ChatGPT

1. Enable Developer Mode in ChatGPT settings.
2. Create an app.
3. Paste `https://mempalace.example.com/mcp`.
4. Choose `OAuth` if the server was started with `--oauth-issuer-url`, otherwise `No Authentication`.
5. Finish the consent flow. When prompted on the MemPalace consent page,
   enter the operator secret from `~/.mempalace/mcp_http_auth_secret`.

## Notes

- Read-only MemPalace tools advertise `readOnlyHint`.
- `localhost` is not enough for ChatGPT. The URL must be publicly reachable.
- The quick `trycloudflare` hostnames are fine for short-lived testing but are not stable.
