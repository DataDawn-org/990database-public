#!/usr/bin/env python3
"""
Extract Schedule I grants from 990 filers (DAFs, community foundations, etc.)
and load into schedule_i_grants table in 990data.db.

Schedule I = "Grants and Other Assistance to Organizations, Governments, and Individuals"
This is how 990 filers (public charities) report their grantmaking.
990-PF filers report grants in Part XV (already extracted into grants table).
"""

import xml.etree.ElementTree as ET
from pathlib import Path
import sqlite3
import sys
import os
import time

DB_PATH = str(Path(__file__).resolve().parent.parent / '990data.db')

# DAFs and major 990-filing grantmakers to extract
# Format: (ein, short_name)
TARGETS = [
    # Giants
    ('110303001', 'Fidelity Charitable'),
    ('311640316', 'Schwab Charitable'),
    ('232888152', 'Vanguard Charitable'),
    ('237825575', 'National Philanthropic Trust'),
    ('205205488', 'Silicon Valley Community Foundation'),
    # Financial institution DAFs
    ('311774905', 'GS Donor Advised Philanthropy Fund'),
    ('527082731', 'Morgan Stanley Global Impact'),
    ('046010342', 'Bank of America Charitable'),
    ('300748315', 'BNY Mellon Charitable'),
    ('593652538', 'Raymond James Charitable'),
    # Community foundation DAFs
    ('237174183', 'Jewish Communal Fund'),
    ('362167000', 'Chicago Community Trust'),
    ('431152398', 'Greater Kansas City Community Foundation'),
    ('133062214', 'New York Community Trust'),
    ('953510055', 'California Community Foundation'),
    # Tech/newer/ideological
    ('522166327', 'Donors Trust'),
    ('541934032', 'Donors Capital Fund'),
    ('510198509', 'Tides Foundation'),
    # Open Phil Action Fund
    ('812644663', 'Open Philanthropy Action Fund'),
    # Also catch the Fidelity 2023 filing under different EIN
    ('934792247', 'Fidelity Charitable (alt EIN)'),
]

def extract_schedule_i_grants(filepath, funder_ein, funder_name, tax_year):
    """Extract all Schedule I grants from a 990 XML file."""
    grants = []
    
    try:
        tree = ET.parse(filepath)
        root = tree.getroot()
    except Exception as e:
        print(f"  ERROR parsing {filepath}: {e}", file=sys.stderr)
        return grants
    
    # US grants: RecipientTable elements
    for elem in root.iter():
        tag = elem.tag.split('}')[-1] if '}' in elem.tag else elem.tag
        
        if tag == 'RecipientTable':
            recipient_name = ''
            recipient_ein = ''
            city = ''
            state = ''
            zip_code = ''
            amount = 0
            purpose = ''
            grant_type = 'us_org'
            
            for child in elem.iter():
                ctag = child.tag.split('}')[-1] if '}' in child.tag else child.tag
                text = (child.text or '').strip()
                
                if ctag == 'BusinessNameLine1Txt':
                    recipient_name = text
                elif ctag == 'BusinessNameLine2Txt' and text:
                    recipient_name += ' ' + text
                elif ctag == 'RecipientEIN':
                    recipient_ein = text
                elif ctag == 'CityNm':
                    city = text
                elif ctag == 'StateAbbreviationCd':
                    state = text
                elif ctag == 'ZIPCd':
                    zip_code = text
                elif ctag == 'CashGrantAmt':
                    try:
                        amount = int(text)
                    except:
                        amount = 0
                elif ctag == 'PurposeOfGrantTxt':
                    purpose = text
                elif ctag == 'NonCashAssistanceAmt':
                    # Some grants are noncash
                    pass
            
            if recipient_name and amount > 0:
                grants.append({
                    'funder_ein': funder_ein,
                    'funder_name': funder_name,
                    'tax_year': tax_year,
                    'grant_type': grant_type,
                    'recipient_name': recipient_name,
                    'recipient_ein': recipient_ein,
                    'recipient_city': city,
                    'recipient_state': state,
                    'recipient_zip': zip_code,
                    'recipient_country': None,
                    'amount': amount,
                    'purpose': purpose,
                    'source_file': filepath,
                })
        
        elif tag == 'GrantsToOrgOutsideUSGrp':
            region = ''
            amount = 0
            purpose = ''
            
            for child in elem.iter():
                ctag = child.tag.split('}')[-1] if '}' in child.tag else child.tag
                text = (child.text or '').strip()
                
                if ctag == 'RegionTxt':
                    region = text
                elif ctag == 'CashGrantAmt':
                    try:
                        amount = int(text)
                    except:
                        amount = 0
                elif ctag == 'PurposeOfGrantTxt':
                    purpose = text
            
            if amount > 0:
                grants.append({
                    'funder_ein': funder_ein,
                    'funder_name': funder_name,
                    'tax_year': tax_year,
                    'grant_type': 'foreign_org',
                    'recipient_name': region,  # No org name for foreign grants
                    'recipient_ein': '',
                    'recipient_city': '',
                    'recipient_state': '',
                    'recipient_zip': '',
                    'recipient_country': region,
                    'amount': amount,
                    'purpose': purpose,
                    'source_file': filepath,
                })
    
    return grants


