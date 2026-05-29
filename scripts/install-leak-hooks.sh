#!/usr/bin/env bash
# Install the local pre-commit credential-class leak gate.
# Run from the repo root:  bash scripts/install-leak-hooks.sh
set -euo pipefail
HOOK_DIR="$(git rev-parse --git-path hooks)"
install -m 755 scripts/pre-commit-hook.sh "$HOOK_DIR/pre-commit"
echo "pre-commit hook installed at $HOOK_DIR/pre-commit"
