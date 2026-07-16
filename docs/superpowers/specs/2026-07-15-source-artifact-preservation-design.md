# 本機原廠來源檔保存設計

日期：2026-07-15
狀態：書面規格完成且三位獨立審查者通過，等待使用者確認
範圍：`mic-frequency-response` issue #19／PR #20

## 背景

目前 repository 有 44 支麥克風、57 條正式頻率響應曲線，但沒有保存任何原廠
PDF、SVG、圖表頁或裁切圖。每支麥克風的 `meta.yaml` 都有主要 `source.url`，總計
可辨識出 55 個不重複官方來源 URL：43 個 PDF、4 個 SVG 與 8 個網頁參考。

只保存 URL 會留下兩個可重現性缺口：來源可能下架或改版，既有 legacy trace 在離線時
甚至沒有原圖可供重新檢查。本設計補上來源 artifact 保存與曲線對應；舊資料的完整像素
校準 recipe 仍需另案重建。另一方面，原廠圖檔公開可下載不等於允許重新散布，因此來源
blob 不應直接提交至公開 GitHub repository。

本設計採用「本機原檔＋GitHub manifest、SHA-256 與下載工具」：原廠檔案和衍生圖只存
本機且由 Git 忽略；可公開審查的來源、完整性與推導資訊則進版控。

## 目標

1. 對全部 55 個已知來源觀測進行正規化、盤點與取得嘗試。
2. 保存可取得的完整原廠 PDF／SVG，以及從原檔新建的標準化 300 DPI 覆核頁與裁切圖。
3. 對每個本機 artifact 保存大小、SHA-256、來源與取得時間。
4. 失效或只有需認證／會過期簽章 URL 的來源明確標示為 `unavailable`，不建立假檔或假 hash。
5. 新 clone 可從官方 URL 恢復仍可取得且 hash 未變的原始檔。
6. 離線驗證能發現缺檔、檔案改寫、路徑逃逸與 manifest 漂移。
7. 公開 GitHub repository 永遠不追蹤原廠 blob。

## 非目標

- 不把公開可下載解讀為可重新散布。
- 不建立通用網站鏡像器，也不保存完整產品網頁。
- 不繞過登入、付費牆、防機器人機制或短期 token。
- 不在來源改版時自動接受新 hash。
- 不把 chart crop 宣稱為原廠原始檔。
- 不要求 GitHub CI 擁有被忽略的本機來源檔。
- 不承諾 57 條既有曲線都能一鍵重播 overlay；本 change 保存必要圖檔與對應關係，但
  `cal-x`／`cal-y`、target color/window 等 legacy calibration recipe 留待後續 change。
- 不宣稱新建的 300 DPI 圖等於歷史描線時使用的 400／600 DPI scratch artifact；既有
  `meta.yaml method` 繼續保存當時做法，manifest v1 的衍生圖是統一的覆核基準。

## 檔案配置

每支麥克風保有獨立 manifest，避免中央檔案衝突並沿用現有資料夾邊界：

```text
data/{slug}/
├── meta.yaml
├── source-manifest.json
└── source/                         # Git ignored
    ├── original.pdf                # 或 original.svg
    ├── page-02.png
    └── chart-frequency-response.png
```

`meta.yaml` 繼續描述麥克風、曲線條件、原廠圖表語意與驗證結論；
`source-manifest.json` 專門描述來源 reference、本機 artifact、完整性與衍生關係。

`.gitignore` 必須忽略整個 `data/**/source/`，並補齊 SVG、TIF、AVIF 等格式的防線。
Schema 測試另行拒絕任何 artifact `local_path` 指向 `source/` 以外的位置。

## Manifest v1

### 頂層

```json
{
  "schema_version": 1,
  "mic_slug": "sennheiser-e906",
  "references": [],
  "artifacts": []
}
```

