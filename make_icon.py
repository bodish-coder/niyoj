"""Generate niyoj.ico — a terminal prompt on a gradient tile.

A console chevron and cursor bar: says "shell on a remote box" rather than
the generic upload arrow, and both shapes stay readable down to 16px.

Run once (or after tweaking colours): python make_icon.py
"""
from PIL import Image, ImageDraw

S = 1024
A, B = (61, 220, 132), (47, 123, 240)          # accent green -> blue

# diagonal gradient
grad = Image.new("RGB", (S, S))
px = grad.load()
for y in range(S):
    for x in range(S):
        t = (x + y) / (2 * S - 2)
        px[x, y] = tuple(round(a + (b - a) * t) for a, b in zip(A, B))

# rounded-square mask
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=232, fill=255)

icon = Image.new("RGBA", (S, S), (0, 0, 0, 0))
icon.paste(grad, (0, 0), mask)

d = ImageDraw.Draw(icon)
W = (255, 255, 255, 255)
STROKE = 116                                   # thick enough to hold at 16px

# ">" prompt chevron
d.line([(272, 320), (520, 512), (272, 704)], fill=W, width=STROKE, joint="curve")
for cx, cy in ((272, 320), (520, 512), (272, 704)):          # round every cap/elbow
    d.ellipse([cx - STROKE // 2, cy - STROKE // 2,
               cx + STROKE // 2, cy + STROKE // 2], fill=W)

# cursor bar, sitting on the prompt's baseline
d.rounded_rectangle([596, 646, 792, 762], radius=58, fill=W)

icon.save("niyoj.ico", sizes=[(256, 256), (128, 128), (64, 64), (48, 48),
                              (40, 40), (32, 32), (24, 24), (20, 20), (16, 16)])
icon.resize((512, 512), Image.LANCZOS).save("logo.png")
print("wrote niyoj.ico + logo.png")
