#!/usr/bin/env python3
"""
Door counter.

Watches a side-on camera view of a doorway, finds motion with background
subtraction, and counts people passing through, by direction. Two modes:

  zones  camera right beside the door; a passing person fills most of the
         image. Direction comes from which edge of the image sees motion
         first.
  line   camera further back; people are tracked as separate objects across
         a counting line.

Crossing in the configured "in" direction adds 1 to the occupancy; the
opposite direction subtracts 1. Staff can add missed crossings with +1
buttons on the preview page; these are logged separately.

Every event is written to a local SQLite queue first, then uploaded in
batches to a Google Apps Script web app that appends it to a Google Sheet.
If the network is down, events wait in the queue and are sent later.

Once a day at the reset time (default 04:00) the occupancy is set back to 0.
The occupancy just before the reset is recorded as the day's "residual":
0 means the ins and outs balanced; anything else shows the counting drift.

Images are only ever held in memory; nothing is saved to disk except the
event log. The optional preview web page shows a live low-resolution view
for positioning the camera and tuning the settings.

Requires: python3-opencv (which brings numpy). Everything else is stdlib.
"""

import argparse
import configparser
import json
import logging
import math
import os
import signal
import socket
import sqlite3
import sys
import threading
import time
import urllib.request
import uuid
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np

log = logging.getLogger("door-counter")

DEFAULTS = {
    "camera": {
        "source": "0",
        "width": "320",
        "height": "240",
        "fps": "30",
        "process_width": "160",
        "rotate": "0",
        "crop_left": "0.0",
        "crop_right": "1.0",
        "crop_top": "0.0",
        "crop_bottom": "1.0",
    },
    "counting": {
        "mode": "zones",
        "direction_in": "left_to_right",
        "zone_width": "0.2",
        "zone_on": "0.25",
        "zone_off": "0.08",
        "max_transit_seconds": "1.5",
        "clear_frames": "3",
        "same_direction_gap_seconds": "1.0",
        "line_position": "0.5",
        "band": "0.08",
        "min_area": "0.015",
        "merge_vertical_splits": "true",
        "merge_gap": "0.03",
        "max_match_distance": "0.25",
        "max_missing_frames": "8",
        "lost_memory_frames": "600",
        "revive_distance": "0.1",
        "history": "300",
        "presence_learning_factor": "10",
        "var_threshold": "25",
        "blur": "5",
        "kernel": "3",
        "dilate_iterations": "2",
        "max_foreground": "0.6",
        "warmup_frames": "60",
    },
    "schedule": {
        "reset_time": "04:00",
        "heartbeat_minutes": "60",
    },
    "upload": {
        "url": "",
        "secret": "",
        "device_name": "front-door",
        "interval_seconds": "60",
        "batch_size": "200",
    },
    "storage": {
        "db_path": "/var/lib/door-counter/events.db",
        "keep_days": "365",
    },
    "preview": {
        "enabled": "true",
        "host": "0.0.0.0",
        "port": "8080",
        "width": "320",
        "fps": "5",
        "show_mask": "true",
        "allow_corrections": "true",
    },
}


def load_config(path):
    cp = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    cp.read_dict(DEFAULTS)
    if path:
        if not cp.read(path):
            raise SystemExit(f"Config file not found: {path}")
    direction = cp.get("counting", "direction_in").strip().lower()
    if direction not in ("left_to_right", "right_to_left"):
        raise SystemExit("counting.direction_in must be left_to_right or right_to_left")
    if cp.get("counting", "mode").strip().lower() not in ("zones", "line"):
        raise SystemExit("counting.mode must be zones or line")
    if cp.getint("camera", "rotate") not in ROTATIONS:
        raise SystemExit("camera.rotate must be 0, 90, 180 or 270")
    for lo, hi in (("crop_left", "crop_right"), ("crop_top", "crop_bottom")):
        a, b = cp.getfloat("camera", lo), cp.getfloat("camera", hi)
        if not (0.0 <= a < b <= 1.0) or b - a < 0.1:
            raise SystemExit(f"camera.{lo} and camera.{hi} must be between 0 and 1, "
                             f"with {lo} at least 0.1 less than {hi}")
    return cp


ROTATIONS = {
    0: None,
    90: cv2.ROTATE_90_CLOCKWISE,
    180: cv2.ROTATE_180,
    270: cv2.ROTATE_90_COUNTERCLOCKWISE,
}


def parse_hhmm(text):
    h, m = text.strip().split(":")
    h, m = int(h), int(m)
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(text)
    return h, m


def business_date(now, reset_h, reset_m):
    """The 'day' an event belongs to. A day runs from reset time to reset time,
    so a crossing at 1am counts toward the previous day."""
    return (now - timedelta(hours=reset_h, minutes=reset_m)).date().isoformat()


# --------------------------------------------------------------------------
# Local event queue and state (SQLite)
# --------------------------------------------------------------------------

