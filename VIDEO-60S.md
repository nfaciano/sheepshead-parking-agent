# 60-second video — read this off a second screen

Live: https://sheepshead-420329548463.us-east1.run.app

---

## Setup (2 min, before you record)

1. **Deploy first** so you're filming the current build.
2. Open the live URL in a clean window. Close other tabs, hide the bookmarks bar,
   quiet Slack and Messages.
3. **Cmd +** twice — about 125%, so it reads on a phone.
4. Pre-type into the **lower** box (the "Or look up an address directly" one):
   `1822 Avenue X` — leave it, don't submit.
5. Pre-type into the **upper** ask box:
   `I get home to 1822 Avenue X in an hour, where should I park?` — leave it.
6. Scroll to the very top.

**Record: Cmd + Shift + 5 → Record Selected Portion → drag around the browser.**

---

# THE SCRIPT

Read it. They're watching your screen, not your face.

---

### [0:00 – 0:12]  Click **"Find me a block"** on the lower box. Results are instant. Scroll down a notch so a grey sign line is visible.

> My uncle lives in Sheepshead Bay. Every night he circles the block for twenty
> minutes looking for parking.
>
> He's not circling because there's nowhere to park.

**← point at the grey monospace line**

> He's circling because that's what the signs look like. There are six thousand two
> hundred and eighty-eight of those in his neighborhood.

---

### [0:12 – 0:35]  Scroll back to the top. Click **Ask**. Keep talking — it takes about seven seconds.

> So I built an agent that reads all of them. Every posted DOT sign, the alternate
> side parking calendar, six months of 311 complaints, and four live NYC traffic
> cameras that Gemini reads.

**← the answer appears**

> You ask it in plain English, and it tells you which block, which side of the street,
> how far you're walking, and when you'd have to move the car.

---

### [0:35 – 0:50]  Click the **"what about Aug 15?"** chip.

> Here's the part I didn't expect.
>
> Traffic tells you cars are arriving. It tells you nothing about whether any are
> leaving. What makes cars leave in New York is alternate side parking.
>
> On August fifteenth it's suspended for a holiday. Nobody has to move their car. So
> the curb stays full. Same traffic, opposite answer.

---

### [0:50 – 1:00]  Stop clicking. Just talk.

> It won't tell you a space is empty. Nothing can — nobody publishes that. It tells
> you where you're allowed to leave the car, which is the part he actually can't
> figure out.
>
> It's live on Cloud Run and the repo's public.

---

**≈155 words. One take. Two if the first is rough. Not five.**

---

## If you have room for one more line

While the agent is thinking, instead of silence:

> And this band down here is the live half — four DOT cameras, read every twenty-five
> seconds. We've been logging it since six o'clock tonight.

---

## Upload

1. `studio.youtube.com` → **Create** → **Upload videos**
2. Title: `Which street should I try? — NYC Vision Hack`
3. Visibility: **Unlisted** — no review delay, link works immediately
4. Skip every optional field. Click through fast.
5. **Copy the link as soon as it appears** — it works while processing finishes.

## Submit

- YouTube link
- Live: `https://sheepshead-420329548463.us-east1.run.app`
- Repo: `https://github.com/nfaciano/sheepshead-parking-agent`

---

## Do not say

- "The cameras watch his street" — there is no DOT camera on any residential street
- "It finds you an open spot" — it finds you a **legal block**
- Any accuracy percentage — you don't have one and don't need one
