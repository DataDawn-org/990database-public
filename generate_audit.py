#!/usr/bin/env python3
"""Generate a data quality audit report for the 990 database.

Run after build/update to produce a graded markdown report.
Saves to build_reports/audit_YYYYMMDD.md and build_reports/audit_latest.md.
"""

import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
PUBLIC_DB = PROJECT_DIR / "990data_public.db"
PRIVATE_DB = PROJECT_DIR / "990data.db"
REPORT_DIR = PROJECT_DIR / "build_reports"


def q1(conn, sql):
    return conn.execute(sql).fetchone()[0]


def qrow(conn, sql):
    cur = conn.execute(sql)
    cols = [d[0] for d in cur.description]
    row = cur.fetchone()
    return dict(zip(cols, row)) if row else {}


def qall(conn, sql):
    cur = conn.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def fmt_count(n):
    if n is None:
        return "—"
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return f"{n:,}"


def fmt_money(n):
    if n is None or n == 0:
        return "—"
    if abs(n) >= 1_000_000_000_000:
        return f"${n/1_000_000_000_000:.1f}T"
    if abs(n) >= 1_000_000_000:
        return f"${n/1_000_000_000:.1f}B"
    if abs(n) >= 1_000_000:
        return f"${n/1_000_000:.1f}M"
    return f"${n:,.0f}"


