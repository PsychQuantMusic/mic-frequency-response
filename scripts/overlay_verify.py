#!/usr/bin/env python3
"""AI 視覺判讀的自我驗證：把判讀的 CSV 曲線 overlay 回原圖，量化偏差。(#6, #7)

原理：原圖 = ground truth。把 CSV 反算回原圖像素座標：
  1. 疊上標記輸出 overlay.png（視覺比對 — 主要驗證）
  2. 量化：沿掃描方向 ±window 找「最寬的暗色 run」（曲線比網格線粗）→
     run 中心與 CSV 點的偏差 → 換算 dB（輔助驗證）

兩種模式：
  FR（預設）  ：CSV = freq_hz,level_db；log-X/linear-Y（同 digitizer/coords.js）；逐欄垂直掃描
  polar（--polar）：CSV = angle_deg,level_db；極座標（同 digitizer/polar-coords.js）；沿射線半徑掃描

用法：
  # FR
  python3 overlay_verify.py chart.png curve.csv \
      --cal-x PX1,HZ1 PX2,HZ2 --cal-y PY1,DB1 PY2,DB2 [options]
  # polar
  python3 overlay_verify.py chart.png polar.csv --polar \
      --cal-center CX,CY --cal-rings R1,DB1 R2,DB2 \
      [--zero-angle-deg 0] [--ccw] [options]
  # 校準輔助
  python3 overlay_verify.py chart.png --detect-lines

共通 options：[--out overlay.png] [--window 40] [--tolerance 1.0] [--json]
              [--marker-color R,G,B]（多曲線累積疊圖時用不同色，--out 串接前一輸出）

依賴：Pillow（dev-only script；web digitizer 本身仍零依賴）。
誠實邊界：量化以 median 為主 — 最寬 run 在網格交點/多曲線處可能誤匹配；polar 多曲線圖
（線型區分）只有 solid 曲線量化可靠，dotted/dashed 以視覺 overlay 為主。
"""

import argparse
import csv
import json
import math
import statistics
import sys

try:
    from PIL import Image, ImageDraw
except ImportError:
    sys.exit("需要 Pillow：pip3 install --user pillow")

DARK_THRESHOLD = 128  # 灰階 < 此值視為「暗」（黑曲線；SM58 類灰網格天然被排除）


# ---------- transforms ----------

def make_transform(cal_x, cal_y):
    """FR：(freq,db)->(px,py)，同 digitizer/coords.js 數學。回傳 (to_pixel, dslope)。"""
    (px1, hz1), (px2, hz2) = cal_x
    (py1, db1), (py2, db2) = cal_y
    if hz1 <= 0 or hz2 <= 0 or hz1 == hz2 or db1 == db2 or px1 == px2 or py1 == py2:
        sys.exit("校準點不合法（頻率需 >0 且相異；dB/px/py 需相異）")
    lf1 = math.log10(hz1)
    fslope = (math.log10(hz2) - lf1) / (px2 - px1)   # log10(Hz) per px
    dslope = (db2 - db1) / (py2 - py1)               # dB per py

    def to_pixel(freq_hz, level_db):
        return (px1 + (math.log10(freq_hz) - lf1) / fslope,
                py1 + (level_db - db1) / dslope)

    return to_pixel, dslope


def make_polar_transform(center, rings, zero_angle_deg, clockwise):
    """polar：(angle,db)->(px,py)，同 digitizer/polar-coords.js 數學。
    回傳 (to_pixel, to_ray, radial_slope)；to_ray(angle) 給 (cx, cy, ux, uy, r_expected(db))。"""
    (cx, cy) = center
    (r1, db1), (r2, db2) = rings
    if not all(math.isfinite(v) for v in (cx, cy, r1, db1, r2, db2, zero_angle_deg)):
        sys.exit("polar 校準需為 finite 數值")
    if r1 < 0 or r2 < 0 or r1 == r2 or db1 == db2:
        sys.exit("polar 校準不合法（半徑非負且相異；dB 相異）")
    slope = (db2 - db1) / (r2 - r1)  # dB per px（通常正：半徑越大 dB 越高）

    def unit(angle_deg):
        screen = zero_angle_deg + (angle_deg if clockwise else -angle_deg)
        rad = math.radians(screen)
        return math.sin(rad), -math.cos(rad)  # screen deg: 0=上（py 向下增）

    def r_of(level_db):
        return r1 + (level_db - db1) / slope

    def to_pixel(angle_deg, level_db):
        ux, uy = unit(angle_deg)
        r = r_of(level_db)
        return cx + r * ux, cy + r * uy

    def to_ray(angle_deg, level_db):
        ux, uy = unit(angle_deg)
        return cx, cy, ux, uy, r_of(level_db)

    return to_pixel, to_ray, slope


