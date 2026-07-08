# data/ — 頻響資料庫

CSV 是本專案的 **source of truth**。每支麥克風一個資料夾，一條曲線一個 CSV。

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

**Polar pattern**（#5）：`<freq>` 是該曲線的頻率（如 `1000hz`）。欄位 `angle_deg,level_db`，0° = on-axis；發表圖左右對稱時記 0–180° 半平面即完整（`meta.yaml` 的 `polar_read_note` 記慣例）。`meta.yaml` 的 `curves[]` 條目以 `kind: polar-pattern` 標示；無 `kind` = frequency-response（向後相容）。

## 新增一筆資料的流程

1. 用 `digitizer/` 網頁工具載入原廠頻響圖。
2. 點校準點（20Hz / 20kHz / 0dB 在畫面的位置）+ 選曲線顏色 → 抽出曲線 → 匯出 CSV。
3. 把 CSV 放到 `data/<brand>-<model>/`，補上 `meta.yaml`（含來源出處 URL）。
4. **不要**把原廠圖片 commit 進來（`.gitignore` 已擋 `data/` 下的圖片）。

範例 `meta.yaml` 與 CSV schema 見 repo 根目錄 `README.md`。
