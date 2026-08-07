"""Cloud Run service: GET /signal -> inbound/outbound demand signal for Sheepshead Bay."""
import os, statistics, time
from flask import Flask, jsonify
from counter import fetch_frame, count_vehicles, ZONES, LocalYOLO, RoboflowHosted

app = Flask(__name__)
BACKEND = (
    RoboflowHosted(os.environ.get("RF_MODEL_ID", "coco/50"))
    if os.environ.get("ROBOFLOW_API_KEY")
    else LocalYOLO(os.environ.get("YOLO_WEIGHTS", "yolo11s.pt"))
)
POLLS = int(os.environ.get("POLLS", "3"))


@app.get("/health")
def health():
    return {"ok": True, "backend": type(BACKEND).__name__}


@app.get("/signal")
def signal():
    cams = []
    for cam_id in ZONES:
        runs = []
        for i in range(POLLS):                 # median over N polls kills flicker
            try:
                runs.append(count_vehicles(fetch_frame(cam_id), BACKEND, cam_id))
            except Exception as e:
                app.logger.warning("poll failed %s: %s", cam_id, e)
            if i < POLLS - 1:
                time.sleep(2)
        if not runs:
            continue
        med = lambda k: int(statistics.median([r[k] for r in runs]))
        inb, outb = med("inbound"), med("outbound")
        cams.append({
            "cam_id": cam_id, "cam_name": runs[0]["cam_name"],
            "inbound": inb, "outbound": outb, "net_inbound": inb - outb,
            "total": med("total_vehicles"), "polls": len(runs),
        })
    net = sum(c["net_inbound"] for c in cams)
    tot = sum(c["total"] for c in cams)
    verdict = ("PACKED - people are still pouring in, parking will be rough" if net > 2
               else "EMPTYING OUT - spots are opening up, good time to go" if net < -2
               else "STEADY - roughly as many leaving as arriving")
    return jsonify({"ts": time.time(), "net_inbound": net, "total_vehicles": tot,
                    "verdict": verdict, "cameras": cams,
                    "privacy": "counts only; no plates, no faces, no imagery retained"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
