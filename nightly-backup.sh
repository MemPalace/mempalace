#!/usr/bin/env bash
# Operator-invoked, offline snapshot -> encrypted remote restic -> 30-day retention.
set -Eeuo pipefail
umask 077

stage=setup
run_dir=""
log_file=""
finish() {
    local status=$?
    trap - EXIT
    if [[ -n "$run_dir" ]]; then
        if ! rm -rf -- "$run_dir"; then
            status=1
            stage="$stage; staging cleanup failed"
        fi
    fi
    if (( status != 0 )); then
        printf 'commonplace nightly backup failed: %s (details: %s)\n' \
            "$stage" "${log_file:-no log created}" >&2
        if [[ ${COMMONPLACE_BACKUP_ALERT:-} == /* && -x ${COMMONPLACE_BACKUP_ALERT:-} ]]; then
            if ! timeout 30s "$COMMONPLACE_BACKUP_ALERT" "$stage" "$log_file"; then
                printf 'commonplace backup alert delivery failed\n' >&2
            fi
        fi
    elif [[ -n "$log_file" ]]; then
        rm -f -- "$log_file"
    fi
    exit "$status"
}
trap finish EXIT

[[ $# == 1 && "$1" == /* && -d "$1" ]] || exit 2
backup_dir=$(realpath -- "$1")
repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
log_file=$(mktemp /tmp/commonplace-nightly.XXXXXXXX.log)

stage=configuration
# The operator provisions this encrypted repository separately. Never initialize
# a repository, read credential files, or accept a local backup destination here.
case ${RESTIC_REPOSITORY:-} in
    sftp:*|rest:*|s3:*|b2:*|azure:*|gs:*|rclone:*|swift:*) ;;
    *) exit 2 ;;
esac
[[ ${COMMONPLACE_BACKUP_ALERT:-} == /* && -x ${COMMONPLACE_BACKUP_ALERT:-} ]] || exit 2
backup_id=${COMMONPLACE_BACKUP_ID:-commonplace}
[[ "$backup_id" =~ ^[a-zA-Z0-9._-]+$ ]] || exit 2
command -v restic >/dev/null
command -v timeout >/dev/null

stage=lock
exec 9>"$backup_dir/.commonplace-nightly.lock"
flock -n 9

run_nightly() {
    run_dir=$(mktemp -d "$backup_dir/.commonplace-nightly.XXXXXXXX")
    stage=snapshot
    "$repo_dir/backup-hook.sh" "$run_dir"
    local snapshots=("$run_dir"/commonplace-*)
    [[ ${#snapshots[@]} == 1 && -s "${snapshots[0]}/manifest.json" && \
        -s "${snapshots[0]}/control.sqlite3" ]]
    stage=upload
    restic backup --host "$backup_id" --tag commonplace -- "${snapshots[0]}"
    stage=retention
    # Snapshot paths change every run. Group by identity and tag, not by path,
    # and scope forgetting so unrelated repository snapshots are never pruned.
    restic forget --host "$backup_id" --tag commonplace --group-by host,tags \
        --keep-within 30d --prune
    stage=complete
}
run_nightly >>"$log_file" 2>&1
printf 'commonplace nightly backup completed\n'
