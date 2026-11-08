#!/usr/bin/env bash
# The host backup job calls this hook before running host-managed restic.
#
# It snapshots the running serve container in place: the snapshot runs through
# `compose exec` inside that container, which pauses its own workers and fences
# its own writers. This hook never stops, starts, restarts, recreates, removes
# or kills the serve container and never starts a one-off container. The only
# Compose verbs it sends are `ps` and `exec`; serve inspection uses
# `docker inspect`.
set -Eeuo pipefail
umask 077
shopt -s nullglob
# Keep diagnostics visible even when errexit fires inside the logged function.
exec 3>&2

stage="setup"
log_file=""
compose=()
snapshot_env=()
data_staging=""
stamp=""
hb=""
hb_pid=""
snap_pid=""
snapshot_started=0
serve_id=""
serve_started=""
# A stamp is UTC time with nanoseconds plus the hook's pid, as the CLI requires.
stamp_re='^[0-9TZ.]+-[0-9]+$'

fail() {
    stage=$1
    exit "${2:-1}"
}

# One printable line (at most 200 characters) of command output for the stage.
summary() {
    local line
    line=$({ grep -v '^[[:space:]]*$' <<<"$1" || true; } | tail -n 1 | tr -d '\000-\037' | cut -c1-200)
    printf '%s' "${line:-no output}"
}

stop_heartbeat() {
    if [[ -n "$hb_pid" ]]; then
        kill "$hb_pid" 2>/dev/null || true
        wait "$hb_pid" 2>/dev/null || true
        hb_pid=""
    fi
}

stop_snapshot_client() {
    # Ends only the host-side `docker compose exec` client. The snapshot in the
    # container notices the stale heartbeat or its deadline and exits itself.
    if [[ -n "$snap_pid" ]]; then
        kill -TERM "$snap_pid" 2>/dev/null || true
        wait "$snap_pid" 2>/dev/null || true
        snap_pid=""
    fi
}

# Remove one stamp's data-side staging (<stamp>, .partial, .lock, .hb) unless a
# live snapshot holds <stamp>.lock. Fails when the lock is held.
remove_stamp() {
    local name=$1 lock="$data_staging/$1.lock"
    local paths=("$data_staging/$name" "$data_staging/$name.partial" "$data_staging/$name.hb" "$lock")
    if [[ -e "$lock" ]]; then
        flock -n "$lock" rm -rf -- "${paths[@]}"
    else
        rm -rf -- "${paths[@]}"
    fi
}

