"""
Turn traffic signals + NYC parking regulations into a plain-English verdict.

PARTIAL STUB. The scoring model here is real and runs, but the weights are
hand-tuned intuition, not fitted to ground truth. There is no labeled dataset
of "how long did it actually take to park in Sheepshead Bay at time T", so
nothing here is calibrated. Treat the score as a heuristic, not a measurement.

The regulation logic (alternate side parking, meter hours) is real NYC rules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from . import parking

NYC_TZ = ZoneInfo("America/New_York")


@dataclass
class CameraSignal:
    """One camera's contribution to the verdict."""

    camera_id: str
    name: str
    approach: str
    inbound: int
    outbound: int
    total: int
    parking_relevance: float = 1.0
    ok: bool = True


@dataclass
class Verdict:
    headline: str
    subtext: str
    recommendation: str  # "go" | "maybe" | "avoid"
    score: float  # 0-100 pressure score. Higher = harder to park.
    reasons: list[str] = field(default_factory=list)
    regulations: list[str] = field(default_factory=list)
    net_inbound: int = 0
    total_vehicles: int = 0
    cameras_reporting: int = 0

    def to_dict(self) -> dict:
        return {
            "headline": self.headline,
            "subtext": self.subtext,
            "recommendation": self.recommendation,
            "score": round(self.score, 1),
            "reasons": self.reasons,
            "regulations": self.regulations,
            "net_inbound": self.net_inbound,
            "total_vehicles": self.total_vehicles,
            "cameras_reporting": self.cameras_reporting,
        }


# ---------------------------------------------------------------------------
# NYC parking regulations
# ---------------------------------------------------------------------------


