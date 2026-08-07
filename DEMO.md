# DEMO.md — "Which Street Should I Try?"

NYC Vision Hack v.2 — Aug 7 2026 — demo slot 8:45 PM, ~3 min
Live: https://sheepshead-420329548463.us-east1.run.app
Repo: https://github.com/nfaciano/sheepshead-parking-agent

---

## 1. The 3-minute script

Stage directions are in `[brackets]` — do not say those out loud. Everything else is said as written. Practice it out loud twice before 8:40, not just read silently — the timing only holds if you've said the words with your mouth.

**[0:00–0:15] HOOK**

> "My uncle lives in Sheepshead Bay. Every night he circles the block for twenty minutes, not because there's no parking, but because he can't read the signs fast enough to trust one."

**[0:15–0:30] WHAT THIS IS**

> "So I built something that reads them for him. It's called Which Street Should I Try. You type an address and when you're arriving, and it ranks the blocks around you."

**[0:30–1:10] LIVE SEARCH — `[type: 1822 Avenue X, hit search]`**

> "Let's try his street. This is checking twelve hundred block faces, built from sixty-two hundred real DOT sign records. And here's the ranked list: legal or not, how long you can stay, how far you'd walk."

**[1:10–1:40] ARRIVAL-TIME FLIP — `[search "now", note top block, then click "in 1 hour"]`**

> "But watch this. I searched for right now — top pick is a two hour meter. Now I search again for one hour from now. Different top block. Same address, only the time changed. That meter runs out at seven, so ten minutes earlier or later, the right street is a different street."

**[1:40–2:20] THE AUGUST 15 INVERSION — the centerpiece, slow down here**

> "Here's the part I actually built this for. Traffic on the main roads tells you cars are arriving. It tells you nothing about who's leaving. What actually clears a spot here is alternate side parking — the street sweeper forces everyone to move and spots churn. On August fifteenth, alternate side is suspended. It's a holiday, the Feast of the Assumption. Nobody has to move their car. So a normal night: heavy traffic, spots about to churn, go now. August fifteenth: same heavy traffic, nothing opens up. Don't bother. Same number. Opposite advice."

**[2:20–2:45] THE LIMITATION — say this like it's a feature, not a confession**

> "One thing this can't do, and I'll say it straight: it can't tell you a spot is empty. No public feed in this city has that, for me or anyone. And there's no camera near his actual block — the nearest one to this address is forty-six hundred feet away, almost a mile. This tells you where the odds are best. It doesn't promise you a space."

**[2:45–3:00] CLOSE**

> "It runs on Cloud Run, it's open source, built entirely on public NYC data. If it saves my uncle one twenty-minute loop, that's a win. Thanks."

Total: ~390 spoken words. That's a comfortable 3:00 even at a slower, nervous pace — you have slack, not a deficit. If you're running long, cut from the arrival-time beat (1:10–1:40) first, not from the August 15 beat or the limitation — those two are the ones judges will remember.

---

## 2. The 60-second version (if they cut you off)

Say this as one continuous paragraph, no pauses for demo — narrate it, don't drive the app:

> "My uncle lives in Sheepshead Bay, and every night he circles the block for twenty minutes — not because there's no parking, but because he can't read the signs fast enough to trust one. So I built something that reads them for him: type an address, type when you're arriving, and it ranks the blocks around you by where you're legally allowed to park and for how long. The part I actually care about — on August fifteenth, alternate side parking is suspended for a holiday, so the street sweeper never comes and spots never churn. Same traffic as any other night, opposite advice. To be straight with you: it can't tell you a spot is empty, no public feed in this city can, and there's no camera anywhere near his actual block — the closest one is almost a mile away. It runs on Cloud Run, it's open source, built on public NYC data."

---

## 3. Judge Q&A — 12 likely questions, honest answers

**1. Isn't this just a database lookup?**
Mostly, yes, for the sign-parsing half — that's deterministic, not ML magic, and I'm not going to dress it up as more than it is. Real DOT sign text goes in, a legal/illegal answer with a "must move by" time comes out. The part that isn't a lookup is the ranking on top: arrival-time-aware legality, the camera-based congestion discount, and the ASP calendar all combine per search. The lookup is the foundation. The insight is what I do with it.

**2. How do you know a spot is open?**
I don't. No public feed in NYC publishes live curb occupancy — not for me, not for anyone building this kind of tool. This ranks where the odds are best. It is not a spot finder and I don't claim it is.

**3. Why a VLM instead of YOLO?**
The question I need answered per camera isn't "how many cars are in frame," it's "does this look like heavy inbound traffic" — a coarser, more contextual judgment across camera angles that differ block to block. A VLM let me get that signal without hand-labeling a training set the night of a hackathon. Trade-off, honestly: it's less reproducible than a detector and slower per frame. I made that trade for time, not because it's strictly the better engineering choice at scale.

