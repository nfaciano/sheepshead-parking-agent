"""Pressure over time, from the readings we've actually taken tonight.

## What this measures, and what it does not

Each reading counts the vehicles *visible in one frame*. Summing those across time
does NOT give you the number of cars that entered the neighborhood -- a car sitting
in stop-and-go traffic appears in dozens of consecutive frames and would be counted
dozens of times. Any "1,247 cars arrived tonight" claim built this way is wrong.

What the samples do support is **net inbound per frame**: at each moment, how many
more vehicles were heading in than out. Compare that across time and you get a real
signal about whether the neighborhood is filling up or draining, which is the thing
a driver actually wants to know. The level is meaningful; the running total is not.

Two sources, merged:
  - a seed file written by scripts/track.py, which runs outside the service and
    therefore survives redeploys
  - the service's own in-memory samples since this instance started
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
from collections import defaultdict

SEED = pathlib.Path(__file__).resolve().parent.parent / "data" / "history.jsonl"


def load_seed() -> list[dict]:
    """Readings logged by the standalone tracker, if the file shipped."""
    if not SEED.exists():
        return []

    per_moment: dict[str, dict] = defaultdict(lambda: {"inbound": 0, "outbound": 0, "cameras": 0})
    for line in SEED.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "inbound" not in row:
            continue  # an error row from a failed sweep
        bucket = per_moment[row["t"]]
        bucket["inbound"] += row["inbound"]
        bucket["outbound"] += row["outbound"]
        bucket["cameras"] += 1

    return [
        {
            "t": stamp,
            "net_inbound": v["inbound"] - v["outbound"],
            "total_vehicles": v["inbound"] + v["outbound"],
            "cameras": v["cameras"],
            "source": "tracker",
        }
        for stamp, v in sorted(per_moment.items())
    ]


def series(live_samples: list[dict], bucket_minutes: int = 5) -> dict:
    """Merge seed + live samples into a bucketed series the page can draw."""
    points = load_seed()

    for s in live_samples:
        points.append({
            "t": s["t"],
            "net_inbound": s.get("net_inbound", 0),
            "total_vehicles": s.get("total_vehicles", 0),
            "cameras": len(s.get("per_camera") or {}),
            "source": "service",
        })

    if not points:
        return {"points": [], "buckets": [], "summary": None}

    buckets: dict[str, list] = defaultdict(list)
    for p in points:
        try:
            stamp = dt.datetime.fromisoformat(p["t"])
        except ValueError:
            continue
        floored = stamp.replace(
            minute=(stamp.minute // bucket_minutes) * bucket_minutes,
            second=0,
            microsecond=0,
        )
        buckets[floored.isoformat()].append(p)

    series_out = []
    for stamp, group in sorted(buckets.items()):
        series_out.append({
            "t": stamp,
            "label": dt.datetime.fromisoformat(stamp).strftime("%-I:%M"),
            "net_inbound": round(sum(g["net_inbound"] for g in group) / len(group), 1),
            "readings": len(group),
        })

    summary = None
    if len(series_out) >= 2:
        first, last = series_out[0], series_out[-1]
        delta = last["net_inbound"] - first["net_inbound"]
        if delta > 1.5:
            verdict = "filling up"
        elif delta < -1.5:
            verdict = "emptying out"
        else:
            verdict = "holding steady"
        summary = {
            "direction": verdict,
            "delta": round(delta, 1),
            "from_label": first["label"],
            "to_label": last["label"],
            "readings": sum(b["readings"] for b in series_out),
        }

    return {
        "buckets": series_out,
        "summary": summary,
        "measures": (
            "Net vehicles heading in minus out, averaged per frame in each window. "
            "This is a pressure level, not a count of cars that arrived -- one car "
            "in stopped traffic appears in many consecutive frames."
        ),
    }
