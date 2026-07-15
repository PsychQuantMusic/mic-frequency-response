# digitizer/ — 頻響圖數位化工具

把官方頻率響應圖表以可重現的像素追蹤轉成 `(freq_hz, level_db)` 資料點，匯出 CSV。純前端、無 build step、零 npm 依賴。

這個工具只負責 deterministic trace；它不會讓任意圖片自動符合正式資料庫的收錄門檻。正式入庫仍須符合 [`data/README.md`](../data/README.md) 的 provenance、驗證與頻率範圍契約。

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
2. **座標校準（4 點）** — X 軸填兩個從軸標籤實讀的已知頻率（如 20 / 20000 Hz），各按「在圖上點」點對應的網格線；Y 軸填兩個已知 dB（如 0 / -40），同樣點。用第三條標籤線覆核校準，不能只靠圖形間距猜標籤值。
3. **取色** — 按「點曲線取色」，在曲線上點一下（取得追蹤的目標顏色）。
4. **追蹤** — 調顏色容差 → 按「追蹤曲線」；綠點疊示還原結果，檢查貼合度。
5. **匯出** — 按「下載 CSV」。正式入庫前，只保留原廠 `frequency_range_hz` 內且 trace 實際存在的點；不得為邊界補點、填補 gap、插值或外推。

座標假設：X 軸對數（頻率 Hz），Y 軸線性（dB）。

手動校準與選種子是可重現 trace 的設定，不是人工估讀資料點。若目標曲線無法由顏色、線型或連續性穩定分離，請停止；人工或 AI 逐點目測不是正式入庫的 fallback。

正式曲線的 `meta.yaml` 必須標示：

```yaml
data_origin: official-published-curve
digitization_method: seeded-pixel-trace
reproduction_status: command-recorded
formal_point_count: 164
formal_frequency_span_hz: [40, 6948.831]
verification_relation: current-csv-directly-verified
trace_command: >
  node digitizer/cli.js --bin /path/to/chart.bin --size WIDTHxHEIGHT
  --cal-x PX1,HZ1 PX2,HZ2 --cal-y PY1,DB1 PY2,DB2
  --seed PX,PY --x-range X0,X1 --strategy viterbi --tolerance 40
  --max-jump 12 --target-color R,G,B,A
```

若 overlay／向量統計描述裁切前 trace，而正式 CSV 只是刪除原廠範圍外或來源缺口後資料，改用
`verification_relation: retained-unmodified-subset-of-verified-trace`，並在 `verification.scope_note`
說明兩者關係。正式點數與首末頻率不可沿用裁切前數字。

若來源是原廠直接提供的數值，請使用 `manufacturer-numeric` + `manufacturer-values`，不必經過本工具。若來源是可解析的官方向量路徑，請使用 `official-published-curve` + `vector-path-extraction`。

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
node --test        # 引擎：coords + trace（含合成圖回歸）+ csv；資料層：schema/provenance/range + anchors 回歸（#18）
```

`data-anchors` 只比對已收錄 CSV 的 regression snapshot，不能證明資料來自原廠，也不能取代 overlay／向量驗證。

## 已定案的設計決策

見 epic #1 與 #2：質心取值、斷點跳過不輸出、4 點校準、`node:test`。
