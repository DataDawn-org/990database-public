#!/usr/bin/env python3
"""
Deep extraction of 990-PF data: grants, officers, contributors, contractors,
investments, capital gains, program activities, and program investments.
Also backfills scalar financial fields on the returns table.

Usage:
    python3 extract_990pf_detail.py              # full run
    python3 extract_990pf_detail.py --limit 100  # test with 100 files
"""

import logging
from pathlib import Path
import multiprocessing as mp
import os
import sqlite3
import sys
import time
from lxml import etree as ET

# ── Configuration ──────────────────────────────────────────────────────────
BASE_DIR = str(Path(__file__).resolve().parent.parent)
DB_PATH = os.path.join(BASE_DIR, "990data.db")
LOG_PATH = os.path.join(BASE_DIR, "extract.log")

NS = "http://www.irs.gov/efile"
WORKER_CHUNK_SIZE = 200
BATCH_INSERT_SIZE = 500
LOG_INTERVAL = 5_000


# ── Helpers ────────────────────────────────────────────────────────────────
def _tag(name):
    return f"{{{NS}}}{name}"


def find_text(el, dotted_path):
    if el is None:
        return None
    node = el
    for tag in dotted_path.split("."):
        node = node.find(_tag(tag))
        if node is None:
            return None
    return node.text


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


def float_or_none(val):
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def object_id_from_path(filepath):
    return os.path.basename(filepath).replace("_public.xml", "")


