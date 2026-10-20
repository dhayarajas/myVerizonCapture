#!/bin/zsh
# Single entry point for the capture toolkit. ./run.sh help (or see commands.txt) for every option.
# Sessions can be given as a path (captures/session-20261009-230842), just the stamp (20261009-230842),
# or "latest" / omitted for the newest session.
set -u
cd "${0:A:h}"
PROXYMAN_CLI=/Applications/Proxyman.app/Contents/MacOS/proxyman-cli

usage() {
  cat <<'EOF'
Usage: ./run.sh <command> [options]

Capture
  capture [--app NAME] [--network PROFILE] [--no-video] [--no-open]
                         record screen + device log + thread archive + HAR, build report (+ annotated video)
  doctor                 check iPhone, Proxyman, ffmpeg, Python deps and the Swift tools before a capture
  build                  (re)compile ios-screen-recorder and screen-ocr

Rebuild from an existing session
  report [SESSION] [--har FILE] [--app NAME] [--video] [--no-open]
                         rebuild report.html (+ annotated video with --video)
  video [SESSION] [--no-open]
                         annotated.mp4 + annotated-1080p.mp4
  threads [SESSION]      re-pull the thread-level log archive from the phone (do it soon after capture)

Compare
  compare [SESSION ...]  cross-run report -> captures/comparison.html (default: every session)
  compare-video A B [OUT.mp4]
                         side-by-side video aligned at launch (default captures/comparison.mp4)

Browse / share
  list                   sessions with what each one has (report, video, HAR, threads)
  open [SESSION]         open report.html
  play [SESSION]         open annotated.mp4 (falls back to screen.mp4)
  publish [--redact] [SESSION ...]
                         copy reports into reports/ (--redact masks tokens/ids/phone numbers)

SESSION = path, stamp (20261009-230842) or "latest" (default).
EOF
}

die() { echo "$*" >&2; exit 1; }

# resolve "latest" / stamp / path -> captures/session-<stamp>
session() {
  local s="${1:-latest}"
  if [[ "$s" == "latest" ]]; then s=$(ls -td captures/session-* 2>/dev/null | head -1)
  elif [[ ! -d "$s" && -d "captures/session-$s" ]]; then s="captures/session-$s"
  elif [[ ! -d "$s" && -d "captures/$s" ]]; then s="captures/$s"; fi
  s="${s%/}"
  [[ -n "$s" && -f "$s/session.json" ]] || die "Not a recorded session: '${1:-latest}' (try ./run.sh list)"
  echo "$s"
}

cmd_capture() {
  local app="" net="" video=1 open=1
  while (( $# )); do
    case "$1" in
      --app) app="${2:?--app NAME}"; shift ;;
      --network) net="${2:?--network PROFILE}"; shift ;;
      --no-video) video=0 ;;
      --no-open) open=0 ;;
      *) die "capture: unknown option '$1'" ;;
    esac; shift
  done
  if (( open )); then
    NETWORK="$net" ANNOTATE="$video" ./capture.sh ${app:+"$app"}
  else
    NETWORK="$net" ANNOTATE="$video" NO_OPEN=1 ./capture.sh ${app:+"$app"}
  fi
}

cmd_report() {
  local s="" har="" app="" video=0 open=1
  while (( $# )); do
    case "$1" in
      --har) har="${2:?--har FILE}"; shift ;;
      --app) app="${2:?--app NAME}"; shift ;;
      --video) video=1 ;;
      --no-open) open=0 ;;
      -*) die "report: unknown option '$1'" ;;
      *) s="$1" ;;
    esac; shift
  done
  s=$(session "$s") || exit 1
  local -a vars=(ANNOTATE=$video); (( open )) || vars+=(NO_OPEN=1)
  env "${vars[@]}" ./report.sh "$s" ${har:+"$har"} -- ${app:+--app-process "$app"}
}

cmd_video() {
  local s="" open=1
  for a in "$@"; do [[ "$a" == "--no-open" ]] && open=0 || s="$a"; done
  s=$(session "$s") || exit 1
  if (( open )); then ./annotate.sh "$s"; else NO_OPEN=1 ./annotate.sh "$s"; fi
}

