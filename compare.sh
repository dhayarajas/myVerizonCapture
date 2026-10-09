#!/bin/zsh
# Compare captured runs (median / p90 / budgets / endpoints) -> captures/comparison.html
# Usage: ./compare.sh [session-dir ...]     (default: every captures/session-*)
#        NO_OPEN=1 ./compare.sh
set -u
cd "${0:A:h}"
SESSIONS=("$@")
(( ${#SESSIONS} )) || SESSIONS=($(ls -d captures/session-* 2>/dev/null))
for S in $SESSIONS; do
  [[ -f "$S/summary.json" ]] || NO_OPEN=1 ./report.sh "$S" >/dev/null || echo "Skipping $S (no report)"
done
python3 compare_runs.py $SESSIONS || exit 1
[[ -n "${NO_OPEN:-}" ]] || open captures/comparison.html
