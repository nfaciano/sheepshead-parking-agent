"""How contested each block actually is, from 311 illegal-parking complaints.

## Why this exists

The traffic cameras cannot answer the question people actually have. They count
vehicles visible on a parkway up to a mile away, and a car in that frame might be
going to Rockaway, or JFK, or picking up a pizza and leaving in four minutes. Nothing
in a still frame distinguishes a through-trip from someone who parked and went inside.
So gateway traffic is context, not parking demand, and it is certainly not evidence
that anybody parked.

311 illegal-parking complaints are different, and better, for three reasons:

  1. They carry a real lat/lon, so they attach to a specific block, not a neighborhood.
  2. They are about parking specifically -- a blocked hydrant is somebody who could not
     find a legal space and took the risk anyway.
  3. They accumulate over months, so they describe how a block behaves, not how it
     looked in one frame.

A block with 40 complaints in six months is genuinely contested. A block with two is
not. That is a fact about the street that a resident already half-knows and can never
quite prove, and it is exactly the thing a camera cannot tell them.

## What it is not

Complaint counts are a proxy for pressure, not a measurement of occupancy. They are
biased by who calls 311: commercial strips and blocks with an aggrieved neighbor are
over-reported, quiet blocks under-reported. This ranks blocks against each other
within one neighborhood, where that bias is at least consistent. It does not say a
space is free, and nothing here ever will.
"""

from __future__ import annotations

import functools
import json
import math
import pathlib
from collections import Counter, defaultdict

DATA = pathlib.Path(__file__).resolve().parent.parent / "data" / "illegal_parking_311.json"

# Complaints get snapped to the nearest block face within this radius. A block face
# is a few hundred feet long, so this is deliberately generous.
SNAP_RADIUS_FT = 260.0
GRID_FT = 500.0


def _cell(x: float, y: float) -> tuple[int, int]:
    return (int(x // GRID_FT), int(y // GRID_FT))


@functools.lru_cache(maxsize=1)
def _complaints() -> list[tuple[float, float, str]]:
    """(x, y, descriptor) in state-plane feet. Empty list if the file didn't ship."""
    if not DATA.exists():
        return []

    from .blocks import to_state_plane

    out = []
    for row in json.loads(DATA.read_text()):
        try:
            lat = float(row["latitude"])
            lon = float(row["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        x, y = to_state_plane(lat, lon)
        out.append((x, y, row.get("descriptor") or "Illegal Parking"))
    return out


@functools.lru_cache(maxsize=1)
def _index() -> dict:
    """Complaint counts per block face, keyed by (on_street, from, to, side).

    Built once. A plain nested loop would be 9,700 complaints x 1,224 block faces =
    ~12M distance checks; bucketing both into a 500 ft grid makes it a few hundred
    thousand and the whole index builds in well under a second.
    """
    from .blocks import _block_faces

    faces = _block_faces()
    if not faces:
        return {}

    grid: dict[tuple[int, int], list] = defaultdict(list)
    for face in faces:
        grid[_cell(face["x"], face["y"])].append(face)

    counts: dict[tuple, Counter] = defaultdict(Counter)
    for cx, cy, descriptor in _complaints():
        best = None
        gx, gy = _cell(cx, cy)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for face in grid.get((gx + dx, gy + dy), ()):
                    d = math.hypot(face["x"] - cx, face["y"] - cy)
                    if d <= SNAP_RADIUS_FT and (best is None or d < best[0]):
                        best = (d, face)
        if best is None:
            continue  # complaint isn't near any block face we know about
        face = best[1]
        key = (face["on_street"], face["from_street"], face["to_street"], face["side"])
        counts[key][descriptor] += 1

    return dict(counts)


@functools.lru_cache(maxsize=1)
def _scale() -> tuple[int, int]:
    """(median, 90th percentile) complaints per indexed block, for relative ranking."""
    totals = sorted(sum(c.values()) for c in _index().values())
    if not totals:
        return (0, 0)
    mid = totals[len(totals) // 2]
    p90 = totals[int(len(totals) * 0.9)]
    return (mid, max(p90, mid + 1))


def for_block(on_street: str, from_street: str, to_street: str, side: str) -> dict | None:
    """Complaint history for one block face, or None if we have nothing on it."""
    entry = _index().get((on_street, from_street, to_street, side))
    if not entry:
        return None

    total = sum(entry.values())
    median, p90 = _scale()

    if total >= p90:
        label, note = "high", "one of the most complained-about blocks around here"
    elif total > median:
        label, note = "moderate", "busier than most blocks nearby"
    else:
        label, note = "low", "quieter than most blocks nearby"

    return {
        "complaints_6mo": total,
        "per_month": round(total / 6.0, 1),
        "level": label,
        "note": note,
        "top_kinds": [k for k, _ in entry.most_common(3)],
        "means": "311 illegal-parking complaints logged on this block face over the "
                 "last 6 months. A proxy for how contested the curb is, not a "
                 "measure of whether a space is free.",
    }


def coverage() -> dict:
    """Summary for the README and the page footer."""
    idx = _index()
    return {
        "complaints_loaded": len(_complaints()),
        "blocks_with_history": len(idx),
        "median_per_block": _scale()[0],
        "busiest": sorted(
            (
                {"block": f"{k[0]} ({k[3]}) {k[1]}–{k[2]}", "complaints": sum(v.values())}
                for k, v in idx.items()
            ),
            key=lambda r: -r["complaints"],
        )[:5],
    }