- `schema_version` 固定為整數 `1`。
- `mic_slug` 必須與父資料夾名稱完全相同。
- `references` 保存不下載的產品頁／landing page。
- `artifacts` 保存原廠可下載檔與本機衍生檔。

結構 schema 保存於 `schemas/source-manifest-v1.schema.json`，使用封閉物件
（`additionalProperties: false`）；全域 host policy 保存於 `config/source-hosts.json`。
JSON Schema 驗證欄位形狀，repository validator 另行驗證跨 entry 的 URL、host、路徑與
推導圖契約。

Manifest v1 只允許下列五種 `(role, availability)` 組合：

| role | availability | 必要欄位 | 禁止欄位 |
|---|---|---|---|
| `original` | `pending` | `id`, `source_url`, `local_path`, `media_type`, `curve_files`, `redistribution`, `allowed_hosts` | hash、大小、日期、reason、衍生欄位 |
| `original` | `available` | 上列欄位加 `size_bytes`, `sha256`, `retrieved_at` | reason、checked、衍生欄位 |
| `original` | `unavailable` | `id`, `checked_at`, `reason`, `curve_files`, `redistribution`，加一種來源定位方式 | local path、media type、hash、大小、retrieved、衍生欄位 |
| `rendered-page` | `available` | `id`, `local_path`, `media_type`, `size_bytes`, `sha256`, `generated_at`, `redistribution`, `derived_from`, `derived_from_sha256`, page derivation | source URL、host、retrieval／unavailable 欄位、crop 欄位 |
| `chart-crop` | `available` | `id`, `local_path`, `media_type`, `size_bytes`, `sha256`, `generated_at`, `redistribution`, `derived_from`, `derived_from_sha256`, `curve_files`, crop derivation | source URL、host、retrieval／unavailable 欄位、page/render 欄位 |

其他組合與未知欄位一律拒絕。`id` 在單一 manifest 內唯一；每個 `local_path` 也只能被
一個 artifact 使用。Unavailable 的來源定位必須恰為下列一種：穩定 `source_url` 加非空
`allowed_hosts`，或指向 `no-stable-endpoint` redaction record 的 `redaction_ref`。
`source_url` hostname 必須出現在該 entry 的非空 `allowed_hosts`，
而 `allowed_hosts` 每一項都必須存在於全域 policy。Reference role 只允許
`product-page`、`landing-page` 或 `documentation-page`，且只含 role 與 HTTPS URL。
Reference URL 的 hostname 也必須存在於全域 host policy。

`derived_from` 必須指向同 manifest 中已存在且 `available` 的 parent。`rendered-page` 的
parent 必須是 PDF `original`；`chart-crop` 的 parent 必須是 `rendered-page`。推導圖不得
循環或引用 unavailable／pending artifact。`derived_from_sha256` 必須等於 parent 目前的
`sha256`；parent 內容更新後，舊 descendant 立即視為 stale，直到重新產生並更新 hash
鏈。副檔名必須與 `media_type` 一致。
`curve_files` 必須是非空且不重複的 CSV basename，並真實存在於同一麥克風資料夾；每個
`original` 與 `chart-crop` 都必填，讓每條正式曲線都能反查實際來源及裁切圖。Rendered
page 可由其下游 crop 反查，不重複保存此欄位。

反向完整性以 `meta.yaml curves[].file` 為權威集合：每個正式 curve file 至少要被一個
`original.curve_files` 覆蓋。每個 available PDF original 列出的 curve 都必須再被該
original 的某個 descendant `chart-crop` 覆蓋；chart-crop 的 `curve_files` 必須是其
ancestor original 的子集合。Available chart-only SVG original 可直接完成覆蓋，不要求
衍生 crop。Unavailable original 只參與來源 coverage，沒有衍生檔義務。允許同一 curve
由多個原廠來源交叉覆蓋，但不得有 manifest 指向 `meta.yaml` 未列出的 CSV。