cmd_compare_video() {
  (( $# >= 2 )) || die "compare-video: need two sessions"
  local a b; a=$(session "$1") || exit 1; b=$(session "$2") || exit 1
  ./annotate.sh --compare "$a" "$b" ${3:+"$3"}
}

cmd_compare() {
  local -a ss=(); local x r
  for x in "$@"; do r=$(session "$x") || exit 1; ss+=("$r"); done
  ./compare.sh "${ss[@]}"
}

cmd_publish() {
  local -a flags=() ss=(); local x r
  for x in "$@"; do
    if [[ "$x" == --* ]]; then flags+=("$x"); else r=$(session "$x") || exit 1; ss+=("$r"); fi
  done
  python3 publish_reports.py "${flags[@]}" "${ss[@]}"
}

cmd_list() {
  local s n=0
  printf '%-34s %-6s %-6s %-6s %-7s %s\n' SESSION REPORT VIDEO HAR THREADS NETWORK
  for s in $(ls -td captures/session-* 2>/dev/null); do
    [[ -f "$s/session.json" ]] || continue
    local net=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get('network','default'))" "$s/conditions.json" 2>/dev/null)
    printf '%-34s %-6s %-6s %-6s %-7s %s\n' "$s" \
      "$([[ -f $s/report.html ]] && print yes || print -- -)" \
      "$([[ -f $s/annotated.mp4 ]] && print yes || print -- -)" \
      "$([[ -f $s/traffic.har || -f $s/traffic.har.gz ]] && print yes || print -- -)" \
      "$([[ -s $s/threads.ndjson ]] && print yes || print -- -)" "${net:-default}"
    (( n++ ))
  done
  (( n )) || echo "(no sessions yet — ./run.sh capture)"
}

cmd_doctor() {
  local bad=0
  ok()   { printf '  ok    %s\n' "$*"; }
  warn() { printf '  WARN  %s\n' "$*"; }
  fail() { printf '  FAIL  %s\n' "$*"; bad=1; }
  echo "Checking capture prerequisites..."
  command -v idevice_id >/dev/null || fail "libimobiledevice missing (brew install libimobiledevice)"
  if command -v idevice_id >/dev/null; then
    local udid=$(idevice_id -l 2>/dev/null | head -1)
    [[ -n "$udid" ]] && ok "iPhone on USB ($udid)" || fail "no iPhone on USB — connect, unlock, 'Trust This Mac'"
  fi
  pgrep -xq "QuickTime Player" && fail "QuickTime Player is running — quit it (it holds the iPhone capture device)" || ok "QuickTime not running"
  command -v ffmpeg >/dev/null && ok "ffmpeg" || fail "ffmpeg missing (brew install ffmpeg)"
  python3 -c "import numpy, PIL" 2>/dev/null && ok "python3 numpy + Pillow" || fail "python3 needs numpy and Pillow (pip install numpy Pillow)"
  [[ -x "$PROXYMAN_CLI" ]] && ok "Proxyman CLI" || fail "Proxyman not found at $PROXYMAN_CLI"
  pgrep -xq Proxyman && ok "Proxyman running" || warn "Proxyman isn't running — start it so traffic is captured"
  [[ -x ./ios-screen-recorder ]] && ok "ios-screen-recorder built" || warn "ios-screen-recorder not built (./run.sh build, or built on first capture)"
  [[ -x ./screen-ocr ]] && ok "screen-ocr built" || warn "screen-ocr not built (built on first report)"
  (( bad )) && { echo "Fix the FAIL items, then ./run.sh capture"; return 1; }
  echo "Ready: ./run.sh capture"
}

cmd_build() {
  swiftc -O ios-screen-recorder.swift -o ios-screen-recorder && swiftc -O screen-ocr.swift -o screen-ocr && echo "Built ios-screen-recorder and screen-ocr"
}

cmd_open() { local s; s=$(session "${1:-}") || exit 1; open "$s/report.html"; }
cmd_play() {
  local s; s=$(session "${1:-}") || exit 1
  [[ -f "$s/annotated.mp4" ]] && open "$s/annotated.mp4" || { echo "No annotated video yet (./run.sh video $s) — opening the raw recording"; open "$s/screen.mp4"; }
}

CMD="${1:-help}"; (( $# )) && shift
case "$CMD" in
  capture)        cmd_capture "$@" ;;
  report)         cmd_report "$@" ;;
  video|annotate) cmd_video "$@" ;;
  threads)        S=$(session "${1:-}") || exit 1; ./pull_threads.sh "$S" ;;
  compare)        cmd_compare "$@" ;;
  compare-video)  cmd_compare_video "$@" ;;
  publish)        cmd_publish "$@" ;;
  list|ls)        cmd_list ;;
  open)           cmd_open "$@" ;;
  play)           cmd_play "$@" ;;
  doctor|check)   cmd_doctor ;;
  build)          cmd_build ;;
  help|-h|--help) usage ;;
  *) echo "Unknown command: $CMD"; echo; usage; exit 1 ;;
esac
