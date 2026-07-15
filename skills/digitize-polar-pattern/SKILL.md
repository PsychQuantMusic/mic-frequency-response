---
name: digitize-polar-pattern
description: >
  Use when adding, extracting, or verifying microphone polar-pattern or directivity data from
  manufacturer numeric files or official vector and raster charts, especially for angular
  calibration, curve-frequency identity, reproducible tracing, provenance, and ingestion into
  the mic-frequency-response database.
---

# Digitize Polar Pattern

把原廠發布的極座標數值，或官方 polar pattern 中可重現解析的曲線，轉成 `(angle_deg, level_db)` CSV；一個頻率一個檔案。

## 收錄契約

每條曲線只能使用：

| `data_origin` | `digitization_method` | 資料來源 |
|---|---|---|
| `manufacturer-numeric` | `manufacturer-values` | 原廠直接發布的角度／dB 數值 |
| `official-published-curve` | `vector-path-extraction` | 官方向量圖中可識別的 polar path |
| `official-published-curve` | `seeded-pixel-trace` | 官方點陣圖中可重現且可穩定分離的曲線 |

人工或 AI 沿角度目測讀值是 manual estimate，不得進正式 `data/`。現有座標轉換與 overlay 工具本身不是 extractor；如果沒有可重現的 curve trace，就不能入庫。

## Workflow

### 1. 取得官方來源並辨識圖表

從原廠產品頁、規格書、使用手冊或官方資產取得 polar 圖，並記錄 URL、頁碼／圖名及取得日期。原圖只放 scratch 或 repo 外，不 commit。

每張圖都要從標籤與 LEGEND 確認：

- 0° on-axis 在哪個畫面方向，以及角度增加方向。
- 外圈與內圈的 dB 值；不要假設一定是 5 dB／格。
- 每條線型／顏色對應的頻率。
- 圖是完整 0–360°、左右半圖，或其他佈局。

Overlay 只能驗證點是否貼線，不能證明曲線對應哪個頻率。若頻率身分無法從圖例與可追蹤的線段確認，停止入庫。

### 2. 選擇可重現方法

#### 原廠數值

忠實保留原廠角度、dB、頻率與基準定義；不要自行增加角度取樣點。

```yaml
data_origin: manufacturer-numeric
digitization_method: manufacturer-values
```

#### 官方向量路徑

只有在 SVG／PDF 內能以 path、style、group 或其他可重現識別方式對應到單一頻率時才解析。套用所有 transform，但不修改 geometry；排除網格、標籤與其他頻率曲線。以渲染 overlay 或第二條 path parser 交叉驗證。

```yaml
data_origin: official-published-curve
digitization_method: vector-path-extraction
```

#### 官方點陣曲線

只有在現有或另行實作的 extractor 能以固定輸入、校準、種子與參數重跑出同一條曲線時才可使用。必要條件：

1. 圓心、兩個已知 dB 圈半徑、0° 畫面方向與旋向可由圖表標籤校準。
2. 目標頻率曲線可由顏色、線型與連續性穩定隔離。
3. Trace 不會在交疊處任意跳到其他頻率或網格。
4. 全路徑能以 overlay 或獨立像素量測驗證。

```yaml
data_origin: official-published-curve
digitization_method: seeded-pixel-trace
```

`digitizer/polar-coords.js` 的 `makePolarTransform(calib)` 只提供 `(angle_deg, level_db) ↔ (px, py)` 座標轉換；`scripts/overlay_verify.py --polar` 只負責驗證。兩者不會自動把人工目測點變成可重現 trace。

目前工具若不能穩定抽出目標 polar 曲線，請明確回報「無可重現 trace，不能入庫」，不要改用 15°／30° 人工取樣。

### 3. 不補造未觀測資料

- 官方圖只提供 0–180° 時，只保存實際發布的角度範圍，不鏡射成 0–360° 新資料點；在 metadata 記錄圖表慣例。
- 不在角度間插值建立新觀測點，不為後瓣或 null 補形狀，也不外推。
- 曲線碰到圖表 dB 下限時，不能把圖表下限當成精確值。現行兩欄 CSV 無法表示 censored value，該不確定區段不要寫成精確資料。
- 多曲線交疊處若無法穩定分離，保留 gap 或整條不入庫；不能以肉眼猜線。

### 4. Overlay 驗證

執行 `scripts/overlay_verify.py --polar`，使用圓心、兩個 dB 圈、`--zero-angle-deg` 與旋向校準：

- 在來源圖上重繪 trace，逐段檢查是否貼住正確頻率曲線。
- 記錄 median、max、gap 與 ambiguous；有歧義不等於通過。
- 多曲線圖的 nearest-pixel 統計可能匹配鄰線或網格，必須以獨立的曲線隔離與頻率身分證據覆核。
- 把方法、參數與結果寫入 `polar_verification`。

### 5. 產出與 metadata

```csv
angle_deg,level_db
0,0
30,-0.5
60,-3.2
```

```yaml
source:
  url: https://manufacturer.example/manual.pdf
  page: 8
  chart: "Polar Pattern, 1 kHz"
  retrieved: 2026-07-15
curves:
  - file: polar-pattern--1000hz.csv
    kind: polar-pattern
    condition: "1 kHz as published; 0 degrees on-axis"
    data_origin: official-published-curve
    digitization_method: vector-path-extraction
polar_read_note: "record angular domain, zero direction, rotation, and dB scale"
polar_verification:
  method: "overlay_verify.py --polar plus independent path identity check"
  result: "record points, median/max deviation, gaps, and ambiguities"
```

`anchors.yaml` 只保存已收錄 CSV 的回歸快照，不是原廠獨立證據。

## Fail-closed 檢查

下列任一條成立，就不要建立正式 polar CSV：

- 沒有官方來源。
- 曲線與頻率、0° 方向或 dB 圈刻度無法確認。
- 只能人工／AI 逐角度目測。
- 向量 path 或點陣 trace 無法以固定方法重現。
- 必須鏡射、插值、補 null 或外推才能形成完整曲線。

停止時記錄無法解析的原因，等待更好的官方數值、向量來源或可靠 extractor。
