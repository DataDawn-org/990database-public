#!/usr/bin/env python3
"""
Extract key financial fields from IRS Form 990 XML files into SQLite.

Processes XML files from ./{2019..2026}
into ./990data.db using multiprocessing.

Usage:
    python3 extract_990.py              # full run
    python3 extract_990.py --limit 100  # test with 100 files
"""

import logging
import multiprocessing as mp
import os
from pathlib import Path
import sqlite3
import sys
import time
from lxml import etree as ET

# XXE-hardened parser for IRS XML — disable external entities + network DTD lookup
# (per-worker module-level constant; lxml XMLParser is process-safe after fork).
_SAFE_PARSER = ET.XMLParser(resolve_entities=False, no_network=True)

# ── Configuration ──────────────────────────────────────────────────────────
BASE_DIR = str(Path(__file__).resolve().parent.parent)
DB_PATH = os.path.join(BASE_DIR, "990data.db")
LOG_PATH = os.path.join(BASE_DIR, "extract.log")

NS = "http://www.irs.gov/efile"
WORKER_CHUNK_SIZE = 500
BATCH_INSERT_SIZE = 2000
LOG_INTERVAL = 10_000

COLUMNS = [
    "object_id", "ein", "org_name", "state", "tax_year", "tax_period_end",
    "return_type", "ntee_code", "total_revenue", "total_expenses",
    "program_expenses", "fundraising_expenses", "management_expenses",
    "total_assets_eoy", "officer_comp", "source_file", "parse_error",
    # 2026-05-24 (4b fix, issue #7): revenue-detail + balance-sheet fields for
    # Form 990 / 990-EZ. Previously 100% NULL for these two form types — only
    # extract_990pf_detail.py populated them (for 990-PF). These columns are
    # ADD COLUMN'd by extract_990pf_detail.create_schema() on existing DBs;
    # we include them in CREATE TABLE + INSERT here so fresh builds carry them
    # and the 990/990-EZ extractors below can write them at insert time.
    # PF rows get NULL here at insert, then UPDATE'd by extract_990pf_detail.py.
    "contributions_received", "dividends", "interest_income",
    "net_gain_sale_assets", "contributions_paid", "fmv_assets_eoy",
    "net_assets_eoy",
    # §2 Deliverable A new fields (Phase-1 dev port):
    "total_functional_expenses", "return_version", "contractors_over_100k_cnt",
]

INSERT_SQL = f"""
    INSERT OR IGNORE INTO returns ({', '.join(COLUMNS)})
    VALUES ({', '.join('?' for _ in COLUMNS)})
"""


# ── Database ───────────────────────────────────────────────────────────────
def create_schema(con):
    con.executescript("""
        CREATE TABLE IF NOT EXISTS returns (
            object_id            TEXT PRIMARY KEY,
            ein                  TEXT,
            org_name             TEXT,
            state                TEXT,
            tax_year             INTEGER,
            tax_period_end       TEXT,
            return_type          TEXT,
            ntee_code            TEXT,
            total_revenue        INTEGER,
            total_expenses       INTEGER,
            program_expenses     INTEGER,
            fundraising_expenses INTEGER,
            management_expenses  INTEGER,
            total_assets_eoy     INTEGER,
            officer_comp         INTEGER,
            source_file          TEXT,
            parse_error          TEXT,
            -- Revenue-detail + balance-sheet fields (4b fix, issue #7, 2026-05-24).
            -- Populated for Form 990 / 990-EZ by extract_990() / extract_990ez();
            -- for 990-PF by extract_990pf_detail.py (which also ADD COLUMNs these
            -- + 5 more PF-only scalars on pre-existing DBs via try/except ALTER).
            contributions_received INTEGER,
            dividends              INTEGER,
            interest_income        INTEGER,
            net_gain_sale_assets   INTEGER,
            contributions_paid     INTEGER,
            fmv_assets_eoy         INTEGER,
            net_assets_eoy         INTEGER,
            -- §2 Deliverable A new fields (Phase-1 dev port). INTEGER affinity for the numerics so
            -- SQLite coerces on insert (the affinity assertion in parser_harness rails this); on
            -- EXISTING DBs the land applies the equivalent `ALTER TABLE returns ADD COLUMN ... INTEGER`.
            total_functional_expenses  INTEGER,
            return_version             TEXT,
            contractors_over_100k_cnt  INTEGER
        );
        -- idx_ein removed 2026-04-11: subset of idx_returns_ein_type and idx_returns_ein_year_oid
        CREATE INDEX IF NOT EXISTS idx_return_type ON returns(return_type);
        CREATE INDEX IF NOT EXISTS idx_tax_year    ON returns(tax_year);
    """)


