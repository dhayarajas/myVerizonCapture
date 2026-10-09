#!/bin/zsh
# Export the annotated video for a session: phone recording + live panel (time, screen, what is blocking
# the screen now with the fix, key findings so far) + history (rolling 15 s waterfall and event log).
# Usage: ./annotate.sh [session-dir]     (default: newest captures/session-*; builds the report first if needed)
#        NO_OPEN=1 ./annotate.sh
set -u
cd "${0:A:h}"
SESSION="${1:-$(ls -td captures/session-* 2>/dev/null | head -1)}"
SESSION="${SESSION%/}"
[[ -f "$SESSION/session.json" ]] || { echo "Not a recorded session: '$SESSION'"; exit 1; }
[[ -f "$SESSION/report.html" ]] || NO_OPEN=1 ./report.sh "$SESSION" || exit 1
python3 annotate_video.py "$SESSION" || exit 1
[[ -n "${NO_OPEN:-}" ]] || open "$SESSION/annotated.mp4"
