#!/usr/bin/env bash
#
# update.sh — Pull new IRS 990 e-file XMLs, parse into 990data.db,
#              build public copy, upload to Datasette server.
#
# Usage:
#   ./update.sh              # full update
#   ./update.sh --dry-run    # show what would be done without changing anything
#   ./update.sh --propagate-only  # re-push newest existing VPS backup to local + B2 (no build/deploy)
#   ./update.sh --preflight-only  # run the #296 writer-certification gate (seconds) and exit;
#                                 # the standalone re-cert vehicle after any writer change
#   ./update.sh --deploy-only     # deploy the EXISTING 990data_public.db through the full
#                                 # safe path (integrity_check → .new upload → atomic mv →
#                                 # restart → smoke + render_smoke); no download/parse/build,
#                                 # state file + form render skipped. The sanctioned vehicle
#                                 # for ad-hoc pushes and restores — never bare rsync to the
#                                 # live filename (incidents 2026-04-11, 2026-05-22; §73).
#
# Prerequisites:
#   - curl for downloading from IRS website
#   - unzip for extracting ZIP archives
#   - ssh key for user@YOUR_SERVER_IP
#   - Python 3 with lxml
#   - sqlite3 CLI
#
set -euo pipefail

# ── Run under the project virtualenv (cron-vs-interactive parity) ──────────
# cron runs this script with a stripped environment and NO venv activation, so a
# bare `python3` falls through to the SYSTEM interpreter — which lacks the
# venv-only deps in requirements.txt (notably defusedxml, the XXE-hardened XML
# parser added 2026-04-20). That divergence silently broke the 2026-06-01
# monthly run: extract_schedule_i.py died at `import defusedxml`, parsed 0
# Schedule I rows, and the DAF near-empty guard aborted AFTER a 47k-file IRS
# download. Prepend the venv bin to PATH (not `source activate`, which is fragile
# under cron's stripped env, and not absolute python paths, of which this script
# has none) so every bare `python3` below resolves to the interpreter that
# matches requirements.txt. Override via DATADAWN_VENV_BIN for testing.
export PATH="${DATADAWN_VENV_BIN:-$HOME/data-env/bin}:$PATH"

# ── Configuration ──────────────────────────────────────────────────────────
PROJECT_DIR="/mnt/data/datadawn/990project"
DB="$PROJECT_DIR/990data.db"
PUBLIC_DB="${PUBLIC_DB:-$PROJECT_DIR/990data_public.db}"  # env-overridable (testing/restore); prod cron leaves it default
STATE_FILE="$PROJECT_DIR/.update_state"
LOG_FILE="$PROJECT_DIR/update.log"
EXTRACTED_DIR="$PROJECT_DIR/.extracted"
IRS_BASE_URL="https://apps.irs.gov/pub/epostcard/990/xml"
REMOTE_HOST="user@YOUR_SERVER_IP"
REMOTE_DB_PATH="/opt/datasette/990data_public.db"
REMOTE_BACKUP_DIR="/opt/datasette/backups"
LOCAL_BACKUP_DIR="$PROJECT_DIR/backups"
# Snapshot-on-volume PARKED 2026-06-03 (SQLite .backup()'s buffered 50 GB write to
# the slow legacy block-storage volume intermittently wedged the deploy — decisions_log §94 +
# deploy.sh). 990 snapshot stays on NVMe, keep-1. Gate 2 FLIPPED 2026-06-03: backups
# now go to the current backup bucket via its rclone remote (key rotated after the
# 2026-05-30 exposure); the prior bucket retained read-only as legacy history (not deleted).
B2_BUCKET="your-b2-bucket"
B2_REMOTE="b2:${B2_BUCKET}/990-weekly"
# Global cross-project deploy lock (G): serialize against the openregs pipeline
# so they never deploy concurrently. 6h timeout ~= 4.3x this pipeline's measured
# 84-min worst-case; env-overridable for testing. See openregs §6.2 / keep1-amendment.md.
GLOBAL_LOCKFILE="${GLOBAL_LOCKFILE:-/tmp/datadawn-deploy.lock}"  # env-overridable (testing); prod cron leaves it default
GLOBAL_LOCK_TIMEOUT="${DATADAWN_DEPLOY_LOCK_TIMEOUT:-21600}"
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
WRITER_HARNESS="$PROJECT_DIR/test_monthly_contractor_writer.py"  # #264 certification harness (#296 gate)

DRY_RUN=0
BUILD_DEPLOY_ONLY=0  # --build-deploy-only: skip Steps 2-3 (IRS download+parse), rebuild public DB from current 990data.db + deploy
PROPAGATE_ONLY=0     # --propagate-only: re-push the newest existing VPS backup to local + B2; no download/build/deploy
DEPLOY_ONLY=0        # --deploy-only: skip Steps 1-4 (download/parse/build); deploy the existing $PUBLIC_DB through integrity_check + the Step-5 safe path. State (6) + render (7) skipped. (§73, 2026-06-05)
PREFLIGHT_ONLY=0     # --preflight-only: run the #296 writer-certification gate standalone, then exit (no lock, no pings)
ACK_LARGE_DELTA=""   # --ack-large-delta="reason": acknowledge an intentional large/negative table delta (e.g. a de-dup reparse) so the delta guard warns+proceeds instead of aborting

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

# ── Good-news milestone pings (2026-07-01) ───────────────────────────────
# Positive Pushover confirmation at start / build-verified / LIVE, so a
# SUCCESSFUL monthly run isn't silent (hc.io only alerts on failure). Priority
# 0 (normal/audible). Toggle off with GOODNEWS_990_PINGS=0. Best-effort:
# ALWAYS call as `notify_ok … || true` — the `||` list disables `set -e` for
# the whole function body, so a Pushover hiccup can never abort the run.
# Mirrors openregs/weekly_update.sh notify_ok verbatim; judges Pushover's own
# accept signal (HTTP 2xx AND "status":1) so a rejected ping logs honest.
PUSHKEYS="$HOME/.pushover_keys"
notify_ok() {  # $1 title  $2 message
    [[ "${GOODNEWS_990_PINGS:-1}" == "0" ]] && return 0
    [ -r "$PUSHKEYS" ] || { log "  (pushover ok-ping: ~/.pushover_keys unreadable — skipped)"; return 1; }
    . "$PUSHKEYS"
    local resp http body
    resp=$(curl -s --max-time 20 -w '\n%{http_code}' \
         -F "token=${PUSHOVER_APP_TOKEN:-}" -F "user=${PUSHOVER_USER_KEY:-}" \
         -F "title=$1" -F "message=$2" -F "priority=0" \
         https://api.pushover.net/1/messages.json 2>/dev/null)
    http=$(printf '%s' "$resp" | tail -n1)
    body=$(printf '%s' "$resp" | sed '$d')
    if [[ "$http" == 2* ]] && printf '%s' "$body" | grep -qE '"status":[[:space:]]*1([^0-9]|$)'; then
        log "  (pushover ok-ping sent, http=$http)"; return 0
    else
        log "  (pushover ok-ping send failed — http=$http)"; return 1
    fi
}

# ── §5a propagation-ledger failure-row writer (#146(b), 2026-06-14) ──────
# Mirrors openregs/deploy/deploy.sh ledger_record_fail(). Until now the 990
# propagation wrote NO §5a ledger row at all (the writer was openregs-only —
# #146 was "resolved-openregs / 990-open"), so a failed 990 propagation was
# invisible by absence: the success-only model can't distinguish "no failure"
# from "failure that wrote nothing" (the 2026-06-13 silent-absence class). This
# writes a status=FAILED row at each propagation failure leg so the §4 daily
# backup-health check (backup_health_report.py section_ledger — which ALREADY
# reads 990project/backups/propagation_ledger.jsonl) REDs on it. Non-fatal: a
# ledger-write failure must never turn a successful-deploy into a failed one.
ledger_record_fail() {
    local ledger_dir="$1" snap="$2" b2r="$3" stage="$4" code="$5" local_ok="$6" b2_ok="$7"
    LEDGER="$ledger_dir/propagation_ledger.jsonl" SNAP="$snap" B2R="$b2r" \
        STAGE="$stage" CODE="$code" LOCAL_OK="$local_ok" B2_OK="$b2_ok" python3 - <<'PYEOF' \
        || log "  WARNING: failure-row ledger write failed (non-fatal)"
import json, os, time
entry = {
    "ts": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    "db": "990",
    "snapshot_file": os.environ['SNAP'],
    "b2_remote": os.environ['B2R'],
    "status": "FAILED",
    "stage": os.environ['STAGE'],
    "return_code": int(os.environ['CODE']),
    "local_ok": os.environ['LOCAL_OK'] == '1',
    "b2_ok": os.environ['B2_OK'] == '1',
}
with open(os.environ['LEDGER'], 'a') as f:
    f.write(json.dumps(entry, sort_keys=True) + "\n")
PYEOF
    log "  §5a FAILURE row recorded (stage=$stage rc=$code) — the daily backup-health check will RED on it"
}

# ── 3-tier backup propagation (post-deploy, non-critical) ────────────────
#
# Mirrors openregs/deploy/deploy.sh propagate_backup_to_local_and_b2().
# Called after a successful 990 upload. Takes the predeploy snapshot that
# was written to the VPS (cp ${REMOTE_DB_PATH} → ${REMOTE_BACKUP_DIR}/) and:
#   1. Quick-checks it on VPS (refuse to propagate corruption)
#   1b. Writes a sidecar manifest.json on the VPS next to the backup
#       (DR drill F-001/F-003 follow-up — row counts of critical tables +
#       schema fingerprint so we can tell what's in a backup without
#       opening it; mirrors openregs/deploy/deploy.sh)
#   2. Rsyncs both files down to LOCAL_BACKUP_DIR
#   3. Rotates local to last 3 (via rotate_local_backups.py, .db + manifest)
#   4. rclone copies both files to B2_REMOTE
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
    local crit_path="$PROJECT_DIR/criticality.json"
    # #269 (2026-07-03): deploy-aware defer marker — the 990 lane was UNWIRED and raced the
    # daily 07:10 backup-health check (the exact 2026-06-27 openregs incident on this lane).
    # Shared helper = one mechanism, no drift. RETURN trap clears on EVERY exit of this fn
    # (success + all handled-failure returns); a hard kill strands it for the checker's
    # content-age backstop (stale = treated-as-absent + WARN, never a standing suppression).
    source /mnt/data/datadawn/openregs/deploy/propagation_marker.sh
    prop_marker_begin "990"
    trap 'prop_marker_end' RETURN

    log "=== Backup propagation (local + B2) ==="

    # 1. Quick-check the VPS snapshot before replicating it.
    log "Quick-checking VPS snapshot (structural integrity)..."
    # SECONDARY tripwire (snapshot was already quick_check-gated at birth). On fail
    # = post-birth corruption: quarantine to .corrupt (de-heads it) + signal exit so
    # hc.io fires. Clean heredoc w/ try/except matches the birth check.
    if ! ssh $SSH_OPTS "$REMOTE_HOST" "python3 - '$remote_backup'" <<'PYQC'
import sqlite3, sys
try:
    r = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True).execute("PRAGMA quick_check").fetchone()[0]
