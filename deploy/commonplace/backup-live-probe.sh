#!/usr/bin/env bash
# Live proof that one real backup run never displaces the serve container.
#
# Operator-run, as root on the data host, only with the owner's approval. It
# starts the backup unit once and samples the service while that unit runs.
# Its only other write is a card heartbeat on a dedicated probe card, every
# 5 seconds. It never stops, starts or recreates a container.
#
# Usage: backup-live-probe.sh EVIDENCE_DIR      (EVIDENCE_DIR must not exist)
#
# Required environment:
#   PROBE_BACKUP_UNIT  systemd service that runs nightly-backup.sh
#   PROBE_PUBLIC_URL   base URL clients use, e.g. https://hub.example.com
#   PROBE_CARD         number of a card the probe token holds in "doing"
#   COMMONPLACE_TOKEN  probe token; never printed, logged or passed in argv
#   PROBE_LATEST_CMD   prints the latest restic snapshot of this deployment as
#                      one JSON object {"id": ..., "time": RFC3339}, or null
#                      (same contract as DEPLOY_BACKUP_LATEST_CMD)
#   PROBE_CHECK_CMD    verifies the restic repository; nonzero on failure
# Optional:
#   PROBE_LOCAL_URL    default http://127.0.0.1:8765
#   PROBE_PAUSE_MAX_S  default 300; memory window W must be <= this + 120 s
#   PROBE_WARMUP_S     default 180; time allowed for 10 OKs per sampler
#   COMMONPLACE_DEPLOY_DIR, COMMONPLACE_ENV_FILE, COMPOSE_FILE: as backup-hook.sh
# Command values split on whitespace into an executable and arguments; shell
# quoting, pipelines and globs are not interpreted.
#
# Exit: 0 PASS, 1 FAIL, 2 refused before the backup started, 3 INCONCLUSIVE
# (an OOM kill in the window). The verdict and samples go to EVIDENCE_DIR
# (mode 0700); it holds no token and no memory content.
set -Eeuo pipefail
umask 077

die() {
    printf 'backup live probe: %s\n' "$1" >&2
    exit "${2:-2}"
}

