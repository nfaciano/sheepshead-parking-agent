"""A parking agent: ask in plain English, it uses the tools and answers.

The rest of this codebase is a set of endpoints you have to know the shape of. This is
the layer that makes it an agent: Gemini gets a description of what it can do, decides
which tools to call and in what order, and writes the answer itself.

Why that is more than decoration. "I'm getting home around 7, where should I park?"
requires resolving "around 7" to an arrival offset, looking up the blocks *at that
time*, checking whether alternate side runs the next morning, and knowing that the
answer changes if it does. Hard-coding that chain gives you one question. Letting the
model plan over the tools answers questions nobody wrote a form for -- "is it worth
waiting an hour?", "what about next Saturday?", "which side of the street?"

The tools return the same structured data the HTTP API returns. Nothing here invents
facts: every number the model can state came out of a DOT sign record, the ASP
calendar, or a live camera frame. The system prompt is explicit that it must not claim
a space is empty, because no source in the system knows that.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

import requests

from . import blocks, parking, vision

NYC = ZoneInfo("America/New_York")


def _now() -> dt.datetime:
    """Local wall-clock time in New York, naive.

    Never use a bare datetime.now() here. Cloud Run containers run in UTC, so a naive
    local call told the agent it was four hours later than it was -- it worked on a
    laptop in New York and confidently offered parking advice for 11 PM at dinner
    time. Everything downstream compares against posted sign times, which are New York
    wall-clock, so that is the only clock this file may use.
    """
    return dt.datetime.now(NYC).replace(tzinfo=None)

API_ROOT = "https://generativelanguage.googleapis.com/v1beta"
MAX_STEPS = 6
TIMEOUT = 45

SYSTEM = """You are a parking assistant for Sheepshead Bay, Brooklyn. You help people
who are driving home and do not want to circle the block.

Right now it is {now}.

How to behave:
- Ground every claim in a tool result. If no tool returned it, you do not know it.
- Prefer the fewest tool calls that answer the question. Each one costs the person a
  wait, and the tools already fold in context you would otherwise ask for separately.
- Answer like a neighbour who knows the area, not like a database. Short sentences.
- Lead with the decision, then the reasoning behind it.
- Be brief. One recommendation, at most two alternatives, and one line each on the rule
  and the walk. Someone reading this is in a car. A list of every option they could
  have is the same problem as no answer at all.
- Resolve whatever time they are asking about and evaluate at that time, not now.
  Interpret an ambiguous time as the nearest one consistent with what they told you
  they were doing, and state which time you used so they can correct you.
- Tell them what they cannot observe for themselves. Anyone asking already knows the
  obvious things about their own neighbourhood; your value is in the parts that take
  a dataset to see.

Bounds on what you can claim:
- Describe locations only at the resolution your data has. The tools work in block
  faces -- one side of a street between two cross streets, a stretch of curb holding
  many cars. Distances are to the middle of such a stretch. Phrase every location and
  distance as being about that stretch, never in a way that points at one spot on it.
- Legality is knowable here. Availability is not. Nothing in this system observes
  whether a space is free, so never state or imply that one is. Assume every block
  already has cars on it and you are advising where it is worth looking.
- A signal is only evidence about what it actually measures. The traffic cameras watch
  arterials some distance away and see through-traffic, not parked cars; complaint
  history describes a block's past, not its present. Attribute each to its own scope
  and do not stretch it to cover the question being asked.
- Give no figure you were not handed. No invented percentages, accuracies, or odds.

