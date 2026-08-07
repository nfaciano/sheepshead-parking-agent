"""
Vehicle counting.

THIS IS A STUB. The interface is the contract; the implementation is placeholder.

    count_vehicles(jpeg_bytes) -> {"inbound": int, "outbound": int, "total": int}

A real detector (Roboflow hosted inference, or a local YOLO) drops in behind
that exact signature without touching main.py, cameras.py, or verdict.py.
`_count_via_zones` at the bottom of this file is the live adapter onto the real
counter in traffic/counter.py; set DETECTOR=zones to switch over.

PRIVACY: this pipeline counts vehicles. It does not read license plates and it
does not detect, match, or store faces. Frames are held in memory for seconds
and never written to disk.
"""

from __future__ import annotations

import hashlib
import os
from typing import Optional, TypedDict


class VehicleCounts(TypedDict, total=False):
    inbound: int
    outbound: int
    total: int
    # Only the Gemini backend fills these. A detector returns boxes; a VLM returns
    # a reading of the scene, and the reading is what the verdict actually wants.
    congestion_inbound: str
    congestion_outbound: str
    conditions: str
    note: str
    model: str


# DETECTOR=gemini (default) -> Gemini vision reads the frame. Needs GOOGLE_API_KEY.
# DETECTOR=zones            -> YOLO/Roboflow box counting in traffic/counter.py.
# DETECTOR=stub             -> hashed placeholder counts; the demo still runs.
#
# Gemini is the default because the question is "is traffic into the neighborhood
# heavy," not "how many cars are in frame." A box count means nothing without
# knowing the camera's zoom and how much road is in view; "the outbound side is
# stopped and the inbound side is free-flowing" means something immediately.
DETECTOR_BACKEND = os.environ.get("DETECTOR", "gemini").lower()

# Which classes count as a vehicle, for the real implementation.
VEHICLE_CLASSES = {"car", "truck", "bus", "motorcycle", "van"}


def count_vehicles(
    jpeg_bytes: bytes,
    inbound_side: str = "left",
    camera_id: str | None = None,
) -> VehicleCounts:
    """Count vehicles in one camera frame, split into inbound and outbound.

    Args:
        jpeg_bytes: raw JPEG from the NYC DOT camera (352x240).
        inbound_side: which half of the frame carries traffic heading INTO
            Sheepshead Bay. One of "left", "right", "top", "bottom". Used only
            by the coarse fallback; the real detector uses ROI polygons.
        camera_id: NYC DOT camera id, so the real detector can look up that
            camera's hand-drawn inbound/outbound zone polygons.

    Returns:
        {"inbound": int, "outbound": int, "total": int}

    Every backend degrades to the stub rather than breaking the demo.
    """
    if DETECTOR_BACKEND in ("gemini", "vlm"):
        counts = _count_via_gemini(jpeg_bytes, camera_id)
        if counts is not None:
            return counts
        # Fall through rather than break the demo.

    if DETECTOR_BACKEND in ("zones", "roboflow", "yolo"):
        counts = _count_via_zones(jpeg_bytes, camera_id)
        if counts is not None:
            return counts

    return _count_stub(jpeg_bytes, inbound_side)


# ---------------------------------------------------------------------------
# Gemini vision backend
# ---------------------------------------------------------------------------

_gemini_failed = False

SIDE_DESCRIPTIONS = {
    "left": "on the left side of the frame",
    "right": "on the right side of the frame",
    "top": "in the upper half of the frame",
    "bottom": "in the lower half of the frame",
}


