# data/ — 頻響資料庫

CSV 是本專案的 **source of truth**。每支麥克風一個資料夾，一條曲線一個 CSV。

## 目錄慣例

```
data/
└── <brand>-<model>/          # 全小寫、以 - 連接，例：shure-sm7b
    ├── meta.yaml
    ├── frequency-response--<condition>.csv
    └── ...
```

- `<condition>`：曲線的辨識條件，例 `flat`、`bass-rolloff`、`off-axis-90`、`cardioid`。
- CSV 欄位固定為 `freq_hz,level_db`，一列一個點，頻率遞增。

## 新增一筆資料的流程

1. 用 `digitizer/` 網頁工具載入原廠頻響圖。
2. 點校準點（20Hz / 20kHz / 0dB 在畫面的位置）+ 選曲線顏色 → 抽出曲線 → 匯出 CSV。
3. 把 CSV 放到 `data/<brand>-<model>/`，補上 `meta.yaml`（含來源出處 URL）。
4. **不要**把原廠圖片 commit 進來（`.gitignore` 已擋 `data/` 下的圖片）。

範例 `meta.yaml` 與 CSV schema 見 repo 根目錄 `README.md`。
