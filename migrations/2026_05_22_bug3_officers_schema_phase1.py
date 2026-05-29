#!/usr/bin/env python3
"""
Bug #3 Phase 1 migration: officers + top_employees schema correction.

Bug: the `officers` table had 3 compensation columns named
`compensation` / `benefits` / `expense_account` that were semantically
correct for Form 990-EZ + 990-PF filings but MISLEADING for Form 990.
For Form 990 rows, `benefits` actually held `ReportableCompFromRltdOrgAmt`
(W-2 from related orgs) and `expense_account` held `OtherCompensationAmt`
(IRS Form 990 "other comp" lump). Cross-form analytical queries were
silently wrong.

Fix: 5-column schema, semantically correct per form. New columns:
  - reportable_comp_filing_org    (renamed from `compensation` — populated for ALL forms; W-2 from filing org)
  - reportable_comp_related_org   (NEW — Form 990 ONLY; W-2 from related orgs)
  - other_compensation            (NEW — Form 990 ONLY; IRS "other comp" lump)

Existing columns kept for Form 990-EZ + 990-PF (where they're correctly named):
  - benefits          (Employee benefit program; 990-EZ + 990-PF ONLY)
  - expense_account   (Expense account + allowances; 990-EZ + 990-PF ONLY)

For Form 990 rows, benefits + expense_account are NULL'd out (their data
is now in reportable_comp_related_org + other_compensation).

For top_employees: currently 990-PF only. Schema is updated to match
officers (3 new columns). No Form 990 data migration needed in this
phase — Bug #1 (separate fix) will later add Form 990 top_employees
rows using the new column convention.

Phase 1 (this script):
  - ADD 3 new columns to officers + top_employees
  - Copy compensation → reportable_comp_filing_org (ALL rows, ALL forms)
  - For Form 990 officers: copy benefits → reportable_comp_related_org,
    expense_account → other_compensation, then NULL the originals
  - Sanity checks: SUM equality (exact) + 100-row spot check
  - Create officer_total_comp view

Phase 2 (separate follow-up after code propagation):
  - DROP COLUMN compensation FROM officers + top_employees

References:
  - bestpractices/decisions_log §64 (planned: Bug #3 rationale; will be
    appended after this migration runs)
  - 990_field_coverage_audit_2026-05-22.md (the audit that surfaced Bug #3)
  - 990data.db.pre_bug3_backup_2026_05_22 (recovery artifact)

Usage:
  python3 migrations/2026_05_22_bug3_officers_schema_phase1.py
  python3 migrations/2026_05_22_bug3_officers_schema_phase1.py /path/to/other.db
  python3 migrations/2026_05_22_bug3_officers_schema_phase1.py --dry-run
"""

import sqlite3
import sys

DEFAULT_DB_PATH = '/mnt/data/datadawn/990project/990data.db'
_non_flag = [a for a in sys.argv[1:] if not a.startswith('--')]
DB_PATH = _non_flag[0] if _non_flag else DEFAULT_DB_PATH
DRY_RUN = '--dry-run' in sys.argv


