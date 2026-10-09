#!/bin/zsh
# One-shot session: record iPhone screen + device log, export Proxyman traffic, build the performance report.
# Usage: ./capture.sh [app-process-regex]     (press Ctrl+C to stop)
#   app-process-regex: the app's process name in the device log (auto-detected if omitted)
# Network-condition runs: NETWORK="3G" ./capture.sh  switches Proxyman's Network Condition tool on for the
#   recording (configure the profile once in Proxyman → Tools → Network Conditions) and tags the run.
# ANNOTATE=0 skips the annotated video export.
set -u
cd "${0:A:h}"
APP_PROC="${1:-}"
NETWORK="${NETWORK:-}"
PROXYMAN_CLI=/Applications/Proxyman.app/Contents/MacOS/proxyman-cli

[[ -x ./ios-screen-recorder ]] || swiftc -O ios-screen-recorder.swift -o ios-screen-recorder || exit 1
mkdir -p captures
LOG=captures/.device-$$.log

if [[ -n "$NETWORK" ]]; then
  echo "Network condition: $NETWORK (Proxyman Network Conditions ON)"
  "$PROXYMAN_CLI" network-condition on >/dev/null || echo "(could not enable Proxyman network condition)"
fi
idevicesyslog --no-colors -o "$LOG" >/dev/null 2>&1 &
SYSLOG=$!
./ios-screen-recorder captures &
REC=$!
trap 'kill -INT $REC 2>/dev/null' INT
wait $REC; wait $REC 2>/dev/null   # first wait is interrupted by Ctrl+C
trap - INT
kill $SYSLOG 2>/dev/null; wait $SYSLOG 2>/dev/null
[[ -n "$NETWORK" ]] && "$PROXYMAN_CLI" network-condition off >/dev/null

SESSION=$(ls -td captures/session-* | head -1)
[[ -f "$SESSION/session.json" ]] || { echo "No session recorded."; rm -f "$LOG"; exit 1; }
mv "$LOG" "$SESSION/device.log" 2>/dev/null
printf '{"network": "%s", "proxyThrottling": %s}\n' "${NETWORK:-default}" "$([[ -n "$NETWORK" ]] && echo true || echo false)" > "$SESSION/conditions.json"

./pull_threads.sh "$SESSION" || echo "(thread data unavailable — continuing without it)"

echo "Exporting Proxyman traffic..."
"$PROXYMAN_CLI" export-log -m all -f har -o "$PWD/$SESSION/traffic.har" >/dev/null || exit 1

NO_OPEN=1 ./report.sh "$SESSION" -- ${APP_PROC:+--app-process "$APP_PROC"} || exit 1
[[ "${ANNOTATE:-1}" == "0" ]] || python3 annotate_video.py "$SESSION" || echo "(annotated video export failed)"
[[ -n "${NO_OPEN:-}" ]] || open "$SESSION/report.html"