except Exception as e:
    print("quick_check error:", e); sys.exit(1)
sys.exit(0 if r == "ok" else 1)
PYQC
    then
        log "  WARNING: quick_check FAILED post-birth — quarantining to .corrupt and de-heading"
        ssh $SSH_OPTS "$REMOTE_HOST" "mv '$remote_backup' '$remote_backup.corrupt'" || true
        SNAPSHOT_CORRUPT=1   # forces non-zero exit at end (hc.io alerts); NOT local
        ledger_record_fail "$LOCAL_BACKUP_DIR" "$backup_file" "$B2_REMOTE" "vps_quick_check" 2 0 0
        return 2
    fi
    log "  quick_check: ok"

    # 1b. Write manifest sidecar on the VPS. Non-fatal — backup is still
    # usable without it; just means DR can't cheap-check this backup's
    # contents until next deploy.
    if [[ -f "$crit_path" ]]; then
        local crit_tables_json
        crit_tables_json=$(python3 -c "import json,sys; print(json.dumps(list(json.load(open('$crit_path'))['tables'].keys())))")
        log "Writing manifest sidecar on VPS..."
        if ssh $SSH_OPTS "$REMOTE_HOST" \
                "REMOTE_BACKUP='$remote_backup' CRIT_TABLES='$crit_tables_json' SOURCE_SCRIPT=990project/update.sh python3 -" \
                <<'PYEOF'
import sqlite3, json, os, hashlib, time, sys
backup = os.environ['REMOTE_BACKUP']
tables = json.loads(os.environ['CRIT_TABLES'])
conn = sqlite3.connect(f'file:{backup}?mode=ro', uri=True)
schema_rows = conn.execute(
    "SELECT type, name, sql FROM sqlite_schema "
    "WHERE sql IS NOT NULL ORDER BY type, name"
).fetchall()
schema_text = "\n".join(f"{t}\t{n}\t{s}" for t, n, s in schema_rows)
schema_fp = hashlib.sha256(schema_text.encode()).hexdigest()
existing = {r[0] for r in conn.execute(
    "SELECT name FROM sqlite_schema WHERE type='table'").fetchall()}
row_counts = {}
for t in tables:
    if t in existing:
        try:
            row_counts[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except sqlite3.Error:
            row_counts[t] = None
manifest = {
    "schema_version": 1,
    "backup_file": os.path.basename(backup),
    "manifest_timestamp_utc": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    "backup_mtime_utc": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(os.path.getmtime(backup))),
    "db_size_bytes": os.path.getsize(backup),
    "quick_check": "ok",
    "schema_fingerprint_sha256": schema_fp,
    "sqlite_version": sqlite3.sqlite_version,
    "row_counts": row_counts,
    "row_count_tables_present": sum(1 for v in row_counts.values() if v is not None),
    "row_count_tables_missing": sum(1 for v in row_counts.values() if v is None),
    "source": os.environ.get('SOURCE_SCRIPT', ''),
}
out = backup + '.manifest.json'
tmp = out + '.tmp'
with open(tmp, 'w') as f:
    json.dump(manifest, f, indent=2, sort_keys=True)
os.rename(tmp, out)
print(f"manifest: {out} ({manifest['row_count_tables_present']} tables, fp={schema_fp[:12]})", file=sys.stderr)
PYEOF
        then
            log "  manifest written"
        else
            log "  WARNING: manifest generation failed (non-fatal — backup itself is intact)"
        fi
    else
        log "  (criticality.json missing locally — skipping manifest sidecar)"
    fi

    # 2. Pull the VPS snapshot + manifest down to the local backups dir.
    mkdir -p "$LOCAL_BACKUP_DIR"
    log "Pulling $remote_backup → $LOCAL_BACKUP_DIR/"
    if ! rsync -a --partial-dir=.rsync-partials -e "ssh $SSH_OPTS" --timeout=600 \
            "$REMOTE_HOST:$remote_backup" "$LOCAL_BACKUP_DIR/$backup_file"; then
        log "  WARNING: rsync down failed; local tier NOT updated this run"
        ledger_record_fail "$LOCAL_BACKUP_DIR" "$backup_file" "$B2_REMOTE" "rsync_local" 3 0 0
        return 3
    fi
    log "  local copy: $LOCAL_BACKUP_DIR/$backup_file"
    # Manifest pull is best-effort — pre-manifest backups won't have one.
    rsync -a -e "ssh $SSH_OPTS" --timeout=60 \
        "$REMOTE_HOST:${remote_backup}.manifest.json" \
        "$LOCAL_BACKUP_DIR/${backup_file}.manifest.json" 2>/dev/null \
        && log "  local manifest: $LOCAL_BACKUP_DIR/${backup_file}.manifest.json" \
        || log "  (no manifest on VPS to pull — pre-manifest backup or generation failed)"

    # 3. Rotate local to last 3 (both .db and .db.manifest.json families).
    if ! python3 "$ROTATE_HELPER" --dir "$LOCAL_BACKUP_DIR" --keep 3 \
            --glob '990-predeploy-*.db'; then
        log "  WARNING: local rotation helper failed"
        ledger_record_fail "$LOCAL_BACKUP_DIR" "$backup_file" "$B2_REMOTE" "rotate_local" 4 1 0
        return 4
    fi
    # Manifest sidecars: keep last 3 too. Exit 2 ("nothing matched") is fine
    # during the rollout period when no manifests exist yet.
    local rc=0
    python3 "$ROTATE_HELPER" --dir "$LOCAL_BACKUP_DIR" --keep 3 \
        --glob '990-predeploy-*.db.manifest.json' || rc=$?
    if [[ $rc -ne 0 && $rc -ne 2 ]]; then
        log "  WARNING: manifest rotation helper failed (exit $rc)"
    fi

    # 4. Push to B2 (db + manifest together).
    log "Pushing to $B2_REMOTE/"
    if ! rclone copy "$LOCAL_BACKUP_DIR/$backup_file" "$B2_REMOTE/"; then
        log "  WARNING: B2 push failed"
        ledger_record_fail "$LOCAL_BACKUP_DIR" "$backup_file" "$B2_REMOTE" "b2_push" 5 1 0
        return 5
    fi
    if [[ -f "$LOCAL_BACKUP_DIR/${backup_file}.manifest.json" ]]; then
        rclone copy "$LOCAL_BACKUP_DIR/${backup_file}.manifest.json" "$B2_REMOTE/" \
            || log "  WARNING: B2 manifest push failed (non-fatal)"
    fi

    # 5. Rotate B2 to last 3 (inline — sort lexically on filename works
    # because our format encodes timestamp in the name). Sweeps both .db
    # backups and any orphaned .manifest.json files.
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
            # Sweep the paired manifest if present.
            rclone delete "$B2_REMOTE/${f}.manifest.json" 2>/dev/null || true
        done <<< "$b2_excess"
    else
        log "  B2 already at or under 3 backups, nothing to rotate"
    fi
    # Also sweep any manifest orphans (e.g. from rollout transitions).
    local b2_manifest_orphans
    b2_manifest_orphans=$(rclone lsf "$B2_REMOTE/" --files-only 2>/dev/null \
                | grep -E '^990-predeploy-.*\.db\.manifest\.json$' \
                | sort | head -n -3 || true)
    if [[ -n "$b2_manifest_orphans" ]]; then
        while IFS= read -r f; do
            [[ -z "$f" ]] && continue
            log "  rclone delete $B2_REMOTE/$f (manifest orphan)"
            rclone delete "$B2_REMOTE/$f" || log "    (delete failed for $f, continuing)"
        done <<< "$b2_manifest_orphans"
    fi

    # 6. Propagation ledger (§5a — 990 writer added 2026-06-14, finishing #146).
    # Append a durable record that THIS generation was hashed and propagated to
    # BOTH local and B2 — the success row whose ABSENCE the §4 daily backup-health
    # check (section_ledger) reads as a silent propagation failure. The reader was
    # already generalized over both DBs (cfg carries 990's ledger path); 990 just
    # never wrote a row, so it sat permanently in the "no §5a ledger yet" pre-rollout
    # state and a real 990 failure would have been absence-blind. FULLY NON-FATAL:
    # the deploy + propagation already succeeded; a ledger-write failure must never
    # fail a deploy. The FAILED legs (return 2/3/4/5 above) write status=FAILED rows.
    local ledger="$LOCAL_BACKUP_DIR/propagation_ledger.jsonl"
    log "Writing propagation ledger entry → $ledger"
    LEDGER="$ledger" LOCAL_COPY="$LOCAL_BACKUP_DIR/$backup_file" \
        MANIFEST="$LOCAL_BACKUP_DIR/${backup_file}.manifest.json" \
        SNAP_NAME="$backup_file" B2R="$B2_REMOTE" python3 - <<'PYEOF' \
        || log "  WARNING: ledger write failed (non-fatal — backup itself is safe)"
import json, os, hashlib, time, sys
ledger = os.environ['LEDGER']
local_copy = os.environ['LOCAL_COPY']
# Full-file sha256 of the local copy (byte-identical to the B2 copy rclone just
# pushed) — recorded NOW so §4 can re-hash the off-site copy later and assert it
# still matches, catching truncation / bit-rot / interrupted transfer.
h = hashlib.sha256()
with open(local_copy, 'rb') as f:
    for chunk in iter(lambda: f.read(8 << 20), b''):
        h.update(chunk)