Manifest v1 的媒體組合是封閉的：`original` 只允許 `.pdf`＋`application/pdf` 或
`.svg`＋`image/svg+xml`；`rendered-page` 與 `chart-crop` 只允許 `.png`＋`image/png`。
TIF／AVIF 等副檔名仍納入 `.gitignore` 防線，純粹防止誤提交，schema 與 CLI 不接受。
本 change 的 `redistribution` 固定為 `local-only`；未來若確認某來源可再散布，必須升版
schema 與政策，不能直接改 manifest 字串。

### Reference

```json
{
  "role": "product-page",
  "url": "https://manufacturer.example/product"
}
```

Reference 只作 provenance，不要求本機檔案、大小或 hash。

### 可取得的原廠 Artifact

```json
{
  "id": "official-specification",
  "role": "original",
  "availability": "available",
  "source_url": "https://manufacturer.example/manual.pdf",
  "local_path": "source/original.pdf",
  "media_type": "application/pdf",
  "curve_files": ["frequency-response--typical.csv"],
  "size_bytes": 123456,
  "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "retrieved_at": "2026-07-15T12:00:00Z",
  "redistribution": "local-only",
  "allowed_hosts": ["manufacturer.example"]
}
```

`available` 原廠 artifact 的上述欄位全部必填。`allowed_hosts` 只能取自受測的全域官方
host policy 子集合，專門描述預期 redirect chain，不得自行擴張到任意 hostname。

### 失效的原廠 Artifact

```json
{
  "id": "official-svg",
  "role": "original",
  "availability": "unavailable",
  "source_url": "https://manufacturer.example/expired.svg",
  "curve_files": ["frequency-response--typical.csv"],
  "checked_at": "2026-07-15T12:00:00Z",
  "reason": "no stable endpoint; only an expired signed URL was known",
  "redistribution": "local-only",
  "allowed_hosts": ["manufacturer.example"]
}
```

`unavailable` 不得帶 `sha256`、`size_bytes` 或宣稱已存在的 `local_path`。具有到期或認證
語意的 signed URL 不得提交；manifest 保存穩定 canonical URL，並以 reason 說明無穩定
下載端點。不含 query credential 的不透明版本路徑不因外觀像 token 就被排除；例如現有
Neumann `/svg/Zz...==` 路徑在 2026-07-15 仍直接回傳 SVG，應保存完整版本化 URL。

沒有安全 canonical URL 時改用 redaction reference：

```json
{
  "id": "official-svg",
  "role": "original",
  "availability": "unavailable",
  "redaction_ref": "neumann-example-source-asset",
  "curve_files": ["frequency-response--typical.csv"],
  "checked_at": "2026-07-15T12:00:00Z",
  "reason": "no stable endpoint",
  "redistribution": "local-only"
}
```

來源正規化會從帶註解的 `meta.yaml` 字串抽出實際 URL，但不改寫 path。若首次遷移真的
發現具 credential／expiry query（例如 `token`、`sig`、`signature`、`expires`、
`X-Amz-*`、`X-Goog-*`），則不得把原字串寫進 Git；改在
`config/source-url-redactions.json` 保存來源檔欄位位置、原 URL 的 SHA-256，以及安全的
canonical replacement 或 `no-stable-endpoint` 原因。這份例外表也是封閉 schema，不能
保存原始敏感 URL。

Redaction 檔頂層只含 `schema_version: 1` 與 `entries`。每筆 entry 的 `id` 全域唯一，並
必填 `mic_slug`、`source_field`、`legacy_url_sha256`、`checked_at` 與 `disposition`：
`replaced` 必須另帶 `canonical_url`，且對應 manifest／reference 必須使用該 URL；
`no-stable-endpoint` 禁止 `canonical_url`，並由 unavailable artifact 的 `redaction_ref`
引用。兩種 variant 均拒絕未知欄位、原 URL 與 credential。如此即使沒有安全 canonical
URL，來源仍有正式、可稽核且不洩漏憑證的 unavailable 分類。