# ── Schema ─────────────────────────────────────────────────────────────────
SCHEMA_SQL = """
-- New scalar columns on returns (for 990-PF backfill)
-- Using ALTER TABLE with IF NOT EXISTS workaround via try/except in Python

-- Grants table (paid + future + expenditure responsibility)
CREATE TABLE IF NOT EXISTS grants (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id             TEXT NOT NULL,
    ein                   TEXT,
    grant_type            TEXT,  -- 'paid', 'future', 'exp_responsibility'
    recipient_name        TEXT,
    recipient_city        TEXT,
    recipient_state       TEXT,
    recipient_country     TEXT,  -- NULL for US
    recipient_zip         TEXT,
    relationship          TEXT,
    foundation_status     TEXT,
    purpose               TEXT,
    amount                INTEGER,
    grant_date            TEXT,       -- exp_responsibility only
    expended_amount       INTEGER     -- exp_responsibility only
);
CREATE INDEX IF NOT EXISTS idx_grants_oid ON grants(object_id);
CREATE INDEX IF NOT EXISTS idx_grants_ein ON grants(ein);
CREATE INDEX IF NOT EXISTS idx_grants_type ON grants(grant_type);

-- Officers / Directors / Trustees
CREATE TABLE IF NOT EXISTS officers (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id             TEXT NOT NULL,
    ein                   TEXT,
    person_name           TEXT,
    title                 TEXT,
    avg_hours_per_week    REAL,
    compensation          INTEGER,
    benefits              INTEGER,
    expense_account       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_officers_oid ON officers(object_id);
CREATE INDEX IF NOT EXISTS idx_officers_ein ON officers(ein);

-- Substantial Contributors (Schedule B)
CREATE TABLE IF NOT EXISTS contributors (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id             TEXT NOT NULL,
    ein                   TEXT,
    contributor_name      TEXT,
    city                  TEXT,
    state                 TEXT,
    zip                   TEXT,
    amount                INTEGER,
    contributor_type      TEXT  -- 'person' or 'business'
);
CREATE INDEX IF NOT EXISTS idx_contributors_oid ON contributors(object_id);
CREATE INDEX IF NOT EXISTS idx_contributors_ein ON contributors(ein);

-- Top Contractors
CREATE TABLE IF NOT EXISTS contractors (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id             TEXT NOT NULL,
    ein                   TEXT,
    contractor_name       TEXT,
    city                  TEXT,
    state                 TEXT,
    service_type          TEXT,
    compensation          INTEGER
);
CREATE INDEX IF NOT EXISTS idx_contractors_oid ON contractors(object_id);
CREATE INDEX IF NOT EXISTS idx_contractors_ein ON contractors(ein);

-- Top Employees (non-officer, highest paid)
CREATE TABLE IF NOT EXISTS top_employees (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id             TEXT NOT NULL,
    ein                   TEXT,
    person_name           TEXT,
    title                 TEXT,
    avg_hours_per_week    REAL,
    compensation          INTEGER,
    benefits              INTEGER,
    expense_account       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_topempl_oid ON top_employees(object_id);
CREATE INDEX IF NOT EXISTS idx_topempl_ein ON top_employees(ein);

-- Investment Holdings
CREATE TABLE IF NOT EXISTS investments (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id             TEXT NOT NULL,
    ein                   TEXT,
    investment_type       TEXT,  -- 'corp_bond', 'other', 'govt', 'land'
    description           TEXT,
    book_value            INTEGER,
    fmv                   INTEGER,
    cost_basis            INTEGER
);
CREATE INDEX IF NOT EXISTS idx_investments_oid ON investments(object_id);
CREATE INDEX IF NOT EXISTS idx_investments_ein ON investments(ein);

-- Capital Gains/Losses Detail
CREATE TABLE IF NOT EXISTS capital_gains (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id             TEXT NOT NULL,
    ein                   TEXT,
    property_desc         TEXT,
    how_acquired          TEXT,
    acquired_date         TEXT,
    sold_date             TEXT,
    gross_sale_price      INTEGER,
    cost_basis            INTEGER,
    gain_or_loss          INTEGER
);
CREATE INDEX IF NOT EXISTS idx_capgains_oid ON capital_gains(object_id);
CREATE INDEX IF NOT EXISTS idx_capgains_ein ON capital_gains(ein);

-- Program Service Accomplishments (up to 3 per filing)
CREATE TABLE IF NOT EXISTS program_activities (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id             TEXT NOT NULL,
    ein                   TEXT,
    activity_num          INTEGER,
    description           TEXT,
    expenses              INTEGER
);
CREATE INDEX IF NOT EXISTS idx_progact_oid ON program_activities(object_id);
CREATE INDEX IF NOT EXISTS idx_progact_ein ON program_activities(ein);

-- Program-Related Investments
CREATE TABLE IF NOT EXISTS program_investments (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id             TEXT NOT NULL,
    ein                   TEXT,
    description           TEXT,
    amount                INTEGER
);
CREATE INDEX IF NOT EXISTS idx_proginv_oid ON program_investments(object_id);
CREATE INDEX IF NOT EXISTS idx_proginv_ein ON program_investments(ein);
"""

SCALAR_COLUMNS = [
    ("contributions_received", "INTEGER"),
    ("dividends", "INTEGER"),
    ("interest_income", "INTEGER"),
    ("net_gain_sale_assets", "INTEGER"),
    ("contributions_paid", "INTEGER"),
    ("fmv_assets_eoy", "INTEGER"),
    ("net_assets_eoy", "INTEGER"),
    ("grants_payable_eoy", "INTEGER"),
    ("qualifying_distributions", "INTEGER"),
    ("distributable_amount", "INTEGER"),
    ("min_investment_return", "INTEGER"),
    ("excess_distribution_cyov", "INTEGER"),
]


def create_schema(con):
    con.executescript(SCHEMA_SQL)
    for col_name, col_type in SCALAR_COLUMNS:
        try:
            con.execute(f"ALTER TABLE returns ADD COLUMN {col_name} {col_type}")
        except sqlite3.OperationalError:
            pass  # column already exists
    con.commit()


# ── File Discovery ─────────────────────────────────────────────────────────
def discover_pf_files(base_dir, db_path):
    """Find all 990-PF object_ids from DB, then locate their source files.
    Skips object_ids already extracted into the grants table."""
    con = sqlite3.connect(db_path)
    rows = con.execute(
        "SELECT object_id, source_file FROM returns WHERE return_type = '990PF'"
    ).fetchall()
    # Get already-processed object_ids from grants table
    already_done = {row[0] for row in con.execute(
        "SELECT DISTINCT object_id FROM grants"
    )}
    con.close()
    # Filter to files that exist and haven't been processed yet
    files = [(oid, sf) for oid, sf in rows
             if os.path.exists(sf) and oid not in already_done]
    return files


