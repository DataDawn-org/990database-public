#!/usr/bin/env bash
#
# update.sh — Pull new IRS 990 e-file XMLs, parse into 990data.db,
#              build public copy, upload to Datasette server.
#
# Usage:
#   ./update.sh              # full update
#   ./update.sh --dry-run    # show what would be done without changing anything
#
# Prerequisites:
#   - curl for downloading from IRS website
#   - unzip for extracting ZIP archives
#   - ssh key for your deployment server
#   - Python 3 with lxml
#   - sqlite3 CLI
#
set -euo pipefail

# ── Configuration ──────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
DB="$PROJECT_DIR/990data.db"
PUBLIC_DB="$PROJECT_DIR/990data_public.db"
STATE_FILE="$PROJECT_DIR/.update_state"
LOG_FILE="$PROJECT_DIR/update.log"
EXTRACTED_DIR="$PROJECT_DIR/.extracted"
IRS_BASE_URL="https://apps.irs.gov/pub/epostcard/990/xml"
REMOTE_HOST="${DATADAWN_REMOTE_HOST:?Set DATADAWN_REMOTE_HOST (e.g. user@your-server)}"
REMOTE_INSTALL_DIR="${DATADAWN_REMOTE_DIR:?Set DATADAWN_REMOTE_DIR (e.g. /opt/datasette)}"
REMOTE_DB_PATH="$REMOTE_INSTALL_DIR/990data_public.db"

EXTRACT_CORE="$PROJECT_DIR/extract_990.py"
EXTRACT_PF="$PROJECT_DIR/extract_990pf_detail.py"
EXTRACT_SI="$PROJECT_DIR/extract_schedule_i.py"
EXTRACT_DETAIL="$PROJECT_DIR/extract_990_detail.py"

DRY_RUN=0

# Tables to KEEP in public copy (everything else gets dropped)
PUBLIC_TABLES=(
    bmf
    returns
    grants
    schedule_i_grants
    schedule_i_990
    related_orgs
    officers
    contractors
    contributors
    top_employees
    investments
    program_investments
    capital_gains
    program_activities
)

# ── Helpers ────────────────────────────────────────────────────────────────
log() {
    local ts
    ts=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[$ts] $*" | tee -a "$LOG_FILE"
}

die() {
    log "FATAL: $*"
    exit 1
}

dry() {
    if [[ $DRY_RUN -eq 1 ]]; then
        log "[DRY-RUN] $*"
        return 0
    fi
    return 1
}

elapsed() {
    local start=$1
    local now
    now=$(date +%s)
    echo $(( now - start ))
}

# ── Parse arguments ───────────────────────────────────────────────────────
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        *) die "Unknown argument: $arg" ;;
    esac
done

# ── Pre-flight checks ─────────────────────────────────────────────────────
log "========================================="
log "990 Update Pipeline Starting"
log "========================================="
[[ $DRY_RUN -eq 1 ]] && log "*** DRY-RUN MODE — no changes will be made ***"

command -v curl >/dev/null 2>&1 || die "curl not found"
command -v unzip >/dev/null 2>&1 || die "unzip not found"
command -v python3 >/dev/null 2>&1 || die "python3 not found"
command -v sqlite3 >/dev/null 2>&1 || die "sqlite3 not found"
if ! command -v scp >/dev/null 2>&1; then
    if [[ $DRY_RUN -eq 1 ]]; then
        log "WARNING: scp not found (dry-run will skip upload)"
    else
        die "scp not found"
    fi
fi
[[ -f "$DB" ]]                     || die "Database not found: $DB"
[[ -f "$EXTRACT_CORE" ]]          || die "Extraction script not found: $EXTRACT_CORE"
[[ -f "$EXTRACT_PF" ]]            || die "Extraction script not found: $EXTRACT_PF"
[[ -f "$EXTRACT_SI" ]]            || die "Extraction script not found: $EXTRACT_SI"
[[ -f "$EXTRACT_DETAIL" ]]        || die "Extraction script not found: $EXTRACT_DETAIL"

mkdir -p "$EXTRACTED_DIR"

# ── Step 0: Git backup of server state ───────────────────────────────────
log "--- Step 0: Git backup of current server state ---"
if ! dry "Would commit current server state to git"; then
    BACKUP_MSG="Pre-update backup: $(date '+%Y-%m-%d %H:%M:%S')"
    ssh "$REMOTE_HOST" "cd $REMOTE_INSTALL_DIR && git add -A && { git diff --cached --quiet && echo 'No changes to backup' || { git commit -m \"$BACKUP_MSG\" && git push origin main; }; }" 2>>"$LOG_FILE"
    log "Git backup complete"
