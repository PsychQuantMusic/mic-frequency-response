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

- **使用者給型號**（如 "Shure SM58"）→ 找官方來源。優先順序：**官方向量 SVG**（最權威，見下）> 官方 spec sheet / user-guide **PDF**（常在 `pubs.<brand>.com` 或官網 spec 頁）> 官網產品頁的頻響圖。用 `WebSearch`（限定 `allowed_domains` 到官方網域）找來源，`curl -sL -A "Mozilla/5.0"` 下載 PDF，`pdftoppm -r 400 -png` 轉頁，找頻響圖那一頁（通常在 Specifications 段）。高解析 crop 出圖表區。
- **使用者直接給圖**（URL / PDF / 截圖）→ 直接用。

**來源取得技巧（#15 batch 累積）**：
- **官方向量 SVG（Neumann/Sennheiser 產品頁）**：`assets.sennheiser.com/assets/Frequency-diagram-<MODEL>.svg`（TLM103/KM184/U87 都有）。向量 = 廠商精確 bezier，比點陣更權威。作法：找出目標曲線的 path（如 `cls-9` 實線，`cls-10` 是 dashed 容差帶不取），**只把那條 path 重上色**（geometry 不動）→ cairosvg 渲染成 PNG（macOS 需 `DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib`）→ `--target-color` trace；**可再解析 bezier path 直接算一次做雙路交叉驗證**（兩路一致 = 強完整性保證）。
- **廠商 CDN / URL 會遷移**：Audix 已搬到 Shopify（`audixusa.com/cdn/shop/files/<MODEL>_V3_*.pdf`）；Sennheiser 在 `assets.sennheiser.com/.../product_specification_*.pdf` 或 `docs.cloud.sennheiser.com`；EV 用 `products.electrovoice.com/binary/...` 或 `downloadfile.php?id=`。舊連結 404 是常態，找現行產品頁的下載連結。
- **cutsheet 未必有 FR 圖**：AKG C214/D5 的 cutsheet 只有 spec 表，FR 曲線在**另一份**（C214 在 "Polar_Patterns" PDF、D5 在 user manual p.21）。找不到就翻同型號的其他官方文件。
- **DataDome / captcha 擋 curl**（akg.com）：`demandware.static` 的 PDF 資產本身可直接 curl，但要先拿到 URL。用 `~/bin/safari-browser`（原生 Safari 過 captcha）：`safari-browser open <產品頁>` → `safari-browser snapshot -i` 讀下載 href → 取得直連再 curl。

> **版權鐵律**：原圖（PDF/PNG/截圖）只暫存在 job scratch 或 repo 外，**絕不 commit**。數據（freq→dB）是不受著作權保護的 facts；原圖是廠商的創作。`meta.yaml` 只記來源 URL。

### 2. Digitize — 依圖的類型選方法（關鍵決策）

這是整個 skill 最重要的判斷。先看圖：**一張圖上有幾條曲線？**

| 圖的類型 | 方法 | 為什麼 |
|----------|------|--------|
| **單曲線真實圖**（含同色網格也行）| **seeded trace 優先**（`strategy: viterbi`，headless CLI 見下）| #3 的種子連續性 + #9 的 Viterbi 全域最優已解掉同色網格污染（decoy 分岔、遮擋、網格相切、粗網格線都有防禦與測試）。真實 SM58 官方圖實測：30/30 對照點、max 0.77dB。**可重現、免判讀誤差**——比 AI 判讀更該信。 |
| **多曲線真實圖**（proximity family、presence/low-cut switch、dual-voicing…）| **多數仍 seeded trace**（見下「多曲線圖處置」）| 只要**目標曲線可由顏色或連續性從其他曲線分離**，trace 就能追對它——實測 30+ 支多曲線圖（Shure proximity 4 曲線、e906 三色 presence switch、RE320 dual-voicing、SM81/KSM137 low-cut）全部 trace 成功。**真正需要 AI 判讀的只剩「同線型同色、只能靠 LEGEND 標籤區分」的極少數**（identity 需 Level-2 先驗——見 curve-extraction-priors 筆記）。 |
| **乾淨 / 合成圖** | pixel-trace（greedy 或 viterbi 皆可）| 快、準，回歸測試守著。 |

triage 小抄：**彩色曲線**（e935 藍、C414/NT1 紅…）是最友善情境——顏色是最強的曲線/網格區分特徵：trace 用 `--target-color R,G,B`（CLI）、量化用 `overlay_verify.py --target-color`（#13），異色網格天然被排除。**灰網格 + 黑曲線**（Shure 慣例）次之（暗度閾值排除網格）；**黑網格 + 黑曲線**（Audio-Technica 慣例）最難——trace 靠 viterbi 防禦、量化歧義較高。

