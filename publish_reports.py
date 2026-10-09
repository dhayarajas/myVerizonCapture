#!/usr/bin/env python3
"""Copy session reports into reports/ for committing (full by default, or --redact for a shareable copy).

Copies what a report needs to open: report.html, report-payloads.js, report-logs.js, thumbs/, screen.mp4,
annotated.mp4 (if exported), summary.json, session.json — plus a cross-run comparison.html.
Raw HAR / device.log / threads.ndjson are not copied.

--redact masks tokens, cookies/auth headers, session keys and ids, phone numbers and emails, and blurs
phone numbers on screen in the videos and thumbnails (positions from the screen-naming OCR).

Usage: python3 publish_reports.py [--redact] [session-dir ...]   (default: every captures/session-* with a report)
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys

from PIL import Image, ImageFilter

TOOL = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(TOOL, "reports")

PHONE = re.compile(r"(?<![\d.])\(?[2-9]\d{2}\)?[-. ]?[2-9]\d{2}[-. ]?\d{4}(?![\d.])")
JWT = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
BLOB = re.compile(r"[A-Za-z0-9+/=_-]{120,}")
SENSITIVE_HEADER = re.compile(r"^(authorization|cookie|set-cookie|.*token.*|.*session.*|x-api-key|apikey|newrelic|tracestate|traceparent|"
                              r"x-device-id|.*secret.*|launch.*|e2erequestid|x-request-id|deviceid)$", re.I)
SENSITIVE_KEY = re.compile(r'("(?:[A-Za-z_]*(?:[Tt]oken|[Ss]ession[A-Za-z]*|[Ss]ecret|[Pp]assword|[Gg]uid|[Hh]ash)|sessionKey|fidoGuid|'
                           r'deviceToken|mdn|mtn|accountNumber|ecid|s_ecid|h|u|m|userName|correlationId|marketingCloudVisitorId|'
                           r'adobeThirdPartyId|soeSessionId|newRelicSessionId|e2eRequestId|Action-UUID|id|handle|message)"\s*:\s*)"[^"]*"')


def scrub(text):
    if not isinstance(text, str) or not text:
        return text
    text = JWT.sub("[JWT]", text)
    text = SENSITIVE_KEY.sub(r'\1"[REDACTED]"', text)
    text = EMAIL.sub("[EMAIL]", text)
    text = PHONE.sub("[PHONE]", text)
    text = BLOB.sub(lambda m: f"[BLOB {len(m.group(0))} chars]", text)
    return text


def walk(o, image_ok=False):
    if isinstance(o, dict):
        return {k: walk(v, image_ok) for k, v in o.items()}
    if isinstance(o, list):
        return [walk(v, image_ok) for v in o]
    return scrub(o) if isinstance(o, str) else o


def redact_payloads(p):
    for x in p:
        if not x:
            continue
        for hk in ("rqH", "rsH"):
            x[hk] = [[n, "[REDACTED]" if SENSITIVE_HEADER.match(n) else scrub(v)] for n, v in x.get(hk, [])]
        x["q"] = [[n, scrub(v)] for n, v in x.get("q", [])]
        x["rqB"] = scrub(x.get("rqB"))
        if x.get("rsK") != "image":
            x["rsB"] = scrub(x.get("rsB"))
        x["srv"] = x.get("srv", "")
    return p


def js_load(path, var):
    s = open(path, encoding="utf-8").read()
    s = s[s.index("=") + 1:].rstrip().rstrip(";")
    return json.loads(s)


def js_dump(path, var, obj):
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"window.{var}=" + json.dumps(obj, separators=(",", ":")).replace("</", "<\\/") + ";")


def phone_boxes(session, D):
    """Boxes (normalized, top-left) of phone numbers per screen time range, from the OCR cache."""
    try:
        ocr = json.load(open(os.path.join(session, "screens-ocr.json")))
    except (OSError, ValueError):
        return []
    out = []
    for sc in D.get("screens", []):
        key = f"{sc['sample']:.2f}" if "sample" in sc else None
        lines = ocr.get(key, []) if key else []
        for l in lines:
            if PHONE.search(l["text"]) or EMAIL.search(l["text"]):
                out.append({"t0": max(0, sc["start"] - 0.8), "t1": sc["end"] + 0.8, **{k: l[k] for k in ("x", "y", "w", "h")}})
    # merge same boxes over adjacent screens
    return out


def blur_video(src, dst, boxes, W, H):
    if not boxes:
        shutil.copy2(src, dst)
        return
    pad = 12
    parts, last = [f"[0:v]split={len(boxes) + 1}[base]" + "".join(f"[s{i}]" for i in range(len(boxes)))], "base"
    for i, b in enumerate(boxes):
        x, y = max(0, int(b["x"] * W) - pad), max(0, int(b["y"] * H) - pad)
        w, h = min(W - x, int(b["w"] * W) + 2 * pad), min(H - y, int(b["h"] * H) + 2 * pad)
        w, h = w - w % 2, h - h % 2
        parts.append(f"[s{i}]crop={w}:{h}:{x}:{y},boxblur=20:3[b{i}]")
        parts.append(f"[{last}][b{i}]overlay={x}:{y}:enable='between(t,{b['t0']:.2f},{b['t1']:.2f})'[v{i}]")
        last = f"v{i}"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", src, "-filter_complex", ";".join(parts), "-map", f"[{last}]",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-pix_fmt", "yuv420p", "-movflags", "+faststart", dst], check=True)


def blur_thumbs(src_dir, dst_dir, thumbs, boxes):
    os.makedirs(dst_dir, exist_ok=True)
    for t, name in thumbs:
        s, d = os.path.join(src_dir, name), os.path.join(dst_dir, name)
        hit = [b for b in boxes if b["t0"] <= t <= b["t1"]]
        if not hit:
            shutil.copy2(s, d)
            continue
        im = Image.open(s).convert("RGB")
        W, H = im.size
        for b in hit:
            box = (max(0, int(b["x"] * W) - 2), max(0, int(b["y"] * H) - 2), min(W, int((b["x"] + b["w"]) * W) + 2), min(H, int((b["y"] + b["h"]) * H) + 2))
            im.paste(im.crop(box).filter(ImageFilter.GaussianBlur(6)), box[:2])
        im.save(d, quality=72)


def publish_full(session):
    name = os.path.basename(session.rstrip("/"))
    if not os.path.exists(os.path.join(session, "report.html")):
        print(f"skip {name}: no report.html")
        return
    dst = os.path.join(OUT, name)
    shutil.rmtree(dst, ignore_errors=True)
    os.makedirs(dst)
    for f in ("report.html", "report-payloads.js", "report-logs.js", "screen.mp4", "annotated.mp4", "annotated-1080p.mp4", "summary.json", "session.json"):
        if os.path.exists(os.path.join(session, f)):
            shutil.copy2(os.path.join(session, f), dst)
    shutil.copytree(os.path.join(session, "thumbs"), os.path.join(dst, "thumbs"))
    print(f"{name}: full report copied")


def publish(session):
    name = os.path.basename(session.rstrip("/"))
    rep = os.path.join(session, "report.html")
    if not os.path.exists(rep):
        print(f"skip {name}: no report.html")
        return
    dst = os.path.join(OUT, name)
    shutil.rmtree(dst, ignore_errors=True)
    os.makedirs(dst)
    html = open(rep, encoding="utf-8").read()
    i = html.index("const D=") + len("const D=")
    D, end = json.JSONDecoder().raw_decode(html[i:])
    boxes = phone_boxes(session, D)
    D = walk(D)
    html = html[:i] + json.dumps(D, separators=(",", ":")).replace("</", "<\\/") + html[i + end:]
    html = PHONE.sub("[PHONE]", JWT.sub("[JWT]", html))
    open(os.path.join(dst, "report.html"), "w", encoding="utf-8").write(html)
    pj = os.path.join(session, "report-payloads.js")
    if os.path.exists(pj):
        js_dump(os.path.join(dst, "report-payloads.js"), "PAYLOADS", redact_payloads(js_load(pj, "PAYLOADS")))
    lj = os.path.join(session, "report-logs.js")
    if os.path.exists(lj):
        js_dump(os.path.join(dst, "report-logs.js"), "LOGS", walk(js_load(lj, "LOGS")))
    meta = json.load(open(os.path.join(session, "session.json")))
    blur_video(os.path.join(session, meta["video"]), os.path.join(dst, meta["video"]), boxes, meta.get("width", 1206), meta.get("height", 2622))
    if os.path.exists(os.path.join(session, "annotated.mp4")):
        print(f"  note: annotated.mp4 not copied in --redact mode (its panel shows request names only, but the phone video is unblurred)")
    blur_thumbs(os.path.join(session, "thumbs"), os.path.join(dst, "thumbs"), D["thumbs"], boxes)
    for f in ("summary.json", "session.json"):
        if os.path.exists(os.path.join(session, f)):
            open(os.path.join(dst, f), "w").write(json.dumps(walk(json.load(open(os.path.join(session, f)))), indent=1))
    print(f"{name}: redacted report, {len(boxes)} on-screen phone region(s) blurred")


def main():
    args = sys.argv[1:]
    redact = "--redact" in args
    sessions = [a for a in args if a != "--redact"] or sorted(glob.glob(os.path.join(TOOL, "captures", "session-*")))
    for s in sessions:
        (publish if redact else publish_full)(s)
    os.makedirs(OUT, exist_ok=True)
    cv = os.path.join(TOOL, "captures", "comparison.mp4")
    if not redact and os.path.exists(cv):
        shutil.copy2(cv, os.path.join(OUT, "comparison.mp4"))
    subprocess.run([sys.executable, os.path.join(TOOL, "compare_runs.py"), *[os.path.join(OUT, os.path.basename(s.rstrip("/"))) for s in sessions],
                    "-o", os.path.join(OUT, "comparison.html")], check=False)


if __name__ == "__main__":
    main()