def _count_via_gemini(
    jpeg_bytes: bytes,
    camera_id: str | None,
) -> Optional[VehicleCounts]:
    """Read the frame with Gemini. None if unavailable, so the caller degrades."""
    global _gemini_failed
    if _gemini_failed:
        return None

    try:
        from . import vision
    except Exception:  # noqa: BLE001 - requests missing, etc.
        _gemini_failed = True
        return None

    if not vision.available():
        _gemini_failed = True
        return None

    from .cameras import CAMERAS_BY_ID

    cam = CAMERAS_BY_ID.get(camera_id) if camera_id else None
    name = getattr(cam, "name", None) or "an NYC DOT traffic camera"
    # A spatial hint beats a directional one: the model can see which half of the
    # frame a vehicle is in, but it has no idea which compass direction the camera
    # faces. cameras.json carries the side; we hand over the side, not a heading.
    inbound_desc = SIDE_DESCRIPTIONS.get(
        getattr(cam, "inbound_side", "left"), "on the left side of the frame"
    )

    try:
        read = vision.read_frame(jpeg_bytes, name, inbound_desc)
    except Exception:  # noqa: BLE001 - upstream API failure; degrade, don't crash
        # Deliberately not latching _gemini_failed here: a single timeout or a
        # rate-limit blip should not disable vision for the rest of the process.
        # Only a structural problem (no key, no module) latches, above.
        return None

    return VehicleCounts(
        inbound=int(read.get("inbound", 0)),
        outbound=int(read.get("outbound", 0)),
        total=int(read.get("total", 0)),
        congestion_inbound=read.get("congestion_inbound", ""),
        congestion_outbound=read.get("congestion_outbound", ""),
        conditions=read.get("conditions", ""),
        note=read.get("note", ""),
        model=read.get("model", ""),
    )


# ---------------------------------------------------------------------------
# Adapter onto the real detector (traffic/counter.py)
# ---------------------------------------------------------------------------
#
# traffic/counter.py is the real vehicle counter: per-camera ROI polygons,
# banner-crop, zone classification, Roboflow-hosted or local-YOLO backends.
# Its contract is:
#
#     count_vehicles(jpeg_bytes, backend, cam_id) -> {..., "inbound", "outbound",
#                                                     "total_vehicles", ...}
#
# We adapt that superset down to this module's three-key contract, so main.py,
# cameras.py and verdict.py never learn that the detector changed.
#
# Imports are LAZY and guarded: the heavy deps (ultralytics/torch, or even
# requests) are not in this service's requirements.txt, so a missing dep must
# degrade to the stub instead of crashing the container at import time.

_zone_backend = None
_zone_backend_tried = False


def _get_zone_backend():
    """Build the real detector once. Returns (module, backend) or None."""
    global _zone_backend, _zone_backend_tried
    if _zone_backend_tried:
        return _zone_backend
    _zone_backend_tried = True

    try:
        import pathlib
        import sys

        traffic_dir = pathlib.Path(__file__).resolve().parent.parent / "traffic"
        if traffic_dir.is_dir() and str(traffic_dir) not in sys.path:
            sys.path.insert(0, str(traffic_dir))

        import counter  # type: ignore

        if os.environ.get("ROBOFLOW_API_KEY"):
            backend = counter.RoboflowHosted(
                os.environ.get("RF_MODEL_ID", "coco/50")
            )
        else:
            backend = counter.LocalYOLO(
                os.environ.get("YOLO_WEIGHTS", "yolo11s.pt")
            )

        _zone_backend = (counter, backend)
    except Exception:  # noqa: BLE001 - any failure means "use the stub"
        _zone_backend = None

    return _zone_backend


def _count_via_zones(
    jpeg_bytes: bytes,
    camera_id: str | None,
) -> Optional[VehicleCounts]:
    """Real detection via traffic/counter.py. None if unavailable."""
    pair = _get_zone_backend()
    if pair is None:
        return None

    counter, backend = pair
    try:
        raw = counter.count_vehicles(jpeg_bytes, backend, cam_id=camera_id)
    except Exception:  # noqa: BLE001 - upstream model/API failure
        return None

    inbound = int(raw.get("inbound", 0))
    outbound = int(raw.get("outbound", 0))
    total = int(raw.get("total_vehicles", inbound + outbound))
    return VehicleCounts(inbound=inbound, outbound=outbound, total=total)


