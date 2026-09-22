#!/usr/bin/env bash
# staging_watcher.sh
#
# Watches a staging directory for new files and runs the full
# MemPalace ingest pipeline:
#
#   stage (verbatim) → mine → verify → compress → archive → clear
#
# When files arrive and stabilize (no writes for DEBOUNCE_SECONDS):
#   1. Claim an immutable batch snapshot (size, mtime, sha256 per file) and
#      copy every claimed file into a private work dir, verifying hashes.
#   2. Stage VERBATIM copies into a per-batch mine dir. MemPalace stores
#      content verbatim — the only transformation is splitting files over
#      MAX_LINES into numbered parts on line boundaries. Unprocessable
#      files (dotfiles, binary extensions, generated dirs) are skipped.
#   3. Mine the batch dir into the palace. The batch dir is keyed by the
#      batch content hash, so mined source_file paths are unique per
#      batch: a later batch's same-named file can never purge an earlier
#      batch's drawers, and a retry of the same batch re-mines in place.
#   4. Verify each mined file is searchable under its own source_file.
#   5. Compress: run mempalace compress (AAAK dialect).
#   6. Archive ONLY the files that were actually mined: gzip the verified
#      work copies into archive/YYYY-MM-DD_HHMMSS/ via atomic rename.
#      Skipped files are never archived — the archive means "this content
#      is in the palace".
#   7. Quarantine skipped files under staging/.mp_quarantine/<batch_id>/
#      (moved, never deleted) and clear mined files by moving them into
#      the private work dir — never unlinking a live staging path.
#
# The batch snapshot is captured after the debounce period completes. Every
# later phase operates on that exact snapshot and verifies that each file
# is still the same file (size, mtime, sha256) before acting on it. Files
# that arrive after the snapshot remain in staging for the next run.
#
# Usage:
#   ./staging_watcher.sh /path/to/staging /path/to/palace
#
# Or with environment variables:
#   STAGING_DIR=/path/to/staging PALACE_PATH=/path/to/palace ./staging_watcher.sh
#
# For auto-start on boot, use @reboot in crontab or a systemd service.

set -uo pipefail

STAGING_DIR="${1:-${STAGING_DIR:-/tmp/mempalace-staging}}"
PALACE_PATH="${2:-${PALACE_PATH:-$HOME/.mempalace/palace}}"
# Normalize STAGING_DIR to an absolute, slash-free-tailing path so we can
# safely derive archive names by stripping the prefix from file paths.
STAGING_DIR=$(cd "$STAGING_DIR" && pwd)
ARCHIVE_DIR="${ARCHIVE_DIR:-$HOME/.mempalace/archive}"
MEMPALACE_BIN="${MEMPALACE_BIN:-mempalace}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
PREPROCESS_SCRIPT="$(dirname "$0")/preprocess_staging.py"
VERIFY_SCRIPT="$(dirname "$0")/verify_mined.py"
LOG_DIR="${LOG_DIR:-$HOME/.mempalace/logs}"
LOG_FILE="$LOG_DIR/staging-watcher.log"
DEBOUNCE_SECONDS="${DEBOUNCE_SECONDS:-30}"
MIN_FILES="${MIN_FILES:-1}"
MAX_LINES="${MAX_LINES:-4000}"
# Agent tag recorded on mined drawers.
MINE_AGENT="${MINE_AGENT:-staging-watcher}"
# Work directory and snapshot live OUTSIDE the watched staging tree so the
# watcher never sees its own private files as a new batch.
WORK_ROOT="${WORK_ROOT:-$(mktemp -d -t mempalace-batch-XXXXXX)}"
BATCH_SNAPSHOT="${BATCH_SNAPSHOT:-$WORK_ROOT/.batch_snapshot}"
BATCH_ID=""
BATCH_DIR=""
WORK_ORIG=""
MINE_DIR=""
RETIRED_DIR=""