sha = h.hexdigest()
# Generation identity from the manifest (schema fingerprint; 990 manifests carry
# row_counts rather than openregs-style content_markers, so markers is None here).
fp, markers = None, None
try:
    with open(os.environ['MANIFEST']) as mf:
        m = json.load(mf)
    fp = m.get('schema_fingerprint_sha256')
    markers = m.get('content_markers')
except (OSError, ValueError):
    pass
entry = {
    "ts": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    "db": "990",
    "snapshot_file": os.environ['SNAP_NAME'],
    "sha256": sha,
    "size_bytes": os.path.getsize(local_copy),
    "build_fingerprint": fp,
    "content_markers": markers,
    "b2_remote": os.environ['B2R'],
    "local_ok": True,
    "b2_ok": True,
}
with open(ledger, 'a') as f:
    f.write(json.dumps(entry, sort_keys=True) + "\n")
print(f"ledger: {entry['snapshot_file']} sha={sha[:12]} fp={(fp or '')[:12]}",
      file=sys.stderr)
PYEOF

    log "Backup propagation complete"
    return 0
}

# ── #296 writer-certification pre-flight (added 2026-07-06) ────────────────
# The #264 harness (test_monthly_contractor_writer.py) certifies the monthly
# writer (extract_990_detail.py): 11 proofs, each RED-then-GREEN, on scratch
# DBs only — it never touches 990data.db (measured ~3s, 2026-07-06). Until now NOTHING ran it
# automatically (followup_queue #296): certification could silently rot the
# day after it was earned. Distinct from the Step-4 parser_harness baseline
# gate (#232), which checks the BUILT DATA — this certifies the WRITER CODE
# before it writes. Runs before Step 2's 47k-file download budget (same
# cheap-early-abort rationale as the venv-deps check). FAIL-CLOSED: a missing
# harness aborts too — absence of certification is not certification.
# Gated to invocations where the writer will actually run: skipped for
# --deploy-only / --build-deploy-only (both skip Step 3's parse), so those
# stay usable as 2AM restore vehicles even while an unrelated writer bug is
# red. Standalone re-cert: `update.sh --preflight-only`.
run_writer_preflight() {
    log "--- Writer-certification pre-flight (#296, the #264 harness) ---"
    [[ -f "$WRITER_HARNESS" ]] || die "writer harness not found: $WRITER_HARNESS — fail-closed (#296): refusing to run the monthly with an uncertified writer. Restore the harness (pipeline repo) or investigate."
    local pf_start pf_rc=0
    pf_start=$(date +%s)
    # Unpiped (redirect, not pipe) + `|| pf_rc=$?` so the recorded exit code is
    # the harness's own — the CLAUDE.md pipeline exit-discipline pattern.
    python3 "$WRITER_HARNESS" >>"$LOG_FILE" 2>&1 || pf_rc=$?
    if [[ $pf_rc -ne 0 ]]; then
        die "writer-certification harness FAILED (rc=$pf_rc, $(elapsed "$pf_start")s) — the monthly writer is NOT certified; aborting before download/parse. See the '#264 harness' block in $LOG_FILE. Do not bypass (#296): fix writer or harness, then re-run 'update.sh --preflight-only' until green."
    fi
    log "Writer certification: all implemented proofs RED-then-GREEN ($(elapsed "$pf_start")s)"
}

# ── Parse arguments ───────────────────────────────────────────────────────
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        --build-deploy-only) BUILD_DEPLOY_ONLY=1 ;;
        --propagate-only) PROPAGATE_ONLY=1 ;;
        --deploy-only) DEPLOY_ONLY=1 ;;
        --preflight-only) PREFLIGHT_ONLY=1 ;;
        --ack-large-delta=*) ACK_LARGE_DELTA="${arg#*=}" ;;
        *) die "Unknown argument: $arg" ;;
    esac
done

if [[ $DEPLOY_ONLY -eq 1 && $BUILD_DEPLOY_ONLY -eq 1 ]]; then
    die "--deploy-only and --build-deploy-only are mutually exclusive (one skips the build, the other runs it)"
fi

# ── --propagate-only: re-propagate the newest existing VPS 990 backup ──────
# Added 2026-05-31. Mirror of openregs deploy.sh --propagate-only: re-runs
# propagate_990_backup_to_local_and_b2() against the newest existing VPS 990
# predeploy backup to CLOSE an off-site propagation gap (a generation stranded
# VPS-only) WITHOUT a download/build/deploy. Reuses the tested propagation
# function (quick_check gate, manifest, rotate-3) — no hand-rolled divergence.
# Runs BEFORE the global deploy lock + pre-flight on purpose: propagate-only is
# read-only on the VPS (copies a backup off; cannot corrupt the live DB), so it
# needs neither the deploy lock nor the build-oriented pre-flight checks.
if [[ $PROPAGATE_ONLY -eq 1 ]]; then
    log "=== --propagate-only: locating newest VPS 990 backup in $REMOTE_BACKUP_DIR ==="
    # awk NR==1 (not head -1) so the remote ls is fully drained — avoids the
    # pipefail+SIGPIPE early-exit class. || true so a failed ssh yields an empty
    # result (-> die below) rather than tripping set -e mid-substitution.
    PROP_PATH=$(ssh $SSH_OPTS "$REMOTE_HOST" \
        "ls -1t '$REMOTE_BACKUP_DIR'/990-predeploy-*.db 2>/dev/null | awk 'NR==1'" || true)
    PROP_FILE=$(basename "$PROP_PATH" 2>/dev/null || true)
    [[ -z "$PROP_FILE" ]] && die "no 990-predeploy-*.db found on the VPS to propagate."
    log "  newest VPS backup: $PROP_FILE"
    if [[ $DRY_RUN -eq 1 ]]; then
        log "  [dry-run] would propagate $PROP_FILE to local + B2, then exit."
        exit 0
    fi
    if propagate_990_backup_to_local_and_b2 "$PROP_FILE"; then
        log "--propagate-only: done."
        exit 0
    else
        rc=$?
        log "--propagate-only: propagation FAILED (rc=$rc) — see warnings above."
        exit "$rc"
    fi
fi

# ── --preflight-only: run the #296 writer-certification gate and exit ──────
# Standalone certification check — the red-prove vehicle and the on-demand
# re-cert after any writer change. Side effects = scratch DBs + update.log
# lines only, so like --propagate-only it needs neither the deploy lock nor
# the pushover kickoff ping.
if [[ $PREFLIGHT_ONLY -eq 1 ]]; then
    run_writer_preflight
    log "--preflight-only: done (writer certified)."
    exit 0
fi

# ── Global cross-project deploy lock (G) ──────────────────────────────────
# Serialize the 990 pipeline against openregs so they never deploy concurrently
# (1st-of-month-Saturday collision + 226 GB-box disk contention). Blocking with a
# 6h timeout; auto-released on process death (kernel closes fd 8). 990 needs only
# G (no openregs-internal O lock). See openregs §6.2 / keep1-amendment.md.
exec 8>"$GLOBAL_LOCKFILE"
if ! flock -w "$GLOBAL_LOCK_TIMEOUT" 8; then
    die "Timed out (${GLOBAL_LOCK_TIMEOUT}s) waiting for global deploy lock ($GLOBAL_LOCKFILE) — an openregs deploy is still running or stuck."
fi

# ── Pre-flight checks ─────────────────────────────────────────────────────
log "========================================="
log "990 Update Pipeline Starting"
log "========================================="
[[ $DRY_RUN -eq 1 ]] && log "*** DRY-RUN MODE — no changes will be made ***"

# Good-news milestone: announce kickoff (real runs only, not dry-run).
[[ $DRY_RUN -eq 0 ]] && notify_ok "🟢 990 monthly: started" \
    "Rebuild + deploy kicked off $(date '+%a %Y-%m-%d %H:%M %Z'). Next pings: build-verified, then LIVE." || true

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

# Fail fast if the build's venv-only XML deps can't import, BEFORE Step 2's 47k-
# file IRS download. On 2026-06-01 a missing defusedxml (venv-only) under cron's
# system python3 went uncaught until extract_schedule_i.py's import at Step 3 —
# after the download + parse work was already spent. This ~2s check makes it an
# immediate, legible abort instead. Mirrors requirements.txt: defusedxml drives
# Schedule I; lxml drives the 990 / 990-PF / detail extractors. command -v in the
# message names WHICH python3 resolved, so a recurrence reads venv-vs-system at a
# glance. (Next: a crontab-wide bare-`python3` audit — update.sh tripped first,
# but the defect class is cron-vs-interactive env divergence, not this script.)
python3 -c "import defusedxml, lxml" 2>/dev/null \
    || die "venv-only XML deps (defusedxml/lxml) failed to import under $(command -v python3) — data-env venv broken or PATH prepend not taking effect? See requirements.txt."

# #296 writer-certification gate — only when the writer will run this invocation
# (--deploy-only / --build-deploy-only skip Step 3's parse; see run_writer_preflight).
if [[ $DEPLOY_ONLY -eq 0 && $BUILD_DEPLOY_ONLY -eq 0 ]]; then
    if ! dry "Would run writer-certification pre-flight (#296, ~seconds)"; then
        run_writer_preflight
    fi
fi

mkdir -p "$EXTRACTED_DIR"

# Step 0 (Git backup of server state) was removed 2026-05-13.
# /opt/datasette was never set up as a git repo, so the step had been a
# precondition-guarded no-op since the 2026-04-11 fix. Several docs
# (incident_log.md, pipeline_verification.md, MEMORY.md) already described
# it as "deleted" — code now matches the docs. If a server-state git
# backup is ever wanted again, initialize /opt/datasette as a repo and
# add a new step here.

# ── Step 1: Determine date range ──────────────────────────────────────────
LAST_UPDATED="2025-01-01"
if [[ -f "$STATE_FILE" ]]; then
    LAST_UPDATED=$(cat "$STATE_FILE")
fi
TODAY=$(date '+%Y-%m-%d')

