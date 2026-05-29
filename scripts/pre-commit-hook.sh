#!/usr/bin/env bash
# Pre-commit credential-class leak gate.
# Config: ./.gitleaks.toml (in repo root)
# Install: bash scripts/install-leak-hooks.sh
#
# Bypass methods (intentional escape hatches; CI is still the backstop):
#   git commit --no-verify    # most common
#   git commit -n             # short form
#   Direct API commit (gh api commits, web editor) — bypasses local hooks entirely
#   Fresh `git clone` does NOT install this hook — re-run scripts/install-leak-hooks.sh
#
# Coverage note: this scans credential shapes only. The identity-marker layer
# runs separately in CI (.github/workflows/leak-scan.yml) and is not duplicated here.

CONFIG=".gitleaks.toml"
GITLEAKS="$(command -v gitleaks || true)"

if [ -z "$GITLEAKS" ]; then
    echo "[pre-commit] WARN: gitleaks not on PATH — credential-class check SKIPPED" >&2
    echo "[pre-commit] install: https://github.com/gitleaks/gitleaks/releases" >&2
    exit 0
fi

if [ ! -f "$CONFIG" ]; then
    echo "[pre-commit] WARN: $CONFIG not found in repo root — credential-class check SKIPPED" >&2
    exit 0
fi

output=$("$GITLEAKS" protect --staged --config "$CONFIG" --redact --no-banner 2>&1)
exit_code=$?

if [ $exit_code -ne 0 ]; then
    echo "$output" >&2
    cat >&2 <<EOF

COMMIT BLOCKED — pre-commit credential-class scan flagged staged content.

Next step (one of):
  - Remove the offending content from staged files, re-stage, retry commit
  - If you've reviewed and confirmed it's a false positive:
        git commit --no-verify

CI scans PR diffs server-side; that backstop remains active either way.
EOF
    exit 1
fi
exit 0
