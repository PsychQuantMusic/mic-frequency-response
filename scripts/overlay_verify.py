#!/usr/bin/env python3
"""AI 視覺判讀的自我驗證：把判讀的 CSV 曲線 overlay 回原圖，量化偏差。(#6)

原理：原圖 = ground truth。用與 digitizer/coords.js 相同的 log-X / linear-Y 數學，
把 CSV 的 (freq_hz, level_db) 反算回原圖像素座標：
  1. 疊上綠色標記輸出 overlay.png（視覺比對 — 主要驗證）
  2. 量化：每點在該欄 ±window 內找「最寬的暗色 run」（曲線比網格線粗）→
     run 中心與 CSV 點的 py 差 → 換算 dB 偏差（輔助驗證）

用法：
  python3 overlay_verify.py chart.png curve.csv \
      --cal-x PX1,HZ1 PX2,HZ2 --cal-y PY1,DB1 PY2,DB2 \
      [--out overlay.png] [--window 40] [--tolerance 1.0] [--json]

  python3 overlay_verify.py chart.png --detect-lines   # 輔助：印網格線候選座標

依賴：Pillow（dev-only script；web digitizer 本身仍零依賴）。
誠實邊界：量化以 median 為主 — 欄內最寬 run 在網格交點/多曲線處可能誤匹配，
超標點應回到 overlay.png 目檢判定（視覺主、數字輔）。
"""

import argparse
import csv
import json
import math
import sys

try:
    from PIL import Image, ImageDraw
except ImportError:
    sys.exit("需要 Pillow：pip3 install --user pillow")

DARK_THRESHOLD = 128  # 灰階 < 此值視為「暗」（曲線/網格候選）


def make_transform(cal_x, cal_y):
    """回傳 (freq,db)->(px,py) 與 (px,py)->(freq,db)，同 digitizer/coords.js 數學。"""
    (px1, hz1), (px2, hz2) = cal_x
    (py1, db1), (py2, db2) = cal_y
    if hz1 <= 0 or hz2 <= 0 or hz1 == hz2 or db1 == db2 or px1 == px2 or py1 == py2:
        sys.exit("校準點不合法（頻率需 >0 且相異；dB/px/py 需相異）")
    lf1, lf2 = math.log10(hz1), math.log10(hz2)
    fslope = (lf2 - lf1) / (px2 - px1)   # log10(Hz) per px
    dslope = (db2 - db1) / (py2 - py1)   # dB per py

    def to_pixel(freq_hz, level_db):
        px = px1 + (math.log10(freq_hz) - lf1) / fslope
        py = py1 + (level_db - db1) / dslope
        return px, py

    return to_pixel, dslope


def luminance_column(gray, x, y0, y1):
    """回傳 x 欄 [y0,y1] 的 (y, lum) 列表。"""
    w, h = gray.size
    x = max(0, min(w - 1, x))
    y0, y1 = max(0, y0), min(h - 1, y1)
    px = gray.load()
    return [(y, px[x, y]) for y in range(y0, y1 + 1)]


def widest_dark_run(col_pixels):
    """欄內找最寬的暗色 run，回傳 (center_y, width)；無 → None。
    曲線通常比網格線粗，寬度比暗度更可靠。tie → 較暗者。"""
    runs = []
    start, dark_sum = None, 0
    for y, lum in col_pixels:
        if lum < DARK_THRESHOLD:
            if start is None:
                start, dark_sum = y, 0
            dark_sum += lum
        else:
            if start is not None:
                runs.append((start, y - 1, dark_sum))
                start = None
    if start is not None:
        runs.append((start, col_pixels[-1][0], dark_sum))
    if not runs:
        return None
    best = max(runs, key=lambda r: (r[1] - r[0], -r[2]))  # 最寬；tie 取較暗
    return (best[0] + best[1]) / 2.0, best[1] - best[0] + 1


