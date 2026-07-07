# mic-frequency-response

麥克風頻率響應資料庫 — 把廠商只以「圖片」提供的頻響曲線，數位化成可用、精確的數據。

> **動機**：每支麥克風的頻率響應都很重要，但很多廠商不提供原始數據，只給一張頻響曲線圖。
> 這個專案提供 ① 一個把圖片描點還原成 (Hz, dB) 數據的 **Digitizer**，以及
> ② 一個以 **CSV 為 source of truth** 的頻響資料庫。

## 架構

2 個 repo、3 個元件，靠 CSV contract 相接：

```
PsychQuantMusic/mic-frequency-response   （本 repo）
├── data/            ← CSV = source of truth（每支麥一個資料夾，一條曲線一個 CSV）
├── digitizer/       ← 網頁描點工具（半自動像素追蹤 + 手動修正）
└── (未來) Swift 完整版 app

「阿澈的音樂筆記」網站                     （另一個 repo，未來，分開）
└── 建置時讀本 repo 的 CSV → 渲染互動圖表
```

**設計原則**：CSV 是穩定的 contract，工具（網頁 / Swift）可以換，資料不動。
Digitizer 的 trace 引擎**不依賴 AI**；AI 只是自動猜座標軸/曲線顏色的加速層，壞了可 fallback 到手動校準。

## 分期路線圖

| 階段 | 內容 |
|------|------|
| **Phase 1（MVP）** | 網頁 Digitizer：半自動像素追蹤 + 手動修正 → 匯出 CSV |
| **Phase 2** | AI 輔助層：自動猜座標軸邊界 / 曲線顏色，減少手動設定 |
| **Phase 3（完整版）** | Swift 原生 app：更好的原生互動、批量、可簽章散佈 |

## 資料模型

一支麥克風一個資料夾，一條曲線一個 CSV（頻響圖常有多條曲線：on-axis、off-axis、不同指向性）。

```
data/
└── <brand>-<model>/
    ├── meta.yaml                          # 見下方 schema
    ├── frequency-response--flat.csv       # freq_hz, level_db
    └── frequency-response--bass-rolloff.csv
```

### CSV schema

極簡兩欄，存 trace 出來的 raw 點：

```csv
freq_hz,level_db
20,-3.2
21.4,-3.0
...
```

> 未來要做曲線比較或 inverse EQ 時，再從 raw 衍生標準 log grid（如 1/24 八度）。**不進 MVP。**

### meta.yaml schema

```yaml
brand: Shure
model: SM7B
type: dynamic            # dynamic | condenser | ribbon | ...
polar_patterns: [cardioid]
source:
  url: https://...       # 原始頻響圖出處
  retrieved: 2026-07-07
digitized_by: che
digitized_on: 2026-07-07
curves:
  - file: frequency-response--flat.csv
    condition: "flat response, on-axis"
license_note: "數據為事實資料（frequency→dB），非原圖之重製。原圖未收錄。"
```

## 版權與來源

- 本 repo 只收錄**數位化後的數據**（frequency→dB 的事實對應），在著作權上通常屬不受保護的 facts。
- **原廠頻響圖 PNG 本身受版權，不收錄進本 repo**（見 `.gitignore`）。`meta.yaml` 只記錄來源出處 URL。
- 每筆數據都應在 `meta.yaml` 標明來源，尊重原始出處。

## 狀態

- **Phase 1（網頁 Digitizer）**：✅ 完成（#2）— `digitizer/`，18 unit tests + 合成圖回歸測試 + Codex 跨模型盲驗。用法見 [`digitizer/README.md`](digitizer/README.md)。
- **Phase 2**（robust tracing，真實髒圖）：#3｜**首批真實資料入庫**：#4。
- 見 GitHub Issues 追蹤各 phase 進度。