# ── File Discovery ─────────────────────────────────────────────────────────
def discover_files(base_dir):
    paths = []
    # Scan all year directories (2019-2026+)
    for entry in sorted(os.listdir(base_dir)):
        if not entry.isdigit() or len(entry) != 4:
            continue
        year_dir = os.path.join(base_dir, entry)
        if not os.path.isdir(year_dir):
            continue
        # Walk the entire year directory tree to find all XML files
        # Handles: direct files, batch subdirs, and nested subdirs
        # (e.g., 2021/2021_TEOS_XML_01A/2021Redo_allCycles/*.xml)
        for dirpath, _dirnames, filenames in os.walk(year_dir):
            for fname in filenames:
                if fname.endswith(".xml"):
                    paths.append(os.path.join(dirpath, fname))
    return paths


def object_id_from_path(filepath):
    return os.path.basename(filepath).replace("_public.xml", "")


def load_processed_ids(db_path):
    if not os.path.exists(db_path):
        return set()
    con = sqlite3.connect(db_path)
    cur = con.execute("SELECT object_id FROM returns")
    ids = {row[0] for row in cur}
    con.close()
    return ids


# ── XML Parsing Helpers ────────────────────────────────────────────────────
def _tag(name):
    return f"{{{NS}}}{name}"


def find_text(el, dotted_path):
    """Walk a dot-separated path of element names under `el`, return text."""
    if el is None:
        return None
    node = el
    for tag in dotted_path.split("."):
        node = node.find(_tag(tag))
        if node is None:
            return None
    return node.text


def first_text(el, *dotted_paths):
    """Return text of the first dotted-path that resolves to a non-None text.

    Used for the 4b revenue/balance-sheet fields (issue #7), where the IRS
    e-file schema offers a true line-item element plus a Part I summary
    fallback. Tag names were confirmed stable across 2017-2026 by a
    schema-fingerprint pass (see decisions/4b memo), so the chains are short.
    """
    for path in dotted_paths:
        txt = find_text(el, path)
        if txt is not None:
            return txt
    return None


def int_or_none(val):
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        try:
            return int(float(val))
        except (ValueError, TypeError):
            return None