所有 `*_at` 欄位採 RFC 3339 UTC timestamp（`YYYY-MM-DDTHH:MM:SSZ`），不接受只有日期
或本地時區的模糊值。

### 尚待取得的 Draft Artifact

```json
{
  "id": "official-specification",
  "role": "original",
  "availability": "pending",
  "source_url": "https://manufacturer.example/manual.pdf",
  "local_path": "source/original.pdf",
  "media_type": "application/pdf",
  "curve_files": ["frequency-response--typical.csv"],
  "redistribution": "local-only",
  "allowed_hosts": ["manufacturer.example"]
}
```

`pending` 只允許出現在 `bootstrap` 尚未完成的未提交 draft，不帶 `sha256`、
`size_bytes` 或取得日期。成功取得後必須改為 `available`；只有符合下述明確分類規則時
才能改為 `unavailable`。其餘錯誤維持 `pending`、命令回傳非零。Repository schema 測試
拒絕任何已提交 manifest 含有 `pending`。

### 已轉譯的圖表頁 Artifact

```json
{
  "id": "frequency-response-page",
  "role": "rendered-page",
  "availability": "available",
  "local_path": "source/page-02.png",
  "derived_from": "official-specification",
  "derived_from_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "derivation": {
    "page": 2,
    "render_dpi": 300,
    "render_tool": "pdftocairo",
    "render_tool_version": "26.06.0"
  },
  "media_type": "image/png",
  "size_bytes": 345678,
  "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "generated_at": "2026-07-15T12:00:00Z",
  "redistribution": "local-only"
}
```

### 圖表裁切 Artifact

```json
{
  "id": "frequency-response-chart",
  "role": "chart-crop",
  "availability": "available",
  "local_path": "source/chart-frequency-response.png",
  "derived_from": "frequency-response-page",
  "derived_from_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "curve_files": ["frequency-response--typical.csv"],
  "derivation": {
    "crop_box_px": [120, 80, 920, 620],
    "crop_tool": "Pillow",
    "crop_tool_version": "10.4.0"
  },
  "media_type": "image/png",
  "size_bytes": 23456,
  "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "generated_at": "2026-07-15T12:00:00Z",
  "redistribution": "local-only"
}
```

衍生 artifact 不得有 `source_url`；它必須引用同 manifest 中的 `derived_from`，形成
`original → rendered-page → chart-crop` 的可稽核推導鏈。PDF 以 300 DPI 保存圖表頁，
再以像素 crop box 產生裁切圖。若原廠 SVG 本身只包含該圖表，原始 SVG 同時就是描線
來源，不強制另製無資訊增益的 PNG。

Manifest 的 `page` 是從 1 開始的 PDF 實體頁序，不是文件印刷頁碼；v1 `render_dpi` 固定
為 `300`。`crop_box_px` 採 `[left, top, right, bottom]`，原點在 rendered PNG 左上角，
right／bottom 不包含在裁切範圍內，且四值皆為整數並必須落在圖片邊界內。

## 官方 Host Policy

Repository 保存一份受測的全域 exact-host allowlist。初始 canonical hosts 為：

- `assets.sennheiser.com`
- `audixusa.com`
- `content-files.shure.com`
- `docs.audio-technica.com`
- `docs.cloud.sennheiser.com`
- `edge.rode.com`
- `products.electrovoice.com`
- `pubs.shure.com`
- `seelectronics.com`
- `www.akg.com`
- `www.lewitt-audio.com`
- `www.neumann.com`
- `www.sennheiser.com`

實際 redirect CDN 必須經一次人工檢查後，以 exact hostname 加入 policy。禁止 wildcard
suffix，避免 `evil-example.com` 類型的 hostname 混入。

## CLI 與資料流