fi

# ── Step 1: Determine date range ──────────────────────────────────────────
LAST_UPDATED="2025-01-01"
if [[ -f "$STATE_FILE" ]]; then
    LAST_UPDATED=$(cat "$STATE_FILE")
fi
TODAY=$(date '+%Y-%m-%d')
log "Last update: $LAST_UPDATED"
log "Checking for new filings since $LAST_UPDATED"

# ── Step 2: Download new IRS batches ─────────────────────────────────────
# The IRS publishes 990 e-files at apps.irs.gov/pub/epostcard/990/xml/{YEAR}/
# organized as ZIP files: {YEAR}_TEOS_XML_{BATCH}.zip
# We check which batches exist on the IRS site but not locally.
log "--- Step 2: IRS download ---"

STEP2_START=$(date +%s)
NEW_FILES=0

# Determine which years to check (current and previous, in case of late postings)
CURRENT_YEAR=$(date '+%Y')
PREV_YEAR=$(( CURRENT_YEAR - 1 ))
YEARS_TO_CHECK=("$PREV_YEAR" "$CURRENT_YEAR")

download_batch() {
    local year="$1"
    local zip_name="$2"
    local batch_name="$3"
    local marker="$EXTRACTED_DIR/${batch_name}.done"

    if [[ -f "$marker" ]]; then
        return 0
    fi

    local year_dir="$PROJECT_DIR/$year"
    local batch_dir="$year_dir/$batch_name"
    local zip_path="$year_dir/${zip_name}"
    local url="$IRS_BASE_URL/$year/$zip_name"

    mkdir -p "$year_dir"

    log "  New batch: $batch_name"

    if dry "Would download $url → $batch_dir/"; then
        NEW_FILES=$(( NEW_FILES + 1 ))
        return 0
    fi

    # Download
    log "  Downloading $zip_name..."
    if ! curl -# -L -o "$zip_path" "$url" 2>>"$LOG_FILE"; then
        log "  ERROR: Failed to download $url"
        rm -f "$zip_path"
        return 1
    fi

    # Extract
    mkdir -p "$batch_dir"
    log "  Extracting..."
    if ! unzip -q -o "$zip_path" -d "$batch_dir" 2>>"$LOG_FILE"; then
        log "  ERROR: Failed to extract $zip_path"
        return 1
    fi

    local xml_count
    xml_count=$(find "$batch_dir" -name '*.xml' -type f | wc -l)
    NEW_FILES=$(( NEW_FILES + xml_count ))

    # Clean up ZIP
    rm -f "$zip_path"

    # Mark as done
    touch "$marker"
    log "  Downloaded $xml_count files from $batch_name"
}

for YEAR in "${YEARS_TO_CHECK[@]}"; do
    log "Checking IRS downloads for year $YEAR..."

    # Fetch the index page to discover available batches
    AVAILABLE_ZIPS=$(curl -sL "$IRS_BASE_URL/$YEAR/" 2>/dev/null \
        | grep -oE '[0-9]{4}_TEOS_XML_[0-9A-Za-z]+\.zip' \
        | sort -u || true)

    if [[ -z "$AVAILABLE_ZIPS" ]]; then
        log "  No TEOS_XML batches found for $YEAR"
        continue
    fi

    for ZIP in $AVAILABLE_ZIPS; do
        BATCH_NAME="${ZIP%.zip}"
        download_batch "$YEAR" "$ZIP" "$BATCH_NAME"
    done
done

log "IRS download complete: $NEW_FILES new files ($(elapsed "$STEP2_START")s)"

if [[ $NEW_FILES -eq 0 ]]; then
    log "No new files to process."
    # Still proceed to rebuild public DB in case analysis tables changed
fi

# ── Step 3: Parse new filings into 990data.db ────────────────────────────
log "--- Step 3: Parse new filings ---"
STEP3_START=$(date +%s)

PRE_COUNT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM returns")
log "Returns before: $PRE_COUNT"

