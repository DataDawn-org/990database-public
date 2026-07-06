#!/usr/bin/env python3
"""
Extract additional detail from 990/990EZ XML files:
  - Officers/Directors (Part VII / 990EZ officer section)
  - Schedule I grants (from ALL 990 filers, not just DAF sponsors)
  - Schedule R related organizations

Processes XML files already indexed in the returns table.
Uses multiprocessing for speed.

Usage:
    python3 extract_990_detail.py              # full run
    python3 extract_990_detail.py --limit 100  # test with 100 files
"""

import logging
import multiprocessing as mp
import os
import sqlite3
import sys
import time
from lxml import etree as ET

# XXE-hardened parser for IRS XML — disable external entities + network DTD lookup
# (per-worker module-level constant; lxml XMLParser is process-safe after fork).
_SAFE_PARSER = ET.XMLParser(resolve_entities=False, no_network=True)

# ── Configuration ──────────────────────────────────────────────────────────
BASE_DIR = "/mnt/data/datadawn/990project"
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


# §2 Deliverable A: the monthly's EIN path — single source of truth. The backfill adapter
# (backfill_contractors._real_extractor) imports BOTH find_text and this constant, so the bulk
# path reads the EIN through the identical code the monthly uses — no re-implementation to drift.
EIN_PATH = "ReturnHeader.Filer.EIN"


def _present(el, tag):
    return el is not None and el.find(_tag(tag)) is not None


# ── Schema ─────────────────────────────────────────────────────────────────
SCHEMA_SQL = """
-- Schedule I grants from 990 filers (public charities)
CREATE TABLE IF NOT EXISTS schedule_i_990 (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id             TEXT NOT NULL,
    ein                   TEXT,
    recipient_name        TEXT,
    recipient_ein         TEXT,
    recipient_city        TEXT,
    recipient_state       TEXT,
    recipient_zip         TEXT,
    irc_section           TEXT,
    cash_grant_amt        INTEGER,
    non_cash_amt          INTEGER,
    purpose               TEXT
);
CREATE INDEX IF NOT EXISTS idx_si990_oid ON schedule_i_990(object_id);
CREATE INDEX IF NOT EXISTS idx_si990_ein ON schedule_i_990(ein);
CREATE INDEX IF NOT EXISTS idx_si990_recip_ein ON schedule_i_990(recipient_ein);

-- Related organizations from Schedule R
CREATE TABLE IF NOT EXISTS related_orgs (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id             TEXT NOT NULL,
    ein                   TEXT,
    related_org_name      TEXT,
    related_ein           TEXT,
    city                  TEXT,
    state                 TEXT,
    zip                   TEXT,
    primary_activity      TEXT,
    legal_domicile        TEXT,
    exempt_code_section   TEXT,
    public_charity_status TEXT,
    direct_controlling    TEXT,
    controlled_org_ind    INTEGER,
    section               TEXT
);
CREATE INDEX IF NOT EXISTS idx_relorg_oid ON related_orgs(object_id);
CREATE INDEX IF NOT EXISTS idx_relorg_ein ON related_orgs(ein);
CREATE INDEX IF NOT EXISTS idx_relorg_related_ein ON related_orgs(related_ein);
"""


def create_schema(con):
    con.executescript(SCHEMA_SQL)
    con.commit()


