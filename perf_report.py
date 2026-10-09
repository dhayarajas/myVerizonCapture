#!/usr/bin/env python3
"""Performance report: Proxyman HAR + iPhone screen recording (+ optional device log).

Joins three clocks-aligned sources into one HTML report:
  * what the screen was doing   (screen.mp4 -> idle / loading indicator / content change)
  * what the network was doing  (Proxyman HAR, device traffic only by default)
  * what the app was logging    (device.log from idevicesyslog, optional)

and derives:
  * loading periods (spinner on screen) and the request chain that kept it there
  * render milestones (first paint -> visually complete) and the response that triggered them
  * resource findings (duplicates, cache revalidation, heavy images, compression, CDN, errors)

Usage:
  python3 perf_report.py <traffic.har> <session-dir> [--device-log device.log]
                         [--app-process REGEX] [--host REGEX] [--all-clients] [--offset-ms N]
"""
import argparse
import base64
import bisect
import html
import json
import os
import re
import statistics
import subprocess
from collections import Counter, defaultdict
from datetime import datetime
from urllib.parse import urlsplit

import numpy as np
from PIL import Image

ANALYTICS = re.compile(
    r"omtrdc|adobedtm|demdex|everesttech|quantummetric|newrelic|nr-data|app-analytics|google-analytics|"
    r"googletagmanager|firebase|crashlytics|app-measurement|appdynamics|dynatrace|braze|branch\.io|appsflyer|"
    r"adjust\.com|doubleclick|facebook|mparticle|segment\.io|amplitude|mixpanel|tealium|optimizely|kampyle|"
    r"medallia|qualtrics|clarity\.ms|hotjar", re.I)
LOGGING = re.compile(r"/log(ger|stat|ging|s)?(/|$)|logstat|telemetry|/metrics|/beacon|/collect$", re.I)
AUTH = re.compile(r"auth|oauth|token|login|signin|fido|/nnl/|saml|sso", re.I)
IMAGE_EXT = re.compile(r"\.(png|jpe?g|gif|webp|svg|heic|avif)$", re.I)
ASSET_EXT = re.compile(r"\.(js|css|woff2?|ttf|otf|zip|json|lottie|mp4|m3u8)$", re.I)
TEXT_MIME = re.compile(r"json|javascript|text/|xml|css|html", re.I)
CDN_HEADERS = ("akamai-grn", "x-akamai-request-id", "x-cache", "cf-ray", "x-amz-cf-id", "x-served-by",
               "x-fastly-request-id", "x-azure-ref", "x-cdn", "x-edge-location")
CAT_PRIORITY = {"api": 0, "auth": 0, "asset": 1, "image": 2, "logging": 3, "analytics": 4, "tunnel": 5}

# Screen-state thresholds (fraction of pixels changed between consecutive 10 fps frames,
# status bar excluded). Calibrated on iOS: a spinner moves ~0.13% of pixels in a ~0.3% box.
IDLE_FRAC, BUSY_FRAC, BUSY_BOX, NOISE_FRAC, SCREEN_SWAP = 0.0005, 0.006, 0.02, 0.0015, 0.10


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def pct(vals, p):
    if not vals:
        return 0
    v = sorted(vals)
    return v[min(len(v) - 1, int(round(p / 100 * (len(v) - 1))))]


def fmt_bytes(n):
    return f"{n/1048576:.2f} MB" if n >= 1048576 else f"{n/1024:.1f} KB" if n >= 1024 else f"{n} B"


# ---------------------------------------------------------------- screen

