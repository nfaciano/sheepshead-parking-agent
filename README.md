# Which street should I try?

My uncle lives in Sheepshead Bay. When he gets home around 7 PM he circles the block
for twenty minutes, because there is no way to know which street is worth trying.

This answers that. You type where you're headed; it ranks every block face within a
few minutes' walk by where you're most likely to find a legal spot — reading the
posted DOT sign for each block, checking whether alternate side parking is about to
churn it, and watching the live traffic camera on the road that feeds it.

**Live:** https://sheepshead-420329548463.us-east1.run.app

```
Avenue Z — south side                     GOOD    2 min walk · 456 ft
between East 13 Street and East 14 Street
Legal now; must move by Sat 8:00 AM
Traffic in via Ocean Pkwy @ Ave X — light (little competition)
```

Coverage: **1,224 block faces across 143 streets**, 4 gateway cameras.

---

## What it actually knows, and what it doesn't

This is the part most worth reading.

**It is a demand estimate, not a spot finder.** It cannot see an open space. Nothing
in the pipeline knows where a parking spot is.

**It watches gateways, not blocks.** There is no NYC DOT camera on Emmons Ave, or
Sheepshead Bay Rd, or Ave X — we checked all 968 cameras in the city. The nearest
surface-street camera is 1.7 miles north. So the system watches the parkways and
arterials that feed the neighborhood and infers pressure from what flows in. Belt
Parkway also carries through-traffic that will never park in Sheepshead Bay, which
inflates the signal.

**The direction split is solid; the direction label is not.** On a divided parkway a
guardrail physically separates the two carriageways, so assigning a vehicle to one
side or the other is reliable. But DOT publishes no compass heading per camera, so
which side means "into the neighborhood" is inferred from scene geography. That label
carries a confidence value in `cameras.json`, and two cameras where the split would
have been a guess are deliberately left `null` rather than filled in with a
confident-looking wrong answer.

**A vision-language model is less reproducible than a detector.** Two reads of the
same frame can differ by a vehicle or two. Temperature is pinned to 0 to reduce that,
and the output leans on a coarse congestion level rather than pretending a count is
exact.

**The scoring weights are not calibrated.** There is no ground-truth dataset of how
long it actually took to park in Sheepshead Bay at time T. The score is a documented
heuristic, not a measurement.

## Privacy

**Vehicle counts and flow only. No plates, no faces, nothing stored.**

This is structural, not a policy we adopted. NYC DOT camera stills are 352x240, where
a vehicle is 15-40 pixels wide. A license plate or a face is not resolvable at that
scale — it is not in the data to begin with. On top of that the prompt explicitly
asks for flow only and forbids describing any person, and frames live in memory for
the length of one request and are never written to disk.

---

## The idea

Traffic tells you how many cars are **arriving**. It tells you nothing about whether
any are **leaving**. For street parking, the second number is the one that matters.

What makes parked cars leave in New York is **alternate side parking**. When a street
sweeper is coming, an entire block face has to clear out and spots churn. When ASP is
suspended for a holiday, nobody moves and the neighborhood locks solid.

So the same traffic reading produces opposite answers:

> **Today:** ASP runs tomorrow morning. That block face has to empty. Go now.
>
> **August 15:** Feast of the Assumption, ASP suspended, nobody has to move their car.
> The curb stays exactly as full as it already is. Don't bother.

That inversion is the whole point of the project. It is also the part a traffic
dashboard cannot tell you.

## Ask it in plain English

```
"I get home to 1822 Avenue X in an hour, where should I park?"

  → Try the north side of Avenue X between East 18th and East 19th Street.
    Legal until Monday 11:30 AM. About 90 feet to the middle of that stretch,
    roughly 8 cars fit on it.
    311 shows very few complaints on this block face compared to the surrounding
    streets, so it's a quieter stretch to check.

  tools used: find_parking
```

Gemini gets the tool definitions and decides what to call. It resolves "in an hour"
to an arrival time, looks the blocks up *at that time*, and writes the answer. The
tools it can reach are `find_parking`, `check_alternate_side`, and
`get_traffic_conditions`; every response lists which ones ran, because a parking
recommendation you cannot audit is worth very little.

Hard-coding that chain would answer one question. Letting the model plan over the
tools answers ones nobody wrote a form for — *"is it worth waiting an hour?"*,
*"what about next Saturday?"*, *"which side of the street?"*

## Architecture

```
                        your address
                             │
                    census geocode → NY State Plane
                             │
    ┌────────────────────────┼────────────────────────┐
    │                        │                        │
6,288 DOT curb signs   ASP calendar          4 gateway cameras
grouped into           (42 holidays,          → Gemini reads flow
1,224 block faces       hardcoded)              + congestion per side
    │                        │                        │
    │   is it legal          │  will it churn         │  who's competing
    │   right now?           │  by morning?           │  for this block?
    └────────────────────────┼────────────────────────┘
                             │
                  ranked block faces near you
```