# ── File Discovery ─────────────────────────────────────────────────────────
def discover_files(db_path):
    """Find all 990/990EZ object_ids from DB that haven't been processed yet."""
    con = sqlite3.connect(db_path)
    rows = con.execute(
        "SELECT object_id, source_file, return_type FROM returns "
        "WHERE return_type IN ('990', '990EZ')"
    ).fetchall()

    # Get already-processed object_ids from each target table
    already_officers = {r[0] for r in con.execute(
        "SELECT DISTINCT object_id FROM officers"
    )}
    # For schedule_i_990 and related_orgs, check if tables exist
    already_sched_i = set()
    already_related = set()
    try:
        already_sched_i = {r[0] for r in con.execute(
            "SELECT DISTINCT object_id FROM schedule_i_990"
        )}
    except sqlite3.OperationalError:
        pass
    try:
        already_related = {r[0] for r in con.execute(
            "SELECT DISTINCT object_id FROM related_orgs"
        )}
    except sqlite3.OperationalError:
        pass
    con.close()

    # A file needs processing if ANY of the three tables still needs it
    already_all = already_officers & already_sched_i & already_related

    # Track missing-file rate as a tripwire (DAF-incident class: 2026-05-01 saw
    # 5.21M stale `source_file` paths silently filtered, producing near-empty
    # extracts). Same 1% guard pattern as extract_schedule_i.py — generalized
    # 2026-05-10 codebase-health audit.
    candidates = [(oid, sf, rt) for oid, sf, rt in rows if oid not in already_all]
    files = [(oid, sf, rt) for oid, sf, rt in candidates if os.path.exists(sf)]
    missing_files = len(candidates) - len(files)
    if candidates and missing_files / len(candidates) > 0.01:
        msg = (f"FATAL: {missing_files:,}/{len(candidates):,} ({missing_files/len(candidates):.1%}) "
               f"source_file paths missing — refusing to extract a near-empty "
               f"officers/schedule_i_990/related_orgs. Check returns.source_file paths.")
        logging.error(msg)
        sys.exit(2)

    logging.info(f"  Already in officers: {len(already_officers):,}")
    logging.info(f"  Already in schedule_i_990: {len(already_sched_i):,}")
    logging.info(f"  Already in related_orgs: {len(already_related):,}")
    return files, already_officers, already_sched_i, already_related


# ── Per-File Extraction ───────────────────────────────────────────────────
def parse_file(args):
    oid, filepath, return_type = args
    result = {
        "object_id": oid,
        "return_type": return_type,
        "ein": None,
        "officers": [],
        "contractors": [],
        "schedule_i": [],
        "related_orgs": [],
        "error": None,
    }

    try:
        tree = ET.parse(filepath, parser=_SAFE_PARSER)
        root = tree.getroot()
        ein_val = find_text(root, EIN_PATH)
        result["ein"] = ein_val

        if return_type == "990":
            _extract_990_officers(root, result)
            _extract_contractors(root, result)
            _extract_schedule_i(root, result)
            _extract_schedule_r(root, result)
        elif return_type == "990EZ":
            _extract_990ez_officers(root, result)

    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"

    return result


def _officer_name(grp):
    """§F person axis — the 3-slot carrier fallback PersonNm → BusinessName/BusinessNameLine1Txt
    → PersonName, identical to instrument_officer_multiset.person_name (the same-measurement
    function that adjudicated the swept corpus). The pre-B parser read only the first two slots;
    aligned per R1–R3 rulings 2026-07-05 so the monthly writer and the re-derive cannot drift
    on the person axis."""
    name = find_text(grp, "PersonNm")
    if name is None:
        biz = grp.find(_tag("BusinessName"))
        if biz is not None:
            name = find_text(biz, "BusinessNameLine1Txt")
    if name is None:
        name = find_text(grp, "PersonName")
    return name


# ── §110/§F keyed dedup (Deliverable B; R1–R3 rulings 2026-07-05) ───────────
# Key = person name + six role flags + six substantive fields + captured hours (R1:
# avg_hours_per_week IS in the key), compared AFTER canonicalization (absent-vs-zero → 0,
# text whitespace-run normalization). Singleton rows store the raw emission (no-change
# witness pins stored==emitted); collapsed groups store the canonicalized values (an
# absent-vs-zero pair stores 0). Tuple layout (16): (object_id, ein, name, title, hours,
# comp_filing, comp_related, other_comp, benefits, expense_account, is_hce, is_officer,
# is_individual_trustee, is_institutional_trustee, is_key_employee, is_former).

def _canon_num(v):
    return 0 if v is None else v


def _canon_text(v):
    return None if v is None else " ".join(str(v).split())


def officer_key(t):
    """Dedup key over one emitted officer tuple — excludes object_id/ein (constant per filing)."""
    return (_canon_text(t[2]), _canon_text(t[3]),
            tuple(_canon_num(t[i]) for i in (4, 5, 6, 7, 8, 9)),
            t[10:16])


def dedup_officers_keyed(rows):
    """Collapse ONE filing's emitted officer tuples under the §110 going-forward key.
    First-occurrence order preserved. NEVER applied across filings (amendments are
    distinct object_ids)."""
    groups, order = {}, []
    for t in rows:
        k = officer_key(t)
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(t)
    out = []
    for k in order:
        g = groups[k]
        if len(g) == 1:
            out.append(g[0])          # singleton: raw emission, byte round-trip
        else:
            name, title, nums, flags = k
            out.append((g[0][0], g[0][1], name, title) + nums + tuple(flags))
    return out