# ---------- sampling ----------

def widest_dark_run(samples):
    """在 (pos, lum) 序列找最寬暗 run，回傳 (center_pos, width)；無 → None。
    曲線通常比網格線粗，寬度比暗度更可靠。tie → 較暗者。"""
    runs = []
    start, dark_sum = None, 0
    for pos, lum in samples:
        if lum < DARK_THRESHOLD:
            if start is None:
                start, dark_sum = pos, 0
            dark_sum += lum
        else:
            if start is not None:
                runs.append((start, prev_pos, dark_sum))
                start = None
        prev_pos = pos
    if start is not None:
        runs.append((start, samples[-1][0], dark_sum))
    if not runs:
        return None
    best = max(runs, key=lambda r: (r[1] - r[0], -r[2]))
    return (best[0] + best[1]) / 2.0, best[1] - best[0] + 1


def column_samples(lum, w, h, x, y0, y1):
    """FR：x 欄 [y0,y1] 的 (y, value)。lum(x,y) 由呼叫端注入（灰階或彩色匹配）。"""
    x = max(0, min(w - 1, x))
    y0, y1 = max(0, y0), min(h - 1, y1)
    return [(y, lum(x, y)) for y in range(y0, y1 + 1)]


def ray_samples(lum, w, h, cx, cy, ux, uy, t0, t1):
    """polar：沿射線 (cx,cy)+t*(ux,uy)，t∈[t0,t1] 的 (t, value)。"""
    out = []
    for t in range(int(t0), int(t1) + 1):
        if t < 0:
            continue
        x = round(cx + t * ux)
        y = round(cy + t * uy)
        if 0 <= x < w and 0 <= y < h:
            out.append((t, lum(x, y)))
    return out


# ---------- detect-lines helper ----------