# ── Form-Specific Extractors ──────────────────────────────────────────────
def extract_990(root, row):
    irs = root.find(f".//{_tag('IRS990')}")
    if irs is None:
        return
    row["return_version"] = root.get("returnVersion")  # §2: MeF schema version (per-version grouping key)
    row["total_revenue"] = int_or_none(find_text(irs, "CYTotalRevenueAmt"))
    row["total_expenses"] = int_or_none(find_text(irs, "CYTotalExpensesAmt"))
    row["total_assets_eoy"] = int_or_none(find_text(irs, "TotalAssetsEOYAmt"))
    row["contractors_over_100k_cnt"] = int_or_none(find_text(irs, "CntrctRcvdGreaterThan100KCnt"))  # §2: Part VII Sec B count

    tfe = irs.find(_tag("TotalFunctionalExpensesGrp"))
    if tfe is not None:
        row["program_expenses"] = int_or_none(find_text(tfe, "ProgramServicesAmt"))
        row["fundraising_expenses"] = int_or_none(find_text(tfe, "FundraisingAmt"))
        row["management_expenses"] = int_or_none(find_text(tfe, "ManagementAndGeneralAmt"))
        row["total_functional_expenses"] = int_or_none(find_text(tfe, "TotalAmt"))  # §2: col-A (Part IX L25), off the SHARED tfe

    comp_grp = irs.find(_tag("CompCurrentOfcrDirectorsGrp"))
    if comp_grp is not None:
        row["officer_comp"] = int_or_none(find_text(comp_grp, "TotalAmt"))

    # ── Revenue-detail + balance-sheet (4b fix, issue #7) ────────────────────
    # Tag names confirmed across 2017-2026 by schema-fingerprint pass.
    #
    # contributions_received  Part VIII line 1h (TotalContributionsAmt);
    #                         Part I line 8 summary (CYContributionsGrantsAmt) fallback.
    row["contributions_received"] = int_or_none(first_text(
        irs, "TotalContributionsAmt", "CYContributionsGrantsAmt"))

    # dividends — NO separate line on the modern Form 990 e-file schema.
    # Part VIII line 3 (InvestmentIncomeGrp) reports dividends + interest
    # TOGETHER. Left NULL for Form 990; see 4b memo open question (i).
    # (Form 990-PF reports them separately, hence the column exists.)

    # interest_income  Part VIII line 3 recurring investment income
    #                  (dividends + interest combined): TotalRevenueColumnAmt
    #                  of InvestmentIncomeGrp. EXCLUDES capital gains (those are
    #                  line 7c, captured separately below) — so this does not
    #                  double-count with net_gain_sale_assets. We deliberately do
    #                  NOT use the Part I line 7 summary (CYInvestmentIncomeAmt),
    #                  which lumps in net gains.
    inv_grp = irs.find(_tag("InvestmentIncomeGrp"))
    if inv_grp is not None:
        row["interest_income"] = int_or_none(find_text(inv_grp, "TotalRevenueColumnAmt"))

    # net_gain_sale_assets  Part VIII line 7c (NetGainOrLossInvestmentsGrp /
    #                       TotalRevenueColumnAmt); GainOrLossGrp/SecuritiesAmt
    #                       fallback (securities-only subset).
    gain_grp = irs.find(_tag("NetGainOrLossInvestmentsGrp"))
    if gain_grp is not None:
        row["net_gain_sale_assets"] = int_or_none(
            find_text(gain_grp, "TotalRevenueColumnAmt"))
    if row["net_gain_sale_assets"] is None:
        row["net_gain_sale_assets"] = int_or_none(
            find_text(irs, "GainOrLossGrp.SecuritiesAmt"))

    # contributions_paid  Part I line 13 summary (CYGrantsAndSimilarPaidAmt).
    #                     Per-recipient Schedule I detail is parsed separately
    #                     into schedule_i_990 by extract_990_detail.py; this
    #                     scalar is the filing-level total grants paid.
    row["contributions_paid"] = int_or_none(find_text(irs, "CYGrantsAndSimilarPaidAmt"))

    # fmv_assets_eoy — NOT on the main IRS990 return (0/80 across all years).
    # FMV of investments lives on Schedule D; left NULL for Form 990. See 4b
    # memo open question (ii). (Form 990-PF reports FMVAssetsEOYAmt directly.)

    # net_assets_eoy  Part X line 33 col (B) (TotalNetAssetsFundBalanceGrp /
    #                 EOYAmt); Part I line 22 summary (NetAssetsOrFundBalancesEOYAmt)
    #                 fallback. Both present ~100% across years.
    nafb_grp = irs.find(_tag("TotalNetAssetsFundBalanceGrp"))
    if nafb_grp is not None:
        row["net_assets_eoy"] = int_or_none(find_text(nafb_grp, "EOYAmt"))
    if row["net_assets_eoy"] is None:
        row["net_assets_eoy"] = int_or_none(
            find_text(irs, "NetAssetsOrFundBalancesEOYAmt"))