# ── Address Extraction ─────────────────────────────────────────────────────
def extract_us_address(el):
    """Extract city, state, zip from a USAddress element."""
    if el is None:
        return None, None, None, None
    addr = el.find(_tag("USAddress"))
    if addr is None:
        return None, None, None, None
    city = find_text(addr, "CityNm")
    state = find_text(addr, "StateAbbreviationCd")
    zipcode = find_text(addr, "ZIPCd")
    return city, state, None, zipcode  # country=None for US


def extract_foreign_address(el):
    """Extract city, state/province, country from a ForeignAddress element."""
    if el is None:
        return None, None, None, None
    addr = el.find(_tag("ForeignAddress"))
    if addr is None:
        return None, None, None, None
    city = find_text(addr, "CityNm")
    province = find_text(addr, "ProvinceOrStateNm")
    country = find_text(addr, "CountryCd")
    zipcode = find_text(addr, "ForeignPostalCd")
    return city, province, country, zipcode


def extract_address(el, us_tag="RecipientUSAddress", foreign_tag="RecipientForeignAddress"):
    """Try US address first, then foreign. Returns (city, state, country, zip)."""
    if el is None:
        return None, None, None, None
    us = el.find(_tag(us_tag))
    if us is not None:
        city = find_text(us, "CityNm")
        state = find_text(us, "StateAbbreviationCd")
        zipcode = find_text(us, "ZIPCd")
        return city, state, None, zipcode
    foreign = el.find(_tag(foreign_tag))
    if foreign is not None:
        city = find_text(foreign, "CityNm")
        province = find_text(foreign, "ProvinceOrStateNm")
        country = find_text(foreign, "CountryCd")
        zipcode = find_text(foreign, "ForeignPostalCd")
        return city, province, country, zipcode
    return None, None, None, None


def get_name(el, biz_tag="RecipientBusinessName", person_tag="RecipientPersonNm"):
    """Get business name or person name from an element."""
    if el is None:
        return None
    biz = el.find(_tag(biz_tag))
    if biz is not None:
        line1 = find_text(biz, "BusinessNameLine1Txt")
        line2 = find_text(biz, "BusinessNameLine2Txt")
        if line1 and line2:
            return f"{line1} {line2}"
        return line1
    person = el.find(_tag(person_tag))
    if person is not None:
        return person.text
    return None