def analyze_video(session, meta, thumbs_dir, fps=10, W=120):
    """Classify every frame as idle / busy (small animation, e.g. spinner) / change."""
    w, h = meta.get("width") or 1206, meta.get("height") or 2622
    H = int(round(W * h / w / 2)) * 2
    os.makedirs(thumbs_dir, exist_ok=True)
    proc = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-i", os.path.join(session, meta["video"]),
         "-vf", f"fps={fps},scale={W}:{H}", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"],
        stdout=subprocess.PIPE)
    top = int(H * 0.06)  # status bar
    frames, thumbs, prev, i, n = [], [], None, 0, W * H * 3
    while True:
        buf = proc.stdout.read(n)
        if len(buf) < n:
            break
        rgb = np.frombuffer(buf, np.uint8).reshape(H, W, 3)
        gray = rgb.mean(axis=2).astype(np.int16)[top:]
        t = round(i / fps, 2)
        frac = box = 0.0
        if prev is not None:
            m = np.abs(gray - prev) > 16
            frac = float(m.mean())
            if frac:
                ys, xs = np.where(m)
                box = float((np.ptp(ys) + 1) * (np.ptp(xs) + 1) / m.size)
        if frac < IDLE_FRAC:
            label = "idle"
        elif box < BUSY_BOX and frac < BUSY_FRAC:
            label = "busy"
        elif frac < NOISE_FRAC:
            label = "idle"
        else:
            label = "change"
        if i % (fps // 2) == 0 or label == "change":
            name = f"{i:06d}.jpg"
            Image.fromarray(rgb).save(os.path.join(thumbs_dir, name), quality=72)
            thumbs.append((t, name))
        frames.append({"t": t, "frac": frac, "box": box, "label": label})
        prev, i = gray, i + 1
    proc.wait()
    return frames, thumbs


def loading_periods(frames, min_dur=0.8, merge_gap=3.0, min_density=0.4):
    """Runs of 'busy' frames (spinner / progress), merged across small overlays."""
    runs, cur, miss = [], None, 0
    for k, f in enumerate(frames):
        if f["label"] == "busy":
            if cur is None:
                cur = [k, k]
            cur[1], miss = k, 0
        elif cur is not None:
            miss += 1
            if miss > 3:
                runs.append(cur)
                cur, miss = None, 0
    if cur:
        runs.append(cur)
    # a spinner animates continuously; short blips are blinking cursors, badges, carousels
    span = lambda r: frames[r[1]]["t"] - frames[r[0]]["t"]
    long = [r for r in runs if span(r) >= 0.6]
    near = lambda r: any(abs(frames[r[0]]["t"] - frames[l[1]]["t"]) <= 1.0 or abs(frames[l[0]]["t"] - frames[r[1]]["t"]) <= 1.0 for l in long)
    runs = [r for r in runs if span(r) >= 0.6 or near(r)]
    merged = []
    for r in runs:
        if merged:
            a, b = merged[-1][1], r[0]
            gap = frames[b]["t"] - frames[a]["t"]
            if gap <= merge_gap and max((frames[j]["frac"] for j in range(a, b + 1)), default=0) < SCREEN_SWAP:
                merged[-1][1] = r[1]
                continue
        merged.append(list(r))
    out = []
    for a, b in merged:
        start = frames[a]["t"]
        for j in range(a - 1, max(-1, a - 6), -1):  # the change that put the spinner on screen
            if frames[j]["label"] == "change":
                start = frames[j]["t"]
                break
        end = frames[min(b + 1, len(frames) - 1)]["t"]
        density = sum(1 for j in range(a, b + 1) if frames[j]["label"] == "busy") / (b - a + 1)
        if end - start >= min_dur and density >= min_density:
            out.append({"start": start, "end": end, "dur": round(end - start, 2)})
    return out


def render_bursts(frames, gap=1.0):
    """Clusters of content changes: first paint -> visually complete."""
    bursts, cur = [], None
    for f in frames:
        if f["label"] != "change":
            continue
        if cur and f["t"] - cur["end"] <= gap:
            cur["end"] = f["t"]
            cur["changes"].append(f["t"])
            cur["peak"] = max(cur["peak"], f["frac"])
        else:
            cur = {"start": f["t"], "end": f["t"], "changes": [f["t"]], "peak": f["frac"]}
            bursts.append(cur)
    for b in bursts:
        b["dur"] = round(b["end"] - b["start"], 2)
    return bursts


def state_runs(frames):
    runs = []
    for f in frames:
        if runs and runs[-1][2] == f["label"]:
            runs[-1][1] = f["t"] + 0.1
        else:
            runs.append([f["t"], f["t"] + 0.1, f["label"]])
    return runs


# ---------------------------------------------------------------- network

def categorize(method, host, path, mime):
    if method == "CONNECT":
        return "tunnel"
    if ANALYTICS.search(host):
        return "analytics"
    if LOGGING.search(path):
        return "logging"
    if mime.startswith("image/") or IMAGE_EXT.search(path):
        return "image"
    if AUTH.search(host + path):
        return "auth"
    if ASSET_EXT.search(path):
        return "asset"
    return "api"


MAX_TEXT, MAX_IMAGE = 512 * 1024, 768 * 1024


def payload(e, mime):
    """Request/response headers and bodies for the details panel."""
    def body(text, enc, m):
        if not text:
            return None, None
        if enc == "base64":
            if m.startswith("image/"):
                return (text, "image") if len(text) <= MAX_IMAGE else (None, "image-too-large")
            try:
                text = base64.b64decode(text).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                return None, "binary"
        return (text[:MAX_TEXT], "truncated") if len(text) > MAX_TEXT else (text, "text")
    rq, rs = e["request"], e["response"]
    pd = rq.get("postData") or {}
    qb, qk = body(pd.get("text") or "", "", pd.get("mimeType") or "")
    c = rs.get("content") or {}
    sb, sk = body(c.get("text") or "", c.get("encoding") or "", mime)
    hdr = lambda hs: [[h["name"], h["value"]] for h in hs or []]
    return {"rqH": hdr(rq.get("headers")), "rqB": qb, "rqK": qk, "rqM": pd.get("mimeType") or "",
            "q": [[x["name"], x["value"]] for x in rq.get("queryString") or []],
            "http": rq.get("httpVersion", ""), "st": rs.get("statusText", ""),
            "rsH": hdr(rs.get("headers")), "rsB": sb, "rsK": sk, "rsM": mime, "srv": e.get("serverIPAddress", "")}


def load_requests(har, start_ms, end_s, a):
    host_re = re.compile(a.host, re.I) if a.host else None
    entries = json.load(open(har, encoding="utf-8"))["log"]["entries"]
    reqs, devices = [], Counter()
    for e in entries:
        dev = e.get("_remoteDeviceIP")
        if not a.all_clients and not dev:
            continue
        if a.device_ip and dev != a.device_ip:
            continue
        url = e["request"]["url"]
        sp = urlsplit(url)
        host, path = (sp.hostname or ""), (sp.path or "/")
        if host_re and not host_re.search(host):
            continue
        st = parse_ts(e["startedDateTime"]) - a.offset_ms / 1000 - start_ms / 1000
        dur = max(0.0, float(e.get("time") or 0)) / 1000
        if st + dur < -2 or st > end_s:
            continue
        devices[dev] += 1
        rh = {x["name"].lower(): x["value"] for x in e["response"].get("headers", [])}
        rqh = {x["name"].lower(): x["value"] for x in e["request"].get("headers", [])}
        mime = (e["response"]["content"].get("mimeType") or rh.get("content-type", "")).split(";")[0].strip()
        size = max(0, e["response"]["content"].get("size") or 0)
        body = (e["request"].get("postData") or {}).get("text") or ""
        method = e["request"]["method"]
        segs = [s for s in path.split("/") if s]
        short = "/".join(segs[-2:]) if segs else "/"
        reqs.append({
            "start": round(st, 3), "end": round(st + dur, 3), "dur": round(dur, 3),
            "method": method, "status": e["response"].get("status", 0), "url": url, "host": host,
            "path": path, "short": short, "mime": mime, "size": size,
            "reqSize": len(body) or max(0, e["request"].get("bodySize") or 0),
            "cat": categorize(method, host, path, mime),
            "cdn": any(k in rh for k in CDN_HEADERS) or bool(re.search(r"akamai|cloudflare|cloudfront|fastly", rh.get("server", ""), re.I)),
            "enc": rh.get("content-encoding", ""), "cache": rh.get("cache-control", ""),
            "expires": rh.get("expires", ""), "etag": rh.get("etag", ""),
            "cond": bool(rqh.get("if-none-match") or rqh.get("if-modified-since")),
            "timings": {k: v for k, v in (e.get("timings") or {}).items() if isinstance(v, (int, float)) and v >= 0},
            "op": op_name(body),
            "rid": rqh.get(a.request_id_header.lower(), ""),
            "http": e["request"].get("httpVersion", ""),
            "_p": payload(e, mime),
        })
    reqs.sort(key=lambda r: r["start"])
    payloads = []
    for i, r in enumerate(reqs):
        r["id"] = i
        payloads.append(r.pop("_p"))
    return reqs, devices, payloads


def op_name(body):
    """Short hint of what a POST is doing (operation / statName / navigateToPage)."""
    m = re.search(r'"(operation|statName|navigateToPage|pageType|action)"\s*:\s*"([^"]{1,60})', body or "")
    return f"{m.group(2)}" if m else ""


def critical_path(span_start, anchor, reqs):
    """Walk back from the moment the screen changed: which request finished last before it,
    what did that one wait for, ... until we are before the span start."""
    cands = [r for r in reqs if r["cat"] not in ("analytics", "tunnel") and r["dur"] > 0]
    chain, cursor = [], anchor
    while len(chain) < 40:
        pool = [r for r in cands if r["end"] <= cursor + 0.06 and r["start"] < cursor and r["end"] >= span_start - 5]
        if not pool:
            break
        best_end = max(r["end"] for r in pool)
        # a business call that finished shortly before a logging/telemetry call is the real blocker
        tied = [r for r in pool if r["end"] >= best_end - (0.5 if r["cat"] in ("api", "auth") else 0.06)]
        r = min(tied, key=lambda x: (CAT_PRIORITY[x["cat"]], -x["end"], -x["dur"]))
        gap = round(cursor - r["end"], 3)
        if gap > 0.05:
            inflight = [o["id"] for o in reqs if o["start"] < cursor and o["end"] > r["end"] and o is not r
                        and o["cat"] not in ("analytics", "tunnel")]
            chain.append({"kind": "gap", "start": r["end"], "end": cursor, "dur": gap, "inflight": inflight})
        chain.append({"kind": "req", "id": r["id"], "start": r["start"], "end": r["end"], "dur": r["dur"],
                      "cat": r["cat"], "tight": gap <= 0.1})
        issued = r.get("issued", r["start"])
        if r["start"] - issued > 0.1:  # app asked for it earlier; it waited on the device
            chain.append({"kind": "queue", "id": r["id"], "start": issued, "end": r["start"], "dur": round(r["start"] - issued, 3)})
        cursor = min(issued, r["start"])
        if cursor <= span_start:
            break
    if cursor > span_start + 0.05:
        chain.append({"kind": "gap", "start": span_start, "end": cursor, "dur": round(cursor - span_start, 3), "inflight": []})
    chain.reverse()
    return chain


def overlap(a0, a1, b0, b1):
    return max(0.0, min(a1, b1) - max(a0, b0))


def breakdown(chain, s, e):
    agg = Counter()
    for c in chain:
        ov = overlap(c["start"], c["end"], s, e)
        if c["kind"] == "gap":
            bio = sum(overlap(b0, b1, max(c["start"], s), min(c["end"], e)) for b0, b1 in c.get("bio", []))
            agg["Face ID prompt (user)"] += bio
            agg["no request (on-device / user)"] += ov - bio
        else:
            agg[{"queue": "queued on device"}.get(c["kind"], c.get("cat"))] += ov
    return {k: round(v, 2) for k, v in agg.most_common() if v >= 0.005}


def phase_breakdown(chain, s, e, R):
    """Same span, split by where the time went: server, connection, download, device, user."""
    agg = Counter()
    for c in chain:
        ov = overlap(c["start"], c["end"], s, e)
        if ov <= 0:
            continue
        if c["kind"] == "gap":
            bio = sum(overlap(b0, b1, max(c["start"], s), min(c["end"], e)) for b0, b1 in c.get("bio", []))
            agg["Face ID prompt (user)"] += bio
            agg["on-device work"] += ov - bio
        elif c["kind"] == "queue":
            agg["waiting for a connection"] += ov
        else:
            ph = R[c["id"]].get("ph")
            if not ph:
                agg["request (no phase data)"] += ov
                continue
            parts = {"connection setup": ph["setup"], "server wait (TTFB)": ph["ttfb"], "download": ph["download"]}
            tot = sum(parts.values()) or 1
            for k, v in parts.items():
                agg[k] += ov * v / tot
    return {k: round(v, 2) for k, v in agg.most_common() if v >= 0.005}


def annotate_gaps(chain, bio, can_eval):
    for c in chain:
        if c["kind"] != "gap":
            continue
        hits = [b for b in bio if overlap(b["start"], b["end"], c["start"], c["end"]) > 0]
        if hits:
            c["bio"] = [[max(b["start"], c["start"]), min(b["end"], c["end"])] for b in hits]
            c["bioInfo"] = hits
        n = sum(1 for t in can_eval if c["start"] <= t <= c["end"])
        if n:
            c["canEval"] = n


# ---------------------------------------------------------------- device log

MARK_RE = re.compile(r"SRE ~ ([^~]+?) ~ (\{.*\})\s*$")
LIFECYCLE = re.compile(r"didFinishLaunching|applicationDidBecomeActive|applicationWillResignActive|applicationDidEnterBackground|"
                       r"applicationWillEnterForeground|sceneDidBecomeActive|sceneWillEnterForeground")
LOG_RE = re.compile(r"^(\w{3}\s+\d+\s+\d\d:\d\d:\d\d(?:\.\d+)?)\s+(\S+?)(?:\(([^)]*)\))?\[(\d+)\]\s+<(\w+)>:\s?(.*)$")


TASK_RE = re.compile(r"Task <([0-9A-Fa-f-]+)>\.<(\d+)> (.*)$")
METRIC_RE = re.compile(r"(\w+)=(\"[^\"]*\"|[^,}]*)")


def parse_tasks(keep):
    """CFNetwork per-task lifecycle and the 'summary for task' timing line."""
    tasks = {}
    for l in keep:
        if l["src"] != "app" or l["sub"] != "CFNetwork":
            continue
        m = TASK_RE.search(l["msg"])
        if not m:
            continue
        d = tasks.setdefault(m.group(1) + "." + m.group(2), {})
        msg, t = m.group(3), l["t"]
        if msg.startswith("resuming"):
            d.setdefault("res", t)
        elif msg.startswith("now using Connection"):
            d.setdefault("use", t)
            d["conn"] = msg.split()[-1]
        elif msg.startswith("sent request"):
            d.setdefault("sent", t)
        elif msg.startswith("received response"):
            d.setdefault("resp", t)
        elif msg.startswith("summary for task"):
            d["sum"], d["ok"] = t, "success" in msg.split("{")[0]
            d["m"] = {k: v.strip('"') for k, v in METRIC_RE.findall(msg.split("{", 1)[-1])}
    return [d for d in tasks.values() if "m" in d and "sum" in d]


def parse_biometrics(lines, names):
    """Face ID / Touch ID prompts: app LocalAuthentication evaluatePolicy + coreauthd matching events."""
    bio, can_eval, cur = [], [], None
    for l in lines:
        msg = l["msg"]
        if l["proc"] in names and l["sub"] == "LocalAuthentication":
            if "canEvaluatePolicy" in msg and "returned" in msg:
                can_eval.append(l["t"])
            elif re.search(r"\bevaluate(Policy|AccessControl)\b", msg) and "canEvaluate" not in msg:
                if "returned" in msg and cur:
                    cur["end"] = l["t"]
                    cur["result"] = "error" if re.search(r"[Ee]rror|failed|cancel", msg.split("returned", 1)[1][:200]) else "success"
                    bio.append(cur)
                    cur = None
                elif "returned" not in msg and cur is None:
                    cur = {"start": l["t"]}
        elif l["proc"] == "coreauthd" and cur:
            if "UI event 0 (DidAppear)" in msg:
                cur.setdefault("ui", l["t"])
            elif "face-in" in msg:
                cur.setdefault("face", l["t"])
            elif "has matched" in msg:
                cur.setdefault("matched", l["t"])
            elif re.search(r"has not matched|no match|failed to match", msg):
                cur["fail"] = l["t"]
    return bio, can_eval


def load_device_log(path, year, start_ms, end_s, a):
    if not path or not os.path.exists(path):
        return [], None, [], {"tasks": [], "bio": [], "canEval": []}
    lines = []
    for raw in open(path, encoding="utf-8", errors="replace"):
        raw = re.sub(r"\x1b\[[0-9;]*m", "", raw.rstrip("\n"))
        m = LOG_RE.match(raw)
        if not m:
            if lines and raw.strip():
                lines[-1]["msg"] += "\n" + raw
            continue
        ts, proc, sub, pid, lvl, msg = m.groups()
        try:
            dt = datetime.strptime(f"{year} {ts}", "%Y %b %d %H:%M:%S.%f" if "." in ts else "%Y %b %d %H:%M:%S")
        except ValueError:
            continue
        t = dt.timestamp() - a.log_offset_ms / 1000 - start_ms / 1000
        if -2 <= t <= end_s + 2:
            lines.append({"t": round(t, 3), "proc": proc, "sub": sub or "", "pid": pid, "lvl": lvl, "msg": msg})
    procs = Counter(l["proc"] for l in lines)
    if a.app_process:
        app_re = re.compile(a.app_process, re.I)
    else:
        guess = [p for p in procs if re.search(r"verizon|vzw|mvm|myvz", p, re.I)]
        app_re = re.compile("^(" + "|".join(map(re.escape, guess)) + ")$") if guess else None
    if not app_re:
        return [], None, [], {"tasks": [], "bio": [], "canEval": []}
    names = {p for p in procs if app_re.search(p)}
    name_re = re.compile("|".join(map(re.escape, sorted(names)))) if names else None
    keep = []
    for l in lines:
        if l["proc"] in names:
            l["src"] = "app"
            keep.append(l)
        elif name_re and l["proc"] in ("runningboardd", "SpringBoard", "backboardd", "watchdogd", "jetsamd", "kernel", "ReportCrash") and name_re.search(l["msg"]):
            l["src"] = "system"
            keep.append(l)
    keep.sort(key=lambda l: l["t"])
    markers = []
    for l in keep:
        m = MARK_RE.search(l["msg"]) if l["src"] == "app" else None
        if m:
            try:
                markers.append({"t": l["t"], "label": m.group(1).strip(), "data": json.loads(m.group(2))})
            except ValueError:
                pass
    bio, can_eval = parse_biometrics(lines, names)
    return keep, sorted(names), markers, {"tasks": parse_tasks(keep), "bio": bio, "canEval": can_eval}


def join_tasks(reqs, tasks):
    """Match CFNetwork task summaries to proxy requests by send/response time and status."""
    pairs = []
    for ti, d in enumerate(tasks):
        st = d["m"].get("response_status")
        sent, resp = d.get("sent", d.get("res")), d.get("resp", d["sum"])
        if sent is None:
            continue
        for r in reqs:
            if r["cat"] == "tunnel" or (st and str(r["status"]) != st):
                continue
            cost = abs(r["start"] - sent) + abs(r["end"] - resp)
            if cost <= 0.5:
                pairs.append((cost, ti, r["id"]))
    used_t, used_r, byid = set(), set(), {r["id"]: r for r in reqs}
    conn_proto = {d["m"]["connection"]: d["m"]["protocol"] for d in tasks if d["m"].get("protocol") and d["m"].get("connection")}
    for cost, ti, rid in sorted(pairs):
        if ti in used_t or rid in used_r:
            continue
        used_t.add(ti); used_r.add(rid)
        d, m = tasks[ti], tasks[ti]["m"]
        ms = lambda k: max(0.0, float(m.get(k, 0) or 0)) / 1000
        reused = m.get("reused") == "1"
        setup = 0.0 if reused else ms("connect_duration_ms") + ms("domain_lookup_duration_ms")
        req_start = ms("request_start_ms")
        byid[rid]["ph"] = {
            "res": d.get("res"), "txn": ms("transaction_duration_ms"),
            "wait": round(max(0.0, req_start - setup), 3), "setup": round(setup, 3),
            "dns": ms("domain_lookup_duration_ms"), "connect": ms("connect_duration_ms"), "tls": ms("secure_connection_duration_ms"),
            "ttfb": round(max(0.0, ms("response_start_ms") - req_start - ms("request_duration_ms")), 3),
            "download": ms("response_duration_ms"), "proto": m.get("protocol") or conn_proto.get(m.get("connection"), ""),
            "reused": reused, "conn": m.get("connection"), "cache": m.get("cache_hit") == "true", "ok": d.get("ok", True),
            "reqBytes": int(m.get("request_bytes", 0) or 0), "respBytes": int(m.get("response_bytes", 0) or 0)}
    return {"tasks": len(tasks), "matched": len(used_r)}


def finalize_issue_times(reqs):
    """When did the app actually ask for each request: SRE start, else CFNetwork task resume."""
    for r in reqs:
        issued = r.get("astart")
        if issued is None and r.get("ph") and r["ph"].get("res") is not None:
            issued = r["ph"]["res"]
        if issued is not None and issued <= r["start"] + 0.05:
            r["issued"] = round(issued, 3)
            r["queue"] = round(r["start"] - issued, 3)


def join_app_network(reqs, markers, start_ms, a):
    """Attach the app's own start/complete timestamps (SRE markers) to proxy requests."""
    rel = lambda ms: (ms - a.log_offset_ms - start_ms) / 1000
    by_rid = {r["rid"]: r for r in reqs if r.get("rid")}
    done, n = set(), 0
    for m in markers:
        d = m["data"]
        if m["label"] != "Network Request Complete" or "startTimestamp" not in d:
            continue
        n += 1
        a0, a1 = rel(d["startTimestamp"]), rel(d.get("endTimestamp", d["startTimestamp"]))
        r = by_rid.get(d.get("e2eRequestId"))
        if r is None:
            cands = [x for x in reqs if x["url"] == d.get("url") and x["id"] not in done and abs(x["start"] - a0) < 3]
            r = min(cands, key=lambda x: abs(x["start"] - a0)) if cands else None
        if r is None or r["id"] in done:
            continue
        done.add(r["id"])
        r["astart"], r["aend"] = round(a0, 3), round(a1, 3)
        r["queue"] = round(r["start"] - a0, 3)
    leads = sorted(r["queue"] for r in reqs if "queue" in r)
    return {"markers": n, "matched": len(done),
            "minLead": leads[0] if leads else None, "medLead": leads[len(leads) // 2] if leads else None}


def lifecycle(logs, markers, reqs, bursts, periods):
    ev = []
    sysl = [l for l in logs if l["src"] == "system"]
    app = [l for l in logs if l["src"] == "app"]
    if sysl and app and sysl[0]["t"] <= app[0]["t"]:
        ev.append({"t": sysl[0]["t"], "event": "Launch requested", "detail": f"{sysl[0]['proc']}: {sysl[0]['msg'][:120]}"})
    if app:
        ev.append({"t": app[0]["t"], "event": "App process logging", "detail": f"pid {app[0]['pid']}"})
    seen = set()
    for l in app:
        m = LIFECYCLE.search(l["msg"])
        if m and m.group(0) not in seen:
            seen.add(m.group(0))
            ev.append({"t": l["t"], "event": m.group(0), "detail": l["msg"][:140]})
    for m in markers:
        if not m["label"].startswith("Network Request"):
            ev.append({"t": m["t"], "event": m["label"], "detail": json.dumps(m["data"])[:160]})
    api = [r for r in reqs if r["cat"] in ("api", "auth")]
    if api:
        r = min(api, key=lambda r: r.get("astart", r["start"]))
        ev.append({"t": r.get("astart", r["start"]), "event": "First API request issued", "detail": r["short"], "id": r["id"]})
    t0 = ev[0]["t"] if ev else None
    if t0 is not None:
        first = next((p for p in periods if p["end"] > t0), None)
        if first:
            ev.append({"t": first["end"], "event": "First loading indicator cleared", "detail": f"loading {first['start']:.2f}s → {first['end']:.2f}s"})
    ev.sort(key=lambda e: e["t"])
    return ev


# ---------------------------------------------------------------- findings

def phase_text(r):
    ph = r.get("ph")
    if not ph:
        return ""
    bits = [f"server wait (TTFB) {ph['ttfb']:.2f}s", f"download {ph['download']:.2f}s"]
    if ph["setup"] >= 0.01:
        bits.append(f"new connection {ph['setup']*1000:.0f} ms (TLS {ph['tls']*1000:.0f} ms, to the proxy)")
    if ph["wait"] >= 0.05:
        bits.append(f"waited {ph['wait']:.2f}s for a free connection")
    return " iOS timing: " + ", ".join(bits) + (f", {ph['proto']}" if ph["proto"] else "") + "."


def build_findings(reqs, periods, bursts, logs, R, life=(), extras=None):
    F = []
    extras = extras or {"bio": [], "canEval": []}

    def add(sev, title, impact_s, detail, rec, refs=(), t=None, kind=""):
        F.append({"sev": sev, "title": title, "impact": round(impact_s, 2), "detail": detail, "rec": rec,
                  "refs": list(refs), "t": t, "kind": kind})

    for p in periods:
        if p["dur"] < 1.5:
            continue
        top = list(p["breakdown"].items())[:3]
        detail = "Time on the critical path: " + "; ".join(f"{k} {v:.1f}s ({v/p['dur']*100:.0f}%)" for k, v in top)
        add("high" if p["dur"] >= 5 else "med", f"Loading indicator on screen for {p['dur']:.1f}s ({p['start']:.1f}s → {p['end']:.1f}s)",
            p["dur"], detail, "See the request chain for this period below; the largest share is where to start.",
            [c["id"] for c in p["chain"] if c["kind"] == "req"], p["start"], "loading")

    spans = [(p, "loading period") for p in periods] + [(b, "render") for b in bursts if b.get("chain")]
    seen, gaps_seen = set(), []
    for span, label in sorted(spans, key=lambda x: -x[0]["dur"]):
        ch = span["chain"]
        for k, c in enumerate(ch):
            if c["kind"] != "req":
                continue
            r = R[c["id"]]
            nxt = ch[k + 1] if k + 1 < len(ch) else None
            if r["cat"] in ("logging", "analytics") and nxt and nxt["kind"] == "req" and c["tight"] and r["dur"] >= 0.25 and r["id"] not in seen:
                seen.add(r["id"])
                n = R[nxt["id"]]
                confirmed = n.get("issued") is not None  # app's own issue time known -> real dependency
                if confirmed and n["issued"] < r["end"] - 0.05:
                    continue  # the app issued it before the logging call finished: not waiting on it
                add(("high" if r["dur"] >= 1 else "med") if confirmed else "low",
                    f"{n['short']} {'waited' if confirmed else 'may have waited'} for logging call {r['short']} ({r['dur']:.2f}s)", r["dur"],
                    f"{n['method']} {n['host']}{n['path']} started {(n['start']-r['end'])*1000:.0f} ms after "
                    f"{r['method']} {r['host']}{r['path']} {('['+r['op']+'] ') if r['op'] else ''}finished, during a {label}."
                    + ("" if confirmed else " Inferred from timing only (no device log for this run) — confirm with a capture that includes the device log."),
                    "Make logging/telemetry fire-and-forget (don't await it before the next business call), or batch it.",
                    [r["id"], n["id"]], r["start"], "logging")
            if r["cat"] in ("api", "auth") and r["dur"] >= 1.5 and r["id"] not in seen:
                seen.add(r["id"])
                add("high" if r["dur"] >= 3 else "med",
                    f"Slow call on critical path: {r['short']}{(' ['+r['op']+']') if r['op'] else ''} {r['dur']:.2f}s", r["dur"],
                    f"{r['method']} {r['url'][:140]} — {fmt_bytes(r['size'])} response, "
                    f"{'CDN-fronted' if r['cdn'] else 'no CDN signature in response headers'}, during a {label}." + phase_text(r),
                    "Profile server time for this endpoint; if it isn't behind the CDN/edge, check origin routing and region.",
                    [r["id"]], r["start"], "slow")
        for c in ch[:-1] if ch and ch[-1]["kind"] == "gap" else ch:  # trailing gap = paint delay, reported below
            if c["kind"] == "gap" and c["dur"] >= 0.5 and c["start"] >= span["start"] - 0.2:
                if any(min(c["end"], g1) - max(c["start"], g0) > 0.3 for g0, g1 in gaps_seen):
                    continue
                gaps_seen.append((c["start"], c["end"]))
                inside = [b for b in bursts if c["start"] - 0.1 <= b["start"] <= c["end"]]
                lg = [l for l in logs if c["start"] <= l["t"] <= c["end"]]
                when = ", ".join("%.1fs" % b["start"] for b in inside[:3])
                extra = f" The screen changed during this gap at {when} (overlay/prompt/animation)." if inside else ""
                extra += f" {len(lg)} app log lines in this window." if lg else ""
                bio = sum(b1 - b0 for b0, b1 in c.get("bio", []))
                dev = c["dur"] - bio
                if c.get("bioInfo"):
                    b = c["bioInfo"][0]
                    extra = (f" Face ID prompt took {bio:.2f}s of it ({b['start']:.2f}s → {b['end']:.2f}s"
                             + (f", shown at {b['ui']:.2f}s" if b.get("ui") else "") + (f", matched at {b['matched']:.2f}s" if b.get("matched") else "")
                             + f"); the remaining {dev:.2f}s was on-device work before/after the prompt." + extra)
                if c.get("canEval"):
                    extra += f" The app checked biometric availability (canEvaluatePolicy) {c['canEval']}× in this window."
                if dev < 0.3 and c.get("bio"):
                    continue
                add("med" if dev >= 1 else "low",
                    f"{c['dur']:.2f}s with no request on the critical path ({c['start']:.1f}s → {c['end']:.1f}s)"
                    + (f" — {dev:.2f}s on-device, {bio:.2f}s Face ID" if bio else ""), dev,
                    "Nothing on the network was being waited on: on-device work (parsing, crypto, layout), a timer, "
                    "or a user/OS prompt." + extra,
                    "Check the screen frames and app log for this window; move heavy work off the main thread or overlap it with network.",
                    c.get("inflight", [])[:5], c["start"], "gap")

    for b in bursts:
        tr = b.get("trigger")
        if tr is None:
            continue
        r = R[tr]
        if b["delay"] >= 0.5 and r["cat"] in ("api", "auth"):
            add("med" if b["delay"] >= 1 else "low",
                f"Screen painted {b['delay']:.2f}s after {r['short']} returned", b["delay"],
                f"Response finished at {r['end']:.2f}s ({fmt_bytes(r['size'])}), first visual change at {b['start']:.2f}s.",
                "Time between response and paint is client-side: decoding/parsing, layout, image decode. Large layout payloads amplify it.",
                [r["id"]], r["end"], "render")

    groups = defaultdict(list)
    for r in reqs:
        if r["method"] == "GET" and r["status"] == 200 and r["size"] > 0:
            groups[r["url"]].append(r)
    for url, rs in groups.items():
        if len(rs) < 2:
            continue
        wasted = sum(r["size"] for r in rs[1:])
        overlap = any(rs[i]["start"] < rs[i - 1]["end"] for i in range(1, len(rs)))
        if wasted >= 20000:
            add("med" if wasted >= 200000 else "low",
                f"Same resource downloaded {len(rs)}× ({fmt_bytes(wasted)} wasted): {rs[0]['short']}", 0,
                f"{url[:140]} — {'requests overlapped in flight (no de-duplication)' if overlap else 'cache not reused'}; "
                f"Cache-Control: {rs[0]['cache'] or '(none)'}.",
                "De-duplicate in-flight image/resource requests and honor the HTTP cache in the image loader.",
                [r["id"] for r in rs], rs[0]["start"], "dup")

    reval = defaultdict(list)
    for r in reqs:
        if r["status"] == 304:
            reval[r["url"]].append(r)
    for url, rs in reval.items():
        if len(rs) >= 3:
            span = rs[-1]["start"] - rs[0]["start"]
            add("low", f"Revalidated {len(rs)}× in {span:.0f}s: {rs[0]['short']}", 0,
                f"{url[:140]} — Cache-Control: {rs[0]['cache'] or '(none)'}; Expires: {rs[0]['expires'] or '(none)'}. "
                "Each 304 still costs a round trip.",
                "Serve an explicit Cache-Control max-age, and let the client use its cache within it.",
                [r["id"] for r in rs[:10]], rs[0]["start"], "cache")

    big = {}
    for r in reqs:
        if r["cat"] == "image" and r["size"] >= 100000:
            big.setdefault(r["url"], r)
    for url, r in big.items():
        add("med" if r["size"] >= 250000 else "low", f"Heavy image {fmt_bytes(r['size'])}: {r['short']}", 0,
            f"{url[:140]} ({r['mime']}).",
            "Request a size matched to the view and a modern format (WebP/AVIF) from the image service.",
            [r["id"]], r["start"], "image")

    unz = {}
    for r in reqs:
        if TEXT_MIME.search(r["mime"]) and r["size"] >= 2048 and not r["enc"] and r["status"] == 200:
            unz.setdefault((r["host"], r["path"]), r)
    for (h, p), r in unz.items():
        add("low", f"Uncompressed {r['mime']} response {fmt_bytes(r['size'])}: {r['short']}", 0,
            f"{r['url'][:140]} has no Content-Encoding.", "Enable gzip/brotli for text responses on this host.",
            [r["id"]], r["start"], "compress")

    for r in reqs:
        if r["cat"] in ("api", "auth") and r["size"] >= 50000:
            add("low", f"Large API response {fmt_bytes(r['size'])}: {r['short']}", 0,
                f"{r['url'][:140]} took {r['dur']:.2f}s.",
                "Trim the payload (only fields the screen renders) or split above/below-the-fold content.",
                [r["id"]], r["start"], "payload")

    hosts = defaultdict(list)
    for r in reqs:
        if r["cat"] in ("api", "auth") and r["dur"] > 0:
            hosts[r["host"]].append(r)
    cdn_meds = [statistics.median(x["dur"] for x in rs) for h, rs in hosts.items() if any(x["cdn"] for x in rs)]
    base = statistics.median(cdn_meds) if cdn_meds else None
    for h, rs in hosts.items():
        if any(x["cdn"] for x in rs) or not base:
            continue
        med = statistics.median(x["dur"] for x in rs)
        if med >= 1.0 and med >= 1.5 * base:
            add("med", f"{h}: no CDN signature, median {med:.2f}s vs {base:.2f}s for CDN-fronted API hosts", 0,
                f"{len(rs)} calls; responses carry no Akamai/CloudFront/Cloudflare/Fastly headers.",
                "Front this host with the edge/CDN or serve it from a region closer to users.",
                [x["id"] for x in rs], rs[0]["start"], "cdn")

    for r in reqs:
        if r["cat"] != "tunnel" and (r["status"] >= 400 or r["status"] == 0):
            add("med", f"HTTP {r['status'] or 'failed'}: {r['short']}", 0, r["url"][:160], "Investigate the failing call.",
                [r["id"]], r["start"], "error")

    decrypted = {r["host"] for r in reqs if r["cat"] != "tunnel"}
    tun = sorted({r["host"] for r in reqs if r["cat"] == "tunnel" and r["host"] not in decrypted})
    if tun:
        add("info", f"{len(tun)} host(s) not decrypted by Proxyman", 0, ", ".join(tun),
            "Enable SSL Proxying for these hosts in Proxyman to include their payloads.", [], None, "tunnel")

    queued = [r for r in reqs if r.get("queue", 0) >= 0.3]
    by_h = defaultdict(list)
    for r in queued:
        by_h[r["host"]].append(r)
    for h, rs in by_h.items():
        hr = [r for r in reqs if r["host"] == h and r["cat"] != "tunnel"]
        peak = max(sum(1 for o in hr if o["start"] <= x["start"] < o["end"]) for x in hr)
        worst = max(r["queue"] for r in rs)
        http = Counter(r["http"] for r in hr).most_common(1)[0][0]
        add("high" if worst >= 1 else "med",
            f"{len(rs)} request(s) to {h} waited up to {worst:.2f}s on the device before being sent", worst,
            f"The app issued them well before they reached the network; at most {peak} requests to this host "
            f"were in flight at once over {http or 'HTTP/1.1'} — consistent with a per-host connection limit. "
            "Check whether production negotiates HTTP/2 for this host (through the proxy it may be downgraded to HTTP/1.1)."
            + (lambda w, nc, pr: f" iOS confirms: waited up to {w*1000:.0f} ms for a free connection; {nc} new connection(s) opened ({pr})." if w else "")(
                max((x["ph"]["wait"] for x in rs if x.get("ph")), default=0),
                sum(1 for x in hr if x.get("ph") and not x["ph"]["reused"]),
                ", ".join(sorted({x["ph"]["proto"] for x in hr if x.get("ph") and x["ph"]["proto"]})) or "protocol n/a"),
            "Reduce the fan-out (aggregate tile calls), prioritise above-the-fold calls first, or ensure HTTP/2 multiplexing is used.",
            [r["id"] for r in rs], min(r["issued"] for r in rs), "queue")

    if life:
        launch = next((e for e in life if e["event"] == "Launch requested"), None)
        cleared = next((e for e in life if e["event"] == "First loading indicator cleared"), None)
        if launch and cleared:
            steps = "; ".join(f"{e['event']} +{e['t']-launch['t']:.2f}s" for e in life if launch["t"] < e["t"] <= cleared["t"])
            add("med" if cleared["t"] - launch["t"] >= 3 else "low",
                f"Cold launch: {cleared['t']-launch['t']:.2f}s from launch to first content", cleared["t"] - launch["t"],
                steps, "See the app lifecycle section; the first loading period shows what the first screen waited for.",
                [e["id"] for e in life if "id" in e], launch["t"], "launch")

    ce = sorted(extras.get("canEval", []))
    burst = max(([t for t in ce if t0 <= t <= t0 + 1] for t0 in ce), key=len, default=[])
    if len(burst) >= 5:
        add("low", f"App checked biometric availability {len(burst)}× within {burst[-1]-burst[0]:.2f}s", burst[-1] - burst[0],
            "canEvaluatePolicy creates a LocalAuthentication context and round-trips to coreauthd each time.",
            "Check once and cache the result for the flow.", [], burst[0], "bio")

    errs = [l for l in logs if l["lvl"] in ("Error", "Fault")]
    if errs:
        sig = Counter(re.sub(r"\d+", "#", l["msg"])[:120] for l in errs)
        add("med", f"{len(errs)} error/fault lines in the app log", 0,
            "Most frequent: " + " | ".join(f"{n}× {s}" for s, n in sig.most_common(3)),
            "Review the device log section.", [], errs[0]["t"], "log")

    order = {"high": 0, "med": 1, "low": 2, "info": 3}
    F.sort(key=lambda f: (order[f["sev"]], -f["impact"]))
    return F


def savings(periods, bursts, R, reqs):
    """Upper-bound time saved on each loading period / render if one thing were fixed (not additive)."""
    cdn = [r["dur"] for r in reqs if r["cat"] in ("api", "auth") and r["cdn"] and r["dur"] > 0]
    target = round(statistics.median(cdn), 2) if cdn else None
    agg = {}

    def put(key, title, basis, save, span, refs):
        a = agg.setdefault(key, {"title": title, "basis": basis, "save": 0.0, "spans": [], "refs": []})
        a["save"] += save
        if span not in a["spans"]:
            a["spans"].append(span)
        a["refs"] += [x for x in refs if x not in a["refs"]]
    spans = [(p, p.get("name") or f"loading {p['start']:.1f}–{p['end']:.1f}s") for p in periods] + \
            [(b, b.get("name") or f"render {b['start']:.1f}–{b['end']:.1f}s") for b in bursts if b.get("chain")]
    for span, label in spans:
        s0, s1 = span["start"], span["end"]
        for c in span["chain"]:
            ov = overlap(c["start"], c["end"], s0, s1)
            if ov <= 0.05:
                continue
            if c["kind"] == "queue":
                h = R[c["id"]]["host"]
                put(("queue", h), f"Requests to {h} sent immediately (HTTP/2 multiplexing or fewer parallel calls)",
                    "removes time requests waited on the device for a connection", ov, label, [c["id"]])
            elif c["kind"] == "gap":
                bio = sum(overlap(b0, b1, max(c["start"], s0), min(c["end"], s1)) for b0, b1 in c.get("bio", []))
                if ov - bio >= 0.3:
                    put(("device",), "On-device work between calls on the critical path halved",
                        "half of the time with no request in flight (excluding Face ID)", (ov - bio) / 2, label, [])
            else:
                r = R[c["id"]]
                if r["cat"] in ("logging", "analytics") and c.get("tight"):
                    put(("logging",), "Logging/telemetry not awaited before the next business call",
                        "removes logging calls from the critical path", ov, label, [r["id"]])
                elif r["cat"] in ("api", "auth") and target and r["dur"] > target * 1.5:
                    nm = r["short"] + (f" [{r['op']}]" if r["op"] else "")
                    put(("slow", r["host"]), f"{r['host']} responds like the CDN-fronted API median ({target:.2f}s)",
                        f"slow calls such as {nm} cut to {target:.2f}s", min(ov, r["dur"] - target), label, [r["id"]])
                ph = r.get("ph")
                if ph and ph["setup"] >= 0.1:
                    put(("conn", r["host"]), f"Connections to {r['host']} pre-warmed / reused",
                        "removes new-connection setup on the critical path", min(ov, ph["setup"]), label, [r["id"]])
    rows = [dict(v, save=round(v["save"], 2)) for v in agg.values() if v["save"] >= 0.1]
    return sorted(rows, key=lambda x: -x["save"])


def name_spans(periods, bursts, R):
    seen = Counter()
    for p in periods:
        reqs = [R[c["id"]] for c in p["chain"] if c["kind"] == "req"]
        if any(c.get("bio") for c in p["chain"]) or any(r["op"] in ("INIT_AUTH", "FINISH_AUTH") for r in reqs):
            base = "Sign-in (Face ID)"
        elif p.get("screens"):
            base = p["screens"]
        else:
            last = next((r for r in reversed(reqs) if r["cat"] in ("api", "auth")), reqs[-1] if reqs else None)
            base = f"Waiting for {last['short']}" if last else "Loading"
        seen[base] += 1
        p["name"] = base if seen[base] == 1 else f"{base} #{seen[base]}"
    for b in bursts:
        if b.get("chain") and b.get("trigger") is not None:
            base = b.get("screens") or f"Render after {R[b['trigger']]['short']}"
            seen[base] += 1
            b["name"] = base if seen[base] == 1 else f"{base} #{seen[base]}"


def endpoint_key(r):
    return f"{r['method']} {r['host']}{r['path']}" + (f" [{r['op']}]" if r["op"] else "")


def build_summary(meta, end_s, reqs, periods, bursts, life, kpi, cold):
    m = {"recording": round(end_s, 2), "spinner_total": kpi["loading"], "spinner_longest": kpi["longest"],
         "api_p50": kpi["apiP50"], "api_p95": kpi["apiP95"], "requests": kpi["requests"], "api_calls": kpi["api"],
         "bytes": kpi["bytes"], "dup_image_bytes": kpi["wasted"],
         "queued_max": round(max(r["queue"] for r in reqs if "issued" in r), 3) if any("issued" in r for r in reqs) else None}
    ev = {e["event"]: e["t"] for e in reversed(life)}
    if "Launch requested" in ev:
        L0 = ev["Launch requested"]
        for k, n in (("First loading indicator cleared", "launch_to_first_content"), ("First API request issued", "launch_to_first_api"),
                     ("didFinishLaunching", "launch_to_did_finish_launching")):
            if k in ev:
                m[n] = round(ev[k] - L0, 2)
    if cold:
        m["cold_start_total"] = cold["total"]
        for ph in cold["phases"]:
            m["cold:" + ph["name"]] = ph["dur"]
    for p in periods:
        m["spinner:" + p["name"]] = p["dur"]
    for b in bursts:
        if b.get("name"):
            m["render:" + b["name"]] = b["dur"]
    eps = defaultdict(lambda: {"dur": [], "ttfb": []})
    for r in reqs:
        if r["cat"] in ("api", "auth") and r["dur"] > 0:
            e = eps[endpoint_key(r)]
            e["dur"].append(r["dur"])
            if r.get("ph"):
                e["ttfb"].append(r["ph"]["ttfb"])
    return {"session": meta.get("startISO"), "device": meta.get("device"), "metrics": m, "endpoints": eps}


def load_budgets(path):
    try:
        return json.load(open(path)).get("budgets", [])
    except (OSError, ValueError):
        return []


def eval_budgets(budgets, metrics):
    out = []
    for b in budgets:
        v = metrics.get(b["metric"])
        out.append({"label": b.get("label", b["metric"]), "metric": b["metric"], "max": b["max"], "value": v,
                    "pass": None if v is None else v <= b["max"]})
    return out


# ---------------------------------------------------------------- screens & cold start

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
SKIP_TITLE = re.compile(r"^(verizon|v|[#@].*|[\d\s().+-]{5,}|\d{1,2}:\d\d.*)$", re.I)


def screen_states(frames, min_dur=0.25):
    """Stable stretches between content changes (spinner animation counts as stable)."""
    runs, cur = [], None
    for f in frames:
        if f["label"] == "change":
            if cur and f["t"] - cur[1] <= 0.15:
                cur[1] = f["t"]
            else:
                cur = [f["t"], f["t"]]
                runs.append(cur)
    states, prev = [], 0.0
    for a, b in runs + [[frames[-1]["t"] + 0.1, None]] if frames else []:
        if a - prev >= min_dur:
            fr = [x for x in frames if prev <= x["t"] < a]
            busy = sum(1 for x in fr if x["label"] == "busy") / max(1, len(fr))
            states.append({"start": round(prev, 2), "end": round(a, 2), "busy": round(busy, 2)})
        prev = (b or a) + 0.1
    return states


def ocr_states(session, meta, states):
    """Name each state from its on-screen text (cached in screens-ocr.json)."""
    cache_f = os.path.join(session, "screens-ocr.json")
    try:
        cache = json.load(open(cache_f))
    except (OSError, ValueError):
        cache = {}
    tool = os.path.join(TOOL_DIR, "screen-ocr")
    if not os.access(tool, os.X_OK):
        subprocess.run(["swiftc", "-O", os.path.join(TOOL_DIR, "screen-ocr.swift"), "-o", tool], check=False)
    tmp = os.path.join(session, ".ocr")
    os.makedirs(tmp, exist_ok=True)
    todo = []
    for st in states:
        st["sample"] = round(st["start"] + min(0.6, (st["end"] - st["start"]) / 2), 2)
        key = f"{st['sample']:.2f}"
        if key not in cache:
            img = os.path.join(tmp, key + ".jpg")
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", key, "-i", os.path.join(session, meta["video"]),
                            "-frames:v", "1", img], check=False)
            todo.append((key, img))
    if todo and os.access(tool, os.X_OK):
        out = subprocess.run([tool] + [i for _, i in todo], capture_output=True, text=True).stdout
        try:
            for (key, _), r in zip(todo, json.loads(out)):
                cache[key] = r["lines"]
        except ValueError:
            pass
        json.dump(cache, open(cache_f, "w"))
    for _, img in todo:
        try:
            os.remove(img)
        except OSError:
            pass
    for st in states:
        st["text"] = [l for l in cache.get(f"{st['sample']:.2f}", []) if l["y"] > 0.06]


def title_of(lines):
    cands = [l for l in lines if 0.07 < l["y"] < 0.62 and len(l["text"].strip()) >= 4
             and re.search(r"[A-Za-z]{3}", l["text"]) and not SKIP_TITLE.match(l["text"].strip())]
    if not cands:
        return ""
    best = max(cands, key=lambda l: (round(l["h"], 3), -l["y"]))
    title = best["text"].strip()
    nxt = [l for l in cands if 0 < l["y"] - best["y"] < best["h"] * 1.6 and abs(l["h"] - best["h"]) < best["h"] * 0.25]
    if nxt:
        title += " " + min(nxt, key=lambda l: l["y"])["text"].strip()
    return re.sub(r"\s+", " ", title).strip(" .")


def name_states(states, launch_t):
    named, seen_title = [], False
    for st in states:
        lines, title = st.get("text", []), title_of(st.get("text", []))
        loading = st["busy"] >= 0.3
        if len(lines) >= 15 and (launch_t is None or st["start"] < launch_t + 0.5) and not seen_title and not named:
            name = "Home screen"
        elif title:
            name = title + (" (loading)" if loading else "")
            seen_title = True
        elif loading:
            name = "Loader"
        elif not seen_title:
            name = "Splash screen"
        else:
            name = "Blank screen"
        if not title and not loading and st["end"] - st["start"] < 0.35 and name not in ("Splash screen", "Home screen"):
            continue
        if named and named[-1]["name"] == name:
            named[-1]["end"] = st["end"]
            continue
        named.append({"name": name, "start": st["start"], "end": st["end"], "loading": loading, "title": title,
                      "sample": st["sample"], "words": " · ".join(l["text"] for l in lines[:12])})
    if launch_t is None and named and named[0]["name"] == "Home screen" and len(named) > 1:
        launch_t = named[0]["end"]
    return named, launch_t


def label_spans_by_screen(periods, bursts, screens):
    at = lambda t: next((s["name"] for s in screens if s["start"] - 0.05 <= t < s["end"] + 0.05), None)
    for p in periods:
        best = max(screens, key=lambda x: overlap(x["start"], x["end"], p["start"], p["end"]), default=None)
        a = best["name"] if best and overlap(best["start"], best["end"], p["start"], p["end"]) > 0 else at(p["start"] + 0.2)
        b = at(p["end"] + 0.3)
        if a and b and a != b:
            p["screens"] = f"{a} → {b}"
    for b in bursts:
        x, y = at(b["start"] - 0.15), at(b["end"] + 0.3)
        if x and y and x != y:
            b["screens"] = f"{x} → {y}"


# ---------------------------------------------------------------- threads (logarchive)

LIB_CATS = [("App code", None), ("UI (UIKit)", r"UIKitCore|FrontBoard|BoardServices|BackBoard|QuartzCore|CoreAnimation|UIFoundation|SwiftUI"),
            ("WebKit", r"WebKit|WebCore|JavaScriptCore"), ("Network", r"CFNetwork|^Network$|libusrtcp|libboringssl|libnetwork|nw"),
            ("Security & auth", r"Security|LocalAuthentication|libcoreauth|Keychain"), ("Location", r"CoreLocation|LocationSupport"),
            ("Prefs & IPC", r"CoreFoundation|libxpc|cfprefs|libsystem_containermanager|MobileGestalt"), ("Other", r".")]


def lib_cat(lib, in_app):
    if in_app:
        return "App code"
    for name, rx in LIB_CATS[1:]:
        if re.search(rx, lib):
            return name
    return "Other"


def load_threads(path, start_ms, end_s, app_procs, step=0.25):
    if not os.path.exists(path):
        return None
    ev = []
    for line in open(path, encoding="utf-8", errors="replace"):
        if not line.startswith("{"):
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("eventType") != "logEvent":
            continue
        proc = os.path.basename(e.get("processImagePath") or "")
        if app_procs and proc not in app_procs:
            continue
        try:
            t = datetime.strptime(e["timestamp"], "%Y-%m-%d %H:%M:%S.%f%z").timestamp() - start_ms / 1000
        except (KeyError, ValueError):
            continue
        if not -2 <= t <= end_s + 2:
            continue
        sp = e.get("senderImagePath") or ""
        lib = os.path.basename(sp)
        ev.append({"t": round(t, 3), "tid": e.get("threadID"), "lib": lib, "cat": lib_cat(lib, ".app/" in sp),
                   "sub": e.get("subsystem") or "", "lvl": e.get("messageType", ""), "msg": (e.get("eventMessage") or "")[:300]})
    if not ev:
        return None
    ev.sort(key=lambda x: x["t"])
    main = next((x["tid"] for x in ev if re.search(r"didFinishLaunching|applicationDidBecomeActive|applicationWillEnterForeground", x["msg"])), None)
    if main is None:
        main = Counter(x["tid"] for x in ev if x["cat"] == "UI (UIKit)").most_common(1)
        main = main[0][0] if main else ev[0]["tid"]
    nb = int(end_s / step) + 1
    cats = [c for c, _ in LIB_CATS]
    bins = {"main": [[0] * len(cats) for _ in range(nb)], "bg": [[0] * len(cats) for _ in range(nb)]}
    for x in ev:
        x["main"] = x["tid"] == main
        i = int(x["t"] / step)
        if 0 <= i < nb:
            bins["main" if x["main"] else "bg"][i][cats.index(x["cat"])] += 1
    return {"events": ev, "main": main, "threads": len({x["tid"] for x in ev}), "cats": cats, "bins": bins, "step": step}


SYNC_RE = re.compile(r"\(sync\)|synchronous|\bsync\b|waitUntilDone|dispatch_sync", re.I)


def thread_summary(th, t0, t1):
    if not th:
        return None
    ev = [x for x in th["events"] if t0 <= x["t"] < t1]
    m, b = [x for x in ev if x["main"]], [x for x in ev if not x["main"]]
    notable = [x for x in m if x["cat"] == "App code" or SYNC_RE.search(x["msg"]) or x["cat"] in ("WebKit", "Security & auth")]
    return {"main": len(m), "bg": len(b), "bgThreads": len({x["tid"] for x in b}),
            "mainCats": Counter(x["cat"] for x in m).most_common(5), "bgCats": Counter(x["cat"] for x in b).most_common(5),
            "sync": [{"t": x["t"], "lib": x["lib"], "msg": x["msg"][:160]} for x in m if SYNC_RE.search(x["msg"])][:6],
            "notable": [{"t": x["t"], "lib": x["lib"], "cat": x["cat"], "msg": x["msg"][:160]} for x in notable][:10],
            "appBg": [{"t": x["t"], "lib": x["lib"], "msg": x["msg"][:160]} for x in b if x["cat"] == "App code"][:8]}


# ---------------------------------------------------------------- highlights

PRIV = re.compile(r"\s*:?\s*<private>")


def log_highlights(logs, markers, reqs, t0, t1, bio, can_eval, th=None):
    """What the app was doing in [t0, t1): lifecycle, app markers, app's own logs, notable system activity."""
    L = [l for l in logs if t0 <= l["t"] < t1]
    hl = []

    def add(t, text, sev="info", kind=""):
        hl.append({"t": round(t, 2), "text": text, "sev": sev, "kind": kind})
    for l in L:
        msg = l["msg"]
        if l["src"] == "system" and ("open(" in msg or "_executablePath" in msg or "==> container" in msg):
            if not any(h["kind"] == "launch" for h in hl):
                add(l["t"], f"iOS launches the app process ({l['proc']})", "info", "launch")
        if l["src"] == "app" and LIFECYCLE.search(msg):
            add(l["t"], "App delegate: " + LIFECYCLE.search(msg).group(0), "key", "lifecycle")
        elif l["src"] == "app" and not l["sub"] and l["lvl"] != "Debug" and not MARK_RE.search(msg):
            txt = PRIV.sub("", msg).strip()
            if txt and not any(h["text"] == txt for h in hl):
                add(l["t"], txt[:160], "app", "app")
        if l["lvl"] in ("Error", "Fault") and "deny" not in msg:
            add(l["t"], f"{l['proc']}({l['sub']}) {l['lvl']}: {msg[:140]}", "error", "error")
    for m in markers:
        if t0 <= m["t"] < t1:
            d = m["data"]
            if m["label"] == "Network Request Started":
                add(m["t"], f"App starts request {urlsplit(d.get('url', '')).path.split('/')[-1] or d.get('url', '')}", "net", "net")
            elif m["label"] != "Network Request Complete":
                add(m["t"], f"App marker: {m['label']}", "key", "marker")
    deny = [l for l in L if "Sandbox" in l["sub"] and "deny" in l["msg"]]
    if deny:
        paths = sorted({m.group(1) for l in deny for m in [re.search(r"(?:file-test-existence|sysctl-read)\s+(\S+)", l["msg"])] if m})
        add(deny[0]["t"], f"Jailbreak / integrity checks: {len(deny)} probes blocked by the sandbox ({', '.join(paths[:5])})", "info", "sandbox")
    n = sum(1 for t in can_eval if t0 <= t < t1)
    if n:
        add(min(t for t in can_eval if t0 <= t < t1), f"Biometric availability checked {n}× (canEvaluatePolicy)", "warn" if n >= 5 else "info", "bio")
    for b in bio:
        if t0 <= b["start"] < t1:
            add(b["start"], f"Face ID prompt {b['end']-b['start']:.2f}s → {b.get('result', '?')}", "key", "bio")
    fire = [l for l in L if re.search(r"Firebase|\bFIR|fireperf", l["msg"])]
    if fire:
        add(fire[0]["t"], f"Firebase SDK activity: {len(fire)} log lines (incl. settings reads)", "info", "sdk")
    prefs = sum(1 for l in L if l["sub"] == "CoreFoundation" and ("looked up value" in l["msg"] or "found no value" in l["msg"]))
    if prefs >= 50:
        add(t0, f"{prefs} UserDefaults/preferences lookups", "warn" if prefs >= 300 else "info", "prefs")
    tasks = [l for l in L if l["sub"] == "CFNetwork" and "resuming" in l["msg"]]
    conns = [l for l in L if l["sub"] == "CFNetwork" and "setting up Connection" in l["msg"]]
    if tasks:
        add(tasks[0]["t"], f"{len(tasks)} network task(s) started, {len(conns)} new connection(s)", "net", "net")
    for r in reqs:
        if r["cat"] in ("api", "auth") and r["dur"] >= 0.3 and overlap(r["start"], r["end"], t0, t1) >= 0.5 * (t1 - t0):
            ph = r.get("ph")
            add(max(r["start"], t0), f"Waiting on {r['short']}{(' [' + r['op'] + ']') if r['op'] else ''} "
                f"({r['dur']:.2f}s{', server wait %.2fs' % ph['ttfb'] if ph else ''}) for most of this phase", "crit", "wait")
    ts = thread_summary(th, t0, t1)
    if ts and ts["sync"]:
        add(ts["sync"][0]["t"], f"Synchronous call(s) on the main thread: {len(ts['sync'])} (e.g. {ts['sync'][0]['msg'][:80]})", "warn", "sync")
    hl.sort(key=lambda h: h["t"])
    subs = Counter((l["sub"] or "(app code)") for l in L if l["src"] == "app").most_common(6)
    return {"highlights": hl[:40], "lines": len(L), "subs": subs,
            "errors": sum(1 for l in L if l["lvl"] in ("Error", "Fault")), "threads": ts}


def cold_start(screens, launch_t, logs, markers, reqs, bio, can_eval, th):
    if launch_t is None:
        return None
    seq = [s for s in screens if s["end"] > launch_t and s["name"] != "Home screen"]
    out, prev_t, prev_name = [], launch_t, "Launch"
    for s in seq:
        out.append({"name": f"{prev_name} → {s['name']}", "from": prev_name, "to": s["name"], "start": round(prev_t, 2),
                    "end": round(max(s["start"], prev_t), 2), "screen": s})
        prev_t, prev_name = s["start"], s["name"]
        if s["title"] and not s["loading"]:
            break
    else:
        return None
    for ph in out:
        ph["dur"] = round(ph["end"] - ph["start"], 2)
        ph["log"] = log_highlights(logs, markers, reqs, ph["start"], ph["end"], bio, can_eval, th)
        ph["reqs"] = [r["id"] for r in reqs if r["cat"] != "tunnel" and r["start"] < ph["end"] and r["end"] > ph["start"]]
        ph.pop("screen", None)
    return {"launch": round(launch_t, 2), "total": round(out[-1]["end"] - launch_t, 2), "phases": out,
            "first": out[-1]["to"]}


UIEVENT_RE = re.compile(r"Evaluating dispatch of UIEvent: (0x[0-9a-f]+); type: (\d+)")
UIEVENT_TYPES = {0: "touch", 1: "motion", 2: "remote control", 3: "hardware button", 4: "scroll", 5: "hover", 6: "pinch/rotate"}


def user_events(logs, bio, life, bursts, screens, reqs, launch_t):
    """User interactions: launch tap, taps / drags (UIKit UIEvent dispatch), Face ID steps, keyboard, system overlays."""
    ev = []
    at = lambda t: next((x["name"] for x in screens if x["start"] - 0.05 <= t < x["end"] + 0.05), "")
    content = [r for r in reqs if r["cat"] in ("api", "auth", "image", "asset", "logging")]

    def response(t0):
        nxt = min((r for r in content if t0 - 0.02 <= r.get("issued", r["start"]) <= t0 + 1.5), key=lambda r: r.get("issued", r["start"]), default=None)
        chg = next((b["start"] for b in bursts if t0 <= b["start"] <= t0 + 3), None)
        return ({"req": nxt["id"], "reqAfter": round(nxt.get("issued", nxt["start"]) - t0, 3)} if nxt else {}) | \
               ({"screenAfter": round(chg - t0, 3)} if chg is not None else {})
    if launch_t is not None:
        ev.append({"t": round(launch_t, 3), "end": round(launch_t, 3), "kind": "launch", "label": "App icon tapped (launch)",
                   "screen": "Home screen", **response(launch_t)})
    # touches
    groups = []
    for l in logs:
        if l["src"] != "app" or l["sub"] != "UIKitCore":
            continue
        m = UIEVENT_RE.search(l["msg"])
        if not m:
            continue
        typ = int(m.group(2))
        if groups and groups[-1]["type"] == typ and l["t"] - groups[-1]["end"] <= 0.4:
            groups[-1]["end"] = l["t"]
            groups[-1]["n"] += 1
        else:
            groups.append({"start": l["t"], "end": l["t"], "type": typ, "n": 1})
    for g in groups:
        dur = g["end"] - g["start"]
        name = UIEVENT_TYPES.get(g["type"], f"event type {g['type']}")
        if g["type"] == 0:
            kind, label = ("tap", "Tap") if dur <= 0.35 else ("drag", "Touch & drag / scroll")
        else:
            kind, label = "input", name.capitalize()
        ev.append({"t": round(g["start"], 3), "end": round(g["end"], 3), "kind": kind, "label": label,
                   "screen": at(g["start"]), "detail": f"{g['n']} UIKit dispatches over {dur*1000:.0f} ms (measured from touch-down)", **response(g["start"])})
    # Face ID
    for b in bio:
        steps = [(k, b[k]) for k in ("ui", "face", "matched") if b.get(k)]
        ev.append({"t": round(b["start"], 3), "end": round(b.get("end", b["start"]), 3), "kind": "bio", "label": "Face ID",
                   "screen": at(b["start"]), "result": b.get("result", "?"),
                   "steps": [{"k": {"ui": "prompt shown", "face": "face detected", "matched": "matched"}[k], "t": round(v, 3)} for k, v in steps],
                   **response(b.get("end", b["start"]))})
    # keyboard + system overlays (app inactive)
    kb, inactive = None, None
    seen_active = False
    for l in logs:
        if l["src"] != "app":
            continue
        m = re.search(r"handleKeyboardChange: set currentKeyboard:([YN])", l["msg"])
        if m and seen_active:
            if m.group(1) == "Y" and kb is None:
                kb = l["t"]
            elif m.group(1) == "N" and kb is not None:
                ev.append({"t": round(kb, 3), "end": round(l["t"], 3), "kind": "keyboard", "label": "Keyboard shown", "screen": at(kb)})
                kb = None
        if "applicationDidBecomeActive" in l["msg"]:
            seen_active = True
        m = re.search(r"Deactivation reason (added|removed): \d+; deactivation reasons: (\d+) -> (\d+)", l["msg"])
        if m and seen_active:
            a, b_ = int(m.group(2)), int(m.group(3))
            if a == 0 and b_ != 0:
                inactive = l["t"]
            elif b_ == 0 and inactive is not None:
                if l["t"] - inactive >= 0.2:
                    ev.append({"t": round(inactive, 3), "end": round(l["t"], 3), "kind": "overlay", "label": "System overlay (app inactive)",
                               "screen": at(inactive), "detail": "e.g. Face ID sheet, notification, control centre"})
                inactive = None
    # no device log: infer taps from screen changes not preceded by a response
    if not any(e["kind"] in ("tap", "drag") for e in ev):
        for b in bursts:
            if b.get("trigger") is None and b["peak"] >= 0.02 and (launch_t is None or b["start"] > launch_t + 1.5):
                ev.append({"t": round(max(0, b["start"] - 0.1), 3), "end": round(b["start"], 3), "kind": "tap", "label": "Likely tap (inferred from screen change)",
                           "screen": at(b["start"] - 0.2), "inferred": True, **response(b["start"] - 0.1)})
    ev.sort(key=lambda e: e["t"])
    return ev


def network_gaps(reqs, end_s, periods, screens, bursts, bio, can_eval, logs, markers, threads, min_gap=0.4):
    """Stretches with no app request in flight (analytics/tunnels ignored) and what happened meanwhile."""
    iv = sorted((r.get("issued", r["start"]), r["end"]) for r in reqs
                if r["cat"] in ("api", "auth", "image", "asset", "logging") and r["dur"] > 0)
    merged = []
    for a, b in iv:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    gaps = []
    for (a0, a1), (b0, b1) in zip(merged, merged[1:] + [[end_s, end_s]]):
        g0, g1 = a1, min(b0, end_s)
        if g1 - g0 < min_gap:
            continue
        dur = g1 - g0
        spin = sum(overlap(p["start"], p["end"], g0, g1) for p in periods)
        fid = sum(overlap(b["start"], b.get("end", b["start"]), g0, g1) for b in bio)
        scr = [s_ for s_ in screens if overlap(s_["start"], s_["end"], g0, g1) > 0.05]
        changes = [b for b in bursts if g0 <= b["start"] < g1]
        user = [b for b in changes if b.get("trigger") is None]
        analytics = sum(1 for r in reqs if r["cat"] == "analytics" and g0 <= r["start"] < g1)
        lg = log_highlights(logs, markers, reqs, g0, g1, bio, can_eval, threads)
        th = lg.get("threads")
        tail = b0 >= end_s
        if fid >= 0.3 * dur:
            kind, what = "bio", "Face ID prompt"
        elif spin >= 0.5 * dur:
            kind, what = "wait", "Spinner on screen with nothing on the network — on-device work"
        elif tail:
            kind, what = "idle", "After the last request — screen settled / user idle"
        elif user or any(x["title"] and not x["loading"] for x in scr):
            nm = next((x["name"] for x in scr if x["title"] and not x["loading"]), "the screen")
            kind, what = "user", f"User on “{nm}” (reading / choosing / tapping)"
        else:
            kind, what = "idle", "Screen idle between requests"
        bits = []
        if fid:
            bits.append(f"Face ID {fid:.2f}s")
        if spin >= 0.3:
            bits.append(f"spinner {spin:.1f}s")
        titled = [x["name"] for x in scr if x["title"]]
        if titled and kind != "wait":
            bits.append(f"on “{titled[0]}”")
        if user and kind != "bio":
            bits.append(f"{len(user)} user screen change(s)")
        if th:
            bits.append(f"main {th['main']} / bg {th['bg']} log events")
        gaps.append({"start": round(g0, 3), "end": round(g1, 3), "dur": round(dur, 3), "kind": kind, "what": what,
                     "label": f"{dur:.2f}s · " + (", ".join(bits[:2]) or what), "bits": bits, "spinner": round(spin, 2),
                     "faceid": round(fid, 2), "screens": [x["name"] for x in scr], "changes": [round(b["start"], 2) for b in changes],
                     "userChanges": len(user), "analytics": analytics, "log": lg, "tail": tail})
    return gaps


def log_hist(ts, end_s, step=0.25):
    h = [0] * (int(end_s / step) + 1)
    for t in ts:
        if 0 <= t <= end_s:
            h[int(t / step)] += 1
    return h


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("har")
    ap.add_argument("session")
    ap.add_argument("--device-log", help="idevicesyslog output (default: <session>/device.log if present)")
    ap.add_argument("--app-process", help="regex of the app's process name in the device log")
    ap.add_argument("--host", help="regex filter on request host")
    ap.add_argument("--device-ip", help="only traffic from this device IP")
    ap.add_argument("--all-clients", action="store_true", help="include Mac traffic too (default: remote devices only)")
    ap.add_argument("--offset-ms", type=int, default=0, help="shift HAR times (HAR ahead of video = positive)")
    ap.add_argument("--log-offset-ms", type=int, default=0, help="shift device-log times (phone clock ahead = positive)")
    ap.add_argument("--request-id-header", default="e2eRequestId", help="request header that carries the app's request id")
    ap.add_argument("--budgets", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "budgets.json"),
                    help="performance budgets file")
    ap.add_argument("-o", "--out")
    a = ap.parse_args()

    S = a.session
    meta = json.load(open(os.path.join(S, "session.json")))
    start_ms = meta["startEpochMs"]
    out = a.out or os.path.join(S, "report.html")
    out_dir = os.path.dirname(os.path.abspath(out))
    rel = lambda p: os.path.relpath(p, out_dir)

    print("Analyzing screen recording...")
    thumbs_dir = os.path.join(S, "thumbs")
    frames, thumbs = analyze_video(S, meta, thumbs_dir)
    end_s = frames[-1]["t"] if frames else (meta.get("endEpochMs", start_ms) - start_ms) / 1000
    periods = loading_periods(frames)
    bursts = render_bursts(frames)

    reqs, devices, payloads = load_requests(a.har, start_ms, end_s, a)
    R = {r["id"]: r for r in reqs}
    year = datetime.fromtimestamp(start_ms / 1000).year
    logs, app_procs, markers, extras = load_device_log(a.device_log or os.path.join(S, "device.log"), year, start_ms, end_s, a)
    appnet = join_app_network(reqs, markers, start_ms, a)
    taskinfo = join_tasks(reqs, extras["tasks"])
    finalize_issue_times(reqs)
    bio, can_eval = extras["bio"], extras["canEval"]
    bio_iv = [{"start": b["start"], "end": b.get("end", b["start"]), **b} for b in bio]
    log_t = [l["t"] for l in logs]

    for p in periods:
        p["chain"] = critical_path(p["start"], p["end"], reqs)
        annotate_gaps(p["chain"], bio_iv, can_eval)
        p["breakdown"] = breakdown(p["chain"], p["start"], p["end"])
        p["phases"] = phase_breakdown(p["chain"], p["start"], p["end"], R)
        p["logs"] = [bisect.bisect_left(log_t, p["start"]), bisect.bisect_right(log_t, p["end"])]
    content = [r for r in reqs if r["cat"] in ("api", "auth", "image", "asset")]
    for b in bursts:
        before = [r for r in content if b["start"] - 1.5 <= r["end"] <= b["start"] + 0.05]
        tr = max(before, key=lambda r: r["end"]) if before else None
        b["trigger"] = tr["id"] if tr else None
        b["delay"] = round(b["start"] - tr["end"], 2) if tr else None
        b["filled"] = [r["id"] for r in content if b["start"] < r["end"] <= b["end"] + 0.05]
        b["inLoading"] = any(p["start"] <= b["start"] < p["end"] - 0.05 for p in periods)
        if b["dur"] >= 1.0:
            b["chain"] = critical_path(b["start"], b["end"] + 0.05, reqs)
            annotate_gaps(b["chain"], bio_iv, can_eval)
            b["breakdown"] = breakdown(b["chain"], b["start"], b["end"])
            b["phases"] = phase_breakdown(b["chain"], b["start"], b["end"], R)
    states = screen_states(frames)
    ocr_states(S, meta, states)
    launch_log = next((l["t"] for l in logs if l["src"] == "system"), None) if logs else None
    screens, launch_t = name_states(states, launch_log)
    label_spans_by_screen(periods, bursts, screens)
    threads = load_threads(os.path.join(S, "threads.ndjson"), start_ms, end_s, app_procs or [])
    name_spans(periods, bursts, R)
    life = lifecycle(logs, markers, reqs, bursts, periods)
    for b in bio_iv:
        life.append({"t": b["start"], "event": "Face ID requested",
                     "detail": f"prompt {b['end']-b['start']:.2f}s" + (f", shown +{b['ui']-b['start']:.2f}s" if b.get("ui") else "")
                     + (f", matched +{b['matched']-b['start']:.2f}s" if b.get("matched") else "") + f" → {b.get('result', '?')}"})
    life.sort(key=lambda e: e["t"])
    findings = build_findings(reqs, periods, bursts, logs, R, life, extras)
    cold = cold_start(screens, launch_t, logs, markers, reqs, bio_iv, can_eval, threads)
    if cold:
        steps = "; ".join(f"{p['name']} {p['dur']:.2f}s" for p in cold["phases"])
        crit = [h["text"] for p in cold["phases"] for h in p["log"]["highlights"] if h["sev"] in ("crit", "error", "warn")]
        findings.insert(0, {"sev": "high" if cold["total"] >= 3 else "med", "title": f"Cold start: {cold['total']:.2f}s from launch to \"{cold['first']}\"",
                            "impact": cold["total"], "detail": steps + (". Highlights: " + "; ".join(crit[:3]) if crit else ""),
                            "rec": "See the Cold start tab for what the app did in each phase.", "refs": [], "t": cold["launch"], "kind": "cold"})
        findings[:] = [f for f in findings if f["kind"] != "launch"]
    for p in periods:
        p["threads"] = thread_summary(threads, p["start"], p["end"])
    gaps = network_gaps(reqs, end_s, periods, screens, bursts, bio_iv, can_eval, logs, markers, threads)
    uev = user_events(logs, bio_iv, life, bursts, screens, reqs, launch_t)
    for e in uev:
        if e["kind"] in ("tap", "drag") and e.get("screenAfter") is not None and e["screenAfter"] >= 1.0 and not e.get("inferred"):
            findings.append({"sev": "med" if e["screenAfter"] >= 2 else "low", "title": f"Screen responded {e['screenAfter']:.2f}s after a tap on “{e['screen']}”",
                             "impact": e["screenAfter"], "detail": f"{e['label']} at {e['t']:.2f}s; first request "
                             + (f"issued +{e['reqAfter']:.2f}s" if "reqAfter" in e else "none within 1.5s") + ".",
                             "rec": "Give immediate visual feedback on tap (pressed state / skeleton) while the request runs.",
                             "refs": [e["req"]] if "req" in e else [], "t": e["t"], "kind": "tap"})
    for g in gaps:
        if g["kind"] == "wait" and g["dur"] >= 0.5:
            findings.append({"sev": "med" if g["dur"] >= 1 else "low", "title": f"{g['dur']:.2f}s with a spinner but no network activity ({g['start']:.1f}s → {g['end']:.1f}s)",
                             "impact": g["dur"], "detail": "; ".join(g["bits"]) + ". " + "; ".join(h["text"] for h in g["log"]["highlights"][:3]),
                             "rec": "Nothing was in flight — the app was working on the device. See the gap in the Timeline waterfall.",
                             "refs": [], "t": g["start"], "kind": "netgap"})
    if threads:
        main_sync = [x for x in threads["events"] if x["main"] and SYNC_RE.search(x["msg"])]
        if main_sync:
            findings.append({"sev": "med", "title": f"{len(main_sync)} synchronous call(s) on the main thread", "impact": 0,
                             "detail": "; ".join(sorted({f"{x['lib']}: {x['msg'][:70]}" for x in main_sync}))[:400],
                             "rec": "Synchronous IPC/work on the main thread can stall rendering; move it off-main or make it async.",
                             "refs": [], "t": main_sync[0]["t"], "kind": "sync"})
        for p in periods:
            wk = [x for x in threads["events"] if x["main"] and x["cat"] == "WebKit" and p["start"] <= x["t"] <= p["end"]]
            if len(wk) >= 20:
                findings.append({"sev": "low", "title": f"WebKit work on the main thread during \"{p['name']}\" ({len(wk)} log events)", "impact": 0,
                                 "detail": "A web view is being created or loaded on the main thread while the spinner is shown.",
                                 "rec": "Create/warm web views earlier or lazily, outside the critical path.", "refs": [], "t": wk[0]["t"], "kind": "webkit"})

    api = [r["dur"] for r in reqs if r["cat"] in ("api", "auth")]
    kpi = {
        "session": round(end_s, 1),
        "requests": len(reqs),
        "api": len(api),
        "bytes": sum(r["size"] for r in reqs),
        "loading": round(sum(p["dur"] for p in periods), 1),
        "longest": max((p["dur"] for p in periods), default=0),
        "apiP50": round(pct(api, 50), 2), "apiP95": round(pct(api, 95), 2),
        "screens": sum(1 for b in bursts if b["peak"] >= SCREEN_SWAP),
        "wasted": sum(sum(r["size"] for r in rs[1:]) for rs in
                      [[r for r in reqs if r["url"] == u and r["method"] == "GET" and r["status"] == 200] for u in
                       {r["url"] for r in reqs if r["cat"] == "image"}]),
    }
    host_rows = []
    by_host = defaultdict(list)
    for r in reqs:
        by_host[r["host"]].append(r)
    for h, rs in by_host.items():
        d = [r["dur"] for r in rs if r["cat"] != "tunnel"]
        host_rows.append({"host": h, "n": len(rs), "cats": dict(Counter(r["cat"] for r in rs)),
                          "p50": round(pct(d, 50), 2), "p95": round(pct(d, 95), 2),
                          "bytes": sum(r["size"] for r in rs), "cdn": any(r["cdn"] for r in rs),
                          "gzip": sum(1 for r in rs if r["enc"]), "text": sum(1 for r in rs if TEXT_MIME.search(r["mime"]))})
    for h in host_rows:
        rs = [r for r in by_host[h["host"]] if r.get("ph")]
        h["proto"] = dict(Counter(r["ph"]["proto"] for r in rs if r["ph"]["proto"]))
        h["newConn"] = sum(1 for r in rs if not r["ph"]["reused"])
        h["ttfb50"] = round(pct([r["ph"]["ttfb"] for r in rs], 50), 2) if rs else None
    host_rows.sort(key=lambda x: -x["n"])
    summary = build_summary(meta, end_s, reqs, periods, bursts, life, kpi, cold)
    budgets = eval_budgets(load_budgets(a.budgets), summary["metrics"])
    summary["budgets"] = budgets
    with open(os.path.join(S, "summary.json"), "w") as f:
        json.dump(summary, f, indent=1)

    data = {
        "meta": meta, "video": rel(os.path.join(S, meta["video"])), "thumbBase": rel(thumbs_dir),
        "thumbs": thumbs, "states": state_runs(frames), "periods": periods, "bursts": bursts,
        "reqs": reqs, "findings": findings, "kpi": kpi, "hosts": host_rows,
        "logsTotal": len(logs), "appProcs": app_procs or [], "life": life, "appNet": appnet,
        "marks": [{"t": m["t"], "label": m["label"]} for m in markers if not m["label"].startswith("Network Request")],
        "logHist": log_hist(log_t, end_s),
        "errT": [l["t"] for l in logs if l["lvl"] in ("Error", "Fault")],
        "devices": {k or "mac": v for k, v in devices.items()}, "end": end_s,
        "bio": bio_iv, "taskInfo": taskinfo, "screens": screens, "gaps": gaps, "uev": uev,
        "threads": ({"main": threads["main"], "n": threads["threads"], "cats": threads["cats"], "bins": threads["bins"], "step": threads["step"],
                     "mainEvents": [{k: x[k] for k in ("t", "lib", "cat", "msg")} for x in threads["events"] if x["main"]][:3000],
                     "total": len(threads["events"]), "mainTotal": sum(1 for x in threads["events"] if x["main"])} if threads else None), "savings": savings(periods, bursts, R, reqs), "budgets": budgets, "cold": cold,
        "generated": datetime.now().isoformat(timespec="seconds"),
    }
    pfile = os.path.splitext(os.path.basename(out))[0] + "-payloads.js"
    with open(os.path.join(out_dir, pfile), "w", encoding="utf-8") as f:
        f.write("window.PAYLOADS=" + json.dumps(payloads, separators=(",", ":")).replace("</", "<\\/") + ";")
    lfile = os.path.splitext(os.path.basename(out))[0] + "-logs.js"
    with open(os.path.join(out_dir, lfile), "w", encoding="utf-8") as f:
        f.write("window.LOGS=" + json.dumps(logs, separators=(",", ":")).replace("</", "<\\/") + ";")
    page = TEMPLATE.replace("__LOGS__", lfile).replace("__PAYLOADS__", pfile).replace("__DATA__", json.dumps(data, separators=(",", ":")).replace("</", "<\\/"))
    page = page.replace("__TITLE__", html.escape(f"MyVerizon App Performance – {meta.get('startISO', '')}"))
    with open(out, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"{len(screens)} screens ({', '.join(x['name'] for x in screens[:8])}), cold start: {cold['total'] if cold else 'n/a'}s, "
          f"threads: {threads['threads'] if threads else 'n/a'}")
    print(f"{len(reqs)} requests, {len(periods)} loading periods, {len(bursts)} render bursts, "
          f"{len(logs)} app log lines, {appnet['matched']}/{appnet['markers']} app network events matched, "
          f"{taskinfo['matched']}/{taskinfo['tasks']} iOS task timings matched, {len(bio)} Face ID prompt(s), "
          f"{len(findings)} findings -> {out}")


TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--hdr:#e9edf3;--tabbg:#dfe4ec;--tabhov:#d3dae5;--bg:#f6f7f9;--card:#fff;--fg:#1d2330;--mut:#667085;--line:#e4e7ec;--acc:#2563eb;
--api:#2563eb;--auth:#7c3aed;--image:#0d9488;--asset:#64748b;--logging:#d97706;--analytics:#a3a3a3;--tunnel:#d4d4d4;--gap:#ef4444;
--busy:#fbbf24;--queue:#f472b6;--change:#3b82f6;--idle:#e5e7eb;--high:#dc2626;--med:#d97706;--low:#2563eb;--info:#6b7280}
@media (prefers-color-scheme:dark){:root{--hdr:#161a22;--tabbg:#1f2430;--tabhov:#272d3a;--bg:#0f1115;--card:#181b22;--fg:#e6e8ec;--mut:#98a2b3;--line:#2a2f3a;--idle:#2a2f3a;--tunnel:#3a3f4a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:13px/1.45 -apple-system,system-ui,Segoe UI,sans-serif}
header{position:sticky;top:0;z-index:5;background:var(--hdr);border-bottom:1px solid var(--line)}.hd{padding:16px 24px 6px}
#tabs{display:flex;gap:4px;padding:6px 20px 0;overflow-x:auto;align-items:flex-end}
#tabs a{padding:8px 16px;cursor:pointer;color:var(--mut);white-space:nowrap;font-weight:500;background:var(--tabbg);border:1px solid var(--line);border-bottom:none;border-radius:9px 9px 0 0;margin-bottom:-1px;transition:background .15s,color .15s}
#tabs a:hover{color:var(--fg);background:var(--tabhov)}#tabs a.on{color:var(--acc);background:var(--bg);font-weight:600;border-color:var(--line);box-shadow:inset 0 3px 0 var(--acc);position:relative;z-index:1}
#tabs a.dis{opacity:.45;cursor:not-allowed;background:transparent;border-style:dashed}#tabs a.dis:hover{color:var(--mut);background:transparent}
#tabs .cnt{font-size:11px;background:var(--line);color:var(--fg);border-radius:9px;padding:0 6px;margin-left:5px}#tabs a.on .cnt{background:var(--acc);color:#fff}#tabs .cnt:empty{display:none}
section{display:none}section.on{display:block}main>section>h2:first-child,main>section>div>h2:first-child{margin-top:18px}h1{font-size:20px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 10px}h3{font-size:14px;margin:0 0 6px}
main{padding:0 24px 60px;max-width:1500px}.mut{color:var(--mut)}.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
.kpis{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:10px;margin:14px 0}
.kpi.bad b{color:var(--high)}.kpi.warn b{color:var(--med)}.kpi em{display:block;font-style:normal;font-size:11px;color:var(--mut);margin-top:3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sum .kpis{margin:6px 0 10px}.take{border-left:3px solid var(--acc)}.take h3{margin:0 0 4px}.take ul{margin:0;padding-left:18px}.take li{margin:4px 0}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px}.kpi b{display:block;font-size:20px}.kpi span{color:var(--mut);font-size:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;margin:10px 0}
.f{display:grid;grid-template-columns:64px 1fr;gap:10px;border-bottom:1px solid var(--line);padding:9px 0}.f:last-child{border:0}
.sev{font-size:11px;font-weight:700;text-transform:uppercase;border-radius:4px;padding:2px 6px;color:#fff;text-align:center;height:fit-content}
.sev.high{background:var(--high)}.sev.med{background:var(--med)}.sev.low{background:var(--low)}.sev.info{background:var(--info)}
.f .t{font-weight:600}.f .rec{color:var(--mut)}a.j{color:var(--acc);cursor:pointer;text-decoration:none}a.j:hover{text-decoration:underline}
table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:5px 6px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--mut);font-weight:600;font-size:12px}
.strip{display:flex;gap:6px;overflow-x:auto;padding:4px 0}.strip figure{margin:0;text-align:center}.strip img{width:84px;border-radius:6px;border:1px solid var(--line);cursor:pointer}.strip figcaption{font-size:11px;color:var(--mut)}
.bar{display:flex;height:22px;border-radius:5px;overflow:hidden;margin:8px 0}.bar div{height:100%;color:#fff;font-size:11px;white-space:nowrap;overflow:hidden;padding:3px 5px}
.legend span{display:inline-flex;align-items:center;gap:4px;margin-right:12px;font-size:12px;color:var(--mut)}.legend i{width:10px;height:10px;border-radius:2px;display:inline-block}
.chain td{font-size:12px}.chain .gap td{color:var(--gap)}
#tl{display:flex;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}
#tlL{width:250px;flex:none;border-right:1px solid var(--line);font-size:11px}#tlL div{height:15px;line-height:15px;padding:0 6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;cursor:pointer}
#tlR{overflow-x:auto;flex:1;position:relative}#tlR svg{display:block}.ctl{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:8px 0}
#curTime{position:fixed;z-index:21;display:none;pointer-events:none;background:#dc2626;color:#fff;font:600 11.5px ui-monospace,Menlo,monospace;padding:3px 8px;border-radius:6px;box-shadow:0 3px 10px #0004;white-space:nowrap}
#curTime:after{content:"";position:absolute;right:-5px;top:50%;margin-top:-5px;border:5px solid transparent;border-right:0;border-left-color:#dc2626}
#tip{position:fixed;z-index:20;display:none;pointer-events:none;width:430px;max-width:calc(100vw - 24px);background:var(--card);border:1px solid var(--line);border-radius:10px;box-shadow:0 10px 32px #0004;padding:10px 12px;font-size:12px}
#tip .tt{display:flex;gap:10px}#tip .tt img{width:62px;border-radius:6px;border:1px solid var(--line);align-self:flex-start}#tip h5{margin:0 0 2px;font-size:13px}
#tip .u{color:var(--mut);word-break:break-all;margin-bottom:6px}#tip .row{display:flex;justify-content:space-between;gap:8px;padding:1px 0}#tip .row span:first-child{color:var(--mut)}
#tip .tb{display:flex;height:8px;border-radius:4px;overflow:hidden;margin:6px 0 2px;background:var(--line)}#tip .tl{display:flex;justify-content:space-between;color:var(--mut);font-size:11px}
#tip .iss{margin-top:6px;border-top:1px solid var(--line);padding-top:6px}#tip .iss div{margin:2px 0}#tip .ok{color:var(--mut)}#tip .cp{color:var(--fg);font-weight:600;margin-top:4px}
#peek{position:fixed;right:16px;top:16px;width:150px;display:none;z-index:9;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:6px;text-align:center;box-shadow:0 6px 24px #0003}#peek img{width:100%;border-radius:6px}
#drawer{position:fixed;right:0;top:0;bottom:0;width:min(760px,100%);background:var(--card);border-left:1px solid var(--line);transform:translateX(100%);transition:transform .2s;z-index:10;overflow:auto;padding:16px;box-shadow:-8px 0 30px #0003}
#drawer.open{transform:none}#drawer video{width:240px;border-radius:10px;background:#000;float:right;margin:0 0 10px 12px}#drawer .x{position:absolute;right:12px;top:8px;cursor:pointer;font-size:20px}
.pill{display:inline-block;border-radius:4px;padding:0 5px;color:#fff;font-size:11px}
details>summary{cursor:pointer;color:var(--acc);margin:6px 0}.lvl-Error,.lvl-Fault{color:var(--high)}
.ovgrid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin:12px 0}.ovgrid .card{margin:0}.ovgrid h3{margin:0 0 8px}
.hb{display:grid;grid-template-columns:minmax(120px,34%) 1fr auto;gap:6px 10px;align-items:center;font-size:12px}.hb .lb{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.hb .tr{display:flex;height:16px;border-radius:4px;overflow:hidden;background:var(--line)}.hb .tr div{height:100%}.hb .v{font-weight:700;font-variant-numeric:tabular-nums}
.lg{display:flex;flex-wrap:wrap;gap:4px 12px;margin-top:8px;font-size:11px;color:var(--mut)}.lg i{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:4px}
.fgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:10px}.fc{background:var(--card);border:1px solid var(--line);border-left:5px solid var(--info);border-radius:10px;padding:10px 12px;display:flex;flex-direction:column;gap:4px}
.fc.high{border-left-color:var(--high)}.fc.med{border-left-color:var(--med)}.fc.low{border-left-color:var(--low)}.fc .ft{font-weight:600;line-height:1.3}.fc .fn{font-size:22px;font-weight:700}
.fc.high .fn{color:var(--high)}.fc.med .fn{color:var(--med)}.fc details{font-size:12px;color:var(--mut)}.fc details summary{font-size:12px}.fc .fr{display:flex;justify-content:space-between;align-items:center}
.sevc{display:inline-flex;gap:6px;margin-left:8px;vertical-align:middle}.sevc span{font-size:11px;font-weight:700;color:#fff;border-radius:10px;padding:1px 8px}
.gauge{height:6px;background:var(--line);border-radius:3px;margin-top:6px;overflow:hidden;position:relative}.gauge div{height:100%}.gauge i{position:absolute;top:0;bottom:0;width:2px;background:var(--fg);opacity:.5}
@media (max-width:900px){.ovgrid{grid-template-columns:1fr}}
.thr{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:10px}.thr>div{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:8px 10px}
.thr h4{margin:0 0 6px;font-size:13px}.thr ul,.hl ul{margin:6px 0 0;padding-left:16px;font-size:12px}.thr li,.hl li{margin:2px 0}.warnline{color:var(--med);font-weight:600;margin-top:4px}
.journey{display:flex;align-items:center;gap:6px;overflow-x:auto;padding:6px 0}.journey figure{margin:0;text-align:center;font-size:11px}.journey img{width:92px;border-radius:8px;border:1px solid var(--line);cursor:pointer}
.journey .arr{text-align:center;color:var(--mut);font-size:11px;min-width:70px}.journey .arr b{display:block;font-size:14px;color:var(--fg)}
.cs{display:grid;grid-template-columns:1.35fr 1fr;gap:14px}.hl .b{display:inline-block;font-size:10px;font-weight:700;border-radius:4px;padding:0 5px;color:#fff;margin-right:4px;text-transform:uppercase}
.b.crit,.b.error{background:var(--high)}.b.warn{background:var(--med)}.b.key{background:var(--auth)}.b.net{background:var(--api)}.b.app{background:var(--image)}.b.info{background:var(--info)}
.budget b small{font-size:12px;color:var(--mut);font-weight:500}.budget.pass{border-left:4px solid #16a34a}.budget.fail{border-left:4px solid var(--high)}.budget.na{border-left:4px solid var(--line)}
@media (max-width:900px){.cs,.thr{grid-template-columns:1fr}}
.tabs{display:flex;gap:4px;border-bottom:1px solid var(--line);margin:10px 0}.tabs a{padding:6px 12px;cursor:pointer;border-radius:6px 6px 0 0;color:var(--mut)}
.tabs a.on{background:var(--bg);color:var(--fg);font-weight:600;border:1px solid var(--line);border-bottom:0}
.kv{width:100%;font-size:12px}.kv td:first-child{color:var(--mut);white-space:nowrap;width:1%;padding-right:12px}.kv td{word-break:break-all;font-family:ui-monospace,Menlo,monospace}
pre.body{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:10px;max-height:60vh;overflow:auto;font-size:12px;white-space:pre-wrap;word-break:break-all;margin:6px 0}
.sec{display:flex;justify-content:space-between;align-items:center;margin-top:14px}.sec h4{margin:0;font-size:13px}.btn{font-size:12px;color:var(--acc);cursor:pointer;background:none;border:1px solid var(--line);border-radius:6px;padding:2px 8px}
.jk{color:#9333ea}.js{color:#059669}.jn{color:#2563eb}.jb{color:#d97706}
.thumb{width:56px;border-radius:5px;border:1px solid var(--line);cursor:pointer}
</style></head><body>
<header><div class="hd"><h1 id="h1"></h1><div class="mut" id="sub"></div></div>
<nav id="tabs"><a data-tab="overview">Overview</a><a data-tab="cold">Cold start <span class="cnt" id="c-cold"></span></a><a data-tab="loading">Loading periods <span class="cnt" id="c-loading"></span></a><a data-tab="render">Render milestones <span class="cnt" id="c-render"></span></a><a data-tab="lifecycle">App lifecycle</a><a data-tab="timeline">Timeline</a><a data-tab="hosts">Hosts <span class="cnt" id="c-hosts"></span></a><a data-tab="requests">Requests <span class="cnt" id="c-requests"></span></a><a data-tab="logs">Device log <span class="cnt" id="c-logs"></span></a></nav></header>
<main>
<section data-tab="overview"><div class="kpis" id="kpis"></div>
<div class="ovgrid"><div class="card" id="ov-spin"></div><div class="card" id="ov-cold"></div></div>
<div id="budgets"></div>
<h2>Key findings <span id="sevcount"></span></h2><div class="fgrid" id="findings"></div>
<div class="ovgrid"><div class="card" id="savings"></div><div class="card" id="ov-ep"></div></div></section>
<section data-tab="cold"><h2>Cold start <span class="mut">— from tapping the app icon to the first usable screen</span></h2><div class="sum" id="s-cold"></div><div id="coldsec"></div></section>
<section data-tab="loading"><h2>Loading periods <span class="mut">— spinner/progress indicator detected on screen, and the request chain that kept it there</span></h2><div class="sum" id="s-loading"></div><div id="periods"></div></section>
<section data-tab="render"><h2>Render milestones <span class="mut">— screen changes and the response that preceded them</span></h2><div class="sum" id="s-render"></div><div class="card"><table id="bursts"></table></div></section>
<section data-tab="lifecycle"><h2>App lifecycle &amp; instrumentation <span class="mut">— from the device log</span></h2><div class="sum" id="s-lifecycle"></div><div id="lifesec"></div></section>
<section data-tab="timeline"><h2>Timeline</h2><div class="sum" id="s-timeline"></div>
<div class="ctl"><label>Zoom <input type="range" id="zoom" min="8" max="200" value="40"></label>
<span id="cats"></span><span class="mut">Hover the timeline to preview the screen; click a bar for details.</span></div>
<div class="ctl"><span style="font-size:12px">📏 <span id="measR"></span></span></div>
<div class="legend" id="leg"></div>
<div id="tl"><div id="tlL"></div><div id="tlR"></div></div></section>
<section data-tab="hosts"><h2>Hosts</h2><div class="sum" id="s-hosts"></div><div class="card"><table id="hosts"></table></div></section>
<section data-tab="requests"><h2>All requests</h2><div class="sum" id="s-requests"></div><div class="card"><table id="all"></table></div></section>
<section data-tab="logs"><h2>Device log <span class="mut" id="logsub"></span></h2><div class="sum" id="s-logs"></div><div id="logsec"></div></section>
</main>
<div id="peek"><img><div class="mono"></div></div><div id="tip"></div><div id="curTime"></div>
<div id="drawer"><span class="x" onclick="closeD()">×</span><div id="dbody"></div></div>
<script src="__PAYLOADS__"></script>
<script src="__LOGS__"></script>
<script>
const D=__DATA__,L=window.LOGS||[];
const $=s=>document.querySelector(s),R=D.reqs,esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const CAT=['api','auth','image','asset','logging','analytics','tunnel'],col=c=>`var(--${c})`;
const fb=n=>n>=1048576?(n/1048576).toFixed(2)+' MB':n>=1024?(n/1024).toFixed(1)+' KB':n+' B';
const s2=t=>(+t).toFixed(2)+'s';
const fmtT=t=>{t=Math.max(0,+t);const ms=Math.round(t*1000),m=Math.floor(ms/60000),sec=Math.floor(ms/1000)%60,r=ms%1000;return `${String(m).padStart(2,'0')}:${String(sec).padStart(2,'0')}.${String(r).padStart(3,'0')}`};
const fmtD=d=>{const a=Math.abs(d);return (d<0?'−':'')+(a<1?(a*1000).toFixed(0)+' ms':a.toFixed(3)+' s')};
const MEAS={a:null,b:null};let TL=null;
const thumbAt=(t,after)=>{let b=null;for(const [tt,n] of D.thumbs){if(after?tt>=t:tt<=t+1e-6){b=n;if(after)break}else if(!after)break}
  if(!b)b=D.thumbs[after?D.thumbs.length-1:0][1];return D.thumbBase+'/'+b};
const reqLink=id=>{const r=R[id];return `<a class="j" onclick="openReq(${id})">${esc(r.method)} ${esc(r.short)}${r.op?' ['+esc(r.op)+']':''}</a>`};
const PHC={'server wait (TTFB)':'#2563eb','connection setup':'#0891b2','download':'#0d9488','waiting for a connection':'#f472b6','on-device work':'#ef4444','Face ID prompt (user)':'#7c3aed','request (no phase data)':'#94a3b8','queued on device':'#f472b6','no request (on-device / user)':'#ef4444'};
const GAPC={wait:'#ef4444',bio:'#7c3aed',user:'#0891b2',idle:'#94a3b8'};
const GAPN={wait:'User waiting — spinner, no network',bio:'Face ID prompt',user:'User on screen',idle:'Idle'};
const UEC={launch:'#0f766e',tap:'#e11d48',drag:'#ea580c',bio:'#7c3aed',keyboard:'#0369a1',overlay:'#64748b',input:'#475569'};
const UEI={launch:'🚀',tap:'👆',drag:'✋',bio:'🔐',keyboard:'⌨️',overlay:'⏸',input:'•'};
const TC={'App code':'#0d9488','UI (UIKit)':'#2563eb','WebKit':'#9333ea','Network':'#0891b2','Security & auth':'#d97706','Location':'#65a30d','Prefs & IPC':'#64748b','Other':'#a3a3a3'};
const msf=v=>v<1?(v*1000).toFixed(0)+' ms':s2(v);
function phBar(r){const ph=r.ph;if(!ph)return '';const q=r.queue>0.05?r.queue:ph.wait;
 const parts=[['waiting for a connection',q],['connection setup',ph.setup],['server wait (TTFB)',ph.ttfb],['download',ph.download]].filter(x=>x[1]>0.0005);const tot=parts.reduce((a,b)=>a+b[1],0)||1;
 return `<div class="tb">${parts.map(([k,v])=>`<div title="${k} ${s2(v)}" style="width:${v/tot*100}%;background:${PHC[k]}"></div>`).join('')}</div><div class="tl"><span>${parts.map(([k,v])=>k.replace(' (TTFB)','')+' '+msf(v)).join(' · ')}</span><span>${esc(ph.proto||'')}${ph.reused?' · reused conn':' · new conn'}</span></div>`}
const catChips=l=>(l||[]).map(([c,n])=>`<span class="pill" style="background:${TC[c]||'#999'}">${esc(c)} ${n}</span>`).join(' ');
function threadPanel(t){if(!t)return '';return `<div class="thr"><div><h4>Main thread <span class="mut">${t.main} log events</span></h4>${catChips(t.mainCats)}
 ${t.main<5?'<div class="mut" style="margin-top:4px">Almost idle — the UI thread had little to do (waiting).</div>':''}
 ${t.notable.length?'<ul>'+t.notable.slice(0,6).map(x=>`<li><a class="j mono" onclick="seek(${x.t})">${s2(x.t)}</a> <b>${esc(x.lib)}</b> ${esc(x.msg)}</li>`).join('')+'</ul>':''}
 ${t.sync.length?`<div class="warnline">${t.sync.length} synchronous call(s) on the main thread</div>`:''}</div>
 <div><h4>Background threads <span class="mut">${t.bg} events on ${t.bgThreads} thread(s)</span></h4>${catChips(t.bgCats)}
 ${t.appBg.length?'<ul>'+t.appBg.slice(0,5).map(x=>`<li><span class="mono">${s2(x.t)}</span> <b>${esc(x.lib)}</b> ${esc(x.msg)}</li>`).join('')+'</ul>':''}</div></div>`}
$('#h1').textContent='MyVerizon App Performance';
$('#sub').textContent=`${D.meta.device?D.meta.device+' · ':''}Recorded ${D.meta.startISO} · ${D.end.toFixed(1)}s · ${R.length} device requests`+(D.appProcs.length?` · app log: ${D.appProcs.join(', ')}`:'')+` · generated ${D.generated}`;
const K=D.kpi;
$('#kpis').innerHTML=[[K.session+'s','Recording'],[K.loading+'s',`Loading indicator on screen (${(K.loading/K.session*100||0).toFixed(0)}%)`],[K.longest+'s','Longest loading period'],
 [K.api,'API calls ('+K.requests+' total)'],[K.apiP50+'s / '+K.apiP95+'s','API p50 / p95'],[fb(K.bytes),'Received'],[fb(K.wasted),'Duplicate image bytes'],[K.screens,'Full-screen transitions']]
 .map(([b,s])=>`<div class="kpi"><b>${b}</b><span>${s}</span></div>`).join('');
// findings
const SEVC={high:'var(--high)',med:'var(--med)',low:'var(--low)',info:'var(--info)'};
{const c={};D.findings.forEach(f=>c[f.sev]=(c[f.sev]||0)+1);$('#sevcount').innerHTML='<span class="sevc">'+['high','med','low','info'].filter(k=>c[k]).map(k=>`<span style="background:${SEVC[k]}">${c[k]} ${k}</span>`).join('')+'</span>'}
const shortT=t=>t.replace(/\s*\(\d+\.\d+s → \d+\.\d+s\)/,'').replace(/^Slow call on critical path: /,'Slow: ').replace(/ on the device before being sent/,' on device');
$('#findings').innerHTML=D.findings.length?D.findings.map(f=>`<div class="fc ${f.sev}"><div class="fr"><span class="sev ${f.sev}">${f.sev}</span>${f.t!=null?`<a class="j mono" onclick="seek(${f.t})">@${s2(f.t)}</a>`:''}</div>
 ${f.impact>=0.1?`<div class="fn">${s2(f.impact)}</div>`:''}<div class="ft">${esc(shortT(f.title))}</div>
 ${f.refs.length?'<div style="font-size:12px">'+f.refs.slice(0,3).map(reqLink).join(' · ')+'</div>':''}
 <details><summary>details</summary><div>${esc(f.detail)}</div><div style="margin-top:4px"><b>Fix:</b> ${esc(f.rec)}</div></details></div>`).join(''):'<div class="card mut">No findings.</div>';
// periods
function chainTable(ch){return `<table class="chain"><tr><th>Start</th><th>End</th><th>Dur</th><th>Step</th><th></th></tr>`+ch.map(c=>c.kind=='queue'?
 `<tr class="gap"><td>${s2(c.start)}</td><td>${s2(c.end)}</td><td>${s2(c.dur)}</td><td colspan=2>Queued on the device: the app issued ${reqLink(c.id)} but it had not reached the network yet (connection limit / app scheduling)</td></tr>`:c.kind=='gap'?
 `<tr class="gap"><td>${s2(c.start)}</td><td>${s2(c.end)}</td><td>${s2(c.dur)}</td><td colspan=2>No request on the path — ${c.bioInfo?`<b>Face ID prompt ${s2(c.bio.reduce((a,b)=>a+b[1]-b[0],0))}</b> (${s2(c.bioInfo[0].start)} → ${s2(c.bioInfo[0].end)}${c.bioInfo[0].matched?', matched at '+s2(c.bioInfo[0].matched):''}) + ${s2(c.dur-c.bio.reduce((a,b)=>a+b[1]-b[0],0))} on-device work`:'on-device work / user / OS prompt'}${c.canEval?` · biometric availability checked ${c.canEval}×`:''}${c.inflight.length?` (${c.inflight.length} other requests in flight)`:''}</td></tr>`:
 `<tr><td>${s2(c.start)}</td><td>${s2(c.end)}</td><td><b>${s2(c.dur)}</b></td><td><span class="pill" style="background:${col(c.cat)}">${c.cat}</span> ${reqLink(c.id)} <span class="mut">${esc(R[c.id].host)} · ${fb(R[c.id].size)}${R[c.id].ph?` · server wait <b>${msf(R[c.id].ph.ttfb)}</b>${R[c.id].ph.setup>=0.01?' · new connection '+msf(R[c.id].ph.setup):''}`:''}</span></td><td class="mut">${c.tight?'tight':''}</td></tr>`).join('')+'</table>'}
function bar(bd,total){return '<div class="bar">'+Object.entries(bd).map(([k,v])=>`<div title="${esc(k)} ${v}s" style="width:${v/total*100}%;background:${PHC[k]||col(k)}">${esc(k)} ${v}s</div>`).join('')+'</div>'}
function strip(s,e){const n=Math.min(10,Math.max(3,Math.round(e-s)));const ts=[s-0.4];for(let i=0;i<=n;i++)ts.push(s+(e-s)*i/n);ts.push(e+0.6);
 return '<div class="strip">'+ts.map(t=>`<figure><img src="${thumbAt(Math.max(0,t))}" onclick="seek(${Math.max(0,t)})"><figcaption>${s2(Math.max(0,t))}</figcaption></figure>`).join('')+'</div>'}
$('#periods').innerHTML=D.periods.length?D.periods.map((p,i)=>`<div class="card"><h3>#${i+1} · ${esc(p.name||'')} · ${s2(p.start)} → ${s2(p.end)} · <span style="color:var(--high)">${s2(p.dur)}</span> showing a loading indicator</h3>
 ${strip(p.start,p.end)}<div class="mut" style="font-size:12px;margin-top:6px">By request type</div>${bar(p.breakdown,p.dur)}
 ${p.phases&&Object.keys(p.phases).length?`<div class="mut" style="font-size:12px">By phase — where the time actually went</div>${bar(p.phases,p.dur)}`:''}${chainTable(p.chain)}${threadPanel(p.threads)}
 ${p.logs[1]>p.logs[0]?`<details><summary>${p.logs[1]-p.logs[0]} app log lines during this period</summary>${logTable(L.slice(p.logs[0],p.logs[1]))}</details>`:''}</div>`).join(''):'<div class="card mut">No loading indicator detected.</div>';
// bursts
$('#bursts').innerHTML='<tr><th>Before</th><th>After</th><th>First change → settled</th><th>Preceded by</th><th>Response → paint</th><th>Filled during render</th></tr>'+
 D.bursts.filter(b=>b.peak>=0.01).map(b=>`<tr><td><img class="thumb" src="${thumbAt(Math.max(0,b.start-0.2))}" onclick="seek(${b.start-0.2})"></td><td><img class="thumb" src="${thumbAt(b.end+0.3,true)}" onclick="seek(${b.end+0.3})"></td>
 <td>${b.screens?'<div><b>'+esc(b.screens)+'</b></div>':''}<a class="j" onclick="seek(${b.start})">${s2(b.start)}</a> → ${s2(b.end)} <span class="mut">(${s2(b.dur)}${b.peak>=0.1?', full-screen':''}${b.inLoading?', during loading':''})</span>
 ${b.chain?`<details><summary>critical path</summary>${bar(b.breakdown,Math.max(b.dur,0.01))}${chainTable(b.chain)}</details>`:''}</td>
 <td>${b.trigger!=null?reqLink(b.trigger):'<span class="mut">no response within 1.5s — user action / animation</span>'}</td>
 <td>${b.delay!=null?`<b style="color:${b.delay>=1?'var(--high)':b.delay>=0.5?'var(--med)':'inherit'}">${s2(b.delay)}</b>`:''}</td>
 <td>${b.filled.slice(0,6).map(reqLink).join('<br>')}${b.filled.length>6?`<br><span class="mut">+${b.filled.length-6} more</span>`:''}</td></tr>`).join('');
// lifecycle
if(D.life.length||D.appNet.markers){const an=D.appNet;$('#lifesec').innerHTML=`<div class="card">
 ${an.markers?`<p>App network events (SRE): <b>${an.matched}</b> of ${an.markers} matched to proxy requests. Request reached the proxy ${s2(an.minLead)}–${s2(an.medLead)} (min–median) after the app issued it${an.minLead<-0.05?' — <b style="color:var(--high)">negative: phone clock ahead of Mac; use --log-offset-ms</b>':' — clocks consistent'}.</p>`:''}
 <table><tr><th>t</th><th>Event</th><th>Detail</th></tr>${D.life.map(e=>`<tr><td><a class="j" onclick="seek(${e.t})">${s2(e.t)}</a></td><td><b>${esc(e.event)}</b></td><td class="mono">${e.id!=null?reqLink(e.id):esc(e.detail)}</td></tr>`).join('')}</table></div>`}
// hosts
$('#hosts').innerHTML='<tr><th>Host</th><th>Requests</th><th>Types</th><th>p50</th><th>p95</th><th>Received</th><th>CDN</th><th>Compressed text</th><th>Protocol</th><th>New conns</th><th>Server wait p50</th></tr>'+
 D.hosts.map(h=>`<tr><td class="mono">${esc(h.host)}</td><td>${h.n}</td><td>${Object.entries(h.cats).map(([k,v])=>`<span class="pill" style="background:${col(k)}">${k} ${v}</span>`).join(' ')}</td><td>${s2(h.p50)}</td><td>${s2(h.p95)}</td><td>${fb(h.bytes)}</td><td>${h.cdn?'yes':'<b>no</b>'}</td><td>${h.text?h.gzip+'/'+h.text:''}</td><td>${Object.entries(h.proto||{}).map(([k,v])=>esc(k)+' '+v).join(', ')}</td><td>${h.newConn||''}</td><td>${h.ttfb50!=null?s2(h.ttfb50):''}</td></tr>`).join('');
// all

$('#all').innerHTML='<tr><th>Screen</th><th>t</th><th>Type</th><th>Status</th><th>Request</th><th>Dur</th><th>Size</th></tr>'+R.map(r=>`<tr><td><img class="thumb" loading="lazy" src="${thumbAt(r.start)}" onclick="seek(${r.start})"></td>
 <td class="mono">${s2(r.start)}</td><td><span class="pill" style="background:${col(r.cat)}">${r.cat}</span></td><td>${r.status}</td><td class="mono" style="word-break:break-all">${reqLink(r.id)}<br><span class="mut">${esc(r.url.slice(0,160))}</span></td><td>${s2(r.dur)}</td><td>${fb(r.size)}</td></tr>`).join('');
// logs
function logTable(ls,max){max=max||400;return (ls.length>max?`<div class="mut">Showing ${max} of ${ls.length} lines.</div>`:'')+'<table class="mono">'+ls.slice(0,max).map(l=>`<tr class="lvl-${l.lvl}"><td><a class="j" onclick="seek(${l.t})">${s2(l.t)}</a></td><td>${esc(l.proc)}${l.sub?'('+esc(l.sub)+')':''}</td><td>${l.lvl}</td><td style="white-space:pre-wrap">${esc(l.msg.slice(0,400))}</td></tr>`).join('')+'</table>'}
if(D.logsTotal){$('#logsub').textContent=`— ${D.logsTotal.toLocaleString()} lines from ${D.appProcs.join(', ')} and system events about it`;$('#logsec').innerHTML=`
 <div class="card"><input id="lq" placeholder="filter (text, subsystem, e.g. SRE, HangTracer)…" style="width:340px;margin-bottom:8px">
 <select id="llv"><option value="">all levels</option><option value="noDebug" selected>hide Debug</option><option value="err">errors / faults</option></select>
 <select id="lsrc"><option value="">app + system</option><option value="app">app only</option><option value="system">system only</option></select>
 <label>from <input id="lfrom" type="number" step="0.1" style="width:70px"> to <input id="lto" type="number" step="0.1" style="width:70px"> s</label><div id="lt"></div></div>`;
 const lr=()=>{const q=$('#lq').value.toLowerCase(),lv=$('#llv').value,src=$('#lsrc').value,f=parseFloat($('#lfrom').value),t=parseFloat($('#lto').value);
  $('#lt').innerHTML=logTable(L.filter(l=>(!lv||(lv=='err'?(l.lvl=='Error'||l.lvl=='Fault'):l.lvl!='Debug'))&&(!src||l.src==src)&&(isNaN(f)||l.t>=f)&&(isNaN(t)||l.t<=t)&&(!q||(l.proc+' '+l.sub+' '+l.msg).toLowerCase().includes(q))),1000)};
 ['#lq','#llv','#lsrc','#lfrom','#lto'].forEach(k=>$(k).oninput=lr);lr()}
// timeline
const on=new Set(['api','auth','image','asset','logging']);
$('#cats').innerHTML=CAT.map(c=>`<label><input type="checkbox" data-c="${c}" ${on.has(c)?'checked':''}> <span class="pill" style="background:${col(c)}">${c}</span></label>`).join(' ');
document.querySelectorAll('#cats input').forEach(i=>i.onchange=()=>{i.checked?on.add(i.dataset.c):on.delete(i.dataset.c);draw()});
$('#leg').innerHTML='<span><i style="background:var(--busy)"></i>loading indicator</span><span><i style="background:var(--change)"></i>screen change</span><span><i style="background:var(--idle)"></i>idle</span><span><i style="border:2px solid var(--fg)"></i>on a critical path</span><span><i style="background:var(--queue)"></i>queued on device (app issued, not yet sent)</span><span><i style="background:var(--gap)"></i>log error</span><span><i style="background:var(--auth)"></i>app marker</span>'+(D.threads?' · threads: '+Object.entries(TC).map(([k,c])=>`<span><i style="background:${c}"></i>${k}</span>`).join(''):'')+'<span><i style="background:#7c3aed"></i>Face ID</span>'+((D.uev||[]).length?' · user events: '+[['launch','launch'],['tap','tap'],['drag','drag/scroll'],['bio','Face ID'],['keyboard','keyboard'],['overlay','system overlay']].map(([k,n])=>`<span>${UEI[k]} ${n}</span>`).join(''):'')+((D.gaps||[]).length?' · no-network gaps: '+Object.entries(GAPN).map(([k,n])=>`<span><i style="background:${GAPC[k]}"></i>${n}</span>`).join(''):'');
const crit=new Set();[...D.periods,...D.bursts].forEach(p=>(p.chain||[]).forEach(c=>c.kind=='req'&&crit.add(c.id)));
$('#zoom').oninput=draw;
function draw(){const pxs=+$('#zoom').value,W=Math.ceil(D.end*pxs)+40,X=t=>t*pxs+4,FH=96,RH=15;
 const rows=R.filter(r=>on.has(r.cat));const lanes=[['Screen',FH],['Screens',RH+4],['Screen state',RH],['Loading / render',RH]];if((D.uev||[]).length)lanes.push(['User events',RH+8]);if((D.gaps||[]).length)lanes.push(['No network',RH+4]);if(D.threads){lanes.push(['Main thread',RH*2]);lanes.push(['Background threads',RH*2])}if(D.logsTotal)lanes.push(['App log',RH*2]);lanes.push(['Time (mm:ss.ms)',RH]);
 let y=0,top={};lanes.forEach(([n,h])=>{top[n]=y;y+=h});const H=y+rows.length*RH+6;
 let s=`<svg width="${W}" height="${H}" xmlns="http://www.w3.org/2000/svg" font-size="10" font-family="system-ui"><defs>${Object.entries(GAPC).map(([k,c])=>`<pattern id="hatch-${k}" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="6" height="6" fill="${c}" opacity=".12"/><line x1="0" y1="0" x2="0" y2="6" stroke="${c}" stroke-width="2" opacity=".45"/></pattern>`).join('')}</defs>`;
 const tw=FH*0.46,step=Math.max(0.5,Math.ceil(tw/pxs/0.5)*0.5);
 for(let t=0;t<=D.end;t+=step)s+=`<image href="${thumbAt(t)}" x="${X(t)}" y="2" width="${tw}" height="${FH-4}" preserveAspectRatio="xMinYMin meet"/>`;
 D.states.forEach(([a,b,l])=>{if(l!='idle')s+=`<rect x="${X(a)}" y="${top['Screen state']+2}" width="${Math.max(1,(b-a)*pxs)}" height="${RH-4}" fill="var(--${l})"/>`});
 D.periods.forEach(p=>s+=`<rect x="${X(p.start)}" y="${top['Loading / render']+2}" width="${p.dur*pxs}" height="${RH-4}" fill="var(--busy)" opacity=".9"/><text x="${X(p.start)+3}" y="${top['Loading / render']+11}">${p.dur.toFixed(1)}s loading</text>`);
 D.bursts.filter(b=>b.peak>=0.01).forEach(b=>s+=`<rect x="${X(b.start)}" y="${top['Loading / render']+2}" width="${Math.max(2,b.dur*pxs)}" height="${RH-4}" fill="var(--change)"/>`);
 (D.bio||[]).forEach(b=>s+=`<rect x="${X(b.start)}" y="${top['Loading / render']}" width="${Math.max(2,(b.end-b.start)*pxs)}" height="${RH}" fill="#7c3aed"><title>Face ID prompt ${s2(b.end-b.start)}</title></rect><text x="${X(b.end)+2}" y="${top['Loading / render']+11}" fill="#7c3aed">Face ID</text>`);
 (D.screens||[]).forEach((sc,i)=>{const yb=top['Screens'],w=Math.max(1,(sc.end-sc.start)*pxs);s+=`<rect x="${X(sc.start)}" y="${yb+1}" width="${w}" height="${RH+2}" rx="3" fill="${sc.loading?'var(--busy)':i%2?'#e0e7ff':'#dbeafe'}" opacity=".9"><title>${esc(sc.name)} ${s2(sc.start)} → ${s2(sc.end)}</title></rect>`;
  if(w>30)s+=`<svg x="${X(sc.start)+3}" y="${yb}" width="${w-6}" height="${RH+4}"><text x="0" y="${RH-1}" fill="#1e3a8a" font-weight="600">${esc(sc.name)}</text></svg>`});
 if(D.threads){const T=D.threads,st=T.step;[['main','Main thread'],['bg','Background threads']].forEach(([k,lab])=>{const bins=T.bins[k],mx=Math.max(1,...bins.map(b=>b.reduce((a,c)=>a+c,0))),yb=top[lab],hh=RH*2-3;
  bins.forEach((b,i)=>{const tot=b.reduce((a,c)=>a+c,0);if(!tot)return;let yy=yb+RH*2-1;b.forEach((n,ci)=>{if(!n)return;const h=Math.max(0.8,n/mx*hh);yy-=h;s+=`<rect x="${X(i*st)}" y="${yy}" width="${Math.max(1,st*pxs-0.5)}" height="${h}" fill="${TC[T.cats[ci]]}"/>`});
   const ev=k=='main'?T.mainEvents.filter(x=>x.t>=i*st&&x.t<(i+1)*st).slice(0,4).map(x=>'\n• '+x.lib+': '+x.msg.slice(0,90)).join(''):'';
   s+=`<rect x="${X(i*st)}" y="${yb}" width="${Math.max(1,st*pxs)}" height="${RH*2}" fill="transparent"><title>${esc(lab)} ${s2(i*st)}–${s2((i+1)*st)}: ${tot} log events\n${b.map((n,ci)=>n?T.cats[ci]+' '+n:'').filter(Boolean).join(' · ')}${esc(ev)}</title></rect>`})})}
 if(D.logsTotal){const mx=Math.max(1,...D.logHist),yb=top['App log'];D.logHist.forEach((n,i)=>{if(n){const h=Math.max(1,n/mx*(RH*2-4));s+=`<rect x="${X(i*0.25)}" y="${yb+RH*2-2-h}" width="${Math.max(1,0.25*pxs-0.5)}" height="${h}" fill="var(--mut)" opacity=".5"><title>${n} log lines</title></rect>`}});
  D.errT.forEach(t=>s+=`<rect x="${X(t)}" y="${yb+1}" width="1.5" height="${RH*2-2}" fill="var(--gap)"/>`);
  D.marks.forEach(m=>s+=`<path d="M${X(m.t)} ${yb+1} l4 0 l-4 6z" fill="var(--auth)"/><text x="${X(m.t)+5}" y="${yb+8}" fill="var(--auth)">${esc(m.label)}</text>`)}
 {const st=[0.1,0.2,0.5,1,2,5,10,30].find(v=>v*pxs>=78)||60,mi=st/5,ty=top['Time (mm:ss.ms)'];
  for(let k=0;k*mi<=D.end+1e-9;k++){const t=+(k*mi).toFixed(3),maj=Math.abs(t/st-Math.round(t/st))<1e-6;
   s+=maj?`<line x1="${X(t)}" x2="${X(t)}" y1="${ty}" y2="${H}" stroke="var(--line)"/><text x="${X(t)+2}" y="${ty+11}" fill="var(--mut)" font-family="ui-monospace,Menlo,monospace" font-size="9.5">${fmtT(t)}</text>`
         :`<line x1="${X(t)}" x2="${X(t)}" y1="${ty+RH-4}" y2="${ty+RH}" stroke="var(--mut)" opacity=".5"/>`}}
 D.periods.forEach(p=>s+=`<rect x="${X(p.start)}" y="${top['Time (mm:ss.ms)']}" width="${p.dur*pxs}" height="${H}" fill="var(--busy)" opacity=".08"/>`);
 (D.gaps||[]).forEach((g,i)=>{const gc=GAPC[g.kind]||'#94a3b8',x0=X(g.start),w=Math.max(2,g.dur*pxs);
  s+=`<rect x="${x0}" y="${y}" width="${w}" height="${H-y}" fill="url(#hatch-${g.kind})" opacity=".55" data-gap="${i}" style="cursor:pointer"/>`;
  s+=`<rect x="${x0}" y="${top['No network']+1}" width="${w}" height="${RH+2}" rx="3" fill="${gc}" data-gap="${i}" style="cursor:pointer"/>`;
  if(w>40)s+=`<svg x="${x0+3}" y="${top['No network']}" width="${w-6}" height="${RH+4}" style="pointer-events:none"><text x="0" y="${RH-1}" fill="#fff" font-weight="600">${esc(g.label)}</text></svg>`});
 (D.uev||[]).forEach((u,i)=>{const yb=top['User events'],x0=X(u.t),c=UEC[u.kind]||'#64748b',w=Math.max(0,(u.end-u.t)*pxs),cy=yb+(RH+8)/2;
  if(u.kind=='overlay'){s+=`<rect x="${x0}" y="${yb+2}" width="${Math.max(2,w)}" height="${RH+4}" rx="3" fill="${c}" opacity=".25" data-uev="${i}" style="cursor:pointer"/>`;return}
  if(u.kind=='tap'||u.kind=='launch'||u.kind=='drag')s+=`<line x1="${x0}" x2="${x0}" y1="${yb}" y2="${H}" stroke="${c}" stroke-width="1.5" stroke-dasharray="3 3" opacity=".8" style="pointer-events:none"/>`;
  if(u.kind=='bio'){s+=`<rect x="${x0}" y="${cy-5}" width="${Math.max(3,w)}" height="10" rx="5" fill="${c}" data-uev="${i}" style="cursor:pointer"/>`;(u.steps||[]).forEach(st=>s+=`<circle cx="${X(st.t)}" cy="${cy}" r="2.5" fill="#fff" style="pointer-events:none"/>`)}
  else if(u.kind=='drag')s+=`<rect x="${x0}" y="${cy-4}" width="${Math.max(6,w)}" height="8" rx="4" fill="${c}" data-uev="${i}" style="cursor:pointer"/>`;
  else s+=`<circle cx="${x0}" cy="${cy}" r="6" fill="${c}" stroke="#fff" stroke-width="1.5" data-uev="${i}" style="cursor:pointer" ${u.inferred?'stroke-dasharray="2 2" opacity=".75"':''}/>`;
  s+=`<text x="${x0+(u.kind=='bio'?Math.max(3,w)+3:9)}" y="${cy+3.5}" fill="${c}" font-weight="700" style="pointer-events:none">${UEI[u.kind]||''} ${esc(u.kind=='launch'?'launch':u.kind=='bio'?'Face ID':u.inferred?'tap?':u.label.split(' ')[0].toLowerCase())}</text>`});
 rows.forEach((r,i)=>{const yy=y+i*RH+2,w=Math.max(2,r.dur*pxs);
  if(r.issued!=null&&r.queue>0.05)s+=`<rect x="${X(r.issued)}" y="${yy+3}" width="${r.queue*pxs}" height="${RH-10}" fill="var(--queue)" class="rq" data-id="${r.id}" style="cursor:pointer"/>`;
  s+=`<rect class="rq" data-id="${r.id}" x="${X(r.start)}" y="${yy}" width="${w}" height="${RH-4}" rx="2" fill="${col(r.cat)}" ${crit.has(r.id)?'stroke="var(--fg)" stroke-width="1.5"':''} style="cursor:pointer"/>`;
  if(r.dur*pxs<200)s+=`<text x="${X(r.start)+w+3}" y="${yy+9}" fill="var(--mut)">${(r.dur*1000).toFixed(0)}ms</text>`});
 s+=`<g id="meas"></g><line id="cur" y1="0" y2="${H}" stroke="var(--high)" opacity=".6" style="pointer-events:none"/><g id="curL" style="pointer-events:none"><rect id="curB" rx="3" height="15" fill="var(--fg)" opacity=".85"/><text id="curT" fill="var(--bg)" font-family="ui-monospace,Menlo,monospace" font-size="10.5" font-weight="600"></text></g></svg>`;$('#tlR').innerHTML=s;
 TL={pxs,X,H,ty:top['Time (mm:ss.ms)']};drawMeasure();
 $('#tlL').innerHTML=lanes.map(([n,h])=>`<div style="height:${h}px;line-height:${h}px;font-weight:600">${n}</div>`).join('')+rows.map(r=>`<div onclick="openReq(${r.id})" data-id="${r.id}"><span class="pill" style="background:${col(r.cat)}">${r.method}</span> ${esc(r.host.split('.').slice(-2,-1)[0]||'')} ${esc(r.short)}</div>`).join('');
 const svg=$('#tlR svg');svg.onclick=e=>{const id=e.target.dataset.id,gi=e.target.dataset.gap,ui=e.target.dataset.uev;if(id)openReq(+id);else if(ui!=null)seek(D.uev[+ui].t);else if(gi!=null)openGap(+gi);
  else{const bx=svg.getBoundingClientRect(),t=Math.max(0,(e.clientX-bx.left-4)/pxs);if(MEAS.a==null||MEAS.b!=null){MEAS.a=t;MEAS.b=null}else MEAS.b=t;drawMeasure()}};
 svg.onmousemove=e=>{const bx=svg.getBoundingClientRect(),t=Math.max(0,(e.clientX-bx.left-4)/pxs);$('#cur').setAttribute('x1',e.clientX-bx.left);$('#cur').setAttribute('x2',e.clientX-bx.left);
  const id=e.target.dataset&&e.target.dataset.id,gi=e.target.dataset&&e.target.dataset.gap,ui=e.target.dataset&&e.target.dataset.uev;if(id){$('#peek').style.display='none';showTip(+id,e)}else if(ui!=null){$('#peek').style.display='none';showUevTip(+ui,e)}else if(gi!=null){$('#peek').style.display='none';showGapTip(+gi,e)}else{hideTip();$('#peek').style.display='block';$('#peek img').src=thumbAt(t);$('#peek div').textContent=fmtT(t)}
  const ct=$('#curTime');ct.textContent=fmtT(t)+(MEAS.a!=null&&MEAS.b==null?'  Δ '+fmtD(t-MEAS.a):'');ct.style.display='block';
  ct.style.left=(e.clientX-ct.offsetWidth-9)+'px';ct.style.top=(e.clientY-ct.offsetHeight/2)+'px';
  const lx=e.clientX-bx.left,txt=fmtT(t)+(MEAS.a!=null&&MEAS.b==null?'  Δ '+fmtD(t-MEAS.a):''),T=$('#curT');T.textContent=txt;const tw=txt.length*6.6+8,ox=lx+tw+6>svg.width.baseVal.value?lx-tw-4:lx+4;
  T.setAttribute('x',ox+4);T.setAttribute('y',TL.ty-3);$('#curB').setAttribute('x',ox);$('#curB').setAttribute('y',TL.ty-14);$('#curB').setAttribute('width',tw);$('#curL').style.display=''};
 svg.onmouseleave=()=>{$('#peek').style.display='none';hideTip();$('#curL').style.display='none';$('#curTime').style.display='none'};
 $('#tlL').onmousemove=e=>{const d=e.target.closest('[data-id]');d?showTip(+d.dataset.id,e):hideTip()};$('#tlL').onmouseleave=hideTip}
// hover card
const ISS={},CPATH={};
D.findings.forEach(f=>f.refs.forEach(id=>{(ISS[id]=ISS[id]||[]).includes(f)||ISS[id].push(f)}));
D.periods.forEach((p,i)=>(p.chain||[]).forEach(c=>c.kind=='req'&&(CPATH[c.id]=CPATH[c.id]||[]).push(`blocks loading period #${i+1} (${s2(p.start)} → ${s2(p.end)}, ${s2(p.dur)})`)));
D.bursts.forEach(b=>(b.chain||[]).forEach(c=>c.kind=='req'&&(CPATH[c.id]=CPATH[c.id]||[]).push(`on the path of the render at ${s2(b.start)} → ${s2(b.end)}`)));
D.bursts.forEach(b=>{if(b.trigger!=null)(CPATH[b.trigger]=CPATH[b.trigger]||[]).push(`its response preceded a screen change by ${s2(b.delay)}`)});
function localIssues(r){const out=[];
 if(r.queue>=0.3&&!(ISS[r.id]||[]).some(f=>f.kind=='queue'))out.push(['high',`Waited ${s2(r.queue)} on the device before reaching the network`]);
 if(r.status>=400||(r.status==0&&r.cat!='tunnel'))out.push(['high',`HTTP ${r.status||'failed'}`]);
 if((r.cat=='api'||r.cat=='auth')&&r.dur>=1.5&&!(ISS[r.id]||[]).some(f=>f.kind=='slow'))out.push([r.dur>=3?'high':'med',`Slow: ${s2(r.dur)}`]);
 if(/json|javascript|text\/|xml|css|html/i.test(r.mime)&&r.size>=2048&&!r.enc&&r.status==200&&!(ISS[r.id]||[]).some(f=>f.kind=='compress'))out.push(['low','Response not compressed']);
 if(r.cat=='tunnel')out.push(['info','Not decrypted (enable SSL Proxying for this host)']);
 return out}
function showTip(id,e){const r=R[id],tip=$('#tip'),q=r.issued!=null&&r.queue>0.05?r.queue:0,tot=q+r.dur||1;
 const iss=[...(ISS[id]||[]).map(f=>[f.sev,f.title]),...localIssues(r)];
 tip.innerHTML=`<div class="tt"><img src="${thumbAt(r.start)}"><div style="flex:1;min-width:0">
  <h5><span class="pill" style="background:${col(r.cat)}">${r.cat}</span> ${esc(r.method)} ${esc(r.short)}${r.op?' <span class="mut">['+esc(r.op)+']</span>':''}</h5>
  <div class="u">${esc(r.url.length>150?r.url.slice(0,150)+'…':r.url)}</div>
  <div class="row"><span>Status</span><span>${r.status||'—'} · ${esc(r.mime||'—')} · ${fb(r.size)}${r.enc?' ('+esc(r.enc)+')':''}</span></div>
  <div class="row"><span>Time</span><span class="mono">${fmtT(r.start)} → ${fmtT(r.end)} · <b>${fmtD(r.dur)}</b></span></div>
  ${r.astart!=null?`<div class="row"><span>App measured</span><span>${s2(r.aend-r.astart)} (issued ${s2(r.astart)})</span></div>`:''}
  ${r.ph?phBar(r):`<div class="tb">${q?`<div style="width:${q/tot*100}%;background:var(--queue)"></div>`:''}<div style="width:${r.dur/tot*100}%;background:${col(r.cat)}"></div></div>
  <div class="tl"><span>${q?'queued '+s2(q)+' · ':''}network ${s2(r.dur)}</span><span>${esc(r.http||'')}${r.cdn?' · CDN':''}${r.cond?' · conditional':''}</span></div>`}
  ${(CPATH[id]||[]).map(t=>`<div class="cp">● ${esc(t)}</div>`).join('')}
 </div></div>
 <div class="iss">${iss.length?iss.map(([sv,t])=>`<div><span class="sev ${sv}" style="font-size:10px;padding:1px 5px">${sv}</span> ${esc(t)}</div>`).join(''):'<div class="ok">No issues flagged for this request.</div>'}
 <div class="mut" style="margin-top:4px">Click for request / response payloads</div></div>`;
 tip.style.display='block';const w=tip.offsetWidth,h=tip.offsetHeight;
 let x=e.clientX+16,y=e.clientY+16;if(x+w>innerWidth-8)x=e.clientX-w-16;if(y+h>innerHeight-8)y=innerHeight-h-8;y=Math.max(8,y);
 tip.style.left=Math.max(8,x)+'px';tip.style.top=y+'px'}
function hideTip(){$('#tip').style.display='none'}
function gapBody(g,full){const lg=g.log||{},hl=(lg.highlights||[]).slice(0,full?40:6),th=lg.threads;
 return `<div class="row"><span>Screen</span><span>${esc([...new Set(g.screens)].join(' → ')||'—')}</span></div>
 ${g.spinner?`<div class="row"><span>Spinner visible</span><span><b>${s2(g.spinner)}</b></span></div>`:''}${g.faceid?`<div class="row"><span>Face ID prompt</span><span><b>${s2(g.faceid)}</b></span></div>`:''}
 ${g.changes.length?`<div class="row"><span>Screen changes</span><span>${g.changes.map(s2).join(', ')}${g.userChanges?' ('+g.userChanges+' user)':''}</span></div>`:''}
 ${g.analytics?`<div class="row"><span>Analytics beacons</span><span>${g.analytics}</span></div>`:''}
 ${th?`<div class="row"><span>Threads</span><span>main ${th.main} · background ${th.bg} on ${th.bgThreads}</span></div><div style="margin:3px 0">${catChips(th.mainCats.slice(0,3))}</div>`:''}
 ${lg.lines?`<div class="row"><span>App log</span><span>${lg.lines.toLocaleString()} lines · ${lg.errors} errors</span></div>`:''}
 ${hl.length?`<div class="iss">${hl.map(h=>`<div><span class="mono">${s2(h.t)}</span> <span class="b ${h.sev}" style="font-size:9px;color:#fff;border-radius:3px;padding:0 4px;background:${{crit:'var(--high)',error:'var(--high)',warn:'var(--med)',key:'var(--auth)',net:'var(--api)',app:'var(--image)'}[h.sev]||'var(--info)'}">${SEVN[h.sev]||h.sev}</span> ${esc(h.text)}</div>`).join('')}</div>`:''}`}
function showGapTip(i,e){const g=D.gaps[i],tip=$('#tip');
 tip.innerHTML=`<div class="tt"><img src="${thumbAt(g.start+g.dur/2)}"><div style="flex:1;min-width:0"><h5><span class="pill" style="background:${GAPC[g.kind]}">${esc(GAPN[g.kind])}</span> ${s2(g.dur)} with no network</h5>
  <div class="u"><span class="mono">${fmtT(g.start)} → ${fmtT(g.end)}</span> · ${esc(g.what)}</div>${gapBody(g,false)}</div></div><div class="mut" style="margin-top:4px">Click for full details</div>`;
 tip.style.display='block';const w=tip.offsetWidth,h=tip.offsetHeight;let x=e.clientX+16,y=e.clientY+16;if(x+w>innerWidth-8)x=e.clientX-w-16;if(y+h>innerHeight-8)y=innerHeight-h-8;tip.style.left=Math.max(8,x)+'px';tip.style.top=Math.max(8,y)+'px'}
function showUevTip(i,e){const u=D.uev[i],tip=$('#tip'),c=UEC[u.kind];
 const rows=[['Screen',esc(u.screen||'—')],['Time',`<span class="mono">${fmtT(u.t)}${u.end>u.t+0.005?' → '+fmtT(u.end)+'</span> ('+msf(u.end-u.t)+')':'</span>'}`]];
 if(u.detail)rows.push(['Detail',esc(u.detail)]);if(u.result)rows.push(['Result',esc(u.result)]);
 (u.steps||[]).forEach(st=>rows.push([esc(st.k),`+${msf(st.t-u.t)} <span class="mono">(${fmtT(st.t)})</span>`]));
 if(u.reqAfter!=null)rows.push(['First request',`${reqLink(u.req)} issued <b>+${msf(u.reqAfter)}</b>`]);
 if(u.screenAfter!=null)rows.push(['Screen responded',`<b style="color:${u.screenAfter>=1?'var(--high)':u.screenAfter>=0.3?'var(--med)':'inherit'}">+${msf(u.screenAfter)}</b>`]);
 tip.innerHTML=`<div class="tt"><img src="${thumbAt(u.t)}"><div style="flex:1;min-width:0"><h5><span class="pill" style="background:${c}">${UEI[u.kind]||''} ${esc(u.label)}</span></h5>
  ${rows.map(([k,v])=>`<div class="row"><span>${k}</span><span>${v}</span></div>`).join('')}${u.inferred?'<div class="mut" style="margin-top:4px">Inferred from the video (no device log in this run).</div>':''}</div></div><div class="mut" style="margin-top:4px">Click to see the screen at this moment</div>`;
 tip.style.display='block';const w=tip.offsetWidth,h=tip.offsetHeight;let x=e.clientX+16,y=e.clientY+16;if(x+w>innerWidth-8)x=e.clientX-w-16;if(y+h>innerHeight-8)y=innerHeight-h-8;tip.style.left=Math.max(8,x)+'px';tip.style.top=Math.max(8,y)+'px'}
function openGap(i){const g=D.gaps[i];drawer(`<h3 style="margin-right:24px"><span class="pill" style="background:${GAPC[g.kind]}">${esc(GAPN[g.kind])}</span> ${s2(g.dur)} with no network · ${s2(g.start)} → ${s2(g.end)}</h3>
 <p>${esc(g.what)}</p><div class="strip"><figure><img src="${thumbAt(g.start)}"><figcaption>start ${s2(g.start)}</figcaption></figure><figure><img src="${thumbAt(g.start+g.dur/2)}"><figcaption>middle</figcaption></figure><figure><img src="${thumbAt(g.end)}"><figcaption>end ${s2(g.end)}</figcaption></figure></div>
 <div id="tip-like" style="font-size:12px">${gapBody(g,true)}</div>${threadPanel(g.log&&g.log.threads)}`)}
draw();
// drawer
function drawer(h){$('#dbody').innerHTML=h;$('#drawer').classList.add('open')}
function closeD(){$('#drawer').classList.remove('open');const v=$('#dbody video');v&&v.pause()}
function vid(t){return `<video id="dv" src="${esc(D.video)}" muted playsinline preload="auto" controls></video>`}
function seek(t){t=Math.max(0,t);drawer(`<h3>Screen at ${s2(t)}</h3>${vid()}<p class="mut">Use the video controls to step through.</p>`);const v=$('#dv');v.addEventListener('loadedmetadata',()=>{v.currentTime=t},{once:true})}
const P=()=>window.PAYLOADS||[];
function pretty(text,mime){if(text==null)return null;let t=text.trim();
 if(/json/.test(mime)||/^[\[{]/.test(t)){try{return hl(JSON.stringify(JSON.parse(t),null,2))}catch(e){}}
 if(/x-www-form-urlencoded/.test(mime)){try{return esc([...new URLSearchParams(t)].map(([k,v])=>k+' = '+v).join('\n'))}catch(e){}}
 return esc(text)}
function hl(j){return esc(j).replace(/(&quot;(?:\\.|[^&]|&(?!quot;))*?&quot;)(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/g,(m,s,c,b)=>s?`<span class="${c?'jk':'js'}">${s}</span>${c||''}`:b?`<span class="jb">${m}</span>`:`<span class="jn">${m}</span>`)}
function kv(rows){return rows&&rows.length?'<table class="kv">'+rows.map(([k,v])=>`<tr><td>${esc(k)}</td><td>${esc(v)}</td></tr>`).join('')+'</table>':'<span class="mut">none</span>'}
let CUR=null;
function copyBody(which){const p=P()[CUR];navigator.clipboard&&navigator.clipboard.writeText(which=='rq'?p.rqB:p.rsB)}
function bodyBlock(b,k,mime,r,which){
 if(k=='image'&&b)return `<img src="data:${esc(mime)};base64,${b}" style="max-width:100%;border:1px solid var(--line);border-radius:8px">`;
 if(k=='image-too-large')return '<span class="mut">Image too large to embed — open the URL directly.</span>';
 if(k=='binary')return '<span class="mut">Binary body (not shown).</span>';
 if(b==null)return '<span class="mut">No body'+(r.method=='CONNECT'?' — tunnel not decrypted (enable SSL Proxying for this host).':'.')+'</span>';
 return `${k=='truncated'?'<div class="mut">Truncated to 512 KB.</div>':''}<pre class="body">${pretty(b,mime)}</pre>`}
function tab(id,name){CUR=id;const r=R[id],p=P()[id];document.querySelectorAll('.tabs a').forEach(a=>a.classList.toggle('on',a.dataset.t==name));
 ['ov','rq','rs'].forEach(t=>{const el=$('#t_'+t);if(el)el.style.display=t==name?'block':'none'});
 if(name=='ov')return;const v=$('#dv');v&&v.pause();
 if(!p){$('#t_'+name).innerHTML='<span class="mut">Payloads not loaded ('+esc('__PAYLOADS__')+' missing next to the report).</span>';return}
 if(name=='rq')$('#t_rq').innerHTML=`<table class="kv"><tr><td>Method</td><td>${esc(r.method)} ${esc(p.http)}</td></tr><tr><td>URL</td><td>${esc(r.url)}</td></tr>${p.srv?`<tr><td>Server IP</td><td>${esc(p.srv)}</td></tr>`:''}</table>
  ${p.q.length?`<div class="sec"><h4>Query parameters</h4></div>${kv(p.q)}`:''}
  <div class="sec"><h4>Body ${p.rqM?'<span class="mut">'+esc(p.rqM)+'</span>':''} ${p.rqB?'<span class="mut">· '+fb(p.rqB.length)+'</span>':''}</h4>${p.rqB?'<button class="btn" onclick="copyBody(\'rq\')">Copy</button>':''}</div>${bodyBlock(p.rqB,p.rqK,p.rqM,r,'rq')}
  <details><summary>Headers (${p.rqH.length})</summary>${kv(p.rqH)}</details>`;
 if(name=='rs')$('#t_rs').innerHTML=`<table class="kv"><tr><td>Status</td><td>${r.status} ${esc(p.st)}</td></tr><tr><td>Type</td><td>${esc(p.rsM||'—')}</td></tr><tr><td>Size</td><td>${fb(r.size)}${r.enc?' (sent '+esc(r.enc)+')':''}</td></tr><tr><td>Time</td><td>${s2(r.dur)}</td></tr></table>
  <div class="sec"><h4>Body</h4>${p.rsB&&p.rsK!='image'?'<button class="btn" onclick="copyBody(\'rs\')">Copy</button>':''}</div>${bodyBlock(p.rsB,p.rsK,p.rsM,r,'rs')}
  <details><summary>Headers (${p.rsH.length})</summary>${kv(p.rsH)}</details>`}
function openReq(id,t){const r=R[id],tm=Object.entries(r.timings).map(([k,v])=>`${k} ${v}ms`).join(' · ');
 const b=D.bursts.find(b=>b.start>=r.end-0.05);
 drawer(`<h3 style="margin-right:24px">${esc(r.method)} ${esc(r.short)}${r.op?' <span class="mut">['+esc(r.op)+']</span>':''}</h3>
 <div class="tabs"><a data-t="ov" onclick="tab(${id},'ov')">Overview</a><a data-t="rq" onclick="tab(${id},'rq')">Request</a><a data-t="rs" onclick="tab(${id},'rs')">Response</a></div>
 <div id="t_rq"></div><div id="t_rs"></div><div id="t_ov">${vid()}<p class="mono" style="word-break:break-all">${esc(r.url)}</p>
 <p><span class="pill" style="background:${col(r.cat)}">${r.cat}</span> ${r.status} · ${fb(r.size)} received · ${fb(r.reqSize)} sent${r.op?' · '+esc(r.op):''}</p>
 <p>${s2(r.start)} → ${s2(r.end)} · <b>${s2(r.dur)}</b>${tm?'<br><span class="mut">'+tm+'</span>':''}</p>
 ${r.astart!=null?`<p>App (SRE log): issued ${s2(r.astart)} → completed ${s2(r.aend)} · app-measured <b>${s2(r.aend-r.astart)}</b><br>${r.queue>0.05?`<b style="color:var(--high)">Waited ${s2(r.queue)} on the device before reaching the network.</b>`:'<span class="mut">Sent immediately ('+(r.queue*1000).toFixed(0)+' ms to proxy).</span>'}</p>`:''}
 ${r.ph?`<p style="margin-bottom:2px"><b>iOS timing</b> <span class="mut">(measured on the phone; connection/TLS are to the proxy)</span></p>${phBar(r)}
 <p class="mut">DNS ${msf(r.ph.dns)} · connect ${msf(r.ph.connect)} · TLS ${msf(r.ph.tls)} · server wait ${msf(r.ph.ttfb)} · download ${msf(r.ph.download)} · ${fb(r.ph.respBytes)} on the wire${r.ph.cache?' · cache hit':''} · connection ${esc(r.ph.conn||'?')}</p>`:''}
 <p>HTTP: ${esc(r.http||'?')}${r.rid?' · request id '+esc(r.rid):''}</p>
 <p>CDN: ${r.cdn?'yes':'no'} · Encoding: ${esc(r.enc||'none')}<br>Cache-Control: ${esc(r.cache||'none')}${r.cond?' · conditional request':''}</p>
 <p><a class="j" onclick="dv.currentTime=${r.start}">▶ screen at start</a> · <a class="j" onclick="dv.currentTime=${r.end}">▶ at end</a>${b?` · <a class="j" onclick="dv.currentTime=${b.start}">▶ next screen change (+${(b.start-r.end).toFixed(2)}s)</a>`:''}</p>
 <div style="clear:both"></div><div class="strip"><figure><img src="${thumbAt(r.start)}"><figcaption>at start</figcaption></figure><figure><img src="${thumbAt(r.end)}"><figcaption>at end</figcaption></figure>${b?`<figure><img src="${thumbAt(b.end+0.2,true)}"><figcaption>after next change</figcaption></figure>`:''}</div></div>`);
 const v=$('#dv');v.addEventListener('loadedmetadata',()=>{v.currentTime=r.start},{once:true});tab(id,t||'ov')}
function drawMeasure(){const g=$('#meas');if(!g||!TL)return;const {X,H,ty}=TL;let h='';const mk=(t,l)=>`<line x1="${X(t)}" x2="${X(t)}" y1="0" y2="${H}" stroke="#0f172a" stroke-width="1.5"/><rect x="${X(t)-9}" y="${ty+1}" width="18" height="13" rx="3" fill="#0f172a"/><text x="${X(t)}" y="${ty+11}" fill="#fff" font-size="10" font-weight="700" text-anchor="middle">${l}</text>`;
 if(MEAS.a!=null)h+=mk(MEAS.a,'A');if(MEAS.b!=null){const a=Math.min(MEAS.a,MEAS.b),b=Math.max(MEAS.a,MEAS.b),lab=`Δ ${fmtD(b-a)}  (${fmtT(a)} → ${fmtT(b)})`;
  h+=mk(MEAS.b,'B')+`<rect x="${X(a)}" y="0" width="${Math.max(1,X(b)-X(a))}" height="${H}" fill="#0f172a" opacity=".06"/><line x1="${X(a)}" x2="${X(b)}" y1="${ty-8}" y2="${ty-8}" stroke="#0f172a" stroke-width="1.5"/>`
   +`<rect x="${(X(a)+X(b))/2-lab.length*3.2-6}" y="${ty-22}" width="${lab.length*6.4+12}" height="16" rx="4" fill="#0f172a"/><text x="${(X(a)+X(b))/2}" y="${ty-10}" fill="#fff" font-size="10.5" font-weight="700" text-anchor="middle" font-family="ui-monospace,Menlo,monospace">${lab}</text>`}
 g.innerHTML=h;const r=$('#measR');if(r)r.innerHTML=MEAS.a==null?'Click the timeline to set point A, then B, to measure.':MEAS.b==null?`A = <b>${fmtT(MEAS.a)}</b> — click to set B`:`A = <b>${fmtT(MEAS.a)}</b> · B = <b>${fmtT(MEAS.b)}</b> · <b style="color:var(--acc)">Δ ${fmtD(Math.abs(MEAS.b-MEAS.a))}</b> <a class="j" onclick="MEAS.a=MEAS.b=null;drawMeasure()">clear</a>`}
document.addEventListener('keydown',e=>{if(e.key=='Escape'){closeD();MEAS.a=MEAS.b=null;drawMeasure()}});
// tab summaries
const kc=(v,l,sub,tone)=>`<div class="kpi${tone?' '+tone:''}"><b>${v}</b><span>${l}</span>${sub?`<em title="${String(sub).replace(/<[^>]+>/g,'').replace(/"/g,'&quot;')}">${sub}</em>`:''}</div>`;
function summ(id,cards,take){const el=$('#s-'+id);if(el)el.innerHTML=`<div class="kpis">${cards.join('')}</div>`+(take.length?`<div class="card take"><h3>Key takeaways</h3><ul>${take.map(t=>`<li>${t}</li>`).join('')}</ul></div>`:'')}
const sum=a=>a.reduce((x,y)=>x+y,0),med=a=>{if(!a.length)return 0;const b=[...a].sort((x,y)=>x-y);return b[Math.floor(b.length/2)]};
const union=iv=>{let t=0,e=-1e9;[...iv].sort((a,b)=>a[0]-b[0]).forEach(([a,b])=>{if(b>e){t+=b-Math.max(a,e);e=b}});return t};
const rq=id=>`${reqLink(id)}`;
// loading
{const P=D.periods,tot=sum(P.map(p=>p.dur)),agg={};P.forEach(p=>Object.entries(p.breakdown).forEach(([k,v])=>agg[k]=(agg[k]||0)+v));
 const top=Object.entries(agg).sort((a,b)=>b[1]-a[1])[0],creq=P.flatMap(p=>p.chain.filter(c=>c.kind=='req')).sort((a,b)=>b.dur-a.dur),slow=creq[0];
 const lg=[...P].sort((a,b)=>b.dur-a.dur)[0],nr=agg['no request (on-device / user)']||0,qd=agg['queued on device']||0;
 summ('loading',[kc(P.length,'Loading periods'),
  kc(s2(tot),'Spinner on screen',`${(tot/D.end*100).toFixed(0)}% of the recording`,tot/D.end>=0.25?'bad':''),
  kc(lg?s2(lg.dur):'—','Longest period',lg?`${s2(lg.start)} → ${s2(lg.end)}`:'',lg&&lg.dur>=5?'bad':lg&&lg.dur>=2?'warn':''),
  kc(top?esc(top[0]):'—','Biggest cause',top?`${s2(top[1])} · ${(top[1]/tot*100).toFixed(0)}% of spinner time`:''),
  kc(slow?s2(slow.dur):'—','Slowest blocking call',slow?esc(R[slow.id].short+(R[slow.id].op?' ['+R[slow.id].op+']':'')):'',slow&&slow.dur>=3?'bad':slow&&slow.dur>=1.5?'warn':''),
  kc(s2(nr),'No request in flight','on-device work, user or OS prompt',nr>=1?'warn':''),
  ...(qd?[kc(s2(qd),'Queued on device','app issued it, not yet sent','bad')]:[])],
  P.map((p,i)=>{const t=Object.entries(p.breakdown)[0],rs=p.chain.filter(c=>c.kind=='req').sort((a,b)=>b.dur-a.dur).slice(0,2);
   return `<b>#${i+1} ${esc(p.name||'')}</b> ${s2(p.start)} → ${s2(p.end)} (<b>${s2(p.dur)}</b>) — mostly <b>${esc(t?t[0]:'?')}</b> ${t?s2(t[1]):''}${rs.length?': '+rs.map(c=>`${rq(c.id)} ${s2(c.dur)}`).join(', '):''}`}))}
// render
{const B=D.bursts.filter(b=>b.peak>=0.01),full=B.filter(b=>b.peak>=0.1),user=B.filter(b=>b.trigger==null),wd=B.filter(b=>b.delay!=null);
 const worst=[...wd].sort((a,b)=>b.delay-a.delay)[0],sl=[...B].sort((a,b)=>b.dur-a.dur);
 summ('render',[kc(B.length,'Screen changes'),kc(full.length,'Full-screen transitions'),
  kc(sl[0]?s2(sl[0].dur):'—','Longest render',sl[0]?`first change → settled, at ${s2(sl[0].start)}`:'',sl[0]&&sl[0].dur>=3?'bad':sl[0]&&sl[0].dur>=1.5?'warn':''),
  kc(worst?s2(worst.delay):'—','Worst response → paint',worst?esc(R[worst.trigger].short):'',worst&&worst.delay>=1?'bad':worst&&worst.delay>=0.5?'warn':''),
  kc(wd.length?s2(med(wd.map(b=>b.delay))):'—','Median response → paint'),
  kc(user.length,'Changes not preceded by a response','user actions / animations')],
  [...sl.filter(b=>b.dur>=1).slice(0,3).map(b=>`Render ${s2(b.start)} → ${s2(b.end)} took <b>${s2(b.dur)}</b>${b.breakdown?' — '+Object.entries(b.breakdown).slice(0,2).map(([k,v])=>esc(k)+' '+s2(v)).join(', '):''}${b.filled.length?`; ${b.filled.length} responses arrived while it filled in`:''}`),
   ...wd.filter(b=>b.delay>=0.5).map(b=>`${rq(b.trigger)} (${fb(R[b.trigger].size)}) returned <b>${s2(b.delay)}</b> before the screen changed — client-side parsing/layout`)])}
// lifecycle
if(D.logsTotal){const ev=n=>D.life.find(e=>e.event==n),L0=ev('Launch requested'),fc=ev('First loading indicator cleared'),fa=ev('First API request issued'),dfl=ev('didFinishLaunching');
 const an=D.appNet,qd=R.filter(r=>r.queue>=0.3),un=R.filter(r=>(r.cat=='api'||r.cat=='auth')&&r.astart==null),post=R.filter(r=>r.aend!=null).map(r=>Math.abs(r.aend-r.end));
 const rel=e=>e?(L0?'+'+s2(e.t-L0.t):s2(e.t)):'—',mq=qd.length?Math.max(...qd.map(r=>r.queue)):0;
 summ('lifecycle',[kc(L0&&fc?s2(fc.t-L0.t):'—','Launch → first content',L0?'cold launch':'no launch in this recording',L0&&fc&&fc.t-L0.t>=3?'bad':''),
  kc(rel(dfl),'didFinishLaunching',L0?'after launch':''),kc(rel(fa),'First API call issued',fa?esc(R[fa.id].short):''),
  kc(`${an.matched}/${an.markers}`,'App network events matched','via e2eRequestId'),
  kc(qd.length,'Requests queued ≥0.3s on device',qd.length?'max '+s2(mq):'',qd.length?'bad':''),
  kc(un.length,'API calls the app does not time',un.length?s2(sum(un.map(r=>r.dur)))+' of network time not in app monitoring':'',un.length?'warn':'')],
  [...(L0&&fc?[`Cold launch: app code ready at <b>${rel(dfl)}</b>, first API call at <b>${rel(fa)}</b>, first content at <b>${rel(fc)}</b>.`]:[]),
   ...(un.length?[`Not covered by the app's own instrumentation: ${un.map(r=>`${rq(r.id)} ${s2(r.dur)}`).join(', ')}.`]:[]),
   ...(qd.length?[`${qd.length} request(s) waited on the device before being sent (max <b>${s2(mq)}</b>): ${qd.slice(0,4).map(r=>rq(r.id)).join(', ')}.`]:[]),
   ...(post.length?[`Requests reached the proxy ${s2(an.minLead)}–${s2(an.medLead)} after the app issued them; the app logged completion within ${s2(med(post))} (median) of the proxy`+(med(post)<0.1?' — response handling adds little time.':'.')]:[])])}
// timeline
{const nt=R.filter(r=>r.cat!='tunnel'&&r.dur>0);let pk=0,pkT=0;nt.forEach(x=>{const c=nt.filter(o=>o.start<=x.start&&o.end>x.start).length;if(c>pk){pk=c;pkT=x.start}});
 const busy=union(R.filter(r=>r.cat=='api'||r.cat=='auth').map(r=>[r.start,r.end])),cats={},bins={};R.forEach(r=>{cats[r.cat]=(cats[r.cat]||0)+1;if(r.cat!='tunnel'){const b=Math.floor(r.start);bins[b]=(bins[b]||0)+1}});
 const bb=Object.entries(bins).sort((a,b)=>b[1]-a[1])[0],load=sum(D.periods.map(p=>p.dur));
 summ('timeline',[kc(s2(D.end),'Recording'),kc(R.length,'Requests',Object.entries(cats).map(([k,v])=>k+' '+v).join(' · ')),
  kc(pk,'Peak concurrent requests','at '+s2(pkT)),kc(s2(busy),'API/auth calls in flight',`${(busy/D.end*100).toFixed(0)}% of the recording`),
  kc(s2(load),'Loading indicator on screen','',load/D.end>=0.25?'bad':''),...(D.logsTotal?[kc((Math.max(...D.logHist)*4).toLocaleString()+'/s','Peak app log rate')]:[])],
  [...((D.uev||[]).filter(u=>u.kind=='tap'||u.kind=='launch').map(u=>`${UEI[u.kind]} <b>${esc(u.label)}</b> at ${s2(u.t)} on “${esc(u.screen||'?')}” → first request +${u.reqAfter!=null?msf(u.reqAfter):'—'}, screen responded +${u.screenAfter!=null?msf(u.screenAfter):'—'}`)),
   ...((D.gaps||[]).length?[`<b>${D.gaps.length}</b> no-network gaps totalling <b>${s2(sum(D.gaps.map(g=>g.dur)))}</b>; <b>${s2(sum(D.gaps.filter(g=>g.kind=='wait').map(g=>g.dur)))}</b> of it with a spinner on screen (user waiting on on-device work), ${s2(sum(D.gaps.map(g=>g.faceid)))} Face ID. Hover the hatched bands for what was happening.`]:[]),
   ...(bb?[`Busiest second: <b>${bb[0]}–${+bb[0]+1}s</b> with ${bb[1]} requests started.`]:[]),
   'Pink = waited on the device · outlined = on a critical path · shaded columns = loading indicator. Hover a bar for timing and issues; click for payloads.'])}
// hosts
{const H=D.hosts,api=H.filter(h=>(h.cats.api||0)+(h.cats.auth||0)>0),sl=[...api].sort((a,b)=>b.p50-a.p50),hv=[...H].sort((a,b)=>b.bytes-a.bytes)[0];
 const th=H.filter(h=>h.cats.analytics||h.cats.tunnel),thN=sum(th.map(h=>(h.cats.analytics||0)+(h.cats.tunnel||0))),nc=api.filter(h=>!h.cdn),tun=H.filter(h=>Object.keys(h.cats).length==1&&h.cats.tunnel);
 summ('hosts',[kc(H.length,'Hosts contacted'),kc(sl[0]?s2(sl[0].p50):'—','Slowest API host (p50)',sl[0]?esc(sl[0].host):'',sl[0]&&sl[0].p50>=2?'bad':sl[0]&&sl[0].p50>=1?'warn':''),
  kc(hv?fb(hv.bytes):'—','Most data from',hv?esc(hv.host):''),kc(`${(thN/Math.max(1,R.length)*100).toFixed(0)}%`,'Analytics / untracked requests',thN+' requests'),
  kc(nc.length,'API hosts without CDN',nc.map(h=>esc(h.host)).join(', '),nc.length?'warn':''),kc(tun.length,'Hosts not decrypted','enable SSL Proxying to inspect')],
  sl.slice(0,3).map(h=>`<span class="mono">${esc(h.host)}</span>: ${h.n} calls, p50 <b>${s2(h.p50)}</b>, p95 ${s2(h.p95)}${h.cdn?'':' — <b>no CDN</b>'}`))}
// requests
{const api=R.filter(r=>r.cat=='api'||r.cat=='auth'),er=R.filter(r=>r.cat!='tunnel'&&(r.status>=400||r.status==0)),c3=R.filter(r=>r.status==304),bg=[...R].sort((a,b)=>b.size-a.size)[0],sl=[...api].sort((a,b)=>b.dur-a.dur);
 summ('requests',[kc(R.length,'Requests'),kc(api.length,'API / auth calls','median '+s2(med(api.map(r=>r.dur)))),
  kc(sl[0]?s2(sl[0].dur):'—','Slowest call',sl[0]?esc(sl[0].short+(sl[0].op?' ['+sl[0].op+']':'')):'',sl[0]&&sl[0].dur>=3?'bad':''),
  kc(bg?fb(bg.size):'—','Largest response',bg?esc(bg.short):'',bg&&bg.size>=250000?'warn':''),kc(er.length,'Errors','HTTP ≥400 or failed',er.length?'bad':''),
  kc(c3.length,'304 revalidations','round trips with nothing new',c3.length>=10?'warn':''),kc(fb(D.kpi.wasted),'Duplicate image bytes','',D.kpi.wasted>=100000?'warn':'')],
  [`Slowest API calls: ${sl.slice(0,5).map(r=>`${rq(r.id)} <b>${s2(r.dur)}</b>`).join(', ')}.`,...(er.length?[`Failed: ${er.slice(0,5).map(r=>rq(r.id)+' '+r.status).join(', ')}.`]:[])])}
// logs
if(D.logsTotal){const ap=L.filter(l=>l.src=='app'),er=L.filter(l=>l.lvl=='Error'||l.lvl=='Fault'),sb={},sg={};ap.forEach(l=>{const k=l.sub||'(app code)';sb[k]=(sb[k]||0)+1});
 er.forEach(l=>{const k=l.msg.replace(/\d+/g,'#').slice(0,90);sg[k]=(sg[k]||0)+1});const ts=Object.entries(sb).sort((a,b)=>b[1]-a[1]);
 summ('logs',[kc(D.logsTotal.toLocaleString(),'Log lines',`${ap.length.toLocaleString()} app · ${(D.logsTotal-ap.length).toLocaleString()} system`),
  kc(er.length,'Errors / faults','',er.length?'warn':''),kc((Math.max(...D.logHist)*4).toLocaleString()+'/s','Peak log rate'),
  kc((sb['(app code)']||0).toLocaleString(),"App's own log lines",'no system subsystem'),kc(D.appNet.markers,'SRE network events',D.marks.length+' other app markers'),
  kc(esc(ts[0]?ts[0][0]:'—'),'Busiest subsystem',ts[0]?ts[0][1].toLocaleString()+' lines':'')],
  [`Top subsystems: ${ts.slice(0,5).map(([k,v])=>esc(k)+' '+v.toLocaleString()).join(', ')}.`,
   ...Object.entries(sg).sort((a,b)=>b[1]-a[1]).slice(0,4).map(([k,v])=>`${v}× <span class="mono">${esc(k)}</span>`)])}
// budgets + savings (overview)
if(D.budgets&&D.budgets.length)$('#budgets').innerHTML=`<h2>Budgets <span class="sevc"><span style="background:#16a34a">${D.budgets.filter(b=>b.pass).length} met</span><span style="background:var(--high)">${D.budgets.filter(b=>b.pass===false).length} over</span></span></h2><div class="kpis">`+D.budgets.map(b=>{const v=b.value,sc=Math.max(b.max*1.25,v||0);
 return `<div class="kpi budget ${b.pass==null?'na':b.pass?'pass':'fail'}"><b>${v==null?'n/a':s2(v)} <small>/ ${s2(b.max)}</small></b><span>${esc(b.label)}</span>
 ${v==null?'':`<div class="gauge"><div style="width:${Math.min(100,v/sc*100)}%;background:${b.pass?'#16a34a':'var(--high)'}"></div><i style="left:${b.max/sc*100}%"></i></div>`}</div>`}).join('')+'</div>';
{const sv=(D.savings||[]).slice(0,6),mx=Math.max(0.01,...sv.map(x=>x.save));
 $('#savings').innerHTML=`<h3>Biggest wins <span class="mut" style="font-weight:400;font-size:12px">— seconds saved, upper bound per fix</span></h3>`+(sv.length?`<div class="hb">${sv.map(x=>`<div class="lb" title="${esc(x.title+' — '+x.basis+' — on: '+x.spans.join(', '))}">${esc(x.title.replace(/ \(.*\)$/,''))}</div><div class="tr"><div style="width:${x.save/mx*100}%;background:#16a34a"></div></div><div class="v">−${s2(x.save)}</div>`).join('')}</div>`:'<div class="mut">Nothing significant to save.</div>')}
// overview infographics
{const P=D.periods.filter(p=>p.phases&&Object.keys(p.phases).length),mx=Math.max(0.01,...D.periods.map(p=>p.dur)),used=new Set();
 const row=p=>{const ph=Object.keys(p.phases||{}).length?p.phases:p.breakdown;Object.keys(ph).forEach(k=>used.add(k));
  return `<div class="lb" title="${esc(p.name||'')}">${esc(p.name||'Loading')}</div><div class="tr" style="width:${p.dur/mx*100}%">${Object.entries(ph).map(([k,v])=>`<div title="${esc(k)} ${s2(v)}" style="width:${v/p.dur*100}%;background:${PHC[k]||col(k)}"></div>`).join('')}</div><div class="v">${s2(p.dur)}</div>`};
 $('#ov-spin').innerHTML=`<h3>Where the spinner time went</h3>`+(D.periods.length?`<div class="hb">${D.periods.map(row).join('')}</div><div class="lg">${[...used].map(k=>`<span><i style="background:${PHC[k]||col(k)}"></i>${esc(k)}</span>`).join('')}</div>`:'<div class="mut">No loading indicator detected.</div>')}
{const C=D.cold,cl=['#7c3aed','#2563eb','#ef4444','#d97706','#0d9488'];
 $('#ov-cold').innerHTML=C?`<h3>Cold start <span style="color:${C.total>=3?'var(--high)':'inherit'}">${s2(C.total)}</span></h3>
  <div class="journey" style="padding:0"><figure><img src="${thumbAt(Math.max(0,C.launch-0.3))}" style="width:58px"><figcaption>Launch</figcaption></figure>${C.phases.map((p,i)=>`<div class="arr" style="min-width:44px"><b style="color:${cl[i%5]}">${s2(p.dur)}</b>→</div><figure><img src="${thumbAt(p.end+0.25,true)}" style="width:58px"><figcaption>${esc(p.to)}</figcaption></figure>`).join('')}</div>
  <div class="bar" style="height:14px;margin-top:8px">${C.phases.map((p,i)=>`<div title="${esc(p.name)} ${s2(p.dur)}" style="width:${p.dur/C.total*100}%;background:${cl[i%5]}"></div>`).join('')}</div>
  <div class="lg">${C.phases.flatMap(p=>p.log.highlights.filter(h=>h.sev=='crit').slice(0,1)).map(h=>`<span style="color:var(--high)">● ${esc(h.text)}</span>`).join('')}</div>`
  :`<h3>Cold start</h3><div class="mut">No app launch in this recording.</div>`}
{const api=R.filter(r=>r.cat=='api'||r.cat=='auth').sort((a,b)=>b.dur-a.dur).slice(0,6),mx=Math.max(0.01,...api.map(r=>r.dur));
 $('#ov-ep').innerHTML=`<h3>Slowest API calls</h3><div class="hb">${api.map(r=>{const sv=r.ph?Math.min(r.dur,r.ph.ttfb):0;return `<div class="lb"><a class="j" onclick="openReq(${r.id})">${esc(r.short)}${r.op?' ['+esc(r.op)+']':''}</a></div><div class="tr" style="width:${r.dur/mx*100}%"><div style="width:${sv/r.dur*100}%;background:${PHC['server wait (TTFB)']}"></div><div style="flex:1;background:#94a3b8"></div></div><div class="v">${s2(r.dur)}</div>`}).join('')}</div>
 <div class="lg"><span><i style="background:${PHC['server wait (TTFB)']}"></i>server wait</span><span><i style="background:#94a3b8"></i>other / unmeasured</span></div>`}
// cold start
const SEVN={crit:'critical',error:'error',warn:'warning',key:'milestone',net:'network',app:'app log',info:'system'};
if(D.cold){const C=D.cold,ph=C.phases;$('#c-cold').textContent=s2(C.total);
 const th=ph.map(p=>p.log.threads).filter(Boolean);
 summ('cold',[kc(s2(C.total),'Launch → '+esc(C.first),'cold start',C.total>=3?'bad':C.total>=2?'warn':''),
  ...ph.map(p=>kc(s2(p.dur),esc(p.name),`${(p.dur/C.total*100).toFixed(0)}% of cold start · ${p.log.lines.toLocaleString()} log lines`,p.dur>=2?'bad':p.dur>=1?'warn':'')),
  ...(th.length?[kc(sum(th.map(t=>t.main)),'Main-thread log events','during cold start'),kc(sum(th.map(t=>t.bg)),'Background log events',Math.max(...th.map(t=>t.bgThreads))+' threads at peak')]:[]),
  kc(sum(ph.map(p=>p.log.errors)),'Errors / faults','',sum(ph.map(p=>p.log.errors))?'warn':'')],
  ph.flatMap(p=>p.log.highlights.filter(h=>['crit','error','warn'].includes(h.sev)).slice(0,3).map(h=>`<b>${esc(p.name)}</b> — ${esc(h.text)}`)));
 const shot=t=>`<img src="${thumbAt(t,true)}" onclick="seek(${t})">`;
 let j=`<div class="card"><h3>Journey</h3><div class="journey"><figure>${shot(Math.max(0,C.launch-0.3))}<figcaption>Launch<br>${s2(C.launch)}</figcaption></figure>`;
 ph.forEach(p=>{j+=`<div class="arr"><b>${s2(p.dur)}</b>→</div><figure>${shot(p.end+0.25)}<figcaption>${esc(p.to)}<br>${s2(p.end)}</figcaption></figure>`});
 j+=`</div><div class="bar" style="height:26px">${ph.map((p,i)=>`<div style="width:${p.dur/C.total*100}%;background:${['#7c3aed','#2563eb','#ef4444','#d97706','#0d9488'][i%5]}" title="${esc(p.name)} ${s2(p.dur)}">${esc(p.name)} ${s2(p.dur)}</div>`).join('')}</div></div>`;
 j+=ph.map((p,i)=>`<div class="card"><h3>${i+1}. ${esc(p.name)} · ${s2(p.start)} → ${s2(p.end)} · <span style="color:${p.dur>=2?'var(--high)':p.dur>=1?'var(--med)':'inherit'}">${s2(p.dur)}</span></h3>
  <div class="strip"><figure>${shot(p.start)}<figcaption>start ${s2(p.start)}</figcaption></figure><figure>${shot(p.end+0.25)}<figcaption>${esc(p.to)}</figcaption></figure></div>
  <div class="cs"><div class="hl"><h4 style="margin:6px 0 2px">What the app was doing</h4><ul>${p.log.highlights.map(h=>`<li><a class="j mono" onclick="seek(${h.t})">${s2(h.t)}</a> <span class="b ${h.sev}">${SEVN[h.sev]||h.sev}</span>${esc(h.text)}</li>`).join('')||'<li class="mut">No app log activity captured.</li>'}</ul></div>
  <div><h4 style="margin:6px 0 2px">Network in this phase</h4>${p.reqs.length?'<ul style="margin:4px 0;padding-left:16px;font-size:12px">'+p.reqs.filter(id=>R[id].cat!='analytics').slice(0,8).map(id=>`<li>${reqLink(id)} ${s2(R[id].dur)}${R[id].ph?' · server '+msf(R[id].ph.ttfb):''}</li>`).join('')+(p.reqs.length>8?`<li class="mut">+${p.reqs.length-8} more (incl. analytics)</li>`:'')+'</ul>':'<div class="mut">None.</div>'}
  <h4 style="margin:8px 0 2px">Log volume</h4><div style="font-size:12px">${p.log.lines.toLocaleString()} lines · ${p.log.errors} errors · ${p.log.subs.map(([k,v])=>esc(k)+' '+v).join(', ')}</div></div></div>
  ${threadPanel(p.log.threads)}</div>`).join('');
 $('#coldsec').innerHTML=j}
// tabs
const has={lifecycle:!!D.logsTotal,logs:!!D.logsTotal,cold:!!D.cold};
const WHY={cold:'No app launch in this recording',lifecycle:'No device log captured for this run',logs:'No device log captured for this run'};
document.querySelectorAll('#tabs a').forEach(a=>{const t=a.dataset.tab;if(has[t]===false){a.classList.add('dis');a.title=WHY[t]||'No data in this run';a.setAttribute('aria-disabled','true')}else a.onclick=()=>showTab(t)});
$('#c-loading').textContent=D.periods.length||'';$('#c-render').textContent=D.bursts.filter(b=>b.peak>=0.01).length||'';
$('#c-hosts').textContent=D.hosts.length;$('#c-requests').textContent=R.length;$('#c-logs').textContent=D.logsTotal?D.logsTotal.toLocaleString():'';
function showTab(t,keep){if(has[t]===false)t='overview';document.querySelectorAll('#tabs a').forEach(a=>a.classList.toggle('on',a.dataset.tab==t));
 document.querySelectorAll('main>section').forEach(x=>x.classList.toggle('on',x.dataset.tab==t));
 if(!keep)history.replaceState(null,'','#tab='+t);window.scrollTo(0,0);try{localStorage.setItem('perfTab',t)}catch(e){}}
{const h=location.hash,t=(h.match(/tab=(\w+)/)||[])[1];let saved=null;try{saved=localStorage.getItem('perfTab')}catch(e){}
 showTab(t||saved||'overview',true);const m=h.match(/req=(\d+)(?::(\w+))?/);if(m&&R[+m[1]])openReq(+m[1],m[2])}
</script></body></html>
"""

if __name__ == "__main__":
    main()
