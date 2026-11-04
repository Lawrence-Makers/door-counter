#!/usr/bin/env python3
"""
Tests for the parts of the counter that don't involve video: day boundaries
and the daily reset, state surviving restarts, manual corrections, database
cleanup, and uploading (against a fake Apps Script endpoint that fails and
then recovers).

    python3 tests/test_logic.py
"""
import argparse
import json
import logging
import os
import sys
import tempfile
import threading
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import counter as C  # noqa: E402

SECRET = "test-secret"


class FakeAppsScript:
    """Mimics an Apps Script web app: answers a POST with a 302 redirect to
    the result, which the client fetches with GET. Can be told to fail."""

    def __init__(self):
        self.received = []
        self.fail = False
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if fake.fail:
                    self.send_response(500)
                    self.end_headers()
                    return
                if body.get("secret") != SECRET:
                    fake.last = {"ok": False, "error": "unauthorized"}
                else:
                    seen = {e["uid"] for e in fake.received}
                    new = [e for e in body["events"] if e["uid"] not in seen]
                    fake.received.extend(new)
                    fake.last = {"ok": True, "added": len(new),
                                 "sheet_url": "https://docs.google.com/spreadsheets/d/TEST/edit",
                                 "sheet_name": "Test counts"}
                self.send_response(302)
                self.send_header("Location", "/echo")
                self.end_headers()

            def do_GET(self):
                b = json.dumps(fake.last).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/exec"


def make_app(db, url=""):
    cfg = C.load_config(os.path.join(ROOT, "counter.example.ini"))
    cfg.set("upload", "url", url)
    cfg.set("upload", "secret", SECRET)
    cfg.set("upload", "batch_size", "3")
    args = argparse.Namespace(db=db, no_upload=not url, no_preview=True, source=None)
    return cfg, C.App(cfg, args)


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("PASS ", msg)


def test_business_date():
    check(C.business_date(datetime(2026, 11, 1, 3, 59), 4, 0) == "2026-10-31",
          "03:59 belongs to the previous day")
    check(C.business_date(datetime(2026, 11, 1, 4, 0), 4, 0) == "2026-11-01",
          "04:00 starts a new day")


def test_local_ip():
    ip = C.local_ip()
    check(ip is None or (ip.count(".") == 3 and not ip.startswith("127.")),
          f"local address is a LAN address or None (got {ip})")


def test_counting_and_reset(tmp):
    db = os.path.join(tmp, "a.db")
    _, app = make_app(db)
    app.check_day(datetime(2026, 11, 1, 10, 0))
    for c in ("LR", "LR", "RL", "LR"):
        app.count(c)
    check(app.occupancy == 2 and app.today["in"] == 3 and app.today["out"] == 1,
          "counts in/out and occupancy")
    app.check_day(datetime(2026, 11, 2, 3, 59))
    check(app.bd == "2026-11-01", "no reset before 4am")
    app.count("RL")
    app.check_day(datetime(2026, 11, 2, 4, 0))
    check(app.bd == "2026-11-02" and app.occupancy == 0 and app.today["in"] == 0,
          "4am reset zeroes occupancy and starts a new day")
    with app.store.lock:
        row = app.store.db.execute(
            "SELECT business_date, residual FROM events WHERE event = 'reset'").fetchone()
    check(tuple(row) == ("2026-11-01", 1), "reset logs residual against the day that ended")

    _, app2 = make_app(db)
    check(app2.bd == "2026-11-02" and app2.occupancy == 0, "state survives a restart")
    app2.count("LR")
    app2.check_day(datetime(2026, 11, 4, 9, 0))
    with app2.store.lock:
        n = app2.store.db.execute("SELECT COUNT(*) FROM events WHERE event = 'reset'").fetchone()[0]
    check(app2.bd == "2026-11-04" and n == 2, "a reset missed while off happens at next start")


def test_corrections(tmp):
    _, app = make_app(os.path.join(tmp, "b.db"))
    app.check_day(datetime(2026, 11, 1, 10, 0))
    app.count("LR")
    results = []
    threads = [threading.Thread(target=lambda k=k: results.append(app.request_correction(k)))
               for k in ("in", "in", "out")]
    for t in threads:
        t.start()
    while len(app.corrections) < 3:
        pass
    app.apply_corrections()  # normally done by the main loop
    for t in threads:
        t.join()
    check(all(r["ok"] for r in results), "web corrections are applied")
    check(app.occupancy == 2 and app.today["manual_in"] == 2 and app.today["manual_out"] == 1
          and app.today["in"] == 1, "manual counts kept separate from camera counts")
    check(app.store.day_counts(app.bd) == app.today, "day totals match the database")


def test_cleanup(tmp):
    s = C.Store(os.path.join(tmp, "c.db"))

    def ev(ts, sent):
        e = dict(uid=uuid.uuid4().hex, ts=ts, business_date=ts[:10], event="in",
                 occupancy=1, residual=None, fps=None)
        s.record(e)
        if sent:
            s.mark_sent([e["uid"]])

    for _ in range(500):
        ev("2025-01-15 10:00:00", True)
    for _ in range(5):
        ev("2025-01-15 11:00:00", False)
    for _ in range(10):
        ev(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), True)
    before = s.stats()["size_kb"]
    n = s.prune(365)
    s.vacuum()
    st = s.stats()
    check(n == 500 and st["events"] == 15 and st["unsent"] == 5,
          "prune removes old uploaded events, never unsent ones")
    check(st["size_kb"] < before, "vacuum shrinks the file")


def test_upload(tmp):
    fake = FakeAppsScript()
    cfg, app = make_app(os.path.join(tmp, "d.db"), url=fake.url)
    app.check_day(datetime(2026, 11, 1, 10, 0))
    for c in ("LR", "RL", "LR", "LR", "RL"):
        app.count(c)
    up = C.Uploader(cfg, app.store, threading.Event())
    fake.fail = True
    up.flush()
    check(app.store.unsent_count() == 5 and up.last_error, "events stay queued when upload fails")
    fake.fail = False
    up.flush()
    check(app.store.unsent_count() == 0 and len(fake.received) == 5,
          "queued events upload in batches after recovery")
    check(up.sheet_name == "Test counts" and up.sheet_url.endswith("/TEST/edit"),
          "sheet name and link reported back by the script are kept")
    cfg.set("upload", "secret", "wrong")
    app.count("LR")
    C.Uploader(cfg, app.store, threading.Event()).flush()
    check(app.store.unsent_count() == 1, "rejected upload (wrong secret) stays queued")


def main():
    logging.basicConfig(level=logging.ERROR)
    with tempfile.TemporaryDirectory() as tmp:
        test_business_date()
        test_local_ip()
        test_counting_and_reset(tmp)
        test_corrections(tmp)
        test_cleanup(tmp)
        test_upload(tmp)
    print("all passed")


if __name__ == "__main__":
    main()
