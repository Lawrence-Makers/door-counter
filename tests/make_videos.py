#!/usr/bin/env python3
"""
Generate synthetic test videos with known answers.

    python3 tests/make_videos.py            # writes into tests/videos/

Videos (320x240, 30 fps, MJPG .avi):

  line.avi       Small dark figures walking across a textured background, for
                 line mode: plain passes each way, two people close together,
                 a short and a long pause on the line, someone reaching the
                 line and turning back, a person split into pieces, and a
                 sudden lighting change.
  zones.avi      A body filling ~70% of the frame, as with a camera right
                 beside the door: fast and slow passes, stepping in and
                 backing out, a lighting flash, an auto-exposure jump, a
                 pause in the doorway, two people back to back.
  zones_ccw.avi  zones.avi rotated 90 degrees counter-clockwise (camera on
  zones_cw.avi   its side) and clockwise, for the rotate setting.
  legs.avi       Camera ~1 ft off the floor: only shins, each leg sweeping
                 across the view separately. Assumes the view is ~0.46 m
                 wide (~700 px/m) and a stride longer than the view.

The expected counts are in test_counts.py.
"""
import os
import sys

import cv2
import numpy as np

W, H = 320, 240
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "videos")


def writer(name, size=(W, H)):
    os.makedirs(OUT, exist_ok=True)
    return cv2.VideoWriter(os.path.join(OUT, name), cv2.VideoWriter_fourcc(*"MJPG"), 30, size)


# --------------------------------------------------------------------------

def make_line():
    rng = np.random.default_rng(1)
    bg = cv2.GaussianBlur(rng.integers(90, 170, (H, W, 3)).astype(np.uint8), (7, 7), 0)
    out = writer("line.avi")

    def person(img, x, split=False):
        x = int(x)
        y0, y1 = 80, 195
        col = (40, 35, 30)
        cv2.rectangle(img, (x, y0), (x + 34, y0 + 22), col, -1)  # head
        if split:
            cv2.rectangle(img, (x, y0 + 40), (x + 34, y1), col, -1)  # gap below the head
        else:
            cv2.rectangle(img, (x, y0 + 22), (x + 34, y1), col, -1)

    def emit(people, bright=0):
        f = bg.astype(np.int16) + bright
        f = np.clip(f + rng.integers(-4, 5, f.shape), 0, 255).astype(np.uint8)
        for p in people:
            person(f, *p)
        out.write(f)

    def walk(a, b, step=5, split=False, bright=0):
        n = int(abs(b - a) / step)
        for i in range(n + 1):
            emit([(a + (b - a) * i / n, split)], bright)

    def idle(n, bright=0):
        for _ in range(n):
            emit([], bright)

    idle(90)
    walk(-40, 360)                                   # LR
    idle(30)
    walk(360, -40)                                   # RL
    idle(30)
    walk(-40, 150)                                   # short pause on the line, then on: LR
    for i in range(60):
        emit([(150 + 3 * np.sin(i / 3), False)])
    walk(150, 360)
    idle(30)
    walk(-40, 140)                                   # reaches the line, turns back: nothing
    walk(140, -40)
    idle(30)
    n = int(400 / 5) + 12                            # two people close together: RL, RL
    for i in range(n + 1):
        xs = [360 - 5 * i, 360 - 5 * (i - 12)]
        emit([(x, False) for x in xs if -40 <= x <= 360])
    idle(30)
    idle(60, bright=70)                              # sudden lighting change: nothing
    walk(-40, 360, bright=70)                        # LR
    idle(30, bright=70)
    walk(360, -40, split=True, bright=70)            # person split in two: one RL
    idle(30, bright=70)
    walk(-40, 150, bright=70)                        # long pause (fades into background): LR
    for i in range(500):
        emit([(150 + 2 * np.sin(i / 4), False)], bright=70)
    walk(150, 360, bright=70)
    idle(60, bright=70)
    walk(-40, 150, bright=70)                        # long pause, then back out: nothing
    for i in range(500):
        emit([(150 + 2 * np.sin(i / 4), False)], bright=70)
    walk(150, -40, bright=70)
    idle(30, bright=70)
    walk(360, -40, bright=70)                        # passes the old pause spot: RL
    idle(60, bright=70)
    out.release()


# --------------------------------------------------------------------------

def make_zones():
    PW = 230  # body width in pixels
    rng = np.random.default_rng(2)
    bg = cv2.GaussianBlur(rng.integers(90, 170, (H, W, 3)).astype(np.uint8), (7, 7), 0)
    tex = cv2.GaussianBlur(rng.integers(20, 70, (H, PW, 3)).astype(np.uint8), (5, 5), 0)
    out = writer("zones.avi")
    frames = []

    def emit(xs=(), bright=0, autoexp=False):
        f = bg.astype(np.int16) + bright
        if autoexp and xs:
            cover = sum(max(0, min(W, x + PW) - max(0, x)) for x in xs) / W
            if cover > 0.4:
                f = f + 35  # camera brightens the background when a dark body fills the view
        f = np.clip(f + rng.integers(-4, 5, f.shape), 0, 255).astype(np.uint8)
        for x in xs:
            x = int(x)
            a, b = max(0, x), min(W, x + PW)
            if b > a:
                f[:, a:b] = tex[:, a - x:b - x]
        out.write(f)
        frames.append(f)

    def walk(a, b, step, **k):
        n = max(1, int(abs(b - a) / step))
        for i in range(n + 1):
            emit([a + (b - a) * i / n], **k)

    def idle(n, **k):
        for _ in range(n):
            emit([], **k)

    idle(90)
    walk(-PW, W, 18)                                 # fast: LR
    idle(20)
    walk(W, -PW, 6)                                  # slow: RL
    idle(20)
    walk(-PW, -PW + 120, 8)                          # steps in, backs out: nothing
    walk(-PW + 120, -PW, 8)
    idle(20)
    idle(4, bright=60)                               # lighting flash: nothing
    idle(20)
    walk(-PW, W, 10, autoexp=True)                   # auto-exposure jump: LR
    idle(20)
    walk(W, 0, 8)                                    # pauses in the doorway: RL
    for i in range(40):
        emit([0 + 2 * np.sin(i / 3)])
    walk(0, -PW, 8)
    idle(20)
    for i in range(0, int((W + PW * 2 + 60) / 10) + 1):  # two people back to back: counted as one LR
        xs = [-PW + 10 * i, -PW + 10 * i - (PW + 60)]
        emit([x for x in xs if -PW < x < W])
    idle(30)
    out.release()

    for name, code in (("zones_ccw.avi", cv2.ROTATE_90_COUNTERCLOCKWISE),
                       ("zones_cw.avi", cv2.ROTATE_90_CLOCKWISE)):
        w = writer(name, (H, W))
        for f in frames:
            w.write(cv2.rotate(f, code))
        w.release()