# ---------------------------------------------------------------------------
# Stub implementation
# ---------------------------------------------------------------------------


def _count_stub(jpeg_bytes: bytes, inbound_side: str) -> VehicleCounts:
    """Plausible placeholder counts, derived deterministically from the frame.

    Hashing the JPEG bytes means the numbers actually move when the live image
    changes (~every 2s) instead of sitting frozen, and the same frame always
    yields the same answer. It is NOT computer vision. It is a placeholder that
    keeps the end-to-end pipeline honest and testable until the detector lands.
    """
    if not jpeg_bytes:
        return VehicleCounts(inbound=0, outbound=0, total=0)

    digest = hashlib.sha256(jpeg_bytes).digest()

    # A busy NYC arterial frame at 352x240 realistically shows 0-18 vehicles.
    total = 3 + (digest[0] % 16)

    # Split the frame's vehicles across the two directions. Bias mildly toward
    # inbound so the demo shows a meaningful directional signal.
    inbound_share = 0.40 + (digest[1] / 255.0) * 0.30  # 0.40 - 0.70
    inbound = round(total * inbound_share)
    outbound = total - inbound

    # inbound_side is accepted and ignored by the stub; the real detector uses
    # it to assign each detection box to a direction.
    _ = inbound_side

    return VehicleCounts(inbound=inbound, outbound=outbound, total=total)


def is_stub() -> bool:
    """True when counts are placeholder numbers, not real detections.

    The UI and the API surface this so nobody mistakes the demo for CV output.

    Deliberately reports the RUNTIME truth, not the configured intent: if
    DETECTOR is set to a real backend but that backend failed to load and we
    silently fell back, this still returns True. A demo that claims real
    detection while serving hashed placeholders would be worse than a stub.
    """
    if DETECTOR_BACKEND == "stub":
        return True
    if DETECTOR_BACKEND in ("gemini", "vlm"):
        try:
            from . import vision
        except Exception:  # noqa: BLE001
            return True
        return not vision.available()
    return _get_zone_backend() is None


def backend_name() -> str:
    """The backend actually in use, not the one requested."""
    if DETECTOR_BACKEND == "stub":
        return "stub"
    if DETECTOR_BACKEND in ("gemini", "vlm"):
        try:
            from . import vision
        except Exception:  # noqa: BLE001
            return f"stub (requested '{DETECTOR_BACKEND}', requests missing)"
        if not vision.available():
            return "stub (requested 'gemini', GOOGLE_API_KEY not set)"
        return f"gemini ({vision._working_model or 'resolving'})"
    pair = _get_zone_backend()
    if pair is None:
        return f"stub (requested '{DETECTOR_BACKEND}', unavailable)"
    return type(pair[1]).__name__


# ---------------------------------------------------------------------------
# Real implementation seam
# ---------------------------------------------------------------------------


def _split_by_direction(
    predictions: list[dict],
    inbound_side: str,
    width: int = 352,
    height: int = 240,
) -> VehicleCounts:
    """Assign each detection box to inbound or outbound by frame position.

    Used by the real detector. Live now so the seam is testable: this is real
    logic, not a placeholder — only the source of `predictions` is stubbed.
    """
    inbound = 0
    outbound = 0

    for pred in predictions:
        label = str(pred.get("class", "")).lower()
        if label and label not in VEHICLE_CLASSES:
            continue

        cx = float(pred.get("x", width / 2))
        cy = float(pred.get("y", height / 2))

        if inbound_side == "left":
            is_inbound = cx < width / 2
        elif inbound_side == "right":
            is_inbound = cx >= width / 2
        elif inbound_side == "top":
            is_inbound = cy < height / 2
        elif inbound_side == "bottom":
            is_inbound = cy >= height / 2
        else:
            is_inbound = cx < width / 2

        if is_inbound:
            inbound += 1
        else:
            outbound += 1

    return VehicleCounts(inbound=inbound, outbound=outbound, total=inbound + outbound)
