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
#   - ssh key for user@YOUR_SERVER_IP
#   - Python 3 with lxml
#   - sqlite3 CLI
#
set -euo pipefail

# ── Configuration ──────────────────────────────────────────────────────────
PROJECT_DIR="/mnt/data/datadawn/990project"
DB="$PROJECT_DIR/990data.db"
PUBLIC_DB="$PROJECT_DIR/990data_public.db"
STATE_FILE="$PROJECT_DIR/.update_state"
LOG_FILE="$PROJECT_DIR/update.log"
EXTRACTED_DIR="$PROJECT_DIR/.extracted"
IRS_BASE_URL="https://apps.irs.gov/pub/epostcard/990/xml"
REMOTE_HOST="user@YOUR_SERVER_IP"
REMOTE_DB_PATH="/opt/datasette/990data_public.db"
REMOTE_BACKUP_DIR="/opt/datasette/backups"
LOCAL_BACKUP_DIR="$PROJECT_DIR/backups"
B2_REMOTE="b2:someones-backup/990-weekly"
ROTATE_HELPER="/mnt/data/datadawn/openregs/deploy/rotate_local_backups.py"

# SSH keepalive + timeout options applied to every ssh/scp/rsync call below.
# Codified 2026-05-02 mirroring openregs/deploy/deploy.sh — without
# ServerAliveInterval, an idle ssh session whose TCP path goes silent (NAT
# timeout, transient blip) hangs the local client for ~2 hours before OS
# keepalive fires. With these, 3 missed probes over 3 minutes severs a dead
# connection. See bestpractices/incident_log.md "2026-05-02 deploy hang".
SSH_OPTS="-o ConnectTimeout=15 -o ServerAliveInterval=60 -o ServerAliveCountMax=3"

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

