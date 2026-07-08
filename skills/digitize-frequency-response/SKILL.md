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

### 2. Digitize — 依圖的類型選方法（關鍵決策）

這是整個 skill 最重要的判斷。先看圖：**一張圖上有幾條曲線？**

| 圖的類型 | 方法 | 為什麼 |
|----------|------|--------|
| **單曲線真實圖**（含同色網格也行）| **seeded trace 優先**（`strategy: viterbi`，headless CLI 見下）| #3 的種子連續性 + #9 的 Viterbi 全域最優已解掉同色網格污染（decoy 分岔、遮擋、網格相切、粗網格線都有防禦與測試）。真實 SM58 官方圖實測：30/30 對照點、max 0.77dB。**可重現、免判讀誤差**——比 AI 判讀更該信。 |
| **多曲線真實圖**（線型區分的 multi-condition，如 SM7B）| **AI 視覺判讀** | trace 無線型辨識（identity 需 Level-2 學出的先驗——見 curve-extraction-priors 筆記），沿 LEGEND 線型逐條判讀仍是唯一路徑。 |
| **乾淨 / 合成圖** | pixel-trace（greedy 或 viterbi 皆可）| 快、準，回歸測試守著。 |

triage 小抄：**彩色曲線**（e935 藍、C414/NT1 紅…）是最友善情境——顏色是最強的曲線/網格區分特徵：trace 用 `--target-color R,G,B`（CLI）、量化用 `overlay_verify.py --target-color`（#13），異色網格天然被排除。**灰網格 + 黑曲線**（Shure 慣例）次之（暗度閾值排除網格）；**黑網格 + 黑曲線**（Audio-Technica 慣例）最難——trace 靠 viterbi 防禦、量化歧義較高。

**Headless trace 流程**（單曲線真實圖的預設路徑，不需瀏覽器）：
1. 高解析轉圖（`pdftoppm -r 400`）並 crop 出圖表區。
2. **校準**：`python3 scripts/overlay_verify.py chart.png --detect-lines` 印網格線候選 → 對軸標籤定 2 個 X 點（已知 Hz）+ 2 個 Y 點（已知 dB）。
   **校準自我核驗（強制）**：用第三條已知標籤線覆核——例如定了 100Hz/10kHz 後，驗算 2kHz/20kHz 線的預測位置是否吻合偵測值（decade 寬度一致性）。校準錯 → 判讀與驗證**一起**錯、抓不到（#6 教訓）。
3. 看圖挑一個**種子點**（曲線上、避開與網格線相切處），然後：
   ```bash
   python3 scripts/dump_pixels.py chart.png /tmp/chart.bin   # 印 "W H"
   node digitizer/cli.js --bin /tmp/chart.bin --size WxH \
     --cal-x PX1,HZ1 PX2,HZ2 --cal-y PY1,DB1 PY2,DB2 \
     --seed PX,PY [--x-range X0,X1] [--max-jump 14] > /tmp/traced.csv
   ```
   實例（SM58 官方圖 @400dpi）：`--cal-x 405,100 1244,10000 --cal-y 141,10 469,-10 --seed 820,313 --x-range 150,1372 --max-jump 14`。
4. traced.csv 沿 log 頻率**重取樣成 ~1/3 八度**的入庫點（trace 每欄一點太密；重取樣時保留特徵點——peak/notch/rolloff 端點）。
5. **Overlay 驗證（強制，同 AI 判讀路徑的第 5 步）**——trace 也可能被校準錯誤或 trace 缺陷污染，原圖 ground-truth 比對不因方法而豁免。

**AI 判讀步驟**（多曲線圖的路徑；單曲線圖 trace 失敗時的 fallback）：
1. 讀懂座標系：X 軸 log（找 `20 / 50 / 100 / 1000 / 10000` 等標籤定範圍與方向）、Y 軸 linear dB（找 `0` 線與刻度間距）。高解析 crop + 放大看能大幅提升精度。
2. 沿 log 頻率取樣（例如 `50, 63, 80, 100, 125, 160, 200, ... , 15k, 20k`，約 1/3 八度；陡峭段加密），逐點讀曲線相對 `0dB` 線的 dB 值。
3. 刻意捕捉特徵點：低頻 rolloff 起點、presence peak、任何 notch、高頻 rolloff 終點——這些是曲線的「臉」，讀漏了資料就失真。
4. 誠實標記精度：typical ±0.5dB，陡峭段更大。
5. **Overlay 自我驗證（強制——原圖 = ground truth）**：跑 `scripts/overlay_verify.py`（見 `--help`；先用 `--detect-lines` 找網格線座標、對標籤定校準點），把 CSV 畫回原圖比對。
   - **視覺為主**：`Read` overlay.png，目檢綠十字是否貼曲線。
   - **數字為輔**：median |dev| 應 ≲0.5dB；超標點目檢判定「真偏差」（→ 重讀該點）vs「網格誤匹配」（→ 記錄後忽略）。
   - 修正後**重跑到全數過檻**，把 `median/max dev` 記進 `meta.yaml` 的 `verification` 欄。
   - 為什麼強制：AT2020 首次入庫時 HF 段被系統性判讀過高 1–2dB，「形狀看起來合理」目測完全抓不到——只有 overlay 比對抓得到（#6）。

**瀏覽器 UI 路徑**（人工互動時的替代）：本機 http server + `digitizer/index.html`，點 4 個校準點 + 取色（取色點即種子），UI 已走 viterbi。引擎細節見 `digitizer/README.md`。

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
**Flow**: WebSearch 官方 SM58 user-guide PDF → curl 下載 → `pdftoppm` 轉頁 → 找到第 6 頁「Typical SM58 Frequency Response」→ 單曲線真實圖 → **headless seeded trace**（detect-lines 校準 + 覆核 → dump_pixels + cli.js → 重取樣 ~30 點）→ overlay 驗證過檻 → 寫 `data/shure-sm58/frequency-response--typical.csv` + `meta.yaml`（method 記 seeded-viterbi trace + 驗證數字）→ commit（原 PDF/PNG 留 scratch、不進 repo）。
**Output**: `data/shure-sm58/` 一個 CSV + 一個 meta.yaml，曲線形狀與官方圖貼合（rolloff + presence peak + notch + 高頻 rolloff）。