mkdir -p "$LOG_DIR" "$ARCHIVE_DIR" "$STAGING_DIR"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG_FILE"
}

# List all files that should participate in the batch. Excludes dotfiles and
# dot-directories at any depth (covers .mp_quarantine and the watcher's own
# namespaced dirs), .DS_Store, *.tmp, and mempalace.yaml. No name-based
# exclusions on ordinary directories — a user dir named "processed" is
# content like any other.
find_batch_files() {
    local target="${1:-$STAGING_DIR}"
    find "$target" -type f \
        ! -path '*/.*' \
        ! -name '.DS_Store' ! -name '*.tmp' \
        ! -name 'mempalace.yaml' \
        2>/dev/null
}

# Return the size of a file in bytes.  Works on GNU and BSD stat.
file_size() {
    local f="$1"
    local size
    size=$(stat -c%s "$f" 2>/dev/null) || size=$(stat -f%z "$f" 2>/dev/null) || size=""
    printf '%s' "$size"
}

# Return the mtime of a file as a Unix timestamp.  Works on GNU and BSD stat.
file_mtime() {
    local f="$1"
    local mtime
    mtime=$(stat -c%Y "$f" 2>/dev/null) || mtime=$(stat -f%m "$f" 2>/dev/null) || mtime=""
    printf '%s' "$mtime"
}

# Return the sha256 of a file.  Prefers sha256sum, falls back to shasum.
file_sha256() {
    local f="$1"
    local hash
    hash=$(sha256sum "$f" 2>/dev/null | awk '{print $1}')
    if [[ -z "$hash" ]]; then
        hash=$(shasum -a 256 "$f" 2>/dev/null | awk '{print $1}')
    fi
    printf '%s' "$hash"
}

count_files() {
    find_batch_files "$STAGING_DIR" | wc -l
}

# Write a snapshot of the current staging tree to stdout.
# Columns: relative_path <US> size <US> mtime <US> sha256 (unit separator
# so paths with spaces and tabs do not break parsing).
snapshot_staging() {
    local target="${1:-$STAGING_DIR}"
    local f rel size mtime hash
    while IFS= read -r -d '' f; do
        rel="${f#$target/}"
        size=$(file_size "$f")
        mtime=$(file_mtime "$f")
        hash=$(file_sha256 "$f")
        printf '%s\x1f%s\x1f%s\x1f%s\n' "$rel" "$size" "$mtime" "$hash"
    done < <(find "$target" -type f \
        ! -path '*/.*' \
        ! -name '.DS_Store' ! -name '*.tmp' \
        ! -name 'mempalace.yaml' \
        -print0 | sort -z)
}

# Hash stdin with sha256.  Tries sha256sum then shasum -a 256.
# Fails closed (returns empty) if neither is available.
hash_stdin() {
    local hash
    hash=$(sha256sum 2>/dev/null | awk '{print $1}')
    if [[ -z "$hash" ]]; then
        hash=$(shasum -a 256 2>/dev/null | awk '{print $1}')
    fi
    printf '%s' "$hash"
}

# Return a single hash that represents the current state of the staging tree.
# Any change to file count, paths, sizes, or mtimes produces a different hash.
fingerprint_staging() {
    local target="${1:-$STAGING_DIR}"
    local fp
    fp=$(snapshot_staging "$target" | hash_stdin)
    if [[ -z "$fp" ]]; then
        # No SHA-256 command available — fail closed so a changing tree
        # is never treated as stable.
        printf 'ERROR_NO_SHA256'
        return 1
    fi
    printf '%s' "$fp"
}

