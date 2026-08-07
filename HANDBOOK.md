# NYC Vision Hack v.2 — Live Feeds, Open Data

Source: https://nyc.aitinkerers.org/hackathons/h_zvqhzy3dMEY/handbook
Presented with Google Cloud · Sponsored by Roboflow · Hosted with Veris AI

## Clock

| Time | Activity |
|---|---|
| 4:00 PM | Doors / check-in |
| 4:30 PM | Kickoff |
| 4:45 PM | Workshop: deploying agents on Cloud Run |
| 5:15 PM | Team formation + building |
| 7:00 PM | Dinner + keep building |
| **8:30 PM** | **SUBMISSION LOCK** |
| 8:45 PM | Demos |
| 9:45 PM | Awards + wrap |

Date: Friday, August 7, 2026 · 4–10 PM · fully in-person, 50 spots, no remote.
Teams: up to 4. Solo allowed.

## HARD GATE

**The agent must be deployed on Google Cloud Run.** Binary. No Cloud Run = not eligible.

## Theme

Vision agents on NYC city data. AI agents + computer vision + NYC Open Data (plus any
public feed you can responsibly integrate tonight). Traffic flow analyzer, pedestrian
counter, double-parking detector, "what's happening on my block" agent, accessibility
auditor, street-life visualizer, weird NYC toy. "If it ships as a demo, it counts."

## Judging

Eligibility gate: agent deployed on Cloud Run.

| Criterion | What they want | 1 · 3 · 5 |
|---|---|---|
| Working Demo | Running on real feeds | Slips · Mostly works · Solid |
| NYC Relevance | Clear tie-in to the city | Loose · Obvious use · Sharp fit |
| Usefulness or Insight | Clearer, faster, safer, cheaper, more legible | Meh · Helpful · "Oh wow" |
| Technical Execution | Thoughtful approach, clean system, good tradeoffs | Rough · Functional · Polished |
| Uses Cloud Run | Required to submit | Does or Doesn't |
| Open Source | Repo + README tell the story, public GitHub | Bare bones · Usable · Clean |

## Prize

Grand Prize: Teenage Engineering EP–2350 ting (handheld performance mic). Plus sponsor credits.

## Google Cloud environment

Temp credentials on the `@gcplab.me` domain. One assigned project.

- Console: console.cloud.google.com — **use a fresh Chrome profile or incognito**
- Same creds work for AI Studio (ai.studio) and Antigravity (antigravity.google).
  For Antigravity, specify your project ID to get the Business Plan.
- AI Studio: "Get API key" → if none shown, hit "Import Projects" and select your project.
  A key is already provisioned. Can export directly to Antigravity.
- Cloud Shell: browser shell, git/python preinstalled. Icon top-right of console.
- Cloud Shell Editor: ide.cloud.google.com or "Open Editor" from Cloud Shell.
- Gemini CLI: type `gemini` in Cloud Shell, or install locally.
- Antigravity download: antigravity.google/download

Where things live: Cloud Run (deploy target) · GKE Autopilot · Agent Platform (LLM APIs +
Model Garden) · Cloud SQL / AlloyDB · Compute Engine · Cloud Storage · BigQuery · Looker ·
Firebase. Gmail/Docs/Chat/Drive/NotebookLM all work for @gcplab.me.

Rules: everything decommissioned at end of event — copy out what you want to keep. Don't
leak API keys. Secure open ports.

## Roboflow (vision sponsor)

- Docs: docs.roboflow.com
- API key: app.roboflow.com/settings/api
- **Workflows** (multi-stage vision pipelines, good for one-evening builds): docs.roboflow.com/workflows
- Deployment: docs.roboflow.com/deployment
- Models: docs.roboflow.com/models
- Datasets: docs.roboflow.com/datasets/create-and-upload/adding-data
- CLI + Python SDK reference: docs.roboflow.com/reference
- **Universe** (thousands of pretrained models + public datasets, skip annotation): universe.roboflow.com

Handbook tip: hosted inference API pairs cleanly with Cloud Run — agent on Cloud Run calls
Roboflow for detections, satisfies the gate while keeping the container light.

## Veris AI (host)

Simulation sandbox for agent development — test scenarios, simulated tools, personas.
Run routine / edge-case / adversarial interactions before the agent touches a real system.
Useful to pressure-test the demo before stage.

## DOT Camera Quickstart

- All cameras: `https://webcams.nyctmc.org/api/cameras`
  → array of hundreds of camera objects: `id`, `name`, `latitude`, `longitude`,
  `area` (borough), `isOnline`, `imageUrl`
- Single frame: `https://webcams.nyctmc.org/api/cameras/{id}/image`
  → current still. Refreshes every couple seconds → poll for a low-fps video feed.

```python
import requests, time

cams = requests.get("https://webcams.nyctmc.org/api/cameras").json()
online = [c for c in cams if c["isOnline"] == "true"]

cam = online[0]  # or filter by name/borough/lat-lng
while True:
    frame = requests.get(cam["imageUrl"]).content  # JPEG bytes
    # hand off to Roboflow inference here
    time.sleep(2)
```

Note: `isOnline` compares against the **string** `"true"` in their sample.

## NYC Open Data sources listed

NYCTMC Traffic Camera Map · NYCTMC Traffic Camera List · NYISO Real-Time Dashboard ·
Reddit collection of NYC 24/7 live cams · NY State Open Data Portal · NYC 311 Rodent
Complaints · Mapillary street-level imagery · MTA BusTime GTFS-Realtime (wiki + beta feed
docs) · NYC Fire Incident Dispatch · NYC Emergency Response Incidents · NYC Street Tree Map ·
NYC Tree Canopy (ArcGIS) · NYC Topobathymetric LiDAR 2017 · MTA Subway Accessible-Station
Platform Availability · NYC Landmarks Preservation Commission Maps · NYC Historical Signs
Directory · NYC Outdoor Public Art Inventory · Green-Wood Cemetery Burial & Vital Records ·
NYC Municipal Archives 1940 Tax Photos (+ map + individual building viewer) · Welikia
Project (Mannahatta) · Hidden Hydrology · NYC DOT Truck Routing · NYC Truck Routes (data.gov) ·
Residential Parcel Demand & Delivery VKT (NYU Built Lab) · Con Edison Steam Service ·
BirdCast live migration · MarineCadastre vessel traffic · OpenSky Network live air traffic

## Best practices (theirs)

1. **Deploy early.** Hello-world agent on Cloud Run in the first hour, then iterate.
   The gate is binary and 8:30 comes fast.
2. **Open source quality counts** — it's a scored criterion. Push early, honest README.
3. **Demo on real feeds**, and have a fallback clip in case a city cam dies at 8:45.
4. **Mind the data.** Sourcing, privacy, reproducible pipelines. Faces and plates
   in public feeds deserve care.
5. **Lock your team at kickoff.** 8:30 PM sharp.

Slack channel for updates/mentors. #NYCVisionHack on X.