# ── 3-tier backup propagation (post-deploy, non-critical) ────────────────
#
# Mirrors openregs/deploy/deploy.sh propagate_backup_to_local_and_b2().
# Called after a successful 990 upload. Takes the predeploy snapshot that
# was written to the VPS (cp ${REMOTE_DB_PATH} → ${REMOTE_BACKUP_DIR}/) and:
#   1. Quick-checks it on VPS (refuse to propagate corruption)
#   2. Rsyncs it down to LOCAL_BACKUP_DIR
#   3. Rotates local to last 3 (via rotate_local_backups.py)
#   4. rclone copies to B2_REMOTE
#   5. Rotates B2 to last 3 (inline)
#
# F-005 from 2026-04-26 DR drill: the openregs path got this propagation
# 2026-04-25; 990 had only daily mnt-data sync + local 990data_source_snapshot.db until
# now, so a failed 990 monthly couldn't roll back to anything else. This
# closes the gap.
#
# Any failure here does NOT abort the deploy — the deploy itself already
# succeeded by the time this is called.
#
# Returns 0 on success, non-zero on any failure (logged).
propagate_990_backup_to_local_and_b2() {
    local backup_file="$1"
    if [[ -z "$backup_file" ]]; then
        log "  (no backup was made — skipping propagation)"
        return 0
    fi
    local remote_backup="$REMOTE_BACKUP_DIR/$backup_file"

    log "=== Backup propagation (local + B2) ==="

    # 1. Quick-check the VPS snapshot before replicating it.
    log "Quick-checking VPS snapshot (structural integrity)..."
    if ! ssh $SSH_OPTS "$REMOTE_HOST" "python3 -c \"
import sqlite3, sys
r = sqlite3.connect('file:$remote_backup?mode=ro', uri=True).execute('PRAGMA quick_check').fetchone()[0]
sys.exit(0 if r == 'ok' else 2)
\""; then
        log "  WARNING: quick_check FAILED on VPS snapshot — refusing to propagate corruption"
        return 2
    fi
    log "  quick_check: ok"

    # 2. Pull the VPS snapshot down to the local backups dir.
    mkdir -p "$LOCAL_BACKUP_DIR"
    log "Pulling $remote_backup → $LOCAL_BACKUP_DIR/"
    if ! rsync -a --partial-dir=.rsync-partials -e "ssh $SSH_OPTS" --timeout=600 \
            "$REMOTE_HOST:$remote_backup" "$LOCAL_BACKUP_DIR/$backup_file"; then
        log "  WARNING: rsync down failed; local tier NOT updated this run"
        return 3
    fi
    log "  local copy: $LOCAL_BACKUP_DIR/$backup_file"

    # 3. Rotate local to last 3.
    if ! python3 "$ROTATE_HELPER" --dir "$LOCAL_BACKUP_DIR" --keep 3 \
            --glob '990-predeploy-*.db'; then
        log "  WARNING: local rotation helper failed"
        return 4
    fi

    # 4. Push to B2.
    log "Pushing to $B2_REMOTE/"
    if ! rclone copy "$LOCAL_BACKUP_DIR/$backup_file" "$B2_REMOTE/"; then
        log "  WARNING: B2 push failed"
        return 5
    fi

    # 5. Rotate B2 to last 3 (inline).
    log "Rotating B2 backups (keep 3)..."
    local b2_excess
    b2_excess=$(rclone lsf "$B2_REMOTE/" --files-only 2>/dev/null \
                | grep -E '^990-predeploy-.*\.db$' \
                | sort | head -n -3 || true)
    if [[ -n "$b2_excess" ]]; then
        while IFS= read -r f; do
            [[ -z "$f" ]] && continue
            log "  rclone delete $B2_REMOTE/$f"
            rclone delete "$B2_REMOTE/$f" || log "    (delete failed for $f, continuing)"
        done <<< "$b2_excess"
    else
        log "  B2 already at or under 3 backups, nothing to rotate"
    fi

    log "Backup propagation complete"
    return 0
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

# ── Step 0: Git backup of server state (optional) ───────────────────────
# This step was added assuming /opt/datasette would be tracked in a private
# git repo, but that setup was never completed on the VPS — /opt/datasette
# has no .git directory. Previously this step aborted the entire pipeline
# on every run (set -e + ssh failure), causing the monthly cron to silently
# fail since it was added. Now gracefully skipped when .git is absent so
# the rest of the pipeline can run. To re-enable, initialize a git repo at
# /opt/datasette on the VPS and set up an upstream remote.
log "--- Step 0: Git backup of current server state (optional) ---"
if ! dry "Would commit current server state to git"; then
    if ssh $SSH_OPTS "$REMOTE_HOST" "test -d /opt/datasette/.git" 2>/dev/null; then
        BACKUP_MSG="Pre-update backup: $(date '+%Y-%m-%d %H:%M:%S')"
        ssh $SSH_OPTS "$REMOTE_HOST" "cd /opt/datasette && git add -A && { git diff --cached --quiet && echo 'No changes to backup' || { git commit -m \"$BACKUP_MSG\" && git push origin main; }; }" 2>>"$LOG_FILE" || log "WARNING: git backup failed (continuing anyway)"
        log "Git backup complete"
    else
        log "Skipping git backup (/opt/datasette has no .git on VPS)"
    fi
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

# Check all years from 2017 through current year.
# IRS directory listings no longer work (302 redirect since ~2025), so we probe
# known batch name patterns via HTTP HEAD to discover available ZIPs.
CURRENT_YEAR=$(date '+%Y')
FIRST_YEAR=2017

download_batch() {
    local year="$1"
    local zip_name="$2"
    local batch_name="$3"
    local marker="$EXTRACTED_DIR/${batch_name}.done"

    if [[ -f "$marker" ]]; then
        return 0
    fi

    local url="$IRS_BASE_URL/$year/$zip_name"

    # Probe with HTTP HEAD — skip if 404/redirect
    local http_code
    http_code=$(curl -sI -o /dev/null -w '%{http_code}' -L "$url" 2>/dev/null || echo "000")
    if [[ "$http_code" != "200" ]]; then
        return 0
    fi

    local year_dir="$PROJECT_DIR/$year"
    local batch_dir="$year_dir/$batch_name"
    local zip_path="$year_dir/${zip_name}"

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

# Known IRS batch name patterns:
#   {YEAR}_TEOS_XML_01A through 12A (monthly batches)
#   {YEAR}_TEOS_XML_11B, 11C, 11D (overflow batches, seen in 2025)
#   {YEAR}_TEOS_XML_CT1 (correction/catch-up batch, seen in 2017)
BATCH_SUFFIXES=(01A 02A 03A 04A 05A 06A 07A 08A 09A 10A 11A 11B 11C 11D 12A CT1)

for (( YEAR=FIRST_YEAR; YEAR<=CURRENT_YEAR; YEAR++ )); do
    log "Checking IRS downloads for year $YEAR..."

    for SUFFIX in "${BATCH_SUFFIXES[@]}"; do
        ZIP_NAME="${YEAR}_TEOS_XML_${SUFFIX}.zip"
        BATCH_NAME="${ZIP_NAME%.zip}"
        download_batch "$YEAR" "$ZIP_NAME" "$BATCH_NAME"
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

# Backup database before modifying
BACKUP_DB="$PROJECT_DIR/990data_source_snapshot.db"
log "Backing up $DB → $BACKUP_DB"
if ! dry "Would backup $DB"; then
    cp "$DB" "$BACKUP_DB"
    BACKUP_SIZE_MB=$(du -m "$BACKUP_DB" | cut -f1)
    log "Backup complete (${BACKUP_SIZE_MB}MB)"
fi

PRE_COUNT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM returns")
log "Returns before: $PRE_COUNT"

if [[ $NEW_FILES -gt 0 ]]; then
    if ! dry "Would run extract_990.py ($NEW_FILES new files)"; then
        log "Running extract_990.py..."
        # Capture stdout too — see 2026-05-10 incident_log entry for the
        # silent-skip bug pattern. Default for ALL extract scripts is now both streams.
        python3 "$EXTRACT_CORE" >>"$LOG_FILE" 2>&1

        POST_COUNT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM returns")
        NEW_RETURNS=$(( POST_COUNT - PRE_COUNT ))
        log "Returns after: $POST_COUNT (+$NEW_RETURNS new)"

        # Extract 990-PF details for new filings
        if [[ $NEW_RETURNS -gt 0 ]]; then
            log "Running extract_990pf_detail.py..."
            python3 "$EXTRACT_PF" >>"$LOG_FILE" 2>&1
            log "990-PF detail extraction complete"

            log "Running extract_schedule_i.py..."
            # Capture stdout too — without this, the per-file SKIP messages and
            # the final guard exit-2 message are invisible to anyone reading
            # update.log. Discovered 2026-05-10 after the 2026-05-01 silent
            # near-empty schedule_i_grants build.
            python3 "$EXTRACT_SI" >>"$LOG_FILE" 2>&1 || die "extract_schedule_i.py failed (see log) — refusing to continue with near-empty DAF table"
            log "Schedule I extraction complete"

            log "Running extract_990_detail.py..."
            python3 "$EXTRACT_DETAIL" >>"$LOG_FILE" 2>&1
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
        "$PROJECT_DIR/update_guide_counts.sh" >>"$LOG_FILE" 2>&1
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

    # Add tax_year to grants table (denormalized from returns for sort/filter)
    log "Adding tax_year to grants table..."
    sqlite3 "$PUBLIC_DB" <<'SQL'
        ALTER TABLE grants ADD COLUMN tax_year INTEGER;
        UPDATE grants SET tax_year = (
            SELECT r.tax_year FROM returns r WHERE r.object_id = grants.object_id
        );
        -- idx_grants_year removed 2026-04-11: subset of idx_grants_year_amount (created later in this script)
SQL
    GRANTS_WITH_YEAR=$(sqlite3 "$PUBLIC_DB" "SELECT COUNT(*) FROM grants WHERE tax_year IS NOT NULL")
    GRANTS_TOTAL=$(sqlite3 "$PUBLIC_DB" "SELECT COUNT(*) FROM grants")
    log "  Grants with tax_year: $GRANTS_WITH_YEAR / $GRANTS_TOTAL"

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

        -- FTS on schedule_i_990: search public charity grants by recipient name
        DROP TABLE IF EXISTS fts_si990;
        CREATE VIRTUAL TABLE fts_si990 USING fts5(
            recipient_name,
            content=schedule_i_990,
            content_rowid=id
        );
        INSERT INTO fts_si990(fts_si990) VALUES('rebuild');

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

        -- FTS on officers: search by person name (44M+ rows, critical for people search API)
        DROP TABLE IF EXISTS fts_officers;
        CREATE VIRTUAL TABLE fts_officers USING fts5(
            person_name,
            content=officers,
            content_rowid=rowid
        );
        INSERT INTO fts_officers(fts_officers) VALUES('rebuild');
SQL
    log "FTS5 indexes built"

    # Verify every expected FTS table is present and populated. This catches
    # the class of silent breakage where update.sh "succeeds" but an FTS table
    # never actually got built — e.g. fts_si990 and fts_officers went missing
    # on 2026-04-11 because the pipeline was aborting at Step 0 before any
    # rebuild could run, and live search had been broken for weeks with no
    # visible signal. Die on any mismatch so a broken DB cannot be deployed.
    log "Verifying FTS5 indexes..."
    python3 - "$PUBLIC_DB" <<'PYEOF' || die "FTS verification failed — refusing to deploy broken DB"
import sqlite3, sys
db = sys.argv[1]
conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
expected = {
    "fts_returns":  "returns",
    "fts_grants":   "grants",
    "fts_daf":      "schedule_i_grants",
    "fts_si990":    "schedule_i_990",
    "fts_bmf":      "bmf",
    "fts_officers": "officers",
}
failures = []
for fts, base in expected.items():
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (fts,)
    ).fetchone()
    if not row:
        failures.append(f"{fts}: MISSING")
        continue
    try:
        fts_count = conn.execute(f"SELECT COUNT(*) FROM {fts}").fetchone()[0]
    except sqlite3.DatabaseError as e:
        failures.append(f"{fts}: BROKEN ({e})")
        continue
    base_count = conn.execute(f"SELECT COUNT(*) FROM {base}").fetchone()[0]
    if base_count > 0 and fts_count < base_count * 0.95:
        failures.append(f"{fts}: only {fts_count:,}/{base_count:,} rows ({fts_count/base_count:.1%})")
    else:
        print(f"  OK {fts}: {fts_count:,} rows")
if failures:
    print("FTS VERIFICATION FAILED:", file=sys.stderr)
    for f in failures:
        print(f"  {f}", file=sys.stderr)
    sys.exit(1)
print("All 6 FTS tables verified present and populated")
PYEOF
    log "FTS verification passed"

    # Critical-table row-count floor. Mirrors the openregs validate_critical_tables
    # guard added 2026-04-21. Refuse to deploy if any of these tables falls below
    # a defensive floor — catches the class of silent regression where a build
    # script "succeeded" but a table was wiped or never repopulated. Floors are
    # set generously below current values; legitimate growth never trips them.
    # Added 2026-05-10 after schedule_i_grants silently dropped from 1.27M to 14,890
    # rows on the 2026-05-01 build (stale source_file paths in returns).
    log "Verifying critical-table row-count floors..."
    python3 - "$PUBLIC_DB" "$PROJECT_DIR/criticality.json" <<'PYEOF' || die "Critical-table floor check failed — refusing to deploy"
import json, sqlite3, sys
db, criticality_json = sys.argv[1], sys.argv[2]
conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
crit = json.load(open(criticality_json))["tables"]
# Tables with floor=null are intentionally tracked in delta/smoke only — skip here.
floors = {t: info["floor"] for t, info in crit.items() if info.get("floor") is not None}
failures = []
for table, floor in floors.items():
    n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    if n < floor:
        failures.append(f"{table}: {n:,} rows < floor {floor:,}")
    else:
        print(f"  OK {table}: {n:,} rows (floor {floor:,})")
if failures:
    print("CRITICAL-TABLE FLOOR CHECK FAILED:", file=sys.stderr)
    for f in failures:
        print(f"  {f}", file=sys.stderr)
    sys.exit(1)
print("All critical-table floors satisfied")
PYEOF
    log "Critical-table floor check passed"

    # Per-table delta guard vs $BACKUP_DB (the snapshot taken at the start
    # of this run, before extraction). Catches the class of regression where
    # a critical table is wiped or shrinks beyond per-table tolerance, even
    # if it stays above the absolute floor. Floors catch "table is gone";
    # deltas catch "table lost meaningful chunk". Added 2026-05-10 after the
    # schedule_i_grants near-empty incident — the table fell from 1.27M to
    # 14,890 rows, well above any sensible floor wouldn't have caught a
    # smaller-but-still-pathological drop. Per-table thresholds:
    #   INCREMENTAL tables (returns, grants, etc.) should never materially
    #     decrease — tolerate 0.1% drift for dedup/backfill noise, fatal beyond.
    #   REBUILD tables (schedule_i_grants) get 5% drop tolerance for
    #     legitimate variation across runs (e.g., a re-filed return removing rows).
    log "Verifying per-table delta vs $BACKUP_DB..."
    python3 - "$PUBLIC_DB" "$BACKUP_DB" "$PROJECT_DIR/criticality.json" <<'PYEOF' || die "Delta guard failed — refusing to deploy"
import json, sqlite3, sys, os
public_db, prev_db, criticality_json = sys.argv[1], sys.argv[2], sys.argv[3]
if not os.path.exists(prev_db):
    print(f"  SKIP delta guard: {prev_db} not found (first build?)")
    sys.exit(0)

crit = json.load(open(criticality_json))["tables"]
INCREMENTAL = {t for t, info in crit.items() if info["growth"] == "incremental"}
REBUILD     = {t for t, info in crit.items() if info["growth"] == "rebuild"}

cur = sqlite3.connect(f"file:{public_db}?mode=ro", uri=True)
prev = sqlite3.connect(f"file:{prev_db}?mode=ro", uri=True)
failures = []
for t in sorted(INCREMENTAL | REBUILD):
    cur_n = cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    try:
        prev_n = prev.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    except sqlite3.OperationalError:
        print(f"  SKIP {t}: not in prev DB (first build for this table?)")
        continue
    if prev_n == 0:
        print(f"  SKIP {t}: prev was 0")
        continue
    delta = cur_n - prev_n
    delta_pct = delta / prev_n * 100
    if t in INCREMENTAL and cur_n < prev_n * 0.999:
        failures.append(f"{t}: {cur_n:,} (prev {prev_n:,}, {delta_pct:+.2f}%) — incremental table dropped beyond noise")
    elif t in REBUILD and cur_n < prev_n * 0.95:
        failures.append(f"{t}: {cur_n:,} (prev {prev_n:,}, {delta_pct:+.2f}%) — rebuild table dropped > 5%")
    else:
        print(f"  OK {t}: {cur_n:,} ({delta:+,}, {delta_pct:+.2f}%)")

if failures:
    print("DELTA GUARD FAILED:", file=sys.stderr)
    for f in failures: print(f"  {f}", file=sys.stderr)
    sys.exit(1)
print("All table deltas within tolerance vs prev DB")
PYEOF
    log "Delta guard passed"

    # Date-sanity + outlier scan (ported from openregs 05_build_database.py
    # validate_dates() added 2026-04-19 after WH visitor schema-drift audit).
    # Warns (never fails) on date outliers outside [1900, 2050].
    # Catches the class of source-side data-entry error that put `3121-01-21`
    # and `1822-01-20` dates into capital_gains.
    log "Date sanity + outlier scan..."
    python3 - "$PUBLIC_DB" <<'PYEOF' || log "WARNING: date scan errored (non-fatal)"
import sqlite3, sys
db_path = sys.argv[1]
conn = sqlite3.connect(db_path)
c = conn.cursor()
tables = [r[0] for r in c.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'fts_%' "
    "AND name NOT LIKE '%_fts%' AND name NOT LIKE '%_data' AND name NOT LIKE '%_idx' "
    "AND name NOT LIKE '%_content' AND name NOT LIKE '%_config' AND name NOT LIKE '%_docsize'"
).fetchall()]
findings = []
for t in tables:
    cols = [r[1] for r in c.execute(f'PRAGMA table_info("{t}")').fetchall()]
    for col in cols:
        if 'date' not in col.lower() and col.lower() not in ('tax_year', 'year', 'filing_year', 'fiscal_year'):
            continue
        try:
            sample = c.execute(f'SELECT "{col}" FROM "{t}" WHERE "{col}" IS NOT NULL AND "{col}" != "" LIMIT 1').fetchone()
            if not sample: continue
            s = str(sample[0])
            if s[:4].isdigit() and len(s) >= 4:
                extract = f'SUBSTR("{col}", 1, 4)'
            elif len(s) >= 10 and s[2] in '/-' and s[5] in '/-':
                extract = f'SUBSTR("{col}", -4, 4)'
            elif s.isdigit() and 1900 <= int(s) <= 2050:
                extract = f'"{col}"'  # raw year column
            else:
                continue
            n_out = c.execute(
                f'SELECT COUNT(*) FROM "{t}" WHERE "{col}" IS NOT NULL AND "{col}" != "" '
                f'AND ({extract} NOT GLOB "[0-9][0-9][0-9][0-9]" OR '
                f'CAST({extract} AS INTEGER) < 1900 OR CAST({extract} AS INTEGER) > 2050)'
            ).fetchone()[0]
            if n_out > 0:
                ex = c.execute(
                    f'SELECT DISTINCT "{col}" FROM "{t}" WHERE "{col}" IS NOT NULL AND "{col}" != "" '
                    f'AND ({extract} NOT GLOB "[0-9][0-9][0-9][0-9]" OR '
                    f'CAST({extract} AS INTEGER) < 1900 OR CAST({extract} AS INTEGER) > 2050) LIMIT 3'
                ).fetchall()
                findings.append((t, col, n_out, [e[0] for e in ex]))
        except Exception:
            pass