wait_for_stable() {
    local last_fingerprint=""
    local stable=0
    local current_fingerprint
    local current_count
    while [[ $stable -lt $DEBOUNCE_SECONDS ]]; do
        current_fingerprint=$(fingerprint_staging "$STAGING_DIR")
        # A missing sha256 tool yields the sentinel — never compare it
        # against last_fingerprint or an error string looks "stable".
        if [[ "$current_fingerprint" == "ERROR_NO_SHA256" || -z "$current_fingerprint" ]]; then
            sleep 5
            continue
        fi
        current_count=$(count_files)
        if [[ "$current_fingerprint" == "$last_fingerprint" && "$current_count" -ge $MIN_FILES ]]; then
            stable=$((stable + 5))
        else
            stable=0
            last_fingerprint="$current_fingerprint"
        fi
        sleep 5
    done
}

# Capture an immutable snapshot of the current stable batch and persist it.
# The snapshot is the source of truth for archive and clear operations.
# BATCH_ID is content-addressed (the snapshot fingerprint), so retrying the
# same file set re-uses the same mine paths — the miner purges by
# source_file, which makes a retry an idempotent replace instead of a
# duplicate insert.
claim_batch() {
    local snapshot
    snapshot=$(snapshot_staging "$STAGING_DIR")
    if [[ -z "$snapshot" ]]; then
        log "Claim FAILED: no files to batch"
        return 1
    fi
    printf '%s\n' "$snapshot" > "$BATCH_SNAPSHOT"

    local fp
    fp=$(printf '%s' "$snapshot" | hash_stdin)
    if [[ -z "$fp" ]]; then
        log "Claim FAILED: no sha256 tool available for batch id"
        return 1
    fi
    BATCH_ID="${fp:0:16}"
    BATCH_DIR="$WORK_ROOT/batch-$BATCH_ID"
    WORK_ORIG="$BATCH_DIR/orig"
    MINE_DIR="$BATCH_DIR/mine"
    RETIRED_DIR="$BATCH_DIR/retired"
    mkdir -p "$WORK_ORIG" "$MINE_DIR" "$RETIRED_DIR"

    # Copy ALL claimed files into the batch work dir with sha256 verification.
    # This gives later phases an immutable copy that cannot be raced by a
    # producer replacing the live staging file.
    local line
    while IFS= read -r line; do
        parse_snapshot_line "$line"
        local rel_path="$_rel"
        local expected_hash="$_hash"
        [[ -z "$rel_path" ]] && continue
        local src="$STAGING_DIR/$rel_path"
        [[ ! -e "$src" ]] && continue
        local dst="$WORK_ORIG/$rel_path"
        mkdir -p "$(dirname "$dst")"
        cp -p "$src" "$dst"
        local copied_hash
        copied_hash=$(file_sha256 "$dst")
        if [[ "$copied_hash" != "$expected_hash" ]]; then
            log "Claim SKIP: $rel_path changed during claim (hash mismatch)"
            rm -f "$dst"
        fi
    done < "$BATCH_SNAPSHOT"
    log "Claimed batch $BATCH_ID: $(wc -l < "$BATCH_SNAPSHOT" | xargs) files -> $BATCH_SNAPSHOT"
    return 0
}

# Read one snapshot record into the caller's variables: _rel, _size, _mtime, _hash.
# $1 = the unit-separator-delimited line.
parse_snapshot_line() {
    local line="$1"
    _rel=$(printf '%s' "$line" | cut -d $'\x1f' -f1)
    _size=$(printf '%s' "$line" | cut -d $'\x1f' -f2)
    _mtime=$(printf '%s' "$line" | cut -d $'\x1f' -f3)
    _hash=$(printf '%s' "$line" | cut -d $'\x1f' -f4)
}

