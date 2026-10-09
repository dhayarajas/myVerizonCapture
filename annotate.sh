#!/bin/zsh
# Annotated video export.
#   ./annotate.sh [session-dir]               -> <session>/annotated.mp4 (2320x1304) + annotated-1080p.mp4
#        recording + live panel (time, screen, cold-start progress, what is blocking the screen now with the fix,
#        key findings so far) + history (rolling 15 s waterfall with critical path, event log); idle stretches
#        fast-forwarded, taps / Face ID / request fan-out in slow motion; intro + summary cards; chapters + subtitles
#   ./annotate.sh --compare <sessionA> <sessionB> [out.mp4]   -> side-by-side comparison aligned at app launch
#        (default output: captures/comparison.mp4)
#   NO_OPEN=1 ./annotate.sh …                 don't open the video afterwards
set -u
cd "${0:A:h}"
if [[ "${1:-}" == "--compare" ]]; then
  A="${2:?session A}"; B="${3:?session B}"; OUT="${4:-captures/comparison.mp4}"
  for S in "$A" "$B"; do [[ -f "${S%/}/report.html" ]] || NO_OPEN=1 ./report.sh "${S%/}" || exit 1; done
  python3 annotate_video.py --compare "${A%/}" "${B%/}" -o "$OUT" || exit 1
  [[ -n "${NO_OPEN:-}" ]] || open "$OUT"
  exit 0
fi
SESSION="${1:-$(ls -td captures/session-* 2>/dev/null | head -1)}"
SESSION="${SESSION%/}"
[[ -f "$SESSION/session.json" ]] || { echo "Not a recorded session: '$SESSION'"; exit 1; }
[[ -f "$SESSION/report.html" ]] || NO_OPEN=1 ./report.sh "$SESSION" || exit 1
python3 annotate_video.py "$SESSION" || exit 1
[[ -n "${NO_OPEN:-}" ]] || open "$SESSION/annotated.mp4"
