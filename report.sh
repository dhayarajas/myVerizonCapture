#!/bin/zsh
# Build (or rebuild) the performance report for a captured session.
# Usage: ./report.sh [session-dir] [traffic.har] [-- extra perf_report.py options]
#   session-dir  defaults to the newest captures/session-*
#   traffic.har  defaults to <session-dir>/traffic.har; if missing, exports the current Proxyman session
# Examples:
#   ./report.sh
#   ./report.sh captures/session-20261009-211154
#   ./report.sh captures/session-20261009-204735 captures/test-run.har
#   ./report.sh captures/session-20261009-211154 -- --app-process myverizonEnterprise
#   NO_OPEN=1 ./report.sh
#   ANNOTATE=1 ./report.sh          (also export the annotated video; or use ./annotate.sh)
set -u
cd "${0:A:h}"
PROXYMAN_CLI=/Applications/Proxyman.app/Contents/MacOS/proxyman-cli

SESSION="" HAR=""
while (( $# )); do
  [[ "$1" == "--" ]] && { shift; break; }
  if [[ -z "$SESSION" ]]; then SESSION="$1"; else HAR="$1"; fi
  shift
done
[[ -n "$SESSION" ]] || SESSION=$(ls -td captures/session-* 2>/dev/null | head -1)
SESSION="${SESSION%/}"
[[ -f "$SESSION/session.json" ]] || { echo "Not a recorded session: '${SESSION:-<none>}'"; exit 1; }

if [[ -z "$HAR" ]]; then
  HAR="$SESSION/traffic.har"
  [[ ! -f "$HAR" && -f "$HAR.gz" ]] && HAR="$HAR.gz"
  if [[ ! -f "$HAR" ]]; then
    echo "No traffic.har in $SESSION — exporting the current Proxyman session..."
    "$PROXYMAN_CLI" export-log -m all -f har -o "$PWD/$HAR" 2>&1 | grep -E '❌|rror'
  fi
  if [[ ! -s "$HAR" ]]; then
    # No traffic in Proxyman: still build the screen / log / thread report, just with no requests.
    echo "⚠️  No Proxyman traffic — building the report without network data."
    rm -f "$HAR"; HAR="$SESSION/.empty.har"
    echo '{"log":{"version":"1.2","creator":{"name":"none","version":"0"},"entries":[]}}' > "$HAR"
  fi
fi
[[ -f "$HAR" ]] || { echo "HAR file not found: $HAR"; exit 1; }

python3 perf_report.py "$HAR" "$SESSION" "$@" || exit 1
[[ "${ANNOTATE:-0}" == "1" ]] && { python3 annotate_video.py "$SESSION" || echo "(annotated video export failed)"; }
[[ -n "${NO_OPEN:-}" ]] || open "$SESSION/report.html"