if [[ $NEW_FILES -gt 0 ]]; then
    if ! dry "Would run extract_990.py ($NEW_FILES new files)"; then
        log "Running extract_990.py..."
        python3 "$EXTRACT_CORE" 2>>"$LOG_FILE"

        POST_COUNT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM returns")
        NEW_RETURNS=$(( POST_COUNT - PRE_COUNT ))
        log "Returns after: $POST_COUNT (+$NEW_RETURNS new)"

        # Extract 990-PF details for new filings
        if [[ $NEW_RETURNS -gt 0 ]]; then
            log "Running extract_990pf_detail.py..."
            python3 "$EXTRACT_PF" 2>>"$LOG_FILE"
            log "990-PF detail extraction complete"

            log "Running extract_schedule_i.py..."
            python3 "$EXTRACT_SI" 2>>"$LOG_FILE"
            log "Schedule I extraction complete"

            log "Running extract_990_detail.py..."
            python3 "$EXTRACT_DETAIL" 2>>"$LOG_FILE"
            log "990/990EZ detail extraction complete"
        fi
    fi
else
    log "Skipping extraction (no new files)"
fi

log "Parse step complete ($(elapsed "$STEP3_START")s)"

# ── Step 3b: Update documentation row counts ────────────────────────────
log "--- Step 3b: Update documentation ---"
if ! dry "Would update DATABASE_GUIDE.md and CLAUDE.md row counts"; then
    if [[ -x "$PROJECT_DIR/update_guide_counts.sh" ]]; then
        "$PROJECT_DIR/update_guide_counts.sh" 2>>"$LOG_FILE"
        log "Documentation row counts updated"
    else
        log "WARNING: update_guide_counts.sh not found or not executable"
    fi
fi

# ── Step 4: Build public database ─────────────────────────────────────────
log "--- Step 4: Build public database ---"
STEP4_START=$(date +%s)

if dry "Would create $PUBLIC_DB (copy + drop analysis tables + FTS5 + VACUUM)"; then
    # Show what tables would be dropped
    ALL_TABLES=$(sqlite3 "$DB" ".tables" | tr -s ' ' '\n' | sort)
    log "[DRY-RUN] Tables that would be KEPT:"
    for t in "${PUBLIC_TABLES[@]}"; do
        log "  + $t"
    done
    log "[DRY-RUN] Tables that would be DROPPED:"
    for t in $ALL_TABLES; do
        KEEP=0
        for pt in "${PUBLIC_TABLES[@]}"; do
            if [[ "$t" == "$pt" ]]; then
                KEEP=1
                break
            fi
        done
        if [[ $KEEP -eq 0 && -n "$t" ]]; then
            log "  - $t"
        fi
    done
else
    # Copy full database
    log "Copying $DB → $PUBLIC_DB..."
    cp "$DB" "$PUBLIC_DB"

    # Build list of tables to drop (everything not in PUBLIC_TABLES)
    ALL_TABLES=$(sqlite3 "$PUBLIC_DB" "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")

    for TABLE in $ALL_TABLES; do
        KEEP=0
        for PT in "${PUBLIC_TABLES[@]}"; do
            if [[ "$TABLE" == "$PT" ]]; then
                KEEP=1
                break
            fi
        done
        if [[ $KEEP -eq 0 ]]; then
            log "  Dropping: $TABLE"
            sqlite3 "$PUBLIC_DB" "DROP TABLE IF EXISTS \"$TABLE\";"
        fi
    done

    # Build FTS5 indexes
    log "Building FTS5 indexes..."
    sqlite3 "$PUBLIC_DB" <<'SQL'
        -- FTS on returns: search by org name or EIN
        DROP TABLE IF EXISTS fts_returns;
        CREATE VIRTUAL TABLE fts_returns USING fts5(
            org_name,
            ein,
            content=returns,
            content_rowid=rowid
        );
        INSERT INTO fts_returns(fts_returns) VALUES('rebuild');

        -- FTS on grants: search by recipient name
        DROP TABLE IF EXISTS fts_grants;
        CREATE VIRTUAL TABLE fts_grants USING fts5(
            recipient_name,
            content=grants,
            content_rowid=rowid
        );
        INSERT INTO fts_grants(fts_grants) VALUES('rebuild');

        -- FTS on schedule_i_grants: search by recipient name
        DROP TABLE IF EXISTS fts_daf;
        CREATE VIRTUAL TABLE fts_daf USING fts5(
            recipient_name,
            content=schedule_i_grants,
            content_rowid=rowid
        );
        INSERT INTO fts_daf(fts_daf) VALUES('rebuild');

        -- FTS on bmf: search by name, EIN, city, state
        DROP TABLE IF EXISTS fts_bmf;
        CREATE VIRTUAL TABLE fts_bmf USING fts5(
            name,
            ein,
            city,
            state,
            content=bmf,
            content_rowid=rowid
        );
        INSERT INTO fts_bmf(fts_bmf) VALUES('rebuild');