#### 多曲線圖處置（#15 batch 累積，30+ 支實戰）

一張圖常有多條曲線。**先決定「哪一條是 typical」，再想「怎麼只追到它」。**

**取哪一條（canonical typical 慣例）**：
| 圖型 | 曲線 | 取哪條 |
|------|------|--------|
| proximity family（Shure：3mm/25mm/51mm/2ft 依距離）| 近距離 = bass boost | **最遠場那條**（2ft / 60cm 的 solid）——published on-axis typical |
| presence switch（e906：bright/moderate/dark）| 3 條 | **moderate/中間 linear** 那條 |
| dual-voicing（RE320：vocal vs kick）| 2 條或**兩張分開的圖** | **general/flat（position 1）**；RE320 是上下兩張圖不是同圖兩線，別看錯 |
| low-cut/bass switch（SM81/KSM137/MD421/C451）| flat + rolloff/cutoff | **flat（無 low-cut、pad off、bass=M）** |
| tolerance band（TLM103/KM184 SVG）| 實線 + dashed ±容差帶 | **nominal 實線**（`cls-9`），dashed 不取 |
| off-axis（AKG D5：0° + 25°）| solid + dashed | **0° on-axis solid** |
`condition` 欄要**明記選了哪條**（例 `"...solid 2ft far-field"` / `"presence switch moderate"` / `"general/flat voicing"`）。

**怎麼只追到它（隔離手段，由易到難）**：
1. **異色** → `--target-color R,G,B`（最乾淨；decoy 天然排除）。
2. **同色但目標與 decoy 色距可分**（e906 三條藍深淺不同）→ `--target-color` + 調 `--tolerance`（實測 40 能隔開中間 cyan、排除 bright dist 48.8/dark dist 180；調太寬會漏進鄰線）。
3. **同色、線型不同（solid vs dashed）** → seed 點在 solid 上，viterbi 連續性天然偏好連續實線、不跳斷線。收斂區（多線靠攏處）若 viterbi 想跳到 decoy → 降 `--max-jump`（6~8）綁住。
4. **收尾必做**：`Read` overlay.png **放大收斂/分岔區**，肉眼確認綠標記全程在目標線、沒在 fork 處跳到 decoy。overlay 的 interpolated max 在收斂區偶爾衝高（decoy 進了量測窗）是**驗證器假影非 trace 錯**——縮 `--window` 或看 CSV 該處值即可辨別。

##### 虛線 proximity 曲線的特例（#16 batch，最難）

近接效應圖（Shure 3mm/25mm/51mm/2ft、AKG 10cm…）的近距曲線是**虛線**，且下方有一條**連續的 solid far-field 曲線**。viterbi 的暗度獎勵偏好連續 ink → 裸追虛線會**在 dash 間隙塌陷到 solid**（solid 是「獎勵磁鐵」，accumulated-dx 讓大 gap 也能觸及下方 solid），peak 值會系統性偏低。四招（實測有效，由簡到繁）：
1. **solid-mask**：先用 committed typical.csv 重追 solid、把 solid 路徑 ±8~22px 塗白，再追虛線（虛線成唯一 ink）。**最通用**。
2. **`--strategy greedy`**（非 viterbi）：greedy 過 dash gap 時凍結 prevY、只在 maxJump 內重連，不會被下方 solid 吸走（D112 實測：viterbi 出錯 +3.98、greedy 正確 +9.96）。
3. **`--max-jump` 調小**（2~8）：curves 間距 >> dash gap 時，小 maxJump 能橋接同曲線 dash、但擋住跳到鄰線（Beta57A：curves ~52px、dash gap ~18px → mj2）。
4. **peel isolation**（多虛線家族）：擦掉 solid + 網格 + 鄰線 ±22px 走廊 → 單曲線圖再追（Beta58A 四曲線用此）。
- **校準的天然 ground-truth**：重追 solid far-field、確認與 committed typical.csv 吻合（median ~0dB）→ 校準正確、非循環。這是 proximity 追法自帶的強交叉驗證。
- **涵蓋範圍慣例**：proximity CSV 收「圖上實際畫成該虛線的區段」（近距 bass boost，到與 far-field **匯合**為止）；匯合以上圖只畫一條共享線 = far-field typical，不重複收（`condition` 註明匯合頻率 + freq range 自帶在 CSV）。**SM58 沒有 proximity 族**（四曲線家族只有 Beta 58A 有）——遇到「型號其實沒這張圖」誠實 SKIP，別拿別支的圖冒充（facts-only 鐵律）。

