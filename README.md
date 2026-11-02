# Door counter

Counts people walking in and out of a doorway with a cheap USB camera and a
small Linux board, and logs every crossing to a Google Sheet: daily totals,
how many are inside now, and how accurate the camera has been.

- Runs on a small RISC-V board (Mango Pi MQ-Pro) with a PS3 Eye camera; any
  Linux machine with Python, OpenCV and a webcam should work.
- Images stay in memory; nothing but the event log is written to disk.
- Works through unreliable internet: events queue on the device and upload
  when the connection is back.
- Live low-res preview page for positioning and tuning, with "+1 in" / "+1
  out" buttons for staff to add crossings the camera missed. Corrections are
  logged separately, so the sheet shows how much the camera caught on its own.
- Several doors can report to one sheet, or each to its own.

## How it counts

Background subtraction finds what's moving; two modes turn that into
crossings:

- **zones** (default): the camera is right beside the door and a passing
  person fills most of the image. There's a narrow zone at each edge of the
  image; whichever sees motion first gives the direction. Handles a camera
  mounted near the floor that sees only legs (see `same_direction_gap_seconds`).
- **line**: the camera is further back and people appear smaller than half
  the image. Each person is tracked across a counting line.

Each day at 4am the "inside now" count is reset to 0. The value just before
is logged as the day's residual: 0 means ins and outs balanced.

### Limitations

- Two people walking through side by side, or one directly behind another,
  count as one (zones mode).
- Low mount (legs only): someone who stops mid-stride in the doorway for
  longer than `same_direction_gap_seconds` counts twice.
- The residual only shows net error: a missed entry and a missed exit cancel
  out. Camera share (from the +1 buttons) only covers misses someone noticed.
- It counts entries, not visitors: staff and people going in and out twice
  are counted each time.

## Files

| File | Goes to (on the device) | What it is |
|---|---|---|
| `counter.py` | `/opt/door-counter/counter.py` | The counter |
| `counter.example.ini` | `/etc/door-counter/counter.ini` | Settings; copy to `counter.ini` and edit |
| `systemd/door-counter.service` | `/etc/systemd/system/` | Starts it on boot, restarts it if it crashes |
| `apps-script/Code.gs` | The Google Sheet's Apps Script editor | Receives events and writes the sheet |
| `tests/` | stays on your computer | Synthetic test videos and tests |

`counter.ini` holds your sheet's URL and secret and is excluded by
`.gitignore`; don't commit it.

## 1. Hardware and OS

Tested with a **Mango Pi MQ-Pro** (Allwinner D1, 1 GB) and a **Sony PS3
Eye**. The PS3 Eye uses the `gspca_ov534` kernel driver; most other USB
webcams use `uvcvideo`.

There is no official Ubuntu image for the MQ-Pro. What worked:

1. Flash Canonical's **Ubuntu 24.04.5 server image for the Sipeed LicheeRV
   Dock**, which uses the same chip:
   `ubuntu-24.04.5-preinstalled-server-riscv64+licheerv.img.xz` from
   [ubuntu.com/download/risc-v](https://ubuntu.com/download/risc-v).
   First boot is slow (several minutes). Log in as `ubuntu` / `ubuntu` over
   SSH (via a USB Ethernet adapter) and set a new password.
2. Its kernel includes the camera drivers and OpenCV is packaged. Check:
   ```
   grep -E "USB_GSPCA_OV534=|USB_VIDEO_CLASS=" /boot/config-$(uname -r)
   ```
3. Wi-Fi (RTL8723DS) was detected with the LicheeRV device tree. Configure
   it with netplan, in its own file so first-boot setup doesn't overwrite it:
   ```
   sudo nano /etc/netplan/60-wifi.yaml
   ```
   ```yaml
   network:
     version: 2
     wifis:
       wlan0:
         optional: true
         dhcp4: true
         access-points:
           "YOUR_SSID":
             password: "YOUR_PASSWORD"
   ```
   ```
   sudo chmod 600 /etc/netplan/60-wifi.yaml
   sudo netplan try
   ```
4. Optionally switch to the MQ-Pro's own device tree, as described in
   [easytarget/MQ-Pro-IO](https://github.com/easytarget/MQ-Pro-IO).

Older images may lack USB camera support entirely: an Armbian 22.08 image we
tried had `CONFIG_MEDIA_USB_SUPPORT` off. An alternative is building a
current Armbian image (`./compile.sh build BOARD=mangopi-mq BRANCH=edge
KERNEL_CONFIGURE=yes ...`) and enabling `MEDIA_USB_SUPPORT`,
`USB_GSPCA_OV534`, `USB_VIDEO_CLASS` and `RTL8723DS` in the kernel menu; that
build completed, but the image hasn't been run with the counter yet.

## 2. Prepare the device

```
sudo apt install python3-opencv
sudo usermod -aG video $USER        # log out and back in afterwards
sudo timedatectl set-timezone America/Chicago   # your timezone
timedatectl
```

`timedatectl` should show `System clock synchronized: yes`. Timestamps and
the daily reset depend on the clock.

Copy the files over from your computer:

```
git clone https://github.com/Lawrence-Makers/door-counter
cd door-counter
cp counter.example.ini counter.ini
scp counter.py counter.ini systemd/door-counter.service user@<device-ip>:
```

## 3. Test run and positioning

With the camera plugged in, on the device:

```
python3 counter.py --config counter.ini --db ~/test.db --no-upload
```

Open `http://<device-ip>:8080/`. The right half of the preview is what the
detector sees as moving (white); the status box below shows counts and frames
per second.

`counter.ini` starts with a short list of the settings you'll usually
change, and each setting has a comment saying what it does and which way to
adjust it. The main ones:

- `[camera] rotate`: if the camera is mounted on its side, set it so people
  move left/right in the preview.
- `direction_in`: `left_to_right` or `right_to_left`, as seen in the preview.
- `same_direction_gap_seconds`: 1.0 if the camera sees only legs (mounted
  near the floor), 0.3 if it sees the body (waist height).

**Zones mode preview:** the two edge zones show the percentage of each with
motion and turn green when active; the state at the top reads `idle`,
`armed_L`/`armed_R` (one side triggered) or `wait_clear` (counted, waiting
for the view to clear).

**Line mode preview:** counting line (red), band edges (yellow), tracked
objects (green boxes).

Walk through 20 or so times in a mix of ways (slow, fast, pausing, turning
back, two people together) and compare with the counts. The status page
helps:

- `merged_as_same_person` rising while people still count twice: raise
  `same_direction_gap_seconds`.
- `ignored_both_zones_at_once` rising as people pass: the camera's
  auto-exposure is probably brightening the whole image; check
  `v4l2-ctl -d /dev/video0 --list-ctrls` (package `v4l-utils`).
- Fast walkers missed: widen `zone_width`.

## 4. Google Sheet

1. Create a Google Sheet.
2. Extensions > Apps Script. Replace the editor contents with
   `apps-script/Code.gs`. Save.
3. Project Settings > Script Properties > Add script property:
   `SECRET` = a long random string (e.g. from `openssl rand -hex 16`).
   Save script properties.
4. Deploy > New deployment > select type: Web app. Execute as: Me.
   Who has access: Anyone. Deploy, authorize, and copy the web app URL
   (ends in `/exec`).
5. Open the URL in a private/incognito window; it should show
   `{"ok":true,"service":"door counter endpoint"}`. (In a normal window,
   being signed into several Google accounts can make it show "unable to
   open file" even when it works.)
6. In `counter.ini`, set `[upload] url` to that URL, `secret` to the same
   value (no quotes), and `device_name` to a name for this door.

"Anyone" means anyone with the URL can reach it, but the script ignores
requests without the secret. After changing `Code.gs`, use Deploy > Manage
deployments > edit > Version: New version, so the URL stays the same.

The script creates three tabs on the first upload:

- **Log**: one row per event: `in` / `out` (camera), `manual_in` /
  `manual_out` (+1 buttons), `heartbeat` (hourly, with frames per second),
  `start` (counter started), `reset` (4am, with the residual). You can add
  your own columns to the right of the existing ones.
- **Daily**: one row per door per day (4am to 4am): total in/out, camera vs
  manual counts, camera share, residual at reset.
- **Status**: one row per door: when it last uploaded, last event, how many
  are inside now.

For several doors: either give every device the same URL and secret and a
different `device_name` (one sheet, filter by Device), or set up a sheet per
door and give each device its own sheet's URL and secret.

## 5. Install as a service

```
sudo useradd --system --no-create-home --shell /usr/sbin/nologin doorcounter
sudo install -d /opt/door-counter /etc/door-counter
sudo install -m 755 counter.py /opt/door-counter/
sudo install -m 640 -g doorcounter counter.ini /etc/door-counter/
sudo install -m 644 door-counter.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now door-counter
journalctl -u door-counter -f
```

The config is readable only by root and the service user, since it holds the
secret. After editing `/etc/door-counter/counter.ini`:
`sudo systemctl restart door-counter`.

Events are kept in `/var/lib/door-counter/events.db` and uploaded every
minute. If the network is down they wait there and are sent later.

### Database size and cleanup

Uploaded events older than `keep_days` (default 365) are deleted
automatically, at about 136 bytes per event. Events not yet uploaded are
never deleted.

```
# size and queue
sudo -u doorcounter python3 /opt/door-counter/counter.py --config /etc/door-counter/counter.ini --stats
# delete uploaded events older than 90 days and shrink the file
sudo -u doorcounter python3 /opt/door-counter/counter.py --config /etc/door-counter/counter.ini --clean 90
```

Run these as `doorcounter`, not plain `sudo`: as root they could leave
database files the service can't write to, so the script refuses.

## Privacy and security

- No images are written to disk. The preview streams a live low-res view to
  anyone on the local network who opens the page; set
  `[preview] enabled = false` once positioned if you don't want that.
- The preview page and the +1 buttons have no login. Anyone on the network
  can add corrections; set `[preview] allow_corrections = false` to remove
  them.
- The sheet's URL plus the secret let anyone add rows to that sheet, nothing
  more. Keep `counter.ini` out of version control.

## Clock

Timestamps, the day each count belongs to, and the 4am reset rely on the
device clock, which is set over the internet. A board without a battery-
backed clock that loses power and boots without internet will have the
wrong time until it syncs.

## Tests

The tests run on any computer with Python 3 and OpenCV (Node.js for the
Apps Script test); no camera needed.

```
./tests/run_tests.sh
```

- `make_videos.py` generates synthetic videos with known answers: walking
  figures for line mode, a body filling the frame for zones mode, sideways
  copies for `rotate`, and a legs-only view for a camera near the floor.
- `test_counts.py` runs the counter on each video and checks the counts.
- `test_logic.py` checks day boundaries and the 4am reset, restarts, manual
  corrections, database cleanup, and uploads against a fake endpoint that
  fails and recovers.
- `test_apps_script.js` runs `Code.gs` against a mock spreadsheet.

## License

MIT; see `LICENSE`.