Everything runs on **Google Cloud Run**.

| Module | Does |
|---|---|
| `app/blocks.py` | Address → nearby block faces, ranked. The product |
| `app/parking.py` | ASP calendar + the NYC sign-text parser |
| `app/vision.py` | Gemini frame reads; ranks and falls through available models |
| `app/detect.py` | Backend seam — `gemini` \| `zones` \| `stub`, degrades without crashing |
| `app/cameras.py` | Camera config, frame fetching, caching |
| `app/verdict.py` | Neighborhood-wide conditions and scoring |
| `app/main.py` | FastAPI, background refresh, history, the page |
| `scripts/track.py` | Standalone logger; survives redeploys |

### How a block gets ranked

Three real signals, in order of how much they're worth:

1. **Legality** — from the posted sign record for that exact block face. Hard gate.
2. **Duration** — how long until you'd have to move. Overnight beats a 2-hour meter.
3. **Walk** — distance from the address you typed.

Then the nearest gateway camera's inbound congestion discounts the score by up to
30%. A block fed by a stopped parkway is a worse bet than an identical block fed by
an empty one. That discount is capped on purpose: heavy traffic makes a legal block
harder, never as bad as an illegal one.

**Why a VLM instead of an object detector.** The question is not "how many cars are in
frame" — a count is meaningless without knowing the camera's zoom and how much road is
visible. The question is "is traffic into the neighborhood heavy," and a VLM answers
that directly. It also sees things a box counter structurally cannot: that one
carriageway is stopped while the other is free-flowing, that it's raining, that a lane
is closed. `detect.py` keeps a YOLO/Roboflow backend behind the same seam.

**Why a background refresh.** Four Gemini calls take seconds. Serially they took 24,
which on stage looks like a crash. They now run concurrently (5.5s) behind a task that
refreshes every 25s, so requests serve from cache in under a millisecond and a Gemini
hiccup keeps serving the last good answer instead of an error.

## Data sources

| Source | Use |
|---|---|
| [NYC DOT traffic cameras](https://webcams.nyctmc.org/api/cameras) | 968 cameras; 4 gateways into Sheepshead Bay. Public, no auth |
| [NYC DOT ASP calendar 2026](https://www.nyc.gov/html/dot/downloads/pdf/asp-calendar-2026.pdf) | The 42 suspension dates. Hardcoded from the PDF — see below |
| [Parking Regulation Locations and Signs](https://data.cityofnewyork.us/resource/nfid-uabd.json) (`nfid-uabd`) | 6,288 curb signs in the neighborhood, pre-fetched |

Two things worth knowing if you build on this:

- **The ASP calendar is not an API.** `api.nyc.gov/public/api/GetCalendar` returns 401
  without a two-step subscription key. The dates are published once a year as a PDF, so
  they're a hardcoded constant here: no network call, nothing to fail mid-demo.
- **`nfid-uabd` has no lat/lon.** Coordinates are NY State Plane Long Island
  (EPSG:2263, feet). The neighborhood bounding box is expressed in state-plane units in
  `app/parking.py`.

## Run it

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
echo "GOOGLE_API_KEY=your-key" > .env        # gitignored
.venv/bin/uvicorn app.main:app --port 8000
```

Without a key it runs on the stub detector and says so, in the API response and on the
page. It never silently pretends to be doing computer vision.

| Env var | Default | |
|---|---|---|
| `GOOGLE_API_KEY` | — | Gemini API key. Absent → stub |
| `DETECTOR` | `gemini` | `gemini` \| `zones` \| `stub` |
| `REFRESH_SECONDS` | `25` | Background refresh interval |
| `PORT` | `8080` | Cloud Run injects this |

## Deploy

```bash
gcloud run deploy sheepshead --source . \
  --region us-east1 --allow-unauthenticated --port 8080 \
  --set-env-vars GOOGLE_API_KEY=...,DETECTOR=gemini
```

The key is a runtime env var and never enters the image — `.env` is in `.dockerignore`
as well as `.gitignore`.

## Routes

| | |
|---|---|
| `GET /` | The page |
| `GET /api/parking?address=...` | **The main one.** Ranked block faces near an address |
| `GET /api/verdict` | Neighborhood conditions, per-camera reads, cache age |
| `GET /api/history` | Every reading since this instance started |
| `GET /api/frame/{id}` | Live JPEG proxy |
| `GET /health` | Cloud Run health |

```bash
curl "https://sheepshead-420329548463.us-east1.run.app/api/parking?address=2650%20E%2014th%20St"
```

---

Built at NYC Vision Hack v.2, August 7 2026.