class Store:
    def __init__(self, path):
        d = os.path.dirname(os.path.abspath(path))
        os.makedirs(d, exist_ok=True)
        self.path = path
        self.lock = threading.Lock()
        # timeout: wait for the other process if the counter and a manual
        # --clean run touch the database at the same moment.
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        with self.lock:
            self.db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS events(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    uid TEXT UNIQUE NOT NULL,
                    ts TEXT NOT NULL,
                    business_date TEXT NOT NULL,
                    event TEXT NOT NULL,
                    occupancy INTEGER,
                    residual INTEGER,
                    fps REAL,
                    sent INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS events_unsent ON events(sent, seq);
                CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT);
                """
            )
            self.db.commit()

    def record(self, ev, state=None):
        """Insert an event and update state values in one transaction."""
        with self.lock:
            self.db.execute(
                "INSERT INTO events(uid, ts, business_date, event, occupancy, residual, fps)"
                " VALUES(:uid, :ts, :business_date, :event, :occupancy, :residual, :fps)",
                ev,
            )
            for k, v in (state or {}).items():
                self.db.execute(
                    "INSERT INTO state(key, value) VALUES(?, ?)"
                    " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (k, str(v)),
                )
            self.db.commit()

    def get_state(self, key, default=None):
        with self.lock:
            row = self.db.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_state(self, key, value):
        with self.lock:
            self.db.execute(
                "INSERT INTO state(key, value) VALUES(?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )
            self.db.commit()

    def unsent(self, limit):
        with self.lock:
            rows = self.db.execute(
                "SELECT uid, ts, business_date, event, occupancy, residual, fps"
                " FROM events WHERE sent = 0 ORDER BY seq LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_sent(self, uids):
        with self.lock:
            self.db.executemany("UPDATE events SET sent = 1 WHERE uid = ?", [(u,) for u in uids])
            self.db.commit()

    def unsent_count(self):
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM events WHERE sent = 0").fetchone()[0]

    def prune(self, days):
        """Delete events already uploaded that are older than `days` days.
        Unsent events are never deleted. Returns the number deleted."""
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        with self.lock:
            cur = self.db.execute("DELETE FROM events WHERE sent = 1 AND ts < ?", (cutoff,))
            self.db.commit()
            return cur.rowcount

    def vacuum(self):
        """Shrink the file after deleting (deleted space is otherwise reused
        but not returned to the disk)."""
        with self.lock:
            self.db.execute("VACUUM")
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def stats(self):
        with self.lock:
            total, unsent, oldest, newest = self.db.execute(
                "SELECT COUNT(*), SUM(sent = 0), MIN(ts), MAX(ts) FROM events").fetchone()
        size = 0
        for suffix in ("", "-wal", "-shm"):
            p = self.path + suffix
            if os.path.exists(p):
                size += os.path.getsize(p)
        return {"events": total, "unsent": unsent or 0, "oldest": oldest, "newest": newest,
                "size_kb": round(size / 1024.0, 1)}

    def day_counts(self, bd):
        with self.lock:
            rows = self.db.execute(
                "SELECT event, COUNT(*) AS n FROM events WHERE business_date = ?"
                " AND event IN ('in', 'out', 'manual_in', 'manual_out') GROUP BY event",
                (bd,),
            ).fetchall()
        counts = {"in": 0, "out": 0, "manual_in": 0, "manual_out": 0}
        for r in rows:
            counts[r["event"]] = r["n"]
        return counts


def local_ip():
    """This device's address on the local network, or None if there is no
    network. Connecting a UDP socket sends nothing; it just picks the
    interface that would be used."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # reserved test address, never contacted
            ip = s.getsockname()[0]
    except OSError:
        return None
    return ip if ip and not ip.startswith("127.") else None


def make_event(kind, bd, occupancy, residual=None, fps=None, now=None):
    now = now or datetime.now()
    return {
        "uid": uuid.uuid4().hex,
        "ts": now.strftime("%Y-%m-%d %H:%M:%S"),
        "business_date": bd,
        "event": kind,
        "occupancy": occupancy,
        "residual": residual,
        "fps": round(fps, 1) if fps is not None else None,
    }


# --------------------------------------------------------------------------
# Detection and tracking
# --------------------------------------------------------------------------

class Detector:
    def __init__(self, cfg, proc_w, proc_h):
        c = cfg["counting"]
        self.bg = cv2.createBackgroundSubtractorMOG2(
            history=c.getint("history"),
            varThreshold=c.getfloat("var_threshold"),
            detectShadows=True,
        )
        k = max(1, c.getint("kernel"))
        self.kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
        b = c.getint("blur")
        self.blur = b if b % 2 == 1 else b + 1
        self.dilate_iterations = c.getint("dilate_iterations")
        self.min_area = c.getfloat("min_area") * proc_w * proc_h
        self.merge = c.getboolean("merge_vertical_splits")
        self.merge_gap = c.getfloat("merge_gap") * proc_w
        # While something is being tracked, learn the background more slowly
        # so a person pausing in the doorway isn't absorbed into it.
        factor = max(1.0, c.getfloat("presence_learning_factor"))
        self.slow_rate = 1.0 / (c.getint("history") * factor)

    def detect(self, gray, presence=False):
        if self.blur > 1:
            gray = cv2.GaussianBlur(gray, (self.blur, self.blur), 0)
        fg = self.bg.apply(gray, learningRate=self.slow_rate if presence else -1)
        # MOG2 marks shadows as 127; keep only real foreground (255).
        _, mask = cv2.threshold(fg, 200, 255, cv2.THRESH_BINARY)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, self.kernel)
        if self.dilate_iterations > 0:
            mask = cv2.dilate(mask, self.kernel, iterations=self.dilate_iterations)
        fg_fraction = cv2.countNonZero(mask) / float(mask.size)
        contours = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[-2]
        boxes = []
        for cnt in contours:
            area = cv2.contourArea(cnt)
            x, y, w, h = cv2.boundingRect(cnt)
            boxes.append([x, y, w, h, area])
        if self.merge:
            boxes = merge_vertical_splits(boxes, self.merge_gap)
        blobs = []
        for x, y, w, h, area in boxes:
            if area < self.min_area:
                continue
            blobs.append((x + w / 2.0, y + h / 2.0, (x, y, w, h)))
        return blobs, mask, fg_fraction