def _extract_990_officers(root, result):
    """Extract officers/directors from 990 Part VII Section A.

    Form 990 column letters from the IRS form:
      D = ReportableCompFromOrgAmt    → reportable_comp_filing_org
      E = ReportableCompFromRltdOrgAmt → reportable_comp_related_org (Form 990 ONLY)
      F = OtherCompensationAmt         → other_compensation (Form 990 ONLY)

    benefits + expense_account columns are NULL for Form 990 rows
    (those columns are for 990-EZ + 990-PF only; see decisions_log §64).
    """
    irs = root.find(f".//{_tag('IRS990')}")
    if irs is None:
        return
    ein = result["ein"]
    oid = result["object_id"]
    for grp in irs.findall(_tag("Form990PartVIISectionAGrp")):
        name = _officer_name(grp)
        title = find_text(grp, "TitleTxt")
        hours = float_or_none(find_text(grp, "AverageHoursPerWeekRt"))
        comp = int_or_none(find_text(grp, "ReportableCompFromOrgAmt"))
        comp_rltd = int_or_none(find_text(grp, "ReportableCompFromRltdOrgAmt"))
        other_comp = int_or_none(find_text(grp, "OtherCompensationAmt"))
        result["officers"].append((
            oid, ein, name, title, hours,
            comp,        # reportable_comp_filing_org
            comp_rltd,   # reportable_comp_related_org (Form 990 only)
            other_comp,  # other_compensation (Form 990 only)
            None,        # benefits (990-EZ/990-PF only — NULL for Form 990)
            None,        # expense_account (990-EZ/990-PF only — NULL for Form 990)
            # §2 + DECISION_role_flags_2026-06-26: every role flag read from ITS OWN box,
            # check-all-that-apply — multi-role rows carry multiple 1s (Norris Fowler);
            # any "if officer then not X" inference is forbidden.
            1 if _present(grp, "HighestCompensatedEmployeeInd") else 0,
            1 if _present(grp, "OfficerInd") else 0,
            1 if _present(grp, "IndividualTrusteeOrDirectorInd") else 0,
            1 if _present(grp, "InstitutionalTrusteeInd") else 0,
            1 if _present(grp, "KeyEmployeeInd") else 0,
            1 if _present(grp, "FormerOfcrDirectorTrusteeInd") else 0,
        ))


def _extract_990ez_officers(root, result):
    """Extract officers/directors from 990EZ.

    990-EZ column letters from the IRS form:
      c = CompensationAmt              → reportable_comp_filing_org
      d = EmployeeBenefitProgramAmt    → benefits
      e = ExpenseAccountOtherAllwncAmt → expense_account

    reportable_comp_related_org + other_compensation are NULL for 990-EZ rows
    (those columns are Form 990 ONLY; see decisions_log §64).
    """
    irs = root.find(f".//{_tag('IRS990EZ')}")
    if irs is None:
        return
    ein = result["ein"]
    oid = result["object_id"]
    for grp in irs.findall(_tag("OfficerDirectorTrusteeEmplGrp")):
        name = _officer_name(grp)
        title = find_text(grp, "TitleTxt")
        hours = float_or_none(find_text(grp, "AverageHrsPerWkDevotedToPosRt"))
        comp = int_or_none(find_text(grp, "CompensationAmt"))
        benefits = int_or_none(find_text(grp, "EmployeeBenefitProgramAmt"))
        expense = int_or_none(find_text(grp, "ExpenseAccountOtherAllwncAmt"))
        result["officers"].append((
            oid, ein, name, title, hours,
            comp,      # reportable_comp_filing_org
            None,      # reportable_comp_related_org (Form 990 only — NULL for 990-EZ)
            None,      # other_compensation (Form 990 only — NULL for 990-EZ)
            benefits,  # benefits
            expense,   # expense_account
            # Section-A role structure does not exist on 990-EZ → all six flags NULL
            # (not-applicable, never 0) — manifest §B rule 3 / §D.
            None,      # is_highest_compensated_employee
            None, None, None, None, None,  # is_officer/indiv_trustee/inst_trustee/key_employee/former
        ))


