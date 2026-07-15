# mic-frequency-response

麥克風頻率響應資料庫 — 保存原廠發布的數值，以及從官方圖表可重現地數位化出的曲線資料。

> **動機**：每支麥克風的頻率響應都很重要，但許多廠商不提供機器可讀的原始數值，只發布曲線圖。
> 這個專案提供 ① 一個把官方圖表可重現地還原成 `(Hz, dB)` 資料的 **Digitizer**，以及
> ② 一個以 **CSV 為應用程式資料 contract** 的頻響資料庫。

CSV 是本 repo 供程式讀取的正式格式，**不代表每份 CSV 都是原廠 raw data**。由官方圖表轉換出的 CSV 是衍生的 digitized data，必須與原廠直接發布的數值分開標示。

## 架構

2 個 repo、3 個元件，靠 CSV contract 相接：

```
PsychQuantMusic/mic-frequency-response   （本 repo）
├── data/            ← 正式 CSV contract（每支麥一個資料夾，一條曲線一個 CSV）
├── digitizer/       ← 網頁數位化工具（4 點校準 + seeded pixel trace）
└── (未來) Swift 完整版 app

「阿澈的音樂筆記」網站（PsychQuantMusic/music-note，已上線，刻意分開）
└── 建置時讀本 repo 的 CSV → 渲染互動圖表
    🌐 https://music-note-psych-quant.vercel.app/
```

**設計原則**：CSV 是穩定的 contract，工具（網頁 / Swift）可以換；證據來源與轉換方法則由 `meta.yaml` 保存。
座標校準、種子選擇與 overlay 覆核可以由人操作，但正式資料點只能來自可重現的數值匯入、向量路徑解析或 seeded pixel trace。人工或 AI 目測估點不是入庫 fallback。

## 分期路線圖

| 階段 | 內容 |
|------|------|
| **Phase 1（MVP）** | 網頁 Digitizer：4 點校準 + seeded pixel trace → 匯出 CSV |
| **Phase 2** | Robust tracing：改善髒圖、網格與多曲線隔離，同時維持可重現驗證 |
| **Phase 3（完整版）** | Swift 原生 app：更好的原生互動、批量、可簽章散佈 |

## 資料模型

一支麥克風一個資料夾，一條曲線一個 CSV（頻響圖常有多條曲線：on-axis、off-axis、不同指向性）。

```
data/
└── <brand>-<model>/
    ├── meta.yaml                          # 見下方 schema
    ├── frequency-response--typical.csv    # freq_hz, level_db
    └── frequency-response--off-axis.csv   # 若官方另有可辨識曲線
```

### CSV schema

極簡兩欄，保存原廠數值或可重現 trace 取得的資料點：

```csv
freq_hz,level_db
20,-3.2
21.4,-3.0
...
```

> 未來要做曲線比較或 inverse EQ 時，可再從正式 CSV 衍生標準 log grid（如 1/24 八度）。衍生資料不得回寫成原廠 raw values。

### meta.yaml schema

```yaml
brand: Audio-Technica
model: AT2020
type: condenser          # dynamic | condenser | ribbon | ...
polar_patterns: [cardioid]
frequency_range_hz: [20, 20000]
source:
  url: https://...       # 原始頻響圖出處
  retrieved: 2026-07-07
digitized_by: che
digitized_on: 2026-07-07
curves:
  - file: frequency-response--typical.csv
    condition: "typical on-axis response as published"
    data_origin: official-published-curve
    digitization_method: seeded-pixel-trace
license_note: "資料為 frequency→dB 的事實對應，非原圖之重製。原圖未收錄。"
```

每條 `curves[]` 都必須使用下列其中一組來源與方法：

| `data_origin` | `digitization_method` | 意義 |
|---|---|---|
| `manufacturer-numeric` | `manufacturer-values` | 原廠直接發布的 CSV、FRD 或數值表 |
| `official-published-curve` | `vector-path-extraction` | 從官方 SVG／PDF 的可識別向量路徑解析 |
| `official-published-curve` | `seeded-pixel-trace` | 從官方點陣圖以固定校準、種子與參數追蹤 |

人工／AI 目測估點不在 allowlist 內，不得放進正式 `data/`。

## 收錄邊界

- 圖表顯示與校準範圍可以固定為 **20–20,000 Hz**；這只是 presentation range。
- 頻響 CSV 只能保留原廠標示 `frequency_range_hz` 內，且來源曲線實際存在的點。
- 不補端點、不填補缺口、不插值成新觀測值，也不外推。沒有資料的區段保持空白。
- `--simplify` 只能刪除重繪誤差容許範圍內的冗餘 trace 點；不得跨越來源曲線沒有資料的缺口。
- 官方圖表數位化必須保留來源 URL、頁碼／圖名、取得日期、校準與 overlay／向量交叉驗證紀錄。
- 無法以可重現方式分離目標曲線時，該曲線不入庫；不能退回目測估點。
- `anchors.yaml` 只是正式 CSV 的回歸快照，用來偵測意外漂移；它不是原廠獨立證據，也不能驗證曲線真實性。

下游 UI 必須依 `data_origin` 顯示「原廠數值」或「由官方圖表數位化」，不得把兩者統稱為原廠 raw data。

## 版權與來源

- 本 repo 只收錄**原廠數值或數位化後的資料點**（frequency→dB 的事實對應），不收錄原圖。
- **原廠頻響圖 PNG 本身受版權，不收錄進本 repo**（見 `.gitignore`）。`meta.yaml` 只記錄來源出處 URL。
- 每條曲線都必須在 `meta.yaml` 標明來源與轉換方法，尊重原始出處並避免誤稱資料層級。

## 狀態

- **Phase 1（網頁 Digitizer）**：✅ 完成（#2）— `digitizer/`，18 unit tests + 合成圖回歸測試 + Codex 跨模型盲驗。用法見 [`digitizer/README.md`](digitizer/README.md)。
- **Phase 2**（robust tracing，真實髒圖）：#3｜**首批真實資料入庫**：#4。
- 見 GitHub Issues 追蹤各 phase 進度。
