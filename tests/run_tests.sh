#!/bin/sh
# Run all tests. Needs python3 with OpenCV (python3-opencv); the Apps Script
# test also needs Node.js and is skipped without it.
set -e
cd "$(dirname "$0")/.."
[ -f tests/videos/legs.avi ] || python3 tests/make_videos.py
python3 tests/test_logic.py
python3 tests/test_counts.py
if command -v node >/dev/null 2>&1; then
    node tests/test_apps_script.js
else
    echo "SKIP  Apps Script test (node not installed)"
fi
