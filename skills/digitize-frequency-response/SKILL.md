---
name: digitize-frequency-response
description: >
  Digitize a microphone (or any audio gear) frequency-response chart into a CSV of (freq_hz,
  level_db), and optionally ingest it into the mic-frequency-response database. Use this skill
  whenever the user wants to add a microphone to the frequency-response database, digitize or
  extract data from a frequency-response graph/chart/curve, convert a spec-sheet response image
  into numbers, or build a queryable dataset from published FR images — even when they only say
  something like "add the SM7B", "get the numbers off this response chart", or "拓一支麥克風進資料庫".
---

# Digitize Frequency Response

把麥克風（或任何音訊器材）的頻率響應曲線圖，還原成 `(freq_hz, level_db)` 的 CSV 數據。

## 為什麼存在

多數廠商只提供頻響「圖片」、不給原始數據。這個 skill 把圖還原成可查詢、可比較、可做 inverse EQ 的數據。核心信念：**資料庫只收真實數據**——所有數字都從實際的廠商圖判讀，絕不編造曲線（編造會污染整個 facts-only 資料庫的可信度）。

## Workflow

### 1. 取得頻響圖

- **使用者給型號**（如 "Shure SM58"）→ 找官方來源。優先順序：官方 spec sheet / user-guide **PDF**（常在 `pubs.<brand>.com` 或官網 spec 頁）> 官網產品頁的頻響圖。用 `WebSearch`（限定 `allowed_domains` 到官方網域）找來源，`curl -sL -A "Mozilla/5.0"` 下載 PDF，`pdftoppm -r 300 -png` 轉頁，找頻響圖那一頁（通常在 Specifications 段）。高解析 crop 出圖表區（`pdftoppm ... -x -y -W -H`）以便精確判讀。
- **使用者直接給圖**（URL / PDF / 截圖）→ 直接用。

> **版權鐵律**：原圖（PDF/PNG/截圖）只暫存在 job scratch 或 repo 外，**絕不 commit**。數據（freq→dB）是不受著作權保護的 facts；原圖是廠商的創作。`meta.yaml` 只記來源 URL。

### 2. Digitize — 依圖的類型二選一（關鍵決策）

這是整個 skill 最重要的判斷。先看圖：**曲線與座標軸網格線是否同色？**

| 圖的類型 | 方法 | 為什麼 |
|----------|------|--------|
| **真實廠商圖**（有座標軸網格線，通常與曲線同為黑色）| **AI 視覺判讀**（你自己看圖讀點）| 網格線橫跨每一欄，`digitizer/` 的全欄質心 pixel-trace 會把 +10/0/-10 等網格交點一起抓進質心 → 輸出看似合理但錯的 dB。你（多模態）看圖沿 log 頻率取樣讀 dB 反而穩健。這是 repo issue #3 記錄的真實限制。 |
| **乾淨 / 合成圖**（單一曲線、無干擾網格）| **pixel-trace**（repo 的 `digitizer/` 引擎）| 快、準、可重現，已有 12 個單元測試 + 合成圖回歸測試守。 |

**AI 判讀步驟**（真實圖的預設路徑）：
1. 讀懂座標系：X 軸 log（找 `20 / 50 / 100 / 1000 / 10000` 等標籤定範圍與方向）、Y 軸 linear dB（找 `0` 線與刻度間距）。高解析 crop + 放大看能大幅提升精度。
2. 沿 log 頻率取樣（例如 `50, 63, 80, 100, 125, 160, 200, ... , 15k, 20k`，約 1/3 八度；陡峭段加密），逐點讀曲線相對 `0dB` 線的 dB 值。
3. 刻意捕捉特徵點：低頻 rolloff 起點、presence peak、任何 notch、高頻 rolloff 終點——這些是曲線的「臉」，讀漏了資料就失真。
4. 誠實標記精度：typical ±0.5dB，陡峭段更大。
5. **Overlay 自我驗證（強制——原圖 = ground truth）**：跑 `scripts/overlay_verify.py`（見 `--help`；先用 `--detect-lines` 找網格線座標、對標籤定校準點），把 CSV 畫回原圖比對。
   - **視覺為主**：`Read` overlay.png，目檢綠十字是否貼曲線。
   - **數字為輔**：median |dev| 應 ≲0.5dB；超標點目檢判定「真偏差」（→ 重讀該點）vs「網格誤匹配」（→ 記錄後忽略）。
   - 修正後**重跑到全數過檻**，把 `median/max dev` 記進 `meta.yaml` 的 `verification` 欄。
   - 為什麼強制：AT2020 首次入庫時 HF 段被系統性判讀過高 1–2dB，「形狀看起來合理」目測完全抓不到——只有 overlay 比對抓得到（#6）。

