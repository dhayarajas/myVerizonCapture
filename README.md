# myVerizonCapture — iPhone app performance capture (Proxyman + screen + device logs)

Records the iPhone screen over USB, the device log and the per-thread log archive while Proxyman
captures traffic, then builds a tabbed performance report that ties network activity, app logs and
threads to what was on screen.

## Requirements
- macOS with Xcode command-line tools (`swiftc`), Python 3 with `numpy` + `Pillow`
- `brew install libimobiledevice ffmpeg`
- Proxyman with the iPhone proxied and SSL Proxying enabled (unpinned app build)
- iPhone connected by USB, unlocked, trusted; QuickTime closed

## One-time
```sh
swiftc -O ios-screen-recorder.swift -o ios-screen-recorder
swiftc -O screen-ocr.swift -o screen-ocr        # also built automatically on first report
```
The first recording may prompt for Camera permission for your terminal app
(System Settings → Privacy & Security → Camera).

## Capture a session
```sh
./capture.sh                       # use the app; Ctrl+C when done
./capture.sh 'myverizonEnterprise' # optional: app process name if auto-detection misses it
```
Records `screen.mp4` + stills, streams the device log, pulls the thread-level log archive, exports
Proxyman traffic (HAR) and opens `captures/session-<ts>/report.html`.

Options: `NETWORK="3G" ./capture.sh` switches Proxyman's Network Conditions tool on for the recording
(configure the throttling profile once in Proxyman) and tags the run; `ANNOTATE=0` skips the annotated video.

## Rebuild a report / compare runs
```sh
./report.sh                                   # newest session
./report.sh captures/session-<ts>             # a specific session
./report.sh captures/session-<ts> other.har   # with a different HAR
./report.sh captures/session-<ts> -- --app-process myverizonEnterprise
./compare.sh                                  # all sessions -> captures/comparison.html
./compare.sh captures/session-A captures/session-B
./pull_threads.sh captures/session-<ts>       # re-pull thread data (the phone keeps it for a limited time)
./annotate.sh                                 # annotated.mp4 + annotated-1080p.mp4 for the newest session
./annotate.sh captures/session-<ts>           # … for a specific session
./annotate.sh --compare captures/session-A captures/session-B   # side-by-side video aligned at launch
ANNOTATE=1 ./report.sh captures/session-<ts>  # rebuild the report and the annotated video together
python3 publish_reports.py                    # copy reports into reports/ for committing (--redact for a shareable copy)
NO_OPEN=1 ./report.sh                         # don't open the browser
```

## Annotated video
Three panels — the recording (tap/Face ID flashes, spinner / no-network chips, speed labels), a live panel (clock,
screen, cold-start progress, what is **blocking the screen now** with its fix and estimated saving, key findings so
far) and history (rolling 15 s waterfall with the critical path outlined, event log). Idle stretches play at 4×,
taps / Face ID / request fan-out at 0.5× (the clock always shows real time). Starts with a KPI intro card, ends with a
"what to fix first" summary; per-screen chapters and an events subtitle track are embedded. Exported at 2320×1304
and 1920×1080. `--compare` renders two runs side by side, aligned at launch, with milestone deltas.

## Report tabs
Every tab starts with summary cards and key takeaways; details follow.
- **Overview** — KPIs, where the spinner time went, cold-start ribbon, budget gauges, finding cards,
  biggest wins (estimated savings) and slowest API calls.
- **Cold start** — (plus warm start / resume when the app is brought back from the background) launch → *Splash screen* → *Loader* → first titled screen (e.g. *Choose your profile*),
  with what the app did in each phase (lifecycle, app markers, SDK init, errors, network, main vs
  background threads).
- **Loading periods** — spinner detected on screen, the request chain that held it, split by request type
  and by phase (server wait, connection setup, download, waiting for a connection, on-device work, Face ID
  prompt), plus main/background thread activity.
- **Render milestones** — named screen transitions, response → paint time, frame rate / hitches while moving,
  and tap → first visual feedback.
- **Screens** — cost per screen: API/analytics/logging/image calls, bytes, network time, spinner, settle time.
- **Efficiency** — largest JSON responses broken down by key, JSON repeated across responses, content sent
  but marked hidden, images (real pixels vs screen width, WebP re-encode estimate), cache audit, and
  third-party/telemetry cost overall and during spinners.
- **App lifecycle** — launch, app delegate callbacks, SRE markers, Face ID prompts.
- **Timeline** — filmstrip, named screens, screen state, main-thread and background-thread activity, app
  log, request waterfall (hover a bar for timing/phases/issues, click for request/response payloads).
- **Hosts**, **Requests**, **Device log** (filterable).

`compare.sh` produces a cross-run report: budget pass rate, milestone median/p90/spread, endpoint
median/p90/server wait/variation.

## Data sources
| Source | Gives |
|---|---|
| `screen.mp4` (AVFoundation over USB) | screen states, spinner detection, screen names (Vision OCR) |
| Proxyman HAR | requests, payloads, headers, durations |
| `device.log` (`idevicesyslog`) | app & system logs, app SRE markers (`e2eRequestId`), CFNetwork per-request timing (DNS / connect / TLS / server wait / download, protocol, connection reuse), Face ID (coreauthd) |
| `threads.ndjson` (`idevicesyslog archive` + `log show`) | thread ID and logging library per entry → main vs background threads |

## Budgets
Edit `budgets.json`; `metric` is any key from a session's `summary.json` → `metrics`
(e.g. `cold_start_total`, `cold:Loader → Choose your profile`, `spinner:Sign-in (Face ID)`, `api_p95`).

## Files
| File | Role |
|---|---|
| `capture.sh` | one-shot session: record → device log → thread archive → HAR export → report |
| `ios-screen-recorder.swift` | records the iPhone screen over USB |
| `screen-ocr.swift` | on-screen text recognition used to name screens |
| `pull_threads.sh` | pulls the unified-log archive with thread IDs |
| `perf_report.py` / `report.sh` | builds the per-session report and `summary.json` |
| `compare_runs.py` / `compare.sh` | cross-run comparison |
| `budgets.json` | performance budgets |
| `annotate_video.py` | annotated video export |
| `publish_reports.py` | copies (optionally redacted) reports into `reports/` |
| `reports/` | committed reports (full, unredacted: contains tokens, cookies and phone numbers — keep the repo private) |

## Raw captures in this repo
`captures/` (raw sessions: recording, stills, HAR, device log, thread data, OCR cache, reports, videos) is committed
alongside `reports/`. Files over GitHub's 100 MB limit are handled losslessly: large device logs are stored as
`device.log.gz` (the tools read `.gz` directly) and oversized original recordings use **Git LFS** — run
`git lfs install` before cloning to get the real video instead of a pointer file.

⚠️ `captures/` and `reports/` contain full payloads, tokens, cookies and phone numbers — keep the repository private.
