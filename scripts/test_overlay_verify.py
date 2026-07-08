#!/usr/bin/env python3
"""overlay_verify.py 的合成自測（#6）：驗證器自己必須先被驗證。

  1. 程式生成已知曲線圖（含同色網格線，模擬真實廠商圖）+ 完美 CSV → median ≈ 0
  2. 故意整體偏移 +2dB 的 CSV → 偏差必須被抓到（median ≈ 2）

跑法：python3 scripts/test_overlay_verify.py（無輸出錯誤 = 通過）
"""

import csv
import json
import math
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).parent
SCRIPT = HERE / "overlay_verify.py"

# 合成圖座標系：與測試 CSV 共用的 ground truth
W, H = 1200, 500
CAL_X = [(100, 20.0), (1100, 20000.0)]   # (px, Hz)
CAL_Y = [(50, 0.0), (450, -40.0)]        # (py, dB)


def to_pixel(freq, db):
    (px1, hz1), (px2, hz2) = CAL_X
    (py1, db1), (py2, db2) = CAL_Y
    lf1, lf2 = math.log10(hz1), math.log10(hz2)
    px = px1 + (math.log10(freq) - lf1) * (px2 - px1) / (lf2 - lf1)
    py = py1 + (db - db1) * (py2 - py1) / (db2 - db1)
    return px, py


def curve_fn(freq):
    """已知答案曲線：0dB@1kHz 的拋物線。"""
    return -6.0 * (math.log10(freq / 1000.0)) ** 2


def make_synthetic(path):
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    # 同色網格線（模擬真實廠商圖的污染源）
    for db in (10, 0, -10, -20, -30):
        _, py = to_pixel(1000, db)
        d.line([(100, py), (1100, py)], fill="black", width=1)
    for f in (50, 100, 1000, 10000):
        px, _ = to_pixel(f, 0)
        d.line([(px, 30), (px, 470)], fill="black", width=1)
    # 曲線（粗 3px — 比網格線寬）
    prev = None
    for x in range(100, 1101):
        freq = 10 ** (math.log10(20) + (x - 100) * (math.log10(20000) - math.log10(20)) / 1000)
        _, py = to_pixel(freq, curve_fn(freq))
        if prev:
            d.line([prev, (x, py)], fill="black", width=3)
        prev = (x, py)
    img.save(path)


def write_csv(path, offset_db=0.0):
    freqs = [20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000]
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["freq_hz", "level_db"])
        for fr in freqs:
            w.writerow([fr, round(curve_fn(fr) + offset_db, 3)])


def run_verify(img, csvf, out):
    r = subprocess.run(
        [sys.executable, str(SCRIPT), str(img), str(csvf),
         "--cal-x", "100,20", "1100,20000", "--cal-y", "50,0", "450,-40",
         "--out", str(out), "--json"],
        capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


# ---------- polar 合成（#7） ----------

PCX, PCY, PR0 = 300, 300, 250  # 圓心 + 0dB 外圈半徑；線性 5dB/50px → -20dB 圈 r=50


def polar_level(angle_deg):
    """已知答案曲線：類 cardioid，0°=0dB、90°≈-7、180°=-14。"""
    return -7.0 * (1 - math.cos(math.radians(angle_deg)))


def make_polar_synthetic(path):
    img = Image.new("RGB", (600, 600), "white")
    d = ImageDraw.Draw(img)
    # 同色網格：等距圈（0,-5,-10,-15,-20）+ 每 30° 輻條（污染源）
    for db in (0, -5, -10, -15, -20):
        r = PR0 + db * 10  # slope = -20dB/-200px → 10px/dB... r = 250 + db*10（-20→50）
        d.ellipse([PCX - r, PCY - r, PCX + r, PCY + r], outline="black", width=1)
    for ang in range(0, 360, 30):
        rad = math.radians(ang)
        d.line([(PCX, PCY), (PCX + 250 * math.sin(rad), PCY - 250 * math.cos(rad))], fill="black", width=1)
    # 已知曲線（粗 3px、0°=上、順時針）
    prev = None
    for ang in range(0, 361, 2):
        r = PR0 + polar_level(ang) * 10
        rad = math.radians(ang)
        p = (PCX + r * math.sin(rad), PCY - r * math.cos(rad))
        if prev:
            d.line([prev, p], fill="black", width=3)
        prev = p
    img.save(path)


def write_polar_csv(path, offset_db=0.0):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["angle_deg", "level_db"])
        for ang in range(0, 181, 30):
            w.writerow([ang, round(polar_level(ang) + offset_db, 3)])