# --deploy-only: skip Steps 1-4 entirely and push the EXISTING public artifact
# through the unchanged safe path. Restores work the same way: copy the restore
# candidate to $PUBLIC_DB first, then run this. Every deploy gate is retained —
# integrity_check (4a2), audit report (4b), predeploy snapshot + .new upload +
# atomic mv + restart + smoke + render_smoke (Step 5). Steps 6 (state file) and
# 7 (form render) are skipped: no new data was processed, and writing the state
# file here would make the next monthly silently skip real filings. (§73)
if [[ $DEPLOY_ONLY -eq 1 ]]; then
    NEW_FILES=0
    [[ -f "$PUBLIC_DB" ]] || die "--deploy-only: $PUBLIC_DB not found — nothing to deploy. Build first, or copy the restore candidate to this path."
    log "--- Steps 1-4 SKIPPED (--deploy-only): deploying existing $PUBLIC_DB ($(stat --format='%y' "$PUBLIC_DB" | cut -d. -f1), $(( $(stat --format='%s' "$PUBLIC_DB") / 1048576 ))MB) ---"
else
log "Last update: $LAST_UPDATED"
log "Checking for new filings since $LAST_UPDATED"

if [[ $BUILD_DEPLOY_ONLY -eq 1 ]]; then
    NEW_FILES=0
    # Mirror Step 3's source snapshot so the per-table delta guard has a valid
    # baseline (BACKUP_DB). Compares the public build against current source —
    # passes when the build is faithful; catches build-step row loss.
    BACKUP_DB="$PROJECT_DIR/990data_source_snapshot.db"
    if ! dry "Would snapshot $DB → $BACKUP_DB (delta-guard baseline)"; then
        cp "$DB" "$BACKUP_DB"
    fi
    log "--- Steps 2-3 SKIPPED (--build-deploy-only): rebuild + deploy current 990data.db ---"
else

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
    local hdr_path="$year_dir/${zip_name}.hdr"

    mkdir -p "$year_dir"

    log "  New batch: $batch_name"

    if dry "Would download $url → $batch_dir/"; then
        NEW_FILES=$(( NEW_FILES + 1 ))
        return 0
    fi

    # Download (-D captures response headers for the freshness marker — no extra request)
    log "  Downloading $zip_name..."
    if ! curl -# -L -D "$hdr_path" -o "$zip_path" "$url" >>"$LOG_FILE" 2>&1; then
        log "  ERROR: Failed to download $url"
        rm -f "$zip_path" "$hdr_path"
        return 1
    fi

    # Count what the ZIP claims to hold BEFORE we extract — needed for the
    # partial-extract integrity check below.
    local declared_count
    declared_count=$(unzip -l "$zip_path" 2>/dev/null | grep -cE '\.xml$' || echo 0)

    # Extract
    mkdir -p "$batch_dir"
    log "  Extracting..."
    if ! unzip -q -o "$zip_path" -d "$batch_dir" >>"$LOG_FILE" 2>&1; then
        log "  ERROR: Failed to extract $zip_path"
        return 1
    fi

    local xml_count
    xml_count=$(find "$batch_dir" -name '*.xml' -type f | wc -l)

    # Integrity check: extracted file count must match ZIP's declared count.
    # Without this, a partial extract (e.g. disk-full mid-unzip) would silently
    # lose files and mark the batch complete forever — same class as the
    # 2026-05-01 DAF incident. Marker is touched LAST, after all checks pass.
    if [[ "$declared_count" -gt 0 && "$xml_count" -lt "$declared_count" ]]; then
        log "  ERROR: extracted $xml_count XML files but ZIP declared $declared_count — partial extract, NOT marking complete"
        return 1
    fi

    NEW_FILES=$(( NEW_FILES + xml_count ))

    # Clean up ZIP and mark complete (in that order — never mark before cleanup
    # could fail mid-flight).
    rm -f "$zip_path"
    # #92 facet (a): the marker RECORDS the served Last-Modified + Content-Length
    # (from the -D capture) instead of an empty touch. Both readers only test -f
    # (the marker check above; backfill_download.sh:30) — content-safe, verified
    # 2026-06-09. reconcile_acquisition.py diffs these against live HEAD probes
    # to catch a same-name re-release the marker-skip would otherwise hold stale.
    # set -o pipefail: greps are ||true'd; a marker is ALWAYS created (the skip
    # logic depends on its existence, not its content).
    if [[ -f "$hdr_path" ]]; then
        {
            tr -d '\r' < "$hdr_path" | grep -i '^last-modified:' | tail -n 1 || true
            tr -d '\r' < "$hdr_path" | grep -i '^content-length:' | tail -n 1 || true
        } > "$marker" || touch "$marker"
        rm -f "$hdr_path"
    else
        touch "$marker"
    fi
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
            python3 "$EXTRACT_SI" >>"$LOG_FILE" 2>&1 || die "extract_schedule_i.py exited non-zero — see $LOG_FILE for the actual cause (could be a near-empty DAF rebuild, a parse failure, OR an environment/import error like the 2026-06-01 defusedxml case). Refusing to continue."
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

fi  # end --build-deploy-only skip of Steps 2-3

# ── Step 3a: #92(iii) ingest-seam reconcile ──────────────────────────────
# Asserts the DB holds what the extracted corpus contains: disk↔returns
# per-object_id both directions + per-year retention floors (manifest).
# Runs UNCONDITIONALLY in the data path — explicitly decoupled from the
# NEW_FILES>0 gate, so an ingest shortfall cannot hide behind "no new files
# this month". Flag-for-review only (never auto-fixes); a breach sets
# SEAM_BREACH → exit 8 at the summary gate so the cron hc ping reds. The
# pipeline CONTINUES past a breach: the DB was already in this state before
# the run, so deploying it changes nothing while a human investigates.
log "--- Step 3a: Ingest-seam reconcile (#92 iii) ---"
STEP3A_START=$(date +%s)
SEAM_BREACH=0
if ! dry "Would run reconcile_ingest_seam.py"; then
    if python3 "$PROJECT_DIR/reconcile_ingest_seam.py" >>"$LOG_FILE" 2>&1; then
        log "Ingest-seam reconcile: clean ($(elapsed "$STEP3A_START")s)"
    else
        SEAM_RC=$?
        SEAM_BREACH=1
        log "CRITICAL: ingest-seam reconcile BREACH/ERROR (rc=$SEAM_RC) — see [seam-reconcile] lines in $LOG_FILE. Flag-for-review; continuing (exit 8 at summary)."
    fi
fi

# ── Step 3c: #92(ii) acquisition catalog observe (ADVISORY — never gates) ─
# HEAD-probes the IRS catalog grid, classifies vs markers + the exclusions
# manifest, and freshness-compares recorded marker headers vs live (facet (a):
# same-name re-release detection). Observe mode per the Guard-A arm-time
# doctrine: log findings, exit 0; arm with --gate only after accrued behavior
# is understood. ~160 HEADs ≈ 1-2 min.
log "--- Step 3c: Acquisition catalog observe (#92 ii, advisory) ---"
if ! dry "Would run reconcile_acquisition.py (observe)"; then
    python3 "$PROJECT_DIR/reconcile_acquisition.py" >>"$LOG_FILE" 2>&1 \
        || log "WARNING: acquisition observe exited nonzero (advisory — see [acq-reconcile] lines in $LOG_FILE)"
fi

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
    # Baseline = the most recent prior-DEPLOYED generation (a predeploy backup),
    # NOT $BACKUP_DB (source_snapshot). source_snapshot is a copy of the current
    # working 990data.db, which an out-of-band backfill can mutate BEFORE update.sh
    # snapshots it — exactly how the 2026-05-24 capital_gains de-dup (24.3M→15.6M)
    # sailed past this guard (snapshot 15.6M vs build 15.6M = 0%). Comparing against
    # the last thing actually deployed closes that. tail -n1 drains the pipe fully
    # (no SIGPIPE early-exit, unlike head -1). See
    # audit_reports/capital_gains_dedup_verdict_2026_05_31.md.
    DELTA_BASELINE=$(find "$PROJECT_DIR/backups" -maxdepth 1 -name '990-predeploy-*.db' 2>/dev/null | sort | tail -n1 || true)
    [[ -z "$DELTA_BASELINE" ]] && DELTA_BASELINE="$BACKUP_DB"   # fallback: source_snapshot (first build)
    log "Verifying per-table delta vs prior-deployed generation: ${DELTA_BASELINE##*/} ..."
    python3 - "$PUBLIC_DB" "$DELTA_BASELINE" "$PROJECT_DIR/criticality.json" "$ACK_LARGE_DELTA" <<'PYEOF' || die "Delta guard failed — refusing to deploy"
import json, sqlite3, sys, os
public_db, prev_db, criticality_json = sys.argv[1], sys.argv[2], sys.argv[3]
ack = sys.argv[4] if len(sys.argv) > 4 else ""
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
    print("DELTA GUARD: negative/large deltas detected:", file=sys.stderr)
    for f in failures: print(f"  {f}", file=sys.stderr)
    if ack:
        print(f'  ACKNOWLEDGED via --ack-large-delta="{ack}" — proceeding despite the above.', file=sys.stderr)
        sys.exit(0)
    print('  (re-run with --ack-large-delta="reason" to deploy an intentional large change.)', file=sys.stderr)
    sys.exit(1)
print("All table deltas within tolerance vs prev DB")
PYEOF
    log "Delta guard passed"

    # Duplication guard (inflation detector) — added 2026-05-31 after the
    # capital_gains de-dup verdict (audit_reports/capital_gains_dedup_verdict_2026_05_31.md).
    # The delta guard only catches DROPS; duplicate rows ACCUMULATING read as healthy
    # growth (capital_gains crept 22.8M→24.3M as dup rows piled up, invisible). This
    # catches inflation directly: COUNT(*) vs COUNT(DISTINCT all-non-id-columns) per
    # dup_check table — the automated form of "decompose by denominator." Scoped via
    # criticality.json (dup_check:true) to detail tables prone to parser fan-out.
    log "Duplication guard on dup_check tables..."
    python3 - "$PUBLIC_DB" "$PROJECT_DIR/criticality.json" <<'PYEOF' || die "Duplication guard failed — refusing to deploy"
import json, sqlite3, sys
public_db, criticality_json = sys.argv[1], sys.argv[2]
crit = json.load(open(criticality_json))["tables"]
checks = {t: info for t, info in crit.items() if info.get("dup_check")}
if not checks:
    print("  (no dup_check tables configured)"); sys.exit(0)
