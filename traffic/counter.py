"""
Sheepshead Bay traffic demand signal — vehicle counting on NYC DOT cameras.
PRIVACY: counts + boxes only. No plates, no faces, no crops persisted.
"""
from __future__ import annotations
import io, os, time, base64, math
from typing import Dict, List, Tuple
import requests
from PIL import Image

CAM_URL = "https://webcams.nyctmc.org/api/cameras/{cam_id}/image"

# COCO vehicle class ids -> name
COCO_VEHICLE = {2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}
# Roboflow/COCO string class names (hosted API returns strings)
VEHICLE_NAMES = {"car", "motorcycle", "bus", "truck", "vehicle", "van", "suv"}

# Burned-in timestamp banner occupies the top ~18px of the 352x240 frame.
BANNER_PX = 18


# --------------------------------------------------------------------------
# Camera fetch
# --------------------------------------------------------------------------
def fetch_frame(cam_id: str, timeout: int = 10) -> bytes:
    r = requests.get(CAM_URL.format(cam_id=cam_id), timeout=timeout)
    r.raise_for_status()
    if not r.headers.get("content-type", "").startswith("image"):
        raise RuntimeError(f"not an image: {r.headers.get('content-type')}")
    return r.content


# --------------------------------------------------------------------------
# Geometry: point-in-polygon (ray casting), no shapely dependency
# --------------------------------------------------------------------------
def point_in_poly(x: float, y: float, poly: List[Tuple[float, float]]) -> bool:
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi:
            inside = not inside
        j = i
    return inside


# --------------------------------------------------------------------------
# Per-camera zone config. Coordinates are in NATIVE 352x240 pixel space.
# Use calibrate.py to draw these by clicking.
# --------------------------------------------------------------------------
ZONES: Dict[str, dict] = {
    # Belt Pkwy @ Plumb 3 St. Carriageways split by a diagonal median guardrail
    # running from approx (0,168) on the left edge to (300,116) at the vanishing point.
    "111e79a9-eb5a-44d0-b062-481ac0a81901": {
        "name": "Belt Pkwy @ Plumb 3 St",
        # far carriageway (above the median) = traffic heading EAST/outbound
        "horizon": 120,
        "outbound": [(0, 122), (300, 104), (306, 120), (0, 172)],
        # near carriageway (below the median) = traffic heading WEST/inbound
        "inbound": [(0, 174), (306, 120), (352, 128), (352, 210), (0, 238)],
        "exclude": [],
    },
    # Ocean Pkwy @ Ave X, facing south. Median tree strip splits directions.
    # Far-right band is a service road / parked cars -> excluded from flow.
    "64ed1f5d-ba90-4c12-afda-9a9c3e658efd": {
        "name": "Ocean Pkwy @ Ave X",
        "horizon": 85,
        "inbound": [(0, 120), (150, 84), (200, 96), (170, 130), (0, 175)],
        "outbound": [(152, 82), (250, 88), (330, 118), (215, 112)],
        "exclude": [(255, 78), (352, 78), (352, 120), (300, 112)],
    },
}


def classify_zone(cx: float, cy: float, cfg: dict) -> str:
    # Above the horizon cutoff the two carriageways converge toward the vanishing
    # point and the left/right split is no longer trustworthy -> drop them.
    if cy < cfg.get("horizon", 0):
        return "excluded"
    ex = cfg.get("exclude") or []
    if ex and point_in_poly(cx, cy, ex):
        return "excluded"
    if cfg.get("inbound") and point_in_poly(cx, cy, cfg["inbound"]):
        return "inbound"
    if cfg.get("outbound") and point_in_poly(cx, cy, cfg["outbound"]):
        return "outbound"
    return "unassigned"


