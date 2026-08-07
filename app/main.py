"""
Should I Drive to Sheepshead Bay?

A FastAPI service that reads live NYC DOT traffic cameras on the roads into
Sheepshead Bay, Brooklyn, counts vehicles inbound vs outbound, folds in NYC
parking regulations, and returns a plain-English verdict.

Runs on Google Cloud Run. Listens on $PORT (default 8080), binds 0.0.0.0.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
import os
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import FastAPI, Response
from fastapi.responses import HTMLResponse, JSONResponse

from . import config  # noqa: F401 - import for side effect: loads .env before detect
from . import agent
from . import blocks
from . import detect
from . import trend
from .cameras import (
    CAMERAS_BY_ID,
    GATEWAY_CAMERAS,
    FrameError,
    fetch_all_frames,
    fetch_frame,
)
from .verdict import NYC_TZ, CameraSignal, build_verdict

APP_TITLE = "Which street should I try? — Sheepshead Bay parking"

_client: httpx.AsyncClient | None = None


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    global _client
    _client = httpx.AsyncClient(
        timeout=httpx.Timeout(8.0),
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        follow_redirects=True,
    )
    refresher = asyncio.create_task(_refresh_loop())
    try:
        yield
    finally:
        refresher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await refresher
        await _client.aclose()
        _client = None


app = FastAPI(title=APP_TITLE, lifespan=lifespan, docs_url="/docs")

# The agent reuses the background refresh's camera reading rather than pulling
# four fresh frames per question -- same data, no added latency on the ask path.
agent.set_conditions_provider(lambda: _cache.get("payload"))


def client() -> httpx.AsyncClient:
    if _client is None:
        raise RuntimeError("HTTP client not initialized")
    return _client


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/health")
@app.get("/healthz")
async def healthz() -> JSONResponse:
    """Cloud Run health check. Must never depend on upstream NYC DOT."""
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


async def _compute_verdict() -> dict:
    """Pull every camera, read each frame, and assemble the full payload.

    The per-camera reads run concurrently. Each Gemini call takes ~5s, so doing four
    of them in a loop cost 24 seconds -- long enough that a demo audience assumes it
    crashed. Fanned out, the whole thing is bounded by the slowest single camera.
    """
    frames = await fetch_all_frames(client())

    # Fan out the vision calls before building the payload. count_vehicles() is
    # blocking (it makes an HTTPS request), so it goes to a worker thread rather
    # than stalling the event loop.
    online = [cam for cam in GATEWAY_CAMERAS if frames.get(cam.id) is not None]
    reads = await asyncio.gather(
        *(
            asyncio.to_thread(
                detect.count_vehicles,
                frames[cam.id].jpeg,
                cam.inbound_side,
                cam.id,
            )
            for cam in online
        ),
        return_exceptions=True,
    )
    counts_by_id: dict[str, dict] = {}
    for cam, result in zip(online, reads):
        if isinstance(result, Exception):
            # One camera's model call failing must not take down the verdict.
            continue
        counts_by_id[cam.id] = result

    signals: list[CameraSignal] = []
    cameras_payload: list[dict] = []

    for cam in GATEWAY_CAMERAS:
        frame = frames.get(cam.id)
        if frame is None:
            signals.append(
                CameraSignal(
                    camera_id=cam.id,
                    name=cam.name,
                    approach=cam.approach,
                    inbound=0,
                    outbound=0,
                    total=0,
                    parking_relevance=cam.parking_relevance,
                    ok=False,
                )
            )
            cameras_payload.append(
                {
                    "id": cam.id,
                    "name": cam.name,
                    "approach": cam.approach,
                    "online": False,
                    "counts": {"inbound": 0, "outbound": 0, "total": 0},
                    "frame_age_seconds": None,
                    "frame_timestamp": None,
                    "proxy_url": f"/api/frame/{cam.id}",
                    "source_url": cam.image_url,
                    "latitude": cam.latitude,
                    "longitude": cam.longitude,
                }
            )
            continue

        counts = counts_by_id.get(cam.id) or {"inbound": 0, "outbound": 0, "total": 0}
        signals.append(
            CameraSignal(
                camera_id=cam.id,
                name=cam.name,
                approach=cam.approach,
                inbound=counts["inbound"],
                outbound=counts["outbound"],
                total=counts["total"],
                parking_relevance=cam.parking_relevance,
                ok=True,
            )
        )

        age = max(0.0, datetime.now(timezone.utc).timestamp() - frame.fetched_at)
        cameras_payload.append(
            {
                "id": cam.id,
                "name": cam.name,
                "approach": cam.approach,
                "online": True,
                "counts": counts,
                "frame_age_seconds": round(age, 1),
                "frame_timestamp": datetime.fromtimestamp(
                    frame.fetched_at, tz=timezone.utc
                ).isoformat(),
                "frame_bytes": len(frame.jpeg),
                "proxy_url": f"/api/frame/{cam.id}",
                "source_url": cam.image_url,
                "latitude": cam.latitude,
                "longitude": cam.longitude,
            }
        )

    now_nyc = datetime.now(NYC_TZ)
    v = build_verdict(signals, now=now_nyc)

    return {
        "verdict": v.to_dict(),
        "cameras": cameras_payload,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "generated_at_nyc": now_nyc.isoformat(),
        "neighborhood": "Sheepshead Bay, Brooklyn, NY",
        "detector": {
            "backend": detect.backend_name(),
            "is_stub": detect.is_stub(),
            "note": (
                "Vehicle counts are PLACEHOLDER values derived from the frame "
                "hash, not computer vision. Swap detect.count_vehicles() for a "
                "real detector."
                if detect.is_stub()
                else "Gemini reads each live frame and reports flow per direction."
            ),
        },
        "privacy": "Vehicle counts only. No license plates, no faces, no frames stored.",
        "limitation": "This is a demand estimate, not a spot finder.",
        "source": "NYC DOT Traffic Management Center public cameras (webcams.nyctmc.org)",
    }


# ---------------------------------------------------------------------------
# Cached refresh
# ---------------------------------------------------------------------------
#
# A demo page must feel instant. Computing the verdict costs one round trip to four
# cameras plus four Gemini calls -- seconds, not milliseconds -- and doing that on
# the request path means every page load and every auto-refresh stalls.
#
# So a background task recomputes on a timer and requests serve the last good
# payload. The page renders immediately, the numbers are never more than
# REFRESH_SECONDS stale, and a Gemini hiccup keeps serving the previous answer
# instead of showing an error on stage.

REFRESH_SECONDS = int(os.environ.get("REFRESH_SECONDS", "25"))

_cache: dict = {"payload": None, "at": 0.0}
_cache_lock = asyncio.Lock()

# Every refresh appends one sample, so the page can show how demand moved over the
# evening rather than only the current instant. A deque bounds memory without any
# eviction logic: at 25s per sample, 2,880 samples is 20 hours of history.
_history: deque = deque(maxlen=2880)


async def _refresh_once() -> None:
    payload = await _compute_verdict()
    _cache["payload"] = payload
    _cache["at"] = datetime.now(timezone.utc).timestamp()

    v = payload["verdict"]
    _history.append({
        "t": payload["generated_at_nyc"],
        "score": v["score"],
        "net_inbound": v["net_inbound"],
        "total_vehicles": v["total_vehicles"],
        "recommendation": v["recommendation"],
        "per_camera": {
            c["name"]: {
                "inbound": c["counts"].get("inbound", 0),
                "outbound": c["counts"].get("outbound", 0),
            }
            for c in payload["cameras"] if c["online"]
        },
    })


async def _refresh_loop() -> None:
    while True:
        try:
            await _refresh_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a bad cycle must not kill the loop
            print(f"[refresh] cycle failed, serving stale: {exc!r}", flush=True)
        await asyncio.sleep(REFRESH_SECONDS)


@app.get("/api/verdict")
async def api_verdict() -> JSONResponse:
    """The verdict, per-camera counts, timestamps, and camera image URLs."""
    if _cache["payload"] is None:
        # Cold start: the background loop hasn't produced anything yet. Compute
        # under a lock so a burst of first requests triggers one pass, not N.
        async with _cache_lock:
            if _cache["payload"] is None:
                await _refresh_once()

    payload = dict(_cache["payload"])
    age = datetime.now(timezone.utc).timestamp() - _cache["at"]
    payload["cache"] = {
        "age_seconds": round(age, 1),
        "refresh_seconds": REFRESH_SECONDS,
    }
    return JSONResponse(payload)


@app.get("/api/ask")
async def api_ask(q: str = "") -> JSONResponse:
    """Ask in plain English. The agent picks its tools and writes the answer."""
    if not q.strip():
        return JSONResponse(
            {"error": "pass ?q=", "example": "/api/ask?q=I get home around 7, where do I park?"},
            status_code=400,
        )
    try:
        # The whole loop is blocking HTTP; keep it off the event loop.
        return JSONResponse(await asyncio.to_thread(agent.ask, q))
    except agent.AgentUnavailable as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)


@app.get("/api/trend")
async def api_trend(bucket_minutes: int = 5) -> JSONResponse:
    """Pressure over the evening: seed file from the tracker plus live samples."""
    bucket_minutes = max(1, min(bucket_minutes, 60))
    return JSONResponse(
        await asyncio.to_thread(trend.series, list(_history), bucket_minutes)
    )


@app.get("/api/history")
async def api_history() -> JSONResponse:
    """Every reading this instance has taken, oldest first.

    In-memory and therefore reset by any redeploy -- scripts/track.py keeps the
    durable copy on disk. This exists so the page can draw a curve without a
    database, which is the right amount of machinery for a service that has been
    running for an hour.
    """
    return JSONResponse({
        "samples": list(_history),
        "count": len(_history),
        "note": "In-memory since this instance started; a redeploy resets it.",
    })


@app.get("/api/parking")
async def api_parking(
    address: str = "",
    radius_ft: int = 1200,
    limit: int = 12,
    arriving_in: int = 0,
) -> JSONResponse:
    """Address -> the block faces around it, ranked by whether you can park there.

    `arriving_in` is minutes from now, and it changes the answers rather than just
    labelling them. Someone leaving work at 6:20 to get home at 7:00 needs the rules
    as they will be at 7:00: a block that is illegal until 7 PM is a fine spot when
    he actually arrives, and a two-hour meter that expires at 7 stops mattering.
    Evaluating "now" for a trip that happens later is simply the wrong answer.
    """
    if not address.strip():
        return JSONResponse(
            {"error": "pass ?address=", "example": "/api/parking?address=2650 E 14th St"},
            status_code=400,
        )

    radius_ft = max(200, min(radius_ft, 3000))
    limit = max(1, min(limit, 40))
    arriving_in = max(0, min(arriving_in, 720))  # up to 12 hours ahead
    when = datetime.now(NYC_TZ).replace(tzinfo=None) + timedelta(minutes=arriving_in)

    # Hand the live camera reads to the ranker so each block is scored against the
    # gateway actually feeding it, not one neighborhood-wide average.
    cached = _cache.get("payload")
    live_cameras = [
        {
            "name": c["name"],
            "lat": c["latitude"],
            "lon": c["longitude"],
            "inbound": c["counts"].get("inbound", 0),
            "outbound": c["counts"].get("outbound", 0),
            "congestion_inbound": c["counts"].get("congestion_inbound", ""),
        }
        for c in (cached or {}).get("cameras", [])
        if c.get("online")
    ]

    try:
        # Blocking HTTP (geocoder) plus a scan over 1,224 faces -- keep it off the loop.
        result = await asyncio.to_thread(
            blocks.lookup,
            address,
            when,
            radius_ft=float(radius_ft),
            limit=limit,
            cameras=live_cameras,
        )
    except blocks.GeocodeError as exc:
        return JSONResponse(
            {"error": "could not locate that address", "detail": str(exc)},
            status_code=404,
        )

    # Fold in the neighborhood demand signal, if we have one cached.
    if cached:
        result["neighborhood_demand"] = {
            "headline": cached["verdict"]["headline"],
            "score": cached["verdict"]["score"],
            "recommendation": cached["verdict"]["recommendation"],
        }

    return JSONResponse(result)


@app.get("/api/cameras")
async def api_cameras() -> JSONResponse:
    """Static config for the gateway cameras."""
    return JSONResponse(
        {
            "cameras": [
                {
                    "id": c.id,
                    "name": c.name,
                    "approach": c.approach,
                    "latitude": c.latitude,
                    "longitude": c.longitude,
                    "inbound_side": c.inbound_side,
                    "parking_relevance": c.parking_relevance,
                    "proxy_url": f"/api/frame/{c.id}",
                    "source_url": c.image_url,
                }
                for c in GATEWAY_CAMERAS
            ]
        }
    )


@app.get("/api/frame/{camera_id}")
async def api_frame(camera_id: str) -> Response:
    """Proxy the live JPEG. Avoids browser CORS / mixed-content problems."""
    if camera_id not in CAMERAS_BY_ID:
        return JSONResponse({"error": "unknown camera id"}, status_code=404)

    try:
        frame = await fetch_frame(camera_id, client())
    except FrameError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)

    return Response(
        content=frame.jpeg,
        media_type=frame.content_type,
        headers={
            "Cache-Control": "no-store, max-age=0",
            "X-Frame-Age-Seconds": str(
                round(datetime.now(timezone.utc).timestamp() - frame.fetched_at, 1)
            ),
        },
    )


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(INDEX_HTML)


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Which street should I try? — Sheepshead Bay parking</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>&#x1F697;</text></svg>">
<style>
  :root {
    --bg: #08090b;
    --panel: #111318;
    --panel-2: #171a21;
    --line: #23262f;
    --text: #f2f4f8;
    --muted: #8a919e;
    --go: #34d17f;
    --maybe: #f5b544;
    --avoid: #ff5f56;
    --accent: var(--maybe);
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI",
                 Helvetica, Arial, sans-serif;
    -webkit-font-smoothing: antialiased;
    line-height: 1.45;
    padding: 28px 20px 64px;
  }
  .wrap { max-width: 1080px; margin: 0 auto; }

  header { margin-bottom: 26px; }
  .eyebrow {
    font-size: 11px; letter-spacing: .16em; text-transform: uppercase;
    color: var(--muted); font-weight: 600;
  }
  h1 {
    font-size: 26px; margin: 6px 0 4px; font-weight: 650; letter-spacing: -.02em;
  }
  .sub { color: var(--muted); font-size: 14px; }

  .verdict {
    background: linear-gradient(160deg, var(--panel) 0%, #0c0e12 100%);
    border: 1px solid var(--line);
    border-left: 4px solid var(--accent);
    border-radius: 14px;
    padding: 30px 30px 26px;
    margin-bottom: 22px;
  }
  .verdict-head {
    font-size: clamp(34px, 6vw, 58px);
    font-weight: 700; letter-spacing: -.035em; line-height: 1.05;
    color: var(--accent);
    margin: 0 0 12px;
  }
  .verdict-sub { font-size: 17px; color: #cdd3dd; max-width: 62ch; margin: 0 0 22px; }
  .lookup { margin: 18px 0; }
  .lookup.hero { padding: 24px; border-color: #2f3c52; background: #121a26; }
  .lookup.hero .addr-row input { font-size: 19px; padding: 15px 16px; }
  .lookup.hero .addr-row button { font-size: 16px; padding: 15px 24px; }
  .addr-row select {
    flex: 0 0 auto; padding: 15px 12px; font-size: 15px; border-radius: 10px;
    border: 1px solid #2a3342; background: #0f1520; color: #e8ecf3;
  }
  .top-pick {
    margin-top: 18px; padding: 20px 22px; border-radius: 12px;
    background: linear-gradient(180deg, #16281d 0%, #12211a 100%);
    border: 1px solid #2c5138;
  }
  .top-label {
    font-size: 11px; letter-spacing: .09em; text-transform: uppercase;
    color: #6ee7a0; font-weight: 700; margin-bottom: 8px;
  }
  .top-street { font-size: 26px; font-weight: 700; line-height: 1.15; }
  .top-between { color: var(--muted); font-size: 14px; margin-top: 3px; }
  .top-why { font-size: 15px; color: #dfe6ef; margin-top: 10px; }
  .top-meta {
    display: flex; gap: 20px; flex-wrap: wrap; margin-top: 12px;
    font-size: 13px; color: var(--muted);
  }
  .trend-wrap { margin-top: 20px; border-top: 1px solid #1e2532; padding-top: 16px; }
  .trend-head {
    display: flex; justify-content: space-between; align-items: baseline;
    gap: 12px; flex-wrap: wrap; margin-bottom: 10px;
  }
  .trend-title {
    font-size: 11px; letter-spacing: .09em; text-transform: uppercase; color: var(--muted);
  }
  .trend-verdict { font-size: 14px; color: #dfe6ef; }
  .trend-note { font-size: 11px; color: var(--muted); margin-top: 8px; max-width: 78ch; }
  .ask-examples { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 12px; }
  .chip {
    font-size: 12px; padding: 6px 12px; border-radius: 999px; cursor: pointer;
    border: 1px solid #2a3342; background: #0f1520; color: #9fb0c6;
  }
  .chip:hover { border-color: #6aa9ff; color: #cfe0ff; }
  .ask-answer {
    margin-top: 18px; padding: 20px 22px; border-radius: 12px;
    background: #101827; border: 1px solid #24304a; line-height: 1.6; font-size: 15px;
  }
  .ask-answer strong { color: #fff; }
  .ask-answer ol, .ask-answer ul { margin: 8px 0 0 18px; }
  .ask-answer ul ul { margin: 4px 0 6px 16px; }
  .ask-answer ul ul li { color: #b9c4d4; font-size: 14px; margin-bottom: 3px; }
  .ask-answer .ans-head { font-weight: 600; margin: 14px 0 2px; }
  .ask-answer li { margin-bottom: 8px; }
  .ask-trace {
    margin-top: 14px; padding-top: 12px; border-top: 1px solid #1e2532;
    font-size: 11px; color: var(--muted); letter-spacing: .03em;
  }
  .ask-trace code {
    background: #0c1119; padding: 2px 7px; border-radius: 5px; color: #8fb6ff;
    margin-right: 8px; display: inline-block; margin-bottom: 3px;
  }
  .thinking { color: var(--muted); font-size: 14px; }
  .map-wrap { margin-top: 18px; }
  .map-head {
    display: flex; justify-content: space-between; align-items: baseline;
    gap: 12px; flex-wrap: wrap; margin-bottom: 8px;
  }
  .map-title {
    font-size: 11px; letter-spacing: .09em; text-transform: uppercase; color: var(--muted);
  }
  .map-legend { display: flex; gap: 14px; font-size: 11px; color: var(--muted); }
  .map-legend i { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 5px; }
  .map-svg {
    width: 100%; height: 420px; display: block; border-radius: 12px;
    background: #0c1119; border: 1px solid #1e2532;
  }
  .faces-label {
    font-size: 11px; letter-spacing: .09em; text-transform: uppercase;
    color: var(--muted); margin: 22px 0 4px;
  }
  .conditions-label {
    font-size: 11px; letter-spacing: .09em; text-transform: uppercase;
    color: var(--muted); margin-bottom: 10px;
  }
  .lookup-hint { color: var(--muted); font-size: 14px; margin: 0 0 14px; max-width: 68ch; }
  .addr-row { display: flex; gap: 10px; flex-wrap: wrap; }
  .addr-row input {
    flex: 1 1 260px; padding: 12px 14px; font-size: 16px; border-radius: 10px;
    border: 1px solid #2a3342; background: #0f1520; color: #e8ecf3;
  }
  .addr-row input:focus { outline: 2px solid var(--accent, #6aa9ff); outline-offset: 1px; }
  .addr-row button {
    padding: 12px 20px; font-size: 15px; font-weight: 600; border-radius: 10px;
    border: 0; background: #6aa9ff; color: #06101f; cursor: pointer;
  }
  .addr-row button:disabled { opacity: .55; cursor: default; }
  .addr-status { color: var(--muted); font-size: 13px; margin-top: 10px; min-height: 18px; }
  .face {
    display: flex; gap: 14px; align-items: baseline; padding: 12px 0;
    border-bottom: 1px solid #1e2532;
  }
  .face:last-child { border-bottom: 0; }
  .pill {
    flex: 0 0 auto; font-size: 11px; font-weight: 700; letter-spacing: .04em;
    text-transform: uppercase; padding: 4px 9px; border-radius: 999px;
  }
  .pill.good  { background: #15351f; color: #6ee7a0; }
  .pill.fair  { background: #3a3216; color: #f5d67b; }
  .pill.tight { background: #3a2a16; color: #f0b37b; }
  .pill.no    { background: #3a1b1b; color: #ff9d9d; }
  .face-main { flex: 1 1 auto; min-width: 0; }
  .face-street { font-weight: 600; font-size: 15px; }
  .face-where { color: var(--muted); font-size: 13px; margin-top: 2px; }
  .face-why { font-size: 13px; margin-top: 4px; color: #cdd3dd; }
  .face-feed { font-size: 12px; margin-top: 3px; color: var(--muted); }
  .face-sign { margin-top: 6px; }
  .face-sign code {
    display: block; font-size: 11px; line-height: 1.5; color: #7d8da0;
    background: #0c1119; border: 1px solid #1b2331; border-radius: 6px;
    padding: 5px 9px; margin-bottom: 4px; word-break: break-word;
  }
  .face-dist { flex: 0 0 auto; color: var(--muted); font-size: 13px; text-align: right; }
  @media (max-width: 620px) {
    .face { flex-wrap: wrap; }
    .face-dist { text-align: left; }
  }

  .stats { display: flex; flex-wrap: wrap; gap: 34px; padding-top: 20px;
           border-top: 1px solid var(--line); }
  .stat-label {
    font-size: 10px; letter-spacing: .14em; text-transform: uppercase;
    color: var(--muted); font-weight: 600; margin-bottom: 4px;
  }
  .stat-val {
    font-size: 38px; font-weight: 680; letter-spacing: -.03em;
    font-variant-numeric: tabular-nums; line-height: 1;
  }
  .stat-val.accent { color: var(--accent); }

  .cols { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; margin-bottom: 22px; }
  @media (max-width: 780px) { .cols { grid-template-columns: 1fr; } }

  .card {
    background: var(--panel); border: 1px solid var(--line);
    border-radius: 12px; padding: 18px 20px;
  }
  .card h2 {
    font-size: 11px; letter-spacing: .14em; text-transform: uppercase;
    color: var(--muted); font-weight: 600; margin: 0 0 12px;
  }
  .card ul { margin: 0; padding-left: 18px; }
  .card li { font-size: 14px; color: #c3c9d4; margin-bottom: 7px; }
  .card li::marker { color: var(--accent); }

  .cams { display: grid; grid-template-columns: repeat(2, 1fr); gap: 16px; }
  @media (max-width: 780px) { .cams { grid-template-columns: 1fr; } }

  .cam {
    background: var(--panel); border: 1px solid var(--line);
    border-radius: 12px; overflow: hidden;
  }
  .cam-img-wrap { position: relative; background: #000; aspect-ratio: 352 / 240; }
  .cam-img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .cam-badge {
    position: absolute; top: 10px; right: 10px;
    background: rgba(8,9,11,.82); backdrop-filter: blur(6px);
    border: 1px solid var(--line); border-radius: 999px;
    padding: 4px 11px; font-size: 12px; font-weight: 650;
    font-variant-numeric: tabular-nums;
  }
  .cam-offline {
    position: absolute; inset: 0; display: flex; align-items: center;
    justify-content: center; color: var(--muted); font-size: 13px;
    background: #0b0c0f;
  }
  .cam-body { padding: 13px 15px 15px; }
  .cam-name { font-size: 14px; font-weight: 620; margin-bottom: 2px; }
  .cam-approach { font-size: 12px; color: var(--muted); margin-bottom: 11px; }
  .flow { display: flex; gap: 18px; }
  .flow-item { flex: 1; }
  .flow-label {
    font-size: 9px; letter-spacing: .13em; text-transform: uppercase;
    color: var(--muted); font-weight: 650; margin-bottom: 2px;
  }
  .flow-val { font-size: 24px; font-weight: 680; font-variant-numeric: tabular-nums;
              letter-spacing: -.02em; line-height: 1.1; }
  .flow-in { color: var(--avoid); }
  .flow-out { color: var(--go); }

  footer {
    margin-top: 30px; padding-top: 20px; border-top: 1px solid var(--line);
    font-size: 12.5px; color: var(--muted); display: grid; gap: 7px;
  }
  footer strong { color: #b8bfcb; font-weight: 620; }
  .live {
    display: inline-flex; align-items: center; gap: 7px;
    font-size: 12px; color: var(--muted);
  }
  .dot {
    width: 7px; height: 7px; border-radius: 50%; background: var(--go);
    animation: pulse 2s ease-in-out infinite;
  }
  @keyframes pulse { 0%,100% { opacity:1 } 50% { opacity:.25 } }
  .stub-flag {
    display: inline-block; margin-top: 10px; font-size: 11px; font-weight: 640;
    letter-spacing: .06em; text-transform: uppercase;
    color: #ffca6b; background: rgba(245,181,68,.11);
    border: 1px solid rgba(245,181,68,.3); border-radius: 6px; padding: 5px 10px;
  }
</style>
</head>
<body>
<div class="wrap">

  <header>
    <div class="eyebrow">NYC Vision Hack &middot; Sheepshead Bay, Brooklyn</div>
    <h1>Which street should I try?</h1>
    <div class="sub">
      Ask about any address in Sheepshead Bay. It reads the posted DOT signs on every
      block around it and tells you where you're allowed to leave the car, and until when.
      <span class="live"><span class="dot"></span><span id="tick">connecting&hellip;</span></span>
    </div>
  </header>

  <section class="card lookup hero">
    <form id="ask-form" class="addr-row" autocomplete="off">
      <input id="ask" type="text" aria-label="Ask a question"
             placeholder="I get home to 1822 Avenue X around 7pm — where should I park?" />
      <button type="submit">Ask</button>
    </form>
    <div class="ask-examples">
      <button type="button" class="chip" data-q="I get home to 1822 Avenue X around 7pm, where should I park?">arriving at 7pm</button>
      <button type="button" class="chip" data-q="Is it worth driving to 2650 E 14th St right now or should I wait an hour?">now or wait an hour?</button>
      <button type="button" class="chip" data-q="What happens to parking near 1822 Avenue X on August 15th?">what about Aug 15?</button>
    </div>
    <div id="ask-answer"></div>
  </section>

  <section class="card lookup">
    <div class="lookup-hint">Or look up an address directly.</div>
    <form id="addr-form" class="addr-row" autocomplete="off">
      <input id="addr" type="text" placeholder="2650 E 14th St" aria-label="Address" />
      <select id="arriving" aria-label="When are you arriving">
        <option value="0">arriving now</option>
        <option value="15">in 15 min</option>
        <option value="30">in 30 min</option>
        <option value="60">in 1 hour</option>
        <option value="120">in 2 hours</option>
      </select>
      <button type="submit">Find me a block</button>
    </form>
    <div id="addr-status" class="addr-status"></div>
    <div id="top-pick"></div>
    <div id="map"></div>
    <div id="faces"></div>
  </section>

  <section class="verdict">
    <div class="conditions-label">Conditions right now &mdash; these apply to every block above</div>
    <h2 class="verdict-head" id="headline">Reading the cameras&hellip;</h2>
    <p class="verdict-sub" id="subtext">Pulling live frames from the NYC DOT Traffic Management Center.</p>
    <div class="stats">
      <div>
        <div class="stat-label">Parking pressure</div>
        <div class="stat-val accent"><span id="score">--</span><span style="font-size:20px;color:var(--muted)">/100</span></div>
      </div>
      <div>
        <div class="stat-label">Net inbound</div>
        <div class="stat-val" id="net">--</div>
      </div>
      <div>
        <div class="stat-label">Trend</div>
        <div class="stat-val" id="trend">--</div>
      </div>
      <div>
        <div class="stat-label">Cameras live</div>
        <div class="stat-val" id="live-cams">--</div>
      </div>
    </div>
    <div id="stub-flag"></div>
    <div id="trend-chart" class="trend-wrap"></div>
  </section>

  <div class="cols">
    <div class="card">
      <h2>What the cameras show</h2>
      <ul id="reasons"><li>&hellip;</li></ul>
    </div>
    <div class="card">
      <h2>NYC parking rules in effect</h2>
      <ul id="regs"><li>&hellip;</li></ul>
    </div>
  </div>

  <div class="cams" id="cams"></div>

  <footer>
    <div><strong>Privacy:</strong> vehicle counts only. No license plates, no faces, no frames stored.</div>
    <div><strong>Honest limitation:</strong> this tells you where parking is <em>legal</em> and for how
      long. It cannot tell you a space is empty &mdash; no public feed publishes that. Every block listed
      already has cars on it.</div>
    <div>Source: NYC DOT Traffic Management Center public cameras (webcams.nyctmc.org). Refreshes every 5s.</div>
  </footer>

</div>

<script>
const ACCENT = { go: "var(--go)", maybe: "var(--maybe)", avoid: "var(--avoid)" };

function esc(s) {
  return String(s).replace(/[&<>"']/g, c => (
    {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]
  ));
}

function renderCameras(cams) {
  const host = document.getElementById("cams");
  const bust = Date.now();
  host.innerHTML = cams.map(c => {
    const img = c.online
      ? `<img class="cam-img" src="${esc(c.proxy_url)}?t=${bust}" alt="${esc(c.name)}">`
      : `<div class="cam-offline">camera offline</div>`;
    return `
      <div class="cam">
        <div class="cam-img-wrap">
          ${img}
          <div class="cam-badge">${c.counts.total} veh</div>
        </div>
        <div class="cam-body">
          <div class="cam-name">${esc(c.name)}</div>
          <div class="cam-approach">${esc(c.approach)}</div>
          <div class="flow">
            <div class="flow-item">
              <div class="flow-label">Inbound</div>
              <div class="flow-val flow-in">${c.counts.inbound}</div>
            </div>
            <div class="flow-item">
              <div class="flow-label">Outbound</div>
              <div class="flow-val flow-out">${c.counts.outbound}</div>
            </div>
          </div>
        </div>
      </div>`;
  }).join("");
}

async function refresh() {
  try {
    const r = await fetch("/api/verdict", { cache: "no-store" });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const d = await r.json();
    const v = d.verdict;

    document.documentElement.style.setProperty(
      "--accent", ACCENT[v.recommendation] || "var(--maybe)");

    document.getElementById("headline").textContent = v.headline;
    document.getElementById("subtext").textContent = v.subtext;
    document.getElementById("score").textContent = Math.round(v.score);
    document.getElementById("net").textContent =
      (v.net_inbound > 0 ? "+" : "") + v.net_inbound;
    document.getElementById("live-cams").textContent =
      v.cameras_reporting + "/" + d.cameras.length;
    updateTrend();
    drawTrendChart();

    document.getElementById("reasons").innerHTML =
      v.reasons.map(x => `<li>${esc(x)}</li>`).join("");
    document.getElementById("regs").innerHTML =
      v.regulations.map(x => `<li>${esc(x)}</li>`).join("");

    document.getElementById("stub-flag").innerHTML = d.detector.is_stub
      ? `<span class="stub-flag">Stub detector &mdash; counts are placeholder, not CV</span>`
      : "";

    renderCameras(d.cameras);

    const t = new Date(d.generated_at_utc);
    document.getElementById("tick").textContent =
      "updated " + t.toLocaleTimeString();
  } catch (e) {
    document.getElementById("tick").textContent = "reconnecting\\u2026";
  }
}

// --- Trend: is the neighborhood filling up or emptying out? ---
//
// One instant reading cannot tell you whether to hurry. The direction of travel
// over the last stretch can: pressure climbing means go now, falling means the
// curb is opening up.

let trendState = { label: "—", detail: "" };

// Inline SVG rather than a chart library: no CDN, no build step, and the whole
// thing is a dozen rects. Bars are net inbound per window -- above the zero line
// means more cars arriving than leaving.
async function drawTrendChart() {
  const el = document.getElementById("trend-chart");
  if (!el) return;
  try {
    const r = await fetch("/api/trend", { cache: "no-store" });
    const d = await r.json();
    const b = d.buckets || [];
    if (b.length < 2) { el.innerHTML = ""; return; }

    const W = 100, H = 34, gap = 0.6;
    const vals = b.map(x => x.net_inbound);
    const peak = Math.max(4, ...vals.map(Math.abs));
    const bw = W / b.length;

    const bars = b.map(function (x, i) {
      const h = Math.abs(x.net_inbound) / peak * (H / 2);
      const up = x.net_inbound >= 0;
      const y = up ? (H / 2 - h) : (H / 2);
      const fill = up ? "#ff8f6b" : "#6ee7a0";
      return '<rect x="' + (i * bw + gap).toFixed(2) + '" y="' + y.toFixed(2) +
             '" width="' + (bw - gap * 2).toFixed(2) + '" height="' + Math.max(h, 0.6).toFixed(2) +
             '" fill="' + fill + '" opacity="0.9"><title>' +
             esc(x.label) + ' — net ' + (x.net_inbound > 0 ? "+" : "") + x.net_inbound +
             ' (' + x.readings + ' readings)</title></rect>';
    }).join("");

    const s = d.summary || {};
    el.innerHTML =
      '<div class="trend-head">' +
        '<span class="trend-title">Pressure since we started watching</span>' +
        '<span class="trend-verdict">' +
          esc(s.from_label || "") + ' → ' + esc(s.to_label || "") + ': <strong>' +
          esc(s.direction || "—") + '</strong>' +
          (s.readings ? ' · ' + esc(String(s.readings)) + ' readings' : '') +
        '</span>' +
      '</div>' +
      '<svg viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none" ' +
        'style="width:100%;height:56px;display:block">' +
        '<line x1="0" y1="' + (H / 2) + '" x2="' + W + '" y2="' + (H / 2) +
          '" stroke="#2a3342" stroke-width="0.3"/>' +
        bars +
      '</svg>' +
      '<div class="trend-note">' + esc(d.measures || "") + '</div>';
  } catch (e) {
    // A missing chart must never break the page.
  }
}

async function updateTrend() {
  try {
    const r = await fetch("/api/history", { cache: "no-store" });
    const d = await r.json();
    const s = d.samples || [];
    const el = document.getElementById("trend");

    if (s.length < 4) {
      el.textContent = "—";
      el.title = "Building history (" + s.length + " readings so far)";
      return;
    }

    // Compare the most recent quarter against the oldest quarter.
    const n = Math.max(2, Math.floor(s.length / 4));
    const avg = a => a.reduce((x, y) => x + y.score, 0) / a.length;
    const early = avg(s.slice(0, n));
    const late  = avg(s.slice(-n));
    const delta = late - early;

    let label;
    if (delta > 4)       label = "↑ filling";
    else if (delta < -4) label = "↓ easing";
    else                 label = "→ steady";

    el.textContent = label;
    el.title = "Pressure " + (delta >= 0 ? "+" : "") + delta.toFixed(1) +
               " over " + s.length + " readings";
    trendState = { label: label, detail: el.title };
  } catch (e) {
    // A missing trend must never break the page.
  }
}

// --- Ask: natural language in, the agent picks its own tools ---
//
// The trace is shown on purpose. A parking recommendation you cannot audit is worth
// very little, and listing which tools ran is the difference between an answer and a
// claim.

const askForm   = document.getElementById("ask-form");
const askInput  = document.getElementById("ask");
const askAnswer = document.getElementById("ask-answer");

// The model replies in light markdown. Rendering it by hand keeps the page free of
// any external library, which matters when the venue wifi is the weak link.
function miniMarkdown(text) {
  const lines = esc(text).split(String.fromCharCode(10));
  let html = "";
  let depth = 0;   // how many <ul> are currently open
  for (const raw of lines) {
    const line = raw.trim();
    if (!line) continue;

    const item = line.match(/^(?:[-*]|\d+\.)\s+(.*)$/);
    if (item) {
      // Indentation in the source decides nesting. The model emits sub-points
      // indented under a heading item; rendering every bullet at one level turned
      // "street / distance / rule" into three sibling bullets and made the answer
      // read as an undifferentiated list.
      const want = Math.min(1 + Math.floor((raw.match(/^\s*/)[0].length) / 2), 2);
      while (depth < want) { html += "<ul>"; depth++; }
      while (depth > want) { html += "</ul>"; depth--; }
      html += "<li>" + item[1] + "</li>";
      continue;
    }

    while (depth > 0) { html += "</ul>"; depth--; }
    // A short bold-only line is a heading, not a paragraph.
    if (/^\*\*[^*]+\*\*:?$/.test(line) || /^#{1,4}\s/.test(line)) {
      html += "<p class='ans-head'>" + line.replace(/^#{1,4}\s*/, "") + "</p>";
    } else {
      html += "<p>" + line + "</p>";
    }
  }
  while (depth > 0) { html += "</ul>"; depth--; }
  return html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
}

async function askAgent(question) {
  if (!question) return;
  const button = askForm.querySelector("button");
  button.disabled = true;
  askAnswer.className = "ask-answer";
  askAnswer.innerHTML = '<div class="thinking">Reading the signs and the cameras&hellip;</div>';

  try {
    const r = await fetch("/api/ask?q=" + encodeURIComponent(question), { cache: "no-store" });
    const d = await r.json();
    if (!r.ok) {
      askAnswer.innerHTML = '<div class="thinking">' + esc(d.error || "That did not work.") + '</div>';
      return;
    }
    const tools = (d.trace || []).map(t => "<code>" + esc(t.tool) + "</code>").join("");
    askAnswer.innerHTML =
      miniMarkdown(d.answer) +
      '<div class="ask-trace">Tools used: ' + (tools || "none") +
        ' &middot; ' + esc(d.model || "") + '</div>';
  } catch (e) {
    askAnswer.innerHTML = '<div class="thinking">Request failed: ' + esc(e.message) + '</div>';
  } finally {
    button.disabled = false;
  }
}

askForm.addEventListener("submit", function (ev) {
  ev.preventDefault();
  askAgent(askInput.value.trim());
});

document.querySelectorAll(".chip").forEach(function (chip) {
  chip.addEventListener("click", function () {
    askInput.value = chip.dataset.q;
    askAgent(chip.dataset.q);
  });
});

// --- Map: where these blocks actually are, relative to the address ---
//
// Hand-drawn SVG rather than a tile map. The API returns each block face as an
// offset in feet east/north of the address, and NY State Plane is already a flat
// grid in feet, so plotting is a subtraction and a scale -- no projection, no tile
// server, no external library, nothing to fail on a venue wifi at 8:45 PM.

const PILL_COLOR = { good: "#6ee7a0", fair: "#f5d67b", tight: "#f0b37b", no: "#ff9d9d" };

function renderMap(d) {
  const el = document.getElementById("map");
  const faces = (d.block_faces || []).filter(b => b.dx_ft !== undefined);
  if (!faces.length) { el.innerHTML = ""; return; }

  const W = 720, H = 440, pad = 34;
  const reach = Math.max(300, ...faces.map(b => Math.max(Math.abs(b.dx_ft), Math.abs(b.dy_ft))));
  // Vertical is the tighter axis, so it sets the scale; the extra horizontal room
  // just means the map breathes rather than distorting the geometry.
  const scale = (H / 2 - pad) / reach;

  // East is +x on screen; north is +y in state plane but -y in SVG, so flip it.
  const px = ft => (W / 2 + ft * scale);
  const py = ft => (H / 2 - ft * scale);

  // Distance rings, so "2 min walk" has a visual scale attached.
  const rings = [500, 1000, 1500]
    .filter(r => r * scale < Math.min(W, H) / 2)
    .map(r =>
      '<circle cx="' + (W/2) + '" cy="' + (H/2) + '" r="' + (r * scale).toFixed(1) + '" ' +
        'fill="none" stroke="#1c2431" stroke-width="1"/>' +
      '<text x="' + (W/2 + r * scale - 4) + '" y="' + (H/2 - 5) + '" fill="#3d4757" ' +
        'font-size="9" text-anchor="end">' + r + ' ft</text>'
    ).join("");

  const dots = faces.map(function (b, i) {
    const x = px(b.dx_ft), y = py(b.dy_ft);
    const isTop = i === 0;
    const c = PILL_COLOR[b.confidence] || "#8aa";
    const r = isTop ? 9 : 6;
    return '<g>' +
      (isTop ? '<circle cx="' + x + '" cy="' + y + '" r="15" fill="' + c + '" opacity="0.16"/>' : '') +
      '<circle cx="' + x + '" cy="' + y + '" r="' + r + '" fill="' + c + '" ' +
        'stroke="#0c1119" stroke-width="2">' +
        '<title>' + esc(b.street) + ' — ' + esc(b.side) + ' · ' + esc(b.reason) +
        ' · ' + b.walk_minutes + ' min walk</title>' +
      '</circle>' +
      (isTop
        ? '<text x="' + x + '" y="' + (y - 20) + '" fill="#e8ecf3" font-size="12" ' +
          'font-weight="700" text-anchor="middle">' + esc(b.street) + '</text>'
        : '') +
    '</g>';
  }).join("");

  // The address itself.
  const pin =
    '<circle cx="' + (W/2) + '" cy="' + (H/2) + '" r="5" fill="#6aa9ff"/>' +
    '<circle cx="' + (W/2) + '" cy="' + (H/2) + '" r="11" fill="none" stroke="#6aa9ff" stroke-width="1.5" opacity="0.6"/>' +
    '<text x="' + (W/2) + '" y="' + (H/2 + 26) + '" fill="#6aa9ff" font-size="11" ' +
      'text-anchor="middle">you</text>';

  el.className = "map-wrap";
  el.innerHTML =
    '<div class="map-head">' +
      '<span class="map-title">Where these blocks are</span>' +
      '<span class="map-legend">' +
        '<span><i style="background:#6ee7a0"></i>good</span>' +
        '<span><i style="background:#f5d67b"></i>fair</span>' +
        '<span><i style="background:#f0b37b"></i>tight</span>' +
        '<span><i style="background:#6aa9ff"></i>your address</span>' +
      '</span>' +
    '</div>' +
    '<svg class="map-svg" viewBox="0 0 ' + W + ' ' + H + '">' +
      '<text x="' + (W/2) + '" y="16" fill="#3d4757" font-size="10" text-anchor="middle">N</text>' +
      rings + dots + pin +
    '</svg>';
}

// --- Address lookup: which block faces near me can I actually park on? ---

const addrForm   = document.getElementById("addr-form");
const addrInput  = document.getElementById("addr");
const addrStatus = document.getElementById("addr-status");
const facesEl    = document.getElementById("faces");

function renderFaces(d) {
  const topEl = document.getElementById("top-pick");
  topEl.innerHTML = "";
  document.getElementById("map").innerHTML = "";

  if (!d.in_coverage) {
    facesEl.innerHTML = "";
    addrStatus.textContent = d.coverage_note || "Outside the covered area.";
    return;
  }
  if (!d.block_faces.length) {
    facesEl.innerHTML = "";
    addrStatus.textContent = "No block faces with posted signs within range. Try a wider radius.";
    return;
  }

  const rec = d.recommendation;
  if (rec) {
    topEl.innerHTML =
      '<div class="top-pick">' +
        '<div class="top-label">Go here first</div>' +
        '<div class="top-street">' + esc(rec.street) + ' — ' + esc(rec.side) + '</div>' +
        '<div class="top-between">' + esc(rec.between || "") + '</div>' +
        '<div class="top-why">' + esc(rec.why) + '</div>' +
        '<div class="top-meta">' +
          '<span><strong>' + esc(String(rec.walk_minutes)) + ' min</strong> walk · ' +
            esc(String(rec.distance_ft)) + ' ft</span>' +
          (rec.backup ? '<span>Backup: ' + esc(rec.backup) + '</span>' : '') +
        '</div>' +
      '</div>';
  }

  const asp = d.asp || {};
  const aspLine = asp.in_effect
    ? "Alternate side is in effect — next sweep " + esc(asp.next_sweep_human || "soon") +
      ", so those block faces will churn."
    : "Alternate side is SUSPENDED (" + esc(asp.suspended_reason || "holiday") +
      ") — nobody has to move, so the curb stays as full as it is.";

  addrStatus.innerHTML =
    "Matched <strong>" + esc(d.location.matched) + "</strong> via " + esc(d.location.source) +
    ". " + aspLine;

  renderMap(d);

  facesEl.innerHTML = '<div class="faces-label">Backups, in order</div>' +
    d.block_faces.slice(1).map(function (b) {
    const when = b.legal_now
      ? (b.until_human ? "until " + esc(b.until_human) : "no posted limit")
      : "not now";
    const bits = [];
    if (b.approx_spaces) bits.push("~" + b.approx_spaces + " spaces");
    if (b.pressure) {
      bits.push(b.pressure.complaints_6mo + " parking complaints in 6mo (" +
                esc(b.pressure.level) + ")");
    }
    const feedLine = bits.length
      ? '<div class="face-feed">' + bits.join(" &middot; ") + '</div>'
      : "";
    // The actual posted sign text. This is the thing the whole project exists to
    // translate, and showing it is what makes the answer above it believable.
    const signLine = (b.rules && b.rules.length)
      ? '<div class="face-sign">' +
          b.rules.slice(0, 2).map(r => '<code>' + esc(r) + '</code>').join("") +
        '</div>'
      : "";
    return '' +
      '<div class="face">' +
        '<span class="pill ' + esc(b.confidence) + '">' + esc(b.confidence) + '</span>' +
        '<div class="face-main">' +
          '<div class="face-street">' + esc(b.street) + ' — ' + esc(b.side) + '</div>' +
          '<div class="face-where">' + esc(b.between || "") + '</div>' +
          '<div class="face-why">' + esc(b.reason) + '</div>' +
          feedLine + signLine +
        '</div>' +
        '<div class="face-dist">' +
          esc(String(b.walk_minutes)) + ' min walk<br>' +
          '<span style="opacity:.7">' + esc(String(b.distance_ft)) + ' ft · ' + when + '</span>' +
        '</div>' +
      '</div>';
  }).join("");
}

async function lookupAddress(ev) {
  if (ev) ev.preventDefault();
  const address = addrInput.value.trim();
  if (!address) return;

  const button = addrForm.querySelector("button");
  button.disabled = true;
  addrStatus.textContent = "Locating …";
  facesEl.innerHTML = "";

  try {
    const mins = document.getElementById("arriving").value || "0";
    const r = await fetch(
      "/api/parking?address=" + encodeURIComponent(address) + "&arriving_in=" + mins,
      { cache: "no-store" });
    const d = await r.json();
    if (!r.ok) {
      addrStatus.textContent = d.error || "Could not look that up.";
      return;
    }
    renderFaces(d);
  } catch (e) {
    addrStatus.textContent = "Lookup failed: " + e.message;
  } finally {
    button.disabled = false;
  }
}

addrForm.addEventListener("submit", lookupAddress);

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
    )