# Stage verified work copies into MINE_DIR — verbatim bytes, splitting only.
# Writes $BATCH_DIR/mined.manifest (orig<US>mined pairs) and skipped.manifest.
preprocess_staging() {
    log "Staging batch $BATCH_ID for mining (verbatim; split >$MAX_LINES lines)..."

    if "$PYTHON_BIN" "$PREPROCESS_SCRIPT" "$STAGING_DIR" \
        --work-dir "$WORK_ORIG" --batch-snapshot "$BATCH_SNAPSHOT" \
        --out-dir "$MINE_DIR" --batch-id "$BATCH_ID" \
        --manifest "$BATCH_DIR/mined.manifest" \
        --skip-manifest "$BATCH_DIR/skipped.manifest" \
        --max-lines "$MAX_LINES" >> "$LOG_FILE" 2>&1; then
        local mined_count
        mined_count=$(wc -l < "$BATCH_DIR/mined.manifest" 2>/dev/null | xargs)
        # Copy mempalace.yaml into the mine dir so the miner uses correct
        # wing routing (the miner skips the file itself by name).
        cp "$STAGING_DIR/mempalace.yaml" "$MINE_DIR/mempalace.yaml" 2>/dev/null || true
        log "Stage complete: $mined_count mine-ready file(s)"
        return 0
    else
        local exit_code=$?
        log "Stage FAILED (exit $exit_code)"
        return $exit_code
    fi
}

mine_processed() {
    local file_count
    file_count=$(find "$MINE_DIR" -type f ! -name 'mempalace.yaml' 2>/dev/null | wc -l)

    if [[ "$file_count" -eq 0 ]]; then
        log "Mine: no staged files to mine — skipping"
        return 0
    fi

    # Wait for any existing mining process to finish (palace lock)
    local lock_holder
    lock_holder=$(pgrep -f "mempalace.*mine" 2>/dev/null | head -1)
    if [[ -n "$lock_holder" ]]; then
        log "Mine: another mining process is running (PID $lock_holder) — waiting..."
        local wait_count=0
        while [[ -n "$(pgrep -f 'mempalace.*mine' 2>/dev/null)" ]] && [[ $wait_count -lt 360 ]]; do
            sleep 10
            wait_count=$((wait_count + 1))
        done
        if [[ -n "$(pgrep -f 'mempalace.*mine' 2>/dev/null)" ]]; then
            log "Mine: timed out waiting — aborting this batch"
            return 1
        fi
        log "Mine: previous mining finished — proceeding"
    fi

    log "Mining $file_count staged files..."

    if "$MEMPALACE_BIN" --palace "$PALACE_PATH" mine "$MINE_DIR" \
        --agent "$MINE_AGENT" --max-chunks-per-file 500 \
        >> "$LOG_FILE" 2>&1; then
        log "Mine complete ($file_count files)"
        return 0
    else
        local exit_code=$?
        log "Mine FAILED (exit $exit_code)"
        return $exit_code
    fi
}

verify_mined() {
    local manifest="$BATCH_DIR/mined.manifest"

    if [[ ! -s "$manifest" ]]; then
        # Nothing was mined — nothing to verify. Quarantine still runs so
        # unprocessable files stop re-triggering the batch.
        log "Verify: manifest empty (nothing mined) — skipping search checks"
        return 0
    fi

    log "Verify: checking searchability of mined files under $MINE_DIR"

    if ! "$PYTHON_BIN" "$VERIFY_SCRIPT" \
        --palace "$PALACE_PATH" \
        --manifest "$manifest" --mine-dir "$MINE_DIR" \
        --snapshot "$BATCH_SNAPSHOT" --work-dir "$WORK_ORIG" \
        --all >> "$LOG_FILE" 2>&1; then
        log "Verify FAILED: one or more mined files are not searchable"
        return 1
    fi

    log "Verify: all mined files are searchable under this batch's source paths"
    return 0
}

compress_palace() {
    log "Compressing palace (AAAK dialect)..."
    if "$MEMPALACE_BIN" --palace "$PALACE_PATH" compress >> "$LOG_FILE" 2>&1; then
        log "Compress complete"
    else
        log "Compress FAILED (exit $?) — continuing (non-fatal)"
    fi
    return 0
}

# Iterate the ORIG rel paths that produced mined output (col 1 of the mined
# manifest, deduplicated — a split file appears once per part).
mined_orig_rels() {
    local manifest="$BATCH_DIR/mined.manifest"
    [[ -s "$manifest" ]] || return 0
    cut -d $'\x1f' -f1 "$manifest" | sort -u
}