def make_zones_background():
    """Body passes in the lower part of the frame; something else (people
    beyond the door) crosses the top 80 px left to right, often. With the top
    cropped off only the real passes should count."""
    PW, TOP = 230, 90
    rng = np.random.default_rng(4)
    bg = cv2.GaussianBlur(rng.integers(90, 170, (H, W, 3)).astype(np.uint8), (7, 7), 0)
    tex = cv2.GaussianBlur(rng.integers(20, 70, (H - TOP, PW, 3)).astype(np.uint8), (5, 5), 0)
    out = writer("zones_background.avi")
    state = {"far": None}

    def emit(x=None):
        f = np.clip(bg.astype(np.int16) + rng.integers(-4, 5, bg.shape), 0, 255).astype(np.uint8)
        # Background walker: a 70x80 block crossing the top, left to right,
        # starting every 70 frames.
        state["far"] = -70 if state["far"] is None or state["far"] > W else state["far"] + 8
        fx = int(state["far"])
        a, b = max(0, fx), min(W, fx + 70)
        if b > a:
            f[0:80, a:b] = (30, 30, 30)
        if x is not None:
            x = int(x)
            a, b = max(0, x), min(W, x + PW)
            if b > a:
                f[TOP:, a:b] = tex[:, a - x:b - x]
        out.write(f)

    def walk(a, b, step):
        n = max(1, int(abs(b - a) / step))
        for i in range(n + 1):
            emit(a + (b - a) * i / n)

    for _ in range(90):
        emit()
    for _ in range(3):              # three real passes each way, spaced out
        walk(-PW, W, 12)
        for _ in range(40):
            emit()
        walk(W, -PW, 12)
        for _ in range(40):
            emit()
    out.release()


# --------------------------------------------------------------------------

def make_legs():
    LEG = 70  # shin ~0.1 m
    rng = np.random.default_rng(3)
    bg = cv2.GaussianBlur(rng.integers(100, 180, (H, W, 3)).astype(np.uint8), (9, 9), 0)
    tex = [cv2.GaussianBlur(rng.integers(lo, lo + 40, (H, LEG, 3)).astype(np.uint8), (5, 5), 0)
           for lo in (20, 35)]
    out = writer("legs.avi")

    def emit(feet):
        f = np.clip(bg.astype(np.int16) + rng.integers(-4, 5, bg.shape), 0, 255).astype(np.uint8)
        for i, x in enumerate(feet):
            x = int(x)
            a, b = max(0, x), min(W, x + LEG)
            if b > a:
                f[:, a:b] = tex[i % 2][:, a - x:b - x]
        out.write(f)

    def idle(n):
        for _ in range(n):
            emit([])

    def walk(direction, step_m=0.65, period_s=0.5, stop_after=None, stop_s=0, turn_back=False):
        """Alternating gait: the swing foot travels two step lengths while
        the other foot stays planted."""
        S = step_m * 700
        per = int(period_s * 30)
        sw = max(2, int(per * 0.8))
        ds = per - sw
        d = 1 if direction == "LR" else -1
        start = -LEG - 2 * S if d == 1 else W + 2 * S
        back, front = start, start + d * S
        steps = 0
        while True:
            for k in range(1, sw + 1):
                emit([back + d * 2 * S * k / sw, front])
            back, front = front, back + d * 2 * S
            for _ in range(ds):
                emit([back, front])
            steps += 1
            if turn_back and steps == 2:
                d = -d
                back, front = front, back
                turn_back = False
            if stop_after and steps == stop_after:
                for _ in range(int(stop_s * 30)):
                    emit([back, front])
            lo, hi = min(back, front), max(back, front)
            if (d == 1 and lo > W + 5) or (d == -1 and hi < -LEG - 5):
                break

    idle(90)
    walk("LR"); idle(60)
    walk("RL"); idle(60)
    walk("LR", period_s=0.8, step_m=0.5); idle(60)   # slow, shorter steps
    walk("RL", period_s=0.4, step_m=0.8); idle(60)   # fast, longer steps
    walk("LR", stop_after=2, stop_s=1.5); idle(60)   # stops mid-stride for 1.5 s
    walk("LR", turn_back=True); idle(60)             # two steps in, then back out
    walk("RL"); idle(40); walk("RL"); idle(60)       # two people ~1.3 s apart
    walk("LR"); idle(60)
    out.release()


if __name__ == "__main__":
    for fn in (make_line, make_zones, make_zones_background, make_legs):
        fn()
        print("made", fn.__name__[5:])
    print("videos in", OUT)
    sys.exit(0)