def main():
    mode = 'DRY RUN' if DRY_RUN else 'EXECUTE'
    print(f"=== Bug #3 Phase 1 schema migration ({mode}) ===")
    print(f"Database: {DB_PATH}")
    print()

    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode=WAL")

    # ── Pre-migration: capture totals + 100-row sample ──────────────
    print("Capturing pre-migration baseline...")
    pre_sums = {}
    for tbl in ['officers', 'top_employees']:
        pre_sums[tbl] = {
            'all_comp': con.execute(
                f'SELECT COALESCE(SUM(compensation), 0) FROM {tbl}').fetchone()[0],
            'f990_benefits': con.execute(
                f"SELECT COALESCE(SUM(benefits), 0) FROM {tbl} "
                f"WHERE object_id IN (SELECT object_id FROM returns WHERE return_type='990')").fetchone()[0],
            'f990_expense': con.execute(
                f"SELECT COALESCE(SUM(expense_account), 0) FROM {tbl} "
                f"WHERE object_id IN (SELECT object_id FROM returns WHERE return_type='990')").fetchone()[0],
            'ez_benefits': con.execute(
                f"SELECT COALESCE(SUM(benefits), 0) FROM {tbl} "
                f"WHERE object_id IN (SELECT object_id FROM returns WHERE return_type='990EZ')").fetchone()[0],
            'ez_expense': con.execute(
                f"SELECT COALESCE(SUM(expense_account), 0) FROM {tbl} "
                f"WHERE object_id IN (SELECT object_id FROM returns WHERE return_type='990EZ')").fetchone()[0],
            'pf_benefits': con.execute(
                f"SELECT COALESCE(SUM(benefits), 0) FROM {tbl} "
                f"WHERE object_id IN (SELECT object_id FROM returns WHERE return_type='990PF')").fetchone()[0],
            'pf_expense': con.execute(
                f"SELECT COALESCE(SUM(expense_account), 0) FROM {tbl} "
                f"WHERE object_id IN (SELECT object_id FROM returns WHERE return_type='990PF')").fetchone()[0],
        }
        print(f"  {tbl}:")
        for k, v in pre_sums[tbl].items():
            print(f"    {k:>18s} = {v:>20,}")

    # 100-row random sample of Form 990 officers
    print("\nSampling 100 random Form 990 officers for per-row spot check...")
    sample = con.execute('''
        SELECT id, compensation, benefits, expense_account FROM officers
        WHERE object_id IN (SELECT object_id FROM returns WHERE return_type='990')
        ORDER BY RANDOM() LIMIT 100
    ''').fetchall()
    print(f"  Captured {len(sample)} rows")

    if DRY_RUN:
        print("\n[DRY RUN] Would execute:")
        print("  1. ALTER TABLE officers ADD COLUMN reportable_comp_filing_org INTEGER")
        print("  2. ALTER TABLE officers ADD COLUMN reportable_comp_related_org INTEGER")
        print("  3. ALTER TABLE officers ADD COLUMN other_compensation INTEGER")
        print("  4. Same 3 ALTERs for top_employees")
        print("  5. UPDATE officers SET reportable_comp_filing_org = compensation (all rows)")
        print("  6. UPDATE top_employees SET reportable_comp_filing_org = compensation (all rows)")
        print("  7. UPDATE officers SET reportable_comp_related_org = benefits, "
              "other_compensation = expense_account WHERE Form 990")
        print("  8. UPDATE officers SET benefits = NULL, expense_account = NULL WHERE Form 990")
        print("  9. CREATE VIEW officer_total_comp")
        print("  Sanity checks throughout.")
        print()
        print("Phase 2 (separate script): DROP COLUMN compensation FROM both tables.")
        con.close()
        return

    # ── Step 1-2: ADD new columns ──────────────────────────────────
    print("\nStep 1-2: ADD new columns...")
    for tbl in ['officers', 'top_employees']:
        for col in ['reportable_comp_filing_org', 'reportable_comp_related_org', 'other_compensation']:
            try:
                con.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} INTEGER")
                print(f"  {tbl}.{col} added")
            except sqlite3.OperationalError as e:
                if 'duplicate column' in str(e).lower():
                    print(f"  {tbl}.{col} already exists, skipping (migration may have run before)")
                else:
                    raise
    con.commit()

    # ── Step 3-4: Copy compensation → reportable_comp_filing_org ────
    print("\nStep 3-4: Copy compensation → reportable_comp_filing_org (ALL rows, ALL forms)...")
    for tbl in ['officers', 'top_employees']:
        cur = con.execute(f"UPDATE {tbl} SET reportable_comp_filing_org = compensation")
        print(f"  {tbl}: {cur.rowcount:,} rows updated")
    con.commit()

    # ── Step 5: Form 990 mapping ────────────────────────────────────
    print("\nStep 5: Form 990 mapping (officers benefits → reportable_comp_related_org, "
          "expense_account → other_compensation)...")
    cur = con.execute('''
        UPDATE officers
        SET reportable_comp_related_org = benefits,
            other_compensation = expense_account
        WHERE object_id IN (SELECT object_id FROM returns WHERE return_type='990')
    ''')
    print(f"  officers Form 990: {cur.rowcount:,} rows updated")
    # top_employees: currently 990-PF only, no Form 990 rows. Bug #1 will add.
    con.commit()

    # ── Sanity check #1: SUM equality, post-copy pre-NULL-out ──────
    print("\nSanity check #1: SUM equality (exact) post-copy pre-NULL-out:")
    failures = []

    for tbl in ['officers', 'top_employees']:
        post = con.execute(f'SELECT COALESCE(SUM(reportable_comp_filing_org), 0) FROM {tbl}').fetchone()[0]
        expected = pre_sums[tbl]['all_comp']
        ok = post == expected
        status = "OK" if ok else "FAIL"
        print(f"  {tbl} SUM(reportable_comp_filing_org)/ALL = {post:>20,} "
              f"(expected {expected:>20,}) {status}")
        if not ok:
            failures.append(f"{tbl} SUM(reportable_comp_filing_org) mismatch")

    post_rcro = con.execute('''
        SELECT COALESCE(SUM(reportable_comp_related_org), 0) FROM officers
        WHERE object_id IN (SELECT object_id FROM returns WHERE return_type='990')
    ''').fetchone()[0]
    expected = pre_sums['officers']['f990_benefits']
    ok = post_rcro == expected
    print(f"  officers SUM(reportable_comp_related_org)/990 = {post_rcro:>20,} "
          f"(expected {expected:>20,}) {'OK' if ok else 'FAIL'}")
    if not ok:
        failures.append("officers SUM(reportable_comp_related_org) mismatch")

    post_oc = con.execute('''
        SELECT COALESCE(SUM(other_compensation), 0) FROM officers
        WHERE object_id IN (SELECT object_id FROM returns WHERE return_type='990')
    ''').fetchone()[0]
    expected = pre_sums['officers']['f990_expense']
    ok = post_oc == expected
    print(f"  officers SUM(other_compensation)/990 = {post_oc:>20,} "
          f"(expected {expected:>20,}) {'OK' if ok else 'FAIL'}")
    if not ok:
        failures.append("officers SUM(other_compensation) mismatch")

    if failures:
        print("\nFAIL: aborting (DB state: new columns added + populated, benefits/expense unchanged)")
        print("  Failures:", failures)
        print("  Restore from /mnt/data/datadawn/990project/990data.db.pre_bug3_backup_2026_05_22 if needed.")
        con.close()
        sys.exit(2)

    # ── 100-row spot check ─────────────────────────────────────────
    print("\nSanity check #2: 100-row spot check (Form 990 officers)...")
    placeholders = ','.join('?' * len(sample))
    sample_ids = [s[0] for s in sample]
    post_rows = {
        r[0]: r for r in con.execute(
            f'SELECT id, reportable_comp_filing_org, reportable_comp_related_org, other_compensation '
            f'FROM officers WHERE id IN ({placeholders})',
            sample_ids
        )
    }
    mismatches = []
    for s_id, s_comp, s_benefits, s_expense in sample:
        post = post_rows.get(s_id)
        if not post:
            mismatches.append((s_id, 'row missing post-migration'))
            continue
        if post[1] != s_comp:
            mismatches.append((s_id, f'rcfo {post[1]} != pre compensation {s_comp}'))
        if post[2] != s_benefits:
            mismatches.append((s_id, f'rcro {post[2]} != pre benefits {s_benefits}'))
        if post[3] != s_expense:
            mismatches.append((s_id, f'oc {post[3]} != pre expense_account {s_expense}'))
    if mismatches:
        print(f"  FAIL: {len(mismatches)} mismatches in 100-row sample")
        for m in mismatches[:10]:
            print(f"    {m}")
        con.close()
        sys.exit(2)
    print(f"  All 100 rows verified: rcfo == pre comp, rcro == pre benefits, oc == pre expense_account  OK")

    # ── Step 6: NULL out Form 990 benefits + expense_account ───────
    print("\nStep 6: NULL out Form 990 benefits + expense_account (now redundant)...")
    cur = con.execute('''
        UPDATE officers SET benefits = NULL, expense_account = NULL
        WHERE object_id IN (SELECT object_id FROM returns WHERE return_type='990')
    ''')
    print(f"  officers Form 990: {cur.rowcount:,} rows updated")
    con.commit()

    # ── Sanity check #3: post-NULL-out ─────────────────────────────
    print("\nSanity check #3: post-NULL-out invariants:")
    f990_b = con.execute(
        "SELECT COALESCE(SUM(benefits), 0) FROM officers "
        "WHERE object_id IN (SELECT object_id FROM returns WHERE return_type='990')").fetchone()[0]
    f990_e = con.execute(
        "SELECT COALESCE(SUM(expense_account), 0) FROM officers "
        "WHERE object_id IN (SELECT object_id FROM returns WHERE return_type='990')").fetchone()[0]
    if f990_b != 0 or f990_e != 0:
        print(f"  FAIL: officers Form 990 benefits={f990_b:,}, expense={f990_e:,} (expected 0/0)")
        con.close()
        sys.exit(2)
    print(f"  officers Form 990: benefits=0, expense_account=0  OK")

    # Verify 990EZ + 990PF unchanged (officers + top_employees)
    for tbl in ['officers', 'top_employees']:
        for rt_label, key_prefix in [('990EZ', 'ez'), ('990PF', 'pf')]:
            post_b = con.execute(
                f"SELECT COALESCE(SUM(benefits), 0) FROM {tbl} "
                f"WHERE object_id IN (SELECT object_id FROM returns WHERE return_type=?)",
                (rt_label,)).fetchone()[0]
            post_e = con.execute(
                f"SELECT COALESCE(SUM(expense_account), 0) FROM {tbl} "
                f"WHERE object_id IN (SELECT object_id FROM returns WHERE return_type=?)",
                (rt_label,)).fetchone()[0]
            exp_b = pre_sums[tbl][f'{key_prefix}_benefits']
            exp_e = pre_sums[tbl][f'{key_prefix}_expense']
            if post_b != exp_b or post_e != exp_e:
                print(f"  FAIL: {tbl} {rt_label}: benefits {post_b:,} (expected {exp_b:,}), "
                      f"expense {post_e:,} (expected {exp_e:,})")
                con.close()
                sys.exit(2)
            print(f"  {tbl} {rt_label}: benefits + expense_account unchanged  OK")

    # ── Step 7: CREATE VIEW officer_total_comp ──────────────────────
    print("\nStep 7: Create officer_total_comp view...")
    con.execute("DROP VIEW IF EXISTS officer_total_comp")
    con.execute('''
        CREATE VIEW officer_total_comp AS
        SELECT
            o.id, o.object_id, o.ein, o.person_name, o.title, o.avg_hours_per_week,
            o.reportable_comp_filing_org,
            o.reportable_comp_related_org,
            o.other_compensation,
            o.benefits,
            o.expense_account,
            r.return_type, r.tax_year,
            -- total_comp: cross-form normalized "all compensation" rollup.
            -- For Form 990: rcfo + rcro + other_comp (the 3 IRS columns).
            -- For 990-EZ/990-PF: rcfo + benefits + expense_account (the 3 IRS columns).
            -- The 5-way COALESCE works because Form 990 has benefits/expense_account NULL,
            -- and 990-EZ/PF have rcro/other_comp NULL — only the populated columns sum.
            -- See decisions_log §64 caveats: may double-count related-org W-2 if related org
            -- also files its own 990 listing same person; for form-specific analysis use
            -- underlying columns directly.
            COALESCE(o.reportable_comp_filing_org, 0)
              + COALESCE(o.reportable_comp_related_org, 0)
              + COALESCE(o.other_compensation, 0)
              + COALESCE(o.benefits, 0)
              + COALESCE(o.expense_account, 0) AS total_comp
        FROM officers o
        JOIN returns r ON r.object_id = o.object_id
    ''')
    print("  officer_total_comp view created")
    con.commit()

    sample_view = con.execute(
        "SELECT COUNT(*), COALESCE(SUM(total_comp), 0) FROM officer_total_comp WHERE total_comp > 0"
    ).fetchone()
    print(f"  Sanity: view returns {sample_view[0]:,} rows with total_comp>0, "
          f"SUM(total_comp)={sample_view[1]:,}")

    # ── Final summary ──────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("=== Phase 1 migration complete ===")
    print()
    print("Post-migration officers schema:")
    for r in con.execute("PRAGMA table_info(officers)"):
        print(f"  {r[1]:30s} {r[2]}")
    print()
    print("Phase 2 (DROP COLUMN compensation) is queued for a separate migration script,")
    print("to be run AFTER code propagation lands and is verified in production.")

    con.close()


if __name__ == '__main__':
    main()