def extract_990ez(root, row):
    irs = root.find(f".//{_tag('IRS990EZ')}")
    if irs is None:
        return
    row["total_revenue"] = int_or_none(find_text(irs, "TotalRevenueAmt"))
    row["total_expenses"] = int_or_none(find_text(irs, "TotalExpensesAmt"))
    row["program_expenses"] = int_or_none(find_text(irs, "TotalProgramServiceExpensesAmt"))

    assets_grp = irs.find(_tag("Form990TotalAssetsGrp"))
    if assets_grp is not None:
        row["total_assets_eoy"] = int_or_none(find_text(assets_grp, "EOYAmt"))

    # Sum all officer/director compensation entries
    comp_total = 0
    found_any = False
    for grp in irs.findall(_tag("OfficerDirectorTrusteeEmplGrp")):
        amt = find_text(grp, "CompensationAmt")
        if amt is not None:
            comp_total += int_or_none(amt) or 0
            found_any = True
    if found_any:
        row["officer_comp"] = comp_total

    # ── Revenue-detail + balance-sheet (4b fix, issue #7) ────────────────────
    # Tag names confirmed across 2017-2026 by schema-fingerprint pass.
    #
    # contributions_received  Part I line 1 (ContributionsGiftsGrantsEtcAmt).
    row["contributions_received"] = int_or_none(
        find_text(irs, "ContributionsGiftsGrantsEtcAmt"))

    # dividends — NO separate line on Form 990-EZ. Part I line 4
    # (InvestmentIncomeAmt) combines dividends + interest. Left NULL; see 4b
    # memo open question (i).

    # interest_income  Part I line 4 (InvestmentIncomeAmt) — combined
    #                  dividends + interest "investment income".
    row["interest_income"] = int_or_none(find_text(irs, "InvestmentIncomeAmt"))

    # net_gain_sale_assets  Part I line 5c net (GainOrLossFromSaleOfAssetsAmt).
    #                       NOTE: scope-doc guess "NetGainOrLossOnAssetsAmt"
    #                       was 0/80 across all years — wrong tag.
    row["net_gain_sale_assets"] = int_or_none(
        find_text(irs, "GainOrLossFromSaleOfAssetsAmt"))

    # contributions_paid  Part I line 10 (GrantsAndSimilarAmountsPaidAmt).
    #                     NOTE: scope-doc guess "GrantsAndSimilarAmtsPaidAmt"
    #                     (missing "ount") was 0/80 — wrong tag.
    row["contributions_paid"] = int_or_none(
        find_text(irs, "GrantsAndSimilarAmountsPaidAmt"))

    # fmv_assets_eoy — no FMV detail on Form 990-EZ; left NULL.

    # net_assets_eoy  Part II col (B) (NetAssetsOrFundBalancesGrp / EOYAmt,
    #                 present ~100%); Part II line 21 scalar
    #                 (NetAssetsOrFundBalancesEOYAmt) fallback.
    nafb_grp = irs.find(_tag("NetAssetsOrFundBalancesGrp"))
    if nafb_grp is not None:
        row["net_assets_eoy"] = int_or_none(find_text(nafb_grp, "EOYAmt"))
    if row["net_assets_eoy"] is None:
        row["net_assets_eoy"] = int_or_none(
            find_text(irs, "NetAssetsOrFundBalancesEOYAmt"))


def extract_990pf(root, row):
    irs = root.find(f".//{_tag('IRS990PF')}")
    if irs is None:
        return
    analysis = irs.find(_tag("AnalysisOfRevenueAndExpenses"))
    if analysis is not None:
        row["total_revenue"] = int_or_none(find_text(analysis, "TotalRevAndExpnssAmt"))
        row["total_expenses"] = int_or_none(find_text(analysis, "TotalExpensesRevAndExpnssAmt"))
        row["officer_comp"] = int_or_none(find_text(analysis, "CompOfcrDirTrstRevAndExpnssAmt"))

    bal = irs.find(_tag("Form990PFBalanceSheetsGrp"))
    if bal is not None:
        row["total_assets_eoy"] = int_or_none(find_text(bal, "TotalAssetsEOYAmt"))


def extract_990t(root, row):
    irs = root.find(f".//{_tag('IRS990T')}")
    if irs is None:
        return
    row["total_revenue"] = int_or_none(find_text(irs, "TotalUBTIComputedAmt"))
    row["total_expenses"] = int_or_none(find_text(irs, "TotalTaxAmt"))
    row["total_assets_eoy"] = int_or_none(find_text(irs, "BookValueAssetsEOYAmt"))