archive_files() {
    local batch_date
    batch_date=$(date '+%Y-%m-%d_%H%M%S')
    local batch_archive="$ARCHIVE_DIR/$batch_date"
    local batch_archive_tmp="$ARCHIVE_DIR/.tmp.$batch_date.$$"

    # Build into a temporary directory and atomically rename on success.
    rm -rf "$batch_archive_tmp"
    mkdir -p "$batch_archive_tmp"

    local manifest="$batch_archive_tmp/MANIFEST.txt"
    {
        echo "# MemPalace Archive Manifest"
        echo "# Batch: $batch_date"
        echo "# Batch ID: $BATCH_ID"
        echo "# Mined: $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
        echo "# Palace: $PALACE_PATH"
        echo "# Pipeline: stage -> mine -> verify -> compress -> gzip -> archive"
        echo ""
    } > "$manifest"

    local file_count=0
    local rel_path
    while IFS= read -r rel_path; do
        [[ -z "$rel_path" ]] && continue

        # Archive the verified work copy — the live staging file may have
        # been replaced since the claim.
        local file="$WORK_ORIG/$rel_path"
        if [[ ! -e "$file" ]]; then
            log "Archive SKIP: $rel_path has no verified work copy"
            continue
        fi

        local expected_hash current_size current_mtime current_hash
        expected_hash=$(grep -F "$rel_path" "$BATCH_SNAPSHOT" | head -1 | cut -d $'\x1f' -f4)
        current_size=$(file_size "$file")
        current_mtime=$(file_mtime "$file")
        current_hash=$(file_sha256 "$file")

        if [[ -n "$expected_hash" && "$current_hash" != "$expected_hash" ]]; then
            log "Archive SKIP: $rel_path work copy no longer matches snapshot"
            continue
        fi

        local archive_name="$batch_archive_tmp/${rel_path}.gz"
        mkdir -p "$(dirname "$archive_name")"

        # Refuse to overwrite a duplicate archive path.
        if [[ -e "$archive_name" ]]; then
            log "Archive FAILED: duplicate path would overwrite $archive_name"
            rm -rf "$batch_archive_tmp"
            return 1
        fi

        if ! gzip -c "$file" > "$archive_name"; then
            log "Archive FAILED: gzip error for $file"
            rm -rf "$batch_archive_tmp"
            return 1
        fi

        # Validate the compressed file before committing the manifest entry.
        if ! gunzip -t "$archive_name" >/dev/null 2>&1; then
            log "Archive FAILED: gzip validation failed for $archive_name"
            rm -rf "$batch_archive_tmp"
            return 1
        fi

        echo "file: $rel_path" >> "$manifest"
        echo "  sha256: $current_hash" >> "$manifest"
        echo "  size: $current_size bytes" >> "$manifest"
        echo "  archived: ${rel_path}.gz" >> "$manifest"
        echo "" >> "$manifest"

        file_count=$((file_count + 1))
    done < <(mined_orig_rels)

    if [[ $file_count -eq 0 ]]; then
        log "Archive: no mined files to archive"
        rm -rf "$batch_archive_tmp"
        return 0
    fi

    if ! mv "$batch_archive_tmp" "$batch_archive"; then
        log "Archive FAILED: could not move temporary archive to $batch_archive"
        rm -rf "$batch_archive_tmp"
        return 1
    fi

    log "Archived $file_count files to $batch_archive"
}