# ── Per-File Extraction ───────────────────────────────────────────────────
def parse_pf_file(oid, filepath):
    result = {
        "object_id": oid,
        "scalars": {},
        "grants": [],
        "officers": [],
        "contributors": [],
        "contractors": [],
        "top_employees": [],
        "investments": [],
        "capital_gains": [],
        "program_activities": [],
        "program_investments": [],
        "error": None,
    }

    try:
        tree = ET.parse(filepath)
        root = tree.getroot()
        ein_val = find_text(root, "ReturnHeader.Filer.EIN")
        result["ein"] = ein_val

        irs = root.find(f".//{_tag('IRS990PF')}")
        if irs is None:
            result["error"] = "No IRS990PF element found"
            return result

        # ── Scalar fields ──────────────────────────────────────────────
        analysis = irs.find(_tag("AnalysisOfRevenueAndExpenses"))
        if analysis is not None:
            result["scalars"]["contributions_received"] = int_or_none(
                find_text(analysis, "ContriRcvdRevAndExpnssAmt"))
            result["scalars"]["dividends"] = int_or_none(
                find_text(analysis, "DividendsRevAndExpnssAmt"))
            result["scalars"]["interest_income"] = int_or_none(
                find_text(analysis, "InterestOnSavRevAndExpnssAmt"))
            result["scalars"]["net_gain_sale_assets"] = int_or_none(
                find_text(analysis, "NetGainSaleAstRevAndExpnssAmt"))
            result["scalars"]["contributions_paid"] = int_or_none(
                find_text(analysis, "ContriPaidRevAndExpnssAmt"))

        result["scalars"]["fmv_assets_eoy"] = int_or_none(
            find_text(irs, "FMVAssetsEOYAmt"))

        bal = irs.find(_tag("Form990PFBalanceSheetsGrp"))
        if bal is not None:
            result["scalars"]["net_assets_eoy"] = int_or_none(
                find_text(bal, "TotNetAstOrFundBalancesEOYAmt"))
            result["scalars"]["grants_payable_eoy"] = int_or_none(
                find_text(bal, "GrantsPayableEOYAmt"))

        qual = irs.find(_tag("PFQualifyingDistributionsGrp"))
        if qual is not None:
            result["scalars"]["qualifying_distributions"] = int_or_none(
                find_text(qual, "QualifyingDistributionsAmt"))

        dist = irs.find(_tag("DistributableAmountGrp"))
        if dist is not None:
            result["scalars"]["distributable_amount"] = int_or_none(
                find_text(dist, "DistributableAsAdjustedAmt"))

        mir = irs.find(_tag("MinimumInvestmentReturnGrp"))
        if mir is not None:
            result["scalars"]["min_investment_return"] = int_or_none(
                find_text(mir, "MinimumInvestmentReturnAmt"))

        undist = irs.find(_tag("UndistributedIncomeGrp"))
        if undist is not None:
            result["scalars"]["excess_distribution_cyov"] = int_or_none(
                find_text(undist, "TotalExcessDistributionCyovAmt"))

        # ── Grants paid ────────────────────────────────────────────────
        supp = irs.find(_tag("SupplementaryInformationGrp"))
        if supp is not None:
            for g in supp.findall(_tag("GrantOrContributionPdDurYrGrp")):
                name = get_name(g)
                city, state, country, zipcode = extract_address(g)
                result["grants"].append((
                    oid, ein_val, "paid", name, city, state, country, zipcode,
                    find_text(g, "RecipientRelationshipTxt"),
                    find_text(g, "RecipientFoundationStatusTxt"),
                    find_text(g, "GrantOrContributionPurposeTxt"),
                    int_or_none(find_text(g, "Amt")),
                    None, None,  # grant_date, expended_amount
                ))

            # Grants approved for future
            for g in supp.findall(_tag("GrantOrContriApprvForFutGrp")):
                name = get_name(g)
                city, state, country, zipcode = extract_address(g)
                result["grants"].append((
                    oid, ein_val, "future", name, city, state, country, zipcode,
                    find_text(g, "RecipientRelationshipTxt"),
                    find_text(g, "RecipientFoundationStatusTxt"),
                    find_text(g, "GrantOrContributionPurposeTxt"),
                    int_or_none(find_text(g, "Amt")),
                    None, None,
                ))

        # Expenditure responsibility grants (separate top-level element)
        for stmt in root.findall(f".//{_tag('ExpenditureResponsibilityGrp')}"):
            name = get_name(stmt, "BusinessName", "PersonNm")
            city, state, country, zipcode = extract_address(
                stmt, "USAddress", "ForeignAddress")
            result["grants"].append((
                oid, ein_val, "exp_responsibility", name, city, state, country, zipcode,
                None,  # relationship
                None,  # foundation status
                find_text(stmt, "PurposeOfGrantTxt"),
                int_or_none(find_text(stmt, "GrantAmt")),
                find_text(stmt, "GrantDt"),
                int_or_none(find_text(stmt, "ExpendedByGranteeAmt")),
            ))

        # ── Officers / Directors / Trustees ────────────────────────────
        oinfo = irs.find(_tag("OfficerDirTrstKeyEmplInfoGrp"))
        if oinfo is not None:
            for o in oinfo.findall(_tag("OfficerDirTrstKeyEmplGrp")):
                result["officers"].append((
                    oid, ein_val,
                    find_text(o, "PersonNm"),
                    find_text(o, "TitleTxt"),
                    float_or_none(find_text(o, "AverageHrsPerWkDevotedToPosRt")),
                    int_or_none(find_text(o, "CompensationAmt")),
                    int_or_none(find_text(o, "EmployeeBenefitProgramAmt")),
                    int_or_none(find_text(o, "ExpenseAccountOtherAllwncAmt")),
                ))

            # Top 5 highest paid employees (non-officer)
            for e in oinfo.findall(_tag("CompensationHighestPaidEmplGrp")):
                result["top_employees"].append((
                    oid, ein_val,
                    find_text(e, "PersonNm"),
                    find_text(e, "TitleTxt"),
                    float_or_none(find_text(e, "AverageHrsPerWkDevotedToPosRt")),
                    int_or_none(find_text(e, "CompensationAmt")),
                    int_or_none(find_text(e, "EmployeeBenefitsAmt")),
                    int_or_none(find_text(e, "ExpenseAccountAmt")),
                ))

            # Top contractors
            for c in oinfo.findall(_tag("CompensationOfHghstPdCntrctGrp")):
                cname = get_name(c, "BusinessName", "PersonNm")
                city, state, country, zipcode = extract_address(
                    c, "USAddress", "ForeignAddress")
                result["contractors"].append((
                    oid, ein_val, cname, city, state,
                    find_text(c, "ServiceTypeTxt"),
                    int_or_none(find_text(c, "CompensationAmt")),
                ))

        # ── Substantial Contributors (Schedule B) ─────────────────────
        for schb in root.findall(f".//{_tag('IRS990ScheduleB')}"):
            for cg in schb.findall(_tag("ContributorInformationGrp")):
                # Name: business or person
                cname = None
                ctype = None
                biz = cg.find(_tag("ContributorBusinessName"))
                if biz is not None:
                    cname = find_text(biz, "BusinessNameLine1Txt")
                    ctype = "business"
                else:
                    pn = cg.find(_tag("ContributorPersonNm"))
                    if pn is not None:
                        cname = pn.text
                        ctype = "person"

                city, state, country, zipcode = extract_address(
                    cg, "ContributorUSAddress", "ContributorForeignAddress")
                result["contributors"].append((
                    oid, ein_val, cname, city, state, zipcode,
                    int_or_none(find_text(cg, "TotalContributionsAmt")),
                    ctype,
                ))

        # ── Investments ────────────────────────────────────────────────
        # Corporate bonds
        for sched in root.findall(f".//{_tag('InvestmentsCorpBondsSchedule')}"):
            for inv in sched.findall(_tag("InvestmentsCorporateBondsGrp")):
                result["investments"].append((
                    oid, ein_val, "corp_bond",
                    find_text(inv, "BondNm"),
                    int_or_none(find_text(inv, "EOYBookValueAmt")),
                    int_or_none(find_text(inv, "EOYFMVAmt")),
                    None,  # cost_basis
                ))

        # Other investments
        for sched in root.findall(f".//{_tag('InvestmentsOtherSchedule2')}"):
            for inv in sched.findall(_tag("InvestmentsOtherGrp")):
                result["investments"].append((
                    oid, ein_val, "other",
                    find_text(inv, "CategoryOrItemTxt"),
                    int_or_none(find_text(inv, "BookValueAmt")),
                    int_or_none(find_text(inv, "EOYFMVAmt")),
                    None,
                ))

        # Government obligations
        for sched in root.findall(f".//{_tag('InvestmentsGovtObligationsSch')}"):
            us_bv = int_or_none(find_text(sched, "USGovtObligationsBookVlEOYAmt"))
            us_fmv = int_or_none(find_text(sched, "USGovtObligationsEOYFMVAmt"))
            if us_bv or us_fmv:
                result["investments"].append((
                    oid, ein_val, "us_govt", "US Government Obligations",
                    us_bv, us_fmv, None,
                ))
            sl_bv = int_or_none(find_text(sched, "StateLocalSecBookVlEOYAmt"))
            sl_fmv = int_or_none(find_text(sched, "StateLocalSecEOYFMVAmt"))
            if sl_bv or sl_fmv:
                result["investments"].append((
                    oid, ein_val, "state_local", "State/Local Securities",
                    sl_bv, sl_fmv, None,
                ))

        # Land/building investments
        for sched in root.findall(f".//{_tag('InvestmentsLandSchedule2')}"):
            for inv in sched.findall(_tag("InvestmentLandGrp")):
                result["investments"].append((
                    oid, ein_val, "land_invest",
                    find_text(inv, "CategoryOrItemTxt"),
                    int_or_none(find_text(inv, "BookValueAmt")),
                    int_or_none(find_text(inv, "EOYFMVAmt")),
                    int_or_none(find_text(inv, "CostOrOtherBasisAmt")),
                ))

        # Land/bldg/equipment (charitable use)
        for sched in root.findall(f".//{_tag('LandEtcSchedule2')}"):
            for inv in sched.findall(_tag("LandEtcGrp")):
                result["investments"].append((
                    oid, ein_val, "land_equip_charitable",
                    find_text(inv, "CategoryOrItemTxt"),
                    int_or_none(find_text(inv, "BookValueAmt")),
                    int_or_none(find_text(inv, "EOYFMVAmt")),
                    int_or_none(find_text(inv, "CostOrOtherBasisAmt")),
                ))

        # ── Capital Gains ──────────────────────────────────────────────
        for detail in root.findall(f".//{_tag('CapGainsLossTxInvstIncmDetail')}"):
            for cg in detail.findall(_tag("CapGainsLossTxInvstIncmGrp")):
                result["capital_gains"].append((
                    oid, ein_val,
                    find_text(cg, "PropertyDesc"),
                    find_text(cg, "HowAcquiredCd"),
                    find_text(cg, "AcquiredDt"),
                    find_text(cg, "SoldDt"),
                    int_or_none(find_text(cg, "GrossSalesPriceAmt")),
                    int_or_none(find_text(cg, "CostOrOtherBasisAmt")),
                    int_or_none(find_text(cg, "GainOrLossAmt")),
                ))

        # ── Program Activities ─────────────────────────────────────────
        prog = irs.find(_tag("SummaryOfDirectChrtblActyGrp"))
        if prog is not None:
            for i in range(1, 5):
                desc = find_text(prog, f"Description{i}Txt")
                exp = int_or_none(find_text(prog, f"Expenses{i}Amt"))
                if desc or exp:
                    result["program_activities"].append((
                        oid, ein_val, i, desc, exp,
                    ))

        # ── Program-Related Investments ────────────────────────────────
        pri_summary = irs.find(_tag("SumOfProgramRelatedInvstGrp"))
        if pri_summary is not None:
            for i in range(1, 4):
                desc = find_text(pri_summary, f"Description{i}Txt")
                amt = int_or_none(find_text(pri_summary, f"Expenses{i}Amt"))
                if desc or amt:
                    result["program_investments"].append((
                        oid, ein_val, desc, amt,
                    ))

        # Detailed PRI schedule
        for sched in root.findall(f".//{_tag('AllOthProgRltdInvestmentsSch')}"):
            for pri in sched.findall(_tag("AllOtherProgramRelatedInvstGrp")):
                result["program_investments"].append((
                    oid, ein_val,
                    find_text(pri, "Desc"),
                    int_or_none(find_text(pri, "Amt")),
                ))

    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"

    return result


