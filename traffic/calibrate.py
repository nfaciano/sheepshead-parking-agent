"""Click-to-draw zone polygons for a camera. Run locally, paste output into ZONES.

    python calibrate.py <cam_id>

Left-click = add point. 'n' = finish current polygon, start next.
Order of polygons: inbound, outbound, exclude. 'q' = quit and print JSON.
"""
import sys, json, io
import requests
from PIL import Image
import matplotlib.pyplot as plt

cam_id = sys.argv[1]
raw = requests.get(f"https://webcams.nyctmc.org/api/cameras/{cam_id}/image", timeout=10).content
im = Image.open(io.BytesIO(raw)).convert("RGB")

names = ["inbound", "outbound", "exclude"]
polys, cur, idx = {}, [], 0

fig, ax = plt.subplots(figsize=(12, 8))
ax.imshow(im)
ax.set_title(f"{names[0]} — click points, 'n'=next polygon, 'q'=done")
ax.grid(color="cyan", alpha=.4); ax.set_xticks(range(0, 353, 32)); ax.set_yticks(range(0, 241, 32))

def on_click(e):
    global cur
    if e.inaxes != ax or e.xdata is None: return
    cur.append((round(e.xdata), round(e.ydata)))
    ax.plot(e.xdata, e.ydata, "o", color="red")
    if len(cur) > 1:
        xs = [p[0] for p in cur[-2:]]; ys = [p[1] for p in cur[-2:]]
        ax.plot(xs, ys, "-", color="red", lw=1.5)
    fig.canvas.draw()

def on_key(e):
    global cur, idx
    if e.key == "n":
        if cur: polys[names[idx]] = cur
        cur = []; idx += 1
        if idx >= len(names): plt.close(fig); return
        ax.set_title(f"{names[idx]} — click points, 'n'=next, 'q'=done")
        fig.canvas.draw()
    elif e.key == "q":
        if cur: polys[names[idx]] = cur
        plt.close(fig)

fig.canvas.mpl_connect("button_press_event", on_click)
fig.canvas.mpl_connect("key_press_event", on_key)
plt.show()
print(json.dumps({cam_id: {"name": "TODO", "horizon": 0, **polys}}, indent=4))
