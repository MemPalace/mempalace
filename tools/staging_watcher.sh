#!/usr/bin/env bash
# staging_watcher.sh
#
# Watches a staging directory for new files and runs the full
# MemPalace ingest pipeline:
#
#   preprocess → mine → verify → compress → gzip → archive
#
# When files arrive and stabilize (no writes for DEBOUNCE_SECONDS):
#   1. Preprocess: strip boilerplate, split files >4000 lines
#   2. Mine processed files into the palace
#   3. Verify a sample of mined content is searchable
#   4. Compress: run mempalace compress (AAAK dialect)
#   5. Gzip original files and move to archive/YYYY-MM-DD_HHMMSS/
#   6. Write manifest with checksums + file list
#   7. Clear staging (ready for next batch)
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

mkdir -p "$LOG_DIR" "$ARCHIVE_DIR" "$STAGING_DIR"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG_FILE"
}

count_files() {
    find "$STAGING_DIR" -type f \
        ! -name '.DS_Store' ! -name '*.tmp' \
        ! -name 'mempalace.yaml' ! -path '*/processed/*' \
        2>/dev/null | wc -l
}

wait_for_stable() {
    local last_count=-1
    local stable=0
    while [[ $stable -lt $DEBOUNCE_SECONDS ]]; do
        local current_count
        current_count=$(count_files)
        if [[ "$current_count" -eq "$last_count" && "$current_count" -ge $MIN_FILES ]]; then
            stable=$((stable + 5))
        else
            stable=0
            last_count=$current_count
        fi
        sleep 5
    done
}

preprocess_staging() {
    local file_count
    file_count=$(count_files)
    log "Preprocessing $file_count files (strip boilerplate, split >$MAX_LINES lines)..."

    if "$PYTHON_BIN" "$PREPROCESS_SCRIPT" "$STAGING_DIR" --max-lines "$MAX_LINES" >> "$LOG_FILE" 2>&1; then
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
    log "Built batch manifest with $(wc -l < "$manifest" | xargs) processed files"
}

verify_mined() {
    local processed_dir="$STAGING_DIR/processed"
    local manifest="$STAGING_DIR/.batch_manifest"
    local sample_file
    sample_file=$(find "$processed_dir" -type f ! -name 'mempalace.yaml' 2>/dev/null | shuf -n 1)

    if [[ -z "$sample_file" ]]; then
        log "Verify FAILED: no files to sample"
        return 1
    fi

    log "Verify: checking searchability of $sample_file against $manifest"

    if ! "$PYTHON_BIN" "$VERIFY_SCRIPT" "$PALACE_PATH" "$sample_file" "$manifest" "$MEMPALACE_BIN" \
        >> "$LOG_FILE" 2>&1; then
        log "Verify FAILED: mined content not searchable for $sample_file"
        return 1
    fi

    log "Verify: $sample_file searchable and matched in current batch"
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
    mkdir -p "$batch_archive"

    local manifest="$batch_archive/MANIFEST.txt"
    {
        echo "# MemPalace Archive Manifest"
        echo "# Batch: $batch_date"
        echo "# Mined: $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
        echo "# Palace: $PALACE_PATH"
        echo "# Pipeline: preprocess -> mine -> verify -> compress -> gzip -> archive"
        echo ""
    } > "$manifest"

    local file_count=0
    while IFS= read -r -d '' file; do
        local rel_path
        rel_path="${file#$STAGING_DIR/}"
        [[ -z "$rel_path" ]] && continue
        [[ "$rel_path" == .DS_Store || "$rel_path" == mempalace.yaml ]] && continue

        local archive_name="$batch_archive/${rel_path}.gz"
        mkdir -p "$(dirname "$archive_name")"

        # Refuse to overwrite a duplicate archive path (failsafe even though
        # the relative path should now be unique under the staging tree).
        if [[ -e "$archive_name" ]]; then
            log "Archive FAILED: duplicate path would overwrite $archive_name"
            return 1
        fi

        local checksum filesize
        checksum=$(sha256sum "$file" 2>/dev/null | awk '{print $1}')
        if [[ -z "$checksum" ]]; then
            checksum=$(shasum -a 256 "$file" 2>/dev/null | awk '{print $1}')
        fi
        filesize=$(stat -c%s "$file" 2>/dev/null || stat -f%z "$file" 2>/dev/null || echo "0")

        gzip -c "$file" > "$archive_name"

        echo "file: $rel_path" >> "$manifest"
        echo "  sha256: $checksum" >> "$manifest"
        echo "  size: $filesize bytes" >> "$manifest"
        echo "  archived: ${rel_path}.gz" >> "$manifest"
        echo "" >> "$manifest"

        file_count=$((file_count + 1))
    done < <(find "$STAGING_DIR" -type f \
        ! -name '.DS_Store' ! -name 'mempalace.yaml' \
        ! -path '*/processed/*' -print0)

    log "Archived $file_count files to $batch_archive"
}

clear_staging() {
    find "$STAGING_DIR" -type f \
        ! -name '.DS_Store' ! -name 'mempalace.yaml' -delete 2>/dev/null
    rm -rf "$STAGING_DIR/processed" 2>/dev/null || true
    # Remove any empty subdirectories left behind after file deletion.
    find "$STAGING_DIR" -mindepth 1 -type d -empty -not -path "*/processed" -delete 2>/dev/null || true
    log "Staging cleared (ready for next batch)"
}

process_batch() {
    log "=== Processing batch ==="

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
    archive_files
    clear_staging

    log "=== Batch complete ==="
    return 0
}

# ── Main loop ───────────────────────────────────────────────────────────

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