def merge_vertical_splits(boxes, gap):
    """Join pieces of one person. Seen from the side, a person often splits
    into head/torso/legs pieces stacked vertically (their horizontal extents
    overlap), or into left/right pieces sitting side by side at the same
    height (a horizontal gap of a few pixels)."""
    boxes = [list(b) for b in boxes]
    merged = True
    while merged:
        merged = False
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                a, b = boxes[i], boxes[j]
                x_overlap = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
                y_overlap = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
                stacked = x_overlap > 0.5 * min(a[2], b[2])
                side_by_side = -x_overlap <= gap and y_overlap > 0.5 * min(a[3], b[3])
                if stacked or side_by_side:
                    x0 = min(a[0], b[0])
                    y0 = min(a[1], b[1])
                    x1 = max(a[0] + a[2], b[0] + b[2])
                    y1 = max(a[1] + a[3], b[1] + b[3])
                    boxes[i] = [x0, y0, x1 - x0, y1 - y0, a[4] + b[4]]
                    del boxes[j]
                    merged = True
                    break
            if merged:
                break
    return boxes


class Track:
    __slots__ = ("id", "x", "y", "box", "missing", "side")

    def __init__(self, tid, x, y, box, side):
        self.id = tid
        self.x, self.y, self.box = x, y, box
        self.missing = 0
        self.side = side


class Tracker:
    def __init__(self, width, line_frac, band_frac, max_dist_frac, max_missing, lost_memory,
                 revive_frac):
        self.left_edge = width * (line_frac - band_frac)
        self.right_edge = width * (line_frac + band_frac)
        self.line_x = width * line_frac
        self.max_dist = width * max_dist_frac
        self.max_missing = max_missing
        self.lost_memory = lost_memory
        self.revive_dist = width * revive_frac
        self.tracks = []
        self.lost = []  # [track, frames since lost]
        self.next_id = 1

    def active(self):
        return any(t.missing == 0 for t in self.tracks)

    def side_of(self, x):
        if x < self.left_edge:
            return "L"
        if x > self.right_edge:
            return "R"
        return None  # inside the hysteresis band

    def clear(self):
        self.tracks = []
        self.lost = []

    def update(self, blobs):
        """Match blobs to tracks; return list of crossings ('LR' or 'RL')."""
        pairs = []
        for ti, t in enumerate(self.tracks):
            for bi, b in enumerate(blobs):
                d = math.hypot(t.x - b[0], t.y - b[1])
                if d <= self.max_dist:
                    pairs.append((d, ti, bi))
        pairs.sort()
        used_t, used_b = set(), set()
        crossings = []
        for _, ti, bi in pairs:
            if ti in used_t or bi in used_b:
                continue
            used_t.add(ti)
            used_b.add(bi)
            t = self.tracks[ti]
            t.x, t.y, t.box = blobs[bi]
            t.missing = 0
            s = self.side_of(t.x)
            if s is not None:
                if t.side is not None and s != t.side:
                    crossings.append(t.side + s)
                t.side = s
        for ti, t in enumerate(self.tracks):
            if ti not in used_t:
                t.missing += 1
        keep = []
        for t in self.tracks:
            if t.missing <= self.max_missing:
                keep.append(t)
            else:
                self.lost.append([t, 0])
        self.tracks = keep
        for entry in self.lost:
            entry[1] += 1
        self.lost = [e for e in self.lost if e[1] <= self.lost_memory]

        for bi, b in enumerate(blobs):
            if bi in used_b:
                continue
            # Something reappearing where a track was recently lost (e.g. a
            # person who stood still long enough to fade into the background)
            # continues that track, so it remembers which side it came from.
            best, best_d = None, self.revive_dist
            for entry in self.lost:
                lt = entry[0]
                d = math.hypot(lt.x - b[0], lt.y - b[1])
                if d <= best_d:
                    best, best_d = entry, d
            if best is not None:
                self.lost.remove(best)
                t = best[0]
                t.x, t.y, t.box = b
                t.missing = 0
                s = self.side_of(t.x)
                if s is not None:
                    if t.side is not None and s != t.side:
                        crossings.append(t.side + s)
                    t.side = s
                self.tracks.append(t)
            else:
                self.tracks.append(Track(self.next_id, b[0], b[1], b[2], self.side_of(b[0])))
                self.next_id += 1
        return crossings


