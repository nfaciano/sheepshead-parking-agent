"""Address -> the block faces around it, ranked by whether you can park there.

This is the half of the project with real spatial resolution. The traffic cameras
only see four gateway roads and can say nothing about any particular street. DOT's
sign inventory, by contrast, is recorded per block face -- 1,224 of them across 143
streets in Sheepshead Bay -- so once you know where someone is standing you can tell
them what the sign on each nearby block actually says right now.

A "block face" is one side of one street between two cross streets: EAST 14 STREET,
from AVENUE X to AVENUE Y, west side. That is the unit New York parking rules are
written in, and it is the unit a driver thinks in.

What this does NOT know: whether a legal block face has an empty space on it. Nobody
publishes that. Legality is a hard fact from the sign inventory; availability is the
camera-derived demand estimate applied to the whole neighborhood.
"""

from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import functools
import math
import re
from typing import Optional
from zoneinfo import ZoneInfo

import requests

from . import parking, pressure

NYC = ZoneInfo("America/New_York")


def now_nyc() -> dt.datetime:
    """Naive New York wall-clock time.

    Posted parking signs are written in local time, so every comparison in this module
    has to be against local time. A bare datetime.now() picks up the container clock,
    which on Cloud Run is UTC -- correct on a laptop in New York, four hours wrong in
    production.
    """
    return dt.datetime.now(NYC).replace(tzinfo=None)

# Lat/lon -> NY State Plane Long Island (EPSG:2263, feet). Fitted against the 311
# dataset, which publishes both coordinate systems; residuals under half a foot,
# which is far finer than the block-face resolution we need.
def to_state_plane(lat: float, lon: float) -> tuple[float, float]:
    x = 277763.5746 * lon + -193.6408 * lat + 21546613.1347
    y = 166.3005 * lon + 364314.2460 * lat + -14620983.9547
    return x, y


NEIGHBORHOOD_BBOX = {"lat": (40.560, 40.615), "lon": (-73.985, -73.905)}

GEOCODERS = (
    "census",
    "planninglabs",
)

TIMEOUT = 12


class GeocodeError(RuntimeError):
    pass


def _clean_street(name: str) -> str:
    """DOT pads street names with runs of spaces: 'EAST   14 STREET'."""
    return re.sub(r"\s+", " ", (name or "").strip()).title()


SIDE_NAMES = {
    "N": "north side", "S": "south side", "E": "east side", "W": "west side",
    "NE": "northeast side", "NW": "northwest side",
    "SE": "southeast side", "SW": "southwest side",
}