if findings:
    print(f"  ⚠  {len(findings)} (table, column) pairs have date outliers:", file=sys.stderr)
    for t, col, n, ex in findings:
        print(f"    {t}.{col}: {n:,} rows outside [1900, 2050]. e.g. {ex}", file=sys.stderr)
else:
    print("  ✓ No date outliers outside [1900, 2050] across any table.")
conn.close()
PYEOF
    log "Date sanity scan complete"

    # Ensure performance indexes exist (safety net — these should carry over
    # from 990data.db, but recreate if missing after table drops or schema changes)
    log "Verifying performance indexes..."
    sqlite3 "$PUBLIC_DB" <<'SQL'
        CREATE INDEX IF NOT EXISTS idx_grants_ein_type     ON grants(ein, grant_type);
        CREATE INDEX IF NOT EXISTS idx_grants_oid_type     ON grants(object_id, grant_type);
        CREATE INDEX IF NOT EXISTS idx_grants_year_amount  ON grants(tax_year, amount DESC);
        CREATE INDEX IF NOT EXISTS idx_grants_ein_recip    ON grants(ein, recipient_name COLLATE NOCASE);
        CREATE INDEX IF NOT EXISTS idx_grants_recip_type   ON grants(recipient_name COLLATE NOCASE, grant_type);
        CREATE INDEX IF NOT EXISTS idx_returns_ein_type    ON returns(ein, return_type);
        CREATE INDEX IF NOT EXISTS idx_returns_ein_year_oid ON returns(ein, tax_year DESC, object_id DESC);
        CREATE INDEX IF NOT EXISTS idx_si_recipient_nocase ON schedule_i_grants(recipient_name COLLATE NOCASE);
        CREATE INDEX IF NOT EXISTS idx_si_funder_year_amt  ON schedule_i_grants(funder_ein, tax_year, amount DESC);
        CREATE INDEX IF NOT EXISTS idx_bmf_subsection       ON bmf(subsection);
        CREATE INDEX IF NOT EXISTS idx_bmf_state            ON bmf(state);
        CREATE INDEX IF NOT EXISTS idx_bmf_foundation       ON bmf(foundation);
        CREATE INDEX IF NOT EXISTS idx_officers_comp        ON officers(compensation DESC);