# --------------------------------------------------------------------------
# Detection backends
# --------------------------------------------------------------------------
class LocalYOLO:
    """Path B: no API key. Ultralytics COCO weights, fully offline after first download."""

    def __init__(self, weights: str = "yolo11s.pt", conf: float = 0.20, imgsz: int = 640):
        from ultralytics import YOLO
        self.model = YOLO(weights)
        self.conf = conf
        self.imgsz = imgsz

    def detect(self, jpeg_bytes: bytes) -> List[dict]:
        im = Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")
        r = self.model.predict(
            im, conf=self.conf, iou=0.45, imgsz=self.imgsz,
            classes=list(COCO_VEHICLE),   # only vehicle classes leave the net
            agnostic_nms=True,            # CRITICAL: else one car returns as car AND truck
            verbose=False,
        )[0]
        out = []
        for box, cls, cf in zip(r.boxes.xyxy.tolist(), r.boxes.cls.tolist(), r.boxes.conf.tolist()):
            c = int(cls)
            if c not in COCO_VEHICLE:
                continue
            x1, y1, x2, y2 = box
            out.append({
                "cls": COCO_VEHICLE[c],
                "conf": float(cf),
                "cx": (x1 + x2) / 2.0,
                "cy": (y1 + y2) / 2.0,
                "w": x2 - x1, "h": y2 - y1,
            })
        return out


class RoboflowHosted:
    """Path A: Roboflow hosted inference. Keeps the container light (requests only)."""

    def __init__(self, model_id: str, api_key: str | None = None,
                 conf: float = 0.20, overlap: float = 0.45,
                 host: str = "https://serverless.roboflow.com"):
        self.model_id = model_id
        self.api_key = api_key or os.environ["ROBOFLOW_API_KEY"]
        self.conf = conf
        self.overlap = overlap
        self.host = host.rstrip("/")

    def detect(self, jpeg_bytes: bytes) -> List[dict]:
        b64 = base64.b64encode(jpeg_bytes).decode("ascii")
        resp = requests.post(
            f"{self.host}/{self.model_id}",
            params={
                "api_key": self.api_key,
                # Current serverless API takes 0-1 FLOATS (the 0-100 percentage
                # convention was the legacy detect.roboflow.com era).
                "confidence": self.conf,
                "overlap": self.overlap,
            },
            data=b64,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=20,
        )
        resp.raise_for_status()
        js = resp.json()
        out = []
        for p in js.get("predictions", []):
            name = str(p.get("class", "")).lower()
            if name not in VEHICLE_NAMES:
                continue
            # Roboflow returns CENTER x,y plus width,height in pixels of the
            # image as the server saw it. Rescale to native 352x240 if needed.
            out.append({
                "cls": name,
                "conf": float(p.get("confidence", 0)),
                "cx": float(p["x"]), "cy": float(p["y"]),
                "w": float(p["width"]), "h": float(p["height"]),
            })
        img_meta = js.get("image") or {}
        sw, sh = img_meta.get("width"), img_meta.get("height")
        if sw and sh and (int(sw) != 352 or int(sh) != 240):
            fx, fy = 352.0 / float(sw), 240.0 / float(sh)
            for d in out:
                d["cx"] *= fx; d["cy"] *= fy; d["w"] *= fx; d["h"] *= fy
        return out


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------
def count_vehicles(jpeg_bytes: bytes, backend, cam_id: str | None = None) -> dict:
    """Detect vehicles and split them into inbound / outbound by zone polygon."""
    dets = [d for d in backend.detect(jpeg_bytes) if d["cy"] >= BANNER_PX]
    cfg = ZONES.get(cam_id or "", {})

    by_class: Dict[str, int] = {}
    by_zone: Dict[str, int] = {"inbound": 0, "outbound": 0, "unassigned": 0, "excluded": 0}
    for d in dets:
        by_class[d["cls"]] = by_class.get(d["cls"], 0) + 1
        z = classify_zone(d["cx"], d["cy"], cfg) if cfg else "unassigned"
        d["zone"] = z
        by_zone[z] += 1

    flow = by_zone["inbound"] + by_zone["outbound"]
    return {
        "cam_id": cam_id,
        "cam_name": cfg.get("name"),
        "ts": time.time(),
        "total_vehicles": len(dets),
        "by_class": by_class,
        "inbound": by_zone["inbound"],
        "outbound": by_zone["outbound"],
        "unassigned": by_zone["unassigned"],
        "excluded": by_zone["excluded"],
        # >0 means more traffic heading in than out
        "net_inbound": by_zone["inbound"] - by_zone["outbound"],
        "inbound_share": (by_zone["inbound"] / flow) if flow else None,
        "detections": dets,
    }
