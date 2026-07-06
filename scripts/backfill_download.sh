#!/usr/bin/env bash
#
# backfill_download.sh — Download and extract all missing IRS 990 XML batches
#
# Downloads from https://apps.irs.gov/pub/epostcard/990/xml/
# Extracts XML files to /mnt/data/datadawn/990project/{YEAR}/{BATCH}/
# Marks completed batches in .extracted/
#
set -euo pipefail

PROJECT_DIR="/mnt/data/datadawn/990project"
EXTRACTED_DIR="$PROJECT_DIR/.extracted"
BASE_URL="https://apps.irs.gov/pub/epostcard/990/xml"
LOG_FILE="$PROJECT_DIR/backfill_download.log"

mkdir -p "$EXTRACTED_DIR"

log() {
    local ts
    ts=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[$ts] $*" | tee -a "$LOG_FILE"
}

download_and_extract() {
    local year="$1"
    local zip_name="$2"
    local batch_name="$3"  # name for the extracted directory and .done marker

    local marker="$EXTRACTED_DIR/${batch_name}.done"
    if [[ -f "$marker" ]]; then
        log "  SKIP: $batch_name (already extracted)"
        return 0
    fi

    local year_dir="$PROJECT_DIR/$year"
    local batch_dir="$year_dir/$batch_name"
    local zip_path="$year_dir/${zip_name}"
    local url="$BASE_URL/$year/$zip_name"

    mkdir -p "$year_dir"

    # Download
    log "  Downloading $zip_name..."
    if ! curl -# -L -o "$zip_path" "$url" >>"$LOG_FILE" 2>&1; then
        log "  ERROR: Failed to download $url"
        rm -f "$zip_path"
        return 1
    fi

    # Verify it's a valid ZIP
    if ! file "$zip_path" | grep -q "Zip\|archive"; then
        local ftype
        ftype=$(file "$zip_path")
        log "  WARNING: $zip_name may not be a valid ZIP: $ftype"
        # Still try to extract
    fi

    # Count what the ZIP claims to hold BEFORE we extract.
    local declared_count
    declared_count=$(unzip -l "$zip_path" 2>/dev/null | grep -cE '\.xml$' || echo 0)

    # Extract
    mkdir -p "$batch_dir"
    log "  Extracting to $batch_dir..."
    if ! unzip -q -o "$zip_path" -d "$batch_dir" >>"$LOG_FILE" 2>&1; then
        log "  ERROR: Failed to extract $zip_path"
        return 1
    fi

    # Count extracted XMLs
    local xml_count
    xml_count=$(find "$batch_dir" -name '*.xml' -type f | wc -l)
    log "  Extracted $xml_count XML files"

    # Integrity check (same as update.sh download_batch): partial extracts
    # would otherwise mark .done with missing files — DAF-incident class.
    if [[ "$declared_count" -gt 0 && "$xml_count" -lt "$declared_count" ]]; then
        log "  ERROR: extracted $xml_count XML files but ZIP declared $declared_count — partial extract, NOT marking complete"
        return 1
    fi

    # Clean up ZIP to save space (last, AFTER integrity check)
    rm -f "$zip_path"

    # Mark as done
    touch "$marker"
    log "  DONE: $batch_name ($xml_count files)"
}

# ── Main ──────────────────────────────────────────────────────────────────
log "========================================="
log "IRS 990 Backfill Download Starting"
log "========================================="

TOTAL_START=$(date +%s)

# ── 2017 Batches ──────────────────────────────────────────────────────────
log "--- Year 2017 ---"
download_and_extract "2017" "2017_TEOS_XML_CT1.zip" "2017_TEOS_XML_CT1"
for i in $(seq 1 7); do
    download_and_extract "2017" "download990xml_2017_${i}.zip" "download990xml_2017_${i}"
done

# ── 2018 Batches ──────────────────────────────────────────────────────────
log "--- Year 2018 ---"
download_and_extract "2018" "2018_TEOS_XML_CT1.zip" "2018_TEOS_XML_CT1"
for i in $(seq 1 7); do
    download_and_extract "2018" "download990xml_2018_${i}.zip" "download990xml_2018_${i}"
done

# ── 2019 Batches ──────────────────────────────────────────────────────────
log "--- Year 2019 ---"
download_and_extract "2019" "2019_TEOS_XML_CT1.zip" "2019_TEOS_XML_CT1"
for i in $(seq 1 8); do
    download_and_extract "2019" "download990xml_2019_${i}.zip" "download990xml_2019_${i}"
done

# ── 2020 Batches ──────────────────────────────────────────────────────────
log "--- Year 2020 ---"
download_and_extract "2020" "2020_TEOS_XML_CT1.zip" "2020_TEOS_XML_CT1"
for i in $(seq 1 8); do
    download_and_extract "2020" "download990xml_2020_${i}.zip" "download990xml_2020_${i}"
done

# ── 2021 Batch ────────────────────────────────────────────────────────────
log "--- Year 2021 ---"
download_and_extract "2021" "2021_TEOS_XML_01A.zip" "2021_TEOS_XML_01A"

# ── 2022 Batch ────────────────────────────────────────────────────────────
log "--- Year 2022 ---"
download_and_extract "2022" "2022_TEOS_XML_01A.zip" "2022_TEOS_XML_01A"

# ── 2026 Batch ────────────────────────────────────────────────────────────
log "--- Year 2026 ---"
download_and_extract "2026" "2026_TEOS_XML_01A.zip" "2026_TEOS_XML_01A"

# ── Summary ──────────────────────────────────────────────────────────────
TOTAL_ELAPSED=$(( $(date +%s) - TOTAL_START ))
log "========================================="
log "Backfill download complete in ${TOTAL_ELAPSED}s"

for year in 2017 2018 2019 2020 2021 2022 2026; do
    if [[ -d "$PROJECT_DIR/$year" ]]; then
        count=$(find "$PROJECT_DIR/$year" -name '*.xml' -type f | wc -l)
        log "  $year: $count XML files"
    fi
done

log "========================================="
