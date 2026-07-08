#!/usr/bin/env python3
"""把圖轉成 RGBA 原始位元組，供 digitizer/cli.js headless trace 用（#12）。

用法：python3 scripts/dump_pixels.py chart.png /tmp/chart.bin
輸出：stdout 印 "W H"（餵給 cli.js 的 --size）
"""

import sys

try:
    from PIL import Image
except ImportError:
    sys.exit("需要 Pillow：pip3 install --user pillow")

if len(sys.argv) != 3:
    sys.exit("用法: dump_pixels.py <image> <out.bin>")

img = Image.open(sys.argv[1]).convert("RGBA")
with open(sys.argv[2], "wb") as f:
    f.write(img.tobytes())
print(img.size[0], img.size[1])
