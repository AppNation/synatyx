#!/usr/bin/env bash
# Sync the canonical synatyx-memory Cursor rule into every repo that carries a copy.
#
# Canonical: <this repo>/.cursor/rules/synatyx-memory.mdc
# Copies:    any .cursor/rules/synatyx-memory.mdc under ~/workspace (except canonical)
#
# Usage:
#   scripts/sync-rules.sh          # overwrite every copy with canonical
#   scripts/sync-rules.sh --check  # report divergent copies, exit 1 if any differ
#
# Cursor always-applied rules are injected verbatim from the .mdc body, so copies
# must carry full content — a pointer file would load as an empty rule. This script
# is the drift guard instead. It cannot reach the user-level Cursor rule
# (Cursor Settings → Rules → User Rules); update that one by pasting canonical manually.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CANON="$REPO_ROOT/.cursor/rules/synatyx-memory.mdc"
SEARCH_ROOT="${SYNATYX_RULES_ROOT:-$HOME/workspace}"
MODE="${1:-sync}"

[[ -f "$CANON" ]] || { echo "canonical rule not found: $CANON" >&2; exit 2; }

drift=0
while IFS= read -r copy; do
  [[ "$copy" == "$CANON" ]] && continue
  if cmp -s "$CANON" "$copy"; then
    [[ "$MODE" == "--check" ]] && echo "ok       $copy"
  elif [[ "$MODE" == "--check" ]]; then
    echo "DIVERGED $copy"
    drift=1
  else
    cp "$CANON" "$copy"
    echo "synced   $copy"
  fi
done < <(find "$SEARCH_ROOT" -maxdepth 6 -path '*/node_modules' -prune -o \
           -path '*/.cursor/rules/synatyx-memory.mdc' -print 2>/dev/null)

if [[ "$MODE" == "--check" && $drift -eq 1 ]]; then
  echo "Divergent copies found — run scripts/sync-rules.sh to re-sync." >&2
  exit 1
fi
