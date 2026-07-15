---
name: digitize-frequency-response
description: >
  Use when adding, extracting, or verifying microphone or audio-equipment frequency-response
  data from manufacturer numeric files or official PDF, SVG, and raster charts, especially for
  provenance classification, multi-curve isolation, calibration, overlay verification, and
  ingestion into the mic-frequency-response database.
---

# Digitize Frequency Response

把原廠發布的頻率響應數值，或官方圖表中可重現解析的曲線，轉成 `(freq_hz, level_db)` CSV。

## 核心原則

正式 `data/` 只接受兩種證據：

| `data_origin` | `digitization_method` | 資料來源 |
|---|---|---|
| `manufacturer-numeric` | `manufacturer-values` | 原廠直接發布的 CSV、FRD 或數值表 |
| `official-published-curve` | `vector-path-extraction` | 官方 SVG／PDF 的可識別向量路徑 |
| `official-published-curve` | `seeded-pixel-trace` | 官方點陣圖的可重現 seeded trace |

由官方圖表轉出的 CSV 是 **digitized data**，不是原廠 raw data。人工或 AI 逐點目測屬於 manual estimate，不能進正式 `data/`，也不能在 deterministic trace 失敗時當 fallback。

## Workflow

### 1. 取得並確認官方來源

- 型號已知時，先找原廠產品頁、規格書、使用手冊或官方資產 CDN。優先使用原廠數值，其次是官方向量圖，最後才是官方點陣圖。
- 使用者提供 URL、PDF 或截圖時，先確認它能追溯到原廠發布頁面；第三方重繪圖不能當正式來源。
- 在 `source` 記錄 URL、頁碼／資產 URL、圖名與取得日期。
- PDF、SVG、PNG 與截圖只放 job scratch 或 repo 外，絕不 commit。

官方下載位置會改動。從現行產品頁找資產連結；規格表沒有曲線時，再查同型號的官方手冊或其他官方文件。找不到官方來源就停止，不以第三方圖補位。

### 2. 先辨識曲線與有效範圍

在抽取前先從標題、LEGEND 與產品設定確認：

- 哪條是目標曲線，以及 condition、角度、距離、指向性、濾波器／開關狀態。
- 原廠標示的 `frequency_range_hz`。
- 軸標籤的實際值。X 軸通常為 log Hz，Y 軸通常為 linear dB，但每張圖都要核對。

多曲線圖只能抽取身分可證明且可穩定隔離的曲線。常見 canonical typical 選擇：

| 圖型 | typical 曲線 |
|---|---|
| proximity family | 原廠標成 on-axis typical／最遠場的曲線；condition 明記距離 |
| presence switch | 原廠標示的 normal／moderate 設定 |
| dual voicing | general／flat 設定 |
| low-cut／bass switch | flat、未啟用 low-cut 的曲線 |
| tolerance band | nominal 實線，不取容差帶 |
| off-axis family | 0° on-axis 曲線 |

若圖例不足以證明曲線身分，停止入庫。Overlay 能驗證幾何貼合，不能證明你追到的是哪個 condition。

### 3. 選擇可重現方法

#### 原廠數值

忠實轉成兩欄 CSV，不改造資料密度，也不把單位或基準猜成另一種定義。使用：

```yaml
data_origin: manufacturer-numeric
digitization_method: manufacturer-values
```

#### 官方向量路徑

辨識目標 path 與其 transform，只解析或重新上色該 path；不得改 geometry。容差帶、其他 angle／setting 與裝飾路徑必須排除。可同時：

1. 解析 Bezier path 並套用 transform。
2. 將同一路徑渲染成點陣圖後 trace。
3. 比較兩路輸出，作為獨立交叉驗證。

使用：

```yaml
data_origin: official-published-curve
digitization_method: vector-path-extraction
```

#### 官方點陣曲線

使用固定校準、種子與參數的 seeded pixel trace。人工操作只用來設定校準與種子，不直接產生資料點。