SQL
    log "FTS5 indexes built"

    # Ensure performance indexes exist (safety net — these should carry over
    # from 990data.db, but recreate if missing after table drops or schema changes)
    log "Verifying performance indexes..."
    sqlite3 "$PUBLIC_DB" <<'SQL'
        CREATE INDEX IF NOT EXISTS idx_grants_ein_type     ON grants(ein, grant_type);
        CREATE INDEX IF NOT EXISTS idx_grants_oid_type     ON grants(object_id, grant_type);
        CREATE INDEX IF NOT EXISTS idx_returns_ein_type    ON returns(ein, return_type);
        CREATE INDEX IF NOT EXISTS idx_returns_ein_year_oid ON returns(ein, tax_year DESC, object_id DESC);
        CREATE INDEX IF NOT EXISTS idx_si_recipient_nocase ON schedule_i_grants(recipient_name COLLATE NOCASE);
        CREATE INDEX IF NOT EXISTS idx_si_funder_year_amt  ON schedule_i_grants(funder_ein, tax_year, amount DESC);
        CREATE INDEX IF NOT EXISTS idx_bmf_subsection       ON bmf(subsection);
        CREATE INDEX IF NOT EXISTS idx_bmf_foundation       ON bmf(foundation);
SQL
    log "Performance indexes verified"

    # VACUUM to reclaim space from dropped tables
    log "VACUUMing public database..."
    sqlite3 "$PUBLIC_DB" "VACUUM;"

    PUBLIC_SIZE=$(stat --format="%s" "$PUBLIC_DB" 2>/dev/null || stat -f "%z" "$PUBLIC_DB")
    PUBLIC_SIZE_MB=$(( PUBLIC_SIZE / 1048576 ))
    log "Public database: ${PUBLIC_SIZE_MB}MB"
fi

log "Public DB build complete ($(elapsed "$STEP4_START")s)"

# ── Step 5: Upload to server ──────────────────────────────────────────────
log "--- Step 5: Upload to Datasette server ---"
STEP5_START=$(date +%s)

if dry "Would upload $PUBLIC_DB → $REMOTE_HOST:$REMOTE_DB_PATH"; then
    log "[DRY-RUN] Would upload ${PUBLIC_SIZE_MB:-?}MB to $REMOTE_HOST"
    log "[DRY-RUN] Would restart Datasette"
