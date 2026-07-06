-- 990data.db — Full Schema Dump
-- Dumped from live database, March 2026
--
-- IMPORTANT: This schema reflects SQLite as-built.
-- The grants table uses AUTOINCREMENT (should be SERIAL/GENERATED in PG).

-- ============================================================
-- CORE DATA TABLES (extracted from IRS 990 XML)
-- ============================================================

CREATE TABLE returns (
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
    contributions_received    INTEGER,
    dividends                 INTEGER,
    interest_income           INTEGER,
    net_gain_sale_assets      INTEGER,
    contributions_paid        INTEGER,
    fmv_assets_eoy            INTEGER,
    net_assets_eoy            INTEGER,
    grants_payable_eoy        INTEGER,
    qualifying_distributions  INTEGER,
    distributable_amount      INTEGER,
    min_investment_return     INTEGER,
    excess_distribution_cyov  INTEGER
);
-- idx_ein removed 2026-04-11: subset of idx_returns_ein_type and idx_returns_ein_year_oid
CREATE INDEX idx_return_type        ON returns(return_type);
CREATE INDEX idx_tax_year           ON returns(tax_year);
CREATE INDEX idx_returns_ein_type   ON returns(ein, return_type);
CREATE INDEX idx_returns_ein_year_oid ON returns(ein, tax_year DESC, object_id DESC);

CREATE TABLE grants (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id             TEXT NOT NULL,
    ein                   TEXT,
    grant_type            TEXT,           -- 'paid', 'future', 'exp_responsibility'
    recipient_name        TEXT,
    recipient_city        TEXT,
    recipient_state       TEXT,
    recipient_country     TEXT,           -- NULL for US
    recipient_zip         TEXT,
    relationship          TEXT,
    foundation_status     TEXT,
    purpose               TEXT,
    amount                INTEGER,
    grant_date            TEXT,           -- exp_responsibility only
    expended_amount       INTEGER         -- exp_responsibility only
);
-- idx_grants_oid removed 2026-04-11: subset of idx_grants_oid_type (added in update.sh)
-- idx_grants_ein removed 2026-04-11: subset of idx_grants_ein_type and idx_grants_ein_recip
CREATE INDEX idx_grants_type         ON grants(grant_type);
-- idx_grants_recip_upper removed 2026-04-11: subset of idx_grants_recip_type (added in update.sh)
CREATE INDEX idx_grants_ein_type     ON grants(ein, grant_type);

-- officers — 5-column comp schema per Bug #3 fix (decisions_log §64):
--   reportable_comp_filing_org   = W-2 from filing org (ALL forms)
--   reportable_comp_related_org  = W-2 from related orgs (Form 990 ONLY)
--   other_compensation           = IRS "other comp" lump (Form 990 ONLY)
--   benefits                     = Employee benefit program (990-EZ + 990-PF ONLY)
--   expense_account              = Expense account + allowances (990-EZ + 990-PF ONLY)
-- `compensation` is legacy from pre-Bug-#3 schema; Phase 2 of the 2026-05-22
-- migration drops it. Kept here for backwards-compat with existing DBs.
CREATE TABLE officers (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id                   TEXT NOT NULL,
    ein                         TEXT,
    person_name                 TEXT,
    title                       TEXT,
    avg_hours_per_week          REAL,
    compensation                INTEGER,
    benefits                    INTEGER,
    expense_account             INTEGER,
    reportable_comp_filing_org  INTEGER,
    reportable_comp_related_org INTEGER,
    other_compensation          INTEGER,
    -- Part VII Sec A role checkboxes (added 2026-07): check-all-that-apply, read
    -- independently per box. Form 990 rows: 1/0 (0 = box unchecked). 990-EZ/PF
    -- rows: all six NULL (no Section-A role structure on those forms).
    is_highest_compensated_employee INTEGER,
    is_officer                  INTEGER,
    is_individual_trustee       INTEGER,
    is_institutional_trustee    INTEGER,
    is_key_employee             INTEGER,
    is_former                   INTEGER
);
CREATE INDEX idx_officers_oid ON officers(object_id);
CREATE INDEX idx_officers_ein ON officers(ein);