def detect_lines(img):
    """輔助模式：偵測長直網格線候選（供人/AI 對應標籤做校準）。"""
    gray = img.convert("L")
    w, h = gray.size
    px = gray.load()
    v_candidates = [x for x in range(w)
                    if sum(1 for y in range(0, h, 2) if px[x, y] < DARK_THRESHOLD) > (h // 2) * 0.6]
    h_candidates = [y for y in range(h)
                    if sum(1 for x in range(0, w, 2) if px[x, y] < DARK_THRESHOLD) > (w // 2) * 0.6]

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


# ---------- main ----------

def parse_pair(s, what):
    try:
        a, b = (float(v) for v in s.split(","))
        return a, b
    except ValueError:
        sys.exit(f"{what} 格式錯誤（需 A,B）: {s}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("csv_file", nargs="?")
    # FR calibration
    ap.add_argument("--cal-x", nargs=2, metavar="PX,HZ")
    ap.add_argument("--cal-y", nargs=2, metavar="PY,DB")
    # polar calibration
    ap.add_argument("--polar", action="store_true", help="polar pattern 模式（CSV = angle_deg,level_db）")
    ap.add_argument("--cal-center", metavar="CX,CY")
    ap.add_argument("--cal-rings", nargs=2, metavar="R,DB", help="兩個已知 dB 圈的半徑")
    ap.add_argument("--zero-angle-deg", type=float, default=0.0,
                    help="0°(on-axis) 的畫面方向（0=上、90=右、180=下；預設 0）")
    ap.add_argument("--ccw", action="store_true", help="角度逆時針增（預設順時針）")
    # common
    ap.add_argument("--out", default=None)
    ap.add_argument("--window", type=int, default=40)
    ap.add_argument("--tolerance", type=float, default=1.0)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--target-color", default=None,
                    help="彩色曲線模式：R,G,B——sampler 以 RGB 色距 ≤ --color-tolerance 判 match（取代灰階暗度）。顏色是最強的曲線/網格區分特徵（#13）。注意：鏈接疊圖時，舊標記色與本次 target 的 RGB 距離需 > --color-tolerance（灰階模式的 lum>135 規則不適用於色距），否則從乾淨原圖重疊")
    ap.add_argument("--color-tolerance", type=float, default=60.0)
    ap.add_argument("--marker-color", default="0,220,90", help="標記色 R,G,B（多曲線累積疊圖用不同色；luminance 需 >135，否則鏈接疊圖時舊標記會被當暗像素自污染量化）")
    ap.add_argument("--interpolated", action="store_true",
                    help="FR 模式：沿 CSV 的線性內插折線（log-f 空間）逐欄量偏差——「重繪保真度」的真正驗證（#14）。點數多寡不是指標，內插誤差才是；稀疏資料在點上驗證通過、點間失真的漏洞由此補上")
    ap.add_argument("--dark-threshold", type=int, default=None,
                    help="灰階暗度門檻覆蓋（預設 128）。高 dpi 細描邊抗鋸齒後偏淡（實測 AT2020 600dpi 部分欄 >128 → 量化 gap），掃描圖可調高至 ~170")
    ap.add_argument("--detect-lines", action="store_true")
    args = ap.parse_args()

    global DARK_THRESHOLD
    if args.dark_threshold is not None:
        if not (0 <= args.dark_threshold <= 255):
            sys.exit(f"--dark-threshold 需在 0-255（got {args.dark_threshold}）")
        DARK_THRESHOLD = args.dark_threshold
    img = Image.open(args.image).convert("RGB")
    if args.detect_lines:
        detect_lines(img)
        return

    if not args.csv_file:
        ap.error("需要 csv_file（或用 --detect-lines）")

    try:
        marker = tuple(int(v) for v in args.marker_color.split(","))
        assert len(marker) == 3
    except (ValueError, AssertionError):
        sys.exit(f"--marker-color 格式錯誤（需 R,G,B）: {args.marker_color}")
    _lum = 0.299 * marker[0] + 0.587 * marker[1] + 0.114 * marker[2]
    if _lum < 135:
        print(f"⚠ marker 色 luminance {_lum:.0f} < 135 —— 鏈接疊圖時舊標記會被當暗像素自污染量化", file=sys.stderr)

    # --- mode setup ---
    if args.polar:
        if not (args.cal_center and args.cal_rings):
            ap.error("--polar 需要 --cal-center + --cal-rings")
        center = parse_pair(args.cal_center, "--cal-center")
        rings = [parse_pair(s, "--cal-rings") for s in args.cal_rings]
        to_pixel, to_ray, slope = make_polar_transform(center, rings, args.zero_angle_deg, not args.ccw)
        col_names = ("angle_deg", "level_db")
    else:
        if not (args.cal_x and args.cal_y):
            ap.error("FR 模式需要 --cal-x + --cal-y（或用 --polar）")
        cal_x = [parse_pair(s, "--cal-x") for s in args.cal_x]
        cal_y = [parse_pair(s, "--cal-y") for s in args.cal_y]
        to_pixel, dslope = make_transform(cal_x, cal_y)
        col_names = ("freq_hz", "level_db")

    # --- read CSV ---
    points = []
    with open(args.csv_file, newline="") as f:
        for i, row in enumerate(csv.DictReader(f), start=2):
            try:
                a, b = float(row[col_names[0]]), float(row[col_names[1]])
            except (KeyError, TypeError, ValueError):
                sys.exit(f"CSV 第 {i} 行格式錯誤（需 {','.join(col_names)} 數值）: {row}")
            if not (math.isfinite(a) and math.isfinite(b)):
                sys.exit(f"CSV 第 {i} 行需為 finite: {row}")
            if not args.polar and a <= 0:
                sys.exit(f"CSV 第 {i} 行不合法（freq 需 >0）: {row}")
            points.append((a, b))
    if not points:
        sys.exit("CSV 無資料點")

    w_img, h_img = img.size
    if args.target_color:
        try:
            tc = tuple(int(v) for v in args.target_color.split(","))
            assert len(tc) == 3
        except (ValueError, AssertionError):
            sys.exit(f"--target-color 格式錯誤（需 R,G,B）: {args.target_color}")
        rgb_px = img.copy().load()  # 快照：markers 畫在 img 上，取樣不得看到自己的標記（同灰階路徑的 convert 快照語意）
        ctol = args.color_tolerance
        def lum(x, y):
            r, g, b = rgb_px[x, y][:3]
            d = ((r - tc[0]) ** 2 + (g - tc[1]) ** 2 + (b - tc[2]) ** 2) ** 0.5
            return 0 if d <= ctol else 255  # match → 視為「暗」
    else:
        gray_img = img.convert("L")
        gray_px = gray_img.load()
        def lum(x, y):
            return gray_px[x, y]
    draw = ImageDraw.Draw(img)
    deviations, gaps, ambiguous = [], [], []

    for a, b in points:
        px, py = to_pixel(a, b)
        xi, yi = round(px), round(py)
        draw.line([(xi - 4, yi), (xi + 4, yi)], fill=marker, width=1)
        draw.line([(xi, yi - 4), (xi, yi + 4)], fill=marker, width=1)

        if args.polar:
            # 輻條迴避：取樣角若正落在輻條（多為 30° 整數倍）上，沿射線 = 沿輻條掃 →
            # 巨型/融合 run。候選 ±3°（曲線 3° 內幾乎不變、輻條是固定角度細線）擇優。
            found, run_w_limit = None, 15  # 融合段（輻條+曲線+圈）遠寬於曲線 stroke
            amb_seen = None
            used_delta = 0.0
            # 校準範圍外（dB 低於圓心外插）→ 負半徑會把標記鏡射到反方向，直接列 gap
            _, _, _, _, r_check = to_ray(a, b)
            if r_check < 0:
                gaps.append({col_names[0]: a, "level_db": b, "note": "below chart center (negative radius)"})
                continue
            for delta in (0.0, 3.0, -3.0):
                cx, cy, ux, uy, r_exp = to_ray(a + delta, b)
                samples = ray_samples(lum, w_img, h_img, cx, cy, ux, uy, r_exp - args.window, r_exp + args.window)
                cand = widest_dark_run(samples) if samples else None
                if cand is None:
                    continue
                if cand[1] >= 0.9 * (2 * args.window + 1):
                    amb_seen = cand
                    continue
                if cand[1] > run_w_limit:
                    amb_seen = amb_seen or cand
                    continue
                found = cand
                expected_pos, dev_slope = r_exp, slope
                used_delta = delta
                break
            if found is None:
                if amb_seen is not None:
                    ambiguous.append({col_names[0]: a, "level_db": b, "run_width": amb_seen[1]})
                else:
                    gaps.append({col_names[0]: a, "level_db": b})
                continue
            run_pos, run_w = found
            if used_delta:
                # 偏移角量化以「原角度的 dB」比「偏移角的曲線」——陡峭段為近似值，
                # 不可作為單獨修值依據（skill 紀律：量化+目測一致才修）
                extra = {"angle_offset_deg": used_delta}
            else:
                extra = {}
        else:
            extra = {}
            samples = column_samples(lum, w_img, h_img, xi, yi - args.window, yi + args.window)
            expected_pos, dev_slope = py, dslope
            found = widest_dark_run(samples) if samples else None
            if found is None:
                gaps.append({col_names[0]: a, "level_db": b})
                continue
            run_pos, run_w = found
            # run 幾乎吃滿搜尋窗 → 疑似垂直線/色塊（中心≈窗中心會給假 0 偏差）→ ambiguous
            if run_w >= 0.9 * (2 * args.window + 1):
                ambiguous.append({col_names[0]: a, "level_db": b, "run_width": run_w})
                continue
        deviations.append({
            col_names[0]: a, "level_db": b,
            "dev_db": round((run_pos - expected_pos) * dev_slope, 3),
            **extra,
        })

    interp_devs = []
    interp_gaps = 0
    interp_ambiguous = 0
    if args.interpolated and not args.polar:
        # 沿內插折線逐欄：對每個整數 px（首尾 CSV 點之間），內插出預期 py，量測欄內最寬暗 run 偏差
        import bisect
        pts_sorted = sorted(points)
        if len(pts_sorted) < 2:
            sys.exit("--interpolated 需要 ≥2 個 CSV 點")
        lfs = [math.log10(f) for f, _ in pts_sorted]
        px_of = [to_pixel(f, d)[0] for f, d in pts_sorted]
        if any(px_of[i + 1] <= px_of[i] for i in range(len(px_of) - 1)):
            sys.exit("--interpolated: CSV/校準必須產生嚴格遞增的 x 像素（reversed 校準或重複頻率？）")
        x_lo, x_hi = int(math.ceil(px_of[0])), int(math.floor(px_of[-1]))
        for xi in range(x_lo, x_hi + 1):
            # 反推該欄頻率 → 內插 dB
            # px 對 log-f 線性，直接在 px 空間內插
            j = bisect.bisect_right(px_of, xi) - 1
            j = max(0, min(j, len(pts_sorted) - 2))
            t = (xi - px_of[j]) / (px_of[j + 1] - px_of[j]) if px_of[j + 1] != px_of[j] else 0
            db_i = pts_sorted[j][1] + t * (pts_sorted[j + 1][1] - pts_sorted[j][1])
            py_i = to_pixel(pts_sorted[j][0], db_i)[1]  # y 只依 dB
            col = column_samples(lum, w_img, h_img, xi, round(py_i) - args.window, round(py_i) + args.window)
            found = widest_dark_run(col) if col else None
            if found is None:
                interp_gaps += 1
                continue
            run_pos, run_w = found
            if run_w >= 0.9 * (2 * args.window + 1):
                interp_ambiguous += 1  # 誠實計數：既非 gap 也非量測（垂直線/粗線佔滿窗）
                continue
            interp_devs.append(abs((run_pos - py_i) * dslope))
        draw_interp = ImageDraw.Draw(img)
        prev_xy = None
        for f, d in pts_sorted:
            xy = tuple(map(round, to_pixel(f, d)))
            if prev_xy:
                draw_interp.line([prev_xy, xy], fill=marker, width=1)
            prev_xy = xy

    out_path = args.out or (args.image + ".overlay.png")
    img.save(out_path)

    abs_devs = sorted(abs(d["dev_db"]) for d in deviations)
    median = round(statistics.median(abs_devs), 3) if abs_devs else None
    mx = abs_devs[-1] if abs_devs else None
    outliers = [d for d in deviations if abs(d["dev_db"]) > args.tolerance]

    report = {
        "mode": "polar" if args.polar else "fr",
        **({"interpolated": {
            "columns_measured": len(interp_devs), "gaps": interp_gaps,
            "ambiguous": interp_ambiguous,
            "median_abs_dev_db": round(statistics.median(interp_devs), 3) if interp_devs else None,
            "max_abs_dev_db": round(max(interp_devs), 3) if interp_devs else None,
        }} if args.interpolated and not args.polar else {}),
        "points": len(points), "matched": len(deviations), "gaps": len(gaps),
        "ambiguous_vertical_runs": ambiguous,
        "median_abs_dev_db": median, "max_abs_dev_db": mx,
        "outliers_over_tolerance": outliers, "tolerance_db": args.tolerance,
        "overlay": out_path,
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"overlay: {out_path}")
        print(f"points={report['points']} matched={report['matched']} gaps={report['gaps']} ambiguous={len(ambiguous)}")
        print(f"median |dev| = {median} dB, max |dev| = {mx} dB")
        if args.interpolated and not args.polar:
            it = report["interpolated"]
            print(f"interpolated: {it['columns_measured']} measured, median {it['median_abs_dev_db']} dB, max {it['max_abs_dev_db']} dB, gaps {it['gaps']}, ambiguous {it['ambiguous']}")
        if outliers:
            print(f"⚠ {len(outliers)} 點超過 ±{args.tolerance} dB（目檢 overlay 判定真偏差 vs 誤匹配）：")
            for d in outliers:
                print(f"   {d[col_names[0]]} {'°' if args.polar else 'Hz'}: dev {d['dev_db']:+.2f} dB")
        else:
            print(f"✓ 全部點在 ±{args.tolerance} dB 內")


if __name__ == "__main__":
    main()
