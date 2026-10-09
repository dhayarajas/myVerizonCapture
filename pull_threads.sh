#!/bin/zsh
# Pull the iPhone's unified-log archive for a recorded session and keep the app's entries
# (with thread IDs and the library that logged each line) as <session>/threads.ndjson.
# Usage: ./pull_threads.sh [session-dir]      (defaults to the newest captures/session-*)
# The phone keeps these logs for a limited time — run it soon after the capture (capture.sh does).
set -u
cd "${0:A:h}"
SESSION="${1:-$(ls -td captures/session-* 2>/dev/null | head -1)}"
SESSION="${SESSION%/}"
[[ -f "$SESSION/session.json" ]] || { echo "Not a recorded session: '$SESSION'"; exit 1; }

read START END <<<"$(python3 -c "
import json; m=json.load(open('$SESSION/session.json'))
print(int(m['startEpochMs']/1000)-5, int(m.get('endEpochMs', m['startEpochMs'])/1000)+5)")"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

echo "Pulling log archive from the iPhone..."
idevicesyslog archive "$TMP/a.tar" --start-time "$START" >/dev/null 2>&1 || { echo "Could not pull the log archive."; exit 1; }
mkdir -p "$TMP/s.logarchive" && tar -xf "$TMP/a.tar" -C "$TMP/s.logarchive" && rm -f "$TMP/a.tar"

FROM=$(date -r "$START" "+%Y-%m-%d %H:%M:%S"); TO=$(date -r "$END" "+%Y-%m-%d %H:%M:%S")
/usr/bin/log show --archive "$TMP/s.logarchive" --style ndjson --info --debug --start "$FROM" --end "$TO" \
  --predicate 'processImagePath BEGINSWITH "/private/var/containers/Bundle/Application"' \
  > "$SESSION/threads.ndjson" 2>/dev/null
echo "$(grep -c '"logEvent"' "$SESSION/threads.ndjson") app log events with thread IDs -> $SESSION/threads.ndjson"