def _contractor_name(grp):
    """ContractorName WRAPPER -> PersonNm xor BusinessName(Line1[+Line2]). COALESCE both slots; 2-line
    business name: dedupe L2==L1 then space-join (continuation). Identical logic to the proven port
    (dev_extract_990_detail.py, re-triangulated against both oracles)."""
    cn = grp.find(_tag("ContractorName"))
    if cn is None:
        return None
    person = find_text(cn, "PersonNm")
    if person is not None:
        return person
    biz = cn.find(_tag("BusinessName"))
    if biz is None:
        return None
    l1 = find_text(biz, "BusinessNameLine1Txt")
    l2 = find_text(biz, "BusinessNameLine2Txt")
    if l2 and l2 != l1:
        return f"{l1} {l2}" if l1 else l2
    return l1


def _contractor_addr(grp):
    addr = grp.find(_tag("ContractorAddress"))
    if addr is None:
        return (None, None)
    us = addr.find(_tag("USAddress"))
    if us is not None:
        return (find_text(us, "CityNm"), find_text(us, "StateAbbreviationCd"))
    fr = addr.find(_tag("ForeignAddress"))
    if fr is not None:
        return (find_text(fr, "CityNm"), None)  # foreign state NULL by convention
    return (None, None)


def _extract_contractors(root, result):
    """§2: Form 990 Part VII Sec B contractors. Appends (object_id, ein, contractor_name, city, state,
    service_type, compensation) tuples. UNCONDITIONAL append — every ContractorCompensationGrp emits a
    row regardless of name resolution (RAIL 1); group source = IRS990/ContractorCompensationGrp, not a
    sibling repeating group (RAIL 2)."""
    irs = root.find(f".//{_tag('IRS990')}")
    if irs is None:
        return
    oid = result["object_id"]
    ein = result.get("ein")
    for g in irs.findall(_tag("ContractorCompensationGrp")):
        city, state = _contractor_addr(g)
        result["contractors"].append((
            oid, ein, _contractor_name(g), city, state,
            find_text(g, "ServicesDesc"), int_or_none(find_text(g, "CompensationAmt")),
        ))


def _extract_schedule_i(root, result):
    """Extract Schedule I grants from 990 filers."""
    sched = root.find(f".//{_tag('IRS990ScheduleI')}")
    if sched is None:
        return
    ein = result["ein"]
    oid = result["object_id"]
    for rec in sched.findall(_tag("RecipientTable")):
        # Recipient name
        name = find_text(rec, "RecipientBusinessName.BusinessNameLine1Txt")
        if name is None:
            name = find_text(rec, "RecipientPersonNm")

        # Address
        us_addr = rec.find(_tag("USAddress"))
        foreign_addr = rec.find(_tag("ForeignAddress"))
        city = state = zipcode = None
        if us_addr is not None:
            city = find_text(us_addr, "CityNm")
            state = find_text(us_addr, "StateAbbreviationCd")
            zipcode = find_text(us_addr, "ZIPCd")
        elif foreign_addr is not None:
            city = find_text(foreign_addr, "CityNm")
            state = find_text(foreign_addr, "ProvinceOrStateNm")
            zipcode = find_text(foreign_addr, "ForeignPostalCd")

        recip_ein = find_text(rec, "RecipientEIN")
        irc_section = find_text(rec, "IRCSectionDesc")
        cash_amt = int_or_none(find_text(rec, "CashGrantAmt"))
        non_cash = int_or_none(find_text(rec, "NonCashAssistanceAmt"))
        purpose = find_text(rec, "PurposeOfGrantTxt")

        result["schedule_i"].append((
            oid, ein, name, recip_ein, city, state, zipcode,
            irc_section, cash_amt, non_cash, purpose,
        ))


def _extract_schedule_r(root, result):
    """Extract Schedule R related organizations."""
    sched = root.find(f".//{_tag('IRS990ScheduleR')}")
    if sched is None:
        return
    ein = result["ein"]
    oid = result["object_id"]

    # Part I: Disregarded entities
    for grp in sched.findall(_tag("IdDisregardedEntitiesGrp")):
        _extract_related_org_grp(grp, oid, ein, "disregarded_entity", result)

    # Part II: Related tax-exempt organizations
    for grp in sched.findall(_tag("IdRelatedTaxExemptOrgGrp")):
        _extract_related_org_grp(grp, oid, ein, "related_tax_exempt", result)

    # Part III: Related orgs taxable as partnership
    for grp in sched.findall(_tag("IdRelatedOrgTxblPartnershipGrp")):
        _extract_related_org_grp(grp, oid, ein, "taxable_partnership", result)

    # Part IV: Related orgs taxable as corporation/trust
    for grp in sched.findall(_tag("IdRelatedOrgTxblCorpTrGrp")):
        _extract_related_org_grp(grp, oid, ein, "taxable_corp_trust", result)


