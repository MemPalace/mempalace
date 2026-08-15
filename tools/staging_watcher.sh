#!/usr/bin/env bash
# staging_watcher.sh
#
# Watches a staging directory for new files and runs the full
# MemPalace ingest pipeline:
#
#   preprocess → mine → verify → compress → gzip → archive
#
# When files arrive and stabilize (no writes for DEBOUNCE_SECONDS):
#   1. Claim an immutable batch snapshot.
#   2. Preprocess only the claimed files (strip boilerplate, split >4000 lines).
#   3. Mine processed files into the palace.
#   4. Verify a sample of mined content is searchable.
#   5. Compress: run mempalace compress (AAAK dialect).
#   6. Gzip original files from the batch snapshot and move to archive/YYYY-MM-DD_HHMMSS/.
#   7. Write manifest with checksums + file list.
#   8. Clear only the claimed batch from staging (ready for next batch).
#
# The batch snapshot is captured after the debounce period completes.  Every
# later destructive phase (archive, clear) operates on that exact snapshot and
# verifies that each file is still the same file (size, mtime, sha256) before
# acting on it.  Files that arrive after the snapshot remain in staging for the
# next run.
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
ARCHIVE_DIR="${ARCHIVE_DIR:-$STAGING_DIR/../archive}"
MEMPALACE_BIN="${MEMPALACE_BIN:-mempalace}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
PREPROCESS_SCRIPT="$(dirname "$0")/preprocess_staging.py"
VERIFY_SCRIPT="$(dirname "$0")/verify_mined.py"
LOG_DIR="${LOG_DIR:-$HOME/.mempalace/logs}"
LOG_FILE="$LOG_DIR/staging-watcher.log"
DEBOUNCE_SECONDS="${DEBOUNCE_SECONDS:-30}"
MIN_FILES="${MIN_FILES:-1}"
MAX_LINES="${MAX_LINES:-4000}"
BATCH_SNAPSHOT="${BATCH_SNAPSHOT:-$STAGING_DIR/.batch_snapshot}"

mkdir -p "$LOG_DIR" "$ARCHIVE_DIR" "$STAGING_DIR"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG_FILE"
}

