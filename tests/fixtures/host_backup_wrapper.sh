#!/usr/bin/env bash
# Reduced, synthetic model of an operator's host backup wrapper, which lives
# outside this repository: run the app's backup hook into a staging directory,
# upload the snapshot with restic, apply retention. Every path comes from the
# environment. REQUIRED policy: on error remove the plaintext snapshot and the
# hook's staging, keep only the private log, so the next night can run.
set -Eeuo pipefail
umask 077
: "${BACKUP_HOOK:?}" "${STAGE:?}" "${LOG_DIR:?}" "${BACKUP_ID:?}"
log=$(mktemp "$LOG_DIR/host-backup.XXXXXXXX.log")

on_error() {
    local status=$?
    trap - EXIT
    if (( status != 0 )); then
        rm -rf -- "$STAGE"/commonplace-* "$STAGE/.commonplace-staging" || status=1
        printf 'host backup failed (details: %s)\n' "$log" >&2
    else
        rm -f -- "$log"
    fi
    exit "$status"
}
trap on_error EXIT

mkdir -p -- "$STAGE"
"$BACKUP_HOOK" "$STAGE" >>"$log" 2>&1
snapshots=("$STAGE"/commonplace-*)
[[ -d ${snapshots[0]} ]]
restic backup --host "$BACKUP_ID" --tag commonplace -- "${snapshots[@]}" >>"$log" 2>&1
restic forget --host "$BACKUP_ID" --tag commonplace --keep-within 30d --prune >>"$log" 2>&1
rm -rf -- "${snapshots[@]}" "$STAGE/.commonplace-staging"
