# 990database

A comprehensive database of IRS Form 990 nonprofit filings: financial returns, foundation grants, officer compensation, DAF disbursements, investments, and more. Over 5.2 million filings covering 1.9 million organizations, extracted from the IRS bulk XML e-file archives. All from official government sources. All public domain.

Built by a human, [Claude](https://www.anthropic.com/claude) (Anthropic), and DJ Crabdaddy ([Claude Code](https://docs.anthropic.com/en/docs/claude-code)) 🦀

**Live instance**: https://data.datadawn.org/
**Explore pages**: https://data.datadawn.org/explore/
**Build**: run the scripts in [Pipeline](#pipeline), in order

## Data at a Glance

| Table | Records | Description |
|-------|---------|-------------|
| `returns` | 5,595,912 | Every 990/990-PF/990-EZ/990-T e-file XML record the IRS has released — no gaps within that source. **Not** every nonprofit (see Known Limitation 2): the smallest orgs file Form 990-N and are not carried here; pre-2021 reflects e-file adoption, not sector growth. Per-year detail at /api/coverage |
| `grants` | 14,402,178 | 990-PF grants paid, future grants, expenditure responsibility |
| `officers` | 45,772,456 | Officers, directors, trustees, key employees (+ six role flags, 2026-07) |
| `schedule_i_990` | 7,243,504 | Schedule I grants (990/990-EZ filers) |
| `schedule_i_grants` | 1,504,940 | DAF and intermediary grant disbursements |
| `related_orgs` | 9,634,856 | Related organizations (Schedule R) |
| `capital_gains` | 18,139,997 | 990-PF capital gains/losses (Part IV) |
| `investments` | 5,334,190 | 990-PF investments (Part II) |
| `contributors` | 534,603 | Schedule B contributors (990-PF only) |
| `program_activities` | 399,569 | 990/990-EZ program service descriptions |
| `program_investments` | 225,908 | 990-PF program-related investments (Part IX-B) |
| `contractors` | 1,096,945 | Top 5 independent contractors (Form 990 + 990-PF, 2026-07) |
| `top_employees` | 56,603 | Highest-compensated employees (990-PF only) |
| `bmf` | 1,957,340 | IRS Business Master File (NTEE codes, subsection, status) — IRS file as of 2026-09-05; vintage in `bmf_source_meta` |
| `bmf_former` | 60,443 | Organizations in a previous IRS file and absent from the current one (last-known section/NTEE, `last_seen_asof`, `dropped_asof`) |
| `returns_ntee_source` | 593,626 | Per-EIN provenance of `returns.ntee_code` (`bmf` = current file; `prior_bmf_kept` = org left the file, code kept from the earlier one) |
| `bmf_source_meta` | 1 | Provenance of `bmf`: load time, as-of date, source-file manifest, row count, content hash |

**Total**: ~112 million records across 14 tables.

---

## Source

**IRS e-File Bulk XML**: https://apps.irs.gov/pub/epostcard/990/xml/

The IRS publishes machine-readable 990 filings as ZIP archives of XML files, organized by year and batch (e.g., `2024_TEOS_XML_01A.zip`). This project downloads, parses, and loads those XMLs into a SQLite database.

**BMF (Business Master File)**: https://www.irs.gov/charities-non-profits/exempt-organizations-business-master-file-extract-eo-bmf

Monthly extract of all tax-exempt organizations with NTEE codes, ruling dates, and financial summary codes.

**License**: All IRS data is public domain. No copyright restrictions.

---

## Prerequisites

- **Python 3** with `lxml` (`pip install lxml`)
- **sqlite3** CLI (pre-installed on most systems)
- **curl** and **unzip** for downloading IRS archives
- ~200 GB disk space for raw XML + database

---

## Pipeline

The maintainer runs the full monthly pipeline — download, parse, build the public copy, deploy — with an orchestrator script that is not part of this repository. The scripts here are the pipeline's download and parsing steps and run on their own. Run them from the project root, in this order:

```
1. bash scripts/backfill_download.sh           # download and extract the IRS XML batches not yet done
2. python3 scripts/extract_990.py              # core fields -> returns table (also creates canonical_returns)
3. python3 scripts/extract_990pf_detail.py     # 990-PF detail tables (grants, officers, etc.)
4. python3 scripts/extract_990_detail.py       # 990/990-EZ detail tables
5. python3 scripts/extract_schedule_i.py       # Schedule I DAF/intermediary grants
6. python3 scripts/backfill_ntee.py            # load the BMF and backfill NTEE codes
```

`scripts/backfill_download.sh` writes to the directory set in `PROJECT_DIR` at its top, which names the maintainer's layout; set it to yours.

Re-running the extraction scripts is safe:
- `extract_990.py` walks the XML files in the year directories and uses `INSERT OR IGNORE`, so filings already loaded are skipped.
- `extract_990pf_detail.py` and `extract_990_detail.py` read the filings already indexed in `returns` and skip any `object_id` already present in their tables.
- `extract_schedule_i.py` rebuilds `schedule_i_grants` from scratch on every run.

### How the IRS data is organized

The IRS publishes e-filed 990s at `https://apps.irs.gov/pub/epostcard/990/xml/{YEAR}/`. Each year directory contains multiple ZIP batches (`2024_TEOS_XML_01A.zip`, etc.), each holding thousands of XML files. `scripts/backfill_download.sh` downloads every batch not yet marked done in `.extracted/` and extracts its XML files into per-year directories; the extraction scripts then parse them.

---

## Schema

See `schema.sql` for the full DDL. The 14 core tables are:

### Core filing data
- **`returns`** — One row per filing. EIN, org name, financials (revenue, expenses, assets), return type (990/990-PF/990-EZ), tax year.
- **`grants`** — 990-PF grants: paid grants, future grants, and expenditure responsibility grants. Recipient name, city, state, amount, purpose.
- **`officers`** — Officers, directors, trustees, and key employees with compensation.
- **`contributors`** — Schedule B contributors (990-PF filers only). Name, location, amount.
- **`schedule_i_990`** — Schedule I grants reported on 990/990-EZ (non-foundation grantmakers).
- **`schedule_i_grants`** — DAF and intermediary grant disbursements extracted from Schedule I of 990-PF filers.
- **`related_orgs`** — Schedule R related organizations.
- **`capital_gains`** — 990-PF Part IV capital gains/losses.
- **`investments`** — 990-PF Part II investments (corporate bonds, government securities, land, other).
- **`program_activities`** — 990/990-EZ program service accomplishments.
- **`program_investments`** — 990-PF Part IX-B program-related investments.
- **`contractors`** — Five highest-paid independent contractors by compensation, parsed from **Form 990 AND 990-PF** filings (990 Part VII Section B live since 2026-07). An empty result means the filer reported no contractors above the $100K threshold.
- **`top_employees`** — Highest-compensated employees (other than officers). Covers **Form 990-PF only by design**; Form-990 highest-compensated employees are not duplicated here — they appear in `officers` flagged `is_highest_compensated_employee=1`.

### Reference data
- **`bmf`** — IRS Business Master File: NTEE codes, subsection, ruling dates, financial summary codes.

### Relationships

All detail tables link to `returns` via `object_id` (the IRS-assigned filing identifier). The `ein` column links filings for the same organization across years. The `bmf` table provides NTEE classification and other reference data, keyed by `ein`.

---

## Known Limitations

1. **Filing lag** — IRS publishes e-filed returns with a delay. Tax year 2024 filings are still accumulating (~140K–180K pending as of early 2026). Tax year 2025 has very few filings.

2. **Coverage is stated relative to IRS-released electronic records — not the whole nonprofit sector.** This database holds every 990/990-EZ/990-PF/990-T e-file XML the IRS has released, with no gaps within that source. But two limits separate "every electronic record" from "every nonprofit":
   - **The smallest organizations are never here.** Organizations with gross receipts of $50,000 or less meet their annual obligation with **Form 990-N**, an eight-field electronic postcard the IRS publishes separately and this database does not carry. At least **827,000** currently-filing organizations appear nowhere in this database, in any year — roughly the same size as the ~669,984 organizations held for tax year 2022. *Counting organizations:* this database sees roughly half the annually-filing exempt sector; don't use it for organization counts. *Analyzing money:* negligible — Form 990-N reports no financial data at all (no revenue, no expenses, no assets), and its filers are by definition under $50,000 in gross receipts. Every organization that reports financial detail to the IRS electronically is here.
   - **Earlier years capture only the organizations that chose to e-file.** E-filing was voluntary until the Taxpayer First Act made it mandatory — Forms 990 and 990-PF from tax year 2020, Form 990-EZ from 2021. Before then the database holds a rising share of filers, skewed toward larger ones: in tax year 2016 roughly 71% of established filing organizations appear, versus ~87% of those above $10M in revenue. From tax year 2021 forward, coverage of organizations required to file a full return is essentially universal. Don't compute year-over-year trends across the pre-2021 span; the apparent 2016–2021 growth in filings is mostly e-filing adoption, not sector growth. (Paper-filed returns don't exist as e-file artifacts, so they can't be here.)

3. **990-T is e-file-era only.** Form 990-T (Exempt Organization Business Income Tax Return) **is** carried — the database holds ~110,980 of them — but only from **tax year 2020 onward**, because the IRS did not offer 990-T e-filing before then (it became mandatory for tax years ending December 2020 and later). There are no 990-T records for TY2016–2019.

4. **NTEE mismatch** — BMF NTEE codes are assigned at organization creation and rarely updated. Some organizations have outdated or incorrect NTEE codes that don't reflect their current activities.

5. **Opaque grantmaking** — Community foundations and DAFs often report grants with generic recipient names (e.g., "various charities") or aggregate amounts. Approximately 6,100 such records exist in the `schedule_i_grants` table.

6. **Contributor records limited** — Only 534,603 contributor records exist because Schedule B data is only available for 990-PF filers. 990 and 990-EZ filers' Schedule B data is redacted in the public XML.

---

## Deployment

data.datadawn.org is deployed by the maintainer's monthly update script, which is not part of this repository. Its deploy step:

- copies the database and drops every table that is not on its list of published tables;
- builds the FTS5 full-text search indexes;
- vacuums the copy;
- uploads it to the Datasette server, with the templates and static assets;
- restarts Datasette.

---

## Sync cadence

This repository is a lagging copy of the published 990 pipeline files, synced from the maintainer's working tree in deliberate batches rather than continuously. No published file trails its sanitized source by more than 60 days. For live data, use https://data.datadawn.org.

## Mirror-sync conventions

This repository mirrors the maintainer's working scripts and pages. Each synced file is built from its source by a sanitizer (maintainer-side tooling, not part of this repository) and is otherwise byte-identical to that source. The sanitizer applies these changes on every sync, and they must survive every sync:

- **In every synced file:** the server address → `YOUR_SERVER_IP`, the maintainer's home path → `$HOME`, and personal-name attributions → "maintainer" ("MAINTAINER" where the source is in capitals).
- **`scripts/extract_990.py`** carries three portability adaptations — do not "correct" them back to absolute paths:
  1. Docstring paths are relative (`./{2019..2026}`, `./990data.db`), not the maintainer's local layout.
  2. `from pathlib import Path` is added to the imports.
  3. `BASE_DIR = str(Path(__file__).resolve().parent.parent)` replaces the hard-coded local directory.
- **`scripts/test_monthly_contractor_writer.py`** carries a `DATA_BASE` portability adaptation (same class as `BASE_DIR`): its two pinned witness XMLs resolve to the IRS year dirs at the **repo root** (`{2019,2020}/download990xml_*/...`), one level above `scripts/`. Download those IRS batches before running it.

The sanitizer refuses to publish a file that still carries a backup bucket or remote name, a hosting-provider name, or any other identity marker, so no synced file carries one. `.gitignore` is this repository's own and is not synced.

One functional difference follows from what is not mirrored: `scripts/parser_harness.py`'s **baseline gate** (`python3 parser_harness.py <db>`, what the maintainer's monthly update invokes) is fully functional here, but its separate *promotion/witness* path reads `witness_fixtures_990.json`, a maintainer-side human-attestation record that is deliberately **not mirrored** (it attests who verified what; republishing it rewritten would blur exactly that provenance) — without it that path refuses loudly (fail-closed RED), which is the designed behavior, not a bug.

Files are synced in batches (see Sync cadence). Between batches, a difference between this repository and the sanitized source is lag; any other divergence is drift, closed by the next sync.

---

## License

This project is licensed under [Creative Commons Zero v1.0 Universal](LICENSE). All IRS-sourced data is in the public domain.

Built by a human, [Claude](https://www.anthropic.com/claude) (Anthropic), and DJ Crabdaddy ([Claude Code](https://docs.anthropic.com/en/docs/claude-code)) 🦀

A [DataDawn](https://datadawn.org) project.
