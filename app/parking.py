"""NYC parking regulations for Sheepshead Bay.

Two signals, both offline at request time:

1. Alternate Side Parking (ASP) status. NYC suspends ASP on 42 holidays a year. The live
   NYC311 calendar API requires a two-step subscription key, so the 2026 date list is
   hardcoded from the official DOT calendar PDF instead. Zero network, cannot fail on stage.
   https://www.nyc.gov/html/dot/downloads/pdf/asp-calendar-2026.pdf

2. Curb regulations from DOT's sign inventory (Socrata `nfid-uabd`), pre-fetched to
   data/sheepshead_signs.json. That dataset has no lat/lon -- coordinates are NY State Plane
   Long Island (EPSG:2263, feet) -- so the neighborhood bbox is expressed in state-plane units.

The ASP signal is the interesting half. Traffic alone says how many cars are arriving. It says
nothing about whether any of them are leaving. ASP is what makes parked cars move: when it runs,
a whole block face has to clear out and spots churn. When it's suspended for a holiday, nobody
moves and the neighborhood locks solid. Same traffic, opposite answer.
"""

from __future__ import annotations

import datetime as dt
import functools
import json
import pathlib
import re

DATA = pathlib.Path(__file__).resolve().parent.parent / "data" / "sheepshead_signs.json"

# Sheepshead Bay bbox in EPSG:2263 feet. Derived by least-squares fit against the 311 dataset,
# which publishes both coordinate systems; residuals under 0.4 ft.
BBOX_STATE_PLANE = {"x": (993968, 1006472), "y": (148766, 157881)}

# 2026 ASP suspensions, MM-DD. From the official DOT calendar PDF.
ASP_SUSPENDED_2026 = {
    "01-01", "01-06", "01-19", "02-12", "02-16", "02-17", "02-18", "03-03", "03-20", "03-21",
    "04-02", "04-03", "04-08", "04-09", "04-10", "05-14", "05-22", "05-23", "05-25", "05-27",
    "05-28", "06-19", "07-03", "07-04", "07-23", "08-15", "09-07", "09-12", "09-13", "09-21",
    "09-26", "09-27", "10-03", "10-04", "10-12", "11-01", "11-03", "11-08", "11-11", "11-26",
    "12-08", "12-25",
}

SUSPENSION_NAMES = {
    "08-15": "Feast of the Assumption",
    "09-07": "Labor Day",
    "09-12": "Rosh Hashanah",
    "09-13": "Rosh Hashanah",
    "09-21": "Yom Kippur",
    "11-26": "Thanksgiving",
    "12-25": "Christmas",
    "01-01": "New Year's Day",
}

DAYS = re.compile(r"\b(MONDAY|TUESDAY|WEDNESDAY|THURSDAY|FRIDAY|SATURDAY|SUNDAY)\b")
DAY_INDEX = {"MONDAY": 0, "TUESDAY": 1, "WEDNESDAY": 2, "THURSDAY": 3,
             "FRIDAY": 4, "SATURDAY": 5, "SUNDAY": 6}
TIME_RANGE = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*(AM|PM)\s*-\s*(\d{1,2})(?::(\d{2}))?\s*(AM|PM)")
MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
          "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
SEASONAL = re.compile(
    r"(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\s*(\d{1,2})\s*-\s*"
    r"(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\s*(\d{1,2})"
)
# "2 HMP" (2-Hour Metered Parking), "1 HOUR PARKING", "3 HR PARKING".
DURATION_LIMIT = re.compile(r"\b(\d+)\s*(?:HMP|HOUR|HR)\b")
ALL_DAYS = set(range(7))


