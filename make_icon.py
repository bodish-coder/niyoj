"""Generate niyoj.ico — upload arrow over a server bar, on a gradient tile.

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
d.rounded_rectangle([456, 296, 568, 688], radius=56, fill=W)          # arrow stem
d.line([(288, 516), (512, 292), (736, 516)], fill=W, width=112, joint="curve")
for cx, cy in ((288, 516), (736, 516)):                               # round the caps
    d.ellipse([cx - 56, cy - 56, cx + 56, cy + 56], fill=W)
d.rounded_rectangle([264, 764, 760, 872], radius=54, fill=W)          # server bar

icon.save("niyoj.ico", sizes=[(256, 256), (128, 128), (64, 64),
                                 (48, 48), (32, 32), (16, 16)])
icon.resize((512, 512), Image.LANCZOS).save("logo.png")
print("wrote niyoj.ico + logo.png")