CREATE TABLE contributors (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id             TEXT NOT NULL,
    ein                   TEXT,
    contributor_name      TEXT,
    city                  TEXT,
    state                 TEXT,
    zip                   TEXT,
    amount                INTEGER,
    contributor_type      TEXT             -- 'person' or 'business'
);
CREATE INDEX idx_contributors_oid ON contributors(object_id);
CREATE INDEX idx_contributors_ein ON contributors(ein);

CREATE TABLE capital_gains (
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
CREATE INDEX idx_capgains_oid ON capital_gains(object_id);
CREATE INDEX idx_capgains_ein ON capital_gains(ein);

CREATE TABLE investments (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id             TEXT NOT NULL,
    ein                   TEXT,
    investment_type       TEXT,           -- 'corp_bond', 'other', 'govt', 'land'
    description           TEXT,
    book_value            INTEGER,
    fmv                   INTEGER,
    cost_basis            INTEGER
);
CREATE INDEX idx_investments_oid ON investments(object_id);
CREATE INDEX idx_investments_ein ON investments(ein);

CREATE TABLE program_activities (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id             TEXT NOT NULL,
    ein                   TEXT,
    activity_num          INTEGER,
    description           TEXT,
    expenses              INTEGER
);
CREATE INDEX idx_progact_oid ON program_activities(object_id);
CREATE INDEX idx_progact_ein ON program_activities(ein);

CREATE TABLE program_investments (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id             TEXT NOT NULL,
    ein                   TEXT,
    description           TEXT,
    amount                INTEGER
);
CREATE INDEX idx_proginv_oid ON program_investments(object_id);
CREATE INDEX idx_proginv_ein ON program_investments(ein);

CREATE TABLE contractors (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id             TEXT NOT NULL,
    ein                   TEXT,
    contractor_name       TEXT,
    city                  TEXT,
    state                 TEXT,
    service_type          TEXT,
    compensation          INTEGER
);
CREATE INDEX idx_contractors_oid ON contractors(object_id);
CREATE INDEX idx_contractors_ein ON contractors(ein);

-- top_employees — same 5-column comp schema as officers (Bug #3, §64).
-- Currently 990-PF only; Bug #1 (separate fix) will add Form 990 rows.
CREATE TABLE top_employees (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id                   TEXT NOT NULL,
    ein                         TEXT,
    person_name                 TEXT,
    title                       TEXT,
    avg_hours_per_week          REAL,
    compensation                INTEGER,
    benefits                    INTEGER,
    expense_account             INTEGER,
    reportable_comp_filing_org  INTEGER,
    reportable_comp_related_org INTEGER,
    other_compensation          INTEGER
);
CREATE INDEX idx_topempl_oid ON top_employees(object_id);
CREATE INDEX idx_topempl_ein ON top_employees(ein);

-- ============================================================
-- SCHEDULE I TABLES (extracted from 990 Schedule I / DAF)
-- ============================================================

CREATE TABLE schedule_i_990 (
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
CREATE INDEX idx_si990_oid       ON schedule_i_990(object_id);
CREATE INDEX idx_si990_ein       ON schedule_i_990(ein);
CREATE INDEX idx_si990_recip_ein ON schedule_i_990(recipient_ein);

CREATE TABLE schedule_i_grants (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    funder_ein            TEXT NOT NULL,
    funder_name           TEXT,
    tax_year              INTEGER,
    grant_type            TEXT,           -- 'us_org' or 'foreign_org'
    recipient_name        TEXT,
    recipient_ein         TEXT,
    recipient_city        TEXT,
    recipient_state       TEXT,
    recipient_zip         TEXT,
    recipient_country     TEXT,
    amount                INTEGER,
    purpose               TEXT,
    source_file           TEXT
);
CREATE INDEX idx_si_funder          ON schedule_i_grants(funder_ein);
CREATE INDEX idx_si_recipient       ON schedule_i_grants(recipient_name);
CREATE INDEX idx_si_recipient_ein   ON schedule_i_grants(recipient_ein);
CREATE INDEX idx_si_year            ON schedule_i_grants(tax_year);
CREATE INDEX idx_si_amount          ON schedule_i_grants(amount);
CREATE INDEX idx_si_recipient_nocase ON schedule_i_grants(recipient_name COLLATE NOCASE);

-- ============================================================
-- REFERENCE TABLES
-- ============================================================

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
CREATE INDEX idx_bmf_ntee       ON bmf(ntee_cd);
CREATE INDEX idx_bmf_name_upper ON bmf(UPPER(name));