# ── Main File Parser ──────────────────────────────────────────────────────
EXTRACTORS = {
    "990": extract_990,
    "990EZ": extract_990ez,
    "990PF": extract_990pf,
    "990T": extract_990t,
}


def parse_file(filepath):
    oid = object_id_from_path(filepath)
    row = {col: None for col in COLUMNS}
    row["object_id"] = oid
    row["source_file"] = filepath

    try:
        tree = ET.parse(filepath, parser=_SAFE_PARSER)
        root = tree.getroot()

        return_type = find_text(root, "ReturnHeader.ReturnTypeCd")
        row["return_type"] = return_type
        row["ein"] = find_text(root, "ReturnHeader.Filer.EIN")
        row["org_name"] = find_text(root, "ReturnHeader.Filer.BusinessName.BusinessNameLine1Txt")
        row["tax_year"] = int_or_none(find_text(root, "ReturnHeader.TaxYr"))
        row["tax_period_end"] = find_text(root, "ReturnHeader.TaxPeriodEndDt")

        # State from USAddress, NULL for foreign orgs
        filer = root.find(f".//{_tag('ReturnHeader')}/{_tag('Filer')}")
        if filer is not None:
            us_addr = filer.find(_tag("USAddress"))
            if us_addr is not None:
                row["state"] = find_text(us_addr, "StateAbbreviationCd")

        extractor = EXTRACTORS.get(return_type)
        if extractor:
            extractor(root, row)

    except Exception as e:
        row["parse_error"] = f"{type(e).__name__}: {e}"

    return row


def _check_namespace_or_bail(sample_files):
    """Pre-flight check: ensure IRS XML root namespace still matches the NS
    constant our extractors hardcode. If IRS bumps the schema namespace,
    every `find(_tag(...))` call returns None and we silently insert
    all-NULL rows — same failure shape as the 2026-05-10 DAF incident.
    Probing the first few files in the to-process set catches this loud
    BEFORE we run 5M files through workers producing nothing useful.
    Audit H3, 2026-05-15. Cost: ~3 file parses (~1 ms each).
    """
    if not sample_files:
        return
    probes = sample_files[:3]
    mismatches = []
    parse_failures = 0
    for fp in probes:
        try:
            tree = ET.parse(fp, parser=_SAFE_PARSER)
            root_tag = tree.getroot().tag
            ns = root_tag.split('}')[0].lstrip('{') if '}' in root_tag else ''
            if ns != NS:
                mismatches.append((fp, ns))
        except Exception as e:
            parse_failures += 1
            logging.warning(f"namespace probe of {fp} failed: {e}")
    if mismatches and len(mismatches) + parse_failures == len(probes):
        sys.stderr.write(
            f"NAMESPACE MISMATCH: all {len(probes)} probe file(s) have unexpected "
            f"root namespace. Expected {NS!r}.\n"
        )
        for fp, ns in mismatches:
            sys.stderr.write(f"  {fp}: {ns!r}\n")
        sys.stderr.write(
            "IRS likely bumped the XML schema. Extract scripts must be updated "
            "before re-running — otherwise all extracted rows will be all-NULL.\n"
        )
        sys.exit(2)


def process_chunk(filepaths):
    results = []
    for fp in filepaths:
        row = parse_file(fp)
        if row is not None:
            results.append(row)
    return results