# List all files that should participate in the batch.  Excludes dotfiles,
# .DS_Store, *.tmp, mempalace.yaml, and anything inside a processed/ tree.
find_batch_files() {
    local target="${1:-$STAGING_DIR}"
    find "$target" -type f \
        ! -name '.DS_Store' ! -name '*.tmp' \
        ! -name 'mempalace.yaml' ! -path '*/processed/*' \
        ! -name '.batch_manifest' ! -name '.batch_snapshot' \
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

# Write a TSV snapshot of the current staging tree to stdout.
# Columns: relative_path size mtime sha256 (all paths are relative to target).
snapshot_staging() {
    local target="${1:-$STAGING_DIR}"
    local f rel size mtime hash
    while IFS= read -r -d '' f; do
        rel="${f#$target/}"
        size=$(file_size "$f")
        mtime=$(file_mtime "$f")
        hash=$(file_sha256 "$f")
        # Use the field separator 0x1F (Unit Separator) so paths with spaces
        # and tabs do not break parsing.
        printf '%s\x1f%s\x1f%s\x1f%s\n' "$rel" "$size" "$mtime" "$hash"
    done < <(find "$target" -type f \
        ! -name '.DS_Store' ! -name '*.tmp' \
        ! -name 'mempalace.yaml' ! -path '*/processed/*' \
        ! -name '.batch_snapshot' ! -name '.batch_manifest' \
        -print0 | sort -z)
}

# Return a single hash that represents the current state of the staging tree.
# Any change to file count, paths, sizes, or mtimes produces a different hash.
fingerprint_staging() {
    local target="${1:-$STAGING_DIR}"
    snapshot_staging "$target" | sha256sum | awk '{print $1}'
}

wait_for_stable() {
    local last_fingerprint=""
    local stable=0
    local current_fingerprint
    local current_count
    while [[ $stable -lt $DEBOUNCE_SECONDS ]]; do
        current_fingerprint=$(fingerprint_staging "$STAGING_DIR")
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
claim_batch() {
    local snapshot
    snapshot=$(snapshot_staging "$STAGING_DIR")
    if [[ -z "$snapshot" ]]; then
        log "Claim FAILED: no files to batch"
        return 1
    fi
    printf '%s\n' "$snapshot" > "$BATCH_SNAPSHOT"
    log "Claimed batch: $(grep -c '^' "$BATCH_SNAPSHOT" 2>/dev/null || wc -l < "$BATCH_SNAPSHOT") files -> $BATCH_SNAPSHOT"
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

preprocess_staging() {
    local file_count
    file_count=$(count_files)
    log "Preprocessing $file_count files (strip boilerplate, split >$MAX_LINES lines)..."

    local extra_args=()
    if [[ -s "$BATCH_SNAPSHOT" ]]; then
        extra_args=(--batch-snapshot "$BATCH_SNAPSHOT")
    fi

    if "$PYTHON_BIN" "$PREPROCESS_SCRIPT" "$STAGING_DIR" --max-lines "$MAX_LINES" "${extra_args[@]}" >> "$LOG_FILE" 2>&1; then
        local processed_count
        processed_count=$(find "$STAGING_DIR/processed" -type f ! -name 'mempalace.yaml' 2>/dev/null | wc -l)
        # Copy mempalace.yaml into processed/ so the miner uses correct wing routing
        cp "$STAGING_DIR/mempalace.yaml" "$STAGING_DIR/processed/mempalace.yaml" 2>/dev/null || true
        log "Preprocess complete: $processed_count output files"
        return 0
    else
        local exit_code=$?
        log "Preprocess FAILED (exit $exit_code)"
        return $exit_code
    fi
}

mine_processed() {
    local processed_dir="$STAGING_DIR/processed"
    local file_count
    file_count=$(find "$processed_dir" -type f ! -name 'mempalace.yaml' 2>/dev/null | wc -l)

    if [[ "$file_count" -eq 0 ]]; then
        log "Mine: no processed files to mine — skipping"
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

    log "Mining $file_count processed files..."

    if "$MEMPALACE_BIN" --palace "$PALACE_PATH" mine "$processed_dir" \
        --agent devin --max-chunks-per-file 500 \
        >> "$LOG_FILE" 2>&1; then
        log "Mine complete ($file_count files)"
        return 0
    else
        local exit_code=$?
        log "Mine FAILED (exit $exit_code)"
        return $exit_code
    fi
}

build_batch_manifest() {
    local processed_dir="$STAGING_DIR/processed"
    local manifest="$STAGING_DIR/.batch_manifest"
    find "$processed_dir" -type f ! -name 'mempalace.yaml' -print > "$manifest" 2>/dev/null
    log "Built batch manifest with $(wc -l < "$manifest" 2>/dev/null | xargs) processed files"
}

verify_mined() {
    local processed_dir="$STAGING_DIR/processed"
    local manifest="$STAGING_DIR/.batch_manifest"

    if [[ ! -s "$manifest" ]]; then
        log "Verify FAILED: batch manifest is empty or missing"
        return 1
    fi

    log "Verify: checking searchability of all processed files against $manifest"

    if ! "$PYTHON_BIN" "$VERIFY_SCRIPT" "$PALACE_PATH" "$manifest" "$manifest" "$MEMPALACE_BIN" \
        >> "$LOG_FILE" 2>&1; then
        log "Verify FAILED: one or more processed files are not searchable"
        return 1
    fi

    log "Verify: all processed files are searchable and in the current batch"
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
        echo "# Mined: $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
        echo "# Palace: $PALACE_PATH"
        echo "# Pipeline: preprocess -> mine -> verify -> compress -> gzip -> archive"
        echo ""
    } > "$manifest"

    # Use the claimed batch snapshot when available.  If it does not exist,
    # fall back to the current staging tree for direct function testing and
    # legacy callers.
    local snapshot_file="$BATCH_SNAPSHOT"
    if [[ ! -s "$snapshot_file" ]]; then
        snapshot_file="$STAGING_DIR/.batch_snapshot"
        if [[ ! -s "$snapshot_file" ]]; then
            snapshot_staging "$STAGING_DIR" > "$snapshot_file"
        fi
    fi

    local file_count=0
    local line
    while IFS= read -r line; do
        parse_snapshot_line "$line"
        local rel_path="$_rel"
        local expected_size="$_size"
        local expected_mtime="$_mtime"
        local expected_hash="$_hash"

        [[ -z "$rel_path" ]] && continue
        [[ "$rel_path" == .DS_Store || "$rel_path" == mempalace.yaml ]] && continue
        [[ "$rel_path" == .batch_snapshot || "$rel_path" == .batch_manifest ]] && continue

        local file="$STAGING_DIR/$rel_path"

        if [[ ! -e "$file" ]]; then
            log "Archive SKIP: $rel_path no longer exists"
            continue
        fi

        local current_size current_mtime current_hash
        current_size=$(file_size "$file")
        current_mtime=$(file_mtime "$file")
        current_hash=$(file_sha256 "$file")

        if [[ "$current_size" != "$expected_size" || "$current_mtime" != "$expected_mtime" || "$current_hash" != "$expected_hash" ]]; then
            log "Archive SKIP: $rel_path changed since batch was claimed (size/mtime/hash)"
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
    done < "$snapshot_file"

    if [[ $file_count -eq 0 ]]; then
        log "Archive FAILED: no files to archive"
        rm -rf "$batch_archive_tmp"
        return 1
    fi

    if [[ ! -s "$manifest" ]]; then
        log "Archive FAILED: manifest is empty or missing"
        rm -rf "$batch_archive_tmp"
        return 1
    fi

    if ! mv "$batch_archive_tmp" "$batch_archive"; then
        log "Archive FAILED: could not move temporary archive to $batch_archive"
        rm -rf "$batch_archive_tmp"
        return 1
    fi

    log "Archived $file_count files to $batch_archive"
}

clear_staging() {
    # Delete only the files listed in the batch snapshot and verify identity
    # before deleting.  Files that arrived after the snapshot or that changed
    # while the pipeline ran are left for the next batch.
    local snapshot_file="$BATCH_SNAPSHOT"
    if [[ ! -s "$snapshot_file" ]]; then
        snapshot_file="$STAGING_DIR/.batch_snapshot"
    fi

    if [[ -s "$snapshot_file" ]]; then
        local line
        while IFS= read -r line; do
            parse_snapshot_line "$line"
            local rel_path="$_rel"
            local expected_size="$_size"
            local expected_mtime="$_mtime"
            local expected_hash="$_hash"

            [[ -z "$rel_path" ]] && continue
            [[ "$rel_path" == .DS_Store || "$rel_path" == mempalace.yaml ]] && continue
            [[ "$rel_path" == .batch_snapshot || "$rel_path" == .batch_manifest ]] && continue

            local file="$STAGING_DIR/$rel_path"
            if [[ ! -e "$file" ]]; then
                continue
            fi

            local current_size current_mtime current_hash
            current_size=$(file_size "$file")
            current_mtime=$(file_mtime "$file")
            current_hash=$(file_sha256 "$file")

            if [[ "$current_size" == "$expected_size" && "$current_mtime" == "$expected_mtime" && "$current_hash" == "$expected_hash" ]]; then
                rm -f "$file"
            else
                log "Clear SKIP: $rel_path changed since batch was claimed, leaving for next run"
            fi
        done < "$snapshot_file"
    fi

    rm -rf "$STAGING_DIR/processed" 2>/dev/null || true
    rm -f "$STAGING_DIR/.batch_snapshot" 2>/dev/null || true
    rm -f "$STAGING_DIR/.batch_manifest" 2>/dev/null || true
    find "$STAGING_DIR" -mindepth 1 -type d -empty -not -path "*/processed" -delete 2>/dev/null || true
    log "Staging cleared (ready for next batch)"
}

process_batch() {
    log "=== Processing batch ==="

    if ! claim_batch; then
        log "ABORT: could not claim batch"
        return 1
    fi

    if ! preprocess_staging; then
        log "ABORT: preprocess failed — files left for retry"
        return 1
    fi

    if ! mine_processed; then
        log "ABORT: mine failed — files left for retry"
        return 1
    fi

    build_batch_manifest

    if ! verify_mined; then
        log "ABORT: verify failed — files left for inspection"
        return 1
    fi

    compress_palace

    if ! archive_files; then
        log "ABORT: archive failed — files left for inspection"
        return 1
    fi

    if ! clear_staging; then
        log "ABORT: clear_staging failed — archive is at $ARCHIVE_DIR but staging may be dirty"
        return 1
    fi

    log "=== Batch complete ==="
    return 0
}

# ── Main loop ────────────────────────────────────────────────────────────────

if [[ -z "${STAGING_WATCHER_TEST_MODE:-}" ]]; then
    log "=== staging_watcher started ==="
    log "Watching: $STAGING_DIR"
    log "Archive: $ARCHIVE_DIR"
    log "Palace: $PALACE_PATH"
    log "Pipeline: preprocess -> mine -> verify -> compress -> gzip -> archive"
    log "Debounce: ${DEBOUNCE_SECONDS}s, Max lines: $MAX_LINES"

    while true; do
        while [[ $(count_files) -lt $MIN_FILES ]]; do
            sleep 10
        done

        log "Files detected — waiting for stable period..."
        wait_for_stable
        process_batch
        sleep 5
    done
fi