1. 高解析渲染後，crop 到 plot area，避免圖外文字成為 trace decoy。
2. 用 `scripts/overlay_verify.py --detect-lines` 找格線候選；從實際軸標籤讀兩個 X 與兩個 Y 校準值。
3. 用第三條已知標籤線覆核校準。log 間距本身不能決定標籤數值。
4. 在目標曲線未碰到網格或其他曲線處選 seed。彩色曲線使用 `--target-color` 隔離。
5. 執行 headless trace：

```bash
python3 scripts/dump_pixels.py chart.png /tmp/chart.bin
node digitizer/cli.js --bin /tmp/chart.bin --size WxH \
  --cal-x PX1,HZ1 PX2,HZ2 --cal-y PY1,DB1 PY2,DB2 \
  --seed PX,PY --x-range X0,X1 --max-jump 14 --simplify 0.05 \
  > /tmp/traced.csv
```

隔離多曲線時，由穩定到脆弱依序使用：目標顏色、色差容差、線型連續性、降低 `--max-jump`、以已驗證曲線建立 mask。任何 mask 都只能移除 decoy，不能補畫目標線。虛線 gap 可用受限 jump 或 greedy trace，但輸出仍須可由同一組參數重跑。

使用：

```yaml
data_origin: official-published-curve
digitization_method: seeded-pixel-trace
```

若 trace 會在交疊處任意跳線、需要逐點目測才能完成，或無法由固定參數重現，停止入庫。

### 4. 套用正式頻率範圍

圖表 UI 與校準軸可以顯示 **20–20,000 Hz**，但正式 CSV 只能保留：

1. 落在原廠標示 `frequency_range_hz` 內；且
2. 官方來源曲線實際存在的資料。

不要為範圍上下限補端點，不要填補曲線 gap，不要插值成新觀測值，也不要外推。若來源曲線只畫出有效範圍的一部分，CSV 就維持較窄範圍。

`--simplify 0.05` 可以刪除重繪誤差容許範圍內的冗餘 trace 點；它不能跨過來源不存在的區段。Overlay 的 interpolated 模式只量測重繪保真度，不會授權把內插值寫進正式 CSV。

### 5. 驗證

官方圖表數位化必須完成 overlay 或向量交叉驗證：

- 把 CSV 重繪回來源圖，確認目標線全程貼合，尤其是分岔、收斂、峰谷與 rolloff。
- 使用 `scripts/overlay_verify.py --interpolated` 記錄 point-wise 與 per-column 的 median、max、gap、ambiguous。
- `ambiguous` 代表驗證器無法量測，不得把它當成通過；回看來源圖與隔離方式。
- 向量來源優先加上 exact path 對照。
- 驗證校準與曲線身分必須有獨立依據，不能用同一組錯誤校準同時產生與驗證資料。

### 6. 產出與 metadata

```csv
freq_hz,level_db
50,-8.0
100,-0.3
```

```yaml
brand: Shure
model: SM58
type: dynamic
polar_patterns: [cardioid]
frequency_range_hz: [50, 15000]
source:
  url: https://pubs.shure.com/view/guide/SM58/en-US.pdf
  page: 6
  chart: "Typical SM58 Frequency Response"
  retrieved: 2026-07-08
curves:
  - file: frequency-response--typical.csv
    condition: "typical on-axis response as published"
    data_origin: official-published-curve
    digitization_method: seeded-pixel-trace
verification:
  method: "overlay_verify.py --interpolated"
  result: "record points, median/max deviation, gaps, and ambiguities"
```

`anchors.yaml` 是正式 CSV 的 regression snapshot，不是原廠證據，也不能取代來源與驗證。

## Fail-closed 檢查

下列任一條成立，就不要建立正式 CSV：

- 沒有官方來源或來源條件不明。
- 只能靠人工／AI 目測估點。
- 軸標籤、曲線身分或開關設定無法確認。
- 向量 path 無法可靠對應目標曲線。
- Seeded trace 無法以固定輸入與參數重現。
- 資料需要補端點、插值填 gap 或外推才看似完整。

停止入庫時，說明缺少哪一項證據或工具能力；不要以 best-effort 數值填補。