工具為 `scripts/source_assets.py`。網路、TLS、路徑、hash 與 manifest 核心只使用 Python
標準函式庫；`crop-chart` 明確依賴 `requirements-source-assets.txt` 鎖定的 Pillow。
Manifest 使用 JSON，避免安全敏感下載工具依賴 repository 目前的最小 YAML parser。

### 首次建立信任

```bash
python3 scripts/source_assets.py bootstrap --all --accept-new
```

1. 讀取工作樹中的 draft manifest entry。
2. 驗證 URL、host、redirect 與目的路徑。
3. 將原檔串流下載到目的資料夾中的暫存檔。
4. 同時計算 SHA-256 與大小，並驗證實際內容 signature。
5. 成功後 `fsync` 並以 `os.replace` 原子發布。
6. 更新 manifest 為 `available`；安全可分類的來源失效則更新為 `unavailable`。
7. 輸出 available／unavailable／failed 摘要。

`--accept-new` 是明確的首次信任邊界，不得在 CI 或一般 restore 中使用。Draft 使用的
`pending` 狀態只允許存在於尚未提交的工作樹；schema 測試拒絕 committed `pending`。

### 恢復已知原檔

```bash
python3 scripts/source_assets.py fetch --all
python3 scripts/source_assets.py fetch --mic sennheiser-e906
```

`fetch` 只處理 manifest 已記錄 SHA-256 的 `available` 原廠 artifact。既有目的檔 hash
不符時拒絕覆寫；遠端內容 hash 不符時刪除暫存檔並失敗。工具不提供略過 hash 的
`--force`。

### 離線驗證

```bash
python3 scripts/source_assets.py verify --all
```

`verify` 不建立 socket，只檢查本機路徑、格式 signature、檔案大小、SHA-256 與
`derived_from_sha256` 鏈。`available` 缺檔／不符為失敗；`unavailable` 是合法、可稽核
的狀態，只列入摘要。

### 產生與恢復衍生圖

```bash
python3 scripts/source_assets.py render-page \
  --mic sennheiser-e906 \
  --id frequency-response-page \
  --from official-specification \
  --output page-02.png \
  --page 2 \
  --render-dpi 300 \
  --accept-new

python3 scripts/source_assets.py crop-chart \
  --mic sennheiser-e906 \
  --id frequency-response-chart \
  --from frequency-response-page \
  --output chart-frequency-response.png \
  --curve frequency-response--typical.csv \
  --crop-box 120,80,920,620 \
  --accept-new

python3 scripts/source_assets.py derive --all

python3 scripts/source_assets.py repair-stale \
  --mic sennheiser-e906 \
  --from official-specification \
  --accept-new
```

`render-page` 由工具直接呼叫 v1 唯一允許的 `pdftocairo` 產生 PNG；`crop-chart` 由工具
內部直接呼叫 Pillow 裁切。兩者都不接受外部既成輸出，也會自行查詢實際工具版本、
計算大小與 hash，記錄當下 parent 的 `derived_from_sha256`，再以原子方式寫入檔案與
manifest，因此記錄不會指向未曾執行的工具或已變更的 parent。
`--accept-new` 與 bootstrap 相同，是首次建立 hash 的人工信任邊界。

`--output` 只能是單一 `.png` basename；工具固定將其解析到該麥克風的 `source/`，拒絕
斜線、絕對路徑、`..`、既有 symlink 與重複 local path。Manifest 記錄正規化後的
`source/{output}`。`crop-chart` 至少要有一個 `--curve`，可重複傳入以對應同一張圖中的
多條正式曲線。

`derive` 只依既有 manifest 重播缺少的衍生 artifact；parent hash、安裝的工具名稱／版本
必須與記錄相同，產出大小與 hash 也必須完全相符，否則清除暫存檔並失敗。要接受 parent
或新工具版本造成的位元差異，必須按拓撲順序重新執行對應產生命令加 `--accept-new`，再由
人工審查 manifest diff。
新 clone 的完整恢復順序固定為 `fetch --all`、`derive --all`、`verify --all`。

