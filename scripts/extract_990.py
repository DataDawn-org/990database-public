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
from collections import Counter
from lxml import etree as ET

from name_rules import join_name, classify_line2

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

# Collision baseline (2026-07-16 audit). The same-object_id collision set is TWO extinct
# IRS TEOS bulk re-export events (2018 + 2020), 93,148 clean 2-way pairs, PROVEN 0/93,148
# substantive diffs in extracted returns fields (both files re-parsed with THIS parser —
# working-docs/collision_audit_2026-07-16.log). Expected-STABLE: incremental runs add
# non-colliding files and leave this untouched. A deviation = a NEW same-object_id re-ship
# ARRIVED (the rare event) → investigate + parse-compare the newcomer; do NOT assume benign.
# ⚠ DELIBERATE-CHANGE-ONLY baseline: move this number ONLY by a considered human decision after
# inspecting WHY the collision universe changed — NEVER bump it reflexively to silence the warning.
# The tripwire's entire value depends on this constant not decaying into muted noise.
EXPECTED_COLLISION_OIDS = 93_148

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
    # Canonical-filing layer Phase-1 (2026-07-06, ratified 2026-07-05): the recency-key
    # inputs. object_id is release-batch order, NOT recency (measured ~24% wrong-pick on
    # multi-filing groups) — selection precedence is amended_return > return_ts >
    # object_id (determinism only). Backfilled over the existing corpus by
    # backfill_canonical_cols.py, which imports extract_canonical_header_fields below.
    "amended_return", "return_ts",
    # #306/#299 store-both phase 1 (2026-07-11, D-spec addendum BUILD SCOPE):
    # RAW as-filed name lines + granular rule label (+street flag), plus the
    # group-exemption fields read in the same parse. org_name above stays
    # LEGACY (line1-only) until the flip; joining lives in name_rules then.
    # Backfilled over the existing corpus by backfill_name_cols.py, which
    # imports extract_name_and_group_fields below (one implementation).
    "name_line1", "name_line2", "name_rule_class", "name_street_suffix",
    "group_exemption_num", "group_return_for_affiliates_ind",
    "all_affiliates_included_ind",
    # Band-1 remainder §A (manifest §A as amended 2026-07-18; ported from
    # dev_extract_band1_remainder.py 2026-07-19, port-equivalence receipts in
    # working-docs). Part I summary scalars + Part III mission + item-F
    # principal officer, Form 990 only (NULL for EZ/PF/T). The manifest draft's
    # group_return_ind is deliberately NOT here: GroupReturnForAffiliatesInd
    # already lands as group_return_for_affiliates_ind above (store-both
    # phase 1) — a second column would duplicate the same element.
    "voting_members_cnt", "voting_members_independent_cnt",
    "total_employee_cnt", "total_volunteers_cnt", "gross_receipts",
    "formation_year", "legal_domicile_state", "activity_or_mission_desc",
    "mission_desc", "website", "principal_officer_name",
]