conn = sqlite3.connect(f"file:{public_db}?mode=ro", uri=True)
conn.execute("PRAGMA temp_store=MEMORY"); conn.execute("PRAGMA cache_size=-2000000")
failures = []
for t, info in sorted(checks.items()):
    ceiling = float(info.get("dup_ceiling_pct", 10.0))
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
    keycols = [c for c in cols if c != "id"]
    if not keycols:
        print(f"  SKIP {t}: no non-id columns"); continue
    key = " || '~' || ".join(f"COALESCE(CAST({c} AS TEXT),'')" for c in keycols)
    total = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    if total == 0:
        print(f"  SKIP {t}: empty"); continue
    distinct = conn.execute(f"SELECT COUNT(DISTINCT {key}) FROM {t}").fetchone()[0]
    dup = total - distinct
    dup_pct = 100.0 * dup / total
    ok = dup_pct <= ceiling
    print(f"  {'OK' if ok else 'FAIL'} {t}: {dup:,} dup rows ({dup_pct:.2f}% of {total:,}, ceiling {ceiling:.1f}%)")
    if not ok:
        failures.append(f"{t}: {dup_pct:.2f}% dup rows > {ceiling:.1f}% ceiling — possible parser fan-out / over-emission")
if failures:
    print("DUPLICATION GUARD FAILED:", file=sys.stderr)
    for f in failures: print(f"  {f}", file=sys.stderr)
    sys.exit(1)
print("All dup_check tables within duplication tolerance")
PYEOF
    log "Duplication guard passed"

    # 990 parser baseline gate (#232) — §64 comp-nullability regression rail on the
    # UNWATCHED auto-monthly. Wires parser_harness.assert_baseline_green as a 5th
    # validate-before-deploy gate (pre-upload, same idiom as the four above). Arms
    # ONLY assert_baseline_green (officers cols that already exist); the
    # promotion_gate newfield invariants ride §2. argv[1] = the freshly-built public DB.
    log "990 parser baseline gate (comp-nullability §64 rail)..."
    python3 "$PROJECT_DIR/parser_harness.py" "$PUBLIC_DB" || die "990 parser baseline gate failed — refusing to deploy"
    log "990 parser baseline gate passed"

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
        CREATE INDEX IF NOT EXISTS idx_officers_comp        ON officers(reportable_comp_filing_org DESC);
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

fi  # end --deploy-only skip of Steps 1-4

# ── Step 4a2: Deep integrity_check on the public DB before upload ─────────
# Gates the deploy. Mirrors openregs/weekly_update.sh:499-513. Without this the
# 990 birth quick_check would be verifying a copy of a never-deep-checked source —
# the asymmetry vs openregs, closed 2026-06-03 (decisions_log §94). exit 1 here
# aborts before any upload/snapshot (live untouched).
if ! dry "Would integrity_check $PUBLIC_DB before upload"; then
    log "Integrity check (public DB)..."
    INTEGRITY=$(python3 -c "import sqlite3; print(sqlite3.connect('$PUBLIC_DB').execute('PRAGMA integrity_check').fetchone()[0])" 2>&1)
    if [[ "$INTEGRITY" == "ok" ]]; then
        log "Integrity: ok"
    else
        log "INTEGRITY CHECK FAILED: $INTEGRITY"
        die "Public DB failed integrity_check — aborting before upload (live untouched)."
    fi
fi

# ── Step 4a3: return_version integrity gate (Pin 2, adversarial review 2026-06-28) ───────
# The per-version grouping key's OWN health, on the UNATTENDED monthly — which otherwise inspects
# NOTHING on return_version (a collapsed/garbage read silently reverts the per-version defense to
# cumulative and ships behind clean-looking data: the masking month). Placed in the always-run
# pre-upload region (past the --deploy-only skip at the `fi` above) so it gates EVERY upload path.
# NOT wrapped in `dry` -> read-only, so it RUNS in dry-run too (only the upload below is dry-skipped),
# which is what lets the integration prove-RED confirm it aborts BEFORE the upload. Pre-land (column
# absent) it SKIPs -> zero effect on the current monthly. Post-land a RED dies here: abort before
# upload, live untouched, cron hc.io heartbeat unpinged -> dead-man pages. Function-level teeth:
# test_parser_harness RVI*; PLACEMENT teeth: the dry-run integration prove-RED. (The FULL new-field
# gate joins this on the monthly at land, after the recalibration flags are flipped — today only
# return_version_integrity is wired here.)
log "return_version integrity gate (per-version grouping key)..."
if ! python3 - "$PROJECT_DIR" "$PUBLIC_DB" <<'PYEOF'
import sys, sqlite3
sys.path.insert(0, sys.argv[1])
import parser_harness as ph
conn = sqlite3.connect(f"file:{sys.argv[2]}?mode=ro", uri=True)
cols = {r[1] for r in conn.execute("PRAGMA table_info(returns)")}
if "return_version" not in cols:
    print("RVI SKIP: return_version not landed (pre-§2) — gate inert on this build")
    sys.exit(0)
sys.exit(0 if ph.return_version_integrity(conn) else 1)
PYEOF
then
    die "return_version integrity FAILED on the built public DB — refusing to deploy (Pin 2: a collapsed/garbage grouping key silently reverts per-version to cumulative; live untouched, dead-man pages)"
fi
log "return_version integrity gate passed (or inert pre-land)"

# ── Step 4b: Generate audit report ───────────────────────────────────────
log "--- Step 4b: Generate audit report ---"
if ! dry "Would generate audit report"; then
    python3 "$PROJECT_DIR/generate_audit.py" "$PUBLIC_DB" >>"$LOG_FILE" 2>&1
    log "Audit report generated: $PROJECT_DIR/build_reports/audit_latest.md"
fi

# Good-news milestone: build + all validation gates passed, about to deploy.
# Skipped in --deploy-only (nothing was rebuilt) and dry-run.
[[ $DRY_RUN -eq 0 && $DEPLOY_ONLY -eq 0 ]] && notify_ok "🟢 990 monthly: build verified" \
    "Rebuild done, all validation gates green. Uploading to data.datadawn.org now." || true

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
        ssh $SSH_OPTS "$REMOTE_HOST" "mkdir -p $REMOTE_BACKUP_DIR"
        # Block-start cleanup: orphan *.db.tmp (hard-kill) + bound .corrupt to newest-1.
        ssh $SSH_OPTS "$REMOTE_HOST" "rm -f $REMOTE_BACKUP_DIR/990-predeploy-*.db.tmp"
        ssh $SSH_OPTS "$REMOTE_HOST" "DIR='$REMOTE_BACKUP_DIR' bash -s" <<'PYCORRUPT'
cd "$DIR" || exit 0
ls -1t 990-predeploy-*.db.corrupt 2>/dev/null | awk 'NR>1' | while IFS= read -r f; do
    echo "  rm old quarantine $f"; rm -f -- "$f"
done
PYCORRUPT
        log "Snapshot → backups/$BACKUP_FILE.tmp (WAL-safe .backup() API, NVMe)"
        # WAL-safe SQLite online-backup API — NOT cp. .backup() → .tmp; mv to the
        # rollback-head name GATED on (1) .backup() success and (2) quick_check —
        # verify BEFORE head-promotion. (decisions_log §94)
        if ! ssh $SSH_OPTS "$REMOTE_HOST" "python3 - '$REMOTE_DB_PATH' '$REMOTE_BACKUP_DIR/$BACKUP_FILE.tmp'" <<'PYBACKUP'
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[2])
try:
    with dst:
        src.backup(dst)
    dst.execute("PRAGMA journal_mode=DELETE")  # single-file snapshot: no -wal/-shm orphans on later opens
finally:
    src.close(); dst.close()
PYBACKUP
        then
            ssh $SSH_OPTS "$REMOTE_HOST" "rm -f '$REMOTE_BACKUP_DIR/$BACKUP_FILE.tmp'" || true
            die "990 snapshot .backup() failed — removed partial .tmp; live untouched; gen-1 remains rollback head."
        fi
        if ! ssh $SSH_OPTS "$REMOTE_HOST" "python3 - '$REMOTE_BACKUP_DIR/$BACKUP_FILE.tmp'" <<'PYCHECK'
import sqlite3, sys
try:
    r = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True).execute("PRAGMA quick_check").fetchone()[0]
except Exception as e:
    print("quick_check error:", e); sys.exit(1)
