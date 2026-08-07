"""Read a traffic camera frame with Gemini and describe the flow.

Why a vision-language model instead of an object detector:

The question this project asks is not "how many cars are in frame." It's "is traffic
into the neighborhood heavy right now." A detector answers the first and leaves you to
infer the second from a number whose meaning depends on the camera's zoom, angle, and
how much road is visible. A VLM answers the second directly, and it can see things a
box-counter structurally cannot — that one carriageway is stop-and-go while the other
is free-flowing, that it's raining, that a lane is closed.

The honest tradeoff, which belongs in the demo: a VLM read is less reproducible than
bounding boxes. Two calls on the same frame can disagree by a car or two. We pin
temperature to 0 and ask for structured output to cut that down, and we report a
qualitative congestion level rather than pretending a count is exact.

PRIVACY: at 352x240 a vehicle is 15-40 pixels. Plates and faces are not resolvable at
that scale -- this is a physical property of the input, not a policy we chose. Frames
are held in memory for the duration of one request and never written to disk. The
prompt explicitly asks for counts and flow only.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Optional, TypedDict

import requests

API_ROOT = "https://generativelanguage.googleapis.com/v1beta"

# Model availability changes faster than any hardcoded list, and a model can be
# *listed* while still refusing new users with a 404 ("no longer available to new
# users"). So we never trust a name: we rank what the API reports and fall through
# on failure until one actually answers. Preference order below is by name shape.
PREFERRED = ("flash-latest", "pro-latest", "flash", "pro")

# Never send an image to these; they can't take one or aren't chat models.
EXCLUDE_SUBSTRINGS = ("embedding", "aqa", "tts", "imagen", "veo", "image-generation")

TIMEOUT = 25

PROMPT = """You are looking at a still frame from a New York City DOT traffic camera.
It is low resolution (352x240). Ignore any burned-in timestamp banner.

This camera watches {name}. Traffic moving {inbound_desc} is heading INTO the
Sheepshead Bay neighborhood. Traffic moving the other way is heading OUT.

Report only vehicle flow. Do not describe or identify any person. Do not read any
license plate. Do not describe individual vehicles.

Return JSON only, no markdown fence:
{{
  "inbound": <int, vehicles visible heading INTO the neighborhood>,
  "outbound": <int, vehicles visible heading OUT>,
  "congestion_inbound": "<one of: empty, light, moderate, heavy, stopped>",
  "congestion_outbound": "<one of: empty, light, moderate, heavy, stopped>",
  "conditions": "<a short phrase: weather, light, visibility>",
  "note": "<one short sentence a driver would find useful>"
}}"""


class VisionRead(TypedDict, total=False):
    inbound: int
    outbound: int
    total: int
    congestion_inbound: str
    congestion_outbound: str
    conditions: str
    note: str
    model: str


class VisionUnavailable(RuntimeError):
    """No API key, or every candidate model refused."""


_working_model: Optional[str] = None


def api_key() -> Optional[str]:
    return os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")


def available() -> bool:
    return bool(api_key())


def _rank(name: str) -> tuple:
    """Sort key: preferred name shape first, then newest version, then shortest."""
    shape = next(
        (i for i, p in enumerate(PREFERRED) if p in name), len(PREFERRED)
    )
    # Pull a version number out of e.g. "gemini-2.5-flash" -> 2.5. Newest first.
    version = 0.0
    for token in name.replace("-", " ").split():
        try:
            version = max(version, float(token))
        except ValueError:
            continue
    return (shape, -version, len(name))


def list_models() -> list[str]:
    """Every model this key can call generateContent on, best candidate first."""
    key = api_key()
    if not key:
        raise VisionUnavailable("GOOGLE_API_KEY is not set")

    r = requests.get(f"{API_ROOT}/models", params={"key": key}, timeout=TIMEOUT)
    r.raise_for_status()

    names = [
        m["name"].split("/")[-1]
        for m in r.json().get("models", [])
        if "generateContent" in m.get("supportedGenerationMethods", [])
    ]
    names = [n for n in names if not any(x in n for x in EXCLUDE_SUBSTRINGS)]
    return sorted(names, key=_rank)


def resolve_model(force: bool = False) -> str:
    """Best-guess model. Not authoritative -- read_frame falls through on failure."""
    global _working_model
    if _working_model and not force:
        return _working_model

    candidates = list_models()
    if not candidates:
        raise VisionUnavailable("no generateContent model reachable with this key")
    return candidates[0]


def read_frame(jpeg_bytes: bytes, name: str, inbound_desc: str = "away from the camera") -> VisionRead:
    """Ask Gemini what the traffic is doing. Raises VisionUnavailable if it can't.

    Walks the candidate list until one model actually answers, then pins it for the
    process. A model can be listed and still 404 with "no longer available to new
    users", so being listed is not evidence it works -- answering is.
    """
    global _working_model
    if not api_key():
        raise VisionUnavailable("GOOGLE_API_KEY is not set")
    if not jpeg_bytes:
        raise VisionUnavailable("empty frame")

    if _working_model:
        candidates = [_working_model]
    else:
        candidates = list_models()[:6]

    errors = []
    for model in candidates:
        try:
            result = _call(jpeg_bytes, name, inbound_desc, model)
        except VisionUnavailable as exc:
            errors.append(f"{model}: {exc}")
            continue
        _working_model = model
        return result

    raise VisionUnavailable("all candidate models failed -- " + " | ".join(errors[:3]))


def _call(jpeg_bytes: bytes, name: str, inbound_desc: str, model: str) -> VisionRead:
    key = api_key()
    payload = {
        "contents": [{
            "parts": [
                {"text": PROMPT.format(name=name, inbound_desc=inbound_desc)},
                {"inline_data": {
                    "mime_type": "image/jpeg",
                    "data": base64.b64encode(jpeg_bytes).decode("ascii"),
                }},
            ]
        }],
        # Temperature 0 so the same frame gives the same read. Reproducibility is the
        # main thing a VLM gives up versus a detector; this claws some of it back.
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
    }

    r = requests.post(
        f"{API_ROOT}/models/{model}:generateContent",
        params={"key": key},
        json=payload,
        timeout=TIMEOUT,
    )
    if r.status_code != 200:
        raise VisionUnavailable(f"{model} returned HTTP {r.status_code}: {r.text[:200]}")

    body = r.json()
    try:
        text = body["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as exc:
        raise VisionUnavailable(f"unexpected response shape: {str(body)[:200]}") from exc

    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise VisionUnavailable(f"model did not return JSON: {text[:200]}") from exc

    inbound = int(data.get("inbound") or 0)
    outbound = int(data.get("outbound") or 0)
    return VisionRead(
        inbound=inbound,
        outbound=outbound,
        total=inbound + outbound,
        congestion_inbound=str(data.get("congestion_inbound", "unknown")),
        congestion_outbound=str(data.get("congestion_outbound", "unknown")),
        conditions=str(data.get("conditions", "")),
        note=str(data.get("note", "")),
        model=model,
    )
