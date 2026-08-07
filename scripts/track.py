#!/usr/bin/env python3
"""Log inbound/outbound readings to a JSONL file, one line per sweep.

    python3 scripts/track.py [interval_seconds]

Runs locally and independently of the deployed service on purpose. The app keeps
its own in-memory history, but that resets on every redeploy -- and we expect to
redeploy several more times tonight. This file survives all of it.

Each line is one camera reading at one moment. Append-only, so an interrupted run
loses at most the sweep in flight.
"""

import datetime as dt
import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: F401,E402 - side effect: loads .env
from app import detect  # noqa: E402
from app.cameras import GATEWAY_CAMERAS  # noqa: E402

import requests  # noqa: E402

OUT = ROOT / "data" / "history.jsonl"
INTERVAL = int(sys.argv[1]) if len(sys.argv) > 1 else 60


def sweep() -> int:
    stamp = dt.datetime.now().isoformat(timespec="seconds")
    written = 0

    for cam in GATEWAY_CAMERAS:
        try:
            frame = requests.get(cam.image_url, timeout=20).content
            counts = detect.count_vehicles(frame, cam.inbound_side, cam.id)
        except Exception as exc:  # noqa: BLE001 - log the gap, keep sweeping
            row = {"t": stamp, "camera": cam.name, "error": str(exc)[:120]}
        else:
            row = {
                "t": stamp,
                "camera": cam.name,
                "camera_id": cam.id,
                "inbound": counts.get("inbound", 0),
                "outbound": counts.get("outbound", 0),
                "total": counts.get("total", 0),
                "congestion_inbound": counts.get("congestion_inbound", ""),
                "congestion_outbound": counts.get("congestion_outbound", ""),
            }
            written += 1

        with OUT.open("a") as fh:
            fh.write(json.dumps(row) + "\n")

    return written


if __name__ == "__main__":
    OUT.parent.mkdir(parents=True, exist_ok=True)
    print(f"logging every {INTERVAL}s -> {OUT}", flush=True)
    while True:
        started = time.time()
        try:
            n = sweep()
            print(f"{dt.datetime.now():%H:%M:%S}  {n} cameras logged", flush=True)
        except Exception as exc:  # noqa: BLE001 - never let the loop die
            print(f"{dt.datetime.now():%H:%M:%S}  sweep failed: {exc!r}", flush=True)
        time.sleep(max(5, INTERVAL - (time.time() - started)))