[[ $# == 1 && "$1" == /* ]] || die "usage: backup-live-probe.sh /absolute/evidence-dir"
evidence=$1
[[ ! -e "$evidence" ]] || die "evidence directory exists"
(( EUID == 0 )) || die "run as root (starts the backup unit, reads the data directory)"
for tool in curl docker python3 systemctl journalctl flock timeout awk find; do
    command -v "$tool" >/dev/null || die "missing $tool"
done
for name in PROBE_BACKUP_UNIT PROBE_PUBLIC_URL PROBE_CARD COMMONPLACE_TOKEN PROBE_LATEST_CMD PROBE_CHECK_CMD; do
    [[ -n "${!name:-}" ]] || die "$name is required"
done
unit=$PROBE_BACKUP_UNIT
public_url=${PROBE_PUBLIC_URL%/}
local_url=${PROBE_LOCAL_URL:-http://127.0.0.1:8765}
local_url=${local_url%/}
card=$PROBE_CARD
[[ "$card" =~ ^[1-9][0-9]*$ ]] || die "PROBE_CARD must be a card number"
pause_max=${PROBE_PAUSE_MAX_S:-300}
warmup=${PROBE_WARMUP_S:-180}
[[ "$pause_max" =~ ^[1-9][0-9]*$ && "$warmup" =~ ^[1-9][0-9]*$ ]] || die "invalid PROBE_PAUSE_MAX_S or PROBE_WARMUP_S"
read -r -a latest_cmd <<<"$PROBE_LATEST_CMD"
read -r -a check_cmd <<<"$PROBE_CHECK_CMD"
(( ${#latest_cmd[@]} && ${#check_cmd[@]} )) || die "empty PROBE_LATEST_CMD or PROBE_CHECK_CMD"

repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
operator_dir=${COMMONPLACE_DEPLOY_DIR:-$(dirname -- "$repo_dir")}
compose=(docker compose --env-file "${COMMONPLACE_ENV_FILE:-$operator_dir/.env}"
    -f "${COMPOSE_FILE:-$operator_dir/compose.yml}")

work=$(mktemp -d /tmp/commonplace-live-probe.XXXXXXXX)
sampler_pids=()
cleanup() {
    touch "$work/stop" 2>/dev/null || true
    local pid
    for pid in "${sampler_pids[@]}"; do
        kill "$pid" 2>/dev/null || true
    done
    wait 2>/dev/null || true
    rm -rf -- "$work"
}
trap cleanup EXIT
trap 'exit 143' TERM INT
install -d -m 0700 -- "$evidence"
# The token lives only in this private header file, never in argv or evidence.
auth="$work/auth"
printf 'Authorization: Bearer %s\n' "$COMMONPLACE_TOKEN" >"$auth"

# Serve identity, project and data directory before the run.
cid=$("${compose[@]}" ps --status running -q commonplace) || die "serve not running"
[[ -n "$cid" && "$cid" != *$'\n'* ]] || die "expected exactly one running serve container"
read -r serve_id serve_started project < <(docker inspect --type container \
    --format '{{.Id}} {{.State.StartedAt}} {{index .Config.Labels "com.docker.compose.project"}}' "$cid") ||
    die "serve inspect failed"
data_host=$(docker inspect --type container --format '{{json .Mounts}}' "$serve_id" | python3 -c '
import json, sys
mounts = [m for m in json.load(sys.stdin) if m.get("Destination") == "/data"]
print(mounts[0]["Source"] if len(mounts) == 1 else "")
')
[[ "$data_host" == /* && -d "$data_host" ]] || die "no /data bind mount on serve"
latest_before=$("${latest_cmd[@]}") || die "PROBE_LATEST_CMD failed"
data_bytes=$({ du -sb --exclude=.backup-staging -- "$data_host" 2>/dev/null || true; } | cut -f1)

now() { date +%s.%N; }

http_code() {
    curl -sS -o "${1:-/dev/null}" -w '%{http_code}' --max-time "${2:-5}" "${@:3}" || true
}

sample_healthz_local() {
    local code
    code=$(http_code /dev/null 5 "$local_url/healthz")
    [[ "$code" == 200 ]] && echo "ok $code" || echo "fail $code"
}

sample_healthz_public() {
    local code
    code=$(http_code /dev/null 5 "$public_url/healthz")
    [[ "$code" == 200 ]] && echo "ok $code" || echo "fail $code"
}

sample_board_read() {
    local code
    code=$(http_code /dev/null 10 -H @"$auth" "$public_url/api/cards/$card")
    [[ "$code" == 200 ]] && echo "ok $code" || echo "fail $code"
}

sample_board_write() {
    local code
    code=$(http_code /dev/null 10 -X POST -H @"$auth" -H 'Content-Type: application/json' \
        --data '{}' "$public_url/api/cards/$card/heartbeat")
    [[ "$code" == 200 ]] && echo "ok $code" || echo "fail $code"
}

sample_mcp() {
    local body="$work/mcp.$BASHPID" code
    code=$(http_code "$body" 40 -X POST -H @"$auth" -H 'Content-Type: application/json' \
        -H 'Accept: application/json, text/event-stream' \
        --data '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"memory_search","arguments":{"query":"backup live probe","limit":1}}}' \
        "$public_url/mcp")
    # Classify only; no response content is kept.
    python3 - "$code" "$body" <<'PY'
import json, sys
code, path = sys.argv[1], sys.argv[2]
try:
    reply = json.load(open(path))
except Exception:
    print(f"fail http={code}")
    sys.exit()
error = reply.get("error") if isinstance(reply, dict) else None
result = reply.get("result") if isinstance(reply, dict) else None
if code == "200" and isinstance(result, dict) and not result.get("isError"):
    print("ok 200")
elif isinstance(error, dict) and error.get("code") == -32603:
    retry = (error.get("data") or {}).get("retry_after_s")
    print(f"paused retry_after_s={retry}")
elif isinstance(error, dict):
    print(f"fail jsonrpc={error.get('code')}")
else:
    print(f"fail http={code}")
PY
    rm -f -- "$body"
}

sample_serve() {
    local line id started oneoffs
    line=$(docker inspect --type container --format '{{.Id}} {{.State.StartedAt}}' "$serve_id" 2>/dev/null) ||
        { echo "fail inspect"; return; }
    read -r id started <<<"$line"
    oneoffs=$(docker ps -a -q --filter "label=com.docker.compose.project=$project" \
        --filter label=com.docker.compose.oneoff=True | wc -l)
    if [[ "$id" == "$serve_id" && "$started" == "$serve_started" && "$oneoffs" == 0 ]]; then
        echo "ok same"
    else
        echo "fail id_or_start_changed_or_oneoffs=$oneoffs"
    fi
}

sample_resources() {
    local wal rss
    wal=$({ find "$data_host" -xdev -name '*-wal' -printf '%s\n' 2>/dev/null || true; } | sort -n | tail -n 1)
    rss=$({ docker top "$serve_id" -eo rss,args 2>/dev/null || true; } |
        awk '/commonplace backup --online/ { if ($1 > max) max = $1 } END { print max + 0 }')
    echo "ok wal_bytes=${wal:-0} backup_rss_kb=${rss:-0}"
}

sampler() {
    local name=$1 interval=$2 start result rest
    while [[ ! -e "$work/stop" ]]; do
        start=$(now)
        result=$("sample_$name" 2>/dev/null) || result="error"
        printf '%s %s %s\n' "$start" "$(now)" "${result:-error}" >>"$evidence/$name.samples"
        rest=$(awk -v s="$start" -v i="$interval" -v n="$(now)" 'BEGIN { r = s + i - n; printf "%.3f", (r < 0 ? 0 : r) }')
        sleep "$rest"
    done
}

checked=(healthz_local healthz_public board_read board_write mcp serve)
declare -A interval=([healthz_local]=1 [healthz_public]=1 [board_read]=5 [board_write]=5 [mcp]=5 [serve]=5 [resources]=15)
for name in "${checked[@]}" resources; do
    sampler "$name" "${interval[$name]}" & sampler_pids+=($!)
done

# Start only after every checked sampler has 10 OKs.
deadline=$(( SECONDS + warmup ))
while :; do
    ready=1
    for name in "${checked[@]}"; do
        oks=$({ grep -c ' ok ' "$evidence/$name.samples" 2>/dev/null || true; })
        (( ${oks:-0} >= 10 )) || ready=0
    done
    (( ready )) && break
    (( SECONDS < deadline )) || die "samplers did not reach 10 OKs each in ${warmup} s; backup not started"
    sleep 1
done

t_start=$(now)
printf 'backup unit start: %s\n' "$t_start" >"$evidence/run.txt"
unit_rc=0
systemctl start --wait "$unit" || unit_rc=$?
t_end=$(now)
final_serve=$(sample_serve)
printf 'backup unit end: %s rc=%s\nfinal serve: %s\n' "$t_end" "$unit_rc" "$final_serve" >>"$evidence/run.txt"
sleep 30
touch "$work/stop"
for pid in "${sampler_pids[@]}"; do
    wait "$pid" 2>/dev/null || true
done
sampler_pids=()

systemctl show -p Result -p ExecMainStartTimestamp -p ExecMainExitTimestamp -p InvocationID \
    "$unit" >"$evidence/scheduler.txt" || true
journalctl -u "$unit" --since "@${t_start%.*}" --no-pager -o short-iso >"$evidence/unit-journal.txt" 2>&1 || true
oom=$({ journalctl -k --since "@${t_start%.*}" --until "@$(( ${t_end%.*} + 30 ))" --no-pager 2>/dev/null || true; } |
    grep -ciE 'out of memory|oom-kill|killed process' || true)
latest_after=$("${latest_cmd[@]}") || latest_after="error"
check_rc=0
"${check_cmd[@]}" >"$evidence/restic-check.txt" 2>&1 || check_rc=$?

python3 - "$evidence" "$t_start" "$t_end" "$unit_rc" "$final_serve" "$latest_before" "$latest_after" \
    "$check_rc" "${oom:-0}" "$pause_max" "${data_bytes:-0}" <<'PY'
import json, re, sys
from datetime import datetime
(evidence, t_start, t_end, unit_rc, final_serve, before, after, check_rc, oom, pause_max,
 data_bytes) = sys.argv[1:]
t_start, t_end, pause_max = float(t_start), float(t_end), float(pause_max)
failures, metrics = [], {}
gap = {"healthz_local": 3, "healthz_public": 3, "board_read": 12, "board_write": 12, "mcp": 12,
       "serve": 12}

def load(name):
    rows = []
    try:
        lines = open(f"{evidence}/{name}.samples").read().splitlines()
    except OSError:
        return rows
    for line in lines:
        start, end, status, *rest = line.split(" ") + [""]
        rows.append((float(start), float(end), status, " ".join(rest).strip()))
    return rows

window = None
for name, max_gap in gap.items():
    rows = load(name)
    if not rows:
        failures.append(f"{name}: no samples")
        continue
    bad = [r for r in rows if r[2] not in ("ok", "paused")]
    errors = [r for r in rows if r[2] == "error"]
    if errors:
        failures.append(f"{name}: {len(errors)} sampler errors")
    if name == "mcp":
        paused = [r for r in rows if r[2] == "paused"]
        if bad:
            failures.append(f"mcp: {len(bad)} failures other than -32603")
        if paused:
            first, last = paused[0][0], paused[-1][0]
            after_ok = [r for r in rows if r[2] == "ok" and r[0] > last]
            end = after_ok[0][0] if after_ok else rows[-1][1]
            window = (first, end)
            metrics["memory_window_s"] = round(end - first, 1)
            if after_ok:
                metrics["memory_cold_start_s"] = round(after_ok[0][1] - after_ok[0][0], 1)
            else:
                failures.append("mcp: memory never answered after the pause")
            if end - first > pause_max + 120:
                failures.append(f"mcp: window {end - first:.0f} s exceeds pause max + 120 s")
            if any(r[2] == "ok" and first < r[0] < last for r in rows):
                metrics["memory_windows"] = "more than one"
    else:
        paused = [r for r in rows if r[2] == "paused"]
        if bad or paused:
            failures.append(f"{name}: {len(bad) + len(paused)} non-OK samples")
    for previous, current in zip(rows, rows[1:]):
        allowed = max_gap
        if name == "mcp" and window and window[0] - 1 <= current[0] <= window[1] + 1:
            allowed = 30
        if current[0] - previous[0] > allowed:
            failures.append(f"{name}: gap {current[0] - previous[0]:.1f} s > {allowed} s")
            break

wal = rss = 0
for _, _, status, rest in load("resources"):
    fields = dict(item.split("=", 1) for item in rest.split() if "=" in item)
    wal = max(wal, int(fields.get("wal_bytes", 0) or 0))
    rss = max(rss, int(fields.get("backup_rss_kb", 0) or 0))
metrics.update(wal_peak_bytes=wal, backup_rss_peak_kb=rss)
duration = t_end - t_start
metrics["unit_seconds"] = round(duration, 1)
if duration > 0 and int(data_bytes) > 0:
    metrics["approx_mb_per_s"] = round(int(data_bytes) / 1e6 / duration, 1)
journal = open(f"{evidence}/unit-journal.txt", errors="replace").read()
for key in ("pause_s", "paused_s", "console_drops", "mb_per_s", "MB/s"):
    match = re.search(rf"{re.escape(key)}\W+([0-9.]+)", journal)
    if match:
        metrics[f"reported_{key}"] = match[1]
metrics["scheduler"] = open(f"{evidence}/scheduler.txt").read().splitlines()

if unit_rc != "0":
    failures.append(f"backup unit exit {unit_rc}")
if not final_serve.startswith("ok "):
    failures.append(f"final serve check: {final_serve}")
try:
    new = json.loads(after)
    old = json.loads(before)
    stamp = datetime.fromisoformat(new["time"].replace("Z", "+00:00")).timestamp()
    if (old and new["id"] == old.get("id")) or stamp < t_start - 1:
        failures.append("no new restic snapshot")
except Exception:
    failures.append("latest restic snapshot unreadable")
if check_rc != "0":
    failures.append("restic check failed")

verdict = "PASS" if not failures else "FAIL"
if int(oom) > 0:
    verdict = "INCONCLUSIVE"
    failures.append(f"{oom} OOM kernel lines in the window")
report = {"verdict": verdict, "failures": failures, "metrics": metrics}
with open(f"{evidence}/verdict.json", "w") as handle:
    json.dump(report, handle, indent=2, sort_keys=True)
print(json.dumps(report, sort_keys=True))
sys.exit({"PASS": 0, "FAIL": 1, "INCONCLUSIVE": 3}[verdict])
PY