`repair-stale` 是 parent 更新後的明確修復路徑。它只處理「artifact 自身檔案仍符合既有
hash，但 `derived_from_sha256` 不等於已驗證 parent」的 stale 節點，按拓撲順序重新產生
全部 descendants，逐筆更新 parent hash、artifact hash 與 timestamp。每一步仍使用暫存
檔與原子 manifest 更新。若在新 artifact 發布後、manifest 更新前中斷，重跑時會先從
目前已驗證 parent 與既有 recipe 重新產生候選暫存檔；只有候選的格式、大小與 SHA-256
和已發布但未登錄的 target 完全相同，才補完 manifest，否則視為未知修改並拒絕。如此
在 artifact 發布前、發布後或 manifest 更新後中斷都可安全接續。命令必須帶
`--accept-new`；一般 `derive` 與 `verify` 對 stale manifest 仍嚴格失敗。

## 網路與檔案安全

- 僅接受 HTTPS。
- 初始 URL 與每一個 redirect Location 都重新驗證，最多五跳。Redirect 只接受帶有效
  `Location` 的 301／302／303／307／308；最終成功 response 必須恰為 200。206、
  `Content-Range` 與其他 2xx／3xx 一律拒絕，避免把 partial content 當成完整原檔。
- 拒絕 HTTP downgrade、user-info、fragment、非 443 port、IP literal，以及
  loopback／private／link-local／reserved 解析結果。
- hostname 先正規化為小寫 IDNA 並移除尾端點，再做 exact match。
- 每一跳只解析 DNS 一次，且全部候選位址都必須是公開位址；工具只連到其中一個已驗證
  IP，TLS SNI、憑證驗證與 HTTP `Host` 仍使用原 hostname，並確認 socket peer 等於
  選定 IP。Redirect 重新執行完整解析與綁定流程，避免 DNS rebinding 的檢查／使用落差。
- Request 固定送出 `Accept-Encoding: identity`，並拒絕非 identity 的 `Content-Encoding`，
  避免壓縮內容繞過大小上限；relative redirect 先以目前 URL 正規化，再套用相同驗證。
- 每次 socket 操作 timeout 為 30 秒；bootstrap 單檔硬上限為 100 MiB。Fetch 若有
  Content-Length，必須與 manifest `size_bytes` 相符，串流超過 `size_bytes` 一個 byte
  即停止；Content-Length 只作早期拒絕，SHA-256 才是完整性權威。
- 下載內容必須通過格式 signature：PDF 以 `%PDF-` 開頭；SVG 必須安全解析 XML 且根
  element 為 `svg`，並拒絕 `DOCTYPE`／entity 宣告；PNG 必須符合八 byte PNG signature。
  HTTP `Content-Type` 只作診斷，不作內容權威；回傳 200 的 HTML 錯誤頁不得冒充 PDF、
  SVG 或 PNG。
- `local_path` 只准 `source/{filename}`，拒絕絕對路徑、`..`、巢狀逃逸與 symlink 逃逸。
- 暫存檔與目的檔位於同一資料夾，任何中斷、短檔、超限或 hash mismatch 都清理暫存檔。
- Manifest 也使用同資料夾暫存檔、`fsync` 與 `os.replace` 原子更新。若程式在 blob 發布後、
  manifest 更新前中斷，下一次相同 `pending` bootstrap 必須重新從已驗證官方 URL 下載到
  另一暫存檔，並要求新下載與 orphan blob 的格式、大小及 SHA-256 完全相同，才可在
  `--accept-new` 下接續採用。兩者不符時不得覆寫或自動刪除，命令失敗並要求人工處理。
- 既有 mismatch 檔永不自動覆寫。
- CLI 輸出不顯示 signed credential query string。

## 錯誤與 Exit Code