**4. What about privacy — plates, faces?**
The honest answer is structural, not policy. DOT camera frames are 352 by 240 pixels. A vehicle in frame is 15 to 40 pixels. A license plate or a face is not physically resolvable at that resolution — it isn't blurred or dropped, it was never recoverable data to begin with. There's nothing to redact because there's nothing there.

**5. How would this scale to all of NYC?**
The data pipeline scales — same DOT sign feed, same block-face structure, citywide. What doesn't get better is the camera gap: all 968 city cameras sit on arterials and parkways, none on residential blocks, everywhere in the city, not just Sheepshead Bay. So the congestion signal stays exactly as indirect everywhere it expands. And VLM calls cost money per camera per interval, so cost scales roughly linearly with coverage — that's a real constraint, not a detail.

**6. What's the accuracy?**
I haven't measured it against ground truth, so I'm not going to give you a number. The scoring weights are hand-tuned, not fitted to real outcomes. I've manually spot-checked sign parsing against real posted signs and it holds up, but "spot-checked" and "validated" are different claims and I'm only making the first one tonight.

**7. Did you build this tonight?**
[Answer with your real timeline here, Nick — say plainly what was built before tonight versus during the event. Judges respect an honest build story more than a clean one. Don't let this be the one improvised answer of the night.]

**8. Why Cloud Run?**
One container, needs to be reachable the whole event, and I didn't want to manage a server on hackathon night. Cloud Run scales to zero when idle and I can redeploy in under a minute if something breaks on stage.

**9. How fresh is the underlying data?**
DOT sign records come from a public dataset (`nfid-uabd`) — curb regulations don't change often, so that's closer to a periodic pull than a live feed. The camera congestion read refreshes on a background loop roughly every 25 seconds. So: static legal rules, near-real-time congestion layer on top.

**10. What happens if a camera feed is down or misreads traffic?**
It degrades, it doesn't break. The legal/illegal ranking is built entirely from the static sign data, so that half always works. Losing a camera just means that block loses its congestion discount and falls back to the sign-only ranking.

**11. Could I get a ticket following this?**
This is advisory, not authoritative — always read the actual posted sign before you park. Sign data can lag reality: new construction, a sign that changed, a temporary posting. I'd never tell someone this replaces reading the sign in front of their windshield.

**12. What's next for this?**
Real ground truth to fit the ranking weights instead of hand-tuning them, and more neighborhoods beyond Sheepshead Bay. The camera-coverage gap is the long pole — solving that means either more cameras than the city has, or a different signal entirely.

---

## 4. Do NOT say

These are false or unverifiable and a judge who's paid attention to the demo will catch every one:

- "The cameras watch his street" — false. No DOT camera is on a residential street, anywhere in the city. Nearest one to the demo address is ~4,600 ft away.
- "We find you an open spot" / "an open spot" in any phrasing — false. No occupancy data exists to find one.
- Any "X% accurate" number — not measured, don't invent one on the spot if asked.
- "This is real-time occupancy" — it's real-time traffic congestion, a proxy, not occupancy.
- "It works citywide" / "it's live everywhere in NYC" — it's Sheepshead Bay only, 1,224 block faces.
- "It'll guarantee you don't get a ticket" — it's advisory. Say that plainly if asked, don't hedge into implying otherwise.
- Don't claim a specific build timeline you haven't actually confirmed in your own head first — see Q7.

---

## 5. Pre-demo checklist — 8:40 PM, 5 minutes before you're up

- [ ] Laptop charger plugged in, or battery above 50%
- [ ] Wifi confirmed working on the actual presentation network — have your phone hotspot ready as backup, venue wifi at hackathons is never trustworthy
- [ ] Tab 1: the live app open and already hit once tonight so Cloud Run is warm — a cold start mid-demo is the single most avoidable failure here
- [ ] Address field: know exactly what you're typing (`1822 Avenue X`), don't rely on memory under lights
- [ ] Run the full "now" vs "in 1 hour" comparison live, tonight, close to your actual slot time — the meter cutoff in the script is a real example, but if it's already past that cutoff by 8:45, the two searches may not visibly differ. Confirm it still flips at demo time; if it doesn't, pick a different real address that does, or fall back to describing it over a screenshot.
- [ ] Tab 2: a screen recording of one full successful search, queued and ready to play if the live network dies mid-demo
- [ ] Tab 3: GitHub repo open, in case a judge asks to see code
- [ ] Browser zoom increased so text is readable from a few feet back
- [ ] Phone silenced
- [ ] Close every other tab and app — no notifications on screen
- [ ] Say the first line out loud once, right before you walk up: "My uncle lives in Sheepshead Bay." Nail the first sentence and the rest follows — nerves hit hardest in the first ten seconds, not the middle.