def run_polar_verify(img, csvf, out):
    r = subprocess.run(
        [sys.executable, str(SCRIPT), str(img), str(csvf), "--polar",
         "--cal-center", f"{PCX},{PCY}", "--cal-rings", f"{PR0},0", "50,-20",
         "--zero-angle-deg", "0",
         "--out", str(out), "--json"],
        capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


def main():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        chart = td / "chart.png"
        make_synthetic(chart)

        # Test 1: 完美 CSV → 偏差 ≈ 0
        good = td / "good.csv"
        write_csv(good)
        rep = run_verify(chart, good, td / "o1.png")
        assert rep["matched"] == rep["points"], f"完美 CSV 應全部匹配: {rep}"
        assert rep["median_abs_dev_db"] < 0.3, f"完美 CSV median 應 ≈0: {rep['median_abs_dev_db']}"
        assert not rep["outliers_over_tolerance"], f"完美 CSV 不應有超標點: {rep}"
        assert (td / "o1.png").exists(), "overlay 應輸出"

        # Test 2: 整體 +2dB 偏移 → 必須被抓到
        bad = td / "bad.csv"
        write_csv(bad, offset_db=2.0)
        rep2 = run_verify(chart, bad, td / "o2.png")
        assert rep2["median_abs_dev_db"] > 1.5, f"+2dB 偏移應被量化抓到: {rep2['median_abs_dev_db']}"
        assert len(rep2["outliers_over_tolerance"]) >= 8, f"多數點應超標: {rep2}"

        # Test 3: polar 完美 CSV → 偏差 ≈ 0（同色圈/輻條網格下）
        pchart = td / "polar.png"
        make_polar_synthetic(pchart)
        pgood = td / "pgood.csv"
        write_polar_csv(pgood)
        rep3 = run_polar_verify(pchart, pgood, td / "o3.png")
        assert rep3["mode"] == "polar", rep3
        assert rep3["matched"] >= 6, f"polar 完美 CSV 應幾乎全匹配: {rep3}"
        assert rep3["median_abs_dev_db"] < 0.4, f"polar 完美 CSV median 應 ≈0: {rep3['median_abs_dev_db']}"

        # Test 4: polar +2dB 偏移 → 被抓到
        pbad = td / "pbad.csv"
        write_polar_csv(pbad, offset_db=2.0)
        rep4 = run_polar_verify(pchart, pbad, td / "o4.png")
        assert rep4["median_abs_dev_db"] > 1.5, f"polar +2dB 偏移應被抓: {rep4['median_abs_dev_db']}"

        # Test 5: 校準範圍外（負半徑）→ 列 gap 不 crash、不鏡射亂畫
        pneg = td / "pneg.csv"
        with open(pneg, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["angle_deg", "level_db"])
            w.writerow([0, 0])
            w.writerow([90, -40])  # 低於 -25 圓心外插 → 負半徑
        rep5 = run_polar_verify(pchart, pneg, td / "o5.png")
        assert rep5["gaps"] >= 1, f"負半徑點應列 gap: {rep5}"

    print("✓ overlay_verify selftest: 5/5 passed (FR ×2 + polar ×2 + negative-radius gap)")


if __name__ == "__main__":
    main()
