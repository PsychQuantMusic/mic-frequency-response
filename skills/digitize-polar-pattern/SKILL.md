---
name: digitize-polar-pattern
description: >
  Digitize a microphone polar pattern (directivity) chart into CSVs of (angle_deg, level_db) —
  one CSV per frequency — and ingest into the mic-frequency-response database. Use this skill
  whenever the user wants to extract data from a polar plot / directivity plot / 指向性圖 /
  極座標圖 on a mic datasheet, add polar pattern data for a microphone, or asks about the
  "circular chart" on a spec sheet — even when they only say "把 SM58 的指向性圖也拓進來"
  or "read the polar plot".
---

# Digitize Polar Pattern

把麥克風的 **polar pattern（極座標指向性圖）**還原成 `(angle_deg, level_db)` 數據，一個頻率一個 CSV。

與 `digitize-frequency-response` 共用「找官方圖 → AI 判讀 → 入庫」骨架，但**座標系不同**：
角度 θ 環繞、半徑 = dB（外圈 0dB、往圓心遞減，廠商圖幾乎都是 5dB/格線性）。

## 讀圖步驟

1. **取得官方 polar 圖**（同 FR skill 第一步；通常與頻響圖在同一張 datasheet）。高解析 crop + 放大。
2. **讀懂圖的慣例**（每張圖必核對，不假設）：
   - **0°（on-axis）在哪**：Shure 慣例 0° 在下、180° 在上；Audio-Technica 0° 在上。看角度標籤。
   - **半徑刻度**：外圈通常 0dB，內圈 -5/-10/-15/-20…（找 `SCALE IS 5 DECIBELS PER DIVISION` 類的說明）。核對圈距**等距**（= 線性半徑）。
   - **曲線 ↔ 頻率對應**：看 LEGEND 的線型（實線/虛線/點線/點虛線）。
3. **對稱性判斷**：麥克風繞主軸旋轉對稱 → 發表圖左右鏡像時，**記錄 0–180° 半平面即完整**（這不是編造，是圖的資訊量本來如此）。若圖明顯不對稱，兩側都讀（0–360）。
4. **沿角度取樣**：30° 步長（0,30,…,180）為基準；後瓣/null 附近形狀變化大可加密至 15°。逐點讀曲線半徑對應的 dB。
5. **誠實標記**：精度 ±1.5~2 dB（小圖多曲線交疊，比 FR 判讀粗）；達圖表下限的 null 記下限值並在 meta 註明（如「實際 ≤ -25」）；線型難分處寧可標注不確定。

## 產出 + 入庫

CSV（一頻率一檔）：`data/<brand>-<model>/polar-pattern--<freq>hz.csv`

```csv
angle_deg,level_db
0,0
30,-0.5
...
180,-14
```

`meta.yaml` 的 `curves[]` 加條目（`kind: polar-pattern`；FR 條目不加 kind = 缺省向後相容），
並加 `polar_read_note` 記取樣密度/精度/對稱慣例/0° 方向。

版權紀律同 FR skill：**原圖不進 repo**，只記來源。

## 座標工具

`digitizer/polar-coords.js` 的 `makePolarTransform(calib)` 提供 `(angle_deg, level_db) ↔ (px, py)` 雙向轉換
（校準 = 圓心 + 兩個已知 dB 圈半徑 + 0° 畫面方向 + 旋轉方向），供未來 pixel-trace 極座標版或
overlay 驗證使用。

## Overlay 自我驗證（強制，#7）

判讀完成後跑 `scripts/overlay_verify.py --polar`（校準 = 圓心 + 兩個已知 dB 圈半徑 + `--zero-angle-deg` + 旋向；
圓心/圈半徑可用「灰帶 bbox + 對向射線掃圈交點」程式測量），把 CSV 畫回原圖：

- **視覺為主**：`Read` overlay（多曲線用 `--marker-color` 亮色鏈接疊圖，luminance > 135），目檢標記貼曲線。
- **數字為輔**：量化在多曲線圖有 match 歧義（window 內鄰曲線/黑網格圈誤匹配）——solid 曲線較可靠；
  outlier 需量化與目測**一致**才修，僅量化超標視為歧義。輻條角度取樣已內建 ±3° 迴避。
- 修正後重驗，`median/max` 記進 meta 的 `polar_verification`。

## 已知限制（誠實邊界）

- 多曲線交疊處（尤其低頻近全向時）線型辨識易錯，讀值前先沿 LEGEND 線型從無交疊區段追進交疊區。