def process_chunk(file_pairs):
    results = []
    for oid, filepath in file_pairs:
        results.append(parse_pf_file(oid, filepath))
    return results


# ── Writer Process ────────────────────────────────────────────────────────
INSERT_GRANTS = """INSERT INTO grants
    (object_id, ein, grant_type, recipient_name, recipient_city, recipient_state,
     recipient_country, recipient_zip, relationship, foundation_status, purpose,
     amount, grant_date, expended_amount)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""

INSERT_OFFICERS = """INSERT INTO officers
    (object_id, ein, person_name, title, avg_hours_per_week,
     compensation, benefits, expense_account)
    VALUES (?,?,?,?,?,?,?,?)"""

INSERT_CONTRIBUTORS = """INSERT INTO contributors
    (object_id, ein, contributor_name, city, state, zip, amount, contributor_type)
    VALUES (?,?,?,?,?,?,?,?)"""

INSERT_CONTRACTORS = """INSERT INTO contractors
    (object_id, ein, contractor_name, city, state, service_type, compensation)
    VALUES (?,?,?,?,?,?,?)"""

INSERT_TOP_EMPLOYEES = """INSERT INTO top_employees
    (object_id, ein, person_name, title, avg_hours_per_week,
     compensation, benefits, expense_account)
    VALUES (?,?,?,?,?,?,?,?)"""

INSERT_INVESTMENTS = """INSERT INTO investments
    (object_id, ein, investment_type, description, book_value, fmv, cost_basis)
    VALUES (?,?,?,?,?,?,?)"""

INSERT_CAPGAINS = """INSERT INTO capital_gains
    (object_id, ein, property_desc, how_acquired, acquired_date, sold_date,
     gross_sale_price, cost_basis, gain_or_loss)
    VALUES (?,?,?,?,?,?,?,?,?)"""

INSERT_PROG_ACT = """INSERT INTO program_activities
    (object_id, ein, activity_num, description, expenses)
    VALUES (?,?,?,?,?)"""

INSERT_PROG_INV = """INSERT INTO program_investments
    (object_id, ein, description, amount)
    VALUES (?,?,?,?)"""


def writer_process(db_path, result_queue, total_files):
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA cache_size=-262144")  # 256 MB
    con.execute("PRAGMA temp_store=MEMORY")
    create_schema(con)
    con.commit()

    # Buffers for each table
    bufs = {
        "grants": [], "officers": [], "contributors": [],
        "contractors": [], "top_employees": [], "investments": [],
        "capital_gains": [], "program_activities": [], "program_investments": [],
        "scalar_updates": [],
    }
    counts = {k: 0 for k in bufs}
    processed = 0
    errors = 0
    last_log = 0
    t0 = time.time()

    while True:
        try:
            item = result_queue.get(timeout=120)
        except Exception:
            continue

        if item is None:
            break

        if isinstance(item, list):
            batch = item
        else:
            batch = [item]

        for res in batch:
            processed += 1
            if res.get("error"):
                errors += 1

            oid = res["object_id"]
            ein = res.get("ein")

            # Scalar updates
            if res["scalars"]:
                bufs["scalar_updates"].append((oid, res["scalars"]))

            for table in ["grants", "officers", "contributors", "contractors",
                          "top_employees", "investments", "capital_gains",
                          "program_activities", "program_investments"]:
                bufs[table].extend(res[table])

        # Flush when buffer gets large
        total_buffered = sum(len(v) for v in bufs.values())
        if total_buffered >= BATCH_INSERT_SIZE * 10:
            _flush_all(con, bufs, counts)

        if processed - last_log >= LOG_INTERVAL:
            elapsed = time.time() - t0
            rate = processed / elapsed if elapsed > 0 else 0
            logging.info(
                f"Progress: {processed:,}/{total_files:,} "
                f"({100*processed/total_files:.1f}%) | "
                f"{rate:.0f} files/sec | "
                f"grants: {counts['grants']:,} | "
                f"errors: {errors:,}"
            )
            last_log = processed

    # Final flush
    _flush_all(con, bufs, counts)

    elapsed = time.time() - t0
    con.close()
    logging.info(
        f"Writer done. {processed:,} files in {elapsed:.1f}s | "
        f"grants: {counts['grants']:,} | officers: {counts['officers']:,} | "
        f"contributors: {counts['contributors']:,} | contractors: {counts['contractors']:,} | "
        f"top_employees: {counts['top_employees']:,} | investments: {counts['investments']:,} | "
        f"capital_gains: {counts['capital_gains']:,} | "
        f"program_activities: {counts['program_activities']:,} | "
        f"program_investments: {counts['program_investments']:,} | "
        f"errors: {errors:,}"
    )


def _flush_all(con, bufs, counts):
    table_sql = {
        "grants": INSERT_GRANTS,
        "officers": INSERT_OFFICERS,
        "contributors": INSERT_CONTRIBUTORS,
        "contractors": INSERT_CONTRACTORS,
        "top_employees": INSERT_TOP_EMPLOYEES,
        "investments": INSERT_INVESTMENTS,
        "capital_gains": INSERT_CAPGAINS,
        "program_activities": INSERT_PROG_ACT,
        "program_investments": INSERT_PROG_INV,
    }
    for table, sql in table_sql.items():
        if bufs[table]:
            con.executemany(sql, bufs[table])
            counts[table] += len(bufs[table])
            bufs[table].clear()

    # Scalar updates
    for oid, scalars in bufs["scalar_updates"]:
        if scalars:
            sets = ", ".join(f"{k} = ?" for k in scalars)
            vals = list(scalars.values()) + [oid]
            con.execute(f"UPDATE returns SET {sets} WHERE object_id = ?", vals)
    counts["scalar_updates"] += len(bufs["scalar_updates"])
    bufs["scalar_updates"].clear()

    con.commit()


# ── Main ──────────────────────────────────────────────────────────────────
def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(LOG_PATH, mode="a"),
            logging.StreamHandler(sys.stdout),
        ],
    )

    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])
        logging.info(f"TEST MODE: limiting to {limit} files")

    n_workers = max(1, mp.cpu_count() - 2)

    logging.info("Discovering 990-PF files...")
    all_files = discover_pf_files(BASE_DIR, DB_PATH)
    logging.info(f"Found {len(all_files):,} 990-PF filings")

    if not all_files:
        logging.info("No 990-PF files found.")
        return

    todo = all_files
    if limit:
        todo = todo[:limit]
        logging.info(f"Limited to {len(todo):,} files")

    chunks = [todo[i:i + WORKER_CHUNK_SIZE]
              for i in range(0, len(todo), WORKER_CHUNK_SIZE)]

    result_queue = mp.Queue(maxsize=10_000)

    writer = mp.Process(target=writer_process, args=(DB_PATH, result_queue, len(todo)))
    writer.start()

    logging.info(f"Starting {n_workers} workers across {len(chunks):,} chunks...")
    t0 = time.time()

    with mp.Pool(n_workers) as pool:
        for chunk_results in pool.imap_unordered(process_chunk, chunks, chunksize=1):
            result_queue.put(chunk_results)

    result_queue.put(None)
    writer.join()

    elapsed = time.time() - t0
    logging.info(f"Total elapsed: {elapsed:.1f}s ({elapsed/60:.1f} min)")

    _print_summary(DB_PATH)


def _print_summary(db_path):
    con = sqlite3.connect(db_path)
    logging.info("─── 990-PF Detail Extraction Summary ───")

    for table in ["grants", "officers", "contributors", "contractors",
                   "top_employees", "investments", "capital_gains",
                   "program_activities", "program_investments"]:
        count = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        logging.info(f"  {table}: {count:,} rows")

    logging.info("Grants by type:")
    for gt, n in con.execute(
        "SELECT grant_type, COUNT(*) FROM grants GROUP BY grant_type ORDER BY COUNT(*) DESC"
    ):
        logging.info(f"  {gt}: {n:,}")

    # Scalar backfill check
    has_qd = con.execute(
        "SELECT COUNT(*) FROM returns WHERE return_type='990PF' AND qualifying_distributions IS NOT NULL"
    ).fetchone()[0]
    total_pf = con.execute(
        "SELECT COUNT(*) FROM returns WHERE return_type='990PF'"
    ).fetchone()[0]
    logging.info(f"Scalar backfill: {has_qd:,}/{total_pf:,} 990-PF returns have qualifying_distributions")

    con.close()


if __name__ == "__main__":
    main()