# ── Writer Process ────────────────────────────────────────────────────────
def writer_process(db_path, result_queue, total_files):
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA cache_size=-131072")
    con.execute("PRAGMA temp_store=MEMORY")
    create_schema(con)
    con.commit()

    buffer = []
    processed = 0
    skipped = 0
    errors = 0
    last_log = 0
    t0 = time.time()

    while True:
        try:
            item = result_queue.get(timeout=120)
        except Exception:
            continue

        if item is None:  # poison pill
            break

        if isinstance(item, list):
            buffer.extend(item)
            processed += len(item)
        else:
            buffer.append(item)
            processed += 1

        if len(buffer) >= BATCH_INSERT_SIZE:
            _flush(con, buffer)
            errors += sum(1 for r in buffer if r.get("parse_error"))
            buffer.clear()

        if processed - last_log >= LOG_INTERVAL:
            elapsed = time.time() - t0
            rate = processed / elapsed if elapsed > 0 else 0
            logging.info(
                f"Progress: {processed:,}/{total_files:,} "
                f"({100*processed/total_files:.1f}%) | "
                f"{rate:.0f} files/sec | "
                f"errors: {errors:,}"
            )
            last_log = processed

    # Final flush
    if buffer:
        errors += sum(1 for r in buffer if r.get("parse_error"))
        _flush(con, buffer)

    elapsed = time.time() - t0
    con.close()
    logging.info(
        f"Writer done. {processed:,} rows in {elapsed:.1f}s "
        f"({processed/elapsed:.0f}/sec), {errors:,} parse errors"
    )


def _flush(con, buffer):
    rows = [tuple(r[col] for col in COLUMNS) for r in buffer]
    con.executemany(INSERT_SQL, rows)
    con.commit()


# ── Main ──────────────────────────────────────────────────────────────────
def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(LOG_PATH),
            logging.StreamHandler(sys.stdout),
        ],
    )

    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])
        logging.info(f"TEST MODE: limiting to {limit} files")

    n_workers = max(1, mp.cpu_count() - 2)

    logging.info("Discovering files...")
    all_files = discover_files(BASE_DIR)
    logging.info(f"Found {len(all_files):,} XML files")

    logging.info("Loading already-processed IDs...")
    processed_ids = load_processed_ids(DB_PATH)
    todo = [f for f in all_files if object_id_from_path(f) not in processed_ids]
    logging.info(f"To process: {len(todo):,} ({len(processed_ids):,} already done)")

    if not todo:
        logging.info("Nothing to do.")
        return

    if limit:
        todo = todo[:limit]
        logging.info(f"Limited to {len(todo):,} files")

    # Pre-flight namespace check — abort loud if IRS schema bumped
    _check_namespace_or_bail(todo)

    # Build chunks
    chunks = [todo[i:i + WORKER_CHUNK_SIZE]
              for i in range(0, len(todo), WORKER_CHUNK_SIZE)]

    result_queue = mp.Queue(maxsize=50_000)

    # Start writer
    writer = mp.Process(target=writer_process, args=(DB_PATH, result_queue, len(todo)))
    writer.start()

    logging.info(f"Starting {n_workers} workers across {len(chunks):,} chunks...")
    t0 = time.time()

    with mp.Pool(n_workers) as pool:
        for chunk_rows in pool.imap_unordered(process_chunk, chunks, chunksize=1):
            result_queue.put(chunk_rows)

    result_queue.put(None)  # poison pill
    writer.join()

    elapsed = time.time() - t0
    logging.info(f"Total elapsed: {elapsed:.1f}s ({elapsed/60:.1f} min)")

    # Print summary stats
    _print_summary(DB_PATH)


def _print_summary(db_path):
    con = sqlite3.connect(db_path)
    logging.info("─── Summary ───")

    total = con.execute("SELECT COUNT(*) FROM returns").fetchone()[0]
    logging.info(f"Total rows: {total:,}")

    logging.info("By return type:")
    for rt, n in con.execute(
        "SELECT return_type, COUNT(*) FROM returns GROUP BY return_type ORDER BY COUNT(*) DESC"
    ):
        logging.info(f"  {rt}: {n:,}")

    logging.info("By tax year:")
    for yr, n in con.execute(
        "SELECT tax_year, COUNT(*) FROM returns GROUP BY tax_year ORDER BY tax_year"
    ):
        logging.info(f"  {yr}: {n:,}")

    errs = con.execute("SELECT COUNT(*) FROM returns WHERE parse_error IS NOT NULL").fetchone()[0]
    logging.info(f"Parse errors: {errs:,}")

    con.close()


if __name__ == "__main__":
    main()