sys.exit(0 if r == "ok" else 1)
PYCHECK
        then
            ssh $SSH_OPTS "$REMOTE_HOST" "mv '$REMOTE_BACKUP_DIR/$BACKUP_FILE.tmp' '$REMOTE_BACKUP_DIR/$BACKUP_FILE.corrupt'" || true
            die "990 snapshot quick_check FAILED at birth — quarantined to .corrupt; live untouched; gen-1 remains rollback head."
        fi
        ssh $SSH_OPTS "$REMOTE_HOST" "mv '$REMOTE_BACKUP_DIR/$BACKUP_FILE.tmp' '$REMOTE_BACKUP_DIR/$BACKUP_FILE'"
        # Part B (2026-06-04): snapshot is journal_mode=DELETE; defensively sweep any
        # .tmp-wal/.tmp-shm so the backup dir is EXACTLY snapshot + manifest.
        ssh $SSH_OPTS "$REMOTE_HOST" "rm -f '$REMOTE_BACKUP_DIR/$BACKUP_FILE.tmp-wal' '$REMOTE_BACKUP_DIR/$BACKUP_FILE.tmp-shm'"
        # keep-1 (GATED — option iii, 2026-06-04): trim an older snapshot ONLY after
        # confirming it is off-box (workstation Tier-2 OR B2 Tier-3). One that ISN'T is NOT
        # deleted — warn + flag exit 7 so the cron pages. Workstation-side. -mtime+5 backstops.
        OLD_SNAPS=$(ssh $SSH_OPTS "$REMOTE_HOST" "ls -1t '$REMOTE_BACKUP_DIR'/990-predeploy-*.db 2>/dev/null | awk 'NR>1'") || { log "  WARNING: keep-1: could not list VPS snapshots (ssh failure) — skipping trim this run; daily -mtime+5 cron backstops disk."; OLD_SNAPS=""; }
        # B2 (Tier-3) listing fetched ONCE for exact-name membership tests below.
        B2_LIST=$(rclone lsf --files-only "$B2_REMOTE/" 2>/dev/null) || B2_LIST=""
        while IFS= read -r REMOTE_OLD; do
            [[ -z "$REMOTE_OLD" ]] && continue
            OB=$(basename "$REMOTE_OLD")
            OFF_BOX=0
            [[ -f "$LOCAL_BACKUP_DIR/$OB" ]] && OFF_BOX=1
            # B2 (Tier-3) fallback: exact-name membership in the once-fetched listing. NOT
            # `rclone lsf "$B2_REMOTE/$OB"` — that lists $OB as a *directory* and exits 0/empty
            # for a NON-existent file → false "present" (sandbox-caught 2026-06-04). Herestring
            # (not a pipe) avoids the pipefail + grep-q SIGPIPE masking class.
            if [[ $OFF_BOX -eq 0 ]] && grep -Fxq "$OB" <<< "$B2_LIST"; then OFF_BOX=1; fi
            if [[ $OFF_BOX -eq 1 ]]; then
                log "  keep-1 trim: $OB present off-box (Tier-2/3) → rm on VPS"
                ssh $SSH_OPTS "$REMOTE_HOST" "rm -f -- '$REMOTE_BACKUP_DIR/$OB' '$REMOTE_BACKUP_DIR/$OB.manifest.json'"
            else
                log "  WARNING: keep-1 trim SKIPPED $OB — NOT on workstation OR B2; refusing to delete the only copy (heal via update.sh --propagate-only)."
                TRIM_UNSAFE=1
            fi
        done <<< "$OLD_SNAPS"
        log "VPS 990 snapshot complete+verified on NVMe (keep-1; deeper → workstation+B2)"
    else
        log "No existing 990data_public.db on VPS — skipping predeploy backup"
        BACKUP_FILE=""
    fi

    log "Uploading to $REMOTE_HOST:${REMOTE_DB_PATH}.new..."
    # Retry transient transport failures (broken pipe / socket IO) up to 3x.
    # --partial-dir keeps the partial so each retry RESUMES rather than restarts.
    # Added 2026-06-01: a single broken pipe at ~16% killed an otherwise-clean
    # ~7-min build's deploy (incident_log 2026-06-01) — keepalive can't stop a hard
    # disconnect, so a bare one-shot rsync makes the whole monthly deploy hostage
    # to one network blip. The live DB is never at risk (.new pattern); worst case
    # after 3 fails is a clean die() with the live file untouched.
    UPLOAD_OK=0
    for attempt in 1 2 3; do
        if rsync -a --partial-dir=.rsync-partials -e "ssh $SSH_OPTS" --progress --timeout=600 "$PUBLIC_DB" "$REMOTE_HOST:${REMOTE_DB_PATH}.new"; then
            UPLOAD_OK=1; break
        fi
        log "  WARNING: upload attempt $attempt/3 failed (transient transport?); retrying (resumes via --partial-dir)..."
        sleep 10
    done
    [[ $UPLOAD_OK -eq 1 ]] || die "rsync upload → ${REMOTE_DB_PATH}.new failed after 3 attempts — live DB untouched (.new pattern); no swap performed."
    # Pre-swap integrity gate (#19, 2026-05-25): quick_check the uploaded .new
    # BEFORE it goes live, so a corrupt build/transfer can't replace a good live
    # DB. Companion to the WAL-safe backup fix — that protects the backup tier,
    # this protects the deployed-DB tier. VPS has no sqlite3 CLI → python3 over ssh.
    if ! ssh $SSH_OPTS "$REMOTE_HOST" "python3 - '${REMOTE_DB_PATH}.new'" <<'PYCHECK'
import sqlite3, sys
try:
    r = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True).execute("PRAGMA quick_check").fetchone()[0]
except Exception as e:
    print("quick_check error:", e); sys.exit(1)
sys.exit(0 if r == "ok" else 1)
PYCHECK
    then
        die "Pre-swap quick_check FAILED on ${REMOTE_DB_PATH}.new — refusing to swap (live DB untouched; .new left in place for inspection)"
    fi
    log "Pre-swap quick_check passed — uploaded DB is structurally sound"
    log "Upload complete — atomically replacing live database..."
    ssh $SSH_OPTS "$REMOTE_HOST" "mv ${REMOTE_DB_PATH}.new ${REMOTE_DB_PATH} && sudo chown datasette:datasette ${REMOTE_DB_PATH} && sudo chmod 664 ${REMOTE_DB_PATH}"
    log "Database swap complete"

    # Deploy detail page templates and static assets
    log "Deploying templates and static assets..."
    ssh $SSH_OPTS "$REMOTE_HOST" 'mkdir -p /opt/datasette/templates/pages/org /opt/datasette/templates/pages/grant /opt/datasette/templates/pages/daf /opt/datasette/templates/pages/charity_grant /opt/datasette/templates/pages/filing /opt/datasette/static'
    scp $SSH_OPTS "$PROJECT_DIR/templates/pages/base_datadawn.html" "$REMOTE_HOST:/opt/datasette/templates/pages/base_datadawn.html"
    # Shared recipient-disclosure macros imported by grant/charity_grant/daf via {% from "_recip_copy.html" %}.
    # Lives at the template-dir ROOT (not pages/). MUST ship or those pages 500 with TemplateNotFound. (ER QC 2026-05-29)
    scp $SSH_OPTS "$PROJECT_DIR/templates/_recip_copy.html" "$REMOTE_HOST:/opt/datasette/templates/_recip_copy.html"
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

    # Deploy REST API (server.py + catalogs). Added 2026-05-29: the api/ dir was
    # previously on NO deploy path, so local fixes never shipped — the live
    # questions.json froze at 2026-03-29 for ~2 months (followup_queue.md #24).
    #
    # FAILURE BEHAVIOR = stay-up-on-last-good, symmetric with the openregs api block
    # (hardened at #117). A guard ladder, each rung a DISJOINT failure class:
    #   1. local pre-ship gate: server.py py_compile + json.load(questions/openapi).
    #      questions/openapi are served RAW per-request (no parse) so a malformed one
    #      ships as 200-with-garbage, caught by nothing downstream — this parse is the
    #      only guard for that class. Fail SKIPS only the api step.
    #   2. snapshot live server.py to backups/ (outside the parity-watched api/ dir).
    #   3. VPS-env `import server` before restart — the compiles-but-won't-START class
    #      py_compile can't see (bad import / module-level error; cf. THIS script's own
    #      extract_schedule_i defusedxml death, the live example). Runs AS the service
    #      User (datasette) under its /usr/bin/python3 — matches user-site + interpreter,
    #      env-accurate by construction. No outage on fail.
    #   4. restart + is-active; roll back on a failed restart (bind/serve-time residual).
    # The served-version check below is NOT a running==source signal: it reads
    # openapi.json per-request from disk (redundant with the #117 md5 parity pair) AND
    # the version is a static "1.0.0" (near-vacuous) — it never reflected server.py's
    # running code. 990's running==source coverage is #121, same as openregs. Kept
    # only as an "endpoint answers parseable JSON" smoke.
    if ! python3 -c "import json,py_compile,sys; py_compile.compile(sys.argv[1],doraise=True); json.load(open(sys.argv[2])); json.load(open(sys.argv[3]))" \
            "$PROJECT_DIR/api/server.py" "$PROJECT_DIR/api/questions.json" "$PROJECT_DIR/api/openapi.json" 2>/dev/null; then
        log "⚠ ERROR: 990 api/ pre-ship validation FAILED (server.py compile or questions/openapi JSON parse) — SKIPPING api deploy (990-api stays on previous code). Fix + redeploy."
    else
        log "Deploying REST API (server.py + questions.json + openapi.json)..."
        ssh $SSH_OPTS "$REMOTE_HOST" 'mkdir -p /opt/datasette/api /opt/datasette/backups'
        ssh $SSH_OPTS "$REMOTE_HOST" 'cp -f /opt/datasette/api/server.py /opt/datasette/backups/server.py.prev 2>/dev/null || true'
        scp $SSH_OPTS "$PROJECT_DIR/api/server.py"      "$REMOTE_HOST:/opt/datasette/api/server.py"
        scp $SSH_OPTS "$PROJECT_DIR/api/questions.json" "$REMOTE_HOST:/opt/datasette/api/questions.json"
        scp $SSH_OPTS "$PROJECT_DIR/api/openapi.json"   "$REMOTE_HOST:/opt/datasette/api/openapi.json"
        # Rung 3: env-accurate import check — AS the service user (datasette) under its
        # interpreter (no outage; __main__ guard starts no server).
        if ! ssh $SSH_OPTS "$REMOTE_HOST" "cd /opt/datasette/api && sudo -n -u datasette /usr/bin/python3 -c 'import server'" >/dev/null 2>&1; then
            log "⚠ ERROR: shipped 990 server.py imports clean locally but FAILS to import on the VPS as the service user (bad import / module-level error) — NOT restarting; 990-api still serving previous code (no outage). Restoring prev on disk. Check: ssh $REMOTE_HOST 'cd /opt/datasette/api && sudo -u datasette /usr/bin/python3 -c \"import server\"'"
            ssh $SSH_OPTS "$REMOTE_HOST" 'cp -f /opt/datasette/backups/server.py.prev /opt/datasette/api/server.py'
        else
            ssh $SSH_OPTS "$REMOTE_HOST" 'sudo systemctl restart 990-api'
            API_STATE=$(ssh $SSH_OPTS "$REMOTE_HOST" 'sudo systemctl is-active 990-api' 2>/dev/null)
            if [[ "$API_STATE" != "active" ]]; then
                # Rung 4: imported clean but didn't come up (bind/serve-time). Roll back.
                log "⚠ ERROR: 990-api '${API_STATE:-unreachable}' after restart (import-clean; bind/serve-time failure) — rolling back to backups/server.py.prev"
                ssh $SSH_OPTS "$REMOTE_HOST" 'cp -f /opt/datasette/backups/server.py.prev /opt/datasette/api/server.py && sudo systemctl restart 990-api'
                ROLLED=$(ssh $SSH_OPTS "$REMOTE_HOST" 'sudo systemctl is-active 990-api' 2>/dev/null)
                if [[ "$ROLLED" == "active" ]]; then
                    log "↩ 990-api RECOVERED on previous server.py — new code NOT live; fix + redeploy. (#117 parity RED on api/server.py until then — expected.)"
                else
                    log "🔴 CRITICAL: 990-api still '${ROLLED:-unreachable}' after rollback — manual intervention. Check: ssh $REMOTE_HOST 'systemctl status 990-api; journalctl -u 990-api -n 30'"
                fi
            else
                # Service up. Secondary smoke only (see header: redundant w/ parity +
                # static version → near-vacuous; NOT a running==source check). WARNING.
                LOCAL_API_VER=$(python3 -c "import json;print(json.load(open('$PROJECT_DIR/api/openapi.json'))['info']['version'])" 2>/dev/null)
                SERVED_API_VER=""
                for _i in 1 2 3 4 5; do
                    SERVED_API_VER=$(curl -fsS --max-time 15 https://data.datadawn.org/api/openapi.json 2>/dev/null \
                        | python3 -c "import sys,json;print(json.load(sys.stdin)['info']['version'])" 2>/dev/null)
                    [[ -n "$SERVED_API_VER" ]] && break
                    sleep 3
                done
                if [[ -n "$SERVED_API_VER" && "$SERVED_API_VER" == "$LOCAL_API_VER" ]]; then
                    log "REST API deployed — 990-api active + serving parseable openapi v$SERVED_API_VER"
                else
                    log "⚠ WARNING: 990-api active but /api/openapi.json smoke soft-failed — endpoint serves '${SERVED_API_VER:-unreachable}' (warm-up/CDN?). Check: ssh $REMOTE_HOST 'systemctl status 990-api'"
                fi
            fi
        fi
    fi

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

    # Per-table descriptions: parser-coverage disclosures (followup_queue Bug #1/#2
    # sizing, 2026-06-05). contractors/top_employees are parsed from Form 990-PF
    # ONLY (extract_990pf_detail.py; verified 100% of rows join return_type='990PF')
    # — without the disclosure, an empty query result for a Form 990 filer reads as
    # "none reported" (the #61 silent-incomplete class) on the AI/SQL surface.
    # Static text, same pattern as the openregs stock_trades note. Remove the
    # "not yet parsed" sentences when Bug #1/#2 parsers ship.
    ssh $SSH_OPTS "$REMOTE_HOST" "cat > /opt/datasette/metadata.json" <<METADATA_EOF
{
    "title": "DataDawn 990 Explorer",
    "description_html": "<p>IRS Form 990 nonprofit data: <strong>${R_FMT} returns</strong> (tax years ${TAX_YEARS}), <strong>${G_FMT} foundation grants</strong>, <strong>${D_FMT} DAF disbursements</strong>, <strong>${S_FMT} Schedule I grants</strong>, <strong>${O_FMT} officers/directors</strong>, and <strong>${RE_FMT} related org relationships</strong>.</p>",
    "license": "Public Domain (IRS data)",
    "license_url": "https://www.irs.gov/privacy-disclosure/irs-privacy-policy",
    "databases": {
        "990data_public": {
            "tables": {
                "contractors": {
                    "description_html": "Five highest-paid independent contractors, parsed from <strong>Form 990 and 990-PF</strong> filings (Form 990 Part VII Section B; 990-PF Part VIII). An empty result means the filer reported no contractors above the \$100K threshold."
                },
                "top_employees": {
                    "description_html": "Highest-compensated employees (other than officers). This table covers Form 990-PF (Part VIII). Form-990 highest-compensated employees are not duplicated here — they appear in the <code>officers</code> table flagged <code>is_highest_compensated_employee</code>."
                },
                "officers": {
                    "description_html": "Officers, directors, trustees, key employees (Part VII Sec A). The six <code>is_*</code> role flags are check-all-that-apply, read independently per box — a person can carry several. 0-vs-NULL is semantic: Form 990 rows use 1/0 (0 = box unchecked); 990-EZ/PF rows carry NULL (no Section-A structure on those forms). Comp columns: <code>reportable_comp_filing_org</code> (all forms) + <code>reportable_comp_related_org</code>/<code>other_compensation</code> (Form 990 only) or <code>benefits</code>/<code>expense_account</code> (990-EZ/PF only). <code>compensation</code> is deprecated (legacy, unwritten by parsers — NULL on re-derived rows); use <code>reportable_comp_filing_org</code>. Within-filing byte-duplicate person entries are collapsed under a keyed dedup; genuine variants stay distinct rows."
                }
            }
        }
    },
    "plugins": {
        "datasette-cors": {
            "allow_all": true
        }
    }
}
METADATA_EOF
    log "Metadata updated"

    # Refresh llms_990.txt structured counts + deploy it (2026-06-05).
    # The file was previously hand-maintained AND deployed by nothing — it
    # drifted bidirectionally (the live copy taught agents the deprecated
    # officers.compensation column; the local copy missed the live welcome
    # section). Canonical copy = $PROJECT_DIR/llms_990.txt: prose edits stay
    # manual there; the parenthesized table counts on "### table (N rows)"
    # and "- \`table\` (N)" lines auto-refresh here from the public DB so
    # they can't rot; then the file rides every monthly deploy.
    log "Refreshing llms_990.txt counts + deploying..."
    if python3 - "$PROJECT_DIR/llms_990.txt" "$PUBLIC_DB" >> "$LOG_FILE" 2>&1 <<'LLMS_EOF'
