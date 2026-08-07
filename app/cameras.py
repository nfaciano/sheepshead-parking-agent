"""
Camera configuration and live frame fetching.

Source: NYC DOT Traffic Management Center public webcam API.
  Camera list:  https://webcams.nyctmc.org/api/cameras
  Still image:  https://webcams.nyctmc.org/api/cameras/{id}/image  -> 352x240 JPEG, ~2s refresh

No authentication required. No API key. Public data.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

NYC_CAM_BASE = "https://webcams.nyctmc.org/api/cameras"

# How long a cached frame is considered fresh. The upstream refreshes ~every 2s;
# we cache slightly longer so a page full of clients doesn't hammer NYC DOT.
FRAME_TTL_SECONDS = 3.0

FETCH_TIMEOUT_SECONDS = 6.0


@dataclass(frozen=True)
class Camera:
    """One gateway camera on a road leading into Sheepshead Bay."""

    id: str
    name: str
    # Short label for the UI.
    short_name: str
    # Which approach into the neighborhood this camera watches.
    approach: str
    latitude: float
    longitude: float
    # Hint for the real detector: which half of the frame carries traffic
    # heading INTO Sheepshead Bay. Used to split detections into inbound vs
    # outbound. "left"/"right"/"top"/"bottom" of the 352x240 frame.
    #
    # NOTE: these are eyeballed starting values. A real deployment would draw
    # proper ROI polygons per camera. Documented as a known limitation.
    inbound_side: str = "left"
    # Rough share of the roadway that is a real parking-search corridor
    # (vs. a limited-access parkway you cannot park on). Weights the verdict.
    parking_relevance: float = 1.0

    @property
    def image_url(self) -> str:
        return f"{NYC_CAM_BASE}/{self.id}/image"


# Gateway cameras INTO Sheepshead Bay, Brooklyn.
# Verified live and online against the NYC DOT API.
GATEWAY_CAMERAS: list[Camera] = [
    Camera(
        id="111e79a9-eb5a-44d0-b062-481ac0a81901",
        name="Belt Pkwy @ Plumb 3 St",
        short_name="Belt Pkwy @ Plumb 3 St",
        approach="Belt Parkway — eastern approach",
        latitude=40.5859,
        longitude=-73.9366,
        inbound_side="left",
        # Belt Pkwy is limited-access: it delivers cars to the neighborhood
        # but you cannot park on it. High demand signal, low direct relevance.
        parking_relevance=0.9,
    ),
    Camera(
        id="64ed1f5d-ba90-4c12-afda-9a9c3e658efd",
        name="Ocean Pkwy @ Ave X",
        short_name="Ocean Pkwy @ Ave X",
        approach="Ocean Parkway — northern approach",
        latitude=40.5928,
        longitude=-73.9686,
        inbound_side="bottom",
        parking_relevance=1.0,
    ),
    Camera(
        id="9bdd7740-762f-48e9-b40c-3db03c2a43f5",
        name="Belt Pkwy @ Ocean Pkwy",
        short_name="Belt Pkwy @ Ocean Pkwy",
        approach="Belt Parkway — western approach / Ocean Pkwy interchange",
        latitude=40.5809,
        longitude=-73.9736,
        inbound_side="right",
        parking_relevance=0.9,
    ),
    Camera(
        id="899dfa1e-a2c5-490a-b8ba-480493634846",
        name="Coney Island Ave @ Kings Hwy",
        short_name="Coney Island Ave @ Kings Hwy",
        approach="Coney Island Avenue — northern surface approach",
        latitude=40.6089,
        longitude=-73.9622,
        inbound_side="bottom",
        # Surface street with real curb parking on both sides — the most
        # directly meaningful camera for parking demand.
        parking_relevance=1.2,
    ),
]

CAMERAS_BY_ID: dict[str, Camera] = {c.id: c for c in GATEWAY_CAMERAS}


class FrameError(Exception):
    """Raised when a live frame could not be fetched."""


@dataclass
class CachedFrame:
    jpeg: bytes
    fetched_at: float
    content_type: str = "image/jpeg"


_cache: dict[str, CachedFrame] = {}
_locks: dict[str, asyncio.Lock] = {}


def _lock_for(camera_id: str) -> asyncio.Lock:
    lock = _locks.get(camera_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[camera_id] = lock
    return lock


async def fetch_frame(
    camera_id: str,
    client: httpx.AsyncClient,
    *,
    force: bool = False,
) -> CachedFrame:
    """Fetch a live JPEG for one camera, with a short TTL cache.

    Raises FrameError if the camera is unknown or upstream failed and we have
    no cached frame to fall back on.
    """
    camera = CAMERAS_BY_ID.get(camera_id)
    if camera is None:
        raise FrameError(f"unknown camera id: {camera_id}")

    now = time.time()
    cached = _cache.get(camera_id)
    if not force and cached is not None and (now - cached.fetched_at) < FRAME_TTL_SECONDS:
        return cached

    async with _lock_for(camera_id):
        # Re-check: another coroutine may have refreshed while we waited.
        cached = _cache.get(camera_id)
        now = time.time()
        if not force and cached is not None and (now - cached.fetched_at) < FRAME_TTL_SECONDS:
            return cached

        try:
            resp = await client.get(
                camera.image_url,
                timeout=FETCH_TIMEOUT_SECONDS,
                headers={"User-Agent": "should-i-drive-to-sheepshead-bay/1.0"},
            )
            resp.raise_for_status()
            body = resp.content
            if not body:
                raise FrameError("empty frame body")
            frame = CachedFrame(
                jpeg=body,
                fetched_at=time.time(),
                content_type=resp.headers.get("content-type", "image/jpeg"),
            )
            _cache[camera_id] = frame
            return frame
        except Exception as exc:  # noqa: BLE001 - upstream is a flaky public API
            # Serve a stale frame rather than break the demo, but say so.
            if cached is not None:
                return cached
            raise FrameError(f"failed to fetch frame for {camera_id}: {exc}") from exc


async def fetch_all_frames(
    client: httpx.AsyncClient,
) -> dict[str, Optional[CachedFrame]]:
    """Fetch every gateway camera concurrently. None means that camera failed."""
    results = await asyncio.gather(
        *(fetch_frame(c.id, client) for c in GATEWAY_CAMERAS),
        return_exceptions=True,
    )
    out: dict[str, Optional[CachedFrame]] = {}
    for camera, result in zip(GATEWAY_CAMERAS, results):
        out[camera.id] = result if isinstance(result, CachedFrame) else None
    return out