SQL
    log "Performance indexes verified"

    # Strip source_file paths before publishing — leaked internal /mnt/data/990project/...
    # paths via the public API. Added 2026-04-20 per security audit.
    log "Stripping source_file internal paths from public DB..."
    sqlite3 "$PUBLIC_DB" <<'SQL'
        UPDATE returns SET source_file = NULL WHERE source_file IS NOT NULL;
        UPDATE schedule_i_grants SET source_file = NULL WHERE source_file IS NOT NULL;
SQL
    log "source_file columns nulled"

    # VACUUM to reclaim space from dropped tables
    log "VACUUMing public database..."
    sqlite3 "$PUBLIC_DB" "VACUUM;"

    # Run ANALYZE so the query planner has statistics — without it, the planner
    # falls back to structural rules and can pick suboptimal plans (e.g., using
    # a low-cardinality index like idx_return_type when a full scan would be
    # faster). Added 2026-04-11 as part of the index cleanup pass.
    log "Running ANALYZE for query planner statistics..."
    sqlite3 "$PUBLIC_DB" "ANALYZE;"

    PUBLIC_SIZE=$(stat --format="%s" "$PUBLIC_DB" 2>/dev/null || stat -f "%z" "$PUBLIC_DB")
    PUBLIC_SIZE_MB=$(( PUBLIC_SIZE / 1048576 ))
    log "Public database: ${PUBLIC_SIZE_MB}MB"