else
    log "Uploading to $REMOTE_HOST..."
    scp -q "$PUBLIC_DB" "$REMOTE_HOST:$REMOTE_DB_PATH"
    log "Upload complete"

    # Deploy detail page templates and static assets
    log "Deploying templates and static assets..."
    ssh "$REMOTE_HOST" "mkdir -p $REMOTE_INSTALL_DIR/templates/pages/org $REMOTE_INSTALL_DIR/templates/pages/grant $REMOTE_INSTALL_DIR/templates/pages/daf $REMOTE_INSTALL_DIR/templates/pages/filing $REMOTE_INSTALL_DIR/static"
    scp "$PROJECT_DIR/templates/pages/base_datadawn.html" "$REMOTE_HOST:$REMOTE_INSTALL_DIR/templates/pages/base_datadawn.html"
    scp "$PROJECT_DIR/templates/pages/org/{ein}.html" "$REMOTE_HOST:$REMOTE_INSTALL_DIR/templates/pages/org/{ein}.html"
    scp "$PROJECT_DIR/templates/pages/grant/{id}.html" "$REMOTE_HOST:$REMOTE_INSTALL_DIR/templates/pages/grant/{id}.html"
    scp "$PROJECT_DIR/templates/pages/daf/{id}.html" "$REMOTE_HOST:$REMOTE_INSTALL_DIR/templates/pages/daf/{id}.html"
    scp "$PROJECT_DIR/templates/pages/filing/{object_id}.html" "$REMOTE_HOST:$REMOTE_INSTALL_DIR/templates/pages/filing/{object_id}.html"
    scp "$PROJECT_DIR/static/datadawn.css" "$REMOTE_HOST:$REMOTE_INSTALL_DIR/static/datadawn.css"
    # Deploy explore pages
    ssh "$REMOTE_HOST" "mkdir -p $REMOTE_INSTALL_DIR/explore"
    scp -rq "$PROJECT_DIR/explore/"* "$REMOTE_HOST:$REMOTE_INSTALL_DIR/explore/"
    log "Explore pages deployed"
    # Deploy performance plugin
    ssh "$REMOTE_HOST" "mkdir -p $REMOTE_INSTALL_DIR/plugins"
    scp "$PROJECT_DIR/plugins/performance.py" "$REMOTE_HOST:$REMOTE_INSTALL_DIR/plugins/performance.py"
    log "Templates, static assets, and plugins deployed"

    # Update metadata.json with current stats
    log "Updating Datasette metadata..."
    RETURNS_COUNT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM returns")
    GRANTS_COUNT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM grants")
    DAF_COUNT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM schedule_i_grants")
    SCHED_I_COUNT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM schedule_i_990")
    OFFICERS_COUNT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM officers")
    RELATED_COUNT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM related_orgs")
    TAX_YEARS=$(sqlite3 "$DB" "SELECT MIN(tax_year) || '-' || MAX(tax_year) FROM returns WHERE tax_year IS NOT NULL")

    # Format as human-readable (e.g., 5.0M, 12.5M)
    fmt() { awk "BEGIN {v=$1/1000000; if (v>=1) printf \"%.1fM\", v; else printf \"%.0fK\", $1/1000}"; }
    R_FMT=$(echo "" | fmt "$RETURNS_COUNT")
    G_FMT=$(echo "" | fmt "$GRANTS_COUNT")
    D_FMT=$(echo "" | fmt "$DAF_COUNT")
    S_FMT=$(echo "" | fmt "$SCHED_I_COUNT")
    O_FMT=$(echo "" | fmt "$OFFICERS_COUNT")
    RE_FMT=$(echo "" | fmt "$RELATED_COUNT")

    ssh "$REMOTE_HOST" "cat > $REMOTE_INSTALL_DIR/metadata.json" <<METADATA_EOF
{
    "title": "DataDawn 990 Explorer",
    "description_html": "<p>IRS Form 990 nonprofit data: <strong>${R_FMT} returns</strong> (tax years ${TAX_YEARS}), <strong>${G_FMT} foundation grants</strong>, <strong>${D_FMT} DAF disbursements</strong>, <strong>${S_FMT} Schedule I grants</strong>, <strong>${O_FMT} officers/directors</strong>, and <strong>${RE_FMT} related org relationships</strong>.</p>",
    "license": "Public Domain (IRS data)",
    "license_url": "https://www.irs.gov/privacy-disclosure/irs-privacy-policy",
    "plugins": {
        "datasette-cors": {
            "allow_all": true
        }
    }
}
METADATA_EOF
    log "Metadata updated"

    log "Restarting Datasette..."
    ssh "$REMOTE_HOST" 'sudo systemctl restart datasette'
    log "Datasette restarted"

    # Verify Datasette is responding
    sleep 2
    if ssh "$REMOTE_HOST" 'sudo systemctl is-active datasette' >/dev/null 2>&1; then
        log "Datasette is running"
    else
        log "WARNING: Datasette may not have started correctly"
    fi
fi

log "Upload step complete ($(elapsed "$STEP5_START")s)"

# ── Step 6: Update state ─────────────────────────────────────────────────
if ! dry "Would update state file to $TODAY"; then
    echo "$TODAY" > "$STATE_FILE"
    log "State file updated: $TODAY"
fi

# ── Summary ──────────────────────────────────────────────────────────────
log "========================================="
log "Update complete"
FINAL_COUNT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM returns")
log "  Returns in DB:     $FINAL_COUNT"
log "  New files synced:  $NEW_FILES"
if [[ -f "$PUBLIC_DB" ]]; then
    PUBLIC_SIZE=$(stat --format="%s" "$PUBLIC_DB" 2>/dev/null || stat -f "%z" "$PUBLIC_DB")
    log "  Public DB size:    $(( PUBLIC_SIZE / 1048576 ))MB"
fi
log "========================================="

# ── Step 7: Auto-commit server config to git ─────────────────────────────
log "--- Step 7: Git auto-commit ---"
if ! dry "Would auto-commit server config changes"; then
    ssh "$REMOTE_HOST" "cd $REMOTE_INSTALL_DIR && git add -A && git diff --cached --quiet || git commit -m 'Auto: \$(date +%Y-%m-%d) data update' && git push origin main" 2>>"$LOG_FILE"
    log "Git auto-commit complete"
fi