class ZoneCounter:
    """For a camera right beside the door, where a passing person fills most
    of the image. Two narrow zones sit at the left and right edges. Whichever
    zone sees motion first gives the direction; the crossing counts when the
    other zone then sees motion within max_transit seconds, even if the
    motion left the view in between (a leg can swing right through the image
    in a few frames). Nothing more is counted until both zones are clear.

    A camera near the floor sees legs, and each leg can sweep across the view
    separately. So a second crossing in the same direction within
    same_dir_gap seconds of the last one is treated as the same person.

    A lighting change hits both zones in the same frame, so it is ignored.
    Two people passing together, or one following closely, count as one."""

    def __init__(self, width, zone_frac, on, off, max_transit_s, clear_frames, same_dir_gap=0.0):
        zw = max(1, int(round(width * zone_frac)))
        self.left = (0, zw)
        self.right = (width - zw, width)
        self.on, self.off = on, off
        self.max_transit = max_transit_s
        self.clear_frames = clear_frames
        self.same_dir_gap = same_dir_gap
        self.last_count = {"LR": float("-inf"), "RL": float("-inf")}
        self.merged = 0              # crossings treated as the same person
        self.a = self.b = False      # left / right zone active
        self.fa = self.fb = 0.0      # fraction of each zone showing motion
        self.state = "idle"
        self.t0 = 0.0
        self.clear_n = 0
        self.ignored = 0             # both zones lit at once (lighting, etc.)

    def busy(self):
        return self.state != "idle" or self.a or self.b

    @staticmethod
    def _fill(mask, x0, x1):
        zone = mask[:, x0:x1]
        return cv2.countNonZero(zone) / float(zone.size)

    def update(self, mask, t):
        self.fa = self._fill(mask, *self.left)
        self.fb = self._fill(mask, *self.right)
        # Hysteresis: turn on above zone_on, off only below zone_off.
        self.a = self.fa > (self.off if self.a else self.on)
        self.b = self.fb > (self.off if self.b else self.on)
        a, b = self.a, self.b
        crossings = []
        st = self.state
        if st == "idle":
            if a and not b:
                self.state, self.t0 = "armed_L", t
            elif b and not a:
                self.state, self.t0 = "armed_R", t
            elif a and b:
                self.ignored += 1
                self.state, self.clear_n = "wait_clear", 0
        elif st in ("armed_L", "armed_R"):
            second = b if st == "armed_L" else a
            if second:
                d = "LR" if st == "armed_L" else "RL"
                if t - self.last_count[d] >= self.same_dir_gap:
                    crossings.append(d)
                    self.last_count[d] = t
                else:
                    self.merged += 1  # e.g. the second leg of the same person
                self.state, self.clear_n = "wait_clear", 0
            elif t - self.t0 > self.max_transit:
                # Never reached the other side: backed out, or lingering.
                if a or b:
                    self.state, self.clear_n = "wait_clear", 0
                else:
                    self.state = "idle"
        elif st == "wait_clear":
            if not a and not b:
                self.clear_n += 1
                if self.clear_n >= self.clear_frames:
                    self.state = "idle"
            else:
                self.clear_n = 0
        return crossings


# --------------------------------------------------------------------------
# Uploader (Google Apps Script web app)
# --------------------------------------------------------------------------

class Uploader(threading.Thread):
    def __init__(self, cfg, store, stop):
        super().__init__(daemon=True, name="uploader")
        u = cfg["upload"]
        self.url = u.get("url").strip()
        self.secret = u.get("secret").strip()
        self.device = u.get("device_name").strip()
        self.interval = u.getint("interval_seconds")
        self.batch = u.getint("batch_size")
        self.store = store
        self.stop = stop
        self.last_ok = None
        self.last_error = None
        self.sheet_url = None   # reported back by the sheet's script
        self.sheet_name = None

    def run(self):
        while not self.stop.is_set():
            self.flush()
            self.stop.wait(self.interval)
        self.flush()

    def flush(self):
        while True:
            rows = self.store.unsent(self.batch)
            if not rows:
                return
            payload = {"secret": self.secret, "device": self.device, "events": rows}
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                self.url, data=data, headers={"Content-Type": "application/json"}, method="POST"
            )
            body = ""
            try:
                # Apps Script answers a POST with a redirect to the result;
                # urllib follows it with a GET, which is what Apps Script expects.
                with urllib.request.urlopen(req, timeout=30) as r:
                    body = r.read().decode("utf-8", "replace")
                resp = json.loads(body)
            except Exception as e:  # network down, bad URL, non-JSON reply
                self.last_error = f"{type(e).__name__}: {e} {body[:200]!r}"
                log.warning("Upload failed (%d events queued): %s", self.store.unsent_count(), self.last_error)
                return
            if not resp.get("ok"):
                self.last_error = f"endpoint said: {resp}"
                log.warning("Upload rejected: %s", resp)
                return
            self.store.mark_sent([r["uid"] for r in rows])
            self.last_ok = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.sheet_url = resp.get("sheet_url") or self.sheet_url
            self.sheet_name = resp.get("sheet_name") or self.sheet_name
            self.last_error = None
            log.info("Uploaded %d events (sheet added %s)", len(rows), resp.get("added"))
            if len(rows) < self.batch:
                return