# Move skipped (unprocessable) files out of the scan set, preserving them
# under staging/.mp_quarantine/<batch_id>/ — moved, never deleted.
quarantine_skipped() {
    local skip_manifest="$BATCH_DIR/skipped.manifest"
    [[ -s "$skip_manifest" ]] || return 0

    local moved=0
    local line rel_path
    while IFS= read -r line; do
        rel_path="${line%%$'\x1f'*}"
        [[ -z "$rel_path" ]] && continue
        local file="$STAGING_DIR/$rel_path"
        [[ ! -e "$file" ]] && continue

        # Only quarantine the file we actually claimed — a file that changed
        # since the snapshot is a new drop and stays for re-evaluation.
        local expected_hash current_hash
        expected_hash=$(grep -F "$rel_path" "$BATCH_SNAPSHOT" | head -1 | cut -d $'\x1f' -f4)
        current_hash=$(file_sha256 "$file")
        if [[ -n "$expected_hash" && "$current_hash" != "$expected_hash" ]]; then
            log "Quarantine SKIP: $rel_path changed since claim — leaving in staging"
            continue
        fi

        local dst="$STAGING_DIR/.mp_quarantine/$BATCH_ID/$rel_path"
        mkdir -p "$(dirname "$dst")"
        mv "$file" "$dst"
        moved=$((moved + 1))
    done < "$skip_manifest"

    [[ $moved -gt 0 ]] && log "Quarantined $moved unprocessable file(s) to staging/.mp_quarantine/$BATCH_ID/"
    return 0
}

clear_staging() {
    # Clear only files that were actually mined (they are now safely in the
    # palace AND archived). Verify the live file still matches the snapshot,
    # then MOVE it into the private work dir rather than unlinking a live
    # staging path — mv is atomic with respect to path replacement, so a
    # producer that swapped the file mid-pipeline loses nothing.
    local rel_path
    while IFS= read -r rel_path; do
        [[ -z "$rel_path" ]] && continue

        local file="$STAGING_DIR/$rel_path"
        if [[ ! -e "$file" ]]; then
            continue
        fi

        local expected_hash current_hash
        expected_hash=$(grep -F "$rel_path" "$BATCH_SNAPSHOT" | head -1 | cut -d $'\x1f' -f4)
        current_hash=$(file_sha256 "$file")

        if [[ -n "$expected_hash" && "$current_hash" == "$expected_hash" ]]; then
            local dst="$RETIRED_DIR/$rel_path"
            mkdir -p "$(dirname "$dst")"
            mv "$file" "$dst"
        else
            log "Clear SKIP: $rel_path changed since batch was claimed, leaving for next run"
        fi
    done < <(mined_orig_rels)

    # Remove empty dirs left behind — excluding the quarantine tree.
    find "$STAGING_DIR" -mindepth 1 -type d -empty \
        -not -path "*/.mp_quarantine*" -delete 2>/dev/null || true
    log "Staging cleared (ready for next batch)"
}

process_batch() {
    log "=== Processing batch ==="

    if ! claim_batch; then
        log "ABORT: could not claim batch"
        return 1
    fi

    if ! preprocess_staging; then
        log "ABORT: staging failed — files left for retry"
        return 1
    fi

    if ! mine_processed; then
        log "ABORT: mine failed — files left for retry"
        return 1
    fi

    if ! verify_mined; then
        log "ABORT: verify failed — files left for inspection"
        return 1
    fi

    compress_palace

    if ! archive_files; then
        log "ABORT: archive failed — files left for inspection"
        return 1
    fi

    quarantine_skipped

    clear_staging

    log "=== Batch complete ==="
    return 0
}

# ── Main loop ────────────────────────────────────────────────────────────────

if [[ -z "${STAGING_WATCHER_TEST_MODE:-}" ]]; then
    log "=== staging_watcher started ==="
    log "Watching: $STAGING_DIR"
    log "Archive: $ARCHIVE_DIR"
    log "Palace: $PALACE_PATH"
    log "Pipeline: stage -> mine -> verify -> compress -> gzip -> archive"
    log "Debounce: ${DEBOUNCE_SECONDS}s, Max lines: $MAX_LINES"

    while true; do
        while [[ $(count_files) -lt $MIN_FILES ]]; do
            sleep 10
        done

        log "Detected $(count_files) files — waiting for stabilization..."
        wait_for_stable

        process_batch
    done
fi