def _extract_related_org_grp(grp, oid, ein, section, result):
    """Extract a single related org group element."""
    # Name can be under several element names
    name = None
    for name_tag in ("DisregardedEntityName", "RelatedOrganizationName",
                     "BusinessName"):
        el = grp.find(_tag(name_tag))
        if el is not None:
            name = find_text(el, "BusinessNameLine1Txt")
            if name:
                break
    if name is None:
        # Some have the name directly
        name = find_text(grp, "BusinessNameLine1Txt")

    # Address
    city = state = zipcode = None
    us_addr = grp.find(_tag("USAddress"))
    if us_addr is not None:
        city = find_text(us_addr, "CityNm")
        state = find_text(us_addr, "StateAbbreviationCd")
        zipcode = find_text(us_addr, "ZIPCd")
    else:
        foreign = grp.find(_tag("ForeignAddress"))
        if foreign is not None:
            city = find_text(foreign, "CityNm")
            state = find_text(foreign, "ProvinceOrStateNm")

    related_ein = find_text(grp, "EIN")
    activity = find_text(grp, "PrimaryActivitiesTxt")
    domicile = find_text(grp, "LegalDomicileStateCd")
    exempt_code = find_text(grp, "ExemptCodeSectionTxt")
    charity_status = find_text(grp, "PublicCharityStatusTxt")

    # Direct controlling entity
    controlling = find_text(grp, "DirectControllingNACd")
    if controlling is None:
        controlling = find_text(grp,
            "DirectControllingEntityName.BusinessNameLine1Txt")

    controlled_ind = find_text(grp, "ControlledOrganizationInd")
    controlled = 1 if controlled_ind in ("1", "true", "X") else 0

    result["related_orgs"].append((
        oid, ein, name, related_ein, city, state, zipcode,
        activity, domicile, exempt_code, charity_status,
        controlling, controlled, section,
    ))


# ── Pre-flight namespace check ────────────────────────────────────────────
def _check_namespace_or_bail(sample_filepaths):
    """Ensure IRS XML root namespace still matches the NS constant our
    extractors hardcode. If IRS bumps the schema namespace, every
    `find(_tag(...))` call returns None and we silently insert all-NULL
    rows — same failure shape as the 2026-05-10 DAF incident. Probing the
    first few files catches this loud BEFORE workers run on the full
    batch. Audit H3, 2026-05-15. Cost: ~3 file parses (~1 ms each).
    """
    if not sample_filepaths:
        return
    probes = sample_filepaths[:3]
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


# ── Chunk Processor ──────────────────────────────────────────────────────
def process_chunk(file_list):
    results = []
    for args in file_list:
        r = parse_file(args)
        if r is not None:
            results.append(r)
    return results


