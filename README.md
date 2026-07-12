# 990database

A comprehensive database of IRS Form 990 nonprofit filings: financial returns, foundation grants, officer compensation, DAF disbursements, investments, and more. Over 5.2 million filings covering 1.9 million organizations, extracted from the IRS bulk XML e-file archives. All from official government sources. All public domain.

Built by a human, [Claude](https://www.anthropic.com/claude) (Anthropic), and DJ Crabdaddy ([Claude Code](https://docs.anthropic.com/en/docs/claude-code)) 🦀

**Live instance**: https://data.datadawn.org/
**Explore pages**: https://data.datadawn.org/explore/
**Build command**: `bash scripts/update.sh`

## Data at a Glance

| Table | Records | Description |
|-------|---------|-------------|
| `returns` | 5,429,970 | 990/990-PF/990-EZ/990-T filings (effectively complete TY2016–2025; TY2014–2015 partial and non-representative — see /api/coverage) |
| `grants` | 13,609,220 | 990-PF grants paid, future grants, expenditure responsibility |
| `officers` | 44,521,930 | Officers, directors, trustees, key employees (+ six role flags, 2026-07) |
| `schedule_i_990` | 6,393,046 | Schedule I grants (990/990-EZ filers) |
| `schedule_i_grants` | 1,272,399 | DAF and intermediary grant disbursements |
| `related_orgs` | 8,540,764 | Related organizations (Schedule R) |
| `capital_gains` | 15,580,627 | 990-PF capital gains/losses (Part IV) |
| `investments` | 4,897,384 | 990-PF investments (Part II) |
| `contributors` | 506,838 | Schedule B contributors (990-PF only) |
| `program_activities` | 374,098 | 990/990-EZ program service descriptions |
| `program_investments` | 211,210 | 990-PF program-related investments (Part IX-B) |
| `contractors` | 1,058,308 | Top 5 independent contractors (Form 990 + 990-PF, 2026-07) |
| `top_employees` | 54,988 | Highest-compensated employees (990-PF only) |
| `bmf` | 1,935,635 | IRS Business Master File (NTEE codes, subsection, status) |

**Total**: ~102 million records across 14 tables.

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

The update script (`scripts/update.sh`) automates the full pipeline:

```bash
bash scripts/update.sh              # full update
bash scripts/update.sh --dry-run    # preview what would happen
```

### Manual execution order

Run from the project root:

```
1. bash scripts/update.sh           # Downloads new IRS ZIPs, extracts, parses
```

Or run individual extraction scripts:

```
1. python3 scripts/extract_990.py            # Core 990 fields → returns table (~2 hrs)
2. python3 scripts/extract_990pf_detail.py   # 990-PF detail tables (grants, officers, etc.)
3. python3 scripts/extract_990_detail.py     # 990/990-EZ detail tables
4. python3 scripts/extract_schedule_i.py     # Schedule I DAF/intermediary grants
5. python3 scripts/backfill_ntee.py          # Backfill NTEE codes from BMF
```

Each script is idempotent — it reads all XML files in the year directories and uses `INSERT OR IGNORE` to skip already-processed filings.

### How the IRS data is organized

The IRS publishes e-filed 990s at `https://apps.irs.gov/pub/epostcard/990/xml/{YEAR}/`. Each year directory contains multiple ZIP batches (`2024_TEOS_XML_01A.zip`, etc.), each holding thousands of XML files. The update script:

1. Checks the IRS site for new batches not yet downloaded
2. Downloads and extracts new ZIPs
3. Runs extraction scripts over the new XML files
4. Builds a public database copy (drops any non-core tables)
5. Optionally uploads to a Datasette server

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

2. **E-file only** — Paper-filed returns are not included. E-filing became mandatory for most large nonprofits in 2020, but smaller organizations and some types still file on paper.

3. **No 990-T** — IRS Form 990-T (Exempt Organization Business Income Tax Return) uses a different XML schema and is not extracted. Approximately 95,000 990-T filings are present in the raw XML but are skipped during parsing.

4. **NTEE mismatch** — BMF NTEE codes are assigned at organization creation and rarely updated. Some organizations have outdated or incorrect NTEE codes that don't reflect their current activities.

5. **Opaque grantmaking** — Community foundations and DAFs often report grants with generic recipient names (e.g., "various charities") or aggregate amounts. Approximately 6,100 such records exist in the `schedule_i_grants` table.

6. **Contributor records limited** — Only 506,838 contributor records exist because Schedule B data is only available for 990-PF filers. 990 and 990-EZ filers' Schedule B data is redacted in the public XML.

7. **2021 filing gap** — Tax year 2021 has approximately 34% fewer filings than adjacent years, likely due to IRS processing backlogs during that period.

---

## Deployment

The update script can optionally deploy to a Datasette instance. Set `REMOTE_HOST` in `scripts/update.sh` to your server's address. The deployment step:

1. Copies the full database and drops non-core tables
2. Builds FTS5 full-text search indexes (org names, grant recipients)
3. Vacuums to reclaim space
4. Uploads the public database via `scp`
5. Deploys templates and static assets
6. Restarts Datasette

---

## Mirror-sync conventions

This repository mirrors the maintainer's working scripts. Synced files are byte-identical to source, with one deliberate exception: **`scripts/extract_990.py` carries three portability adaptations that must survive every sync** — do not "correct" them back to absolute paths:

1. Docstring paths are relative (`./{2019..2026}`, `./990data.db`), not the maintainer's local layout.
2. `from pathlib import Path` is added to the imports.
3. `BASE_DIR = str(Path(__file__).resolve().parent.parent)` replaces the hard-coded local directory.

Additionally, **`scripts/update.sh` and the two validation harnesses (`scripts/parser_harness.py`, `scripts/test_monthly_contractor_writer.py`) carry identity/infrastructure sanitization that must survive every sync**: server address → `user@YOUR_SERVER_IP`, backup bucket/remote → `your-b2-bucket`/`b2:`, the maintainer's home path → `$HOME`, provider names in comments genericized, and personal-name attributions in comments → "maintainer". The scripts are otherwise byte-identical to source, with two functional exceptions:

- `scripts/test_monthly_contractor_writer.py` carries a `DATA_BASE` portability adaptation (same class as extract_990.py's `BASE_DIR`): its two pinned witness XMLs resolve to the IRS year dirs at the **repo root** (`{2019,2020}/download990xml_*/...`), one level above `scripts/`. Download those IRS batches before running it; verified green (11/11 proofs) in this layout.
- `scripts/parser_harness.py`'s **baseline gate** (`python3 parser_harness.py <db>`, what update.sh invokes) is fully functional here. Its separate *promotion/witness* path reads `witness_fixtures_990.json`, a maintainer-side human-attestation record that is deliberately **not mirrored** (it attests who verified what; republishing it rewritten would blur exactly that provenance) — without it that path refuses loudly (fail-closed RED), which is the designed behavior, not a bug.

Any other divergence between this repo and the source scripts is drift, not convention, and should be closed by a sync PR.

---

## License

This project is licensed under [Creative Commons Zero v1.0 Universal](LICENSE). All IRS-sourced data is in the public domain.

Built by a human, [Claude](https://www.anthropic.com/claude) (Anthropic), and DJ Crabdaddy ([Claude Code](https://docs.anthropic.com/en/docs/claude-code)) 🦀

A [DataDawn](https://datadawn.org) project.