- `0`：所有要求的操作完成；明確記錄的 `unavailable` 不視為程式錯誤。
- `1`：manifest/schema、路徑、host policy、安全或本機 I/O 錯誤。
- `2`：下載、遠端內容、hash、大小或本機完整性驗證失敗。

Batch 操作會處理其餘安全可繼續的項目後輸出完整摘要；同時出現多類錯誤時回傳數值最大
的 exit code，因此下載／完整性錯誤 `2` 優先於本機契約錯誤 `1`。
`bootstrap` 只有在穩定 canonical URL 明確回傳 HTTP 404／410 時，才可自動將 draft 從
`pending` 改為 `unavailable`。已知沒有穩定端點或只能取得過期 signed URL 的來源，必須
由人工填入具體 reason 後標記。401／403／429、timeout、DNS／TLS、5xx、redirect policy
違反、HTML 錯誤頁與內容格式不符一律維持 `pending`／failed 並回傳非零，避免把暫時性
封鎖誤判成永久消失。

## 測試

### Repository 契約測試

新增 `digitizer/test/source-manifest.test.js`，由既有 `node --test` 自動執行：

- 44 個資料夾各有一份 manifest，且 `mic_slug` 與資料夾相同。
- 55 個現有官方來源觀測均被覆蓋：正規化 URL 出現在 artifact／reference；若包含真正
  signed credential，則由 redaction record 的原 URL hash、欄位位置與 canonical mapping
  覆蓋。URL 不再只藏於未結構化 note。
- JSON schema、ID 唯一性、derived graph 與 availability 欄位組合正確。
- 媒體類型／副檔名只接受 PDF、SVG、PNG 對應，且 redistribution 固定 local-only。
- available 原檔的 hash、大小、media type、RFC 3339 timestamp、redistribution 與 host 完整。
- unavailable 不帶假 hash 或假 local path。
- rendered-page 與 chart-crop 的欄位組合、工具版本及推導鏈完整。
- derived_from_sha256 與目前 parent 相符；parent 更新會讓未重建 descendant 失效。
- 每個 `meta.yaml curves[].file` 都有 original 覆蓋；available PDF descendant crop 完整
  且為 ancestor curve set 子集合；available SVG 直接覆蓋與 unavailable 無衍生義務正確。
- 所有 local path 受限於同一支麥克風的 `source/`。
- manifest 主來源可對應 `meta.yaml source.url`。
- 另有 draft fixture 驗證合法 `pending`；committed data tree 一律不含 `pending`。
- Signed credential URL 偵測與 redaction mapping 覆蓋完整，Git 追蹤內容不得含原始憑證。
- Redaction 的 replaced／no-stable-endpoint variant、canonical 對應與 redaction_ref 完整。

### CLI 行為測試

新增 `scripts/test_source_assets.py`，使用 injected transport／本機測試資料，不連公開網路：

- 成功下載及同 host 合法 absolute／relative redirect。
- allowlist 外 redirect、HTTP downgrade、redirect loop 與超過五跳。
- Redirect status allowlist、最終非 200、206 與 Content-Range 拒絕。
- hostname 正規化、私人位址、IP literal 與非標準 port。
- credential／expiry query 拒絕、版本化 opaque path 接受，以及 CLI URL redaction。
- DNS 綁定、socket peer 不符與每一跳重新解析。
- PDF／SVG／PNG signature 驗證，以及 200 HTML 錯誤頁拒絕。
- hash mismatch、size mismatch、下載中斷、超過上限與暫存檔清理。
- 非 identity Content-Encoding 與壓縮大小繞過拒絕。
- 目的檔已存在且 mismatch 時不覆寫。
- 原子發布、路徑穿越與 symlink 逃逸。
- render/crop output basename、重複 local path 與輸出 symlink 拒絕。
- 1-based page、固定 300 DPI、crop box 邊界與至少一個有效 curve mapping。
- verify 的零網路保證，以及即使 hash／大小吻合仍拒絕 signature 或副檔名不符。
- unavailable 與 derived artifact 的合法／非法組合。
- bootstrap 的 pending→available、404／410→unavailable、其他失敗維持 pending。
- `--accept-new` 必要性、entry `allowed_hosts` 子集合、batch exit code 優先序與摘要。
- orphan blob crash recovery 必須重下載比對；不符時不覆寫、不刪除並回傳失敗。
- render-page 固定執行 pdftocairo、crop-chart 固定使用 Pillow，並記錄實際版本。
- derive 的版本不符、重播 hash 不符、暫存檔清理與成功恢復。
- parent hash 變更後 stale descendant 的 verify／derive 拒絕與拓撲順序重建。
- repair-stale 可處理 self-hash 正確的 stale 節點；若在 artifact 發布後中斷，須重新產生
  候選並與 target 完整比對後補 manifest，且拒絕不同的未知修改。
