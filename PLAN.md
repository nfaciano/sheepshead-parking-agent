# Should I Drive to Sheepshead Bay?

A parking-demand agent for NYC. Reads live DOT traffic cameras on the roads into Sheepshead Bay,
counts vehicles inbound vs outbound, cross-references NYC parking rules, and answers one question
in plain English: **is it worth driving in right now?**

Built for someone specific — my uncle, who lives in Sheepshead Bay and circles the block.

---

## Honest scope (say this on stage)

- It is a **demand estimate, not a spot finder.** It cannot see an open space.
- It reads **gateway roads**, not residential blocks. The DOT cameras point at parkways and
  arterials, not at the streets you actually park on.
- **Counts only. No plates, no faces.** Vehicle bounding boxes, discarded after counting.

That limitation is a feature of the pitch, not a hole in it. Naming it scores on data craft.

---

## Verified before writing a line of code (5:07 PM)

| Assumption | Status |
|---|---|
| DOT camera API is public, no auth | ✅ `HTTP 200`, 968 cameras, 965 online |
| Cameras exist near Sheepshead Bay | ✅ 4 gateway cams within 1.8 mi |
| Live frames actually fetch | ✅ JPEG, EXIF `datetime=2026:08:07 17:07:04` |
| Vehicles are countable at that resolution | ✅ 352×240, cars ~20–40px, clearly visible |
| The inflow/outflow signal is real | ✅ see below |

**The signal, visible to the naked eye:** `demo-assets/beltpkwy-plumb3-1707.jpg` — Belt Pkwy at
5:07 PM Friday. Eastbound (into Sheepshead Bay) is bumper to bumper. Westbound is nearly empty.
That is the entire thesis in one frame, with a burned-in timestamp proving it's live.

Gateway cameras, all online:

| Distance | Camera | ID |
|---|---|---|
| 0.81 mi | Belt Pkwy @ Plumb 3 St | `111e79a9-eb5a-44d0-b062-481ac0a81901` |
| 1.17 mi | Ocean Pkwy @ Ave X | `64ed1f5d-ba90-4c12-afda-9a9c3e658efd` |
| 1.21 mi | Belt Pkwy @ Ocean Pkwy | `9bdd7740-762f-48e9-b40c-3db03c2a43f5` |
| 1.71 mi | Coney Island Ave @ Kings Hwy | `899dfa1e-a2c5-490a-b8ba-480493634846` |

API shape:
```
GET https://webcams.nyctmc.org/api/cameras            → [{id,name,latitude,longitude,area,isOnline,imageUrl}]
GET https://webcams.nyctmc.org/api/cameras/{id}/image  → 352x240 JPEG, refreshes ~2s
```
Note `isOnline` is the **string** `"true"`, not a boolean.

---

## Architecture

```
DOT traffic cams  ──►  vehicle detection  ──►  inbound/outbound counts  ──┐
(4 gateway cams)       (Roboflow)              (per-camera lane polygons) │
                                                                          ├──►  verdict
NYC parking rules ──►  alternate-side status ─────────────────────────────┘     (plain English)
(ASP calendar,         + curb regs
 311 complaints)
                              all of it on Google Cloud Run
```

Why the two halves multiply instead of add: heavy inbound traffic means competition for spots.
Alternate-side **suspended** means nobody moved their car, so the spots that exist are already
taken. Traffic alone is a weak signal. Traffic × churn is the real one.

---

## Timeline — lock is 8:30 PM

| By | Milestone | Owner |
|---|---|---|
| 5:45 | Hello-world live on Cloud Run. **Gate cleared.** Public GitHub repo pushed. | Nick |
| 6:15 | Real app deployed: live frames rendering, stub counts | agent → Nick deploys |
| 6:45 | Real vehicle detection returning counts on live frames | agent |
| 7:15 | Parking-rules signal wired in, verdict text generating | agent |
| 7:45 | **Record the fallback demo video.** Cameras die. | Nick |
| 8:10 | README final, screenshots in, last push | Nick |
| 8:30 | **SUBMIT** | Nick |

~20 min of slack. Deploy early beats build pretty.

---

## Judging criteria → what we do about each

| Criterion | Our play |
|---|---|
| Working Demo | Live cams on stage, real timestamps burned into the frames |
| NYC Relevance | Cannot be more NYC. Traffic cams, alternate-side parking, a Brooklyn uncle |
| Usefulness / Insight | The ASP-churn insight is the "oh wow" — it's non-obvious and true |
| Technical Execution | Clean seams: detect / rules / verdict are separate, swappable modules |
| Cloud Run | The whole thing runs there. Gate cleared first, not last |
| Open Source | README written as we go, honest limitations section, public repo |

---

## Fallback ladder (if something breaks)

1. Primary camera offline → swap to the fallback ID in `cameras.json`
2. Roboflow API down/keyless → local YOLO in the container
3. Detection unusable → ship the frame-diff "activity level" heuristic, say so honestly
4. Everything on fire at 8:25 → the recorded video from 7:45 plus the live repo

---

## Repo

```
HACK/
  app/          FastAPI service — main, cameras, detect, verdict
  cameras.json  camera set + per-camera inbound/outbound lane polygons
  demo-assets/  verified live frames, camera snapshot
  Dockerfile    listens on $PORT, 0.0.0.0
  README.md     the story, the limits, the deploy command
  HANDBOOK.md   event rules, judging, links
  PLAN.md       this file
```
