#!/usr/bin/env python3
"""Smoke test: pull a live NYC DOT frame and have Gemini read the traffic.

    python3 scripts/test_vision.py

Reads GOOGLE_API_KEY from the environment, or from a .env file passed as argv[1].
"""

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import os  # noqa: E402

if len(sys.argv) > 1:
    for line in pathlib.Path(sys.argv[1]).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

import requests  # noqa: E402

from app import vision  # noqa: E402

CAMS = [
    ("111e79a9-eb5a-44d0-b062-481ac0a81901", "Belt Pkwy @ Plumb 3 St"),
    ("64ed1f5d-ba90-4c12-afda-9a9c3e658efd", "Ocean Pkwy @ Ave X"),
]

if not vision.available():
    sys.exit("GOOGLE_API_KEY not set — pass a .env file as the first argument.")

print("models this key can reach (best candidate first):", flush=True)
try:
    models = vision.list_models()
except Exception as exc:
    sys.exit(f"FAILED to list models: {exc}")
for m in models[:12]:
    print("   ", m)
print(f"    ... {len(models)} total\n", flush=True)

for cam_id, name in CAMS:
    frame = requests.get(
        f"https://webcams.nyctmc.org/api/cameras/{cam_id}/image", timeout=20
    ).content
    print(f"\n=== {name} ({len(frame):,} bytes) ===", flush=True)
    try:
        print(json.dumps(vision.read_frame(frame, name), indent=2))
    except Exception as exc:
        print("FAILED:", exc)