import re, sqlite3, sys
path, db = sys.argv[1], sys.argv[2]
con = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
tables = {r[0] for r in con.execute(
    "SELECT name FROM sqlite_schema WHERE type='table'")}
def fmt(n):
    if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
    if n >= 1_000: return f"{round(n/1_000)}K"
    return str(n)
def count(t):
    return con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
src = open(path).read()
out, refreshed = src, 0
# Core-table headers: "### returns (5.2M rows)"
for m in re.finditer(r'^### (\w+) \([^)]*\)', src, re.M):
    t = m.group(1)
    if t in tables:
        out = out.replace(m.group(0), f"### {t} ({fmt(count(t))} rows)")
        refreshed += 1
# Other-table bullets: "- `capital_gains` (15.9M)"
for m in re.finditer(r'^- `(\w+)` \([^)]*\)', src, re.M):
    t = m.group(1)
    if t in tables:
        out = out.replace(m.group(0), f"- `{t}` ({fmt(count(t))})")
        refreshed += 1
if refreshed == 0:
    print("llms refresh: 0 count lines matched — file format changed? NOT writing", file=sys.stderr)
    sys.exit(3)
# Semantic-drift guard (2026-06-05): execute every ```sql example block
# against the public DB. The worst llms drift wasn't a stale count — it was
# an example query teaching the deprecated officers.compensation column.
# A dropped/renamed column or table makes its example DIE here, loudly,
# instead of being served to agents for months. LIMIT 1 wrap keeps it cheap.
sql_blocks = re.findall(r'```sql\n(.*?)```', out, re.S)
if not sql_blocks:
    print("llms guard: 0 sql example blocks found — file format changed? NOT writing", file=sys.stderr)
    sys.exit(3)
failures = 0
for i, q in enumerate(sql_blocks, 1):
    q = q.strip().rstrip(';')
    try:
        con.execute(f"SELECT * FROM ({q}) LIMIT 1").fetchall()
    except sqlite3.Error as e:
        print(f"llms guard: example query #{i} FAILED against the public DB: {e}", file=sys.stderr)
        failures += 1
if failures:
    print(f"llms guard: {failures}/{len(sql_blocks)} example queries broken — NOT writing/deploying", file=sys.stderr)
    sys.exit(4)
open(path, 'w').write(out)
print(f"llms refresh: {refreshed} count lines refreshed; {len(sql_blocks)} example queries executed OK")
LLMS_EOF
    then
        if scp $SSH_OPTS "$PROJECT_DIR/llms_990.txt" "$REMOTE_HOST:/opt/datasette/llms_990.txt" >> "$LOG_FILE" 2>&1; then
            log "llms_990.txt refreshed + example queries verified + deployed"
        else
            log "WARNING: llms_990.txt scp failed (non-fatal — live copy stays on previous generation)"
        fi
    else
        log "ERROR: llms_990.txt refresh/query-guard FAILED — NOT deployed (see log; broken example = semantic drift, fix before next deploy)"
        SMOKE_FAILED=1
    fi

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
    sleep 15  # let Datasette/WAL warmup; bumped from 5s 2026-05-13 for 22GB DB
    log "Post-deploy smoke test (prod vs local row counts)..."
    # Preserving init (2026-06-05): the llms_990.txt refresh/query-guard above
    # may already have set SMOKE_FAILED=1 — a plain =0 here would clobber it.
    SMOKE_FAILED=${SMOKE_FAILED:-0}
    # Smoke-test table list comes from criticality.json (single source of truth
    # shared with the floor + delta guards). Adding a table to the smoke set
    # means flipping `smoke: true` in criticality.json — no code edit here.
    SMOKE_TABLES=$(python3 -c "import json; print(' '.join(t for t,info in json.load(open('$PROJECT_DIR/criticality.json'))['tables'].items() if info.get('smoke')))")
    for table in $SMOKE_TABLES; do
        # AS+n alias gives us a predictable JSON key regardless of Datasette
        # version's default column-name behavior. _shape=array returns a list
        # of single-key dicts: [{"n": 12345}].
        # Retry 3× with 5s/10s backoff (added 2026-05-13) — a single transient
        # 503 right after restart used to false-positive the smoke test.
        PROD=""
        for attempt in 1 2 3; do
            PROD=$(curl -fsS --max-time 15 \
                "https://data.datadawn.org/990data_public.json?sql=SELECT+COUNT(*)+AS+n+FROM+${table}&_shape=array" \
                2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['n'])" 2>/dev/null)
            [[ -n "$PROD" ]] && break
            [[ $attempt -lt 3 ]] && sleep $((attempt * 5))
        done
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

    # Render smoke: the ER-QC recipient-disclosure templates (grant/charity_grant/daf;
    # add ',org' to --surfaces once org/{ein}.html + its branch are live, June-1+).
    # Complements the row-count smoke above: that proves the DB transferred intact;
    # THIS proves the served PAGES render correctly AND that no page references a file
    # the deploy forgot to ship (the _recip_copy.html / api-freeze class — a missing
    # `{% from %}` 500s the page, caught here). Derives test rows by branch criteria at
    # runtime (ids are autoincrement, never hardcoded). Same SMOKE_FAILED convention:
    # log + flag + non-zero at end so the cron's hc.io ping alerts; does NOT abort
    # backup propagation. (ER QC 2026-05-30)
    log "Post-deploy render smoke (ER-QC disclosure templates)..."
    if python3 "$PROJECT_DIR/render_smoke.py" --base https://data.datadawn.org --db "$PUBLIC_DB" --surfaces grant,charity_grant,daf,org >>"$LOG_FILE" 2>&1; then
        log "Render smoke passed"
    else
        log "WARNING: post-deploy render smoke FAILED — a disclosure page renders wrong, or references an unshipped file. See render_smoke output above."
        SMOKE_FAILED=1
    fi

    # ── §5 DEPLOY-COVERAGE ASSERTION (Deliverable A, 2026-07-03) — HARD GATE, NON-SKIPPABLE. ──
    # Asserts every public surface's coverage statement is TRUE against the DEPLOYED artifacts
    # (live URLs + VPS ssh reads + the live /api/coverage) with P computed on the SERVED DB:
    # P ⟺ RESOLVED per (surface × object). This is the ONLY safety net under the manual MCP
    # repoint (the §49-freeze class). Proven RED-first against real pre-land surfaces 2026-07-03.
    # NEVER soften to advisory; can't-evaluate = RED ([[feedback_no_false_coverage_monitors]]).
    log "Post-deploy §5 coverage assertion (deployed surfaces × served DB)..."
    if python3 "$PROJECT_DIR/assert_contractor_coverage.py" --live >>"$LOG_FILE" 2>&1; then
        log "§5 coverage assertion GREEN (every surface × object correct-for-P)"
    else
        log "CRITICAL: §5 DEPLOY-COVERAGE ASSERTION RED — a public surface lies about coverage (or a surface is unreadable). See GATE_S5 lines above. Script exits non-zero; cron pages."
        SMOKE_FAILED=1
    fi

    # ER-QC reconcile (advisory): re-measure the published methodology error-rates
    # against the FRESH 990 build (D3 over-merge / fragmentation claims) + the openregs
    # entity layer, page on GATE drift = the live methodology page is stale-wrong.
    # Non-fatal; the runner pages via Pushover and exits 0. followup #59/#62.
    log "Post-deploy ER-QC reconcile (advisory)..."
    bash /mnt/data/datadawn/audit_reports/run_erqc_reconcile.sh >>"$LOG_FILE" 2>&1 || true

    # Backup propagation to local + B2 — runs after the deploy succeeded.
    # Failures here do NOT roll back the deploy (it's already live); they
    # just mean this run's snapshot didn't make it to all 3 tiers.
    propagate_990_backup_to_local_and_b2 "${BACKUP_FILE:-}" || \
        log "WARNING: backup propagation exited non-zero (see above). Deploy continues."
