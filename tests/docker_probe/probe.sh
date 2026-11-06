#!/usr/bin/env bash
# Real-Docker probe for the online backup hook (backup-hook.sh). Disposable hosts only.
#
# The hook tests use a fake docker client. This probe checks the same promises
# against a real engine and a real image built from this checkout:
#   - /healthz answers 200 at a 1 s interval for the whole run (max gap 3 s);
#   - the serve container keeps its Id and StartedAt; no one-off container appears;
#   - the in-container child creates <stamp>.partial/; the heartbeat stays fresh (30 s);
#   - the snapshot process has stdin on /dev/null;
#   - exit 0 and backups/commonplace-<stamp>/manifest.json;
#   - kill -9 of the hook, and separately of the in-container supervisor: the child
#     is gone and the stamp lock is released within 90 s, nothing is published, and
#     the next run cleans up and succeeds.
#
# It runs inside a docker:27-cli container that talks to the engine socket and shares
# PROBE_ROOT with the engine at the same path, so the compose bind mount and the hook
# see the same files. From a source checkout, on a host with no other containers:
#
#   PROBE_ROOT=/tmp/cp-backup-probe
#   docker run --rm -v "$PWD":/src:ro -v "$PROBE_ROOT":"$PROBE_ROOT" alpine:3 \
#       sh -c "rm -rf $PROBE_ROOT/src && mkdir -p $PROBE_ROOT && cp -a /src $PROBE_ROOT/src"
#   docker run --rm --name cp-backup-probe-cli -e PROBE_CONFIRM=disposable-host \
#       -v /var/run/docker.sock:/var/run/docker.sock -v "$PROBE_ROOT":"$PROBE_ROOT" \
#       docker:27-cli sh -c "apk add --no-cache bash util-linux coreutils python3 >/dev/null \
#           && bash $PROBE_ROOT/src/tests/docker_probe/probe.sh"
#
# Start the engine only for the probe and stop it afterwards. Run it when nothing else
# needs the host; if the host is needed, stop the probe (its EXIT trap cleans up).
# Evidence stays in $PROBE_ROOT/evidence. Exit 0 = every check passed.
# awk programs are passed through check() as literal single-quoted arguments.
# shellcheck disable=SC2016
set -Eeuo pipefail
umask 077

PROBE_ROOT=${PROBE_ROOT:-/tmp/cp-backup-probe}
PROJECT=cpbackupprobe
IMAGE=commonplace:backup-probe
SRC=$PROBE_ROOT/src
OPERATOR=$PROBE_ROOT/operator
DATA=$PROBE_ROOT/data
BACKUPS=$PROBE_ROOT/backups
EVIDENCE=$PROBE_ROOT/evidence
KILL_WAIT_S=90
failures=0
sampler_pids=()
network_joined=0
self_id=""

log() { printf '[probe %(%H:%M:%S)T] %s\n' -1 "$*" | tee -a "$EVIDENCE/probe.log" >&2; }
check() {
    # check <description> <command...>: record PASS/FAIL, never abort the probe.
    local what=$1
    shift
    if "$@"; then
        log "PASS $what"
    else
        log "FAIL $what"
        failures=$((failures + 1))
    fi
}
die() {
    log "ABORT $*"
    exit 2
}

compose() { docker compose --env-file "$OPERATOR/.env" -f "$OPERATOR/compose.yml" "$@"; }

cleanup() {
    local status=$?
    trap - EXIT
    for pid in "${sampler_pids[@]}"; do
        kill "$pid" 2>/dev/null || true
    done
    wait 2>/dev/null || true
    if ((network_joined)); then
        docker network disconnect "${PROJECT}_default" "$self_id" >/dev/null 2>&1 || true
    fi
    if [[ -f $OPERATOR/compose.yml ]]; then
        compose down --remove-orphans --timeout 60 >/dev/null 2>&1 || true
    fi
    rm -rf -- "$DATA" "$BACKUPS"
    log "done: $failures failed check(s); evidence in $EVIDENCE"
    if ((status == 0 && failures > 0)); then
        status=1
    fi
    exit "$status"
}

# ---------------------------------------------------------------- preconditions
mkdir -p "$EVIDENCE"
[[ ${PROBE_CONFIRM:-} == disposable-host ]] || die "set PROBE_CONFIRM=disposable-host"
[[ -f $SRC/backup-hook.sh && -f $SRC/deploy/commonplace/Dockerfile ]] || die "no source at $SRC"
for tool in docker flock python3 timeout stat; do
    command -v "$tool" >/dev/null || die "missing $tool"
