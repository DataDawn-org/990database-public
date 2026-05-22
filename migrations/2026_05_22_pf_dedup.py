#!/usr/bin/env python3
"""
One-shot migration: dedup duplicate rows in 990-PF-extracted tables caused by
extract_990pf_detail.py idempotency bug (no writer-side per-table guards).

Symptom: 18-48% dup rate across PF officers, contributors, contractors,
top_employees, investments, capital_gains, program_activities,
program_investments. ~14.8M excess rows.

Root cause: extract_990pf_detail.py::discover_pf_files used membership in the
grants table as the sole "already-processed" signal. 990-PF filings with zero
grants (small foundations) never enter `grants`, are re-discovered on every
cron run, and re-extract into officers/contributors/contractors/etc. with NO
writer-side guard to catch the re-insertion.

Companion code fix (same push): writer-side per-table skip guards added to
extract_990pf_detail.py per the canonical extract_990_detail.py pattern.
After both fixes, future runs cannot dup; this script cleans up the historic
backlog.

Fix sequence (in this script, per table):
1. Pre-count rows (PF only).
2. Predict excess via window-function GROUP BY natural key.
3. DELETE keeping MIN(id) per natural key.
4. Sanity: post-count == pre-count - predicted_excess (EXACT equality).
5. Verify: re-running the excess query returns 0.

Natural keys are full-column equality per decisions_log §63. The
capital_gains residual same-day-same-price edge case is accepted per §62.

References:
  - bestpractices/incident_log.md 2026-05-22 entry
  - bestpractices/decisions_log.md §61 (parser idempotency rule)
  - bestpractices/decisions_log.md §62 (capital_gains residual edge accepted)
  - bestpractices/decisions_log.md §63 (true-bug deflation methodology)
  - 990project/990data.db.pre_pf_dedup_backup_2026_05_22 (recovery artifact)

Usage:
  python3 migrations/2026_05_22_pf_dedup.py            # execute
  python3 migrations/2026_05_22_pf_dedup.py --dry-run  # report only
"""

import sqlite3
import sys
import time

DEFAULT_DB_PATH = '/mnt/data/datadawn/990project/990data.db'
# Optional positional arg overrides the default DB path (e.g. to run the same
# dedup against 990data_public.db). Use:
#   python3 migrations/2026_05_22_pf_dedup.py
#   python3 migrations/2026_05_22_pf_dedup.py /path/to/990data_public.db
#   python3 migrations/2026_05_22_pf_dedup.py --dry-run
#   python3 migrations/2026_05_22_pf_dedup.py /path/to/other.db --dry-run
_non_flag = [a for a in sys.argv[1:] if not a.startswith('--')]
DB_PATH = _non_flag[0] if _non_flag else DEFAULT_DB_PATH
DRY_RUN = '--dry-run' in sys.argv

# Per-table natural keys: all non-id, non-source columns.
# A row is a re-extraction dup iff identical on ALL of these columns.
# Differing in any column => both rows kept (legit multi-record).
DEDUP_KEYS = {
    'officers': ['object_id', 'ein', 'person_name', 'title',
                 'avg_hours_per_week', 'compensation', 'benefits', 'expense_account'],
    'top_employees': ['object_id', 'ein', 'person_name', 'title',
                      'avg_hours_per_week', 'compensation', 'benefits', 'expense_account'],
    'contributors': ['object_id', 'ein', 'contributor_name', 'city', 'state',
                     'zip', 'amount', 'contributor_type'],
    'contractors': ['object_id', 'ein', 'contractor_name', 'city', 'state',
                    'service_type', 'compensation'],
    'investments': ['object_id', 'ein', 'investment_type', 'description',
                    'book_value', 'fmv', 'cost_basis'],
    'capital_gains': ['object_id', 'ein', 'property_desc', 'how_acquired',
                      'acquired_date', 'sold_date', 'gross_sale_price',
                      'cost_basis', 'gain_or_loss'],
    'program_activities': ['object_id', 'ein', 'activity_num', 'description', 'expenses'],
    'program_investments': ['object_id', 'ein', 'description', 'amount'],
}

