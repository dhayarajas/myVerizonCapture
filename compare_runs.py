#!/usr/bin/env python3
"""Compare several captured runs: milestone medians / p90, budget pass rate, endpoint stability.

Reads <session>/summary.json written by perf_report.py.
Usage: python3 compare_runs.py [session-dir ...] [-o captures/comparison.html] [--budgets budgets.json]
       (no session dirs = every captures/session-* that has a summary.json)
"""
import argparse
import glob
import html
import json
import os
import statistics
from collections import defaultdict

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
FRIENDLY = {"recording": "Recording length", "spinner_total": "Total spinner time", "spinner_longest": "Longest spinner",
            "api_p50": "API p50", "api_p95": "API p95", "requests": "Requests", "api_calls": "API calls", "bytes": "Bytes received",
            "dup_image_bytes": "Duplicate image bytes", "queued_max": "Max on-device queueing", "cold_start_total": "Cold start total",
            "launch_to_first_content": "Launch → first content", "launch_to_first_api": "Launch → first API call",
            "launch_to_did_finish_launching": "Launch → didFinishLaunching"}
SECONDS = lambda k: not k.endswith(("requests", "api_calls", "bytes")) and k not in ("requests", "api_calls", "bytes", "dup_image_bytes")


def pct(v, p):
    v = sorted(v)
    return v[min(len(v) - 1, max(0, int(round(p / 100 * len(v) + 0.5)) - 1))] if v else None


def label(k):
    for pre, name in (("cold:", "Cold start · "), ("spinner:", "Spinner · "), ("render:", "Render · ")):
        if k.startswith(pre):
            return name + k[len(pre):]
    return FRIENDLY.get(k, k)