If asked how it works, explain honestly: posted DOT sign records for each block face,
the alternate side parking calendar, and live traffic cameras read by a vision model.
"""

TOOL_DECLARATIONS = [
    {
        "name": "find_parking",
        "description": (
            "Rank the block faces near an address by where you are allowed to park. "
            "Returns street, side of street, cross streets, walk distance, whether it is "
            "legal at the given time, and when the car would have to move."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "address": {
                    "type": "string",
                    "description": "Street address in or near Sheepshead Bay, Brooklyn.",
                },
                "arriving_in_minutes": {
                    "type": "integer",
                    "description": (
                        "Minutes from now the driver will arrive. 0 means now. Use this "
                        "for 'in an hour', 'around 7', etc. Max 720."
                    ),
                },
            },
            "required": ["address"],
        },
    },
    {
        "name": "check_alternate_side",
        "description": (
            "Whether alternate side parking runs on a given date, and when the next street "
            "sweep is. Suspended means nobody has to move their car, so the curb stays full. "
            "Use this to explain why parking is easier or harder."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "Date as YYYY-MM-DD. Omit for today.",
                }
            },
        },
    },
    {
        "name": "get_traffic_conditions",
        "description": (
            "Live read of the four NYC DOT cameras on the roads into Sheepshead Bay. "
            "Returns vehicles and congestion per direction for each gateway. These are "
            "arterials up to a mile away, not residential streets."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
]


def _tool_find_parking(address: str, arriving_in_minutes: int = 0) -> dict:
    minutes = max(0, min(int(arriving_in_minutes or 0), 720))
    when = _now() + dt.timedelta(minutes=minutes)
    try:
        result = blocks.lookup(address, when, limit=6, cameras=_camera_snapshot())
    except blocks.GeocodeError as exc:
        return {"error": f"could not locate that address: {exc}"}

    if not result["in_coverage"]:
        return {"error": result["coverage_note"], "in_coverage": False}

    # Everything the model is likely to want next, returned up front. It used to make
    # three sequential round trips for one question -- find_parking, then the ASP
    # calendar, then the cameras -- and each one is a full model call, so the answer
    # took 13 seconds. Folding them in makes the common case a single call.
    conditions = _tool_get_traffic_conditions()

    return {
        "matched_address": result["location"]["matched"],
        "evaluated_at": when.strftime("%A %-I:%M %p"),
        "alternate_side": result["asp"],
        "traffic_right_now": (
            None if "error" in conditions else {
                "summary": conditions["summary"],
                "note": conditions["note"],
                "gateways": [
                    {"name": c["name"], "congestion_inbound": c["congestion_inbound"]}
                    for c in conditions["cameras"]
                ],
            }
        ),
        "what_a_block_is": (
            "Each entry is a BLOCK FACE: one side of one street between two cross "
            "streets. That is a stretch of curb several hundred feet long holding many "
            "parked cars. distance_to_block_midpoint_ft is the distance to the middle "
            "of that stretch, NOT to an available space. Nothing here knows whether any "
            "space is free -- assume the block already has cars on it."
        ),
        "blocks": [
            {
                "street": b["street"],
                "side": b["side"],
                "between": b["between"],
                "walk_minutes": b["walk_minutes"],
                "distance_to_block_midpoint_ft": b["distance_ft"],
                "legal": b["legal_now"],
                "reason": b["reason"],
                "must_move_by": b["until_human"],
                "rating": b["confidence"],
                "approx_spaces_on_this_stretch": b.get("approx_spaces"),
                "how_contested": b.get("pressure"),
            }
            for b in result["block_faces"]
        ],
        "how_contested_means": (
            "311 illegal-parking complaints logged on that block face over 6 months. "
            "High means people there routinely park illegally, which happens when "
            "nothing legal is left. Mention it when it is high or when comparing two "
            "blocks. Never present it as a chance of finding a space."
        ),
    }


def _tool_check_alternate_side(date: Optional[str] = None) -> dict:
    when = _now()
    if date:
        try:
            when = dt.datetime.combine(dt.date.fromisoformat(date), dt.time(9, 0))
        except ValueError:
            return {"error": f"could not read date '{date}', use YYYY-MM-DD"}

    status = parking.asp_status(when)
    status["date"] = when.strftime("%A, %B %-d %Y")
    status["what_it_means"] = (
        "Suspended: nobody has to move their car, so the curb stays exactly as full as "
        "it already is and parking is harder."
        if not status["in_effect"]
        else "In effect: a street sweeper forces that block face to clear out, so spots "
             "churn and parking gets easier right after the sweep."
    )
    return status


_conditions_provider: Optional[Callable[[], Optional[dict]]] = None


def set_conditions_provider(fn: Callable[[], Optional[dict]]) -> None:
    """Let main.py hand us its cached camera payload instead of re-reading frames."""
    global _conditions_provider
    _conditions_provider = fn


def _camera_snapshot() -> list[dict]:
    payload = _conditions_provider() if _conditions_provider else None
    if not payload:
        return []
    return [
        {
            "name": c["name"],
            "lat": c["latitude"],
            "lon": c["longitude"],
            "inbound": c["counts"].get("inbound", 0),
            "outbound": c["counts"].get("outbound", 0),
            "congestion_inbound": c["counts"].get("congestion_inbound", ""),
        }
        for c in payload.get("cameras", [])
        if c.get("online")
    ]


def _tool_get_traffic_conditions() -> dict:
    payload = _conditions_provider() if _conditions_provider else None
    if not payload:
        return {"error": "no camera reading available yet"}

    return {
        "as_of": payload.get("generated_at_nyc", "")[11:16],
        "summary": payload["verdict"]["headline"],
        "note": "These cameras are on arterials and parkways up to a mile from any "
                "residential block. They measure traffic heading into the neighborhood.",
        "cameras": [
            {
                "name": c["name"],
                "inbound": c["counts"].get("inbound"),
                "outbound": c["counts"].get("outbound"),
                "congestion_inbound": c["counts"].get("congestion_inbound"),
                "congestion_outbound": c["counts"].get("congestion_outbound"),
                "observation": c["counts"].get("note"),
            }
            for c in payload.get("cameras", []) if c.get("online")
        ],
    }


DISPATCH: dict[str, Callable[..., Any]] = {
    "find_parking": _tool_find_parking,
    "check_alternate_side": _tool_check_alternate_side,
    "get_traffic_conditions": _tool_get_traffic_conditions,
}


class AgentUnavailable(RuntimeError):
    pass


def ask(question: str) -> dict:
    """Run the tool-use loop and return the answer plus the trace of what it did.

    The trace is returned deliberately: a parking recommendation you cannot audit is
    worth very little, and showing which tools ran is the difference between an answer
    and a claim.
    """
    if not vision.available():
        raise AgentUnavailable("GOOGLE_API_KEY is not set")

    question = (question or "").strip()
    if not question:
        raise AgentUnavailable("empty question")

    model = vision.resolve_model()
    now = _now().strftime("%A, %B %-d %Y at %-I:%M %p")

    contents: list[dict] = [{"role": "user", "parts": [{"text": question}]}]
    trace: list[dict] = []

    for _ in range(MAX_STEPS):
        body = {
            "systemInstruction": {"parts": [{"text": SYSTEM.format(now=now)}]},
            "contents": contents,
            "tools": [{"functionDeclarations": TOOL_DECLARATIONS}],
            "generationConfig": {"temperature": 0.2},
        }

        r = requests.post(
            f"{API_ROOT}/models/{model}:generateContent",
            params={"key": vision.api_key()},
            json=body,
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            raise AgentUnavailable(f"{model} returned HTTP {r.status_code}: {r.text[:200]}")

        candidates = r.json().get("candidates") or []
        if not candidates:
            raise AgentUnavailable("model returned no candidates")

        parts = candidates[0].get("content", {}).get("parts", []) or []
        calls = [p["functionCall"] for p in parts if "functionCall" in p]

        if not calls:
            text = "".join(p.get("text", "") for p in parts).strip()
            return {
                "question": question,
                "answer": text or "I could not work that out.",
                "trace": trace,
                "model": model,
                "steps": len(trace),
            }

        contents.append({"role": "model", "parts": parts})

        responses = []
        for call in calls:
            name = call.get("name", "")
            args = call.get("args", {}) or {}
            fn = DISPATCH.get(name)
            if fn is None:
                out: Any = {"error": f"unknown tool {name}"}
            else:
                try:
                    out = fn(**args)
                except Exception as exc:  # noqa: BLE001 - report, don't crash the turn
                    out = {"error": f"{type(exc).__name__}: {exc}"}

            trace.append({"tool": name, "args": args, "ok": "error" not in (out or {})})
            responses.append({
                "functionResponse": {"name": name, "response": {"result": out}}
            })

        contents.append({"role": "user", "parts": responses})

    return {
        "question": question,
        "answer": "That took too many steps to work out. Try asking it more simply.",
        "trace": trace,
        "model": model,
        "steps": len(trace),
    }
