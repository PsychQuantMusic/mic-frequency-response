# data/ — 頻響資料庫

CSV 是本專案供應用程式讀取的正式 **data contract**。每支麥克風一個資料夾，一條曲線一個 CSV。

這個 contract 不等於原廠 raw data。原廠直接發布的數值與從官方圖表數位化出的衍生資料，必須在每條 `curves[]` 分別標示。

## 目錄慣例

```
data/
└── <brand>-<model>/          # 全小寫、以 - 連接，例：shure-sm7b
    ├── meta.yaml
    ├── frequency-response--<condition>.csv   # freq_hz,level_db（頻率遞增）
    ├── polar-pattern--<freq>hz.csv           # angle_deg,level_db（一頻率一檔）
    └── ...
```

**Frequency response**：`<condition>` 是曲線的辨識條件，例 `typical`、`flat`、`bass-rolloff`、`off-axis-90`。欄位 `freq_hz,level_db`，頻率遞增。

**Polar pattern**（#5）：`<freq>` 是該曲線的頻率（如 `1000hz`）。欄位 `angle_deg,level_db`，0° = on-axis；官方圖只發布 0–180° 半平面時，就只保存該範圍並在 `polar_read_note` 記錄慣例，不鏡射補成 0–360°。`meta.yaml` 的 `curves[]` 條目以 `kind: polar-pattern` 標示；無 `kind` = frequency-response（向後相容）。

## 正式收錄政策

每條曲線必須有 `data_origin` 與 `digitization_method`，且只能是下列配對：

| `data_origin` | `digitization_method` | 可接受來源 |
|---|---|---|
| `manufacturer-numeric` | `manufacturer-values` | 原廠 CSV、FRD 或數值表 |
| `official-published-curve` | `vector-path-extraction` | 官方 SVG／PDF 中可識別且可重現解析的向量路徑 |
| `official-published-curve` | `seeded-pixel-trace` | 官方點陣圖中以固定校準、種子與參數可重現的像素追蹤 |

人工或 AI 目測估點（manual estimate）不得進入正式 `data/`。Overlay 只能驗證重繪是否貼近來源曲線，不能把不可重現的目測點升級為合格資料。

頻響資料另有範圍限制：

- 圖表 UI 與校準軸可顯示 20–20,000 Hz。
- CSV 只能保存原廠標示 `frequency_range_hz` 內，且官方來源實際畫出的資料。
- 不為範圍邊界補點，不插值填缺口，不外推曲線。缺資料處保持缺資料。
- 簡化 trace 只能移除可由原 trace 重繪的冗餘點，不得跨過來源曲線缺口。

`anchors.yaml` 是既有 CSV 的 regression snapshot，只用來發現意外變動；它不是原廠獨立證據，不能取代 provenance 或 overlay／向量驗證。

## 新增一筆資料的流程

1. 找到原廠發布的數值或官方圖表，記錄官方 URL、頁碼／圖名與取得日期。
2. 原廠數值直接匯入；官方圖表只用 `vector-path-extraction` 或可重現的 `seeded-pixel-trace`。若無法穩定分離目標曲線，停止入庫。
3. 頻響 CSV 裁在 `frequency_range_hz` 內，只保留來源中已有的點；不要補端點、插值或外推。
4. 把 CSV 放到 `data/<brand>-<model>/`，並在對應 `curves[]` 加入 `data_origin` 與 `digitization_method`。
5. 官方圖表數位化必須加入 `verification`（polar 使用 `polar_verification`），記錄校準、參數、overlay 統計或向量交叉驗證。
6. **不要**把原廠 PDF／SVG／圖片 commit 進來（`.gitignore` 已擋 `data/` 下的圖片）。

最小曲線 metadata：

```yaml
frequency_range_hz: [80, 16000]
source:
  url: https://manufacturer.example/manual.pdf
  page: 12
  chart: "Frequency Response"
  retrieved: 2026-07-15
curves:
  - file: frequency-response--typical.csv
    condition: "0-degree on-axis response as published"
    data_origin: official-published-curve
    digitization_method: seeded-pixel-trace
verification:
  method: "overlay_verify.py --interpolated"
  result: "record measured deviations, gaps, and ambiguities"
```

範例 `meta.yaml` 與 CSV schema 見 repo 根目錄 `README.md`。