def _geocode_census(query: str) -> dict:
    r = requests.get(
        "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress",
        params={"address": query, "benchmark": "Public_AR_Current", "format": "json"},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    matches = r.json()["result"]["addressMatches"]
    if not matches:
        raise GeocodeError("census: no match")
    m = matches[0]
    return {
        "lat": float(m["coordinates"]["y"]),
        "lon": float(m["coordinates"]["x"]),
        "matched": m["matchedAddress"],
        "source": "US Census Geocoder",
    }


def _geocode_planninglabs(query: str) -> dict:
    r = requests.get(
        "https://geosearch.planninglabs.nyc/v2/search",
        params={"text": query, "size": 1},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    features = r.json().get("features") or []
    if not features:
        raise GeocodeError("planninglabs: no match")
    f = features[0]
    lon, lat = f["geometry"]["coordinates"]
    return {
        "lat": float(lat),
        "lon": float(lon),
        "matched": f["properties"].get("label", query),
        "source": "NYC Planning GeoSearch",
    }


def _in_neighborhood(hit: dict) -> bool:
    return (
        NEIGHBORHOOD_BBOX["lat"][0] <= hit["lat"] <= NEIGHBORHOOD_BBOX["lat"][1]
        and NEIGHBORHOOD_BBOX["lon"][0] <= hit["lon"] <= NEIGHBORHOOD_BBOX["lon"][1]
    )


def geocode(address: str) -> dict:
    """Address -> {lat, lon, matched, source}.

    Both geocoders are queried at once rather than in turn. Running them in sequence
    meant that anything the Census geocoder could not resolve -- "Emmons Ave" with no
    house number, a bare zip, a neighbourhood name -- paid its full round trip before
    the NYC-specific one was even tried, so the loosest queries were the slowest.
    Concurrently, the cost of any lookup is the slower of the two, not the sum.

    Where they disagree, prefer the one that landed in the neighbourhood: the national
    geocoder will cheerfully resolve "Manhattan" to Montana, while NYC's own service
    understands a partial New York street name.
    """
    address = (address or "").strip()
    if not address:
        raise GeocodeError("empty address")

    query = address
    if "brooklyn" not in query.lower() and ", ny" not in query.lower():
        query = f"{query}, Brooklyn, NY"

    hits: list[dict] = []
    errors: list[str] = []

    pool = cf.ThreadPoolExecutor(max_workers=2)
    try:
        futures = {
            pool.submit(_geocode_census, query): "census",
            pool.submit(_geocode_planninglabs, query): "planninglabs",
        }
        for future in cf.as_completed(futures, timeout=TIMEOUT + 3):
            try:
                hit = future.result()
            except Exception as exc:  # noqa: BLE001 - one failing is expected
                errors.append(f"{futures[future]}: {exc}")
                continue
            # First answer that lands in the neighbourhood wins. Waiting for the
            # other one cannot improve it and costs the caller the slower round trip.
            if _in_neighborhood(hit):
                return hit
            hits.append(hit)
    finally:
        # Don't block on the loser; its result is no longer wanted.
        pool.shutdown(wait=False, cancel_futures=True)

    if not hits:
        raise GeocodeError("; ".join(errors) or "no geocoder available")

    # Nothing landed in the neighbourhood. Return a hit anyway so the caller can say
    # "that address is outside Sheepshead Bay" rather than "I could not find it".
    return hits[0]


@functools.lru_cache(maxsize=1)
def _block_faces() -> list[dict]:
    """Collapse the sign inventory into one record per block face.

    A block face carries several signs; they can disagree (a broom rule and a No
    Standing rule on the same stretch). We keep every distinct rule text so the
    evaluation can apply the most restrictive one.
    """
    grouped: dict[tuple, dict] = {}

    for s in parking.load_signs():
        on = _clean_street(s.get("on_street"))
        if not on:
            continue
        key = (
            on,
            _clean_street(s.get("from_street")),
            _clean_street(s.get("to_street")),
            (s.get("side_of_street") or "").strip().upper(),
        )
        try:
            x = float(s["sign_x_coord"])
            y = float(s["sign_y_coord"])
        except (KeyError, TypeError, ValueError):
            continue

        face = grouped.get(key)
        if face is None:
            face = grouped[key] = {
                "on_street": key[0],
                "from_street": key[1],
                "to_street": key[2],
                "side": key[3],
                "rules": [],
                "_xs": [],
                "_ys": [],
            }
        desc = (s.get("sign_description") or "").strip()
        if desc and desc not in face["rules"]:
            face["rules"].append(desc)
        face["_xs"].append(x)
        face["_ys"].append(y)

    faces = []
    for face in grouped.values():
        xs, ys = face["_xs"], face["_ys"]
        face["x"] = sum(xs) / len(xs)
        face["y"] = sum(ys) / len(ys)
        face["sign_count"] = len(xs)
        # Distance between the two furthest-apart signs on this face: a lower bound
        # on the block's length, used to estimate how many cars it holds.
        face["span_ft"] = (
            math.hypot(max(xs) - min(xs), max(ys) - min(ys)) if len(xs) > 1 else 0.0
        )
        del face["_xs"], face["_ys"]
        faces.append(face)
    return faces


SPACE_LENGTH_FT = 22.0  # a parallel-parking space plus the gap you leave


def _approx_spaces(face: dict) -> Optional[int]:
    """Roughly how many cars fit along this block face.

    Estimated from how far apart its own signs are: DOT posts signs along the length
    of a regulated stretch, so the spread between the outermost signs is a floor on
    the block's length. It is a floor, not a measurement -- a face with one sign gives
    us nothing, and we return None rather than guess.

    Turning "try Avenue X south side" into "about 30 spaces on that stretch" is the
    difference between a street name and a place you can picture.
    """
    span = face.get("span_ft")
    if not span or span < SPACE_LENGTH_FT:
        return None
    return max(1, int(span / SPACE_LENGTH_FT))


def _evaluate_face(face: dict, when: dt.datetime) -> dict:
    """Apply every rule posted on this face; the most restrictive one wins."""
    verdicts = [parking.evaluate(rule, when) for rule in face["rules"]] or [
        {"legal": True, "until": None, "reason": "No posted restriction found"}
    ]

    illegal = [v for v in verdicts if not v["legal"]]
    if illegal:
        # Surface the one that frees up soonest -- that's what a driver waits for.
        with_until = [v for v in illegal if v["until"]]
        chosen = min(with_until, key=lambda v: v["until"]) if with_until else illegal[0]
        return {"legal": False, "until": chosen["until"], "reason": chosen["reason"]}

    # All legal: the binding constraint is the soonest time you'd have to move.
    with_until = [v for v in verdicts if v["until"]]
    if with_until:
        chosen = min(with_until, key=lambda v: v["until"])
        return {"legal": True, "until": chosen["until"], "reason": chosen["reason"]}
    return verdicts[0]


def _confidence(
    face: dict,
    legal: bool,
    hours_free: Optional[float],
    distance_ft: float,
    radius_ft: float,
    time_limited: bool,
) -> tuple[str, int]:
    """How good a bet is this block face, and a 0-100 number behind the label.

    Scores LEGALITY, DURATION and DISTANCE, all of which come from real records.
    It does not score availability -- no public feed says whether a space is empty.
    The label means "worth trying", never "there is a spot here".

    Distance carries real weight on purpose. An earlier version scored only
    legality and duration, and since almost every residential face in Sheepshead
    Bay is legal overnight, every result came back at exactly 100 -- a ranking that
    ranks nothing. What actually separates these blocks for a driver is how far
    they are from the door.
    """
    if not legal:
        return "no", 0

    # More posted signs means the rule is well attested, not inferred from one
    # stray record.
    attested = min(face.get("sign_count", 1), 4) / 4.0

    if hours_free is None:
        duration = 1.0
    else:
        # Overnight (14h+) is as good as it gets. Under an hour is nearly useless.
        duration = max(0.0, min(hours_free / 14.0, 1.0))

    # A 2-hour metered limit is legal but not somewhere you leave the car.
    if time_limited:
        duration = min(duration, 0.35)

    proximity = max(0.0, 1.0 - (distance_ft / radius_ft))

    score = int(round(100 * (0.45 * duration + 0.35 * proximity + 0.20 * attested)))
    score = max(1, min(99, score))

    # Thresholds sit where they do because of what the data actually looks like:
    # almost every residential face in Sheepshead Bay is legal overnight, so scores
    # bunch in the 65-90 band. Set "good" at 70 and every row gets the same green
    # pill, which reads as "not ranking anything" even though the ordering is real.
    if score >= 80:
        return "good", score
    if score >= 68:
        return "fair", score
    return "tight", score


# How much each congestion level a camera reports drags down the odds on the blocks
# nearest to it. A gateway running at a standstill is feeding cars into those blocks
# right now; an empty one is not.
CONGESTION_PRESSURE = {
    "empty": 0.00,
    "light": 0.15,
    "moderate": 0.45,
    "heavy": 0.80,
    "stopped": 1.00,
}


def _camera_pressure(face_x: float, face_y: float, cameras: list[dict]) -> Optional[dict]:
    """Which gateway camera is feeding this block, and how hard.

    Each camera watches one road into the neighborhood. Cars that pass it have to
    park somewhere, and the blocks closest to it absorb them first. So the nearest
    camera's inbound congestion is a real, local signal for a given block face --
    much better than applying one neighborhood-wide number to every street equally.

    Honest about what this is: a proxy. It does not track any individual vehicle
    from a camera to a parking space, and it cannot.
    """
    if not cameras:
        return None

    best = None
    for cam in cameras:
        cx, cy = to_state_plane(cam["lat"], cam["lon"])
        d = math.hypot(face_x - cx, face_y - cy)
        if best is None or d < best[0]:
            best = (d, cam)

    distance, cam = best
    level = (cam.get("congestion_inbound") or "").lower()
    pressure = CONGESTION_PRESSURE.get(level)

    if pressure is None:
        # No congestion word from the model -- fall back to the raw net inbound.
        net = cam.get("inbound", 0) - cam.get("outbound", 0)
        pressure = max(0.0, min(net / 12.0, 1.0))

    # Influence decays with distance. Past ~1.5 miles a gateway says nothing useful
    # about a particular block.
    reach_ft = 8000.0
    influence = max(0.0, 1.0 - (distance / reach_ft))

    return {
        "camera": cam["name"],
        "camera_distance_ft": int(distance),
        "congestion_inbound": level or "unknown",
        "pressure": round(pressure * influence, 3),
    }


def nearby(
    lat: float,
    lon: float,
    when: Optional[dt.datetime] = None,
    radius_ft: float = 1200.0,
    limit: int = 12,
    cameras: Optional[list[dict]] = None,
) -> list[dict]:
    """Block faces within `radius_ft`, best bets first."""
    when = when or now_nyc()
    ox, oy = to_state_plane(lat, lon)

    results = []
    for face in _block_faces():
        distance = math.hypot(face["x"] - ox, face["y"] - oy)
        if distance > radius_ft:
            continue

        verdict = _evaluate_face(face, when)
        until = verdict["until"]
        hours_free = (until - when).total_seconds() / 3600 if until else None
        time_limited = "limit" in verdict["reason"].lower()
        label, score = _confidence(
            face, verdict["legal"], hours_free, distance, radius_ft, time_limited
        )

        feed = _camera_pressure(face["x"], face["y"], cameras or [])
        contested = pressure.for_block(
            face["on_street"], face["from_street"], face["to_street"], face["side"]
        )

        if verdict["legal"]:
            # Two independent discounts, both capped so neither can outrank legality.
            #
            # The gateway camera says how much traffic is arriving in the area right
            # now. The 311 history says how contested this particular block has been
            # over six months. The camera is live but neighbourhood-wide; the
            # complaints are local but historical. Applying both means a block that is
            # normally fought over AND is being fed by a jammed road ranks below an
            # otherwise identical quiet one -- which is the whole point of collecting
            # them separately.
            if feed:
                score = int(round(score * (1.0 - 0.30 * feed["pressure"])))
            if contested:
                # Ranked against the neighbourhood, not on an absolute count: a block
                # with 40 complaints is contested in Sheepshead Bay terms, which is
                # what matters when choosing between blocks in Sheepshead Bay.
                penalty = {"high": 0.20, "moderate": 0.08}.get(contested["level"], 0.0)
                score = int(round(score * (1.0 - penalty)))
            label = "good" if score >= 80 else "fair" if score >= 68 else "tight"

        between = ""
        if face["from_street"] and face["to_street"]:
            between = f"between {face['from_street']} and {face['to_street']}"

        results.append({
            "street": face["on_street"],
            "between": between,
            "side": SIDE_NAMES.get(face["side"], face["side"] or "unspecified side"),
            "legal_now": verdict["legal"],
            "reason": verdict["reason"],
            "until": until.isoformat() if until else None,
            "until_human": until.strftime("%a %-I:%M %p") if until else None,
            "hours_free": round(hours_free, 1) if hours_free is not None else None,
            "confidence": label,
            "confidence_score": score,
            "distance_ft": int(distance),
            "walk_minutes": max(1, round(distance / 280)),  # ~3.2 ft/s
            "signs_posted": face["sign_count"],
            "rules": face["rules"][:3],
            "feeder_camera": feed,
            # How contested this block actually is, from 311 illegal-parking
            # complaints snapped to it. Unlike the cameras, this covers every block
            # in the neighborhood and is about parking rather than through-traffic.
            "pressure": contested,
            # Roughly how many cars the stretch holds, so "try this block" has a size.
            "approx_spaces": _approx_spaces(face),
            # Offsets in feet from the queried address, east and north positive.
            # State plane is already a flat grid in feet, so this is a plain
            # subtraction -- no projection maths needed on the client, and the map
            # can be drawn without a tile server or any external library.
            "dx_ft": int(face["x"] - ox),
            "dy_ft": int(face["y"] - oy),
        })

    # Legal first, then the longest window, then closest.
    results.sort(key=lambda r: (not r["legal_now"], -r["confidence_score"], r["distance_ft"]))
    return results[:limit]


def lookup(address: str, when: Optional[dt.datetime] = None, **kwargs) -> dict:
    """Geocode, then rank the block faces around it."""
    when = when or now_nyc()
    location = geocode(address)

    in_area = (
        NEIGHBORHOOD_BBOX["lat"][0] <= location["lat"] <= NEIGHBORHOOD_BBOX["lat"][1]
        and NEIGHBORHOOD_BBOX["lon"][0] <= location["lon"] <= NEIGHBORHOOD_BBOX["lon"][1]
    )

    faces = nearby(location["lat"], location["lon"], when=when, **kwargs) if in_area else []


    # The single answer. "Instead of circling" means telling someone where to go
    # first, not handing them a ranked list to choose from. The list stays below
    # it as the fallbacks, in order.
    top = faces[0] if faces else None
    recommendation = None
    if top:
        where = f"{top['street']}, {top['side']}"
        if top["between"]:
            where += f" ({top['between']})"
        recommendation = {
            "street": top["street"],
            "side": top["side"],
            "between": top["between"],
            "headline": f"Try {where} first",
            "walk_minutes": top["walk_minutes"],
            "distance_ft": top["distance_ft"],
            "why": top["reason"],
            # The posted sign this answer was derived from. Showing it next to the
            # plain-English version is the clearest proof the translation is real.
            "rules": top["rules"],
            "confidence": top["confidence"],
            "confidence_score": top["confidence_score"],
            # Include the cross streets: the runner-up is often the same street and
            # side as the top pick, just a different stretch of it, and without them
            # the two lines read as the same place named twice.
            "backup": (
                ", ".join(
                    p for p in (
                        faces[1]["street"], faces[1]["side"], faces[1]["between"]
                    ) if p
                )
                if len(faces) > 1 else None
            ),
        }

    return {
        "query": address,
        "location": location,
        "recommendation": recommendation,
        "in_coverage": in_area,
        "coverage_note": (
            None
            if in_area
            else "That address is outside Sheepshead Bay. Curb data is only "
                 "pre-fetched for this neighborhood."
        ),
        "asp": parking.asp_status(when),
        "block_faces": faces,
        "evaluated_at": when.isoformat(),
        "caveat": "Legality comes from posted DOT sign records. Whether a space is "
                  "actually free is not knowable from any public feed.",
    }
