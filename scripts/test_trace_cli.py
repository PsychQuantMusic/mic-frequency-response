#!/usr/bin/env python3
"""headless trace CLI 的 smoke test（#12）：PNG → dump_pixels → cli.js → CSV 對照已知曲線。

跑法：python3 scripts/test_trace_cli.py
"""

import csv
import io
import math
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).parent
ROOT = HERE.parent

# 座標系：20Hz@px100 → 20kHz@px1100；0dB@py50 → -40dB@py450（10px/dB）
W, H = 1200, 500


def truth(freq):
    return -6.0 * (math.log10(freq / 1000.0)) ** 2


def to_pixel(freq, db):
    px = 100 + (math.log10(freq) - math.log10(20)) * 1000 / 3
    py = 50 + db * -10
    return px, py


def main():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        # 合成圖：已知曲線 + 同色網格線（真實情境）
        img = Image.new("RGB", (W, H), "white")
        d = ImageDraw.Draw(img)
        for db in (0, -10, -20, -30):
            _, py = to_pixel(1000, db)
            d.line([(100, py), (1100, py)], fill="black", width=1)
        prev = None
        for x in range(100, 1101):
            freq = 10 ** (math.log10(20) + (x - 100) * 3 / 1000)
            p = (x, to_pixel(freq, truth(freq))[1])
            if prev:
                d.line([prev, p], fill="black", width=3)
            prev = p
        chart = td / "chart.png"
        img.save(chart)

        # dump → cli
        r1 = subprocess.run([sys.executable, str(HERE / "dump_pixels.py"), str(chart), str(td / "chart.bin")],
                            capture_output=True, text=True, check=True)
        w, h = r1.stdout.split()
        seed_px, seed_py = 600, round(to_pixel(10 ** (math.log10(20) + 500 * 3 / 1000), truth(10 ** (math.log10(20) + 500 * 3 / 1000)))[1])
        r2 = subprocess.run(
            ["node", str(ROOT / "digitizer" / "cli.js"),
             "--bin", str(td / "chart.bin"), "--size", f"{w}x{h}",
             "--cal-x", "100,20", "1100,20000", "--cal-y", "50,0", "450,-40",
             "--seed", f"{seed_px},{seed_py}", "--x-range", "100,1100"],
            capture_output=True, text=True, check=True)

        rows = list(csv.DictReader(io.StringIO(r2.stdout)))
        assert len(rows) >= 950, f"應近乎全數還原（got {len(rows)}）"
        max_err = 0.0
        for row in rows:
            f, db = float(row["freq_hz"]), float(row["level_db"])
            max_err = max(max_err, abs(db - truth(f)))
        assert max_err < 0.5, f"同色網格下誤差應 <0.5dB（got {max_err:.2f}）"

        # 錯誤路徑：種子點在空白處 → 非零退出 + 訊息
        r3 = subprocess.run(
            ["node", str(ROOT / "digitizer" / "cli.js"),
             "--bin", str(td / "chart.bin"), "--size", f"{w}x{h}",
             "--cal-x", "100,20", "1100,20000", "--cal-y", "50,0", "450,-40",
             "--seed", "600,490"],
            capture_output=True, text=True)
        assert r3.returncode != 0 and "trace 回空" in r3.stderr, f"off-curve 種子應誠實失敗: {r3.stderr}"

    print(f"✓ trace CLI smoke: {len(rows)} pts, max err {max_err:.2f} dB, off-curve 誠實失敗")


if __name__ == "__main__":
    main()