**pixel-trace 步驟**（乾淨 / 合成圖）：見 `digitizer/README.md`。核心是純函式 `traceCurve({ pixelAt, width, height, xStart, xEnd, calib, targetColor, tolerance })`，`calib` 用 4 點（2 個已知頻率定 X、2 個已知 dB 定 Y）。可透過本機 http server + 瀏覽器讀 canvas `ImageData` 包成 `pixelAt` 餵引擎（見 repo issue #2 的做法）。

### 3. 產出 + 入庫

**CSV**（`frequency-response--<condition>.csv`，`condition` 例：`typical` / `flat` / `off-axis-90`）：
```csv
freq_hz,level_db
50,-8.0
100,-0.3
...
```
頻率遞增，一行一點。

**meta.yaml**：
```yaml
brand: Shure
model: SM58
type: dynamic            # dynamic | condenser | ribbon | ...
polar_patterns: [cardioid]
frequency_range_hz: [50, 15000]
source:
  url: https://pubs.shure.com/view/guide/SM58/en-US.pdf
  page: 6
  chart: "Typical SM58 Frequency Response"
  retrieved: 2026-07-08
digitized_by: <name>
digitized_on: 2026-07-08
method: >
  AI visual reading（真實圖含同色網格，pixel-trace 不適用）— best-effort ±0.5 dB.
  # 乾淨圖用 pixel-trace 時改寫：pixel-trace via digitizer/, 4-point calibration.
curves:
  - file: frequency-response--typical.csv
    condition: "typical on-axis response (as published)"
license_note: >
  數據為事實資料（frequency→dB），從公開官方圖判讀，非原圖之重製。原圖未收錄。
```

**入庫**（若在 `mic-frequency-response` repo）：放 `data/<brand>-<model>/`（全小寫、以 `-` 連接，如 `shure-sm58`）。`.gitignore` 已擋 `data/` 下圖片，commit 時只會收 CSV + meta.yaml。commit 訊息引用對應 issue（`Refs #N`），不用 `Closes`。

## 誠實紀律（這個 skill 的靈魂）

- **只收真實數據**：從實際廠商圖判讀，絕不編造或內插不存在的曲線。沒有可用的真實圖 → 停下請使用者提供，不要自己畫一條。
- **標明 digitize 方法**：`meta.yaml` 的 `method` 記 AI-read 還是 pixel-trace + 誤差估計。三個月後回來看要知道這筆數據怎麼來的、多可信。
- **原圖不進 repo**：只記來源 URL + 頁碼。

## 範例

**Input**: 「把 Shure SM58 加進資料庫」
**Flow**: WebSearch 官方 SM58 user-guide PDF → curl 下載 → `pdftoppm` 轉頁 → 找到第 6 頁「Typical SM58 Frequency Response」→ 判斷是真實圖（黑曲線 + 同色黑網格）→ 走 AI 視覺判讀，沿 log 頻率取樣 ~30 點 → 寫 `data/shure-sm58/frequency-response--typical.csv` + `meta.yaml`（method: AI-read）→ commit（原 PDF/PNG 留 scratch、不進 repo）。
**Output**: `data/shure-sm58/` 一個 CSV + 一個 meta.yaml，曲線形狀與官方圖貼合（rolloff + presence peak + notch + 高頻 rolloff）。