# Bytes held by one stamp's data-side staging (0 when nothing exists).
stamp_bytes() {
    local name=$1 path existing=()
    for path in "$data_staging/$name" "$data_staging/$name.partial" "$data_staging/$name.hb" \
        "$data_staging/$name.lock"; do
        [[ -e "$path" || -L "$path" ]] && existing+=("$path")
    done
    if (( ${#existing[@]} == 0 )); then
        printf '0'
        return
    fi
    { du -scb -- "${existing[@]}" 2>/dev/null || true; } | tail -n 1 | cut -f1
}

cleanup_failed_run() {
    (( snapshot_started )) || return 0
    local status=0
    # The CLI removes only what it can lock and never signals a process.
    if ! timeout -k 5 60 "${compose[@]}" exec -T commonplace commonplace backup --online-cleanup \
        "/data/.backup-staging/$stamp" </dev/null 3>&- 9>&-; then
        printf 'online cleanup in the container failed\n'
    fi
    rm -rf -- "$backup_dir/.commonplace-staging/$stamp" || status=1
    if ! remove_stamp "$stamp"; then
        printf 'staging %s is still locked by a running snapshot; the next run removes it\n' "$stamp"
        status=1
    fi
    return "$status"
}

finish() {
    local status=$?
    trap - EXIT
    # Finish cleanup on a second signal; a caught (not ignored) signal is reset
    # to its default in the commands started below.
    trap : TERM INT
    stop_snapshot_client
    stop_heartbeat
    if (( status != 0 )); then
        if ! cleanup_failed_run >>"${log_file:-/dev/null}" 2>&1; then
            stage="$stage; staging cleanup incomplete"
        fi
        printf 'commonplace backup failed: %s (details: %s)\n' "$stage" "${log_file:-no log created}" >&3
    elif [[ -n "$log_file" ]]; then
        rm -f -- "$log_file"
    fi
    exit "$status"
}
trap finish EXIT
trap 'exit 143' TERM INT

[[ $# == 1 && "$1" == /* && -d "$1" ]] || exit 2
backup_dir=$(realpath -- "$1")
repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cd -- "$repo_dir"
log_file=$(mktemp /tmp/commonplace-backup.XXXXXXXX.log)
operator_dir=${COMMONPLACE_DEPLOY_DIR:-$(dirname -- "$repo_dir")}
env_file=${COMMONPLACE_ENV_FILE:-$operator_dir/.env}
compose_file=${COMPOSE_FILE:-$operator_dir/compose.yml}
for config_name in COMMONPLACE_DEPLOY_DIR COMMONPLACE_ENV_FILE COMPOSE_FILE; do
    config_value=${!config_name-}
    if [[ -n "$config_value" && "$config_value" != /* ]]; then
        stage="configuration: $config_name must be an absolute path"
        exit 2
    fi
done
deadline_s=${COMMONPLACE_BACKUP_DEADLINE:-4500}
if [[ ! "$deadline_s" =~ ^[1-9][0-9]{0,6}$ ]]; then
    stage="configuration: COMMONPLACE_BACKUP_DEADLINE must be positive whole seconds"
    exit 2
fi
reserve=${COMMONPLACE_BACKUP_RESERVE_BYTES:-}
if [[ -n "$reserve" && ! "$reserve" =~ ^[0-9]{1,18}$ ]]; then
    stage="configuration: COMMONPLACE_BACKUP_RESERVE_BYTES must be whole bytes"
    exit 2
fi
# Extra free space on top of the size estimate and the reserve (default 2 GB).
margin=${COMMONPLACE_BACKUP_MARGIN_BYTES:-2000000000}
if [[ ! "$margin" =~ ^[0-9]{1,18}$ ]]; then
    stage="configuration: COMMONPLACE_BACKUP_MARGIN_BYTES must be whole bytes"
    exit 2
fi
# The heartbeat is touched every 10 s, or every third of a shorter stale limit.
stale_s=${COMMONPLACE_BACKUP_HEARTBEAT_STALE_S:-60}
if [[ ! "$stale_s" =~ ^[1-9][0-9]{0,6}$ ]]; then
    stage="configuration: COMMONPLACE_BACKUP_HEARTBEAT_STALE_S must be positive whole seconds"
    exit 2
fi
hb_every=$(( stale_s / 3 ))
(( hb_every <= 10 )) || hb_every=10
(( hb_every >= 1 )) || hb_every=1
if [[ -n "${COMMONPLACE_BACKUP_STATUS_WRITE_DIR:-}" ]]; then
    if [[ "$COMMONPLACE_BACKUP_STATUS_WRITE_DIR" != /run/commonplace-backup-status-rw ]]; then
        stage="configuration: invalid backup status writer alias"
        exit 2
    fi
    snapshot_env=(-e "COMMONPLACE_BACKUP_STATUS_DIR=$COMMONPLACE_BACKUP_STATUS_WRITE_DIR")
fi
# This hook never reads the env file itself; Compose does.
compose=(docker compose --env-file "$env_file" -f "$compose_file")

# Prints "<Id> <StartedAt> <Running>" of one container.
inspect_serve() {
    docker inspect --type container --format '{{.Id}} {{.State.StartedAt}} {{.State.Running}}' "$1"
}

# Prints the host path bind-mounted at /data in one container.
serve_data_source() {
    docker inspect --type container --format '{{json .Mounts}}' "$1" | python3 -c '
import json, sys
mounts = [m for m in json.load(sys.stdin) if m.get("Destination") == "/data"]
source = mounts[0].get("Source", "") if len(mounts) == 1 else ""
if not source.startswith("/") or "\n" in source or mounts[0].get("RW") is False:
    sys.exit(1)
print(source)
'
}

# Fails with "serve changed" unless the same serve container is still running
# with the same start time it had before the snapshot.
check_serve_unchanged() {
    local ids id started running
    ids=$("${compose[@]}" ps --status running -q commonplace) || fail "serve changed"
    [[ "$ids" == "$serve_id" ]] || fail "serve changed"
    read -r id started running < <(inspect_serve "$serve_id" || true) || true
    [[ "${id:-}" == "$serve_id" && "${started:-}" == "$serve_started" && "${running:-}" == true ]] ||
        fail "serve changed"
}

run_backup() {
    stage="lock"
    # The timer is root; keep this lock outside the source data tree. Runs are
    # also serialized by the nightly lock, the in-container exclusive fence and
    # the per-stamp lock.
    exec 9>"$backup_dir/.commonplace-backup.lock"
    flock -n 9
    # Every long-lived child closes fd 9 (`9>&-`): an exec client or heartbeat
    # that outlives a killed hook must not keep the next run locked out.

    stage="serve inspect"
    local ids running data_host
    ids=$("${compose[@]}" ps --status running -q commonplace) || fail "serve not running"
    [[ -n "$ids" && "$ids" != *$'\n'* ]] || fail "serve not running"
    serve_id=$ids
    read -r serve_id serve_started running < <(inspect_serve "$ids") || fail "serve inspect failed"
    [[ "$serve_id" == "$ids" && -n "$serve_started" && "$running" == true ]] || fail "serve not running"
    data_host=$(serve_data_source "$serve_id") || fail "serve inspect: no single writable /data bind mount"
    [[ -d "$data_host" && ! -L "$data_host" ]] || fail "serve inspect: /data source is not a directory"
    data_host=$(realpath -- "$data_host")
    # Publishing renames the snapshot out of the data tree, so both must share
    # one filesystem. Never copy across filesystems here.
    if [[ $(stat -c %d -- "$data_host") != "$(stat -c %d -- "$backup_dir")" ]]; then
        stage="configuration: backup directory is not on the data directory's filesystem"
        exit 2
    fi

    stage="staging"
    data_staging="$data_host/.backup-staging"
    [[ ! -L "$data_staging" && ! -L "$backup_dir/.commonplace-staging" ]] || fail "staging: symlink refused"
    install -d -o 10001 -g 10001 -m 0700 "$data_host/.backup-staging"
    install -d -m 0700 -- "$backup_dir/.commonplace-staging"
    [[ -d "$data_staging" && ! -L "$data_staging" ]] || fail "staging: not a directory"

    stage="leftover staging"
    local entry name bytes removed=0 unexpected=0
    local -A seen=()
    for entry in "$data_staging"/* "$data_staging"/.[!.]*; do
        name=${entry##*/}
        name=${name%.partial}
        name=${name%.lock}
        name=${name%.hb}
        if [[ ! "$name" =~ $stamp_re ]]; then
            unexpected=$(( unexpected + 1 ))
            continue
        fi
        [[ -z "${seen[$name]+x}" ]] || continue
        seen[$name]=1
        bytes=$(stamp_bytes "$name")
        remove_stamp "$name" || fail "previous backup still running"
        printf 'removed leftover staging %s (%s bytes)\n' "$name" "${bytes:-0}"
        removed=$(( removed + 1 ))
    done
    (( unexpected == 0 )) || printf 'kept %d unexpected entries in data-side staging\n' "$unexpected"
    find "$backup_dir/.commonplace-staging" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
    printf 'leftover stamps removed: %d\n' "$removed"

    stage="disk precheck"
    local total avail estimate need
    read -r total avail < <(df -B1 --output=size,avail -- "$data_host" | tail -n 1) || true
    [[ "${total:-}" =~ ^[0-9]+$ && "${avail:-}" =~ ^[0-9]+$ ]] || fail "disk precheck: cannot read free space"
    if [[ -z "$reserve" ]]; then
        reserve=$(( total / 20 > 10000000000 ? total / 20 : 10000000000 ))
    fi
    estimate=$({ du -sb --exclude=.backup-staging -- "$data_host" 2>/dev/null || true; } | cut -f1)
    [[ "$estimate" =~ ^[0-9]+$ ]] || fail "disk precheck: cannot estimate data size"
    need=$(( estimate + 10#$margin + 10#$reserve ))
    printf 'disk: estimate %s bytes, free %s bytes, margin %s bytes, reserve %s bytes\n' \
        "$estimate" "$avail" "$margin" "$reserve"
    (( avail >= need )) || fail "disk precheck: free space below estimate + margin + reserve"

    stamp=$(date -u +%Y%m%dT%H%M%S.%NZ)-$$
    [[ "$stamp" =~ $stamp_re ]] || fail "stamp"
    local deadline t output rc=0
    deadline=$(( $(date +%s) + deadline_s ))

    stage="CLI preflight"
    # An image without online backup, or a gateway that does not answer, fails
    # closed here. There is no stop-and-copy fallback. The command substitution
    # is its own bash process that waits for the client, so it closes fd 9 too.
    output=$(exec 3>&- 9>&-; timeout -k 5 120 "${compose[@]}" exec -T commonplace commonplace \
        backup --check-online "/data/.backup-staging/$stamp" </dev/null 2>&1) || rc=$?
    printf '%s\n' "$output"
    (( rc == 0 )) || fail "CLI preflight: $(summary "$output")"

    stage="snapshot"
    hb="$data_staging/$stamp.hb"
    touch "$hb"
    # The snapshot supervisor aborts when this heartbeat goes stale, so a killed
    # hook ends the in-container snapshot without any signal crossing into it.
    # stop_heartbeat TERMs the subshell; its trap also ends the running sleep.
    (
        set +e
        sleep_pid=""
        trap '[[ -z "$sleep_pid" ]] || kill "$sleep_pid" 2>/dev/null; exit 0' TERM
        while kill -0 $$ 2>/dev/null; do
            touch -- "$hb"
            sleep "$hb_every" & sleep_pid=$!
            wait "$sleep_pid"
            sleep_pid=""
        done
    ) 9>&- 3>&- </dev/null & hb_pid=$!
    t=$(( deadline - $(date +%s) + 120 ))
    snapshot_started=1
    printf 'snapshot %s started\n' "$stamp"
    SECONDS=0
    timeout -k 10 "$t" "${compose[@]}" exec -T "${snapshot_env[@]}" commonplace commonplace backup --online --deadline "$deadline" "/data/.backup-staging/$stamp" </dev/null 3>&- 9>&- &
    snap_pid=$!
    rc=0
    wait "$snap_pid" || rc=$?
    snap_pid=""
    local snapshot_tail
    snapshot_tail=$(tail -n 5 -- "$log_file")
    printf 'snapshot exited %d after %d s\n' "$rc" "$SECONDS"

    stage="serve check"
    check_serve_unchanged
    if (( rc == 124 || rc == 137 )); then
        fail "snapshot timed out after $t s"
    fi
    (( rc == 0 )) || fail "snapshot: $(summary "$snapshot_tail")"

    stage="publish snapshot"
    local source="$data_staging/$stamp"
    local host_staging="$backup_dir/.commonplace-staging/$stamp"
    local destination="$backup_dir/commonplace-$stamp"
    [[ -d "$source" && ! -L "$source" ]] || fail "publish snapshot: no committed snapshot"
    [[ ! -e "$host_staging" && ! -e "$destination" ]]
    mv -T -- "$source" "$host_staging"
    stage="verify snapshot"
    [[ -s "$host_staging/control.sqlite3" && -s "$host_staging/manifest.json" ]]
    python3 -c 'import json, sys; json.load(open(sys.argv[1]))' "$host_staging/manifest.json"
    stage="publish snapshot"
    # Both paths share one filesystem: publish atomically only after the
    # in-container commit and these checks succeed.
    mv -T -- "$host_staging" "$destination"
    sync -- "$data_staging" "$backup_dir/.commonplace-staging" "$backup_dir"
    stop_heartbeat
    rm -f -- "$hb"
    if [[ -e "$data_staging/$stamp.lock" ]]; then
        flock -n "$data_staging/$stamp.lock" rm -f -- "$data_staging/$stamp.lock" ||
            printf 'stamp lock still held; the next run removes it\n'
    fi
    printf 'commonplace backup ready: %s\n' "$destination"
}
run_backup >>"$log_file" 2>&1
