#!/usr/bin/env python3
"""
Run the counter on the synthetic videos and compare against known answers.

    python3 tests/make_videos.py   # once
    python3 tests/test_counts.py

Each case loads counter.example.ini, applies a few setting overrides, runs
the counter on a video with a throwaway database, and checks the day's
camera in/out totals.
"""
import argparse
import logging
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import counter as C  # noqa: E402

VIDEOS = os.path.join(HERE, "videos")

# (description, video, {section.key: value}, expected (in, out))
CASES = [
    ("line mode, in = left to right", "line.avi",
     {"counting.mode": "line"}, (4, 5)),
    ("line mode, in = right to left", "line.avi",
     {"counting.mode": "line", "counting.direction_in": "right_to_left"}, (5, 4)),
    # zones.avi: fast LR, slow RL, step in/back out (none), flash (none),
    # auto-exposure LR, pause RL, two back to back (counted as one LR).
    ("zones mode, body fills frame", "zones.avi", {}, (3, 2)),
    ("zones mode, camera on its side, rotate 90", "zones_ccw.avi",
     {"camera.rotate": "90"}, (3, 2)),
    ("zones mode, camera on its side, rotate 270", "zones_cw.avi",
     {"camera.rotate": "270"}, (3, 2)),
    ("zones mode, camera on its side, rotate not set", "zones_ccw.avi",
     {"camera.rotate": "0"}, (0, 0)),
    # legs.avi: 4 people LR, 4 RL, plus one who steps in and back out.
    # Expected includes two known limitations: the person who stops
    # mid-stride for 1.5 s counts twice (one extra in), and the step-in/
    # back-out person counts as one in and one out.
    ("zones mode, legs only, same_direction_gap 1.0", "legs.avi",
     {"counting.same_direction_gap_seconds": "1.0"}, (6, 5)),
]


def run_case(video, overrides):
    cfg = C.load_config(os.path.join(ROOT, "counter.example.ini"))
    for key, value in overrides.items():
        section, option = key.split(".", 1)
        cfg.set(section, option, value)
    with tempfile.TemporaryDirectory() as tmp:
        args = argparse.Namespace(source=os.path.join(VIDEOS, video), db=os.path.join(tmp, "t.db"),
                                  no_upload=True, no_preview=True, verbose=False)
        app = C.App(cfg, args)
        app.run()
        return app.today["in"], app.today["out"]


def main():
    logging.basicConfig(level=logging.WARNING)
    missing = [c[1] for c in CASES if not os.path.exists(os.path.join(VIDEOS, c[1]))]
    if missing:
        print("Missing videos; run: python3 tests/make_videos.py")
        return 2
    failed = 0
    for desc, video, overrides, expected in CASES:
        got = run_case(video, overrides)
        ok = got == expected
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {desc}: in/out {got[0]}/{got[1]}"
              + ("" if ok else f" (expected {expected[0]}/{expected[1]})"))
    # Without the same-direction gap, each leg counts separately.
    got = run_case("legs.avi", {"counting.same_direction_gap_seconds": "0"})
    ok = got[0] + got[1] > 13
    failed += not ok
    print(f"{'PASS' if ok else 'FAIL'}  zones mode, legs only, gap off double-counts: "
          f"in/out {got[0]}/{got[1]}")
    print("all passed" if not failed else f"{failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
