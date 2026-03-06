#!/usr/bin/env python3
"""
Backfill ntee_code in 990data.db from IRS Business Master File (BMF) CSVs.
Also loads the full BMF into a 'bmf' table for reference.
"""

import csv
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
DB_PATH = str(PROJECT_DIR / "990data.db")
BMF_DIR = str(PROJECT_DIR / "bmf")
BMF_FILES = ["eo1.csv", "eo2.csv", "eo3.csv", "eo4.csv"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(str(Path(__file__).resolve().parent.parent / "extract.log"), mode="a"),
        logging.StreamHandler(sys.stdout),
    ],
)


def main():
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA cache_size=-131072")

    # Create BMF table
    logging.info("Creating bmf table...")
    con.executescript("""
        DROP TABLE IF EXISTS bmf;
        CREATE TABLE bmf (
            ein              TEXT PRIMARY KEY,
            name             TEXT,
            ico              TEXT,
            street           TEXT,
            city             TEXT,
            state            TEXT,
            zip              TEXT,
            grp              TEXT,
            subsection       TEXT,
            affiliation      TEXT,
            classification   TEXT,
            ruling           TEXT,
            deductibility    TEXT,
            foundation       TEXT,
            activity         TEXT,
            organization     TEXT,
            status           TEXT,
            tax_period       TEXT,
            asset_cd         TEXT,
            income_cd        TEXT,
            filing_req_cd    TEXT,
            pf_filing_req_cd TEXT,
            acct_pd          TEXT,
            asset_amt        INTEGER,
            income_amt       INTEGER,
            revenue_amt      INTEGER,
            ntee_cd          TEXT,
            sort_name        TEXT
        );
    """)

    # Load all BMF CSVs
    total_loaded = 0
    for fname in BMF_FILES:
        path = os.path.join(BMF_DIR, fname)
        logging.info(f"Loading {fname}...")
        count = 0
        batch = []

        with open(path, "r", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            for row in reader:
                ein = row.get("EIN", "").strip()
                if not ein:
                    continue

                def int_val(key):
                    v = row.get(key, "").strip()
                    if not v:
                        return None
                    try:
                        return int(v)
                    except ValueError:
                        try:
                            return int(float(v))
                        except ValueError:
                            return None

                batch.append((
                    ein,
                    row.get("NAME", "").strip() or None,
                    row.get("ICO", "").strip() or None,
                    row.get("STREET", "").strip() or None,
                    row.get("CITY", "").strip() or None,
                    row.get("STATE", "").strip() or None,
                    row.get("ZIP", "").strip() or None,
                    row.get("GROUP", "").strip() or None,
                    row.get("SUBSECTION", "").strip() or None,
                    row.get("AFFILIATION", "").strip() or None,
                    row.get("CLASSIFICATION", "").strip() or None,
                    row.get("RULING", "").strip() or None,
                    row.get("DEDUCTIBILITY", "").strip() or None,
                    row.get("FOUNDATION", "").strip() or None,
                    row.get("ACTIVITY", "").strip() or None,
                    row.get("ORGANIZATION", "").strip() or None,
                    row.get("STATUS", "").strip() or None,
                    row.get("TAX_PERIOD", "").strip() or None,
                    row.get("ASSET_CD", "").strip() or None,
                    row.get("INCOME_CD", "").strip() or None,
                    row.get("FILING_REQ_CD", "").strip() or None,
                    row.get("PF_FILING_REQ_CD", "").strip() or None,
                    row.get("ACCT_PD", "").strip() or None,
                    int_val("ASSET_AMT"),
                    int_val("INCOME_AMT"),
                    int_val("REVENUE_AMT"),
                    row.get("NTEE_CD", "").strip() or None,
                    row.get("SORT_NAME", "").strip() or None,
                ))
                count += 1

                if len(batch) >= 5000:
                    con.executemany(
                        "INSERT OR IGNORE INTO bmf VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        batch,
                    )
                    con.commit()
                    batch.clear()

        if batch:
            con.executemany(
                "INSERT OR IGNORE INTO bmf VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                batch,
            )
            con.commit()

        total_loaded += count
        logging.info(f"  {fname}: {count:,} rows")

    logging.info(f"Total BMF rows loaded: {total_loaded:,}")

    # Create index on bmf.ein (already PK but let's be explicit)
    logging.info("Creating BMF indexes...")
    con.execute("CREATE INDEX IF NOT EXISTS idx_bmf_ntee ON bmf(ntee_cd)")
    con.commit()

    # Backfill ntee_code in returns table
    logging.info("Backfilling ntee_code in returns table...")
    t0 = time.time()
    cur = con.execute("""
        UPDATE returns
        SET ntee_code = (SELECT bmf.ntee_cd FROM bmf WHERE bmf.ein = returns.ein)
        WHERE ntee_code IS NULL
    """)
    con.commit()
    updated = cur.rowcount
    elapsed = time.time() - t0
    logging.info(f"Updated {updated:,} rows with NTEE codes in {elapsed:.1f}s")

    # Stats
    logging.info("─── NTEE Backfill Summary ───")
    total = con.execute("SELECT COUNT(*) FROM returns").fetchone()[0]
    has_ntee = con.execute("SELECT COUNT(*) FROM returns WHERE ntee_code IS NOT NULL").fetchone()[0]
    logging.info(f"Returns with NTEE code: {has_ntee:,} / {total:,} ({100*has_ntee/total:.1f}%)")
    logging.info(f"Returns still missing NTEE: {total - has_ntee:,}")

    logging.info("Top 20 NTEE major groups:")
    for code, n in con.execute("""
        SELECT SUBSTR(ntee_code, 1, 1) as major, COUNT(*) as n
        FROM returns
        WHERE ntee_code IS NOT NULL
        GROUP BY major
        ORDER BY n DESC
        LIMIT 20
    """):
        labels = {
            'A': 'Arts/Culture', 'B': 'Education', 'C': 'Environment',
            'D': 'Animal-Related', 'E': 'Health', 'F': 'Mental Health',
            'G': 'Disease/Disorders', 'H': 'Medical Research',
            'I': 'Crime/Legal', 'J': 'Employment', 'K': 'Food/Agriculture',
            'L': 'Housing/Shelter', 'M': 'Public Safety',
            'N': 'Recreation/Sports', 'O': 'Youth Development',
            'P': 'Human Services', 'Q': 'International',
            'R': 'Civil Rights', 'S': 'Community Improvement',
            'T': 'Philanthropy/Voluntarism', 'U': 'Science/Technology',
            'V': 'Social Science', 'W': 'Public/Society Benefit',
            'X': 'Religion-Related', 'Y': 'Mutual/Membership',
            'Z': 'Unknown',
        }
        label = labels.get(code, '?')
        logging.info(f"  {code} ({label}): {n:,}")

    # Animal-related quick count
    animal = con.execute(
        "SELECT COUNT(DISTINCT ein), COUNT(*) FROM returns WHERE ntee_code LIKE 'D%'"
    ).fetchone()
    logging.info(f"Animal-related orgs (NTEE D%): {animal[0]:,} unique EINs, {animal[1]:,} filings")

    con.close()
    logging.info("Done.")


if __name__ == "__main__":
    main()
