#!/usr/bin/env bash
#
# Smoke-test a built MemPalace image and the shipped Compose files.
#
#   scripts/docker-smoke.sh [IMAGE]        # default image: mempalace:smoke
#
# CI builds the container images but, until this existed, never ran one and
# never parsed a Compose file — so two defects that break the very first
# documented command shipped anyway: a `docker-compose.yml` Compose refused to
# load (#2188), and an embedding-vector bug that aborted `mine` on the first
# drawer (#2187). A build that succeeds proves the image *compiles*; it says
# nothing about whether the thing inside it works.
#
# So this exercises the paths the README actually tells users to run, and
# asserts on returned content rather than exit codes alone — the `mine` crash
# was a non-zero exit, but the verbatim read-back is what proves the palace
# really holds the text.
#
# Requires: docker (with the compose plugin) and python3.

set -euo pipefail

IMAGE="${1:-mempalace:smoke}"
VOLUME="mempalace-smoke-$$"
WORKDIR="$(mktemp -d)"

# The image runs as uid 1000, which will not match the host user on Linux, and
# bind mounts carry host ownership through unchanged (Docker Desktop's uid
# mapping hides this on macOS). `mktemp -d` gives 0700, so the container could
# not even stat inside /work. Model an ordinary project checkout instead: 0755
# dir, 0644 files — which is what makes the README's `-v /path/to/project:/work`
# work for a normal repo.
chmod 0755 "$WORKDIR"

# A sentence we can assert on verbatim. Storing user words exactly is the
# project's core promise, so the read-back check is the real assertion here.
NEEDLE="eleven round trips to render one screen"

cleanup() {
    docker volume rm -f "$VOLUME" >/dev/null 2>&1 || true
    rm -rf "$WORKDIR"
}
trap cleanup EXIT

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

# Dump captured output on failure. Lines are clipped because the failure this
# most often reports — a rejected embedding batch — puts a whole 384-dim vector
# repr on a single line, which buries the actual message in the CI log.
dump() {
    echo "--- last 40 lines (clipped to 500 cols) ---" >&2
    printf '%s\n' "$1" | tail -40 | cut -c1-500 >&2
    echo "-------------------------------------------" >&2
}

echo "== 1/5  Compose files parse =="
# A key with only comments under it parses as null and Compose rejects the
# whole file. `config` is the cheapest way to catch that class of defect.
docker compose -f docker-compose.yml config --quiet \
    || fail "docker-compose.yml is not a valid Compose file"
echo "  docker-compose.yml ok"

# The server file interpolates a mandatory token; a dummy satisfies the
# `:?` guard so the rest of the file still gets validated.
MEMPALACE_MCP_HTTP_TOKEN=smoke-token \
    docker compose -f deploy/docker-compose.server.yml config --quiet \
    || fail "deploy/docker-compose.server.yml is not a valid Compose file"
echo "  deploy/docker-compose.server.yml ok"

echo "== 2/5  CLI entrypoint dispatch =="
# docker-entrypoint.sh routes `cli`/`mcp` keywords and forwards anything else
# to the CLI. Both forms are documented, so both are checked.
docker run --rm -v "$VOLUME:/data" "$IMAGE" cli --version >/dev/null \
    || fail "'cli --version' failed"
docker run --rm -v "$VOLUME:/data" "$IMAGE" --version >/dev/null \
    || fail "bare '--version' passthrough failed"
echo "  'cli ...' and bare passthrough both dispatch"

echo "== 3/5  mine a mounted directory =="
cat > "$WORKDIR/notes.md" <<EOF
# Architecture decision

We switched from REST to GraphQL because the mobile client was making
$NEEDLE. We settled on persisted queries.
EOF
chmod 0644 "$WORKDIR/notes.md"

# First run also downloads the ~80 MB embedding model into the volume, so this
# doubles as a check that a cold container can reach and cache it.
mine_out="$(docker run --rm -v "$VOLUME:/data" -v "$WORKDIR:/work" "$IMAGE" mine /work 2>&1)" \
    || { dump "$mine_out"; fail "'mine /work' exited non-zero"; }

echo "$mine_out" | grep -q "Drawers filed: 1" \
    || { dump "$mine_out"; fail "expected 'Drawers filed: 1'"; }
echo "  filed 1 drawer"

echo "== 4/5  search it back from a separate container =="
# A new container against the same volume: proves the palace persisted and
# that the stored text is returned verbatim, not summarised.
search_out="$(docker run --rm -v "$VOLUME:/data" "$IMAGE" search "why did we move off REST" 2>&1)" \
    || { dump "$search_out"; fail "'search' exited non-zero"; }

echo "$search_out" | grep -qF "$NEEDLE" \
    || { dump "$search_out"; fail "search did not return the drawer verbatim"; }
echo "  drawer returned verbatim"

echo "== 5/5  MCP server over stdio =="
# The README's MCP client config runs the image with -i and speaks JSON-RPC on
# stdin/stdout. Drive a real handshake and one tool call.
mcp_out="$(printf '%s\n' \
    '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}' \
    '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
    '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
    '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"mempalace_search","arguments":{"query":"GraphQL"}}}' \
    | docker run -i --rm -v "$VOLUME:/data" "$IMAGE" mcp 2>/dev/null)"

# The transcript goes to a file and the parser reads it via argv: the heredoc
# already occupies this command's stdin.
printf '%s\n' "$mcp_out" > "$WORKDIR/mcp.jsonl"

NEEDLE="$NEEDLE" python3 - "$WORKDIR/mcp.jsonl" <<'PY'
import json, os, sys

needle = os.environ["NEEDLE"]
seen = {}
with open(sys.argv[1], encoding="utf-8") as fh:
    for line in fh:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue  # non-JSON banner lines are not our concern here
        if "id" in msg:
            seen[msg["id"]] = msg


def result(rid, what):
    msg = seen.get(rid)
    if msg is None:
        sys.exit(f"FAIL: no JSON-RPC response for {what} (id={rid})")
    if "error" in msg:
        sys.exit(f"FAIL: {what} returned an error: {msg['error']}")
    return msg["result"]


name = result(1, "initialize")["serverInfo"]["name"]
if name != "mempalace":
    sys.exit(f"FAIL: initialize reported serverInfo.name={name!r}")
print(f"  initialize ok (server: {name})")

tools = result(2, "tools/list")["tools"]
if not tools:
    sys.exit("FAIL: tools/list returned no tools")
print(f"  tools/list ok ({len(tools)} tools)")

call = result(3, "tools/call")
if call.get("isError"):
    sys.exit(f"FAIL: mempalace_search reported isError: {call}")
text = "".join(part.get("text", "") for part in call.get("content", []))
if needle not in text:
    sys.exit("FAIL: mempalace_search did not return the drawer verbatim")
print("  mempalace_search ok (drawer returned verbatim)")
PY

echo
echo "docker smoke: all checks passed ($IMAGE)"