# --------------------------------------------------------------------------
# Live preview web page
# --------------------------------------------------------------------------

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Door counter</title>
<style>
body{font-family:system-ui,sans-serif;margin:16px;background:#111;color:#eee}
img{max-width:100%;image-rendering:pixelated;border:1px solid #444}
pre{background:#222;padding:8px;overflow:auto}
</style></head><body>
<h2>Door counter</h2>
<img src="/stream" alt="live view">
<p id="c"></p>
<p id="r" style="display:none">Counter missed someone?
<button id="bi" onclick="correct('in')">+1 in</button>
<button id="bo" onclick="correct('out')">+1 out</button> <span id="rm"></span></p>
<p id="sh"></p>
<pre id="s">loading...</pre>
<script>
const CAP={
 zones:'Left: the camera view. Only the area outlined in white is analysed; anything '+
       'dimmed is ignored. Inside it, the two edge zones show how much of each has motion '+
       '(yellow = idle, green = active), with the counter state above. '+
       'Right: what the detector sees as moving (white), inside the outlined area.',
 line:'Left: the camera view. Only the area outlined in white is analysed; anything '+
      'dimmed is ignored. Inside it: counting line (red), band edges (yellow), tracked '+
      'objects (green). Right: what the detector sees as moving (white).'};
async function correct(kind){
 const m=document.getElementById('rm'), bs=[document.getElementById('bi'),document.getElementById('bo')];
 bs.forEach(b=>b.disabled=true); m.textContent='...';
 try{const r=await fetch('/correct?kind='+kind,{method:'POST'});const j=await r.json();
  m.textContent=j.ok?('Added 1 '+kind+' (manual). Inside now: '+j.occupancy+'.'):('Failed: '+j.error);}
 catch(e){m.textContent='Failed: '+e;}
 setTimeout(()=>bs.forEach(b=>b.disabled=false),1000);}  // guard against double clicks
async function poll(){try{const r=await fetch('/status');const s=await r.json();
document.getElementById('c').textContent=CAP[s.mode]||'';
const sh=document.getElementById('sh');
if(s.sheet_url){sh.textContent='Writing to sheet: ';const a=document.createElement('a');
 a.href=s.sheet_url;a.target='_blank';a.style.color='#8cf';a.textContent=s.sheet_name||s.sheet_url;sh.appendChild(a);}
else{sh.textContent=s.uploading?'Sheet: shown here after the first successful upload':
 'Not uploading to a sheet (no [upload] url set, or started with --no-upload)';}
document.getElementById('r').style.display=s.manual_corrections_allowed?'':'none';
document.getElementById('s').textContent=JSON.stringify(s,null,2);}catch(e){}
setTimeout(poll,2000);}poll();
</script></body></html>"""


class PreviewServer:
    def __init__(self, host, port, status_fn, correct_fn=None):
        self.cond = threading.Condition()
        self.jpeg = None
        self.seq = 0
        self.viewers = 0
        self.status_fn = status_fn
        self.correct_fn = correct_fn
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                path, _, query = self.path.partition("?")
                params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
                kind = params.get("kind")
                if path == "/correct" and server.correct_fn and kind in ("in", "out"):
                    result = server.correct_fn(kind)
                    code = 200 if result.get("ok") else 503
                    self._send(code, "application/json", json.dumps(result).encode())
                else:
                    self._send(404, "text/plain", b"not found")

            def do_GET(self):
                path = self.path.split("?")[0]
                if path == "/":
                    self._send(200, "text/html; charset=utf-8", PAGE.encode())
                elif path == "/status":
                    self._send(200, "application/json", json.dumps(server.status_fn()).encode())
                elif path == "/snapshot.jpg":
                    jpeg = server.wait_frame(timeout=5)
                    if jpeg is None:
                        self._send(503, "text/plain", b"no frame yet")
                    else:
                        self._send(200, "image/jpeg", jpeg)
                elif path == "/stream":
                    self._stream()
                else:
                    self._send(404, "text/plain", b"not found")

            def _send(self, code, ctype, body):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _stream(self):
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                with server.cond:
                    server.viewers += 1
                last = -1
                try:
                    while True:
                        with server.cond:
                            server.cond.wait_for(lambda: server.seq != last, timeout=5)
                            if server.seq == last or server.jpeg is None:
                                continue
                            last, jpeg = server.seq, server.jpeg
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n")
                        self.wfile.write(f"Content-Length: {len(jpeg)}\r\n\r\n".encode())
                        self.wfile.write(jpeg)
                        self.wfile.write(b"\r\n")
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    with server.cond:
                        server.viewers -= 1

        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True, name="preview").start()
        log.info("Preview at http://%s:%d/", host, port)

    def wanted(self):
        with self.cond:
            return self.viewers > 0

    def publish(self, jpeg):
        with self.cond:
            self.jpeg = jpeg
            self.seq += 1
            self.cond.notify_all()

    def wait_frame(self, timeout):
        with self.cond:
            self.viewers += 1
            start = self.seq
            self.cond.wait_for(lambda: self.seq != start, timeout=timeout)
            self.viewers -= 1
            return self.jpeg

    def shutdown(self):
        self.httpd.shutdown()


# --------------------------------------------------------------------------
# Main application
# --------------------------------------------------------------------------

def open_camera(source, w, h, fps):
    is_file = os.path.isfile(source) and not source.startswith("/dev/")
    if is_file:
        cap = cv2.VideoCapture(source)
    else:
        dev = int(source) if source.isdigit() else source
        cap = cv2.VideoCapture(dev, cv2.CAP_V4L2)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
        cap.set(cv2.CAP_PROP_FPS, fps)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return cap, is_file


class App:
    def __init__(self, cfg, args):
        self.cfg = cfg
        self.args = args
        self.stop = threading.Event()
        c = cfg["counting"]
        self.mode = c.get("mode").strip().lower()
        self.zones = None
        self.direction_in = c.get("direction_in").strip().lower()
        self.max_fg = c.getfloat("max_foreground")
        self.warmup = c.getint("warmup_frames")
        self.keep_days = cfg.getint("storage", "keep_days")
        self.reset_h, self.reset_m = parse_hhmm(cfg.get("schedule", "reset_time"))
        self.heartbeat_s = cfg.getint("schedule", "heartbeat_minutes") * 60

        self.store = Store(args.db or cfg.get("storage", "db_path"))
        self.occupancy = int(self.store.get_state("occupancy", "0"))
        self.bd = self.store.get_state("business_date")

        self.uploader = None
        if not args.no_upload and cfg.get("upload", "url").strip():
            self.uploader = Uploader(cfg, self.store, self.stop)
        elif not args.no_upload:
            log.warning("No upload url set: events are only stored locally")

        self.preview = None
        p = cfg["preview"]
        self.allow_corrections = p.getboolean("allow_corrections")
        self.corrections = []  # queued by the web page, applied in the main loop
        self.corrections_lock = threading.Lock()
        if p.getboolean("enabled") and not args.no_preview:
            self.preview = PreviewServer(p.get("host"), p.getint("port"), self.status,
                                         self.request_correction if self.allow_corrections else None)
            self.preview_w = p.getint("width")
            self.preview_interval = 1.0 / max(0.1, p.getfloat("fps"))
            self.show_mask = p.getboolean("show_mask")
        self.last_preview = 0.0

        self.fps = 0.0
        self.fps_n = 0
        self.fps_since = time.monotonic()
        self.suppressed = 0
        self.today = {"in": 0, "out": 0, "manual_in": 0, "manual_out": 0}
        self.tracker = None

    # ---- status / counting ------------------------------------------------

    def status(self):
        s = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "business_day": self.bd,
            "inside_now": self.occupancy,
            "today_in_total": self.today["in"] + self.today["manual_in"],
            "today_out_total": self.today["out"] + self.today["manual_out"],
            "today_in_camera": self.today["in"],
            "today_out_camera": self.today["out"],
            "today_in_manual": self.today["manual_in"],
            "today_out_manual": self.today["manual_out"],
            "fps": round(self.fps, 1),
            "mode": self.mode,
            "direction_in": self.direction_in,
        }
        if self.mode == "zones":
            z = self.zones
            s.update({
                "zone_state": z.state if z else None,
                "left_zone_motion": round(z.fa, 2) if z else None,
                "right_zone_motion": round(z.fb, 2) if z else None,
                "ignored_both_zones_at_once": z.ignored if z else 0,
                "merged_as_same_person": z.merged if z else 0,
            })
        else:
            s.update({
                "tracks": len(self.tracker.tracks) if self.tracker else 0,
                "frames_suppressed_light_change": self.suppressed,
            })
        s.update({
            "manual_corrections_allowed": self.allow_corrections,
            "unsent_events": self.store.unsent_count(),
            "uploading": self.uploader is not None,
            "sheet_name": self.uploader.sheet_name if self.uploader else None,
            "sheet_url": self.uploader.sheet_url if self.uploader else None,
            "last_upload_ok": self.uploader.last_ok if self.uploader else None,
            "last_upload_error": self.uploader.last_error if self.uploader else None,
        })
        return s

    def count(self, crossing):
        ltr = crossing == "LR"
        is_in = ltr if self.direction_in == "left_to_right" else not ltr
        kind = "in" if is_in else "out"
        self.occupancy += 1 if is_in else -1
        self.today[kind] += 1
        ev = make_event(kind, self.bd, self.occupancy)
        self.store.record(ev, {"occupancy": self.occupancy})
        log.info("%s  inside=%d  today camera in=%d out=%d", kind.upper(), self.occupancy,
                 self.today["in"], self.today["out"])

    def check_day(self, now):
        bd = business_date(now, self.reset_h, self.reset_m)
        if self.bd is None:
            self.bd = bd
            self.store.set_state("business_date", bd)
        elif bd != self.bd:
            residual = self.occupancy
            ev = make_event("reset", self.bd, 0, residual=residual, now=now)
            self.occupancy = 0
            ended = self.bd
            self.bd = bd
            self.store.record(ev, {"occupancy": 0, "business_date": bd})
            log.info("Daily reset for %s: residual=%d", ended, residual)
            self.auto_prune()
        self.today = self.store.day_counts(self.bd)

    def request_correction(self, kind, timeout=5.0):
        """Called from the web server thread when someone presses +1 in or
        +1 out. Applied in the main loop so it can't collide with a camera
        count happening at the same moment."""
        req = {"kind": kind, "done": threading.Event(), "occupancy": None}
        with self.corrections_lock:
            self.corrections.append(req)
        if not req["done"].wait(timeout):
            return {"ok": False, "error": "counter loop not responding"}
        return {"ok": True, "occupancy": req["occupancy"]}

    def apply_corrections(self):
        with self.corrections_lock:
            pending, self.corrections = self.corrections, []
        for req in pending:
            kind = req["kind"]
            self.occupancy += 1 if kind == "in" else -1
            self.today["manual_" + kind] += 1
            self.store.record(make_event("manual_" + kind, self.bd, self.occupancy),
                              {"occupancy": self.occupancy})
            log.info("MANUAL %s from web page  inside=%d", kind.upper(), self.occupancy)
            req["occupancy"] = self.occupancy
            req["done"].set()

    def auto_prune(self):
        if self.keep_days > 0:
            n = self.store.prune(self.keep_days)
            if n:
                log.info("Removed %d uploaded events older than %d days", n, self.keep_days)

    def heartbeat(self, now):
        t = time.monotonic()
        elapsed = t - self.fps_since
        avg = self.fps_n / elapsed if elapsed > 0 else self.fps
        self.fps_n, self.fps_since = 0, t
        self.store.record(make_event("heartbeat", self.bd, self.occupancy, fps=avg, now=now))
        log.info("Heartbeat: occupancy=%d fps=%.1f queued=%d", self.occupancy, avg,
                 self.store.unsent_count())

    # ---- preview rendering ------------------------------------------------

    def render(self, full, mask):
        """Preview: the whole (rotated) camera view with the area outside the
        crop dimmed, the crop outlined in white, and the zones or counting
        line drawn inside the crop. Optional right panel: the motion mask in
        the same layout."""
        v = self.view
        s = self.preview_w / float(v["rw"])                 # full image -> preview
        size = (self.preview_w, int(round(v["rh"] * s)))
        h = size[1]
        X0, Y0 = int(round(v["x0"] * s)), int(round(v["y0"] * s))
        X1, Y1 = int(round((v["x0"] + v["cw"]) * s)), int(round((v["y0"] + v["ch"]) * s))
        k = v["cw"] * s / v["pw"]                            # processing pixel -> preview

        def px(x):
            return int(round(X0 + x * k))

        def py(y):
            return int(round(Y0 + y * k))

        base = cv2.resize(full, size, interpolation=cv2.INTER_NEAREST)
        img = (base * 0.35).astype(np.uint8)                # dim everything...
        img[Y0:Y1, X0:X1] = base[Y0:Y1, X0:X1]              # ...except the crop
        if self.mode == "zones":
            z = self.zones
            for (x0, x1), active, fill in ((z.left, z.a, z.fa), (z.right, z.b, z.fb)):
                Z0, Z1 = px(x0), px(x1) - 1
                color = (0, 255, 0) if active else (0, 220, 220)
                if active:
                    overlay = img.copy()
                    cv2.rectangle(overlay, (Z0, Y0), (Z1, Y1 - 1), color, -1)
                    img = cv2.addWeighted(overlay, 0.3, img, 0.7, 0)
                cv2.rectangle(img, (Z0, Y0), (Z1, Y1 - 1), color, 2 if active else 1)
                cv2.putText(img, f"{int(fill * 100)}%", (Z0 + 3, Y0 + 14),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.4, color, 1)
            cv2.putText(img, z.state, ((X0 + X1) // 2 - 25, Y0 + 14), cv2.FONT_HERSHEY_SIMPLEX,
                        0.4, (255, 255, 255), 1)
        else:
            t = self.tracker
            for x, color, width in ((t.left_edge, (0, 220, 220), 1), (t.right_edge, (0, 220, 220), 1),
                                    (t.line_x, (0, 0, 255), 2)):
                cv2.line(img, (px(x), Y0), (px(x), Y1 - 1), color, width)
            for tr in t.tracks:
                if tr.missing:
                    continue
                x, y, w, hh = tr.box
                cv2.rectangle(img, (px(x), py(y)), (px(x + w), py(y + hh)), (0, 255, 0), 1)
                cv2.putText(img, f"{tr.id}{tr.side or '-'}", (px(x), max(10, py(y) - 2)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.35, (0, 255, 0), 1)
        cv2.rectangle(img, (X0, Y0), (X1 - 1, Y1 - 1), (255, 255, 255), 1)  # crop outline
        arrow = "in ->" if self.direction_in == "left_to_right" else "<- in"
        td = self.today
        def tot(k):
            m = td["manual_" + k]
            return f"{td[k] + m}" + (f"({m}m)" if m else "")
        text = f"{arrow}  today in {tot('in')} out {tot('out')}  inside {self.occupancy}  {self.fps:.0f}fps"
        if self.show_mask:
            m = np.zeros_like(img)
            if X1 > X0 and Y1 > Y0:
                m[Y0:Y1, X0:X1] = cv2.cvtColor(
                    cv2.resize(mask, (X1 - X0, Y1 - Y0), interpolation=cv2.INTER_NEAREST),
                    cv2.COLOR_GRAY2BGR)
            cv2.rectangle(m, (X0, Y0), (X1 - 1, Y1 - 1), (255, 255, 255), 1)
            if self.mode == "line":
                min_side = int(math.sqrt(self.detector.min_area) * k)
                cv2.rectangle(m, (X0 + 2, Y0 + 2), (X0 + 2 + min_side, Y0 + 2 + min_side),
                              (0, 0, 255), 1)
                cv2.putText(m, "min size", (X0 + 4 + min_side, Y0 + 12), cv2.FONT_HERSHEY_SIMPLEX,
                            0.35, (0, 0, 255), 1)
            img = np.hstack([img, m])
        # Status line in its own strip below the image, so it doesn't hide
        # the bottom of the view.
        bar = np.zeros((16, img.shape[1], 3), np.uint8)
        cv2.putText(bar, text, (3, 12), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
        img = np.vstack([img, bar])
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 70])
        if ok:
            self.preview.publish(buf.tobytes())

    # ---- main loop --------------------------------------------------------

    def watch_address(self, logged, interval=10):
        """Log a "network <address>" row when the device gets an address it
        hasn't logged yet: after booting without network, or if the router
        gives it a new one. Losing the connection briefly logs nothing."""
        while not self.stop.wait(interval):
            ip = local_ip()
            if ip and ip != logged:
                self.store.record(make_event("network " + ip, self.bd, self.occupancy))
                log.info("Network address: %s", ip)
                logged = ip

    def run(self):
        cam = self.cfg["camera"]
        source = self.args.source or cam.get("source")
        cap, is_file = open_camera(source, cam.getint("width"), cam.getint("height"), cam.getint("fps"))
        if not cap.isOpened():
            log.error("Could not open camera/source %r", source)
            return 1

        ok, frame = cap.read()
        if not ok:
            log.error("Camera opened but returned no frame")
            return 1
        fh, fw = frame.shape[:2]
        file_fps = (cap.get(cv2.CAP_PROP_FPS) or 30.0) if is_file else None
        rotate = cam.getint("rotate")
        rot_code = ROTATIONS[rotate]
        # Size of the image after rotation. Everything downstream (crop,
        # zones, line, direction, preview) works on the rotated image.
        rw, rh = (fh, fw) if rotate in (90, 270) else (fw, fh)
        # Only the cropped part of the image is analysed.
        cx0 = int(round(cam.getfloat("crop_left") * rw))
        cx1 = int(round(cam.getfloat("crop_right") * rw))
        cy0 = int(round(cam.getfloat("crop_top") * rh))
        cy1 = int(round(cam.getfloat("crop_bottom") * rh))
        cw, ch = cx1 - cx0, cy1 - cy0
        pw = cam.getint("process_width")
        ph = max(1, int(round(ch * pw / float(cw))))
        self.view = {"rw": rw, "rh": rh, "x0": cx0, "y0": cy0, "cw": cw, "ch": ch, "pw": pw, "ph": ph}
        log.info("Camera %dx%d, rotated %d, crop x %d-%d y %d-%d, processing at %dx%d, "
                 "mode = %s, in = %s", fw, fh, rotate, cx0, cx1, cy0, cy1, pw, ph,
                 self.mode, self.direction_in)

        c = self.cfg["counting"]
        self.detector = Detector(self.cfg, pw, ph)
        self.tracker = Tracker(pw, c.getfloat("line_position"), c.getfloat("band"),
                               c.getfloat("max_match_distance"), c.getint("max_missing_frames"),
                               c.getint("lost_memory_frames"), c.getfloat("revive_distance"))
        if self.mode == "zones":
            self.zones = ZoneCounter(pw, c.getfloat("zone_width"), c.getfloat("zone_on"),
                                     c.getfloat("zone_off"), c.getfloat("max_transit_seconds"),
                                     c.getint("clear_frames"),
                                     c.getfloat("same_direction_gap_seconds"))

        now = datetime.now()
        self.check_day(now)
        self.auto_prune()
        # The start event carries the device's address (e.g. "start
        # 192.168.1.23") so the sheet shows where to find the preview page.
        # At boot Wi-Fi is often not up yet: then it logs "start (no
        # network)", and a "network <address>" row follows once it connects.
        ip = local_ip()
        start = "start " + ip if ip else "start (no network)"
        self.store.record(make_event(start, self.bd, self.occupancy, now=now))
        if not is_file:
            threading.Thread(target=self.watch_address, args=(ip,), daemon=True,
                             name="address").start()
        if self.uploader:
            self.uploader.start()

        frames = 0
        failures = 0
        last_tick = time.monotonic()
        last_heartbeat = time.monotonic()
        win_frames = 0
        self.fps_n = 0
        self.fps_since = time.monotonic()
        result = 0

        while not self.stop.is_set():
            if frames > 0:
                ok, frame = cap.read()
            if not ok:
                if is_file:
                    break
                failures += 1
                if failures > 50:
                    log.error("Camera stopped delivering frames; exiting so systemd restarts us")
                    result = 1
                    break
                time.sleep(0.1)
                continue
            failures = 0
            frames += 1

            full = cv2.rotate(frame, rot_code) if rot_code is not None else frame
            small = cv2.resize(full[cy0:cy1, cx0:cx1], (pw, ph), interpolation=cv2.INTER_AREA)
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            if self.mode == "zones":
                _, mask, _ = self.detector.detect(gray, presence=self.zones.busy())
                if frames > self.warmup:
                    # For a test video, use the video's own clock so timing
                    # settings behave as they would live.
                    t_zone = frames / file_fps if is_file else time.monotonic()
                    for crossing in self.zones.update(mask, t_zone):
                        self.count(crossing)
            else:
                blobs, mask, fg = self.detector.detect(gray, presence=self.tracker.active())

            if self.mode == "line" and frames > self.warmup:
                if fg > self.max_fg:
                    # Sudden lighting change: most of the frame "moved".
                    # Don't count; let the background model catch up.
                    self.tracker.clear()
                    self.suppressed += 1
                else:
                    for crossing in self.tracker.update(blobs):
                        self.count(crossing)

            if self.corrections:
                self.apply_corrections()

            t = time.monotonic()
            # Frames per second = frames processed / time elapsed, measured
            # over each second (live value) and each heartbeat interval.
            win_frames += 1
            self.fps_n += 1

            if t - last_tick >= 1.0:
                self.fps = win_frames / (t - last_tick)
                win_frames = 0
                last_tick = t
                now = datetime.now()
                self.check_day(now)
                if t - last_heartbeat >= self.heartbeat_s:
                    last_heartbeat = t
                    self.heartbeat(now)

            if self.preview and self.preview.wanted() and t - self.last_preview >= self.preview_interval:
                self.last_preview = t
                self.render(full, mask)

        cap.release()
        self.stop.set()
        if self.uploader:
            self.uploader.join(timeout=40)
        if self.preview:
            self.preview.shutdown()
        log.info("Stopped. inside=%d today camera in=%d out=%d, manual in=%d out=%d, frames=%d",
                 self.occupancy, self.today["in"], self.today["out"],
                 self.today["manual_in"], self.today["manual_out"], frames)
        return result


def main():
    ap = argparse.ArgumentParser(description="Door counter")
    ap.add_argument("--config", default="/etc/door-counter/counter.ini")
    ap.add_argument("--source", help="override camera source (device index, /dev/videoN, or a video file)")
    ap.add_argument("--db", help="override storage.db_path")
    ap.add_argument("--no-upload", action="store_true", help="store events locally only")
    ap.add_argument("--no-preview", action="store_true", help="disable the preview web page")
    ap.add_argument("--stats", action="store_true",
                    help="show database size and event counts, then exit")
    ap.add_argument("--clean", type=int, metavar="DAYS",
                    help="delete uploaded events older than DAYS days (0 = all uploaded events), "
                         "shrink the file, then exit")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config)

    if args.stats or args.clean is not None:
        db_path = args.db or cfg.get("storage", "db_path")
        if os.geteuid() == 0:
            # Running as root could leave root-owned database files the
            # service can no longer write to.
            raise SystemExit("Run this as the service user instead of root:\n"
                             f"  sudo -u doorcounter python3 {os.path.abspath(sys.argv[0])} "
                             + " ".join(sys.argv[1:]))
        if not os.path.exists(db_path):
            raise SystemExit(f"No database at {db_path}")
        store = Store(db_path)
        print("Before:" if args.clean is not None else "Database:", db_path, store.stats())
        if args.clean is not None:
            if args.clean < 0:
                raise SystemExit("DAYS must be 0 or more")
            n = store.prune(args.clean)
            store.vacuum()
            print(f"Deleted {n} uploaded events older than {args.clean} days (unsent events kept)")
            print("After: ", store.stats())
        return

    app = App(cfg, args)

    def handle_signal(signum, _frame):
        log.info("Signal %d received, stopping", signum)
        app.stop.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    sys.exit(app.run())


if __name__ == "__main__":
    main()
