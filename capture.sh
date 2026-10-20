#!/bin/zsh
# One-shot session: record iPhone screen + device log, export Proxyman traffic, build the performance report.
# Usage: ./capture.sh [app-process-regex]     (press Ctrl+C to stop)
#   app-process-regex: the app's process name in the device log (auto-detected if omitted)
# Network-condition runs: NETWORK="3G" ./capture.sh  switches Proxyman's Network Condition tool on for the
#   recording (configure the profile once in Proxyman → Tools → Network Conditions) and tags the run.
# ANNOTATE=0 skips the annotated video export (Ctrl+C during the video step only cancels the video).
# See ./run.sh help and commands.txt for every option.
set -u
cd "${0:A:h}"
APP_PROC="${1:-}"
NETWORK="${NETWORK:-}"
PROXYMAN_CLI=/Applications/Proxyman.app/Contents/MacOS/proxyman-cli

[[ -x ./ios-screen-recorder ]] || swiftc -O ios-screen-recorder.swift -o ios-screen-recorder || exit 1
[[ -x "$PROXYMAN_CLI" ]] || { echo "Proxyman not found at $PROXYMAN_CLI"; exit 1; }
if [[ -z "$(idevice_id -l 2>/dev/null)" ]]; then
  echo "No iPhone on USB. Connect it, unlock it, tap 'Trust This Mac', close QuickTime, then retry."
  echo "(check with: idevice_id -l)"
  exit 1
fi
mkdir -p captures
LOG=captures/.device-$$.log
PREV=$(ls -td captures/session-* 2>/dev/null | head -1)

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

# Only ever process the session this run recorded — never fall back to an older one.
SESSION=$(ls -td captures/session-* 2>/dev/null | head -1)
if [[ -z "$SESSION" || "$SESSION" == "$PREV" || ! -f "$SESSION/session.json" ]]; then
  echo "No new session was recorded — nothing processed (earlier sessions untouched)."
  rm -f "$LOG"; exit 1
fi
mv "$LOG" "$SESSION/device.log" 2>/dev/null
printf '{"network": "%s", "proxyThrottling": %s}\n' "${NETWORK:-default}" "$([[ -n "$NETWORK" ]] && echo true || echo false)" > "$SESSION/conditions.json"

./pull_threads.sh "$SESSION" || echo "(thread data unavailable — continuing without it)"

echo "Exporting Proxyman traffic..."
"$PROXYMAN_CLI" export-log -m all -f har -o "$PWD/$SESSION/traffic.har" >/dev/null || exit 1

NO_OPEN=1 ./report.sh "$SESSION" -- ${APP_PROC:+--app-process "$APP_PROC"} || exit 1
[[ "${ANNOTATE:-1}" == "0" ]] || NO_OPEN=1 ./annotate.sh "$SESSION" || echo "(annotated video export failed)"
[[ -n "${NO_OPEN:-}" ]] || open "$SESSION/report.html"