def regulation_notes(now: datetime) -> tuple[list[str], float]:
    """Real NYC parking rules relevant to a Sheepshead Bay street-parking search.

    Returns (human-readable notes, a multiplier on the difficulty score).

    Sources: NYC DOT Alternate Side Parking calendar (the official 2026 holiday
    list, in app/parking.py) and DOT's sign inventory (`nfid-uabd`, 6,288 signs
    pre-fetched for this neighborhood). Muni-meter hours in Brooklyn commercial
    corridors typically run 8 AM - 7 PM, Mon-Sat.

    The ASP term is the one that matters and it runs OPPOSITE to intuition.
    Traffic tells you how many cars are arriving; it says nothing about whether
    any are leaving. ASP is what makes parked cars leave. When a sweep is coming,
    a whole block face has to clear out and spots churn. When ASP is suspended
    for a holiday, nobody moves and the neighborhood locks solid. Same traffic,
    opposite answer.
    """
    notes: list[str] = []
    multiplier = 1.0

    weekday = now.weekday()  # Monday = 0, Sunday = 6
    hour = now.hour

    # parking.py works in naive local time; strip the tzinfo before handing off.
    local = now.replace(tzinfo=None)
    asp = parking.asp_status(local)

    # --- Alternate side parking: the churn signal ---
    if not asp["in_effect"]:
        reason = asp["suspended_reason"] or "holiday"
        notes.append(
            f"Alternate side parking is SUSPENDED today ({reason}) — nobody has to move "
            f"their car, so the curb stays exactly as full as it already is."
        )
        multiplier *= 1.20
    elif asp["churn_soon"]:
        notes.append(
            f"Alternate side parking runs {asp['next_sweep_human']} — that block face has to "
            f"clear out, so spots will churn. Parking gets easier right after the sweep."
        )
        multiplier *= 0.85
    else:
        notes.append(
            f"Alternate side parking is in effect, next sweep {asp['next_sweep_human']} "
            f"({asp['hours_until_sweep']}h out) — no churn coming in the near term."
        )

    # --- What the actual signs on these blocks say right now ---
    signs = parking.sign_summary(local)
    if signs["total"]:
        notes.append(
            f"{signs['legal_now']:,} of {signs['total']:,} posted curb regulations in the "
            f"neighborhood allow parking at this moment ({signs['pct_legal']}% of the curb)."
        )
        # A curb that is mostly illegal right now concentrates everyone on what's left.
        if signs["pct_legal"] is not None and signs["pct_legal"] < 70:
            multiplier *= 1.15

    # --- Meters ---
    if weekday != 6 and 8 <= hour < 19:
        notes.append("Muni-meters are active on the commercial strips (roughly 8 AM - 7 PM, Mon-Sat).")
    else:
        notes.append("Muni-meters are off — metered spots on the avenues are free right now.")
        # Free metered spots absorb some demand off the residential blocks.
        multiplier *= 0.95

    # --- Residential occupancy by time of day ---
    if 18 <= hour < 23:
        notes.append("Evening: residents are home and parked. Curb occupancy is at its daily peak.")
        multiplier *= 1.25
    elif 23 <= hour or hour < 6:
        notes.append("Overnight: the curb is full but nobody is competing for it — expect a long crawl.")
        multiplier *= 1.15
    elif 6 <= hour < 10:
        notes.append("Morning: commuters are leaving, spots are opening up.")
        multiplier *= 0.80
    else:
        notes.append("Midday: moderate turnover on the residential blocks.")
        multiplier *= 0.95

    # --- Weekend beach/boardwalk effect ---
    if weekday >= 5 and 10 <= hour < 19:
        notes.append("Weekend daytime: Sheepshead Bay / Manhattan Beach draws outside traffic.")
        multiplier *= 1.15

    return notes, multiplier


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def build_verdict(
    signals: list[CameraSignal],
    now: Optional[datetime] = None,
) -> Verdict:
    """Combine live camera counts + parking regulations into a verdict."""
    now = now or datetime.now(NYC_TZ)

    reporting = [s for s in signals if s.ok]
    regs, reg_multiplier = regulation_notes(now)

    if not reporting:
        return Verdict(
            headline="No signal",
            subtext="Every gateway camera is unreachable right now. Can't call it.",
            recommendation="maybe",
            score=50.0,
            reasons=["All NYC DOT gateway cameras failed to respond."],
            regulations=regs,
            cameras_reporting=0,
        )

    total_vehicles = sum(s.total for s in reporting)
    net_inbound = sum(s.inbound - s.outbound for s in reporting)

    # Weighted inbound pressure: cameras on real parking corridors count more
    # than limited-access parkway cameras.
    weighted_pressure = sum(
        (s.inbound - s.outbound) * s.parking_relevance for s in reporting
    )
    weighted_volume = sum(s.total * s.parking_relevance for s in reporting)

    # Base score from raw volume across the gateways. ~10 vehicles per camera
    # in frame is a busy road.
    per_camera_volume = weighted_volume / max(len(reporting), 1)
    volume_score = min(per_camera_volume / 14.0, 1.0) * 55.0

    # Directional score: net cars flowing IN is the demand signal that matters.
    # +4 net per camera is a strong inbound surge.
    per_camera_net = weighted_pressure / max(len(reporting), 1)
    direction_score = max(min(per_camera_net / 4.0, 1.0), -1.0) * 25.0

    raw = 45.0 + volume_score * 0.6 + direction_score
    score = max(0.0, min(100.0, raw * reg_multiplier))

    reasons: list[str] = []
    reasons.append(
        f"{total_vehicles} vehicles across {len(reporting)} gateway cameras right now."
    )
    if net_inbound > 2:
        reasons.append(
            f"Net +{net_inbound} vehicles heading INTO the neighborhood — demand is building."
        )
    elif net_inbound < -2:
        reasons.append(
            f"Net {net_inbound} — more cars are leaving than arriving. The curb is freeing up."
        )
    else:
        reasons.append(
            f"Inbound and outbound are roughly balanced (net {net_inbound:+d}) — steady state."
        )

    busiest = max(reporting, key=lambda s: s.total)
    reasons.append(f"Heaviest approach: {busiest.name} ({busiest.total} vehicles in frame).")

    if len(reporting) < len(signals):
        reasons.append(
            f"{len(signals) - len(reporting)} camera(s) offline — confidence reduced."
        )

    headline, subtext, recommendation = _phrase(score, now)

    return Verdict(
        headline=headline,
        subtext=subtext,
        recommendation=recommendation,
        score=score,
        reasons=reasons,
        regulations=regs,
        net_inbound=net_inbound,
        total_vehicles=total_vehicles,
        cameras_reporting=len(reporting),
    )


def _phrase(score: float, now: datetime) -> tuple[str, str, str]:
    """Map a 0-100 difficulty score to a plain-English answer."""
    when = now.strftime("%-I:%M %p")

    if score < 30:
        return (
            "Yes — go now.",
            f"As of {when} the roads in are quiet and more cars are leaving than arriving. "
            f"This is about as good as it gets. Expect a short loop.",
            "go",
        )
    if score < 50:
        return (
            "Yes, probably.",
            f"As of {when} inbound traffic is moderate. You should find something within "
            f"a few blocks. Bring patience, not a lot of it.",
            "go",
        )
    if score < 68:
        return (
            "Toss-up.",
            f"As of {when} demand is real but not brutal. Budget 10-15 minutes of "
            f"circling and widen your radius past Emmons Ave.",
            "maybe",
        )
    if score < 82:
        return (
            "Not great.",
            f"As of {when} cars are pouring in faster than they're leaving. Expect a long "
            f"crawl. If you can push it an hour, do that.",
            "avoid",
        )
    return (
        "No — don't bother.",
        f"As of {when} every gateway is loaded and inbound flow is heavy. You will circle. "
        f"Take the B/Q or park north of Kings Hwy and walk.",
        "avoid",
    )