PF_FILTER = "object_id IN (SELECT object_id FROM returns WHERE return_type='990PF')"


def main():
    mode = 'DRY RUN' if DRY_RUN else 'EXECUTE'
    print(f"=== PF dedup migration ({mode}) ===")
    print(f"Database: {DB_PATH}")
    print()

    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode=WAL")

    grand_pre = grand_excess = grand_deleted = 0

    for table, keys in DEDUP_KEYS.items():
        partition_cols = ', '.join(keys)
        t0 = time.time()

        # Pre-count: PF rows in this table.
        pre_count = con.execute(f'''
            SELECT COUNT(*) FROM {table} WHERE {PF_FILTER}
        ''').fetchone()[0]

        # Predicted excess: rows beyond first per natural key.
        predicted_excess = con.execute(f'''
            SELECT COALESCE(SUM(n - 1), 0) FROM (
                SELECT COUNT(*) as n FROM {table}
                WHERE {PF_FILTER}
                GROUP BY {partition_cols}
            )
        ''').fetchone()[0]

        print(f"{table}:")
        print(f"  Pre-count (PF rows):     {pre_count:>12,}")
        print(f"  Predicted excess:        {predicted_excess:>12,}")

        if DRY_RUN:
            print(f"  [DRY RUN] would delete:  {predicted_excess:>12,}")
            grand_pre += pre_count
            grand_excess += predicted_excess
            print()
            continue

        # DELETE keeping MIN(id) per natural key.
        delete_sql = f'''
            DELETE FROM {table} WHERE id IN (
                SELECT id FROM (
                    SELECT id, ROW_NUMBER() OVER (
                        PARTITION BY {partition_cols}
                        ORDER BY id
                    ) AS rn FROM {table}
                    WHERE {PF_FILTER}
                ) WHERE rn > 1
            )
        '''
        cur = con.execute(delete_sql)
        deleted = cur.rowcount
        con.commit()

        post_count = con.execute(f'''
            SELECT COUNT(*) FROM {table} WHERE {PF_FILTER}
        ''').fetchone()[0]

        # Sanity: post = pre - predicted_excess (EXACT equality).
        expected_post = pre_count - predicted_excess
        if post_count != expected_post:
            print(f"  FAIL: expected post={expected_post:,}, got {post_count:,}")
            con.close()
            sys.exit(2)

        # Verify zero remaining dups.
        remaining = con.execute(f'''
            SELECT COALESCE(SUM(n - 1), 0) FROM (
                SELECT COUNT(*) as n FROM {table}
                WHERE {PF_FILTER}
                GROUP BY {partition_cols}
            )
        ''').fetchone()[0]
        if remaining > 0:
            print(f"  FAIL: {remaining:,} dups remain post-dedup")
            con.close()
            sys.exit(2)

        elapsed = time.time() - t0
        print(f"  Deleted:                 {deleted:>12,}")
        print(f"  Post-count:              {post_count:>12,}")
        print(f"  Sanity: post == pre - excess ({post_count:,} == {pre_count:,} - {predicted_excess:,})  OK")
        print(f"  Verify: 0 dups remaining  OK")
        print(f"  Elapsed:                 {elapsed:>12.1f}s")
        print()

        grand_pre += pre_count
        grand_excess += predicted_excess
        grand_deleted += deleted

    print("=" * 60)
    print("Summary:")
    print(f"  Tables processed:    {len(DEDUP_KEYS)}")
    print(f"  Total pre-count:     {grand_pre:>12,}")
    print(f"  Total excess:        {grand_excess:>12,}")
    if not DRY_RUN:
        print(f"  Total deleted:       {grand_deleted:>12,}")
        if grand_pre > 0:
            print(f"  Overall dedup rate:  {grand_deleted/grand_pre*100:.2f}%")

    con.close()


if __name__ == '__main__':
    main()