def _restricted_days(d: str) -> set[int]:
    """Which weekdays a rule actually applies to.

    NYC signs express this two ways and they mean opposite things:
        "MONDAY THURSDAY 8AM-9:30AM"   -> applies Mon and Thu
        "8AM-7PM EXCEPT SUNDAY"        -> applies every day BUT Sunday

    Reading the day names out of the second form with a plain regex inverts the
    rule -- it was reporting "you must move by Sunday 8 AM" for a sign whose whole
    point is that Sunday is the day it does not apply.
    """
    head, sep, tail = d.partition("EXCEPT")
    named = {DAY_INDEX[x] for x in DAYS.findall(head)}

    if sep:
        excluded = {DAY_INDEX[x] for x in DAYS.findall(tail)}
        base = named or ALL_DAYS
        return base - excluded

    if "ALL DAYS" in d or "EVERY DAY" in d:
        return set(ALL_DAYS)
    return named


def _time(hour: str, minute: str | None, meridiem: str) -> dt.time:
    h = int(hour) % 12
    if meridiem == "PM":
        h += 12
    return dt.time(h, int(minute or 0))


def evaluate(sign_description: str, when: dt.datetime) -> dict:
    """Translate one cryptic NYC parking sign into a plain-English answer.

    Returns {legal: bool, until: datetime|None, reason: str}. `until` is what makes the answer
    useful -- "legal now, move by Tue 8 AM" beats a bare yes.
    """
    d = sign_description.upper()
    is_asp = "SANITATION BROOM" in d

    # Year-round bans. An ASP suspension does not help you here, which people get wrong.
    if "ANYTIME" in d and not is_asp:
        kind = "No Standing" if "NO STANDING" in d else "No Parking"
        return {"legal": False, "until": None, "reason": f"{kind} Anytime — never legal here"}

    # Seasonal bans. Sheepshead Bay is coastal; May 15-Sep 15 weekend bans are live right now.
    season = SEASONAL.search(d)
    if season:
        start = dt.date(when.year, MONTHS[season.group(1)], int(season.group(2)))
        end = dt.date(when.year, MONTHS[season.group(3)], int(season.group(4)))
        in_season = start <= when.date() <= end
        days = {DAY_INDEX[x] for x in DAYS.findall(d)}
        if in_season and (not days or when.weekday() in days):
            return {"legal": False,
                    "until": dt.datetime.combine(end, dt.time(23, 59)),
                    "reason": f"Seasonal ban {season.group(0)} on posted days"}
        why = "outside season" if not in_season else "in season but not a restricted day"
        return {"legal": True, "until": None, "reason": f"Legal — {why}"}

    days = _restricted_days(d)
    window = TIME_RANGE.search(d)

    # "2 HMP 8AM-7PM" is a two-hour metered limit, not a ban. You may park; you
    # just cannot leave the car all day. Reporting it as a prohibition was wrong,
    # and reporting it as unrestricted would be equally wrong.
    limit = DURATION_LIMIT.search(d)
    if limit and not any(k in d for k in ("NO PARKING", "NO STANDING", "NO STOPPING")):
        hours = limit.group(1)
        if days and window and when.weekday() in days:
            start_t = _time(window.group(1), window.group(2), window.group(3))
            end_t = _time(window.group(4), window.group(5), window.group(6))
            if start_t <= when.time() < end_t:
                return {"legal": True,
                        "until": dt.datetime.combine(when.date(), end_t),
                        "reason": f"{hours}-hour limit until {end_t.strftime('%-I:%M %p')}"}
        return {"legal": True, "until": None,
                "reason": f"{hours}-hour metered limit, not in effect right now"}

    # "AMBULETTE ONLY", "AUTHORIZED VEHICLES ONLY" -- reserved for someone else.
    if " ONLY" in d and "PARKING" not in d.split(" ONLY")[0][-12:]:
        if days and window:
            start_t = _time(window.group(1), window.group(2), window.group(3))
            end_t = _time(window.group(4), window.group(5), window.group(6))
            if when.weekday() in days and start_t <= when.time() < end_t:
                return {"legal": False,
                        "until": dt.datetime.combine(when.date(), end_t),
                        "reason": f"Reserved use until {end_t.strftime('%-I:%M %p')}"}

    if not days or not window:
        return {"legal": True, "until": None, "reason": "No parsed restriction (verify the sign)"}

    start_t = _time(window.group(1), window.group(2), window.group(3))
    end_t = _time(window.group(4), window.group(5), window.group(6))

    if is_asp and when.strftime("%m-%d") in ASP_SUSPENDED_2026:
        holiday = SUSPENSION_NAMES.get(when.strftime("%m-%d"), "holiday")
        return {"legal": True, "until": None,
                "reason": f"ASP suspended today ({holiday}) — nobody has to move"}

    if when.weekday() in days and start_t <= when.time() < end_t:
        return {"legal": False,
                "until": dt.datetime.combine(when.date(), end_t),
                "reason": f"Street cleaning until {end_t.strftime('%-I:%M %p')}"}

    for offset in range(8):
        candidate = when + dt.timedelta(days=offset)
        if candidate.weekday() not in days:
            continue
        nxt = dt.datetime.combine(candidate.date(), start_t)
        if nxt <= when:
            continue
        if is_asp and nxt.strftime("%m-%d") in ASP_SUSPENDED_2026:
            continue
        return {"legal": True, "until": nxt,
                "reason": f"Legal now; must move by {nxt.strftime('%a %-I:%M %p')}"}

    return {"legal": True, "until": None, "reason": "Legal now"}


