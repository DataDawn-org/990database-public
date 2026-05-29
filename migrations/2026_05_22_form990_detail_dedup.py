#!/usr/bin/env python3
"""
One-shot follow-up migration: dedup residual duplicate rows in Form 990 / 990EZ
detail tables (officers, schedule_i_990, related_orgs).

Distinct from the PF dedup migration (`2026_05_22_pf_dedup.py`):
extract_990_detail.py already HAS writer-side per-table guards (the canonical
pattern that decisions_log §61 codifies). The residual ~80K excess rows are
historic dups that predate the writer guards OR resulted from edge cases
(partial runs, manual interventions, etc.). The fix is data cleanup only,
no code change needed.

Magnitude: 80,105 true-bug excess rows / 56,579,363 total = 0.142% across 3
tables.

Per-table excess (precomputed 2026-05-22 via full-column equality per §63):
  - 990 officers:       14,457 / 31,298,448 (0.046%)
  - 990 schedule_i_990: 47,338 /  6,768,324 (0.699%)
  - 990 related_orgs:   12,083 /  9,065,459 (0.133%)
  - 990EZ officers:      6,227 /  9,447,132 (0.066%)

Same dedup pattern as PF — full-column equality, keep MIN(id).

References:
  - bestpractices/incident_log.md 2026-05-22 entry (PF idempotency bug, where
    this residual was identified during the Tier 0 audit)
  - bestpractices/decisions_log.md §61 (parser idempotency rule)
  - bestpractices/decisions_log.md §63 (true-bug deflation methodology)

Usage:
  python3 migrations/2026_05_22_form990_detail_dedup.py            # execute
  python3 migrations/2026_05_22_form990_detail_dedup.py --dry-run  # report only
"""

import sqlite3
import sys
import time

DEFAULT_DB_PATH = '/mnt/data/datadawn/990project/990data.db'
# Optional positional arg overrides default (same convention as the PF dedup).
_non_flag = [a for a in sys.argv[1:] if not a.startswith('--')]
DB_PATH = _non_flag[0] if _non_flag else DEFAULT_DB_PATH
DRY_RUN = '--dry-run' in sys.argv

# Per-table natural keys: full-column equality. Same definition as PF dedup.
# A row is a true-bug dup iff identical on ALL data columns.
DEDUP_TARGETS = [
    # (table, partition_cols, return_type_filter)
    ('officers',
     ['object_id', 'ein', 'person_name', 'title', 'avg_hours_per_week',
      'compensation', 'benefits', 'expense_account'],
     "object_id IN (SELECT object_id FROM returns WHERE return_type IN ('990', '990EZ'))"),
    ('schedule_i_990',
     ['object_id', 'ein', 'recipient_name', 'recipient_ein', 'recipient_city',
      'recipient_state', 'recipient_zip', 'irc_section', 'cash_grant_amt',
      'non_cash_amt', 'purpose'],
     "object_id IN (SELECT object_id FROM returns WHERE return_type='990')"),
    ('related_orgs',
     ['object_id', 'ein', 'related_org_name', 'related_ein', 'city', 'state',
      'zip', 'primary_activity', 'legal_domicile', 'exempt_code_section',
      'public_charity_status', 'direct_controlling', 'controlled_org_ind',
      'section'],
     "object_id IN (SELECT object_id FROM returns WHERE return_type='990')"),
]


def main():
    mode = 'DRY RUN' if DRY_RUN else 'EXECUTE'
    print(f"=== Form 990 detail dedup migration ({mode}) ===")
    print(f"Database: {DB_PATH}")
    print()

    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode=WAL")

    grand_pre = grand_excess = grand_deleted = 0

    for table, keys, filter_clause in DEDUP_TARGETS:
        partition_cols = ', '.join(keys)
        t0 = time.time()

        pre_count = con.execute(f'SELECT COUNT(*) FROM {table} WHERE {filter_clause}').fetchone()[0]

        predicted_excess = con.execute(f'''
            SELECT COALESCE(SUM(n - 1), 0) FROM (
                SELECT COUNT(*) as n FROM {table} WHERE {filter_clause}
                GROUP BY {partition_cols}
            )
        ''').fetchone()[0]

        print(f"{table}:")
        print(f"  Pre-count:           {pre_count:>12,}")
        print(f"  Predicted excess:    {predicted_excess:>12,}")

        if DRY_RUN:
            print(f"  [DRY RUN] would delete: {predicted_excess:,}")
            grand_pre += pre_count
            grand_excess += predicted_excess
            print()
            continue

        delete_sql = f'''
            DELETE FROM {table} WHERE id IN (
                SELECT id FROM (
                    SELECT id, ROW_NUMBER() OVER (
                        PARTITION BY {partition_cols}
                        ORDER BY id
                    ) AS rn FROM {table} WHERE {filter_clause}
                ) WHERE rn > 1
            )
        '''
        cur = con.execute(delete_sql)
        deleted = cur.rowcount
        con.commit()

        post_count = con.execute(f'SELECT COUNT(*) FROM {table} WHERE {filter_clause}').fetchone()[0]
        expected_post = pre_count - predicted_excess
        if post_count != expected_post:
            print(f"  FAIL: expected post={expected_post:,}, got {post_count:,}")
            con.close()
            sys.exit(2)

        remaining = con.execute(f'''
            SELECT COALESCE(SUM(n - 1), 0) FROM (
                SELECT COUNT(*) as n FROM {table} WHERE {filter_clause}
                GROUP BY {partition_cols}
            )
        ''').fetchone()[0]
        if remaining > 0:
            print(f"  FAIL: {remaining:,} dups remain post-dedup")
            con.close()
            sys.exit(2)

        elapsed = time.time() - t0
        print(f"  Deleted:             {deleted:>12,}")
        print(f"  Post-count:          {post_count:>12,}")
        print(f"  Sanity OK, 0 remaining, {elapsed:.1f}s")
        print()

        grand_pre += pre_count
        grand_excess += predicted_excess
        grand_deleted += deleted

    print("=" * 60)
    print(f"Total pre:     {grand_pre:,}")
    print(f"Total excess:  {grand_excess:,}")
    if not DRY_RUN:
        print(f"Total deleted: {grand_deleted:,}")
        if grand_pre > 0:
            print(f"Overall rate:  {grand_deleted/grand_pre*100:.3f}%")

    con.close()


if __name__ == '__main__':
    main()
