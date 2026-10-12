#!/usr/bin/env python3
"""make-icon.py — draws Filmstamp.icns (a rubber stamp on a film-orange tile). Needs Pillow + iconutil.
Run only when the design changes; the resulting Filmstamp.icns is committed and used by build.sh."""
import os, subprocess, shutil, tempfile
from PIL import Image, ImageDraw

S = 4096                      # draw large, shrink for smoothness
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
k = S / 1024

def box(x0, y0, x1, y1): return [x0 * k, y0 * k, x1 * k, y1 * k]

# tile: 824px rounded square centred in the 1024 canvas, vertical gradient
tile = Image.new("RGBA", (S, S))
td = ImageDraw.Draw(tile)
top, bot = (255, 150, 60), (190, 70, 20)
for y in range(S):
    t = y / S
    td.line([(0, y), (S, y)], fill=tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)) + (255,))
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle(box(100, 100, 924, 924), radius=185 * k, fill=255)
img.paste(tile, (0, 0), mask)

d = ImageDraw.Draw(img)
cream, shade = (255, 244, 224), (60, 25, 10, 255)
# stamp: knob, handle, flange, base
d.ellipse(box(400, 200, 624, 400), fill=cream)
d.rounded_rectangle(box(455, 360, 569, 540), radius=30 * k, fill=cream)
d.rounded_rectangle(box(290, 520, 734, 610), radius=45 * k, fill=cream)
d.rounded_rectangle(box(250, 590, 774, 690), radius=30 * k, fill=(120, 40, 10, 255))
# ink impression: a film-strip with perforations
d.rounded_rectangle(box(220, 760, 804, 830), radius=10 * k, fill=shade)
for i in range(11):
    x = 245 + i * 52
    d.rounded_rectangle(box(x, 773, x + 28, 787), radius=3 * k, fill=cream)
    d.rounded_rectangle(box(x, 803, x + 28, 817), radius=3 * k, fill=cream)

img = img.resize((1024, 1024), Image.LANCZOS)
here = os.path.dirname(os.path.abspath(__file__))
tmp = tempfile.mkdtemp()
iconset = os.path.join(tmp, "Filmstamp.iconset")
os.mkdir(iconset)
for size in (16, 32, 128, 256, 512):
    img.resize((size, size), Image.LANCZOS).save(f"{iconset}/icon_{size}x{size}.png")
    img.resize((size * 2, size * 2), Image.LANCZOS).save(f"{iconset}/icon_{size}x{size}@2x.png")
subprocess.run(["iconutil", "-c", "icns", iconset, "-o", os.path.join(here, "Filmstamp.icns")], check=True)
img.save(os.path.join(tmp, "preview.png"))
print(os.path.join(tmp, "preview.png"))