def detect_lines(img):
    """輔助模式：偵測長直網格線候選（供人/AI 對應標籤做校準）。"""
    gray = img.convert("L")
    w, h = gray.size
    px = gray.load()
    v_candidates = []
    for x in range(w):
        dark = sum(1 for y in range(0, h, 2) if px[x, y] < DARK_THRESHOLD)
        if dark > (h // 2) * 0.6:
            v_candidates.append(x)
    h_candidates = []
    for y in range(h):
        dark = sum(1 for x in range(0, w, 2) if px[x, y] < DARK_THRESHOLD)
        if dark > (w // 2) * 0.6:
            h_candidates.append(y)

    def group(xs):
        groups, cur = [], []
        for v in xs:
            if cur and v - cur[-1] > 2:
                groups.append(sum(cur) // len(cur))
                cur = []
            cur.append(v)
        if cur:
            groups.append(sum(cur) // len(cur))
        return groups

    print("vertical lines (x):", group(v_candidates))
    print("horizontal lines (y):", group(h_candidates))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("csv_file", nargs="?")
    ap.add_argument("--cal-x", nargs=2, metavar="PX,HZ", help="兩個 X 校準點")
    ap.add_argument("--cal-y", nargs=2, metavar="PY,DB", help="兩個 Y 校準點")
    ap.add_argument("--out", default=None, help="overlay 輸出路徑（預設 <image>.overlay.png）")
    ap.add_argument("--window", type=int, default=40, help="欄內搜尋半徑 px（預設 40）")
    ap.add_argument("--tolerance", type=float, default=1.0, help="超標門檻 dB（預設 1.0）")
    ap.add_argument("--json", action="store_true", help="輸出 JSON 統計")
    ap.add_argument("--detect-lines", action="store_true", help="只印網格線候選座標")
    args = ap.parse_args()

    img = Image.open(args.image).convert("RGB")
    if args.detect_lines:
        detect_lines(img)
        return

    if not (args.csv_file and args.cal_x and args.cal_y):
        ap.error("需要 csv_file + --cal-x + --cal-y（或用 --detect-lines）")

    cal_x = [tuple(float(v) for v in s.split(",")) for s in args.cal_x]
    cal_y = [tuple(float(v) for v in s.split(",")) for s in args.cal_y]
    to_pixel, dslope = make_transform(cal_x, cal_y)

    points = []
    with open(args.csv_file, newline="") as f:
        for row in csv.DictReader(f):
            points.append((float(row["freq_hz"]), float(row["level_db"])))
    if not points:
        sys.exit("CSV 無資料點")

    gray = img.convert("L")
    draw = ImageDraw.Draw(img)
    deviations, gaps = [], []

    for freq, db in points:
        px, py = to_pixel(freq, db)
        xi, yi = round(px), round(py)
        # 綠色十字標記（視覺比對用）
        draw.line([(xi - 4, yi), (xi + 4, yi)], fill=(0, 200, 80), width=1)
        draw.line([(xi, yi - 4), (xi, yi + 4)], fill=(0, 200, 80), width=1)
        # 量化：欄內 ±window 找最寬暗 run
        col = luminance_column(gray, xi, yi - args.window, yi + args.window)
        found = widest_dark_run(col)
        if found is None:
            gaps.append({"freq_hz": freq, "level_db": db})
            continue
        run_y, _w = found
        deviations.append({
            "freq_hz": freq, "level_db": db,
            "dev_db": round((run_y - py) * dslope, 3),
        })

    out_path = args.out or (args.image + ".overlay.png")
    img.save(out_path)

    abs_devs = sorted(abs(d["dev_db"]) for d in deviations)
    median = abs_devs[len(abs_devs) // 2] if abs_devs else None
    mx = abs_devs[-1] if abs_devs else None
    outliers = [d for d in deviations if abs(d["dev_db"]) > args.tolerance]

    report = {
        "points": len(points), "matched": len(deviations), "gaps": len(gaps),
        "median_abs_dev_db": median, "max_abs_dev_db": mx,
        "outliers_over_tolerance": outliers, "tolerance_db": args.tolerance,
        "overlay": out_path,
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"overlay: {out_path}")
        print(f"points={report['points']} matched={report['matched']} gaps={report['gaps']}")
        print(f"median |dev| = {median} dB, max |dev| = {mx} dB")
        if outliers:
            print(f"⚠ {len(outliers)} 點超過 ±{args.tolerance} dB（目檢 overlay 判定真偏差 vs 網格誤匹配）：")
            for d in outliers:
                print(f"   {d['freq_hz']} Hz: dev {d['dev_db']:+.2f} dB")
        else:
            print(f"✓ 全部點在 ±{args.tolerance} dB 內")


if __name__ == "__main__":
    main()
