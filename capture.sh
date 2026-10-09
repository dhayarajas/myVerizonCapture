#!/bin/zsh
# One-shot session: record iPhone screen + device log, export Proxyman traffic, build the performance report.
# Usage: ./capture.sh [app-process-regex]     (press Ctrl+C to stop)
#   app-process-regex: the app's process name in the device log (auto-detected if omitted)
set -u
cd "${0:A:h}"
APP_PROC="${1:-}"
PROXYMAN_CLI=/Applications/Proxyman.app/Contents/MacOS/proxyman-cli

[[ -x ./ios-screen-recorder ]] || swiftc -O ios-screen-recorder.swift -o ios-screen-recorder || exit 1
mkdir -p captures
LOG=captures/.device-$$.log

idevicesyslog --no-colors -o "$LOG" >/dev/null 2>&1 &
SYSLOG=$!
./ios-screen-recorder captures &
REC=$!
trap 'kill -INT $REC 2>/dev/null' INT
wait $REC; wait $REC 2>/dev/null   # first wait is interrupted by Ctrl+C
trap - INT
kill $SYSLOG 2>/dev/null; wait $SYSLOG 2>/dev/null

SESSION=$(ls -td captures/session-* | head -1)
[[ -f "$SESSION/session.json" ]] || { echo "No session recorded."; rm -f "$LOG"; exit 1; }
mv "$LOG" "$SESSION/device.log" 2>/dev/null

./pull_threads.sh "$SESSION" || echo "(thread data unavailable — continuing without it)"

echo "Exporting Proxyman traffic..."
"$PROXYMAN_CLI" export-log -m all -f har -o "$PWD/$SESSION/traffic.har" >/dev/null || exit 1

./report.sh "$SESSION" -- ${APP_PROC:+--app-process "$APP_PROC"}
