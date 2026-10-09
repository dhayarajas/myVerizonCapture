#!/usr/bin/env python3
"""Export an annotated video: the phone recording, a live panel (time, screen, phase, spinner, user events,
no-network gaps, requests in flight, session mini-timeline) and a history panel (rolling waterfall of the
last WINDOW seconds + event log), so past requests stay visible as the video plays.

Reads the session's report.html data (run report.sh first). Output: <session>/annotated.mp4
Usage: python3 annotate_video.py [session-dir]                      -> annotated.mp4 (2320x1304) + annotated-1080p.mp4
       python3 annotate_video.py --compare <sessionA> <sessionB> [-o out.mp4]   (side-by-side, aligned at launch)
Idle stretches play fast-forwarded, taps / Face ID / request fan-out in slow motion; intro and summary cards,
chapters (per screen) and an events subtitle track are embedded.
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from multiprocessing import Pool

from PIL import Image, ImageDraw, ImageFont

TOOL = os.path.dirname(os.path.abspath(__file__))
PH, PW = 1304, 760  # output height, panel width (phone is scaled to the same height)
HW, WINDOW, AHEAD = 960, 15.0, 0.6  # history panel width, seconds of history shown, blank space right of the playhead
CAT = {"api": "#2563eb", "auth": "#7c3aed", "image": "#0d9488", "asset": "#64748b", "logging": "#d97706", "analytics": "#a3a3a3"}
UEV = {"launch": ("#0f766e", "LAUNCH"), "tap": ("#e11d48", "TAP"), "drag": ("#ea580c", "DRAG"), "bio": ("#7c3aed", "FACE ID"),
       "keyboard": ("#0369a1", "KEYBOARD"), "overlay": ("#64748b", "SYSTEM OVERLAY")}
GAP = {"wait": "#ef4444", "bio": "#7c3aed", "user": "#0891b2", "idle": "#94a3b8"}


def font(size, mono=False, bold=False):
    for p in (["/System/Library/Fonts/SFNSMono.ttf", "/System/Library/Fonts/Menlo.ttc"] if mono else
              ["/System/Library/Fonts/SFNS.ttf", "/System/Library/Fonts/Helvetica.ttc"]):
        try:
            f = ImageFont.truetype(p, size)
            if bold and not mono:
                try:
                    f.set_variation_by_name("Bold")
                except Exception:
                    pass
            return f
        except OSError:
            continue
    return ImageFont.load_default()


def fmt(t):
    ms = int(round(max(0, t) * 1000))
    return f"{ms // 60000:02d}:{ms // 1000 % 60:02d}.{ms % 1000:03d}"


def short(r):
    return (r["method"] + " " + r["short"] + (f" [{r['op']}]" if r.get("op") else ""))[:46]


def load(session):
    html = open(os.path.join(session, "report.html"), encoding="utf-8").read()
    i = html.index("const D=") + len("const D=")
    return json.JSONDecoder().raw_decode(html[i:])[0]


def panel(D, t, F):
    im = Image.new("RGB", (PW, PH), "#0f172a")
    d = ImageDraw.Draw(im)
    R = D["reqs"]
    y = 28
    d.text((28, y), "MyVerizon App Performance", fill="#94a3b8", font=F["s"])
    y += 34
    d.text((28, y), fmt(t), fill="#ffffff", font=F["clock"])
    y += 78
    scr = next((s for s in D.get("screens", []) if s["start"] - 0.05 <= t < s["end"] + 0.05), None) or \
        max((s for s in D.get("screens", []) if s["start"] <= t), key=lambda s: s["start"], default=None)
    d.text((28, y), "SCREEN", fill="#64748b", font=F["xs"])
    d.text((28, y + 20), (scr["name"] if scr else "—")[:34], fill="#e2e8f0", font=F["m"])
    y += 66
    cold = D.get("cold")
    ph = next((p for p in (cold or {}).get("phases", []) if p["start"] <= t < p["end"]), None)
    per = next((p for p in D.get("periods", []) if p["start"] <= t < p["end"]), None)
    if ph:
        d.rounded_rectangle((28, y, PW - 28, y + 44), 8, fill="#312e81")
        d.text((42, y + 10), f"COLD START  {ph['name']}  +{t - ph['start']:.2f}s / {ph['dur']:.2f}s"[:58], fill="#e0e7ff", font=F["s"])
        y += 54
    if per:
        frac = (t - per["start"]) / max(0.01, per["dur"])
        d.rounded_rectangle((28, y, PW - 28, y + 44), 8, fill="#78350f")
        d.rounded_rectangle((28, y, 28 + int((PW - 56) * frac), y + 44), 8, fill="#b45309")
        d.text((42, y + 10), f"SPINNER  {t - per['start']:.2f}s / {per['dur']:.2f}s  ·  {per.get('name', '')}"[:58], fill="#fff7ed", font=F["s"])
        y += 54
    gap = next((g for g in D.get("gaps", []) if g["start"] <= t < g["end"]), None)
    if gap:
        d.rounded_rectangle((28, y, PW - 28, y + 44), 8, outline=GAP.get(gap["kind"], "#94a3b8"), width=3)
        d.text((42, y + 10), f"NO NETWORK  {t - gap['start']:.2f}s / {gap['dur']:.2f}s  ·  {gap['what']}"[:62], fill="#fecaca" if gap["kind"] == "wait" else "#e2e8f0", font=F["s"])
        y += 54
    for u in D.get("uev", []):
        hold = max(1.2, u["end"] - u["t"] + 0.6)
        if u["t"] - 0.05 <= t <= u["t"] + hold:
            c, lab = UEV.get(u["kind"], ("#475569", u["kind"].upper()))
            d.rounded_rectangle((28, y, PW - 28, y + 44), 8, fill=c)
            extra = ""
            if u.get("reqAfter") is not None:
                extra += f" → request +{u['reqAfter']*1000:.0f}ms"
            if u.get("screenAfter") is not None:
                extra += f", screen +{u['screenAfter']*1000:.0f}ms"
            d.text((42, y + 10), f"{lab}{(' on ' + u['screen']) if u.get('screen') else ''}{extra}"[:60], fill="#ffffff", font=F["s"])
            y += 54
    y += 4
    if cold and cold["launch"] - 0.5 <= t <= cold["launch"] + cold["total"] + 2 and y <= 300:
        d.text((28, y), f"COLD START  {min(max(0, t - cold['launch']), cold['total']):.2f}s / {cold['total']:.2f}s", fill="#a5b4fc", font=F["xs"])
        y += 24
        x0r, x1r, L0 = 28, PW - 28, cold["launch"]
        cl = ["#7c3aed", "#2563eb", "#ef4444", "#d97706", "#0d9488"]
        for i_, p_ in enumerate(cold["phases"]):
            a_ = x0r + (x1r - x0r) * (p_["start"] - L0) / cold["total"]
            b_ = x0r + (x1r - x0r) * (p_["end"] - L0) / cold["total"]
            d.rectangle((a_, y, b_, y + 16), fill=cl[i_ % 5] if t >= p_["start"] else "#334155")
        cx = x0r + (x1r - x0r) * min(1, max(0, (t - L0) / cold["total"]))
        d.polygon([(cx - 7, y - 7), (cx + 7, y - 7), (cx, y + 2)], fill="#ffffff")
        y += 22
        d.text((28, y), "  →  ".join(p_["to"] for p_ in cold["phases"])[:64], fill="#64748b", font=F["xs"])
        y += 30
    sc_ = next((c_ for c_ in D.get("screenCost", []) if c_["start"] <= t < c_["end"] + 0.05), None)
    if sc_ and y <= 350:
        rs_ = [r for r in R if r["cat"] != "tunnel" and sc_["start"] <= r.get("issued", r["start"]) <= t]
        api_ = [r for r in rs_ if r["cat"] in ("api", "auth")]
        kb = sum(r["size"] for r in rs_ if r["end"] <= t) / 1024
        spin_ = sum(max(0, min(p_["end"], t) - max(p_["start"], sc_["start"])) for p_ in D.get("periods", []) if p_["start"] < t)
        d.text((28, y), f"THIS SCREEN SO FAR  ({t - sc_['start']:.1f}s on {sc_['name'][:26]})", fill="#64748b", font=F["xs"])
        y += 24
        d.text((28, y), f"{len(api_)} API · {sum(1 for r in rs_ if r['cat'] == 'analytics')} analytics · {kb:.0f} KB received"
               + (f" · spinner {spin_:.1f}s" if spin_ >= 0.05 else ""), fill="#cbd5e1", font=F["xs2"])
        y += 30
    y = max(y + 10, 400)
    act = [r for r in R if r["cat"] not in ("tunnel",) and r.get("issued", r["start"]) <= t < r["end"]]
    na = sum(1 for r in act if r["cat"] == "analytics")
    d.text((28, y), f"Requests in flight: {len(act) - na}" + (f"  (+{na} analytics)" if na else "") + "   →  history", fill="#64748b", font=F["xs"])
    y += 34
    # ---- what is blocking the screen right now
    blk = blocking_now(D, t)
    if blk:
        lines = wrap(blk["what"], F["s"], PW - 84)
        fix = wrap("FIX: " + blk["fix"], F["xs2"], PW - 84) if blk.get("fix") else []
        h = 44 + 26 * len(lines) + 30 + 24 * len(fix) + (26 if blk.get("save") else 0)
        d.rounded_rectangle((28, y, PW - 28, y + h), 10, fill="#2a0f12", outline="#ef4444", width=3)
        d.text((44, y + 10), "BLOCKING THE SCREEN NOW", fill="#fca5a5", font=F["xs"])
        d.text((PW - 230, y + 10), blk["span"][:22], fill="#fca5a5", font=F["xs"])
        yy = y + 36
        for ln in lines:
            d.text((44, yy), ln, fill="#ffffff", font=F["s"])
            yy += 26
        frac = min(1, max(0, (t - blk["start"]) / max(0.01, blk["end"] - blk["start"])))
        d.rounded_rectangle((44, yy + 4, PW - 44, yy + 18), 5, fill="#3f1d22")
        d.rounded_rectangle((44, yy + 4, 44 + int((PW - 88) * frac), yy + 18), 5, fill="#ef4444")
        d.text((PW - 230, yy - 1), f"{t - blk['start']:.2f}s / {blk['end'] - blk['start']:.2f}s", fill="#fecaca", font=F["mono"])
        yy += 28
        for ln in fix:
            d.text((44, yy), ln, fill="#86efac", font=F["xs2"])
            yy += 24
        if blk.get("save"):
            d.text((44, yy), f"Estimated saving: up to {blk['save']:.2f}s", fill="#4ade80", font=F["xs2"])
        y += h + 14
    # ---- findings so far
    sev = {"high": 0, "med": 1, "low": 2, "info": 3}
    col = {"high": "#dc2626", "med": "#d97706", "low": "#2563eb", "info": "#6b7280"}
    seen = [f for f in D.get("findings", []) if f.get("t") is not None and f["t"] <= t and f["sev"] in ("high", "med")]
    seen.sort(key=lambda f: (sev[f["sev"]], -(f.get("impact") or 0)))
    if seen:
        d.text((28, y), f"KEY FINDINGS SO FAR  ({len(seen)})", fill="#94a3b8", font=F["xs"])
        y += 28
    limit = PH - 150

    def short_title(f):
        x = re.sub(r"\s*\(\d+\.\d+s → \d+\.\d+s\)", "", f["title"])
        return x.replace("Slow call on critical path: ", "Slow: ").replace(" on the device before being sent", " on device")

    def rows_for(mode):
        out = []
        for f in seen:
            if mode == "full" or (mode == "mixed" and f["sev"] == "high"):
                tl = wrap(f["title"], F["xs2"], PW - 200)
                title = tl[:2] if len(tl) <= 2 else [tl[0], tl[1] + " …"]
                fl = wrap("→ " + f["rec"], F["xs"], PW - 150)
                fix = fl[:1] if len(fl) <= 1 else [fl[0] + " …"]
            else:
                tl = wrap(short_title(f), F["xs2"], PW - 200)
                title, fix = [tl[0] + (" …" if len(tl) > 1 else "")], []
            out.append((f, title, fix, 10 + 23 * len(title) + 21 * len(fix) + 8))
        return out
    room = limit - y
    for mode in ("full", "mixed", "compact"):
        rows = rows_for(mode)
        if sum(h + 8 for *_, h in rows) <= room:
            break
    shown, more, used = [], [], 0
    fresh = [r for r in rows if t - r[0]["t"] <= 2.0]
    for r in rows:
        need = r[3] + 8 + (sum(x[3] + 8 for x in fresh if x not in shown and x is not r) if r not in fresh else 0)
        if used + need + 34 <= room or r in fresh:
            shown.append(r)
            used += r[3] + 8
        else:
            more.append(r[0])
    for f, title, fix, h in shown:
        new = t - f["t"] <= 2.0
        d.rounded_rectangle((28, y, PW - 28, y + h), 8, fill="#1e293b" if not new else "#334155", outline=col[f["sev"]] if new else None, width=2)
        d.rounded_rectangle((38, y + 9, 92, y + 31), 5, fill=col[f["sev"]])
        d.text((44, y + 11), f["sev"].upper(), fill="#ffffff", font=F["xs"])
        yy = y + 8
        for ln in title:
            d.text((102, yy), ln, fill="#ffffff", font=F["xs2"])
            yy += 23
        for ln in fix:
            d.text((102, yy), ln, fill="#86efac", font=F["xs"])
            yy += 21
        if new:
            d.text((PW - 84, y + 9), "NEW", fill="#fde047", font=F["xs"])
        y += h + 8
    if more:
        txt = wrap(f"+{len(more)} more: " + " · ".join(short_title(f) for f in more), F["xs"], PW - 70)
        for ln in txt[:2]:
            d.text((32, y), ln if len(txt) <= 2 or ln is not txt[1] else ln + " …", fill="#94a3b8", font=F["xs"])
            y += 22
    # mini timeline
    ty, x0, x1, end = PH - 92, 28, PW - 28, max(0.1, D["end"])
    X = lambda v: x0 + (x1 - x0) * min(1, max(0, v / end))
    d.text((x0, ty - 28), "SESSION", fill="#64748b", font=F["xs"])
    d.rounded_rectangle((x0, ty, x1, ty + 26), 5, fill="#1e293b")
    for p in D.get("periods", []):
        d.rectangle((X(p["start"]), ty, X(p["end"]), ty + 26), fill="#b45309")
    for g in D.get("gaps", []):
        d.rectangle((X(g["start"]), ty + 20, X(g["end"]), ty + 26), fill=GAP.get(g["kind"], "#94a3b8"))
    for u in D.get("uev", []):
        if u["kind"] in ("tap", "launch", "bio"):
            d.ellipse((X(u["t"]) - 4, ty + 9, X(u["t"]) + 4, ty + 17), fill=UEV[u["kind"]][0])
    d.rectangle((X(t) - 1, ty - 6, X(t) + 1, ty + 32), fill="#ef4444")
    d.text((x0, ty + 34), fmt(0), fill="#64748b", font=F["mono"])
    d.text((x1 - 110, ty + 34), fmt(end), fill="#64748b", font=F["mono"])
    return im


def fit(text, f, width):
    if f.getlength(text) <= width:
        return text
    while text and f.getlength(text + "…") > width:
        text = text[:-1]
    return (text + "…") if text else ""


def wrap(text, f, width):
    words, lines, cur = text.split(), [], ""
    for w in words:
        nxt = (cur + " " + w).strip()
        if f.getlength(nxt) <= width:
            cur = nxt
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


FIXES = {
    "slow": "Profile server time for this endpoint (it is almost all server wait); front it with the CDN/edge or fix origin routing.",
    "slow-nocdn": "This host has no CDN in front — serve it from the edge / a closer region and profile the server time.",
    "queue": "Requests wait for a free connection (6 per host over HTTP/1.1): use HTTP/2 multiplexing, or fewer / combined tile calls.",
    "logging": "Don't await logging/telemetry before the next business call — make it fire-and-forget or batch it.",
    "device": "Nothing is on the network: start the next request as soon as the response arrives and move parsing/layout off the main thread.",
    "bio": "Face ID prompt (user) — not app time; keep the work before and after the prompt minimal.",
    "conn": "New connection setup on the critical path — pre-warm or reuse connections to this host.",
}


def blocking_now(D, t):
    """The critical-path step that is holding the screen at time t (spinner or render)."""
    R = D["reqs"]
    spans = [(p, p.get("name") or "loading") for p in D.get("periods", [])] + \
            [(b, b.get("name") or "render") for b in D.get("bursts", []) if b.get("chain")]
    for span, name in spans:
        if not (span["start"] <= t < span["end"]):
            continue
        for c in span.get("chain", []):
            if not (c["start"] <= t < c["end"]):
                continue
            save = None
            if c["kind"] == "req":
                r = R[c["id"]]
                ph = r.get("ph")
                what = f"Waiting on {r['method']} {r['short']}{(' [' + r['op'] + ']') if r.get('op') else ''} ({r['host']})"
                if ph:
                    what += f" — server wait {ph['ttfb']:.2f}s of {r['dur']:.2f}s"
                if r["cat"] in ("logging", "analytics"):
                    key = "logging"
                elif ph and ph["setup"] >= 0.1 and t < r["start"] + ph["setup"]:
                    key = "conn"
                else:
                    key = "slow" if r.get("cdn") else "slow-nocdn"
                for sv in D.get("savings", []):
                    if c["id"] in sv.get("refs", []):
                        save = max(save or 0, sv["save"])
            elif c["kind"] == "queue":
                r = R[c["id"]]
                what, key = f"Queued on the phone: {r['short']} is waiting for a free connection to {r['host']}", "queue"
                save = next((sv["save"] for sv in D.get("savings", []) if c["id"] in sv.get("refs", [])), None)
            else:
                bio = any(b0 <= t < b1 for b0, b1 in c.get("bio", []))
                if bio:
                    what, key = "Face ID prompt on screen (user authenticating)", "bio"
                else:
                    what, key = "Nothing on the network — the app is working on the device", "device"
                    save = next((sv["save"] for sv in D.get("savings", []) if sv["title"].startswith("On-device")), None)
            return {"what": what, "fix": FIXES.get(key), "start": c["start"], "end": c["end"], "span": name, "save": save, "id": c.get("id")}
    return None


def build_events(D):
    """Chronological event log (only what has happened by time t is ever shown)."""
    R, ev = D["reqs"], []
    for r in R:
        if r["cat"] == "tunnel":
            continue
        st = r.get("issued", r["start"])
        lab = short(r)
        if r["cat"] == "analytics":
            continue
        ev.append({"t": st, "kind": "req", "c": CAT.get(r["cat"], "#64748b"), "text": f"→ {lab}" + (" (queued)" if r["start"] - st > 0.1 else "")})
        bad = r["status"] >= 400 or r["status"] == 0
        size = f"{r['size']/1024:.1f}KB" if r["size"] >= 1024 else f"{r['size']}B"
        ev.append({"t": r["end"], "kind": "done", "c": "#ef4444" if bad else CAT.get(r["cat"], "#64748b"),
                   "text": f"{'✗' if bad else '✓'} {lab}  {r['status']} · {size} · {r['dur']:.2f}s"})
    for sc in D.get("screens", []):
        ev.append({"t": sc["start"], "kind": "screen", "c": "#38bdf8", "text": f"▣ Screen: {sc['name']}"})
    for p in D.get("periods", []):
        ev.append({"t": p["start"], "kind": "spin", "c": "#f59e0b", "text": f"◌ Spinner shown ({p.get('name', '')})"})
        ev.append({"t": p["end"], "kind": "spin", "c": "#f59e0b", "text": f"◉ Spinner gone after {p['dur']:.2f}s"})
    for g in D.get("gaps", []):
        if g["kind"] in ("wait", "bio"):
            ev.append({"t": g["start"], "kind": "gap", "c": GAP[g["kind"]], "text": f"∅ No network for {g['dur']:.2f}s — {g['what']}"[:80]})
    for u in D.get("uev", []):
        c, lab = UEV.get(u["kind"], ("#475569", u["kind"].upper()))
        if u["kind"] == "bio":
            ev.append({"t": u["t"], "kind": "uev", "c": c, "text": "🔐 Face ID requested"})
            for st in u.get("steps", []):
                ev.append({"t": st["t"], "kind": "uev", "c": c, "text": f"   Face ID {st['k']}"})
        elif u["kind"] != "overlay":
            ev.append({"t": u["t"], "kind": "uev", "c": c, "text": f"{lab.title()} on {u.get('screen') or '?'}"
                       + (f" (feedback {u['feedback']*1000:.0f}ms)" if u.get("feedback") is not None else "")})
    for m in D.get("marks", []):
        ev.append({"t": m["t"], "kind": "mark", "c": "#a78bfa", "text": f"★ App: {m['label']}"})
    ev.sort(key=lambda e: e["t"])
    return ev


def history(D, t, F, events):
    im = Image.new("RGB", (HW, PH), "#0b1220")
    d = ImageDraw.Draw(im)
    d.line((0, 0, 0, PH), fill="#1e293b", width=2)
    d.text((24, 22), f"HISTORY — last {WINDOW:.0f} s", fill="#94a3b8", font=F["s"])
    L, Rr, top = 24, HW - 24, 64
    t0 = t - WINDOW
    X = lambda v: L + (Rr - L) * (v - t0) / (WINDOW + AHEAD)
    px = X(t)
    # time grid
    g0 = int(t0) if t0 > 0 else 0
    for k in range(g0, int(t) + 2):
        if t0 <= k <= t + AHEAD:
            d.line((X(k), top, X(k), 832), fill="#1e293b")
            if k % 2 == 0 or WINDOW <= 10:
                d.text((X(k) + 3, top), fmt(k)[:-4], fill="#475569", font=F["mono"])
    y = top + 26
    # screens strip
    for sc in D.get("screens", []):
        a, b = max(sc["start"], t0), min(sc["end"], t)
        if b > a:
            d.rounded_rectangle((X(a), y, X(b), y + 26), 4, fill="#78350f" if sc["loading"] else "#1e3a8a")
            if X(b) - X(a) > 22:
                d.text((X(a) + 5, y + 3), fit(sc["name"], F["xs"], X(b) - X(a) - 10), fill="#e2e8f0", font=F["xs"])
    y += 32
    # gaps + face id + taps lane
    for g in D.get("gaps", []):
        a, b = max(g["start"], t0), min(g["end"], t)
        if b > a:
            d.rectangle((X(a), y, X(b), y + 12), fill=GAP.get(g["kind"], "#94a3b8"))
    for u in D.get("uev", []):
        if t0 <= u["t"] <= t:
            c = UEV.get(u["kind"], ("#475569", ""))[0]
            if u["kind"] in ("bio", "overlay"):
                d.rectangle((X(u["t"]), y + 14, X(min(u["end"], t)), y + 24), fill=c)
            else:
                d.ellipse((X(u["t"]) - 7, y + 12, X(u["t"]) + 7, y + 26), fill=c)
                d.line((X(u["t"]), y + 26, X(u["t"]), 832), fill=c, width=1)
    y += 36
    # rolling waterfall
    blk = blocking_now(D, t)
    blk_id = blk.get("id") if blk else None
    crit = {c["id"] for sp in D.get("periods", []) + [b for b in D.get("bursts", []) if b.get("chain")]
            if sp["start"] - 1 <= t <= sp["end"] + 5 for c in sp.get("chain", []) if c["kind"] == "req"}
    rows = [r for r in D["reqs"] if r["cat"] not in ("tunnel", "analytics") and r.get("issued", r["start"]) <= t and r["end"] >= t0]
    rows.sort(key=lambda r: r.get("issued", r["start"]))
    RH, maxrows = 24, int((832 - y) / 24)
    hidden = max(0, len(rows) - maxrows)
    rows = rows[hidden:]
    if hidden:
        d.text((L, y - 18), f"↑ {hidden} earlier request(s) in window", fill="#475569", font=F["xs"])
    for r in rows:
        st = r.get("issued", r["start"])
        a = max(st, t0)
        live = r["end"] > t
        c = CAT.get(r["cat"], "#64748b")
        if r["start"] > a:  # queued part
            d.rectangle((X(a), y + 6, X(min(r["start"], t)), y + RH - 6), fill="#f472b6")
        if t > r["start"]:
            x1 = X(min(r["end"], t))
            d.rounded_rectangle((X(max(r["start"], t0)), y + 3, max(X(max(r["start"], t0)) + 3, x1), y + RH - 3), 4,
                                fill="#ef4444" if (r["status"] >= 400 or r["status"] == 0) and not live else c)
            if live:
                d.rectangle((x1 - 4, y + 3, x1, y + RH - 3), fill="#ffffff")
            if r["id"] == blk_id:
                d.rounded_rectangle((X(max(r["start"], t0)) - 3, y, max(X(max(r["start"], t0)) + 6, x1 + 3), y + RH), 5, outline="#ef4444", width=3)
            elif r["id"] in crit:
                d.rounded_rectangle((X(max(r["start"], t0)) - 2, y + 1, max(X(max(r["start"], t0)) + 5, x1 + 2), y + RH - 1), 5, outline="#fca5a5", width=1)
        lab = short(r)
        dur = f"{(t - st) if live else r['dur']:.2f}s" + ("…" if live else "")
        tx = X(max(st, t0)) + 4
        txt = f"{lab}  {dur}"
        w = F["xs2"].getlength(txt)
        if tx + w > px - 6:
            tx = max(L, X(max(st, t0)) - w - 6) if X(max(st, t0)) - w - 6 > L else tx
        d.text((tx, y + 3), txt, fill="#ffffff" if live else "#cbd5e1", font=F["xs2"])
        y += RH
    # playhead
    d.line((px, top + 20, px, 832), fill="#ef4444", width=2)
    d.rounded_rectangle((px - 58, 836, px + 58, 860), 5, fill="#ef4444")
    d.text((px - 52, 838), fmt(t), fill="#ffffff", font=F["mono"])
    # event log
    ly = 880
    d.line((L, ly - 8, Rr, ly - 8), fill="#1e293b", width=2)
    d.text((L, ly), "EVENT LOG", fill="#94a3b8", font=F["s"])
    ly += 34
    past = [e for e in events if e["t"] <= t]
    n = int((PH - ly - 12) / 26)
    for k, e in enumerate(past[-n:]):
        age = t - e["t"]
        fresh = age < 0.6
        if fresh:
            d.rounded_rectangle((L - 6, ly - 2, Rr, ly + 23), 4, fill="#1e293b")
        fade = max(0.45, 1 - age / 25)
        col = tuple(int(int(e["c"][i:i + 2], 16) * fade + 15 * (1 - fade)) for i in (1, 3, 5))
        d.text((L, ly), fmt(e["t"]), fill="#64748b" if not fresh else "#e2e8f0", font=F["mono"])
        d.text((L + 118, ly), e["text"][:78], fill=col if not fresh else "#ffffff", font=F["xs2"])
        ly += 26
    return im


# ---------------------------------------------------------------- speed map (fast-forward idle, slow-mo key moments)

FPS, FAST, SLOW = 30, 4.0, 0.5
PHW = 600  # phone width in the composite (1206x2622 scaled to height 1304)
W = PHW + PW + HW


def segments(D):
    """[(src_start, src_end, speed, label)] covering the whole recording."""
    end, step = D["end"], 0.05
    n = int(end / step) + 1
    speed = [1.0] * n
    reason = [""] * n

    def mark(a, b, v, why, override=False):
        for i in range(max(0, int(a / step)), min(n, int(b / step) + 1)):
            if override or speed[i] == 1.0:
                speed[i], reason[i] = v, why
    launch = next((u["t"] for u in D.get("uev", []) if u["kind"] == "launch"), None)
    if launch and launch > 3:
        mark(0, launch - 0.6, FAST, "idle home screen")
    for g in D.get("gaps", []):
        if g["kind"] in ("idle", "user") and g["dur"] >= 2.0:
            mark(g["start"] + 0.4, (g["end"] if g.get("tail") else g["end"] - 0.4), FAST, "user idle" if g["kind"] == "idle" else "user on screen")
    for u in D.get("uev", []):
        if u["kind"] in ("tap", "launch", "drag") and not u.get("inferred"):
            mark(u["t"] - 0.25, u["t"] + 0.9, SLOW, "tap", True)
        elif u["kind"] == "bio":
            mark(u["t"] - 0.2, u["end"] + 0.3, SLOW, "Face ID", True)
    issued = sorted(r.get("issued", r["start"]) for r in D["reqs"] if r["cat"] in ("api", "auth"))
    for i, t0 in enumerate(issued):
        if sum(1 for x in issued[i:] if x - t0 <= 0.4) >= 5:
            mark(t0 - 0.2, t0 + 1.0, SLOW, "request fan-out", True)
            break
    segs, a = [], 0.0
    for i in range(1, n + 1):
        if i == n or speed[i] != speed[i - 1] or reason[i] != reason[i - 1]:
            b = min(end, i * step)
            if b - a > 0.01:
                segs.append((round(a, 3), round(b, 3), speed[i - 1], reason[i - 1]))
            a = b
    return segs


def out_frames(segs):
    """Every output frame: (src_time, speed, label)."""
    fr = []
    for a, b, v, why in segs:
        k = 0
        while True:
            t = a + k / FPS * v
            if t >= b - 1e-9:
                break
            fr.append((t, v, why))
            k += 1
    return fr


def src_to_out(segs, t):
    o = 0.0
    for a, b, v, _ in segs:
        if t <= a:
            break
        o += (min(t, b) - a) / v
    return o


# ---------------------------------------------------------------- composite frame (panels + phone annotations)

def phone_overlay(d, D, t, v, why, F):
    """Annotations drawn over the phone recording (left PHW px)."""
    for u in D.get("uev", []):
        if u["kind"] in ("tap", "launch", "drag") and u["t"] - 0.02 <= t <= u["t"] + 0.6:
            c = UEV[u["kind"]][0]
            for k in range(12):
                d.rectangle((k, k, PHW - 1 - k, PH - 1 - k), outline="#ffffff" if k < 4 else c)
            d.rounded_rectangle((PHW // 2 - 90, PH - 150, PHW // 2 + 90, PH - 108), 10, fill=c)
            d.text((PHW // 2 - 70, PH - 145), ("TAP" if u["kind"] == "tap" else u["kind"].upper()) + (f"  +{(t - u['t'])*1000:.0f}ms"), fill="#fff", font=F["s"])
        if u["kind"] == "bio" and u["t"] <= t <= u["end"]:
            for k in range(8):
                d.rectangle((k, k, PHW - 1 - k, PH - 1 - k), outline="#7c3aed")
    chips = []
    per = next((p for p in D.get("periods", []) if p["start"] <= t < p["end"]), None)
    if per:
        chips.append((f"SPINNER {t - per['start']:.1f}s", "#b45309"))
    gap = next((g for g in D.get("gaps", []) if g["start"] <= t < g["end"] and g["kind"] in ("wait", "bio")), None)
    if gap:
        chips.append(("NO NETWORK", "#dc2626"))
    y = 64
    for txt, c in chips:
        w = F["s"].getlength(txt) + 24
        d.rounded_rectangle((16, y, 16 + w, y + 38), 9, fill=c)
        d.text((28, y + 7), txt, fill="#fff", font=F["s"])
        y += 46
    if v != 1.0:
        txt = (f"{v:g}× FAST-FORWARD" if v > 1 else f"{v:g}× SLOW MOTION") + (f" · {why}" if why else "")
        w = F["s"].getlength(txt) + 24
        c = "#0369a1" if v > 1 else "#a21caf"
        d.rounded_rectangle((PHW - 16 - w, PH - 64, PHW - 16, PH - 24), 9, fill=c)
        d.text((PHW - 4 - w, PH - 57), txt, fill="#fff", font=F["s"])


_W = {}


def _init(session):
    _W["D"] = load(session)
    _W["F"] = fonts()
    _W["ev"] = build_events(_W["D"])


def _render(job):
    i, t, v, why, out = job
    D, F, ev = _W["D"], _W["F"], _W["ev"]
    im = Image.new("RGBA", (W, PH), (0, 0, 0, 0))
    phone_overlay(ImageDraw.Draw(im), D, t, v, why, F)
    im.paste(panel(D, t, F), (PHW, 0))
    im.paste(history(D, t, F, ev), (PHW + PW, 0))
    im.save(os.path.join(out, f"o{i:05d}.png"), compress_level=1)


def fonts():
    return {"clock": font(64, mono=True), "m": font(30, bold=True), "s": font(21), "xs": font(17), "xs2": font(18), "mono": font(18, mono=True),
            "h1": font(58, bold=True), "h2": font(34, bold=True), "big": font(54, mono=True), "l": font(26)}


# ---------------------------------------------------------------- intro / summary cards

def card_intro(D, F, segs):
    im = Image.new("RGB", (W, PH), "#0b1220")
    d = ImageDraw.Draw(im)
    m, b, c = D.get("meta", {}), D.get("build", {}), D.get("cond", {})
    d.text((90, 380), "MyVerizon App Performance", fill="#ffffff", font=F["h1"])
    d.text((90, 460), f"{m.get('device', '')} · recorded {m.get('startISO', '')} · app {b.get('appVersion', '?')} ({b.get('build', '?')}) · iOS {b.get('os', '?')} · network {c.get('network', 'default')}",
           fill="#94a3b8", font=F["l"])
    K = D.get("kpi", {})
    cold = (D.get("cold") or {}).get("total")
    sign = next((p["dur"] for p in D.get("periods", []) if "Sign-in" in p.get("name", "")), None)
    bud = D.get("budgets") or []
    tiles = [("Cold start", f"{cold:.2f}s" if cold else "—", cold and cold >= 3), ("Sign-in spinner", f"{sign:.2f}s" if sign else "—", sign and sign >= 6),
             ("Spinner on screen", f"{K.get('loading', 0)}s", K.get("loading", 0) >= 10), ("API p95", f"{K.get('apiP95', 0)}s", K.get("apiP95", 0) >= 2),
             ("Requests", str(K.get("requests", "")), False),
             ("Budgets met", f"{sum(1 for x in bud if x.get('pass'))}/{sum(1 for x in bud if x.get('pass') is not None)}" if bud else "—",
              bud and any(x.get("pass") is False for x in bud))]
    x, y = 90, 560
    for lab, val, bad in tiles:
        d.rounded_rectangle((x, y, x + 340, y + 170), 18, fill="#1e293b")
        d.text((x + 28, y + 30), val, fill="#f87171" if bad else "#ffffff", font=F["big"])
        d.text((x + 28, y + 112), lab, fill="#94a3b8", font=F["l"])
        x += 365
        if x + 340 > W - 60:
            x, y = 90, y + 195
    return im


def card_summary(D, F):
    im = Image.new("RGB", (W, PH), "#0b1220")
    d = ImageDraw.Draw(im)
    R = D["reqs"]
    d.text((90, 70), "Summary — what to fix first", fill="#ffffff", font=F["h1"])
    col = {"high": "#dc2626", "med": "#d97706", "low": "#2563eb", "info": "#6b7280"}
    sev = {"high": 0, "med": 1, "low": 2, "info": 3}
    fs = sorted([f for f in D.get("findings", []) if f["sev"] in ("high", "med")], key=lambda f: (sev[f["sev"]], -(f.get("impact") or 0)))[:7]
    y = 170
    d.text((90, y), "Top findings", fill="#e2e8f0", font=F["h2"])
    y += 58
    for f in fs:
        d.rounded_rectangle((90, y, 1300, y + 104), 12, fill="#1e293b")
        d.rounded_rectangle((110, y + 18, 200, y + 52), 7, fill=col[f["sev"]])
        d.text((122, y + 22), f["sev"].upper(), fill="#fff", font=F["xs2"])
        d.text((220, y + 14), fit(f["title"], F["l"], 1060), fill="#ffffff", font=F["l"])
        d.text((220, y + 58), fit("Fix: " + f["rec"], F["s"], 1060), fill="#86efac", font=F["s"])
        y += 118
    sv = (D.get("savings") or [])[:5]
    x0, y = 1360, 170
    d.text((x0, y), "Biggest wins (upper bound)", fill="#e2e8f0", font=F["h2"])
    y += 64
    mx = max([x["save"] for x in sv] or [1])
    for x in sv:
        d.text((x0, y), fit(x["title"], F["s"], 880), fill="#cbd5e1", font=F["s"])
        d.rounded_rectangle((x0, y + 34, x0 + int(700 * x["save"] / mx), y + 58), 6, fill="#16a34a")
        d.text((x0 + int(700 * x["save"] / mx) + 14, y + 32), f"−{x['save']:.2f}s", fill="#4ade80", font=F["l"])
        y += 92
    bud = D.get("budgets") or []
    if bud:
        y += 20
        d.text((x0, y), "Budgets", fill="#e2e8f0", font=F["h2"])
        y += 58
        for b in bud:
            ok = b.get("pass")
            mark = "n/a" if ok is None else ("✓" if ok else "✗")
            d.text((x0, y), f"{mark}  {b['label']}: " + ("—" if b.get("value") is None else f"{b['value']:.2f}s / {b['max']:.2f}s"),
                   fill="#4ade80" if ok else "#f87171" if ok is False else "#94a3b8", font=F["s"])
            y += 36
    return im


# ---------------------------------------------------------------- chapters & subtitles

def srt_time(x):
    ms = int(round(x * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def write_chapters(D, segs, intro, total, path):
    ch = [(0.0, "Intro")]
    last = None
    for sc in D.get("screens", []):
        nm = sc["name"].replace(" (loading)", "")
        if nm != last:
            ch.append((intro + src_to_out(segs, sc["start"]), nm + (" (spinner)" if sc.get("loading") else "")))
            last = nm
    ch.append((intro + src_to_out(segs, D["end"]), "Summary"))
    ch = [c for i, c in enumerate(ch) if i == 0 or c[0] - ch[i - 1][0] > 0.3 or c[1] == "Summary"]
    with open(path, "w") as f:
        f.write(";FFMETADATA1\ntitle=MyVerizon App Performance\n")
        for i, (a, name) in enumerate(ch):
            b = ch[i + 1][0] if i + 1 < len(ch) else total
            f.write(f"[CHAPTER]\nTIMEBASE=1/1000\nSTART={int(a*1000)}\nEND={int(max(a + 0.1, b)*1000)}\ntitle={name}\n")
    return ch


def write_srt(D, segs, intro, path):
    ev = [e for e in build_events(D) if e["kind"] != "req"]
    with open(path, "w", encoding="utf-8") as f:
        n = 0
        for i, e in enumerate(ev):
            a = intro + src_to_out(segs, e["t"])
            b = a + 1.6
            group = [x["text"] for x in ev if abs(x["t"] - e["t"]) < 0.05]
            if i and abs(ev[i - 1]["t"] - e["t"]) < 0.05:
                continue
            n += 1
            f.write(f"{n}\n{srt_time(a)} --> {srt_time(b)}\n{fmt(e['t'])}  " + "\n".join(group[:3]) + "\n\n")


# ---------------------------------------------------------------- main export

def export(session, intro_s=4.0, outro_s=8.0):
    D = load(session)
    meta = json.load(open(os.path.join(session, "session.json")))
    F = fonts()
    segs = segments(D)
    frames = out_frames(segs)
    tmp = tempfile.mkdtemp()
    try:
        jobs = [(i, t, v, why, tmp) for i, (t, v, why) in enumerate(frames)]
        with Pool(max(2, (os.cpu_count() or 4) - 1), initializer=_init, initargs=(session,)) as pool:
            for _ in pool.imap_unordered(_render, jobs, chunksize=8):
                pass
        card_intro(D, F, segs).save(os.path.join(tmp, "intro.png"))
        card_summary(D, F).save(os.path.join(tmp, "outro.png"))
        main_dur = len(frames) / FPS
        parts = [f"[0:v]trim=start={a}:end={b},setpts=(PTS-STARTPTS)/{v},fps={FPS}[s{i}]" for i, (a, b, v, _) in enumerate(segs)]
        flt = ";".join(parts) + ";" + "".join(f"[s{i}]" for i in range(len(segs))) + f"concat=n={len(segs)}:v=1:a=0,scale={PHW}:{PH},setsar=1,pad={W}:{PH}:0:0:color=#0b1220[ph];"
        flt += f"[ph][1:v]overlay=0:0:shortest=1,format=yuv420p,trim=duration={main_dur:.3f}[main];"
        flt += f"[2:v]fps={FPS},format=yuv420p,setsar=1[in];[3:v]fps={FPS},format=yuv420p,setsar=1[out];[in][main][out]concat=n=3:v=1:a=0[v]"
        raw = os.path.join(tmp, "raw.mp4")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", os.path.join(session, meta["video"]),
                        "-framerate", str(FPS), "-i", os.path.join(tmp, "o%05d.png"),
                        "-loop", "1", "-t", str(intro_s), "-i", os.path.join(tmp, "intro.png"),
                        "-loop", "1", "-t", str(outro_s), "-i", os.path.join(tmp, "outro.png"),
                        "-filter_complex", flt, "-map", "[v]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", raw], check=True)
        total = intro_s + main_dur + outro_s
        chap = write_chapters(D, segs, intro_s, total, os.path.join(tmp, "chapters.txt"))
        write_srt(D, segs, intro_s, os.path.join(tmp, "events.srt"))
        out = os.path.join(session, "annotated.mp4")
        mux = ["-i", os.path.join(tmp, "events.srt"), "-i", os.path.join(tmp, "chapters.txt"),
               "-map", "0:v", "-map", "1:s", "-map_metadata", "2", "-map_chapters", "2", "-c:s", "mov_text",
               "-metadata:s:s:0", "language=eng", "-metadata:s:s:0", "title=Events", "-movflags", "+faststart"]
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", raw, *mux, "-c:v", "copy", out], check=True)
        out2 = os.path.join(session, "annotated-1080p.mp4")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", raw, *mux, "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=#0b1220,setsar=1",
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", out2], check=True)
        print(f"annotated video: {total:.1f}s ({len(frames)} frames at {FPS} fps, recording {D['end']:.1f}s, "
              f"{sum(1 for s_ in segs if s_[2] > 1)} fast-forward / {sum(1 for s_ in segs if s_[2] < 1)} slow-mo segment(s), {len(chap)} chapters)")
        print(f"  -> {out}\n  -> {out2}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- side-by-side comparison of two runs

def milestones(D):
    L = next((u["t"] for u in D.get("uev", []) if u["kind"] == "launch"), (D.get("cold") or {}).get("launch", 0.0))
    ms = []
    cold = D.get("cold")
    if cold:
        for p in cold["phases"]:
            ms.append((p["to"], p["end"] - L))
    for p in D.get("periods", []):
        ms.append(("Spinner gone: " + p.get("name", ""), p["end"] - L))
    big = [b for b in D.get("bursts", []) if b.get("name")]
    if big:
        b = max(big, key=lambda b: b["end"])
        ms.append(("Settled: " + b["name"].split("→")[-1].strip(), b["end"] - L))
    seen, out = set(), []
    for n, t in sorted(ms, key=lambda x: x[1]):
        if n not in seen:
            seen.add(n)
            out.append((n, round(t, 2)))
    return L, out


def compare(a, b, out):
    DA, DB = load(a), load(b)
    MA, MB = (json.load(open(os.path.join(x, "session.json"))) for x in (a, b))
    F = fonts()
    LA, msA = milestones(DA)
    LB, msB = milestones(DB)
    pre = 1.0
    dur = max(DA["end"] - LA, DB["end"] - LB) + pre
    CW, CH, PWc, PHc = 1920, 1080, 430, 935
    tmp = tempfile.mkdtemp()
    try:
        n = int(dur * FPS)
        names = [os.path.basename(x.rstrip("/")).replace("session-", "") for x in (a, b)]
        tags = [f"{(D.get('build') or {}).get('appVersion', '?')} · {(D.get('cond') or {}).get('network', 'default')}" for D in (DA, DB)]
        other = {0: dict(msB), 1: dict(msA)}
        for i in range(n):
            rel = i / FPS - pre
            im = Image.new("RGBA", (CW, CH), (0, 0, 0, 0))
            d = ImageDraw.Draw(im)
            d.rectangle((0, 0, CW, 120), fill="#0b1220")
            d.text((30, 20), "MyVerizon App Performance — run comparison (aligned at app launch)", fill="#ffffff", font=F["m"])
            d.text((30, 64), f"since launch  {('-' if rel < 0 else '') + fmt(abs(rel))}", fill="#fde047", font=F["clock"].font_variant(size=40) if hasattr(F["clock"], "font_variant") else F["mono"])
            for k, (D, L, ms, x0) in enumerate(((DA, LA, msA, 0), (DB, LB, msB, CW // 2))):
                t = L + rel
                px = x0 + PWc + 20
                d.rectangle((px, 120, x0 + CW // 2, CH), fill="#0f172a")
                d.text((px + 16, 136), f"Run {'AB'[k]} · {names[k]}", fill="#e2e8f0", font=F["s"])
                d.text((px + 16, 166), tags[k], fill="#94a3b8", font=F["xs"])
                scr = next((s for s in D.get("screens", []) if s["start"] - 0.05 <= t < s["end"] + 0.05), None) or \
                    max((s for s in D.get("screens", []) if s["start"] <= t), key=lambda s: s["start"], default=None)
                d.text((px + 16, 204), fit(scr["name"] if scr else "—", F["m"], CW // 2 - PWc - 60), fill="#ffffff", font=F["m"])
                y = 252
                per = next((p for p in D.get("periods", []) if p["start"] <= t < p["end"]), None)
                if per:
                    d.rounded_rectangle((px + 16, y, x0 + CW // 2 - 20, y + 36), 8, fill="#b45309")
                    d.text((px + 28, y + 6), fit(f"SPINNER {t - per['start']:.1f}s / {per['dur']:.1f}s", F["s"], 420), fill="#fff", font=F["s"])
                    y += 46
                blk = blocking_now(D, t)
                if blk:
                    lines = wrap(blk["what"], F["xs2"], CW // 2 - PWc - 80)[:3]
                    d.rounded_rectangle((px + 16, y, x0 + CW // 2 - 20, y + 30 + 22 * len(lines)), 8, outline="#ef4444", width=2, fill="#2a0f12")
                    d.text((px + 28, y + 6), "BLOCKING NOW", fill="#fca5a5", font=F["xs"])
                    for j, ln in enumerate(lines):
                        d.text((px + 28, y + 28 + 22 * j), ln, fill="#fff", font=F["xs2"])
                    y += 40 + 22 * len(lines)
                y = max(y + 10, 470)
                d.text((px + 16, y), "MILESTONES (since launch)", fill="#64748b", font=F["xs"])
                y += 28
                for name, mt in ms:
                    reached = rel >= mt
                    o = other[k].get(name)
                    delta = ""
                    if reached and o is not None and rel >= o:
                        dd = mt - o
                        delta = f"  {'+' if dd > 0 else '−'}{abs(dd):.2f}s"
                    d.text((px + 16, y), ("✓ " if reached else "· ") + fit(name, F["xs2"], 300), fill="#e2e8f0" if reached else "#475569", font=F["xs2"])
                    d.text((px + 330, y), f"{mt:6.2f}s" if reached else "", fill="#cbd5e1", font=F["mono"])
                    if delta:
                        d.text((px + 420, y), delta, fill="#f87171" if dd > 0 else "#4ade80", font=F["mono"])
                    y += 28
            im.save(os.path.join(tmp, f"c{i:05d}.png"), compress_level=1)
        def phone(sess, meta, L):
            st = max(0.0, L - pre)
            return f"trim=start={st:.3f},setpts=PTS-STARTPTS,fps={FPS},scale={PWc}:{PHc},tpad=stop_mode=clone:stop_duration={dur:.2f}"
        flt = (f"color=c=#0b1220:s={CW}x{CH}:r={FPS}:d={dur:.2f}[bg];[0:v]{phone(a, MA, LA)}[pa];[1:v]{phone(b, MB, LB)}[pb];"
               f"[bg][pa]overlay=10:130:shortest=0[t1];[t1][pb]overlay={CW//2 + 10}:130[t2];[t2][2:v]overlay=0:0:shortest=1,format=yuv420p[v]")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", os.path.join(a, MA["video"]), "-i", os.path.join(b, MB["video"]),
                        "-framerate", str(FPS), "-i", os.path.join(tmp, "c%05d.png"), "-filter_complex", flt, "-map", "[v]",
                        "-t", f"{dur:.2f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-movflags", "+faststart", out], check=True)
        print(f"comparison video {dur:.1f}s -> {out}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    args = sys.argv[1:]
    if "--compare" in args:
        i = args.index("--compare")
        a, b = args[i + 1], args[i + 2]
        out = args[args.index("-o") + 1] if "-o" in args else os.path.join(TOOL, "captures", "comparison.mp4")
        compare(a.rstrip("/"), b.rstrip("/"), out)
        return
    sess = [x for x in args if not x.startswith("-")]
    session = (sess[0] if sess else sorted(glob.glob(os.path.join(TOOL, "captures", "session-*")))[-1]).rstrip("/")
    export(session)


if __name__ == "__main__":
    main()