# Band-1 §A (name, declared type) — single list driving CREATE TABLE parity,
# the existing-DB ALTER path in create_schema (same idiom as the PF scalars in
# extract_990pf_detail.create_schema), and the canonical view refresh.
BAND1_A_COLUMNS = [
    ("voting_members_cnt", "INTEGER"),
    ("voting_members_independent_cnt", "INTEGER"),
    ("total_employee_cnt", "INTEGER"),
    ("total_volunteers_cnt", "INTEGER"),
    ("gross_receipts", "INTEGER"),
    ("formation_year", "INTEGER"),
    ("legal_domicile_state", "TEXT"),
    ("activity_or_mission_desc", "TEXT"),
    ("mission_desc", "TEXT"),
    ("website", "TEXT"),
    ("principal_officer_name", "TEXT"),
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
            contractors_over_100k_cnt  INTEGER,
            -- Canonical-filing layer Phase-1 (2026-07-06). amended_return: 1 = the form
            -- element carries AmendedReturnInd (checkbox present), 0 = XML parsed and the
            -- checkbox is absent, NULL = unknown (parse error / no local XML at backfill).
            -- return_ts: ReturnHeader/ReturnTs verbatim as filed (ISO-8601 with UTC offset).
            amended_return             INTEGER,
            return_ts                  TEXT,
            -- #306/#299 store-both phase 1 (2026-07-11). name_line1/name_line2:
            -- Filer BusinessNameLine1Txt/Line2Txt RAW as filed (byte-faithful —
            -- never normalized here; the derive layer at the flip owns joining).
            -- name_rule_class: name_rules.classify_line2 granular label (cached
            -- convenience, re-derivable from l1/l2 alone; READ_ERR = backfill
            -- could not read/parse the source XML). name_street_suffix:
            -- street/suite token flag (labeled, never excluded — Amendment 1).
            -- group_exemption_num: GEN as filed (TEXT — leading zeros are
            -- significant, e.g. '0544'). *_ind: 1/0 from the true/false element,
            -- NULL = element absent (or parse error / pre-backfill row).
            name_line1                 TEXT,
            name_line2                 TEXT,
            name_rule_class            TEXT,
            name_street_suffix         INTEGER,
            group_exemption_num        TEXT,
            group_return_for_affiliates_ind INTEGER,
            all_affiliates_included_ind INTEGER,
            -- Band-1 remainder §A (2026-07-19). Part I summary + Part III
            -- mission + item-F principal officer; Form 990 only, NULL = not
            -- reported (rule 3). formation_year / count outliers are stored
            -- AS FILED (flag-class, never auto-corrected). On existing DBs
            -- these are ALTER-added by create_schema from BAND1_A_COLUMNS.
            voting_members_cnt             INTEGER,
            voting_members_independent_cnt INTEGER,
            total_employee_cnt             INTEGER,
            total_volunteers_cnt           INTEGER,
            gross_receipts                 INTEGER,
            formation_year                 INTEGER,
            legal_domicile_state           TEXT,
            activity_or_mission_desc       TEXT,
            mission_desc                   TEXT,
            website                        TEXT,
            principal_officer_name         TEXT
        );
        -- idx_ein removed 2026-04-11: subset of idx_returns_ein_type and idx_returns_ein_year_oid
        CREATE INDEX IF NOT EXISTS idx_return_type ON returns(return_type);
        CREATE INDEX IF NOT EXISTS idx_tax_year    ON returns(tax_year);

    """)
    # Existing-DB path for the Band-1 §A columns (fresh builds get them from
    # CREATE TABLE above; a pre-Band-1 DB gains them here, appended in
    # BAND1_A_COLUMNS order so live PRAGMA order matches the view list below).
    have = {r[1] for r in con.execute("PRAGMA table_info(returns)")}
    for col, decl in BAND1_A_COLUMNS:
        if col not in have:
            con.execute(f"ALTER TABLE returns ADD COLUMN {col} {decl}")
    _refresh_canonical_view(con)
    con.commit()


# Canonical-filing selection view (Phase-1, ratified 2026-07-05). One canonical
# filing per (ein, tax_year, return_type) — per-TYPE, never across types.
# Precedence: amended_return=1 > latest datetime(return_ts) > object_id
# (determinism only; object_id is release-batch order, NOT recency).
# Column list is EXPLICIT and ORDERED to match the live DB's PRAGMA order
# (projects away the window rn); the canonical_selection harness invariant REDs
# if returns gains a column this list lacks — adding a returns column REQUIRES
# extending this list in the same change (the conscious refresh; Band-1 §A did
# exactly that 2026-07-19). It names the FULL final master schema including the
# 5 PF scalars that extract_990pf_detail ADD COLUMNs later — SQLite resolves
# view columns at QUERY time, so on a fresh build the view exists early and
# errors loud (never silently wrong) if queried before those ALTERs land.
CANONICAL_VIEW_SQL = """CREATE VIEW canonical_returns AS
        SELECT
            object_id, ein, org_name, state, tax_year, tax_period_end,
            return_type, ntee_code, total_revenue, total_expenses,
            program_expenses, fundraising_expenses, management_expenses,
            total_assets_eoy, officer_comp, source_file, parse_error,
            contributions_received, dividends, interest_income,
            net_gain_sale_assets, contributions_paid, fmv_assets_eoy,
            net_assets_eoy, grants_payable_eoy, qualifying_distributions,
            distributable_amount, min_investment_return, excess_distribution_cyov,
            total_functional_expenses, return_version,
            contractors_over_100k_cnt, amended_return, return_ts,
            name_line1, name_line2, name_rule_class, name_street_suffix,
            group_exemption_num, group_return_for_affiliates_ind,
            all_affiliates_included_ind,
            voting_members_cnt, voting_members_independent_cnt,
            total_employee_cnt, total_volunteers_cnt, gross_receipts,
            formation_year, legal_domicile_state, activity_or_mission_desc,
            mission_desc, website, principal_officer_name
        FROM (
          SELECT r.*, ROW_NUMBER() OVER (
            PARTITION BY ein, tax_year, return_type
            ORDER BY (amended_return IS 1) DESC,
                     COALESCE(datetime(return_ts),'') DESC,
                     object_id DESC) AS rn
          FROM returns r)
        WHERE rn = 1"""


def _refresh_canonical_view(con):
    """Create canonical_returns, or recreate it when its stored definition
    differs from CANONICAL_VIEW_SQL (i.e. the view predates a column add).
    Compares sqlite_master's stored CREATE text — works on fresh DBs too,
    where querying the view would error (PF scalars not yet ALTER-added)."""
    stored = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='view' AND name='canonical_returns'"
    ).fetchone()
    if stored is not None and stored[0] == CANONICAL_VIEW_SQL:
        return
    con.execute("DROP VIEW IF EXISTS canonical_returns")
    con.execute(CANONICAL_VIEW_SQL)


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


def collision_census(all_files):
    """Count the object_ids backed by >1 on-disk file — the silent-skip universe.

    `object_id` is IRS's per-SUBMISSION identifier, so a same-object_id collision
    can ONLY be the IRS re-serializing ONE submission across batches (the 2018/2020
    TEOS_XML_CT1 re-exports). A genuine amendment gets a NEW object_id and does not
    collide (verified 2026-07-15: EIN 421200523 / 592919630 TY2021 originals +
    amendments carry distinct object_ids and changed revenue). Both silent-skip
    sites — the todo-filter (already-in-DB) and `INSERT OR IGNORE` (within-run) —
    drop exactly (n-1) rows per colliding object_id.

    This OBSERVES and COUNTS every such drop; it does NOT change ingest behavior
    (keep-first stands — "newest wins" is a maintainer ratification, queue TOP). Returns
    (n_collision_oids, n_rows_dropped, {oid: [files]}).
    """
    counts = Counter(object_id_from_path(f) for f in all_files)
    collision_oids = {oid for oid, n in counts.items() if n > 1}
    n_dropped = sum(counts[oid] - 1 for oid in collision_oids)
    coll_files = {}
    if collision_oids:
        for f in all_files:
            oid = object_id_from_path(f)
            if oid in collision_oids:
                coll_files.setdefault(oid, []).append(f)
    return len(collision_oids), n_dropped, coll_files


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


def full_biz_name(biz):
    """BusinessName container element -> Line1 [+ Line2] under the #306/#299
    rule — ONE implementation, name_rules.join_name (see that module's header
    for the rule, its receipts, and the emission semantics)."""
    if biz is None:
        return None
    return join_name(find_text(biz, "BusinessNameLine1Txt"),
                     find_text(biz, "BusinessNameLine2Txt"))


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


def text_or_none(val):
    """Band-1 §A TEXT normalization (witnessed dev semantics): strip; a
    present-but-empty/whitespace element carries no content → NULL (rule-5
    value-level pin). NOT for name_line1/name_line2 — those stay byte-faithful
    (DO-NOT #1, raw_name_fidelity_gate)."""
    if val is None:
        return None
    v = val.strip()
    return v if v else None


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

    # ── Band-1 remainder §A (manifest §A as amended 2026-07-18) ──────────────
    # Part I summary scalars + Part III mission, flat under IRS990. Values are
    # stored AS FILED (formation_year 9999-class, >1000 "boards" = flag-class,
    # never auto-corrected). GroupReturnForAffiliatesInd is deliberately NOT
    # read here — it already lands as group_return_for_affiliates_ind
    # (extract_name_and_group_fields).
    row["voting_members_cnt"] = int_or_none(find_text(irs, "VotingMembersGoverningBodyCnt"))
    row["voting_members_independent_cnt"] = int_or_none(find_text(irs, "VotingMembersIndependentCnt"))
    row["total_employee_cnt"] = int_or_none(find_text(irs, "TotalEmployeeCnt"))
    row["total_volunteers_cnt"] = int_or_none(find_text(irs, "TotalVolunteersCnt"))
    row["gross_receipts"] = int_or_none(find_text(irs, "GrossReceiptsAmt"))
    row["formation_year"] = int_or_none(find_text(irs, "FormationYr"))
    row["legal_domicile_state"] = text_or_none(find_text(irs, "LegalDomicileStateCd"))
    row["activity_or_mission_desc"] = text_or_none(find_text(irs, "ActivityOrMissionDesc"))
    row["mission_desc"] = text_or_none(find_text(irs, "MissionDesc"))
    row["website"] = text_or_none(find_text(irs, "WebsiteAddressTxt"))

    # principal_officer_name: item F. PersonNm carrier first; ~3.1% of filings
    # carry the same line as a business name — value-level fallback (rule 5)
    # via the sanctioned name_rules.join_name idiom. NEVER the ReturnHeader
    # e-file signer (rule 4 — that is a different person; store NULL instead).
    row["principal_officer_name"] = text_or_none(find_text(irs, "PrincipalOfficerNm"))
    if row["principal_officer_name"] is None:
        pob = irs.find(_tag("PrincipalOfcrBusinessName"))
        if pob is not None:
            row["principal_officer_name"] = join_name(
                find_text(pob, "BusinessNameLine1Txt"),
                find_text(pob, "BusinessNameLine2Txt"))


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


def extract_canonical_header_fields(root, row):
    """Canonical-filing layer Phase-1: read the recency-key inputs, fully anchored.

    return_ts       ReturnHeader/ReturnTs — direct child of ReturnHeader in every
                    processing year 2017-2026 (verified on real filings 2026-07-06).
                    Stored verbatim as filed.
    amended_return  ReturnData/IRS<return_type>/AmendedReturnInd — a direct child of
                    the form element in ALL sampled amended filings across all 10
                    processing years and all 4 form types (250-file census 2026-07-06;
                    never appears deeper, so no bare .// descent that could mis-anchor
                    on a schedule-level element). Presence of the element = amended
                    (same measurement as the Phase-0 ground-truth probe). 0 requires
                    the form element to be present-and-checked; if ReturnData or the
                    form element is missing, the flag stays NULL (unknown), never 0.

    Called by parse_file() on the monthly path and IMPORTED by
    backfill_canonical_cols.py — one implementation, no drift.
    """
    row["return_ts"] = find_text(root, "ReturnHeader.ReturnTs")
    return_type = row.get("return_type")
    if not return_type:
        return
    return_data = root.find(_tag("ReturnData"))
    if return_data is None:
        return
    form = return_data.find(_tag(f"IRS{return_type}"))
    if form is None:
        return
    row["amended_return"] = 1 if form.find(_tag("AmendedReturnInd")) is not None else 0


def _ind_01(txt):
    """MeF boolean/checkbox element text -> 1/0/NULL (absent or unrecognized)."""
    if txt is None:
        return None
    t = txt.strip().lower()
    if t in ("true", "1", "x"):
        return 1
    if t in ("false", "0"):
        return 0
    return None


def extract_name_and_group_fields(root, row):
    """#306/#299 store-both phase 1 (D-spec addendum BUILD SCOPE, 2026-07-11).

    name_line1/name_line2: Filer BusinessNameLine1Txt/Line2Txt element text
    VERBATIM — deliberately NOT routed through full_biz_name/join_name (DO-NOT
    #1: those normalize; joining is org_name's job — DONE at the flip, maintainer-GO
    2026-07-15). org_name above = the join; name_line1/name_line2 stay verbatim.

    name_rule_class/name_street_suffix: name_rules.classify_line2 on the raw
    pair — a cached convenience, re-derivable from the stored columns alone.

    Group-exemption fields (round-9 probe 2026-07-11): direct children of the
    form element (IRS990/IRS990EZ carry them; PF/T do not), anchored exactly
    like extract_canonical_header_fields above — no bare .// descent.
    GroupExemptionNum stays TEXT (leading zeros significant, e.g. '0544').

    Called by parse_file() on the monthly path and IMPORTED by
    backfill_name_cols.py (backfill_newcols doctrine: one implementation, the
    backfill cannot drift from what the monthly writes going forward).
    """
    row["name_line1"] = find_text(root, "ReturnHeader.Filer.BusinessName.BusinessNameLine1Txt")
    row["name_line2"] = find_text(root, "ReturnHeader.Filer.BusinessName.BusinessNameLine2Txt")
    cls, street = classify_line2(row["name_line1"], row["name_line2"])
    row["name_rule_class"] = cls
    row["name_street_suffix"] = street
    return_type = row.get("return_type")
    if not return_type:
        return
    return_data = root.find(_tag("ReturnData"))
    if return_data is None:
        return
    form = return_data.find(_tag(f"IRS{return_type}"))
    if form is None:
        return
    row["group_exemption_num"] = find_text(form, "GroupExemptionNum")
    row["group_return_for_affiliates_ind"] = _ind_01(find_text(form, "GroupReturnForAffiliatesInd"))
    row["all_affiliates_included_ind"] = _ind_01(find_text(form, "AllAffiliatesIncludedInd"))


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
        # #306/#299 FLIP (maintainer-GO 2026-07-15): org_name = Line1 [+ Line2] via the
        # single rule name_rules.join_name (bare-CO carve merged into that module).
        # DO-NOT #2 (legacy line1-only) is now LIFTED. Existing rows were folded by
        # backfill_org_name_flip.py using the SAME join_name over stored name_line1/
        # name_line2 — provably identical to this parse-time emission.
        row["org_name"] = join_name(
            find_text(root, "ReturnHeader.Filer.BusinessName.BusinessNameLine1Txt"),
            find_text(root, "ReturnHeader.Filer.BusinessName.BusinessNameLine2Txt"))
        row["tax_year"] = int_or_none(find_text(root, "ReturnHeader.TaxYr"))
        row["tax_period_end"] = find_text(root, "ReturnHeader.TaxPeriodEndDt")

        # Canonical-filing layer Phase-1: recency-key inputs (needs return_type, set above)
        extract_canonical_header_fields(root, row)

        # #306/#299 store-both phase 1: raw l1/l2 + rule label + group-exemption
        # fields, ALONGSIDE the untouched legacy org_name above (needs return_type)
        extract_name_and_group_fields(root, row)

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
    # Corpus write-lock seam gate (completeness spec §0.12): delegated under
    # update.sh's lock via CORPUS_LOCK_TOKEN_990; standalone runs acquire
    # (auto-release at exit); any other holder = hard stop, never a warning.
    sys.path.insert(0, "/mnt/data/datadawn/tools")
    from corpus_lock import gate as _corpus_gate
    _corpus_gate("990", intent="extract_990.py (returns ingest)")

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

    # Collision census (queue-TOP silent-skip visibility, 2026-07-15): count + list
    # every object_id backed by >1 on-disk file BEFORE the todo-filter and
    # INSERT OR IGNORE silently drop the extras. Turns "silent row loss" into an
    # observed, logged fact. See collision_census() docstring. Ingest unchanged.
    n_coll, n_dropped, coll_files = collision_census(all_files)
    logging.info(
        f"Collision census: {n_coll:,} object_id(s) backed by >1 on-disk file; "
        f"{n_dropped:,} row(s) dropped by object_id dedup (keep-first)."
    )
    # Forward tripwire (#4): baseline is 93,148 clean 2-way pairs (extinct 2018+2020 TEOS
    # events). A count off the baseline, or any oid gaining a 3rd file, means a NEW re-ship
    # arrived — the rare event worth reacting to (parse+compare the delta), never auto-benign.
    max_mult = max((len(fs) for fs in coll_files.values()), default=0)
    if n_coll != EXPECTED_COLLISION_OIDS or max_mult > 2:
        logging.warning(
            f"COLLISION BASELINE DEVIATION: expected {EXPECTED_COLLISION_OIDS:,} clean 2-way "
            f"pairs, got {n_coll:,} colliding oids (max files/oid={max_mult}). A new "
            f"same-object_id re-ship arrived — parse+compare the delta before trusting it."
        )
    if coll_files:
        census_path = os.path.join(BASE_DIR, "collision_census.txt")
        with open(census_path, "w") as fh:
            for oid in sorted(coll_files):
                fh.write(f"{oid}\t{len(coll_files[oid])}\t{'|'.join(coll_files[oid])}\n")
        logging.info(f"  → colliding object_ids listed in {census_path}")

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