def generate(db_path=None):
    if db_path is None:
        db_path = PUBLIC_DB

    if not db_path.exists():
        print(f"Database not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    REPORT_DIR.mkdir(exist_ok=True)
    now = datetime.now(timezone.utc)
    conn = sqlite3.connect(str(db_path))

    db_size_bytes = db_path.stat().st_size
    db_size_gb = round(db_size_bytes / (1024**3), 1)

    # Count core tables (exclude FTS shadow tables)
    all_tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '%_fts%' "
        "AND name NOT LIKE 'fts_%' ORDER BY name"
    )]
    total_tables = len(all_tables)

    # ── Table counts ─────────────────────────────────────────────
    table_counts = {}
    for t in all_tables:
        try:
            table_counts[t] = q1(conn, f"SELECT COUNT(*) FROM [{t}]")
        except Exception:
            table_counts[t] = -1

    total_rows = sum(c for c in table_counts.values() if c > 0)

    # ── Sections ─────────────────────────────────────────────────
    sections = []

    # Returns
    ret = qrow(conn, "SELECT COUNT(*) AS cnt, MIN(tax_year) AS mn, MAX(tax_year) AS mx, COUNT(DISTINCT tax_year) AS years FROM returns")
    ret_types = qall(conn, "SELECT return_type, COUNT(*) AS cnt FROM returns GROUP BY return_type ORDER BY cnt DESC")
    ret_breakdown = ", ".join(f"{r['return_type']}: {fmt_count(r['cnt'])}" for r in ret_types)
    sections.append((
        "IRS 990 Returns", "A",
        f"{ret['cnt']:,}",
        f"Tax years {ret['mn']}–{ret['mx']} ({ret['years']} years)",
        ret_breakdown,
    ))

    # Grants
    gr = qrow(conn, "SELECT COUNT(*) AS cnt FROM grants")
    gr_types = qall(conn, "SELECT grant_type, COUNT(*) AS cnt, SUM(amount) AS total FROM grants GROUP BY grant_type ORDER BY cnt DESC")
    gr_detail = ", ".join(f"{r['grant_type']}: {fmt_count(r['cnt'])} ({fmt_money(r['total'])})" for r in gr_types)
    sections.append(("Foundation Grants (990PF)", "A", f"{gr['cnt']:,}", gr_detail, "Filter grant_type = 'paid' for disbursements"))

    # Officers
    off = qrow(conn, """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN compensation > 0 THEN 1 ELSE 0 END) AS with_comp,
               ROUND(AVG(CASE WHEN compensation > 0 THEN compensation END)) AS avg_comp
        FROM officers
    """)
    off_pct = round(100 * (off['with_comp'] or 0) / off['total'], 1) if off['total'] else 0
    sections.append((
        "Officers/Directors", "A",
        f"{off['total']:,}",
        f"{off_pct}% with compensation > $0",
        f"Avg compensation (where > $0): ${off['avg_comp']:,.0f}" if off['avg_comp'] else "",
    ))

    # Other detail tables
    detail_tables = [
        ("capital_gains", "Capital Gains (990PF)", "A"),
        ("related_orgs", "Related Organizations", "A"),
        ("schedule_i_990", "Schedule I (990 Grants)", "A"),
        ("investments", "Investments (990PF)", "A"),
        ("contributors", "Contributors (990PF)", "A"),
        ("program_activities", "Program Activities", "A"),
        ("program_investments", "Program Investments", "A"),
        ("contractors", "Top Contractors", "A"),
        ("top_employees", "Top Employees", "A"),
    ]
    for tbl, label, grade in detail_tables:
        cnt = table_counts.get(tbl, 0)
        if cnt > 0:
            sections.append((label, grade, f"{cnt:,}", "", ""))

    # BMF
    bmf = qrow(conn, "SELECT COUNT(*) AS cnt FROM bmf")
    bmf_top = qall(conn, "SELECT subsection, COUNT(*) AS cnt FROM bmf GROUP BY subsection ORDER BY cnt DESC LIMIT 3")
    bmf_detail = ", ".join(f"501(c)({r['subsection']}): {fmt_count(r['cnt'])}" for r in bmf_top)
    sections.append(("BMF (Master File)", "A+", f"{bmf['cnt']:,}", bmf_detail, "All registered nonprofits"))

    # Schedule I grants (DAF)
    si = qrow(conn, "SELECT COUNT(*) AS cnt FROM schedule_i_grants")
    si_types = qall(conn, "SELECT grant_type, COUNT(*) AS cnt, SUM(amount) AS total FROM schedule_i_grants GROUP BY grant_type ORDER BY cnt DESC")
    si_detail = ", ".join(f"{r['grant_type']}: {fmt_count(r['cnt'])} ({fmt_money(r['total'])})" for r in si_types)
    sections.append(("DAF Disbursements", "A", f"{si['cnt']:,}", si_detail, ""))

    # ── FTS verification ─────────────────────────────────────────
    fts_names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND (name LIKE 'fts_%' OR name LIKE '%_fts') "
        "AND name NOT LIKE '%_config' AND name NOT LIKE '%_data' "
        "AND name NOT LIKE '%_idx' AND name NOT LIKE '%_docsize' "
        "AND name NOT LIKE '%_content' ORDER BY name"
    )]
    fts_results = []
    for tname in fts_names:
        try:
            cnt = conn.execute(
                f"SELECT COUNT(*) FROM [{tname}] WHERE [{tname}] MATCH 'education'"
            ).fetchone()[0]
            fts_results.append((tname, cnt, "OK"))
        except Exception as e:
            fts_results.append((tname, 0, f"ERROR: {e}"))

    fts_ok = sum(1 for _, _, s in fts_results if s == "OK")

    # ── Quality flags ────────────────────────────────────────────
    flags = []

    # Duplicate returns
    dupes = q1(conn, """
        SELECT COUNT(*) FROM (
            SELECT ein, tax_year, COUNT(*) AS c
            FROM returns WHERE return_type IN ('990','990EZ')
            GROUP BY ein, tax_year HAVING c > 1
        )
    """)
    if dupes > 0:
        flags.append(f"**{dupes:,} duplicate EIN+tax_year pairs** in 990/990EZ (amended returns)")

    # 2021 dip check
    try:
        y2020 = q1(conn, "SELECT COUNT(*) FROM returns WHERE tax_year = 2020")
        y2021 = q1(conn, "SELECT COUNT(*) FROM returns WHERE tax_year = 2021")
        y2022 = q1(conn, "SELECT COUNT(*) FROM returns WHERE tax_year = 2022")
        if y2021 < y2020 * 0.8 and y2021 < y2022 * 0.8:
            flags.append(f"**2021 tax year dip**: {y2021:,} returns vs {y2020:,} (2020) and {y2022:,} (2022)")
    except Exception:
        pass

    # 990T reminder
    t_count = q1(conn, "SELECT COUNT(*) FROM returns WHERE return_type = '990T'")
    if t_count > 0:
        flags.append(f"**{t_count:,} 990-T filings** — filter `return_type IN ('990', '990EZ')` for revenue analysis")

    # ── Build report ─────────────────────────────────────────────
    lines = [
        "# 990 Database — Full Audit Report",
        "",
        f"**Generated**: {now.strftime('%Y-%m-%d %H:%M UTC')} (auto-generated by generate_audit.py)",
        f"**Database**: {db_size_gb} GB, {total_tables} core tables, {len(fts_results)} FTS indexes ({fts_ok} verified OK)",
        f"**Total rows**: {total_rows:,}",
        "",
        "---",
        "",
        "## Data Source Grades",
        "",
        "| Dataset | Grade | Records | Coverage | Notes |",
        "|---------|-------|---------|----------|-------|",
    ]

    for title, grade, rows, coverage, notes in sections:
        lines.append(f"| {title} | **{grade}** | {rows} | {coverage} | {notes} |")

    lines.extend([
        "",
        "---",
        "",
        "## Full-Text Search Indexes",
        "",
        "| Index | Test Matches ('education') | Status |",
        "|-------|---------------------------|--------|",
    ])
    for tname, cnt, status in fts_results:
        lines.append(f"| `{tname}` | {cnt:,} | {status} |")

    if flags:
        lines.extend([
            "",
            "---",
            "",
            "## Data Quality Flags",
            "",
        ])
        for i, flag in enumerate(flags, 1):
            lines.append(f"{i}. {flag}")

    # All tables
    lines.extend([
        "",
        "---",
        "",
        "## All Tables",
        "",
        "| Table | Rows |",
        "|-------|------|",
    ])
    for t, c in sorted(table_counts.items()):
        lines.append(f"| `{t}` | {c:,} |" if c >= 0 else f"| `{t}` | (error) |")

    lines.extend([
        "",
        "---",
        "",
        f"*Auto-generated by `generate_audit.py` — {now.strftime('%Y-%m-%d')}*",
    ])

    report_text = "\n".join(lines)

    # Save
    filename = now.strftime("%Y%m%d_%H%M%S")
    (REPORT_DIR / f"audit_{filename}.md").write_text(report_text)
    (REPORT_DIR / "audit_latest.md").write_text(report_text)

    print(f"Audit report saved: {REPORT_DIR / f'audit_{filename}.md'}")
    print(f"Audit latest: {REPORT_DIR / 'audit_latest.md'}")

    # Console summary
    grade_counts = {}
    for _, grade, _, _, _ in sections:
        grade_counts[grade] = grade_counts.get(grade, 0) + 1
    print(f"Grades: {', '.join(f'{g}: {c}' for g, c in sorted(grade_counts.items()))}")
    if flags:
        print(f"Quality flags: {len(flags)}")
        for f in flags:
            print(f"  ! {f}")
    else:
        print("No quality flags — all clean!")

    conn.close()


if __name__ == "__main__":
    db = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    generate(db)