# ── Writer Process ────────────────────────────────────────────────────────
def writer_process(db_path, result_queue, total_files,
                   skip_officers, skip_sched_i, skip_related):
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA cache_size=-131072")
    con.execute("PRAGMA temp_store=MEMORY")
    create_schema(con)
    con.commit()

    officer_buf = []
    sched_i_buf = []
    related_buf = []
    contractor_buf = []
    contractor_del_buf = []
    processed = 0
    counts = {"officers": 0, "schedule_i": 0, "related_orgs": 0, "contractors": 0,
              "contractor_rows_deleted": 0, "errors": 0}
    t0 = time.time()
    last_log = 0

    # 5-column comp schema per Bug #3 fix (decisions_log §64):
    #   reportable_comp_filing_org   — W-2 from filing org (all forms)
    #   reportable_comp_related_org  — W-2 from related orgs (Form 990 ONLY; NULL otherwise)
    #   other_compensation           — IRS "other comp" lump (Form 990 ONLY; NULL otherwise)
    #   benefits                     — Employee benefit program (990-EZ + 990-PF ONLY; NULL for Form 990)
    #   expense_account              — Expense account + allowances (990-EZ + 990-PF ONLY; NULL for Form 990)
    # `compensation` column is legacy and will be dropped in Phase 2 of the Bug #3 migration;
    # parsers do NOT write to it. Phase 1 migration copied historical compensation → reportable_comp_filing_org.
    OFFICER_SQL = """INSERT INTO officers
        (object_id, ein, person_name, title, avg_hours_per_week,
         reportable_comp_filing_org, reportable_comp_related_org, other_compensation,
         benefits, expense_account, is_highest_compensated_employee,
         is_officer, is_individual_trustee, is_institutional_trustee,
         is_key_employee, is_former)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""
    # §2: contractors are per-oid DELETE-then-INSERT (idempotency contract, build packet §3/§6.2 —
    # cleared 2026-06-26). NOT skip-set-gated: a zero-contractor filing is indistinguishable from an
    # unprocessed one by presence, so presence-skipping would reprocess forever / append-only would
    # double-insert on re-runs. A parse-error result neither deletes nor inserts.
    CONTRACTOR_SQL = """INSERT INTO contractors
        (object_id, ein, contractor_name, city, state, service_type, compensation)
        VALUES (?,?,?,?,?,?,?)"""
    CONTRACTOR_DEL_SQL = "DELETE FROM contractors WHERE object_id=?"
    SCHED_I_SQL = """INSERT INTO schedule_i_990
        (object_id, ein, recipient_name, recipient_ein,
         recipient_city, recipient_state, recipient_zip,
         irc_section, cash_grant_amt, non_cash_amt, purpose)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)"""
    RELATED_SQL = """INSERT INTO related_orgs
        (object_id, ein, related_org_name, related_ein,
         city, state, zip, primary_activity, legal_domicile,
         exempt_code_section, public_charity_status,
         direct_controlling, controlled_org_ind, section)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""

    while True:
        try:
            item = result_queue.get(timeout=120)
        except Exception:
            continue

        if item is None:
            break

        items = item if isinstance(item, list) else [item]
        for r in items:
            processed += 1
            oid = r["object_id"]

            if r.get("error"):
                counts["errors"] += 1

            # Officers (skip if already in DB). Going-forward clause (§110 / R1–R3
            # rulings 2026-07-05): writes land under the keyed dedup, never the raw
            # emission — otherwise raw dups re-accumulate every monthly (#265 conseq. 1).
            if oid not in skip_officers and r["officers"]:
                keyed = dedup_officers_keyed(r["officers"])
                officer_buf.extend(keyed)
                counts["officers"] += len(keyed)

            # Schedule I
            if oid not in skip_sched_i and r["schedule_i"]:
                sched_i_buf.extend(r["schedule_i"])
                counts["schedule_i"] += len(r["schedule_i"])

            # Related orgs
            if oid not in skip_related and r["related_orgs"]:
                related_buf.extend(r["related_orgs"])
                counts["related_orgs"] += len(r["related_orgs"])

            # Contractors — DELETE-then-INSERT per parsed 990 (even when 0 rows: clears stale rows
            # on a re-run); never on a parse error (a bad parse must not wipe existing rows).
            if r["return_type"] == "990" and not r.get("error"):
                contractor_del_buf.append((oid,))
                if r.get("contractors"):
                    contractor_buf.extend(r["contractors"])
                    counts["contractors"] += len(r["contractors"])

        # Flush buffers — ALL tables under ONE commit whenever ANY buffer hits the
        # threshold (#264 P9 cross-table fix): a filing committed in the legacy
        # tables can then never be missing its contractor leg; an interrupted
        # flush rolls back whole, across all four tables.
        if (len(officer_buf) >= BATCH_INSERT_SIZE
                or len(sched_i_buf) >= BATCH_INSERT_SIZE
                or len(related_buf) >= BATCH_INSERT_SIZE
                or len(contractor_del_buf) >= BATCH_INSERT_SIZE
                or len(contractor_buf) >= BATCH_INSERT_SIZE):
            con.executemany(OFFICER_SQL, officer_buf)
            con.executemany(SCHED_I_SQL, sched_i_buf)
            con.executemany(RELATED_SQL, related_buf)
            pre_changes = con.total_changes
            con.executemany(CONTRACTOR_DEL_SQL, contractor_del_buf)
            counts["contractor_rows_deleted"] += con.total_changes - pre_changes
            con.executemany(CONTRACTOR_SQL, contractor_buf)
            con.commit()
            officer_buf.clear()
            sched_i_buf.clear()
            related_buf.clear()
            contractor_del_buf.clear()
            contractor_buf.clear()

        if processed - last_log >= LOG_INTERVAL:
            elapsed = time.time() - t0
            rate = processed / elapsed if elapsed > 0 else 0
            logging.info(
                f"Progress: {processed:,}/{total_files:,} "
                f"({100*processed/total_files:.1f}%) | "
                f"{rate:.0f} files/sec | "
                f"officers: {counts['officers']:,} | "
                f"sched_i: {counts['schedule_i']:,} | "
                f"related: {counts['related_orgs']:,} | "
                f"contractors: {counts['contractors']:,} ins/{counts['contractor_rows_deleted']:,} del | "
                f"errors: {counts['errors']:,}"
            )
            last_log = processed

    # Final flush
    if officer_buf:
        con.executemany(OFFICER_SQL, officer_buf)
    if sched_i_buf:
        con.executemany(SCHED_I_SQL, sched_i_buf)
    if related_buf:
        con.executemany(RELATED_SQL, related_buf)
    if contractor_del_buf or contractor_buf:
        pre_changes = con.total_changes
        con.executemany(CONTRACTOR_DEL_SQL, contractor_del_buf)
        counts["contractor_rows_deleted"] += con.total_changes - pre_changes
        con.executemany(CONTRACTOR_SQL, contractor_buf)
    con.commit()

    elapsed = time.time() - t0
    con.close()
    logging.info(
        f"Writer done. {processed:,} files in {elapsed:.1f}s | "
        f"officers: {counts['officers']:,} | "
        f"schedule_i: {counts['schedule_i']:,} | "
        f"related_orgs: {counts['related_orgs']:,} | "
        f"contractors: {counts['contractors']:,} inserted / "
        f"{counts['contractor_rows_deleted']:,} deleted "
        f"(net {counts['contractors'] - counts['contractor_rows_deleted']:+,}) | "
        f"errors: {counts['errors']:,}"
    )


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

    logging.info("Discovering 990/990EZ files for detail extraction...")
    files, skip_officers, skip_sched_i, skip_related = discover_files(DB_PATH)
    logging.info(f"Found {len(files):,} files to process")

    if not files:
        logging.info("Nothing to do.")
        return

    if limit:
        files = files[:limit]
        logging.info(f"Limited to {len(files):,} files")

    # Pre-flight namespace check — abort loud if IRS schema bumped
    _check_namespace_or_bail([f[1] for f in files])

    # Build chunks
    chunks = [files[i:i + WORKER_CHUNK_SIZE]
              for i in range(0, len(files), WORKER_CHUNK_SIZE)]

    result_queue = mp.Queue(maxsize=50_000)

    # Start writer
    writer = mp.Process(
        target=writer_process,
        args=(DB_PATH, result_queue, len(files),
              skip_officers, skip_sched_i, skip_related),
    )
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

    # Print summary
    _print_summary(DB_PATH)