fi

log "Upload step complete ($(elapsed "$STEP5_START")s)"

# ── Step 6: Update state ─────────────────────────────────────────────────
if [[ $DEPLOY_ONLY -eq 1 ]]; then
    log "Step 6 SKIPPED (--deploy-only): state file untouched — no new data processed; writing it would make the next monthly skip real filings"
elif ! dry "Would update state file to $TODAY"; then
    echo "$TODAY" > "$STATE_FILE"
    log "State file updated: $TODAY"
fi

# ── Step 7: Render new 990 form HTMLs to R2 ──────────────────────────────
# Renders any new XMLs that have landed since the last run into HTML and
# uploads to the R2-backed forms.datadawn.org. Idempotent — the renderer's
# state file tracks which object_ids have already been pushed and skips
# them. Scoped to current + prior IRS batch year so the scan stays cheap
# (each year dir is ~50-150K XMLs); state filtering handles dedup.
#
# Failures here are non-blocking — the deploy is already live by this
# point; missing renders just mean the form viewer 404s for those filings
# until the next monthly run. Added 2026-05-17 after a site-audit warning
# surfaced 19,196 missing 2026-batch forms because the renderer had been
# manual-only (last run 2026-03-25).
log "--- Step 7: Render new forms to R2 ---"
STEP7_START=$(date +%s)
RENDER_SCRIPT="$PROJECT_DIR/990_viewer/batch_render_upload.py"
RENDER_LOG="$PROJECT_DIR/logs/batch_render.log"
if [[ $DEPLOY_ONLY -eq 1 ]]; then
    log "Step 7 SKIPPED (--deploy-only): no new XMLs parsed this run; renderer state untouched"
elif [[ ! -f "$RENDER_SCRIPT" ]]; then
    log "WARNING: $RENDER_SCRIPT not found, skipping form render step"
elif ! dry "Would render new XMLs (year $(date +%Y) + $(($(date +%Y)-1))) to R2"; then
    RENDER_OK=1
    for YR in $(date +%Y) $(($(date +%Y) - 1)); do
        if python3 "$RENDER_SCRIPT" --year "$YR" --workers 4 \
                >> "$RENDER_LOG" 2>&1; then
            log "  render year=$YR: OK"
        else
            log "  WARNING: render year=$YR exited non-zero (see $RENDER_LOG)"
            RENDER_OK=0
        fi
    done
    if [[ $RENDER_OK -eq 1 ]]; then
        log "Form renderer: OK"
    else
        log "WARNING: form renderer had failures. Deploy continues; will retry next run."
    fi
fi
log "Form render step complete ($(elapsed "$STEP7_START")s)"

# ── Summary ──────────────────────────────────────────────────────────────
log "========================================="
log "Update complete"
FINAL_COUNT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM returns")
log "  Returns in DB:     $FINAL_COUNT"
log "  New files synced:  $NEW_FILES"
# Corpus-wide contractor indeterminate rate — INFORMATIONAL trend line (maintainer, 2026-07-03): the
# per-version ceilings (0.076/teeth 0.10) are structurally blind to slow UNIFORM creep; one line
# per monthly in this log makes creep visible for free. Never gates.
if [[ -f "$PUBLIC_DB" ]]; then
    INDET_TREND=$(sqlite3 "$PUBLIC_DB" "SELECT printf('%d/%d = %.4f%%', SUM(x), COUNT(*), 100.0*SUM(x)/COUNT(*)) FROM (SELECT EXISTS(SELECT 1 FROM contractors c WHERE c.object_id=r.object_id AND c.compensation IS NULL) AS x FROM returns r WHERE r.return_type='990' AND r.contractors_over_100k_cnt IS NOT NULL)" 2>/dev/null || echo "unavailable")
    log "  Contractor indeterminate rate (corpus trend, informational): $INDET_TREND"
fi
# Max-year tie count — INFORMATIONAL trend line (orgs-query provenance, 2026-07-04, followup_queue #289).
# The org search on both deep-dive surfaces dedups one row/ein via GROUP BY ein + MAX(tax_year) and emits
# BARE columns; when an ein has >1 filing at its MAX(tax_year) (amended/re-filed) the bare pick is
# non-deterministic. (ein,tax_year) is NOT unique (PK=object_id) so this is watched, not assumed. One line
# per monthly makes the count MOVE visible (emits the numbers, not "ties: yes"). The structural fix
# (ROW_NUMBER OVER (PARTITION BY ein ORDER BY tax_year DESC, object_id DESC)=1) lands at the next natural
# orgs-query edit. Never gates (maintainer, 2026-07-04).
if [[ -f "$PUBLIC_DB" ]]; then
    TIE_TREND=$(sqlite3 "$PUBLIC_DB" "SELECT printf('ties=%d differing_figures=%d', COUNT(*), COALESCE(SUM(CASE WHEN drev>1 OR dexp>1 THEN 1 ELSE 0 END),0)) FROM (SELECT p.ein, COUNT(DISTINCT p.total_revenue) drev, COUNT(DISTINCT p.total_expenses) dexp FROM returns p JOIN (SELECT ein, MAX(tax_year) mty FROM returns WHERE return_type IN ('990','990EZ','990PF') AND TRIM(ein)<>'' GROUP BY ein) m ON p.ein=m.ein AND p.tax_year=m.mty AND p.return_type IN ('990','990EZ','990PF') AND TRIM(p.ein)<>'' GROUP BY p.ein HAVING COUNT(*)>1)" 2>/dev/null || echo "unavailable")
    log "  Max-year tie count (orgs provenance, informational): $TIE_TREND"
fi
if [[ -f "$PUBLIC_DB" ]]; then
    PUBLIC_SIZE=$(stat --format="%s" "$PUBLIC_DB" 2>/dev/null || stat -f "%z" "$PUBLIC_DB")
    log "  Public DB size:    $(( PUBLIC_SIZE / 1048576 ))MB"
fi
log "========================================="

# Surface any post-deploy smoke-test failure as a non-zero exit so the cron's
# hc.io ping alerts. By this point the deploy is live, the state file is
# updated, the backup chain is propagated — failure here means "prod data
# doesn't match local; investigate" rather than "rebuild from scratch".
if [[ "${SNAPSHOT_CORRUPT:-0}" -eq 1 ]]; then
    log "EXITING with status 6: a snapshot failed quick_check and was quarantined to .corrupt — rollback head reverted to gen-1; investigate."
    exit 6
fi
if [[ "${TRIM_UNSAFE:-0}" -eq 1 ]]; then
    log "EXITING with status 7: keep-1 trim skipped an un-propagated snapshot (not on workstation OR B2) — the deploy itself succeeded; investigate + update.sh --propagate-only."
    exit 7
fi
if [[ "${SMOKE_FAILED:-0}" -eq 1 ]]; then
    log "EXITING with status 4 due to smoke-test failure (see WARNING above)"
    exit 4
fi
if [[ "${SEAM_BREACH:-0}" -eq 1 ]]; then
    log "EXITING with status 8: #92(iii) ingest-seam reconcile breached (DB↔extracted-corpus mismatch, retention-floor violation, or reconcile self-error) — see [seam-reconcile] lines. The deploy itself completed; the data was already in this state. Investigate; never auto-fix."
    exit 8
fi

# Good-news milestone: fully clean finish. Reaching here means we cleared every
# exit-gate above (smoke/seam/snapshot/trim) → genuine exit 0. ${VAR:-} keeps
# it nounset-safe; `|| true` keeps it from ever changing the exit status.
[[ $DRY_RUN -eq 0 ]] && notify_ok "🟢 990 monthly: LIVE" \
    "Deploy complete — data.datadawn.org updated. Returns: ${FINAL_COUNT:-?}, new files: ${NEW_FILES:-0}. All post-deploy checks passed." || true
