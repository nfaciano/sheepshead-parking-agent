# 60-second video — script + steps

Record now. YouTube processing takes 5–10 min and you need the link before 8:30.

---

## Before you hit record

1. Open `https://sheepshead-420329548463.us-east1.run.app` in a clean browser window
2. Close every other tab, hide the bookmarks bar, close Slack/Messages
3. Have this typed and ready in the ask box (do NOT submit yet):
   `I get home to 1822 Avenue X in an hour, where should I park?`
4. Zoom the page to ~125% (Cmd +) so text reads on a phone

## Record

**Cmd + Shift + 5** → "Record Selected Portion" → drag around the browser window → Record.
Stop with the stop button in the menu bar. It saves to your Desktop.

Talk while you click. One take is fine. Two if the first is rough — don't do five.

---

## The script — ~150 words, times are cumulative

**[0:00–0:10] The problem. Show the page.**

> My uncle lives in Sheepshead Bay. Every night he circles the block for twenty
> minutes looking for parking.
>
> He's not circling because there's nowhere to park. He's circling because this is
> what the signs look like.

**[0:10–0:35] Hit Ask. Talk while it thinks (~7 seconds).**

> So I built an agent that reads them for him. It's got six thousand two hundred and
> eighty-eight real DOT sign records, the alternate side parking calendar, and four
> live NYC traffic cameras that Gemini reads.
>
> You ask it in plain English.

*(answer appears)*

> And it tells you the block, the side of the street, how far you're walking, and when
> you'd have to move the car.

**[0:35–0:50] Click "what about Aug 15?"**

> Here's the part I didn't expect. Traffic tells you cars are arriving. It tells you
> nothing about whether any are leaving.
>
> What makes cars leave in New York is alternate side parking. On August fifteenth
> it's suspended for a holiday. Nobody has to move. So the curb stays full — same
> traffic, opposite answer.

**[0:50–1:00] Close.**

> It won't tell you a space is empty. Nothing can. It tells you where you're allowed
> to leave the car, which is the part he actually can't figure out.
>
> It's running on Cloud Run, the repo's public.

---

## Upload

1. `studio.youtube.com` → **Create** → **Upload videos**
2. Title: `Which street should I try? — NYC Vision Hack`
3. Visibility: **Unlisted** ← faster than Public, no review delay, link still works
4. Skip everything optional. Hit through the wizard fast.
5. Copy the link as soon as it appears — it works while processing finishes

## Then submit

Paste the YouTube link plus:
- Live: `https://sheepshead-420329548463.us-east1.run.app`
- Repo: `https://github.com/nfaciano/sheepshead-parking-agent`

---

## If you're short on time

Cut the middle. This still works at 35 seconds:

> My uncle circles Sheepshead Bay for twenty minutes every night looking for parking.
> Not because there's nowhere to park — because NYC parking signs are unreadable.
> There are 6,288 of them in his neighborhood.
>
> *(ask it, show the answer)*
>
> It reads them all and tells him which block, which side, and when he'd have to move.
> It won't tell you a space is empty — nothing can. It tells you where you're allowed
> to leave the car. Running on Cloud Run, repo's public.