def asp_status(when: dt.datetime) -> dict:
    """Is alternate-side parking running, and when does the next sweep churn spots?

    `churn_soon` is the demand multiplier: a sweep in the next 14 hours means a block face is
    about to empty out.
    """
    key = when.strftime("%m-%d")
    suspended = key in ASP_SUSPENDED_2026

    nxt = None
    for offset in range(8):
        day = (when + dt.timedelta(days=offset)).date()
        if day.strftime("%m-%d") in ASP_SUSPENDED_2026 or day.weekday() == 6:
            continue
        # Most Sheepshead Bay sweeps start between 8 and 11:30 AM; 8:30 is the modal time.
        candidate = dt.datetime.combine(day, dt.time(8, 30))
        if candidate > when:
            nxt = candidate
            break

    hours_out = (nxt - when).total_seconds() / 3600 if nxt else None
    return {
        "in_effect": not suspended,
        "suspended_reason": SUSPENSION_NAMES.get(key, "holiday") if suspended else None,
        "next_sweep": nxt.isoformat() if nxt else None,
        "next_sweep_human": nxt.strftime("%a %-I:%M %p") if nxt else None,
        "hours_until_sweep": round(hours_out, 1) if hours_out is not None else None,
        "churn_soon": bool(hours_out is not None and hours_out <= 14),
    }


@functools.lru_cache(maxsize=1)
def load_signs() -> list[dict]:
    """Pre-fetched DOT sign inventory for the neighborhood. Never hits the network."""
    if not DATA.exists():
        return []
    return json.loads(DATA.read_text())


def sign_summary(when: dt.datetime) -> dict:
    """How much of the neighborhood's curb is legal right now, per the sign inventory."""
    signs = load_signs()
    if not signs:
        return {"total": 0, "legal_now": 0, "pct_legal": None, "asp_signs": 0}

    legal = 0
    asp = 0
    for s in signs:
        desc = s.get("sign_description", "")
        if "SANITATION BROOM" in desc.upper():
            asp += 1
        if evaluate(desc, when)["legal"]:
            legal += 1

    return {
        "total": len(signs),
        "legal_now": legal,
        "pct_legal": round(100 * legal / len(signs), 1),
        "asp_signs": asp,
    }


if __name__ == "__main__":
    now = dt.datetime.now()
    print("ASP:", json.dumps(asp_status(now), indent=2))
    print("signs:", json.dumps(sign_summary(now), indent=2))
    print("Aug 15:", json.dumps(asp_status(dt.datetime(2026, 8, 15, 9, 0)), indent=2))
