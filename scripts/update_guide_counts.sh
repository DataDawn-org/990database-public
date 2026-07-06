#!/usr/bin/env bash
#
# update_guide_counts.sh — Update row counts in DATABASE_GUIDE.md and CLAUDE.md
#                          to match the current state of 990data.db
#
# Usage:
#   ./update_guide_counts.sh              # update both files
#   ./update_guide_counts.sh --dry-run    # show what would change
#
set -euo pipefail

PROJECT_DIR="/mnt/data/datadawn/990project"
DB="$PROJECT_DIR/990data.db"
GUIDE="$PROJECT_DIR/DATABASE_GUIDE.md"
CLAUDE_MD="$PROJECT_DIR/CLAUDE.md"
DRY_RUN=0

for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
    esac
done

[[ -f "$DB" ]] || { echo "ERROR: Database not found: $DB"; exit 1; }

# Get current row counts for all tables
declare -A COUNTS
while IFS='|' read -r table count; do
    COUNTS["$table"]="$count"
done < <(sqlite3 "$DB" "
    SELECT name, 0 FROM sqlite_master
    WHERE type='table' AND name NOT LIKE 'sqlite_%'
    ORDER BY name;
" 2>/dev/null)

# Now get actual counts
for table in "${!COUNTS[@]}"; do
    cnt=$(sqlite3 "$DB" "SELECT COUNT(*) FROM \"$table\"" 2>/dev/null || echo "0")
    COUNTS["$table"]="$cnt"
done

# Format number with commas
format_num() {
    printf "%'d" "$1"
}

# Get DB size
DB_SIZE_GB=$(stat --format="%s" "$DB" 2>/dev/null || stat -f "%z" "$DB")
DB_SIZE_GB=$(echo "scale=0; $DB_SIZE_GB / 1073741824" | bc)

# Get total returns count
TOTAL_RETURNS="${COUNTS[returns]:-0}"

# Determine tax year range
TAX_YEAR_RANGE=$(sqlite3 "$DB" "SELECT MIN(tax_year) || '–' || MAX(tax_year) FROM returns WHERE tax_year IS NOT NULL")

echo "Current database stats:"
echo "  Size: ~${DB_SIZE_GB} GB"
echo "  Returns: $(format_num "$TOTAL_RETURNS")"
echo "  Tax years: $TAX_YEAR_RANGE"
echo ""

# Update DATABASE_GUIDE.md
if [[ -f "$GUIDE" ]]; then
    echo "Updating $GUIDE..."
    cp "$GUIDE" "${GUIDE}.bak"
    TMPFILE=$(mktemp)
    cp "$GUIDE" "$TMPFILE"

    # Update header line: **Source data**: X IRS Form 990 XML files
    sed -i "s/\*\*Source data\*\*: [0-9,]* IRS Form 990 XML files (tax years [0-9–]*)/\*\*Source data\*\*: $(format_num "$TOTAL_RETURNS") IRS Form 990 XML files (tax years $TAX_YEAR_RANGE)/" "$TMPFILE"

    # Update header line: (~X GB,
    sed -i "s/(~[0-9]* GB, SQLite WAL mode)/(~${DB_SIZE_GB} GB, SQLite WAL mode)/" "$TMPFILE"

    # Update each table's row count: ### `table_name` — X rows
    for table in "${!COUNTS[@]}"; do
        count="${COUNTS[$table]}"
        formatted=$(format_num "$count")
        # Match both ### and #### headers
        sed -i "s/\`${table}\` — [0-9,]* rows/\`${table}\` — ${formatted} rows/g" "$TMPFILE"
    done

    if [[ $DRY_RUN -eq 1 ]]; then
        echo "[DRY-RUN] Changes that would be made to DATABASE_GUIDE.md:"
        diff "$GUIDE" "$TMPFILE" || true
        rm -f "$TMPFILE" "${GUIDE}.bak"
    else
        mv "$TMPFILE" "$GUIDE"
        rm -f "${GUIDE}.bak"
        echo "  DATABASE_GUIDE.md updated"
    fi
fi

# Update CLAUDE.md
if [[ -f "$CLAUDE_MD" ]]; then
    echo "Updating $CLAUDE_MD..."
    cp "$CLAUDE_MD" "${CLAUDE_MD}.bak"
    TMPFILE=$(mktemp)
    cp "$CLAUDE_MD" "$TMPFILE"

    # Update header line: **Source data**: X IRS Form 990 XML files
    sed -i "s/\*\*Source data\*\*: [0-9,]* IRS Form 990 XML files (tax years [0-9–]*)/\*\*Source data\*\*: $(format_num "$TOTAL_RETURNS") IRS Form 990 XML files (tax years $TAX_YEAR_RANGE)/" "$TMPFILE"

    # Update header line: (~X GB,
    sed -i "s/(~[0-9]* GB, SQLite WAL mode)/(~${DB_SIZE_GB} GB, SQLite WAL mode)/" "$TMPFILE"

    # Update each table's row count in #### headers
    for table in "${!COUNTS[@]}"; do
        count="${COUNTS[$table]}"
        formatted=$(format_num "$count")
        sed -i "s/\`${table}\` — [0-9,]* rows/\`${table}\` — ${formatted} rows/g" "$TMPFILE"
    done

    if [[ $DRY_RUN -eq 1 ]]; then
        echo "[DRY-RUN] Changes that would be made to CLAUDE.md:"
        diff "$CLAUDE_MD" "$TMPFILE" || true
        rm -f "$TMPFILE" "${CLAUDE_MD}.bak"
    else
        mv "$TMPFILE" "$CLAUDE_MD"
        rm -f "${CLAUDE_MD}.bak"
        echo "  CLAUDE.md updated"
    fi
fi

echo "Done."