fi

log "Public DB build complete ($(elapsed "$STEP4_START")s)"

# ── Step 4b: Generate audit report ───────────────────────────────────────
log "--- Step 4b: Generate audit report ---"
if ! dry "Would generate audit report"; then
    python3 "$PROJECT_DIR/generate_audit.py" "$PUBLIC_DB" >>"$LOG_FILE" 2>&1
    log "Audit report generated: $PROJECT_DIR/build_reports/audit_latest.md"
fi

# ── Step 5: Upload to server ──────────────────────────────────────────────
log "--- Step 5: Upload to Datasette server ---"
STEP5_START=$(date +%s)

if dry "Would upload $PUBLIC_DB → $REMOTE_HOST:${REMOTE_DB_PATH}.new, then atomic mv to $REMOTE_DB_PATH"; then
    log "[DRY-RUN] Would upload ${PUBLIC_SIZE_MB:-?}MB to $REMOTE_HOST (via .new + mv)"
    log "[DRY-RUN] Would restart Datasette"
else
    # Upload strategy: write to "${REMOTE_DB_PATH}.new", then atomic mv on success.
    # Never upload directly to the live filename — see openregs/deploy/deploy.sh
    # comment block and bestpractices/deploy_guide.md "Critical Rules" for the
    # full explanation. Short version: rsync -aP and scp can both leave the live
    # DB in a corrupt half-written state if interrupted mid-transfer. Uploading
    # to ".new" guarantees the live file is never touched until the upload is
    # verified-complete on the remote side.
    # Predeploy backup: snapshot the live DB before we overwrite it. Same
    # pattern as openregs/deploy/deploy.sh. The snapshot is what the
    # propagate_990_backup_to_local_and_b2 hook below pulls down to local + B2,
    # so this also seeds the 3-tier backup chain.
    BACKUP_TIMESTAMP=$(date '+%Y%m%d_%H%M')
    BACKUP_FILE="990-predeploy-${BACKUP_TIMESTAMP}.db"
    if ssh $SSH_OPTS "$REMOTE_HOST" "test -f $REMOTE_DB_PATH"; then
        log "Backing up live $REMOTE_DB_PATH → $REMOTE_BACKUP_DIR/$BACKUP_FILE"
        ssh $SSH_OPTS "$REMOTE_HOST" "mkdir -p $REMOTE_BACKUP_DIR && cp $REMOTE_DB_PATH $REMOTE_BACKUP_DIR/$BACKUP_FILE"
        log "VPS predeploy backup complete (cleanup owned by daily sweep cron)"
    else
        log "No existing 990data_public.db on VPS — skipping predeploy backup"
        BACKUP_FILE=""
    fi

    log "Uploading to $REMOTE_HOST:${REMOTE_DB_PATH}.new..."
    rsync -a --partial-dir=.rsync-partials -e "ssh $SSH_OPTS" --progress --timeout=600 "$PUBLIC_DB" "$REMOTE_HOST:${REMOTE_DB_PATH}.new"
    log "Upload complete — atomically replacing live database..."
    ssh $SSH_OPTS "$REMOTE_HOST" "mv ${REMOTE_DB_PATH}.new ${REMOTE_DB_PATH} && sudo chown datasette:datasette ${REMOTE_DB_PATH} && sudo chmod 664 ${REMOTE_DB_PATH}"
    log "Database swap complete"

    # Deploy detail page templates and static assets
    log "Deploying templates and static assets..."
    ssh $SSH_OPTS "$REMOTE_HOST" 'mkdir -p /opt/datasette/templates/pages/org /opt/datasette/templates/pages/grant /opt/datasette/templates/pages/daf /opt/datasette/templates/pages/charity_grant /opt/datasette/templates/pages/filing /opt/datasette/static'
    scp $SSH_OPTS "$PROJECT_DIR/templates/pages/base_datadawn.html" "$REMOTE_HOST:/opt/datasette/templates/pages/base_datadawn.html"
    scp $SSH_OPTS "$PROJECT_DIR/templates/pages/org/{ein}.html" "$REMOTE_HOST:/opt/datasette/templates/pages/org/{ein}.html"
    scp $SSH_OPTS "$PROJECT_DIR/templates/pages/grant/{id}.html" "$REMOTE_HOST:/opt/datasette/templates/pages/grant/{id}.html"
    scp $SSH_OPTS "$PROJECT_DIR/templates/pages/daf/{id}.html" "$REMOTE_HOST:/opt/datasette/templates/pages/daf/{id}.html"
    scp $SSH_OPTS "$PROJECT_DIR/templates/pages/charity_grant/{id}.html" "$REMOTE_HOST:/opt/datasette/templates/pages/charity_grant/{id}.html"
    scp $SSH_OPTS "$PROJECT_DIR/templates/pages/filing/{object_id}.html" "$REMOTE_HOST:/opt/datasette/templates/pages/filing/{object_id}.html"
    scp $SSH_OPTS "$PROJECT_DIR/static/datadawn.css" "$REMOTE_HOST:/opt/datasette/static/datadawn.css"
    # Deploy explore pages
    ssh $SSH_OPTS "$REMOTE_HOST" 'mkdir -p /opt/datasette/explore'
    scp $SSH_OPTS -rq "$PROJECT_DIR/explore/"* "$REMOTE_HOST:/opt/datasette/explore/"
    log "Explore pages deployed"
    # Deploy performance plugin
    ssh $SSH_OPTS "$REMOTE_HOST" 'mkdir -p /opt/datasette/plugins'
    scp $SSH_OPTS "$PROJECT_DIR/plugins/performance.py" "$REMOTE_HOST:/opt/datasette/plugins/performance.py"
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

    ssh $SSH_OPTS "$REMOTE_HOST" "cat > /opt/datasette/metadata.json" <<METADATA_EOF
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
    ssh $SSH_OPTS "$REMOTE_HOST" 'sudo systemctl restart datasette'
    log "Datasette restarted"

    # Verify Datasette is responding
    sleep 2
    if ssh $SSH_OPTS "$REMOTE_HOST" 'sudo systemctl is-active datasette' >/dev/null 2>&1; then
        log "Datasette is running"
    else
        log "WARNING: Datasette may not have started correctly"
    fi

    # Post-deploy smoke test: verify prod returns the same row counts as the
    # DB we just built and uploaded. Catches mid-transfer corruption (rsync
    # interrupt — see 2026-04-11 incident), atomic-rename races, Datasette
    # opening the wrong file, or any other "deploy ran but prod is wrong"
    # failure mode. The atomic-rename pattern protects most cases but is not
    # bulletproof. Failures here do NOT die immediately — we still want
    # backup propagation to land — but set SMOKE_FAILED so the script exits
    # non-zero at the end and the cron's hc.io ping alerts. Added 2026-05-10.
    sleep 5  # let Datasette warmup get going; COUNT(*) is fast even cold
    log "Post-deploy smoke test (prod vs local row counts)..."
    SMOKE_FAILED=0
    # Smoke-test table list comes from criticality.json (single source of truth
    # shared with the floor + delta guards). Adding a table to the smoke set
    # means flipping `smoke: true` in criticality.json — no code edit here.
    SMOKE_TABLES=$(python3 -c "import json; print(' '.join(t for t,info in json.load(open('$PROJECT_DIR/criticality.json'))['tables'].items() if info.get('smoke')))")
    for table in $SMOKE_TABLES; do
        # AS+n alias gives us a predictable JSON key regardless of Datasette
        # version's default column-name behavior. _shape=array returns a list
        # of single-key dicts: [{"n": 12345}].
        PROD=$(curl -fsS --max-time 15 \
            "https://data.datadawn.org/990data_public.json?sql=SELECT+COUNT(*)+AS+n+FROM+${table}&_shape=array" \
            2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['n'])" 2>/dev/null) || PROD=""
        LOCAL=$(python3 -c "import sqlite3; print(sqlite3.connect('$PUBLIC_DB').execute('SELECT COUNT(*) FROM $table').fetchone()[0])")
        if [[ -z "$PROD" ]]; then
            log "  WARN  $table: prod query failed or returned no data (local=$LOCAL)"
            SMOKE_FAILED=1
        elif [[ "$PROD" != "$LOCAL" ]]; then
            log "  FAIL  $table: prod=$PROD local=$LOCAL"
            SMOKE_FAILED=1
        else
            log "  OK    $table: $PROD"
        fi
    done
    if [[ "$SMOKE_FAILED" -eq 1 ]]; then
        log "WARNING: post-deploy smoke test failed — prod row counts don't match local. Investigate."
        log "Backup propagation will still run; script will exit non-zero at end so the cron alerts."
    else
        log "Post-deploy smoke test passed"
    fi

    # Backup propagation to local + B2 — runs after the deploy succeeded.
    # Failures here do NOT roll back the deploy (it's already live); they
    # just mean this run's snapshot didn't make it to all 3 tiers.
    propagate_990_backup_to_local_and_b2 "${BACKUP_FILE:-}" || \
        log "WARNING: backup propagation exited non-zero (see above). Deploy continues."
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

# Surface any post-deploy smoke-test failure as a non-zero exit so the cron's
# hc.io ping alerts. By this point the deploy is live, the state file is
# updated, the backup chain is propagated — failure here means "prod data
# doesn't match local; investigate" rather than "rebuild from scratch".
if [[ "${SMOKE_FAILED:-0}" -eq 1 ]]; then
    log "EXITING with status 4 due to smoke-test failure (see WARNING above)"
    exit 4
fi
