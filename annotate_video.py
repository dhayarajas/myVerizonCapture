#!/usr/bin/env python3
"""Export an annotated video: the phone recording, a live panel (time, screen, phase, spinner, user events,
no-network gaps, requests in flight, session mini-timeline) and a history panel (rolling waterfall of the
last WINDOW seconds + event log), so past requests stay visible as the video plays.

Reads the session's report.html data (run report.sh first). Output: <session>/annotated.mp4
Usage: python3 annotate_video.py [session-dir] [--fps 10]
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

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
            return {"what": what, "fix": FIXES.get(key), "start": c["start"], "end": c["end"], "span": name, "save": save}
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
            if X(b) - X(a) > 60:
                d.text((X(a) + 6, y + 3), sc["name"][:int((X(b) - X(a)) / 9)], fill="#e2e8f0", font=F["xs"])
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


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    fps = 10
    if "--fps" in sys.argv:
        fps = int(sys.argv[sys.argv.index("--fps") + 1])
        args = [a for a in args if a != str(fps)]
    session = (args[0] if args else sorted(glob.glob(os.path.join(TOOL, "captures", "session-*")))[-1]).rstrip("/")
    D = load(session)
    meta = json.load(open(os.path.join(session, "session.json")))
    F = {"clock": font(64, mono=True), "m": font(30, bold=True), "s": font(21), "xs": font(17), "xs2": font(18), "mono": font(18, mono=True)}
    events = build_events(D)
    tmp = tempfile.mkdtemp()
    try:
        n = int(D["end"] * fps) + 1
        for i in range(n):
            t = i / fps
            fr = Image.new("RGB", (PW + HW, PH))
            fr.paste(panel(D, t, F), (0, 0))
            fr.paste(history(D, t, F, events), (PW, 0))
            fr.save(os.path.join(tmp, f"p{i:05d}.png"))
        out = os.path.join(session, "annotated.mp4")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", os.path.join(session, meta["video"]),
                        "-framerate", str(fps), "-i", os.path.join(tmp, "p%05d.png"),
                        "-filter_complex", f"[0:v]scale=-2:{PH},setsar=1[ph];[1:v]format=yuv420p[pn];[ph][pn]hstack=inputs=2:shortest=1,fps=30,format=yuv420p[v]",
                        "-map", "[v]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-movflags", "+faststart", out], check=True)
        print(f"annotated video ({n} panel frames) -> {out}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
