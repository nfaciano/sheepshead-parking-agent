"""
Should I Drive to Sheepshead Bay?

A FastAPI service that reads live NYC DOT traffic cameras on the roads into
Sheepshead Bay, Brooklyn, counts vehicles inbound vs outbound, folds in NYC
parking regulations, and returns a plain-English verdict.

Runs on Google Cloud Run. Listens on $PORT (default 8080), binds 0.0.0.0.
"""

from __future__ import annotations

import contextlib
import os
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Response
from fastapi.responses import HTMLResponse, JSONResponse

from . import detect
from .cameras import (
    CAMERAS_BY_ID,
    GATEWAY_CAMERAS,
    FrameError,
    fetch_all_frames,
    fetch_frame,
)
from .verdict import NYC_TZ, CameraSignal, build_verdict

APP_TITLE = "Should I Drive to Sheepshead Bay?"

_client: httpx.AsyncClient | None = None


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    global _client
    _client = httpx.AsyncClient(
        timeout=httpx.Timeout(8.0),
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        follow_redirects=True,
    )
    try:
        yield
    finally:
        await _client.aclose()
        _client = None


app = FastAPI(title=APP_TITLE, lifespan=lifespan, docs_url="/docs")


def client() -> httpx.AsyncClient:
    if _client is None:
        raise RuntimeError("HTTP client not initialized")
    return _client


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/healthz")
async def healthz() -> JSONResponse:
    """Cloud Run health check. Must never depend on upstream NYC DOT."""
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@app.get("/api/verdict")
async def api_verdict() -> JSONResponse:
    """The verdict, per-camera counts, timestamps, and camera image URLs."""
    frames = await fetch_all_frames(client())

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

        counts = detect.count_vehicles(
            frame.jpeg,
            inbound_side=cam.inbound_side,
            camera_id=cam.id,
        )
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

    return JSONResponse(
        {
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
                    else "Live vehicle detection."
                ),
            },
            "privacy": "Vehicle counts only. No license plates, no faces, no frames stored.",
            "limitation": "This is a demand estimate, not a spot finder.",
            "source": "NYC DOT Traffic Management Center public cameras (webcams.nyctmc.org)",
        }
    )


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
<title>Should I Drive to Sheepshead Bay?</title>
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
    <div class="eyebrow">NYC Vision Hack &middot; live NYC DOT traffic cameras</div>
    <h1>Should I Drive to Sheepshead Bay?</h1>
    <div class="sub">
      Counting vehicles on the four roads into the neighborhood, right now.
      <span class="live"><span class="dot"></span><span id="tick">connecting&hellip;</span></span>
    </div>
  </header>

  <section class="verdict">
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
        <div class="stat-label">Vehicles in frame</div>
        <div class="stat-val" id="total">--</div>
      </div>
      <div>
        <div class="stat-label">Cameras live</div>
        <div class="stat-val" id="live-cams">--</div>
      </div>
    </div>
    <div id="stub-flag"></div>
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
    <div><strong>Honest limitation:</strong> this is a demand estimate, not a spot finder. It tells you
      how hard the search will be, not where an open space is.</div>
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
    document.getElementById("total").textContent = v.total_vehicles;
    document.getElementById("live-cams").textContent =
      v.cameras_reporting + "/" + d.cameras.length;

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