def fmt(k, v):
    if v is None:
        return "—"
    if "bytes" in k:
        return f"{v/1024:.0f} KB"
    if k in ("requests", "api_calls"):
        return f"{v:.0f}"
    return f"{v:.2f}s"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sessions", nargs="*")
    ap.add_argument("-o", "--out", default=os.path.join(TOOL_DIR, "captures", "comparison.html"))
    ap.add_argument("--budgets", default=os.path.join(TOOL_DIR, "budgets.json"))
    a = ap.parse_args()
    dirs = a.sessions or sorted(glob.glob(os.path.join(TOOL_DIR, "captures", "session-*")))
    runs = []
    for d in dirs:
        f = os.path.join(d, "summary.json")
        if os.path.exists(f):
            s = json.load(open(f))
            s["dir"] = d.rstrip("/")
            runs.append(s)
    if not runs:
        raise SystemExit("No summary.json found — run ./report.sh on the sessions first.")
    try:
        budgets = json.load(open(a.budgets)).get("budgets", [])
    except (OSError, ValueError):
        budgets = []
    out_dir = os.path.dirname(os.path.abspath(a.out))
    names = [os.path.basename(r["dir"]).replace("session-", "") for r in runs]

    keys = []
    for r in runs:
        for k in r["metrics"]:
            if k not in keys:
                keys.append(k)
    order = lambda k: (0 if k.startswith("cold") else 1 if k.startswith("launch") else 2 if k.startswith("spinner") else
                       3 if k.startswith("render") else 4, k)
    keys.sort(key=order)
    rows = []
    for k in keys:
        vals = [r["metrics"].get(k) for r in runs]
        got = [v for v in vals if isinstance(v, (int, float))]
        med = statistics.median(got) if got else None
        spread = (max(got) - min(got)) / med * 100 if got and med else None
        rows.append({"k": k, "vals": vals, "n": len(got), "med": med, "p90": pct(got, 90), "min": min(got) if got else None,
                     "max": max(got) if got else None, "spread": spread})

    brow = []
    for b in budgets:
        vals = [r["metrics"].get(b["metric"]) for r in runs]
        got = [v for v in vals if isinstance(v, (int, float))]
        med = statistics.median(got) if got else None
        brow.append({**b, "vals": vals, "med": med, "p90": pct(got, 90), "passN": sum(1 for v in got if v <= b["max"]), "n": len(got)})

    eps = defaultdict(lambda: {"runs": [None] * len(runs), "all": [], "ttfb": []})
    for i, r in enumerate(runs):
        for k, e in r.get("endpoints", {}).items():
            if e["dur"]:
                eps[k]["runs"][i] = statistics.median(e["dur"])
                eps[k]["all"] += e["dur"]
                eps[k]["ttfb"] += e.get("ttfb", [])
    erows = sorted(({"k": k, **v, "med": statistics.median(v["all"]), "p90": pct(v["all"], 90),
                     "ttfb": statistics.median(v["ttfb"]) if v["ttfb"] else None,
                     "cv": (statistics.pstdev(v["all"]) / statistics.mean(v["all"]) * 100) if len(v["all"]) > 1 else None}
                    for k, v in eps.items()), key=lambda x: -x["med"])
    for x in erows:
        x["cvs"] = "" if x["cv"] is None else "%.0f%%" % x["cv"]

    noisy = max((r for r in rows if r["spread"] is not None and r["n"] >= 2 and SECONDS(r["k"]) and (r["med"] or 0) >= 0.3),
                key=lambda r: r["spread"], default=None)
    passing = sum(1 for b in brow if b["med"] is not None and b["med"] <= b["max"])
    e = html.escape
    cards = [(len(runs), "Runs compared", ", ".join(names[:4]) + ("…" if len(names) > 4 else ""), ""),
             (f"{passing}/{sum(1 for b in brow if b['med'] is not None)}", "Budgets met (median)", "", "bad" if passing < len(brow) else ""),
             (fmt("x", next((r["med"] for r in rows if r["k"] == "cold_start_total"), None)), "Cold start (median)", "", ""),
             (fmt("x", next((r["med"] for r in rows if r["k"] == "spinner_total"), None)), "Total spinner (median)", "", ""),
             (f"{noisy['spread']:.0f}%" if noisy else "—", "Most variable metric", e(label(noisy["k"])) if noisy else "", "warn" if noisy and noisy["spread"] >= 30 else ""),
             (fmt("x", erows[0]["med"]) if erows else "—", "Slowest endpoint (median)", e(erows[0]["k"].split(" ", 1)[1][-60:]) if erows else "", "bad" if erows and erows[0]["med"] >= 2 else "")]
    take = []
    for b in brow:
        if b["med"] is not None and b["med"] > b["max"]:
            take.append(f"<b>{e(b.get('label', b['metric']))}</b> over budget: median {b['med']:.2f}s vs {b['max']:.2f}s (met in {b['passN']}/{b['n']} runs)")
    for r in sorted((r for r in rows if r["spread"] and r["n"] >= 2 and SECONDS(r["k"]) and (r["med"] or 0) >= 0.5), key=lambda r: -r["spread"])[:3]:
        take.append(f"<b>{e(label(r['k']))}</b> varies {r['spread']:.0f}% across runs ({fmt(r['k'], r['min'])} – {fmt(r['k'], r['max'])})")
    for x in erows[:3]:
        take.append(f"<span class='mono'>{e(x['k'])}</span>: median {x['med']:.2f}s, p90 {x['p90']:.2f}s" + (f", server wait {x['ttfb']:.2f}s" if x["ttfb"] else ""))

    head = "".join(f"<th><a href='{e(os.path.relpath(os.path.join(r['dir'], 'report.html'), out_dir))}'>{e(n)}</a></th>" for r, n in zip(runs, names))
    def mrow(r):
        bud = next((b for b in budgets if b["metric"] == r["k"]), None)
        cells = "".join(f"<td class='{'over' if bud and v is not None and v > bud['max'] else ''}'>{fmt(r['k'], v)}</td>" for v in r["vals"])
        spread = "" if r["spread"] is None else "%.0f%%" % r["spread"]
        return (f"<tr><td>{e(label(r['k']))}</td>{cells}<td><b>{fmt(r['k'], r['med'])}</b></td><td>{fmt(r['k'], r['p90'])}</td>"
                f"<td>{spread}</td><td>{fmt(r['k'], bud['max']) if bud else ''}</td></tr>")
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MyVerizon App Performance – run comparison</title><style>
:root{{--bg:#f6f7f9;--card:#fff;--fg:#1d2330;--mut:#667085;--line:#e4e7ec;--acc:#2563eb;--high:#dc2626;--med:#d97706}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0f1115;--card:#181b22;--fg:#e6e8ec;--mut:#98a2b3;--line:#2a2f3a}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:13px/1.45 -apple-system,system-ui,sans-serif}}main{{padding:16px 24px 60px;max-width:1500px}}
h1{{font-size:20px;margin:0 0 4px}}h2{{font-size:16px;margin:26px 0 10px}}.mut{{color:var(--mut)}}.mono{{font-family:ui-monospace,Menlo,monospace;font-size:12px}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:10px;margin:12px 0}}.kpi{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px}}
.kpi b{{display:block;font-size:20px}}.kpi span{{color:var(--mut);font-size:12px}}.kpi em{{display:block;font-style:normal;font-size:11px;color:var(--mut);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.kpi.bad b{{color:var(--high)}}.kpi.warn b{{color:var(--med)}}.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;margin:10px 0;overflow-x:auto}}
.take{{border-left:3px solid var(--acc)}}.take ul{{margin:0;padding-left:18px}}.take li{{margin:4px 0}}
table{{border-collapse:collapse;width:100%}}th,td{{text-align:left;padding:5px 8px;border-bottom:1px solid var(--line);white-space:nowrap}}th{{color:var(--mut);font-size:12px}}
td.over{{color:var(--high);font-weight:600}}a{{color:var(--acc)}}.ok{{color:#16a34a;font-weight:600}}.no{{color:var(--high);font-weight:600}}
</style></head><body><main>
<h1>MyVerizon App Performance — run comparison</h1><div class="mut">{len(runs)} runs · {e(runs[0].get('device') or '')} · values in red are over budget</div>
<div class="kpis">{''.join(f"<div class='kpi {t}'><b>{v}</b><span>{l}</span>{f'<em>{s}</em>' if s else ''}</div>" for v, l, s, t in cards)}</div>
<div class="card take"><h3 style="margin:0 0 4px">Key takeaways</h3><ul>{''.join(f'<li>{t}</li>' for t in take) or '<li>No issues across runs.</li>'}</ul></div>
<h2>Budgets</h2><div class="card"><table><tr><th>Budget</th><th>Max</th>{head}<th>Median</th><th>p90</th><th>Met</th></tr>
{''.join(f"<tr><td>{e(b.get('label', b['metric']))}</td><td>{fmt(b['metric'], b['max'])}</td>" + ''.join(f"<td class='{'over' if v is not None and v > b['max'] else ''}'>{fmt(b['metric'], v)}</td>" for v in b['vals']) + f"<td><b>{fmt(b['metric'], b['med'])}</b></td><td>{fmt(b['metric'], b['p90'])}</td><td class='{'ok' if b['n'] and b['passN'] == b['n'] else 'no'}'>{b['passN']}/{b['n']}</td></tr>" for b in brow)}</table></div>
<h2>Milestones &amp; metrics</h2><div class="card"><table><tr><th>Metric</th>{head}<th>Median</th><th>p90</th><th>Spread</th><th>Budget</th></tr>{''.join(mrow(r) for r in rows)}</table></div>
<h2>Endpoints <span class="mut">— API/auth calls, slowest first</span></h2><div class="card"><table><tr><th>Endpoint</th>{head}<th>Median</th><th>p90</th><th>Server wait</th><th>Variation</th><th>Calls</th></tr>
{''.join(f"<tr><td class='mono'>{e(x['k'][:110])}</td>" + ''.join(f"<td>{fmt('x', v)}</td>" for v in x['runs']) + f"<td><b>{fmt('x', x['med'])}</b></td><td>{fmt('x', x['p90'])}</td><td>{fmt('x', x['ttfb'])}</td><td>{x['cvs']}</td><td>{len(x['all'])}</td></tr>" for x in erows)}</table></div>
</main></body></html>"""
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"{len(runs)} runs, {len(rows)} metrics, {len(erows)} endpoints -> {a.out}")


if __name__ == "__main__":
    main()