def _print_summary(db_path):
    con = sqlite3.connect(db_path)
    logging.info("─── 990/990EZ Detail Extraction Summary ───")

    for table in ("officers", "schedule_i_990", "related_orgs", "contractors"):
        try:
            count = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            logging.info(f"  {table}: {count:,} rows")
        except sqlite3.OperationalError:
            logging.info(f"  {table}: table not found")

    # Schedule I stats
    try:
        row = con.execute(
            "SELECT COUNT(*), COUNT(DISTINCT ein), SUM(cash_grant_amt) "
            "FROM schedule_i_990"
        ).fetchone()
        logging.info(f"  Schedule I 990: {row[0]:,} grants from {row[1]:,} "
                     f"filers, ${row[2]:,} total cash grants")
    except Exception:
        pass

    # Related orgs stats
    try:
        row = con.execute(
            "SELECT COUNT(*), COUNT(DISTINCT ein), COUNT(DISTINCT related_ein) "
            "FROM related_orgs"
        ).fetchone()
        logging.info(f"  Related orgs: {row[0]:,} relationships, "
                     f"{row[1]:,} filers, {row[2]:,} related EINs")
    except Exception:
        pass

    # Related orgs by section
    try:
        for section, n in con.execute(
            "SELECT section, COUNT(*) FROM related_orgs "
            "GROUP BY section ORDER BY COUNT(*) DESC"
        ):
            logging.info(f"    {section}: {n:,}")
    except Exception:
        pass

    con.close()


if __name__ == "__main__":
    main()