done
docker compose version >/dev/null || die "docker compose plugin missing"
self_id=$(hostname)
mapfile -t foreign < <(docker ps -aq --no-trunc | grep -v "^${self_id}" || true)
((${#foreign[@]} == 0)) || die "foreign containers exist (${#foreign[@]}); use a disposable host"
trap cleanup EXIT

# ---------------------------------------------------------------- setup
log "building $IMAGE from $SRC"
docker build -q -f "$SRC/deploy/commonplace/Dockerfile" -t "$IMAGE" "$SRC" >"$EVIDENCE/build.log"
rm -rf -- "$DATA" "$BACKUPS" "$OPERATOR"
install -d -o 10001 -g 10001 -m 0700 -- "$DATA"
install -d -m 0700 -- "$BACKUPS" "$OPERATOR"
cat >"$OPERATOR/.env" <<EOF
COMMONPLACE_BASE_URL=http://localhost
COMMONPLACE_BACKUP_MAX_MBPS=2
COMMONPLACE_BACKUP_RESERVE_BYTES=1048576
EOF
cat >"$OPERATOR/compose.yml" <<EOF
name: $PROJECT
services:
  commonplace:
    image: $IMAGE
    init: true
    env_file:
      - $OPERATOR/.env
    environment:
      COMMONPLACE_DATA: /data
    volumes:
      - $DATA:/data
    stop_grace_period: 60s
EOF
export COMMONPLACE_ENV_FILE=$OPERATOR/.env COMPOSE_FILE=$OPERATOR/compose.yml
export COMMONPLACE_BACKUP_RESERVE_BYTES=1048576

log "initialising synthetic data"
compose run --rm --no-deps -T commonplace \
    commonplace init --admin-email probe@example.test --workspace probe >"$EVIDENCE/init.log"
# A 96 MiB worker file keeps the throttled copy (2 MB/s) busy long enough to kill mid-run.
compose run --rm --no-deps -T commonplace python -c '
import os, sqlite3
wid = sqlite3.connect("/data/control.sqlite3").execute("SELECT id FROM workspaces").fetchone()[0]
path = f"/data/workspaces/{wid}/palace"
os.makedirs(path, exist_ok=True)
with open(f"{path}/probe-blob.bin", "wb") as out:
    for _ in range(96):
        out.write(os.urandom(1 << 20))
' >>"$EVIDENCE/init.log"
compose up -d commonplace >>"$EVIDENCE/init.log"
cid=$(compose ps -q commonplace)
[[ -n $cid ]] || die "serve container did not start"
docker network connect "${PROJECT}_default" "$self_id"
network_joined=1
healthy=0
for _ in $(seq 120); do
    if python3 -c 'import urllib.request as u; u.urlopen("http://commonplace:8765/healthz", timeout=2)' 2>/dev/null; then
        healthy=1
        break
    fi
    sleep 1
done
((healthy)) || die "serve never answered /healthz"
identity() { docker inspect -f '{{.Id}} {{.State.StartedAt}}' "$cid"; }
identity_before=$(identity)
log "serve $identity_before"

# ---------------------------------------------------------------- samplers
python3 - "$EVIDENCE/healthz.log" <<'EOF' &
import sys, time, urllib.request
with open(sys.argv[1], "a", buffering=1) as out:
    while True:
        try:
            with urllib.request.urlopen("http://commonplace:8765/healthz", timeout=2) as r:
                status = r.status
        except Exception as exc:  # any failure is a non-200 sample
            status = type(exc).__name__
        out.write(f"{time.time():.3f} {status}\n")
        time.sleep(1)
EOF
sampler_pids+=($!)
(
    while true; do
        n=$(docker ps -aq --filter "label=com.docker.compose.project=$PROJECT" \
            --filter label=com.docker.compose.oneoff=True | wc -l)
        printf '%s %s\n' "$(date +%s)" "$n"
        sleep 1
    done
) >>"$EVIDENCE/oneoffs.log" 2>&1 &
sampler_pids+=($!)
(
    while true; do
        now=$(date +%s)
        for entry in "$DATA"/.backup-staging/*; do
            [[ -e $entry ]] || continue
            age=$((now - $(stat -c %Y -- "$entry")))
            printf '%s %s %s\n' "$now" "${entry##*/}" "$age"
        done
        sleep 0.2
    done
) >>"$EVIDENCE/staging.log" 2>&1 &
sampler_pids+=($!)
# Snapshot processes inside the container: pid, ppid and the target of fd 0.
PROC_SCAN='
import os
for pid in filter(str.isdigit, os.listdir("/proc")):
    try:
        args = open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")
        if b"backup" in args and b"--online" in args:
            ppid = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()[1]
            print(pid, ppid, os.readlink(f"/proc/{pid}/fd/0"))
    except OSError:
        pass
'
(
    while true; do
        compose exec -T commonplace python -c "$PROC_SCAN" 2>/dev/null | sed "s/^/$(date +%s) /"
        sleep 2
    done
) >>"$EVIDENCE/procs.log" 2>&1 &
sampler_pids+=($!)

# ---------------------------------------------------------------- helpers
run_hook() { bash "$SRC/backup-hook.sh" "$BACKUPS" </dev/null; }
published() { find "$BACKUPS" -mindepth 1 -maxdepth 1 -name 'commonplace-*' | sort; }
snapshot_procs() { compose exec -T commonplace python -c "$PROC_SCAN" 2>/dev/null || true; }
wait_for_partial() {
    local _
    for _ in $(seq 600); do
        compgen -G "$DATA/.backup-staging/*.partial" >/dev/null && return 0
        sleep 0.2
    done
    return 1
}
locks_free() {
    local lock
    for lock in "$DATA"/.backup-staging/*.lock; do
        [[ -e $lock ]] || continue
        flock -n "$lock" true || return 1
    done
}
child_gone_and_unlocked() {
    local _
    for _ in $(seq "$KILL_WAIT_S"); do
        if [[ -z $(snapshot_procs) ]] && locks_free; then
            return 0
        fi
        sleep 1
    done
    return 1
}
same_serve() { [[ $(identity) == "$identity_before" ]]; }
one_new_snapshot() {
    local before=$1 after
    after=$(published | wc -l)
    ((after == before + 1)) && [[ -s $(published | tail -n 1)/manifest.json ]]
}

# ---------------------------------------------------------------- run 1: success
count=$(published | wc -l)
log "run 1: complete backup"
run1_from=$(wc -l <"$EVIDENCE/staging.log")
rc=0
run_hook || rc=$?
run1_to=$(wc -l <"$EVIDENCE/staging.log")
check "run 1 exit 0 (got $rc)" test "$rc" -eq 0
check "run 1 published commonplace-<stamp>/manifest.json" one_new_snapshot "$count"
check "run 1 serve Id and StartedAt unchanged" same_serve
check "run 1 child created <stamp>.partial/" grep -q '\.partial ' "$EVIDENCE/staging.log"
check "run 1 heartbeat never older than 30 s" awk -v from="$run1_from" -v to="$run1_to" \
    'NR > from && NR <= to && $2 ~ /\.hb$/ && $3 > 30 { exit 1 }' "$EVIDENCE/staging.log"
check "run 1 snapshot stdin is /dev/null" \
    awk '$4 != "" { seen = 1; if ($4 != "/dev/null") bad = 1 } END { exit !(seen && !bad) }' \
    "$EVIDENCE/procs.log"

# ---------------------------------------------------------------- run 2: kill -9 the hook
count=$(published | wc -l)
log "run 2: kill -9 of the hook mid-run"
run_hook &
hook_pid=$!
if wait_for_partial; then
    kill -9 "$hook_pid"
    wait "$hook_pid" 2>/dev/null || true
    check "run 2 child gone and lock released within ${KILL_WAIT_S}s" child_gone_and_unlocked
else
    check "run 2 reached <stamp>.partial/ before the kill" false
    wait "$hook_pid" 2>/dev/null || true
fi
check "run 2 published nothing" test "$(published | wc -l)" -eq "$count"
check "run 2 serve Id and StartedAt unchanged" same_serve

log "run 3: the next run cleans up"
rc=0
run_hook || rc=$?
check "run 3 exit 0 (got $rc)" test "$rc" -eq 0
check "run 3 published one snapshot" one_new_snapshot "$count"
check "run 3 left no earlier staging" test -z "$(find "$DATA/.backup-staging" "$BACKUPS/.commonplace-staging" -mindepth 1 2>/dev/null)"

# ---------------------------------------------------------------- run 4: kill -9 the supervisor
count=$(published | wc -l)
log "run 4: kill -9 of the in-container supervisor mid-run"
run_hook &
hook_pid=$!
if wait_for_partial; then
    # The supervisor is the snapshot process whose parent is not a snapshot process.
    supervisor=$(snapshot_procs | awk '{ pid[$1] = 1; parent[$1] = $2 } END { for (p in pid) if (!(parent[p] in pid)) print p }' | head -n 1)
    if [[ -n $supervisor ]]; then
        compose exec -T commonplace sh -c "kill -9 $supervisor"
        check "run 4 child gone and lock released within ${KILL_WAIT_S}s" child_gone_and_unlocked
    else
        check "run 4 found the supervisor" false
    fi
else
    check "run 4 reached <stamp>.partial/ before the kill" false
fi
rc=0
timeout "$KILL_WAIT_S" tail --pid="$hook_pid" -f /dev/null || true
wait "$hook_pid" 2>/dev/null || rc=$?
check "run 4 hook failed (got $rc)" test "$rc" -ne 0
check "run 4 published nothing" test "$(published | wc -l)" -eq "$count"
check "run 4 serve Id and StartedAt unchanged" same_serve

log "run 5: the next run cleans up"
rc=0
run_hook || rc=$?
check "run 5 exit 0 (got $rc)" test "$rc" -eq 0
check "run 5 published one snapshot" one_new_snapshot "$count"

# ---------------------------------------------------------------- whole-probe checks
check "serve Id and StartedAt unchanged for the whole probe" same_serve
check "every /healthz sample was 200" awk '$2 != 200 { exit 1 }' "$EVIDENCE/healthz.log"
check "no /healthz gap over 3 s" \
    awk 'NR > 1 && $1 - last > 3 { exit 1 } { last = $1 }' "$EVIDENCE/healthz.log"
check "no one-off container during the hook runs" awk '$2 != 0 { exit 1 }' "$EVIDENCE/oneoffs.log"
