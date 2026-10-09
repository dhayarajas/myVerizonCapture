#!/usr/bin/env python3
"""Export an annotated video: the phone recording next to a live panel (time, screen, phase, spinner,
user events, no-network gaps, requests in flight, session mini-timeline).

Reads the session's report.html data (run report.sh first). Output: <session>/annotated.mp4
Usage: python3 annotate_video.py [session-dir] [--fps 10]
"""
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile

from PIL import Image, ImageDraw, ImageFont

TOOL = os.path.dirname(os.path.abspath(__file__))
PH, PW = 1304, 760  # output height, panel width (phone is scaled to the same height)
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
    y = max(y + 10, 470)
    act = [r for r in R if r["cat"] not in ("tunnel",) and r.get("issued", r["start"]) <= t < r["end"]]
    done = [r for r in R if r["cat"] not in ("tunnel", "analytics") and r["end"] <= t < r["end"] + 0.8]
    d.text((28, y), f"REQUESTS IN FLIGHT  ({len(act)})", fill="#64748b", font=F["xs"])
    y += 26
    shown = sorted(act, key=lambda r: (r["cat"] == "analytics", r.get("issued", r["start"])))[:14]
    for r in shown:
        st = r.get("issued", r["start"])
        tot = max(0.01, r["end"] - st)
        frac = (t - st) / tot
        q = r["start"] > t
        c = CAT.get(r["cat"], "#64748b")
        d.rounded_rectangle((28, y, PW - 28, y + 34), 6, fill="#1e293b")
        d.rounded_rectangle((28, y, 28 + max(6, int((PW - 56) * frac)), y + 34), 6, fill="#f472b6" if q else c)
        d.text((40, y + 7), short(r), fill="#ffffff", font=F["xs2"])
        d.text((PW - 150, y + 7), ("queued " if q else "") + f"{t - st:.2f}s", fill="#e2e8f0", font=F["mono"])
        y += 40
    if len(act) > len(shown):
        d.text((40, y), f"+{len(act) - len(shown)} more", fill="#94a3b8", font=F["xs"])
        y += 26
    for r in done[:4]:
        d.text((40, y), f"✓ {short(r)}  {r['dur']:.2f}s", fill="#64748b", font=F["xs2"])
        y += 26
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
    tmp = tempfile.mkdtemp()
    try:
        n = int(D["end"] * fps) + 1
        for i in range(n):
            panel(D, i / fps, F).save(os.path.join(tmp, f"p{i:05d}.png"))
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
