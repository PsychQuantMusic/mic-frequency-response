# digitizer/ — 頻響圖描點工具

把頻率響應圖片轉成 `(freq_hz, level_db)` 數據點，匯出 CSV。

## 引擎（Phase 1，MVP）

半自動像素追蹤，資料流：

```
載入圖片
  → 使用者點座標校準點（20Hz / 20kHz 對應畫面 x；0dB 與另一參考 dB 對應畫面 y）
  → 選曲線顏色
  → 沿 x 逐像素欄掃描，找該顏色的 y 像素
  → 以 log-X / linear-Y 反算成 (Hz, dB)
  → canvas 疊示抽出的曲線供即時檢查
  → 手動修斷點
  → 匯出 CSV
```

**座標假設**：X 軸為對數刻度（20Hz–20kHz），Y 軸為線性 dB。

## 技術載體

- **Phase 1**：網頁工具（HTML / JS + Canvas）— 描點是視覺互動任務，canvas 最自然，零安裝跨平台。
- **Phase 3**：Swift 原生 app 為完整版。

兩者共用 `data/` 的 CSV schema，互為替代。

## 測試策略

以**已知答案的合成頻響圖**（自畫一條已知曲線）餵進引擎，驗證還原 CSV 的誤差在容忍內 —
這是本專案最核心的回歸測試。

🚧 尚未實作 — 見 GitHub Issues。