- Fault injection 精確覆蓋 artifact `os.replace` 完成、manifest `os.replace` 尚未執行的
  中斷點，重跑後必須恢復為 verify exit 0。

GitHub CI 安裝 `poppler-utils` 與 `requirements-source-assets.txt`（Pillow 鎖版），再執行
manifest contract 與隔離的 CLI 測試；不要求被忽略的本機 blob 存在。測試 transport
與 subprocess runner 可注入，因此單元測試不連公開網路；另以小型 fixture 做一次真實
pdftocairo／Pillow 衍生鏈整合測試。本機完整快取另以 `source_assets.py verify --all`
驗證。

## 第一輪遷移

1. 為 44 支麥克風建立 manifest，將 55 個已知 URL 正規化為 artifact 或 reference。
2. 修正 AT2020 缺少 page locator，以及 SM35 URL 藏在 note、Neumann asset 混入文字等
   既有 provenance 缺漏；相容欄位暫不刪除，manifest 成為下載權威。
3. 對全部原廠 artifact 執行 bootstrap。
4. 可取得 PDF 保存完整原檔，再以 `render-page`／`crop-chart` 建立帶獨立 hash 的
   300 DPI rendered-page 與 chart-crop；可取得 chart-only SVG 保存原始 SVG。
5. 失效／signed credential／無穩定直接 URL 者標為 unavailable，保存檢查時間與具體原因。
6. 執行離線 verify，記錄 available／unavailable／failed 統計。
7. 更新 README、`data/README.md`、兩個 digitize skills 與 `.gitignore`，把政策統一為
   「本機保存、公開 repo 不重製」。

## 驗收條件

- 44 份 manifest 與 55 個來源全部被明確分類。
- 每個 available artifact（原檔、rendered-page 與 chart-crop）均真實存在於本機並通過
  格式、SHA-256 與大小驗證。
- 每個可用 PDF 有圖表頁與 crop；chart-only SVG 可直接作為圖表 artifact。
- 每個 unavailable entry 都有 RFC 3339 UTC 檢查時間與原因，且沒有 hash。
- `node --test`、CLI 行為測試、既有 overlay／trace／anchors 測試全數通過。
- `python3 scripts/source_assets.py verify --all` 回傳 `0`，摘要為 `failed=0`，且 committed
  manifest 沒有 `pending`。
- `git diff --check` 通過，Git 追蹤清單不包含任何原廠 blob。
- 文件不再宣稱原始來源只應放 scratch 或 repo 外，而是明確指定被忽略的本機
  `data/{slug}/source/`。

## 版權與資料邊界

原廠 artifact 一律預設 `redistribution: local-only`。只有來源附帶明確再散布授權並經
人工確認，且另案升版 schema 與政策時，才能變更；本 change 不把任何原廠 blob 加入
公開 Git history。
正式頻率／dB CSV 的 facts-only 邊界不變：20–20,000 Hz 仍只是顯示軸，CSV 仍只能保存
原廠正式範圍內且來源實際存在的點。
