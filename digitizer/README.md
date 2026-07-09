# digitizer/ — 頻響圖描點工具

把頻率響應圖片轉成 `(freq_hz, level_db)` 數據點，匯出 CSV。純前端、無 build step、零 npm 依賴。

## 跑起來

⚠️ ES module 無法從 `file://` 載入（瀏覽器 CORS 會擋，畫面會空白）。需要一個 local 靜態 server：

```bash
cd digitizer
python3 -m http.server 8000
# 瀏覽器開 http://localhost:8000/
```

（任何靜態 server 都行；`python3 -m http.server` 只是零依賴的一種。這只是 serve 靜態檔，不是 build step。）

## 使用步驟

1. **載入圖片** — 選一張頻響圖（原圖留 local，別 commit 進 repo）。
2. **座標校準（4 點）** — X 軸填兩個已知頻率（如 20 / 20000 Hz），各按「在圖上點」點對應的網格線；Y 軸填兩個已知 dB（如 0 / -40），同樣點。
3. **取色** — 按「點曲線取色」，在曲線上點一下（取得追蹤的目標顏色）。
4. **追蹤** — 調顏色容差 → 按「追蹤曲線」；綠點疊示還原結果，檢查貼合度。
5. **匯出** — 按「下載 CSV」，存到 `data/<brand>-<model>/frequency-response--<condition>.csv`。

座標假設：X 軸對數（頻率 Hz），Y 軸線性（dB）。

## 架構（引擎與 canvas 解耦）

| 檔案 | 職責 | 測試 |
|------|------|------|
| `coords.js` | log-X / linear-Y 座標雙向轉換（純函式）| `test/coords.test.js` |
| `trace.js` | 像素追蹤，吃 `pixelAt(x,y)→[r,g,b,a]` accessor（純函式，不碰 canvas）| `test/trace.test.js`（含合成圖回歸）|
| `csv.js` | CSV 匯出 `freq_hz,level_db`（純函式）| `test/csv.test.js` |
| `app.js` | canvas/DOM 殼層：把 ImageData 包成 `pixelAt`、點擊換算、疊示、觸發下載 | 瀏覽器 smoke |
| `index.html` | UI | — |

引擎吃抽象 pixel accessor 的設計，讓核心正確性能 **headless 回歸測試**（餵程式生成的合成圖 pixel array），不需開瀏覽器。

## 測試

```bash
cd digitizer
node --test        # 引擎：coords + trace（含合成圖回歸）+ csv；資料層：data-schema（格式）+ data-anchors（值回歸，#18）
```

## 已定案的設計決策

見 epic #1 與 #2：質心取值、斷點跳過不輸出、4 點校準、`node:test`。