**Headless trace 流程**（單曲線真實圖的預設路徑，不需瀏覽器）：
1. 高解析轉圖（`pdftoppm -r 400`）並 **crop 到 plot area**（只留格線框內）。為什麼：圖外的標題/標籤文字是暗像素，viterbi lookback 在淡描邊 gap 段會跳上去騎文字（#14 實測 AT2020 +14dB 髒點 45 欄）——文字不在畫面裡就沒有 hop 目標。
2. **校準**：`python3 scripts/overlay_verify.py chart.png --detect-lines` 印網格線候選 → 對軸標籤定 2 個 X 點（已知 Hz）+ 2 個 Y 點（已知 dB）。
   **校準自我核驗（強制）**：用第三條已知標籤線覆核——例如定了 100Hz/10kHz 後，驗算 2kHz/20kHz 線的預測位置是否吻合偵測值（decade 寬度一致性）。校準錯 → 判讀與驗證**一起**錯、抓不到（#6 教訓）。
   **標籤「值」必須實讀，不可用 log 型態推**（#14 SM57 教訓）：log 網格擬合只能給 decade **間距**，給不了標籤**值**——同一組線在「10/25/50…」與「20/50/100…」兩種指派下都自洽（log 尺度自相似），overlay/interpolated 驗證用同一組校準也自洽通過，**結構上抓不到整軸偏移**。破法只有兩條：(a) `Read` 軸標籤 crop 實讀數字；(b) 用獨立來源交叉——published 特徵頻率（如 SM57 的 6kHz presence peak）或既有獨立判讀資料的特徵位置。至少做其一。
3. 看圖挑一個**種子點**（曲線上、避開與網格線相切處、**避開垂直網格線欄**——黑網格圖整欄融成巨 run，種子會誠實回空 #14）。彩色曲線加 `--target-color R,G,B`（網格天然被排除，種子可放心點在曲線任何處）。然後：
   ```bash
   python3 scripts/dump_pixels.py chart.png /tmp/chart.bin   # 印 "W H"
   node digitizer/cli.js --bin /tmp/chart.bin --size WxH \
     --cal-x PX1,HZ1 PX2,HZ2 --cal-y PY1,DB1 PY2,DB2 \
     --seed PX,PY [--x-range X0,X1] [--max-jump 14] > /tmp/traced.csv
   ```
   實例（SM58 官方圖 @400dpi）：`--cal-x 405,100 1244,10000 --cal-y 141,10 469,-10 --seed 820,313 --x-range 150,1372 --max-jump 14`。
4. **`--simplify 0.05` 感知無損降採樣**（#14——取代舊的固定 1/3 八度重取樣）。判準不是點數而是「重繪誤差 ≤ 容差」：密度由曲線複雜度自動決定（平坦段極稀、notch/peak 密）。固定頻率格是 AI 判讀時代「人工讀點貴」的遺產，trace 之後每點免費，別再主動丟密度——資料庫的承諾是**畫回去與原圖一模一樣**。x-range 用 published `frequency_range_hz` 換算 px 定界（圖緣外的 1px 碎片不撿）。
5. **Overlay 驗證（強制，加 `--interpolated`）**——除逐點比對外，沿 CSV 內插折線**逐欄**量偏差（重繪保真度的真正驗證；稀疏資料「點上過檻、點間失真」的漏洞由此補上）。過檻參考：interpolated median ≲0.05 dB、max ≲0.3 dB、零 gap（#14 六支實測 median 0.003–0.017）；`ambiguous` 欄數（垂直網格佔滿窗、不可量測）要一併記錄——黑網格圖幾十欄正常、彩色圖應為 0。把 interpolated 統計記進 meta 的 `verification`。淡描邊高 dpi 圖可加 `--dark-threshold 150`。**綠色曲線**（Lewitt 等）要用 `--marker-color <對比色>`（如紅），否則 overlay 綠標記與綠曲線撞色、目視無法分離——此時改看 interpolated 數字（對原圖像素獨立取樣算的，是視覺檢查的量化版）。**若 CSV 由自訂 pipeline（非 cli.js）產出，確認行尾是 LF 不是 CRLF**——data-schema 測試會抓 header 的 `\r`（MD441-U 踩過）。

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
**Flow**: WebSearch 官方 SM58 user-guide PDF → curl 下載 → `pdftoppm` 轉頁 → 找到第 6 頁「Typical SM58 Frequency Response」→ 單曲線真實圖 → **headless seeded trace**（detect-lines 校準 + 覆核 → plot-area crop → dump_pixels + cli.js `--simplify 0.05`）→ overlay `--interpolated` 驗證過檻 → 寫 `data/shure-sm58/frequency-response--typical.csv` + `meta.yaml`（method 記 seeded-viterbi trace + interpolated 驗證數字）→ commit（原 PDF/PNG 留 scratch、不進 repo）。
**Output**: `data/shure-sm58/` 一個 CSV + 一個 meta.yaml，曲線形狀與官方圖貼合（rolloff + presence peak + notch + 高頻 rolloff）。