def main():
    db = sqlite3.connect(DB_PATH)
    db.execute("PRAGMA journal_mode=WAL")
    
    # Create table
    db.execute("DROP TABLE IF EXISTS schedule_i_grants")
    db.execute("""
        CREATE TABLE schedule_i_grants (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            funder_ein      TEXT NOT NULL,
            funder_name     TEXT,
            tax_year        INTEGER,
            grant_type      TEXT,  -- 'us_org' or 'foreign_org'
            recipient_name  TEXT,
            recipient_ein   TEXT,
            recipient_city  TEXT,
            recipient_state TEXT,
            recipient_zip   TEXT,
            recipient_country TEXT,
            amount          INTEGER,
            purpose         TEXT,
            source_file     TEXT
        )
    """)
    
    # Find all filings for target orgs
    target_eins = [t[0] for t in TARGETS]
    placeholders = ','.join(['?' for _ in target_eins])
    
    filings = db.execute(f"""
        SELECT ein, org_name, tax_year, source_file
        FROM returns
        WHERE ein IN ({placeholders})
          AND return_type = '990'
        ORDER BY ein, tax_year
    """, target_eins).fetchall()
    
    print(f"Found {len(filings)} filings to process for {len(TARGETS)} target orgs")
    
    # Build EIN -> short name lookup
    ein_to_name = {t[0]: t[1] for t in TARGETS}
    
    total_grants = 0
    total_amount = 0
    batch = []
    batch_size = 10000
    
    for i, (ein, org_name, tax_year, source_file) in enumerate(filings):
        short_name = ein_to_name.get(ein, org_name)
        
        if not os.path.exists(source_file):
            print(f"  SKIP {short_name} {tax_year}: file not found")
            continue
        
        t0 = time.time()
        grants = extract_schedule_i_grants(source_file, ein, short_name, tax_year)
        elapsed = time.time() - t0
        
        file_amount = sum(g['amount'] for g in grants)
        print(f"  [{i+1}/{len(filings)}] {short_name} {tax_year}: "
              f"{len(grants):,} grants, ${file_amount:,.0f} ({elapsed:.1f}s)")
        
        total_grants += len(grants)
        total_amount += file_amount
        batch.extend(grants)
        
        if len(batch) >= batch_size:
            _insert_batch(db, batch)
            batch = []
    
    if batch:
        _insert_batch(db, batch)
    
    # Create indexes
    print("Creating indexes...")
    db.execute("CREATE INDEX idx_si_funder ON schedule_i_grants(funder_ein)")
    db.execute("CREATE INDEX idx_si_recipient ON schedule_i_grants(recipient_name)")
    db.execute("CREATE INDEX idx_si_recipient_ein ON schedule_i_grants(recipient_ein)")
    db.execute("CREATE INDEX idx_si_year ON schedule_i_grants(tax_year)")
    db.execute("CREATE INDEX idx_si_amount ON schedule_i_grants(amount)")
    
    db.commit()
    
    print(f"\nDone! {total_grants:,} grants, ${total_amount:,.0f} total")
    print(f"Table: schedule_i_grants")
    
    # Quick summary
    rows = db.execute("""
        SELECT funder_name, COUNT(*) as grants, SUM(amount) as total,
               MIN(tax_year) as min_yr, MAX(tax_year) as max_yr
        FROM schedule_i_grants
        GROUP BY funder_ein
        ORDER BY total DESC
    """).fetchall()
    
    print(f"\n{'Funder':<45} {'Grants':>8} {'Total':>16} {'Years':>10}")
    print("-" * 85)
    for name, grants, total, min_yr, max_yr in rows:
        print(f"{name:<45} {grants:>8,} ${total:>14,} {min_yr}-{max_yr}")
    
    db.close()


def _insert_batch(db, batch):
    db.executemany("""
        INSERT INTO schedule_i_grants 
        (funder_ein, funder_name, tax_year, grant_type, recipient_name,
         recipient_ein, recipient_city, recipient_state, recipient_zip,
         recipient_country, amount, purpose, source_file)
        VALUES (:funder_ein, :funder_name, :tax_year, :grant_type, :recipient_name,
                :recipient_ein, :recipient_city, :recipient_state, :recipient_zip,
                :recipient_country, :amount, :purpose, :source_file)
    """, batch)
    db.commit()


if __name__ == '__main__':
    main()
