# 990database

A comprehensive database of IRS Form 990 nonprofit filings: financial returns, foundation grants, officer compensation, DAF disbursements, investments, and more. Over 5.2 million filings covering 1.9 million organizations, extracted from the IRS bulk XML e-file archives. All from official government sources. All public domain.

Built by a human, [Claude](https://www.anthropic.com/claude) (Anthropic), and DJ Crabdaddy ([Claude Code](https://docs.anthropic.com/en/docs/claude-code)) 🦀

**Live instance**: https://data.datadawn.org/
**Explore pages**: https://data.datadawn.org/explore/
**Build command**: `bash scripts/update.sh`

## Data at a Glance

| Table | Records | Description |
|-------|---------|-------------|
| `returns` | 5,210,981 | 990/990-PF/990-EZ filings (tax years 2014–2025) |
| `grants` | 13,609,220 | 990-PF grants paid, future grants, expenditure responsibility |
| `officers` | 44,762,634 | Officers, directors, trustees, key employees |
| `schedule_i_990` | 6,393,046 | Schedule I grants (990/990-EZ filers) |
| `schedule_i_grants` | 1,272,399 | DAF and intermediary grant disbursements |
| `related_orgs` | 8,540,764 | Related organizations (Schedule R) |
| `capital_gains` | 22,774,992 | 990-PF capital gains/losses (Part IV) |
| `investments` | 5,228,704 | 990-PF investments (Part II) |
| `contributors` | 662,265 | Schedule B contributors (990-PF only) |
| `program_activities` | 576,397 | 990/990-EZ program service descriptions |
| `program_investments` | 314,487 | 990-PF program-related investments (Part IX-B) |
| `contractors` | 77,456 | Top 5 independent contractors |
| `top_employees` | 74,260 | Highest-compensated employees (990/990-EZ) |
| `bmf` | 1,935,635 | IRS Business Master File (NTEE codes, subsection, status) |

**Total**: ~114 million records across 14 tables.

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
- **`contractors`** — Top 5 independent contractors by compensation.
- **`top_employees`** — Highest-compensated employees on 990/990-EZ.

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

6. **Contributor records limited** — Only 662,265 contributor records exist because Schedule B data is only available for 990-PF filers. 990 and 990-EZ filers' Schedule B data is redacted in the public XML.

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

## License

This project is licensed under [Creative Commons Zero v1.0 Universal](LICENSE). All IRS-sourced data is in the public domain.

Built by a human, [Claude](https://www.anthropic.com/claude) (Anthropic), and DJ Crabdaddy ([Claude Code](https://docs.anthropic.com/en/docs/claude-code)) 🦀

A [DataDawn](https://datadawn.org) project.
