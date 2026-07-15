# Source Artifact Preservation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 為 44 支麥克風建立可稽核的本機原廠來源保存系統，完成 55 個來源的 manifest、下載、標準化覆核圖、離線驗證與 CI 契約，同時確保原廠 blob 永不進入公開 Git history。

**Architecture:** 每支麥克風以 tracked `source-manifest.json` 描述來源與衍生鏈，以 ignored `source/` 保存 blob。Python CLI 分成 manifest／storage、受限 HTTPS transport、生命週期命令與衍生圖四個單一責任模組；Node contract test 使用 JSON Schema 驗證 tracked data，Python `unittest` 驗證 CLI 真實行為。

**Tech Stack:** Python 3.12+ 標準函式庫、Pillow 10.4.0、Poppler `pdftocairo`、Node.js 22、Node test runner、Ajv 8、GitHub Actions。

## Global Constraints

- 只使用台灣正體中文文件與錯誤訊息；保留既有英文 API／欄位名稱。
- Manifest schema version 固定為整數 `1`；tracked manifest 不得含 `pending`。
- Original 僅接受 PDF／SVG；衍生圖僅接受 PNG；`redistribution` 固定 `local-only`。
- PDF 覆核頁固定 300 DPI、1-based physical page；crop box 為左上原點、right/bottom exclusive。
- 只接受 HTTPS、exact-host allowlist、最多五次 redirect、最終 HTTP 200；初始 URL 與每次 redirect 都拒絕 user-info、fragment、IP literal、非 443 port、206、Content-Range 與非 identity encoding。
- DNS 位址必須綁定實際連線並驗證 TLS SNI／憑證與 socket peer；不得接受 private／loopback／link-local／reserved IP。
- 每次 socket 操作 timeout 為 30 秒；bootstrap 單檔上限 100 MiB；fetch 超過 manifest `size_bytes` 一個 byte 即停止。
- `--accept-new` 只用於人工建立或更新信任；一般 restore／CI 不得使用。
- 本機 blob 只准位於 `data/{slug}/source/`，而且永不加入 Git。
- 所有 production behavior 先寫測試、確認 RED，再寫最小實作、確認 GREEN。新檔案先以 `existsSync`／`importlib.util.find_spec` 的 assertion 測試介面尚不存在並觀察 RED，才建立「可讀取／可 import、但沒有行為」的 scaffold；之後每個列出的行為都各自走一次 RED→GREEN micro-cycle。RED 必須是 assertion failure（例如不存在、錯誤回傳值、未拒絕非法輸入或未拋出指定例外），不得把 uncaught `ModuleNotFoundError`／`FileNotFoundError` 當作 RED 證據。
- `DownloadRequest.expected_media_type` 是下載內容 signature 的權威；HTTP `Content-Type` 與 URL 副檔名只作診斷，opaque URL 也必須依此驗證。
- 所有 blob 與 manifest 都經 production `AtomicArtifactStore` 發布：暫存檔必須與 target 同資料夾，成功路徑依序 flush、file `fsync`、`os.replace`、parent directory `fsync`；任何失敗都清除尚存 temp。Producer／驗證／replace 失敗不得破壞既有 target；directory `fsync` 失敗發生在 replace 後，必須保留完整新 target、回報失敗並交由精確 crash-recovery 路徑判定。

## File Structure

- `schemas/source-manifest-v1.schema.json`: manifest v1 封閉 JSON Schema。
- `schemas/source-url-redactions-v1.schema.json`: signed URL redaction 封閉 schema。
- `config/source-hosts.json`: 13 個官方 exact host allowlist。
- `config/source-url-redactions.json`: 本次遷移的 credential URL redaction registry。
- `config/source-inventory-v1.json`: 55 筆來源觀測的固定遷移 inventory。
- `scripts/source_assets.py`: argparse entrypoint，只有參數解析與 exit code。
- `scripts/source_assets_lib/model.py`: manifest dataclass、schema mirror 與 repository semantic contract。
- `scripts/source_assets_lib/storage.py`: 路徑限制、signature、digest 與原子檔案操作。
- `scripts/source_assets_lib/network.py`: URL policy、DNS pinning、TLS 與串流下載。
- `scripts/source_assets_lib/commands.py`: bootstrap／fetch／verify 的狀態轉移。
- `scripts/source_assets_lib/derivation.py`: render-page／crop-chart／derive／repair-stale。
- `scripts/build_source_manifests.py`: 一次性 inventory 對帳與 draft manifest 產生器。
- `scripts/test_source_assets.py`: Python unit／integration tests。
- `digitizer/test/source-manifest.test.js`: tracked repository contract tests。
- `digitizer/test/fixtures/source-manifests/`: schema 與 graph 正反例 fixtures。
- `requirements-source-assets.txt`: Pillow exact version。
- `.github/workflows/ci.yml`: offline CI contract。

---

### Task 1: JSON Schema、Host Policy 與 Node Contract Fixtures

**Files:**
- Create: `schemas/source-manifest-v1.schema.json`
- Create: `schemas/source-url-redactions-v1.schema.json`
- Create: `config/source-hosts.json`
- Create: `config/source-url-redactions.json`
- Create: `digitizer/test/fixtures/source-manifests/available-pdf.json`
- Create: `digitizer/test/fixtures/source-manifests/pending-pdf.json`
- Create: `digitizer/test/fixtures/source-manifests/unavailable-redacted.json`
- Create: `digitizer/test/fixtures/source-manifests/derived-chain.json`
- Create: `digitizer/test/fixtures/source-manifests/redaction-replaced.json`
- Create: `digitizer/test/fixtures/source-manifests/redaction-no-stable-endpoint.json`
- Create: `digitizer/test/source-manifest.test.js`
- Modify: `digitizer/package.json`
- Create: `digitizer/package-lock.json`

**Interfaces:**
- Produces: JSON Schema `$id` values `https://psychquantmusic.github.io/mic-frequency-response/schemas/source-manifest-v1.schema.json` and `.../source-url-redactions-v1.schema.json`.
- Produces: `config/source-hosts.json` shape `{ "schema_version": 1, "hosts": string[] }`.
- Produces: Ajv validator used by later repository tests.

- [ ] **Step 1: Write production-file existence assertions and observe RED before creating files**

Add `"ajv": "8.17.1"` to the existing `digitizer/package.json`, but do not run install yet. Create `digitizer/test/source-manifest.test.js` using only Node built-ins. Before creating any new production file, evaluate the complete missing-path list for both schemas, `config/source-hosts.json`, `config/source-url-redactions.json`, and `digitizer/package-lock.json`, then assert it equals `[]`:

```js
test('source contract schemas exist', () => {
  const missing = [
    manifestSchemaPath,
    redactionSchemaPath,
    sourceHostsPath,
    sourceRedactionsPath,
    packageLockPath,
  ]
    .filter((path) => !existsSync(path))
    .map((path) => fileURLToPath(path));
  assert.deepEqual(missing, []);
});
```

Run: `cd digitizer && node --test test/source-manifest.test.js`

Expected: assertion FAIL with an actual missing-path array containing all five new production files; the expression evaluates every path before asserting and no file-read exception occurs.

- [ ] **Step 2: Install Ajv, create non-behavioral scaffolds, then write the first behavior assertion**

From the `digitizer/` working directory run `npm install` to create `package-lock.json`. Create both permissive schemas plus `{}` scaffolds for the two config JSON files, rerun the existence test, and confirm GREEN.

Create the manifest schema scaffold:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psychquantmusic.github.io/mic-frequency-response/schemas/source-manifest-v1.schema.json"
}
```

Create an equivalent scaffold with the redaction `$id` for `source-url-redactions-v1.schema.json`. Create the complete valid manifest fixtures, then add only the manifest unknown-field rejection assertion:

```js
import Ajv from 'ajv/dist/2020.js';
import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import test from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';

const ROOT = new URL('../../', import.meta.url);
const schemaPath = new URL('schemas/source-manifest-v1.schema.json', ROOT);

test('manifest schema rejects an unknown top-level field', () => {
  const schema = JSON.parse(readFileSync(schemaPath, 'utf8'));
  const validate = new Ajv({ strict: true }).compile(schema);
  const fixture = JSON.parse(readFileSync(join(import.meta.dirname, 'fixtures/source-manifests/derived-chain.json')));
  fixture.unknown = true;
  assert.equal(validate(fixture), false, JSON.stringify(validate.errors));
});
```

- [ ] **Step 3: Run behavior tests and verify RED**

Run: `cd digitizer && node --test test/source-manifest.test.js`

Expected: one assertion failure because the permissive manifest schema accepts `unknown`; imports and schema compilation succeed.

- [ ] **Step 4: Implement one schema rule per RED→GREEN micro-cycle**

First add `additionalProperties: false` and confirm the manifest assertion GREEN. Then, one isolated fixture copy at a time, add a failing assertion and the minimum manifest schema branch needed for: all five allowed `(role, availability)` variants, `.png` original rejection, non-local redistribution, missing `derived_from_sha256`, non-UTC RFC 3339 timestamp, forbidden pending-only／available-only／unavailable-only fields, and unavailable entries that contain both `source_url` and `redaction_ref`. Run the focused test after every assertion before and after its implementation. Only after every manifest case is GREEN, create the two redaction fixtures, add the redaction unknown-field assertion, observe RED, then implement the redaction cycles below.

After both schemas are GREEN, add a failing assertion for the exact 13-host policy against the `{}` host scaffold, implement the exact object, then add a failing assertion for the empty schema-v1 redaction registry against its `{}` scaffold and implement it. Keep these two config cycles isolated.

For the redaction schema, independently cycle through both closed variants. Every entry requires exactly `id`, `mic_slug`, `source_field`, `legacy_url_sha256`, `checked_at`, and `disposition`; `replaced` additionally requires `canonical_url`, while `no-stable-endpoint` forbids it. Assert unknown fields, raw credential URL fields, duplicate IDs, invalid SHA, non-UTC timestamp, missing canonical replacement, and canonical URL on `no-stable-endpoint` all fail. The repository validator, added later, enforces global ID uniqueness and manifest-reference relationships that JSON Schema cannot express.

Use `oneOf` branches keyed by `role`／`availability`, `additionalProperties: false`, 64-lowercase-hex SHA patterns, RFC 3339 UTC timestamps, enum media pairs, `local-only` const, and the exact 13 hosts:

```json
{
  "schema_version": 1,
  "hosts": [
    "assets.sennheiser.com",
    "audixusa.com",
    "content-files.shure.com",
    "docs.audio-technica.com",
    "docs.cloud.sennheiser.com",
    "edge.rode.com",
    "products.electrovoice.com",
    "pubs.shure.com",
    "seelectronics.com",
    "www.akg.com",
    "www.lewitt-audio.com",
    "www.neumann.com",
    "www.sennheiser.com"
  ]
}
```

The redaction registry starts as:

```json
{
  "schema_version": 1,
  "entries": []
}
```

- [ ] **Step 5: Verify all positive／negative fixture tests GREEN**

Run: `cd digitizer && node --test test/source-manifest.test.js`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add schemas config digitizer/package.json digitizer/package-lock.json digitizer/test/source-manifest.test.js digitizer/test/fixtures/source-manifests
git commit -m "test: define source manifest v1 contract (#19)"
```

### Task 2: Python Manifest Model、Path Safety 與 Offline Verification

**Files:**
- Create: `scripts/source_assets_lib/__init__.py`
- Create: `scripts/source_assets_lib/model.py`
- Create: `scripts/source_assets_lib/storage.py`
- Create: `scripts/test_source_assets.py`

**Interfaces:**
- Produces: `ContractError`, `IntegrityError`, `RemoteError` with exit codes `1`, `2`, `2`.
- Produces: `Policy(hosts: frozenset[str], redactions: dict[str, dict])`.
- Produces: `load_manifest(path: Path, allow_pending: bool) -> dict`.
- Produces: `validate_manifest(document: dict, mic_dir: Path, policy: Policy, allow_pending: bool) -> None`.
- Produces: `resolve_local_path(mic_dir: Path, local_path: str) -> Path`.
- Produces: `digest_file(path: Path) -> FileDigest(size_bytes: int, sha256: str)`.
- Produces: `validate_signature(path: Path, media_type: str) -> None`.
- Produces: production `ArtifactStore` protocol:

```python
BlobProducer = Callable[[BinaryIO], object]

class ArtifactStore(Protocol):
    def publish_blob(
        self,
        target: Path,
        media_type: str,
        producer: BlobProducer,
    ) -> tuple[object, FileDigest]: ...

    def publish_manifest(self, target: Path, document: dict) -> None: ...

class AtomicArtifactStore:
    # Production implementation of ArtifactStore; used by every command.
    ...
```

`publish_blob` calls `producer` with a same-directory temp handle, flushes and fsyncs it, validates the signature and digest, replaces the target, then fsyncs the parent directory. `publish_manifest` serializes canonical UTF-8 JSON with trailing newline and uses the same durability sequence. This is the real storage boundary for downloads, derivations and manifest changes, not a test-only API.

- [ ] **Step 1: Write interface-existence assertions and observe RED before creating modules**

Create `scripts/test_source_assets.py` without importing the absent modules at top level. Build a list of all three missing paths—`source_assets_lib/__init__.py`, `model.py` and `storage.py`—then use one `self.assertEqual(missing_paths, [])`; the list comprehension must evaluate every path before asserting. Run `python3 scripts/test_source_assets.py InterfaceExistenceTests`.

Expected: assertion FAIL whose actual value lists all three missing module paths; no uncaught import error occurs.

- [ ] **Step 2: Create importable scaffolds, then write the first failing behavior test**

Create `__init__.py`, the exceptions/dataclasses/protocol signatures above, and minimal no-op implementations that return without enforcing policy. Confirm `InterfaceExistenceTests` GREEN. Add only `test_resolve_local_path_rejects_parent_escape`; because the scaffold returns a path instead of raising, `assertRaises(ContractError)` must fail as an assertion.

```python
class StorageContractTests(unittest.TestCase):
    def test_resolve_local_path_rejects_parent_escape(self):
        with tempfile.TemporaryDirectory() as td:
            mic = Path(td) / "data" / "mic"
            mic.mkdir(parents=True)
            with self.assertRaises(ContractError):
                resolve_local_path(mic, "source/../meta.yaml")
```

- [ ] **Step 3: Run the first behavior test and verify RED**

Run: `python3 scripts/test_source_assets.py StorageContractTests`

Expected: assertion FAIL because parent escape is accepted; all imports succeed.

- [ ] **Step 4: Implement contract and storage behavior in isolated RED→GREEN micro-cycles and two reviewable commits**

Storage phase: make parent escape GREEN, then add exactly one failing assertion before each minimum implementation for symlink escape, PDF／PNG signature, SVG `DOCTYPE`／entity rejection, digest and atomic publication. For atomic publication, record filesystem events and assert exact order `write → flush → file fsync → os.replace → directory fsync`; assert the temp parent equals `target.parent`; inject producer, validator, replace and directory-fsync failures separately. Producer／validator／replace failure must leave the old target byte-for-byte unchanged; directory-fsync failure occurs after replace, must report failure while retaining the fully replaced target for the documented recovery path. Every failure leaves no temp file. Commit this phase as `feat: add safe source artifact storage (#19)`.

Manifest phase: add exactly one failing assertion before each minimum implementation for duplicate local path, invalid timestamp, graph cycle, parent hash mismatch, curve coverage, stale descendant and unavailable redaction references. Commit this phase as `feat: validate source artifact manifests offline (#19)`.

```python
@dataclass(frozen=True)
class FileDigest:
    size_bytes: int
    sha256: str

class SourceAssetError(Exception):
    exit_code = 1

class ContractError(SourceAssetError):
    exit_code = 1

class IntegrityError(SourceAssetError):
    exit_code = 2

def digest_file(path: Path) -> FileDigest:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return FileDigest(size, digest.hexdigest())
```

For SVG, reject case-insensitive `<!DOCTYPE` and `<!ENTITY` before `xml.etree.ElementTree.fromstring`; require the local-name of root tag to equal `svg`.

Create temp files with `tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)`, set mode `0o600`, flush and `os.fsync(handle.fileno())`, then `os.replace(temp, target)`. Open `target.parent` with `os.open(..., os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))`, `os.fsync(dir_fd)`, and close it. A `finally` block unlinks only the temp path if it still exists.

- [ ] **Step 5: Run focused and regression tests**

Run: `python3 scripts/test_source_assets.py StorageContractTests`

Expected: PASS.

Run: `cd digitizer && node --test`

Expected: all existing tests plus source manifest fixture tests PASS.

- [ ] **Step 6: Confirm the two task commits contain only Task 2 files**

Run `git show --stat --oneline HEAD~1..HEAD` and `git diff --name-only HEAD~2..HEAD`; expected paths are limited to `scripts/source_assets_lib/` and `scripts/test_source_assets.py`.

### Task 3: Pinned HTTPS Transport

**Files:**
- Create: `scripts/source_assets_lib/network.py`
- Modify: `scripts/test_source_assets.py`

**Interfaces:**
- Produces: `URLPolicy.parse(url: str, allowed_hosts: frozenset[str]) -> ValidatedURL`.
- Produces: `resolve_public_addresses(hostname: str) -> tuple[str, ...]`.
- Produces: `Transport` protocol with `download(request: DownloadRequest, target: BinaryIO) -> DownloadResult`.
- Produces: `PinnedHTTPSClient.download(request: DownloadRequest, target: BinaryIO) -> DownloadResult`.
- Produces these exact immutable records and transport seams:

```python
@dataclass(frozen=True)
class ValidatedURL:
    normalized_url: str
    hostname: str
    port: int
    request_target: str
    redacted_display: str

@dataclass(frozen=True)
class DownloadRequest:
    url: str
    allowed_hosts: frozenset[str]
    expected_media_type: str
    expected_size: int | None
    max_size: int
    expected_sha256: str | None

@dataclass(frozen=True)
class DownloadResult:
    final_url: str
    size_bytes: int
    sha256: str
    content_type: str | None
    status: int

class RemoteHTTPError(RemoteError):
    def __init__(self, status: int, url: str): ...
    status: int
    redacted_url: str

class ConnectedHTTPS(Protocol):
    @property
    def peer_ip(self) -> str: ...
    def request(self, method: str, target: str, headers: dict[str, str]) -> None: ...
    def getresponse(self) -> HTTPResponse: ...
    def close(self) -> None: ...

class Connector(Protocol):
    def connect(
        self,
        *,
        ip_address: str,
        server_hostname: str,
        port: int,
        timeout_seconds: float,
    ) -> ConnectedHTTPS: ...

class Resolver(Protocol):
    def __call__(self, hostname: str) -> tuple[str, ...]: ...

class PinnedHTTPSClient:
    def __init__(
        self,
        resolver: Resolver = resolve_public_addresses,
        connector: Connector | None = None,
        *,
        timeout_seconds: float = 30.0,
        max_redirects: int = 5,
    ) -> None: ...

    def download(self, request: DownloadRequest, target: BinaryIO) -> DownloadResult: ...
```

`RemoteHTTPError` stores the numeric status for lifecycle classification but stringifies only a credential-redacted URL. Production uses `PinnedTLSConnector`; tests replace only `Resolver`／`Connector`, the socket-HTTP boundary.

- [ ] **Step 1: Write an interface-existence assertion and observe RED before creating the module**

Add an assertion that `(ROOT / "scripts/source_assets_lib/network.py").exists()` is true to `InterfaceExistenceTests`, then run that test.

Expected: assertion FAIL because the module spec is `None`; no uncaught import error occurs.

- [ ] **Step 2: Create transport scaffolds, then write the first failing policy test**

Create the records, protocols, constructor and no-policy `URLPolicy.parse` scaffold. Confirm the existence assertion GREEN. Add `test_parse_rejects_user_info`; because the scaffold accepts it, `assertRaises(ContractError)` must fail as an assertion.

Do not add the remaining policy or transport assertions until the first user-info cycle is GREEN.

- [ ] **Step 3: Run the first policy test and verify RED**

Run: `python3 scripts/test_source_assets.py NetworkPolicyTests PinnedHTTPSTests`

Expected: assertion FAIL because a URL with user-info is accepted; all imports succeed.

- [ ] **Step 4: Implement URL policy and commit it**

Cycle separately through exact host matching, lower-case IDNA normalization, credential query rejection, harmless `?id=968996`, fragment, user-info, IP literal, non-443 port and log redaction. Commit the GREEN URL-policy slice as `feat: validate official source URLs (#19)`.

- [ ] **Step 5: Implement pinned transport through isolated RED→GREEN cycles and commit it**

Add one failing assertion and minimum implementation at a time for public-address requirement, peer mismatch, TLS hostname preservation, relative redirects, redirect loops, status allowlist, 206／Content-Range, identity encoding, size limits, HTML-as-PDF and opaque SVG URL signature validation. Redirect tests separately prove that user-info and fragment in every `Location` are rejected after `urljoin` and before DNS resolution.

The transport fake is limited to the socket／HTTP boundary and returns the complete response shape consumed by production code:

```python
@dataclass
class ResponseFixture:
    status: int
    headers: dict[str, str]
    body: bytes
    peer_ip: str

def test_download_rejects_peer_different_from_pinned_ip(self):
    connector = ConnectorFixture(peer_ip="93.184.216.35", body=b"%PDF-1.7\n")
    client = PinnedHTTPSClient(resolver=lambda host: ("93.184.216.34",), connector=connector)
    with self.assertRaises(ContractError):
        client.download(pdf_request(), io.BytesIO())
```

Use `socket.getaddrinfo` once per hop, require every candidate `ipaddress.ip_address(ip).is_global`, connect to one selected IP, wrap with `ssl.create_default_context().wrap_socket(..., server_hostname=hostname)`, send the original hostname as `Host`, and compare `getpeername()[0]` to the selected IP.

```python
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
SIGNED_QUERY_KEYS = frozenset({"token", "sig", "signature", "expires"})

def is_signed_query_key(key: str) -> bool:
    normalized = key.casefold()
    return normalized in SIGNED_QUERY_KEYS or normalized.startswith("x-amz-") or normalized.startswith("x-goog-")
```

Always send `Accept-Encoding: identity`; apply a 30-second timeout to connect／read operations; stream into the caller-provided temporary file while computing hash and enforcing `expected_size + 1`／100 MiB limits. Validate bytes against `request.expected_media_type` before the caller may publish them. Treat response `Content-Type` and URL suffix as diagnostic only; this is what makes opaque Neumann SVG paths safe.

Commit the GREEN transport slice as `feat: add pinned HTTPS source downloader (#19)`.

- [ ] **Step 6: Verify GREEN**

Run: `python3 scripts/test_source_assets.py NetworkPolicyTests PinnedHTTPSTests`

Expected: PASS with no public network access.

- [ ] **Step 7: Confirm both Task 3 commits are scoped**

Run `git diff --name-only HEAD~2..HEAD`; expected paths are only `scripts/source_assets_lib/network.py` and `scripts/test_source_assets.py`.

### Task 4: Bootstrap、Fetch、Verify 與 CLI Exit Contracts

**Files:**
- Create: `scripts/source_assets_lib/commands.py`
- Create: `scripts/source_assets.py`
- Modify: `scripts/test_source_assets.py`

**Interfaces:**
- Produces: `OperationSummary(available: int, unavailable: int, failed: int, exit_code: int)`.
- Produces: `Selector.all()` and `Selector.mic(slug: str)` as the only batch selectors.
- Produces: `CommandContext(transport: Transport, store: ArtifactStore, now: Callable[[], datetime])`; CLI constructs it with `PinnedHTTPSClient`, `AtomicArtifactStore` and an aware UTC clock. This internal dependency bundle is used by every command and is the only injection seam.
- Produces: `bootstrap(root: Path, selector: Selector, accept_new: bool, context: CommandContext) -> OperationSummary`.
- Produces: `fetch(root: Path, selector: Selector, context: CommandContext) -> OperationSummary`.
- Produces: `verify(root: Path, selector: Selector) -> OperationSummary` with zero network calls.
- Produces: `main(argv: Sequence[str] | None = None) -> int`.

- [ ] **Step 1: Write lifecycle interface-existence assertions and observe RED before creating files**

Extend `InterfaceExistenceTests` to build the complete list of missing `scripts/source_assets_lib/commands.py` and `scripts/source_assets.py` paths, then assert that list equals `[]`; both paths are evaluated before the one assertion. Run that test before creating either file.

Expected: assertion FAIL because the module/file is absent; no uncaught import error occurs.

- [ ] **Step 2: Create lifecycle scaffolds, then write the first failing transition test**

Create the dataclasses and functions with a deterministic failed summary scaffold plus an argparse entrypoint returning `1`. Confirm existence tests GREEN. Add only the pending→available test first; it must fail an equality assertion while imports succeed.

Do not add the remaining lifecycle or CLI assertions until the first transition is GREEN.

```python
def test_bootstrap_changes_pending_pdf_to_available(self):
    repo = make_repo_with_pending_pdf()
    transport = StaticTransport(body=b"%PDF-1.7\n")
    summary = bootstrap(repo.root, Selector.all(), True, context_with(transport))
    self.assertEqual(summary.exit_code, 0)
    self.assertEqual(load_json(repo.manifest)["artifacts"][0]["availability"], "available")
```

- [ ] **Step 3: Run and verify the first transition RED**

Run: `python3 scripts/test_source_assets.py LifecycleCommandTests CLITests`

Expected: assertion FAIL because the scaffold reports exit `1` and leaves the entry pending; all imports succeed.

- [ ] **Step 4: Implement lifecycle behavior through RED→GREEN cycles and commit**

Add one failing assertion and minimum implementation per case: `RemoteHTTPError.status` 404／410→unavailable; 401／403／429／5xx and generic timeout remaining pending; `--accept-new` requirement; batch numeric-max exit precedence; existing mismatch refusal; orphan re-download equality; and artifact-before-manifest publication. Use `CommandContext` with the real production `AtomicArtifactStore` for integration cases and a protocol-conforming recording store only for publication-order assertions. Commit GREEN lifecycle behavior as `feat: add source artifact lifecycle commands (#19)`.

- [ ] **Step 5: Implement argparse behavior through RED→GREEN cycles and commit**

Add one failing assertion and minimum implementation per case for selector exclusivity, exit-code propagation, exact summary output, exact no-manifest error and token-free output.

CLI grammar:

```text
source_assets.py bootstrap (--all | --mic SLUG) --accept-new
source_assets.py fetch (--all | --mic SLUG)
source_assets.py verify (--all | --mic SLUG)
```

Write artifact bytes through `context.store.publish_blob` before writing the manifest through `context.store.publish_manifest`. On a bootstrap crash orphan, re-download to a new same-directory temp and require signature／size／hash equality before completing the manifest. A mismatch must neither overwrite nor delete the orphan. Summary output is one deterministic line:

```text
available=47 unavailable=0 failed=0
```

Commit GREEN CLI behavior as `feat: add source artifact lifecycle CLI (#19)`.

- [ ] **Step 6: Verify GREEN and offline guarantee**

Run: `python3 scripts/test_source_assets.py LifecycleCommandTests CLITests`

Expected: PASS.

Run: `python3 scripts/source_assets.py verify --all`

Expected at this stage: exit `1` and exact stderr `找不到 source-manifest.json；請先執行來源 manifest 遷移\n`; stdout is empty and no resolver／socket method is called.

- [ ] **Step 7: Confirm both Task 4 commits are scoped**

Run `git diff --name-only HEAD~2..HEAD`; expected paths are only `scripts/source_assets.py`, `scripts/source_assets_lib/commands.py` and `scripts/test_source_assets.py`.

### Task 5: Render、Crop、Derive 與 Stale Repair

**Files:**
- Create: `requirements-source-assets.txt`
- Create: `scripts/source_assets_lib/derivation.py`
- Modify: `scripts/source_assets.py`
- Modify: `scripts/test_source_assets.py`

**Interfaces:**
- Produces: `CropBox = tuple[int, int, int, int]`.
- Produces: `render_page(root: Path, mic: str, artifact_id: str, parent_id: str, output: str, page: int, dpi: int, accept_new: bool, context: CommandContext) -> OperationSummary`.
- Produces: `crop_chart(root: Path, mic: str, artifact_id: str, parent_id: str, output: str, crop_box: CropBox, curves: Sequence[str], accept_new: bool, context: CommandContext) -> OperationSummary`.
- Produces: `derive(root: Path, selector: Selector, context: CommandContext) -> OperationSummary`.
- Produces: `repair_stale(root: Path, mic: str, parent_id: str, accept_new: bool, context: CommandContext) -> OperationSummary`.
- Reuses Task 2 production `ArtifactStore` via `CommandContext`; all generated PNGs and manifest updates use that same real atomic storage boundary.

- [ ] **Step 1: Write dependency/module existence assertions and observe RED before creating files**

Extend `InterfaceExistenceTests` to build the complete list of missing `requirements-source-assets.txt` and `scripts/source_assets_lib/derivation.py` paths, then assert that list equals `[]`; both paths are evaluated before the one assertion. Run before creating either file.

Expected: assertion FAIL because the requirement/module is absent; no uncaught import error occurs.

- [ ] **Step 2: Install exact dependencies, preflight Poppler and create derivation scaffolds**

Create `requirements-source-assets.txt` containing exactly `Pillow==10.4.0`, run `python3 -m pip install -r requirements-source-assets.txt`, then run `pdftocairo -v` and require exit `0`. If the executable is absent on macOS, install it with `brew install poppler`; CI installs `poppler-utils`. Record the detected version only in generated manifest recipes, not as a hard-coded test expectation. Create the exact function signatures above with deterministic failed-summary bodies and confirm existence tests GREEN.

- [ ] **Step 3: Write the first failing derivation behavior test**

Create a one-page PDF in a temporary directory with Pillow. First assert that `render_page` rejects `dpi=299`; it must fail because the scaffold does not enforce the value. Do not add the remaining derivation assertions until this first cycle is GREEN.

- [ ] **Step 4: Run and verify RED**

Run: `python3 scripts/test_source_assets.py DerivationTests`

Expected: assertion FAIL because `dpi=299` is not rejected; all imports and Pillow fixture creation succeed.

- [ ] **Step 5: Implement render/crop through RED→GREEN cycles and commit**

Cycle through real `pdftocairo` output, 300-DPI enforcement, 1-based page, basename-only output, bounded crop, repeatable curves, tool versions and `derived_from_sha256`. Commit GREEN render/crop behavior as `feat: generate source review images (#19)`.

- [ ] **Step 6: Implement derive/stale repair through RED→GREEN cycles and commit**

Cycle through replay hash, stale rejection, topological repair and unknown self-hash refusal, then add the exact blob-replace／manifest-not-replaced crash recovery test below. Commit GREEN replay/repair behavior as `feat: repair stale source review images (#19)`.

For crash recovery, wrap a real `AtomicArtifactStore` in a protocol-conforming `CrashAfterBlobStore` that delegates `publish_blob`, then raises before delegating `publish_manifest`. Add this test only after the previous stale-repair cycle is GREEN:

```python
def test_repair_stale_recovers_after_blob_publish_crash(self):
    repo = make_verified_chain()
    update_parent(repo)
    with self.assertRaises(SimulatedCrash):
        repair_stale(repo.root, repo.slug, "official-primary", True, context_with_store(CrashAfterBlobStore()))
    summary = repair_stale(repo.root, repo.slug, "official-primary", True, production_context())
    self.assertEqual(summary.exit_code, 0)
    self.assertEqual(verify(repo.root, Selector.all()).exit_code, 0)
```

This proves the production store published the blob and the command can safely finish the omitted manifest publication on retry; it does not add a test-only public parameter.

Invoke `pdftocairo -png -r 300 -f PAGE -l PAGE -singlefile INPUT OUTPUT_PREFIX`; discover its version from `pdftocairo -v`. Pillow performs the crop and records `PIL.__version__`. Use basename-only output validation and repeatable `--curve` arguments.

Add CLI grammar:

```text
source_assets.py render-page --mic SLUG --id ID --from ID --output FILE.png --page N --render-dpi 300 --accept-new
source_assets.py crop-chart --mic SLUG --id ID --from ID --output FILE.png --curve FILE.csv [--curve FILE.csv ...] --crop-box L,T,R,B --accept-new
source_assets.py derive (--all | --mic SLUG)
source_assets.py repair-stale --mic SLUG --from ID --accept-new
```

- [ ] **Step 7: Verify GREEN with real Poppler／Pillow**

Run: `python3 -m pip install -r requirements-source-assets.txt`

Then run: `python3 scripts/test_source_assets.py DerivationTests`

Expected: PASS.

- [ ] **Step 8: Confirm both Task 5 commits are scoped**

Run `git diff --name-only HEAD~2..HEAD`; expected paths are limited to `requirements-source-assets.txt`, `scripts/source_assets.py`, `scripts/source_assets_lib/derivation.py` and `scripts/test_source_assets.py`.

### Task 6: Fixed Source Inventory 與 Draft Builder

**Files:**
- Create: `config/source-inventory-v1.json`
- Create: `scripts/build_source_manifests.py`
- Modify: `scripts/test_source_assets.py`

**Interfaces:**
- Produces these exact immutable records:

```python
@dataclass(frozen=True)
class SourceObservation:
    mic_slug: str
    source_field: str
    url: str
    curve_files: tuple[str, ...]

    def key(self) -> tuple[str, str, str]:
        return (self.mic_slug, self.source_field, self.url)

@dataclass(frozen=True)
class InventoryEntry:
    mic_slug: str
    source_field: str
    url: str
    kind: Literal["artifact", "reference"]
    role: Literal["original", "product-page", "landing-page", "documentation-page"]
    artifact_id: str | None
    media_type: Literal["application/pdf", "image/svg+xml"] | None
    local_path: str | None
    allowed_hosts: tuple[str, ...]
    physical_page: int | None
    curve_files: tuple[str, ...]

    def key(self) -> tuple[str, str, str]:
        return (self.mic_slug, self.source_field, self.url)
```

- Produces: `extract_source_observations(root: Path) -> list[SourceObservation]`.
- Produces: `load_inventory(path: Path) -> list[InventoryEntry]`.
- Produces: `build_draft_documents(root: Path, inventory: Sequence[InventoryEntry]) -> dict[str, dict]`; this pure function does not write the real data tree.
- Produces: CLI `scripts/build_source_manifests.py --check` and `--write-drafts`; only the latter writes via `AtomicArtifactStore`.
- Inventory exact contract: 55 unique URLs = 47 original artifacts (43 PDF, 4 SVG) + 8 references across 13 hosts.

- [ ] **Step 1: Write inventory-file existence assertions and observe RED before creating files**

Extend `InterfaceExistenceTests` to build the complete list of missing `config/source-inventory-v1.json` and `scripts/build_source_manifests.py` paths, then assert that list equals `[]`; both paths are evaluated before the one assertion. Run before creating either file.

Expected: assertion FAIL because both files are absent; no file-read/import error occurs.

- [ ] **Step 2: Create inventory scaffolds, then write the first failing observation test**

Create the records and functions with `extract_source_observations` returning `[]`, plus an inventory document containing an empty `sources` array. Confirm existence tests GREEN. Add the exact-55 assertion; it must fail as `0 != 55` while imports succeed.

```python
def test_inventory_matches_all_meta_source_observations(self):
    observed = extract_source_observations(ROOT)
    inventory = load_inventory(ROOT / "config/source-inventory-v1.json")
    self.assertEqual(len({item.url for item in observed}), 55)
    self.assertEqual({item.key() for item in observed}, {item.key() for item in inventory})
```

- [ ] **Step 3: Implement extraction and inventory in RED→GREEN micro-cycles**

After the URL total is GREEN, add one failing assertion before each implementation for field origin distribution (38 primary PDFs, 2 `spec_sheet`, 1 `alt_source`, 1 `also_published_at`, 1 SM35 note PDF, 4 Neumann SVG assets, 7 product-page references, 1 documentation reference), 44 microphone directories, 57 curve files, seven multi-curve directory counts, inventory key equality, unique IDs／local paths, host subset and pure draft generation.

The importer parses only the controlled `source:` mapping and `curves[].file`; it is not a general YAML parser. The checked inventory, not heuristic code, carries kind, role, page and naming decisions. Normalize AKG C451 to physical PDF page 15; leave AT2020 page as an explicit non-null value only after inspecting the downloaded official PDF during Task 7. Opaque Neumann `/svg/Zz...==` paths remain intact; annotated prose never enters a URL field.

Artifact naming rules:

```text
primary original: official-primary -> source/original.pdf|svg
secondary original: official-alternate-N -> source/original-alternate-N.pdf
rendered page: <parent-id>-page -> source/<parent-id>-page-NNN.png
chart crop: <parent-id>-chart -> source/<parent-id>-chart.png
```

- [ ] **Step 4: Verify focused and regression tests**

Run: `python3 scripts/test_source_assets.py InventoryMigrationTests`

Expected: PASS with 55/55 observation coverage and 44 draft documents produced only in temporary test directories.

Run: `cd digitizer && node --test`

Expected: PASS; no real `data/*/source-manifest.json` has been committed yet.

- [ ] **Step 5: Commit only inventory code and tests**

```bash
git add config/source-inventory-v1.json scripts/build_source_manifests.py scripts/test_source_assets.py
git commit -m "feat: inventory official source artifacts (#19)"
```

### Task 7: Atomic 44-Manifest Migration、Local Originals 與 Reviewed Crops

**Files:**
- Modify: `digitizer/test/source-manifest.test.js`
- Modify: `config/source-inventory-v1.json` only to replace the inspected AT2020 physical-page placeholder.
- Modify: `config/source-url-redactions.json` only if a source requires a safe `replaced` or `no-stable-endpoint` record.
- Create: `data/*/source-manifest.json` (44 files)
- Modify: `data/audio-technica-at2020/meta.yaml`
- Modify: `data/shure-sm35/meta.yaml`
- Modify only if normalization is required: `data/neumann-km184/meta.yaml`, `data/neumann-tlm102/meta.yaml`, `data/neumann-tlm103/meta.yaml`, `data/neumann-u87-ai/meta.yaml`
- Local ignored: `data/*/source/*`

**Interfaces:**
- Consumes: Task 6 inventory and draft builder.
- Produces: a tracked repository state with all 44 manifests valid at commit time; no intermediate commit may contain an available PDF without its rendered page and chart crop.
- Produces: at most 47 originals + 43 rendered pages + 43 crops = 133 verified local artifacts. With `P` available PDF originals and `S` available SVG originals, exact available count is `3 * P + S`; unavailable originals have no descendants.

- [ ] **Step 1: Add the first tracked-repository assertion and verify RED**

Add only the assertion that exactly 44 data directories contain `source-manifest.json`. Run the focused Node test; imports and schema loading must succeed, and the assertion must fail with actual count `0` rather than a file-not-found error.

Run: `cd digitizer && node --test test/source-manifest.test.js`

Expected: assertion FAIL, expected 44 and actual 0.

- [ ] **Step 2: Generate drafts locally and add semantic assertions one at a time**

Run `python3 scripts/build_source_manifests.py --write-drafts` and make the 44-count assertion GREEN. While entries are still legal local drafts, add one failing assertion and fix only draft-stage properties that can become GREEN now: schema validity with `allow_pending=true`; `mic_slug`; 55/55 normalized inventory coverage; unique IDs and local paths; all 57 `meta.yaml curves[].file` values covered by an original; every `original.curve_files` basename exists and belongs to that same microphone's `meta.yaml curves[].file` set; global and entry host subsets; and any already-declared redaction reference. Do not add no-pending or descendant assertions yet. Keep all files uncommitted.

- [ ] **Step 3: Bootstrap all original artifacts and resolve classification**

Run:

```bash
python3 scripts/build_source_manifests.py --check
python3 scripts/source_assets.py bootstrap --all --accept-new
```

Every successful artifact becomes `available`. Only a stable canonical URL with an explicit HTTP 404／410 becomes `unavailable`; 401／403／429／5xx／timeout／DNS／TLS／policy／signature failures remain `pending` and require investigation. Do not bypass authentication or host policy. Replace a failed endpoint only with a canonical official URL; when no stable endpoint exists, create a timestamped manual unavailable entry with a safe stable URL or `redaction_ref` and concrete reason. Repeat until this exact command succeeds:

```bash
! rg -n '"availability"\s*:\s*"pending"' data/*/source-manifest.json
```

Only now add the committed-data no-pending assertion and make it GREEN. Then cycle through available/unavailable field combinations, SVG direct-coverage rules, redaction mapping relationships and original hash/size/signature metadata; all must be GREEN before rendering begins.

- [ ] **Step 4: Render and visually review every available PDF**

Resolve AT2020's physical page from its downloaded official PDF before continuing. For every available PDF, invoke `render-page` at the inventory's verified 1-based physical page and exactly 300 DPI. Four available chart-only Neumann SVG artifacts skip derivation. Inspect each generated full page visually against `meta.yaml source.chart`; legacy 400／600-DPI coordinates are hints only and must not be copied.

Before rendering the first page, add assertions requiring one valid rendered-page for that selected PDF—PDF-original parent type, physical page, 300 DPI, PNG media/path fields and `derived_from_sha256`—and observe RED because it is absent. Render the first page to make them GREEN, then keep the generalized assertions GREEN for each later page. Do not add chart-crop completeness yet.

To keep review bounded without invalid commits, process local files in batches of at most ten PDFs and record each reviewed page/crop in a temporary checklist under `.superpowers/sdd/`; do not commit a batch until the entire repository contract is valid.

- [ ] **Step 5: Crop every reviewed PDF chart and close all semantic assertions**

For each reviewed page, choose integer `[left, top, right, bottom]` coordinates with a left-top origin and exclusive right/bottom. The crop must retain full axes, labels, legend and every official curve while excluding unrelated page content. Run `crop-chart` with every applicable CSV as a repeatable `--curve` argument.

Before creating the first crop, add assertions requiring one valid crop for that selected rendered page—rendered-page parent type, bounded/exclusive crop box, crop curve subset and current parent hash—and observe RED because it is absent. Create the first crop to make them GREEN, then keep the generalized assertions GREEN per crop. Add the global available-PDF descendant coverage assertion while later crops are still absent, observe its failing count, and complete remaining crops until GREEN.

After each local batch, run `python3 scripts/source_assets.py verify --all`. At the end run:

```bash
python3 scripts/source_assets.py verify --all
cd digitizer && node --test test/source-manifest.test.js
```

Expected: `failed=0`; available count equals `3 * P + S`; unavailable count equals `47 - P - S`; all 57 CSV files have original coverage and every available PDF curve has descendant crop coverage.

- [ ] **Step 6: Commit the first and only tracked migration state**

Stage only the repository contract, 44 manifests and the explicitly named metadata files—never glob all metadata and never stage ignored blobs:

```bash
git add digitizer/test/source-manifest.test.js config/source-inventory-v1.json \
  config/source-url-redactions.json data/*/source-manifest.json \
  data/audio-technica-at2020/meta.yaml data/shure-sm35/meta.yaml \
  data/neumann-km184/meta.yaml data/neumann-tlm102/meta.yaml \
  data/neumann-tlm103/meta.yaml data/neumann-u87-ai/meta.yaml
test -z "$(git diff --cached --name-only | rg '/source/')"
! git diff --cached --name-only | rg -v '^(digitizer/test/source-manifest\.test\.js|config/source-(inventory-v1|url-redactions)\.json|data/[^/]+/(source-manifest\.json|meta\.yaml))$'
git commit -m "data: preserve official source artifact manifests (#19)"
```

This intentionally keeps a large generated-data commit: splitting it would violate the approved contract by exposing a commit with available PDF originals but missing reviewed descendants.

### Task 8: Documentation、Git Ignore、CI 與 Final Verification

**Files:**
- Create: `.github/workflows/ci.yml`
- Modify: `.gitignore`
- Modify: `README.md`
- Modify: `data/README.md`
- Modify: `skills/digitize-frequency-response/SKILL.md`
- Modify: `skills/digitize-polar-pattern/SKILL.md`

**Interfaces:**
- Produces: offline CI that installs Poppler／Pillow／Node dependencies, runs all tests, and rejects tracked vendor blobs.
- Produces: operator documentation for bootstrap, restore, derive, verify, and stale repair.

- [ ] **Step 1: Write a workflow-existence assertion and observe RED before creating it**

Extend `digitizer/test/source-manifest.test.js` with `assert.equal(existsSync(workflowPath), true)` before creating `.github/workflows/ci.yml`.

Run: `cd digitizer && node --test test/source-manifest.test.js`

Expected: assertion FAIL (`false !== true`); no file-read exception occurs.

- [ ] **Step 2: Create a valid no-op workflow scaffold, then write the first CI behavior assertion**

Create `.github/workflows/ci.yml` as a syntactically valid workflow with only its name and a `workflow_dispatch` job that runs `true`; confirm the existence assertion GREEN. Add an assertion for `actions/checkout@v7`; because the scaffold lacks it, the assertion must fail while file reads succeed.

- [ ] **Step 3: Run and verify the first CI behavior RED**

Run: `cd digitizer && node --test test/source-manifest.test.js`

Expected: assertion FAIL because the valid workflow scaffold lacks `actions/checkout@v7`; all files load successfully.

- [ ] **Step 4: Implement CI and documentation through isolated RED→GREEN cycles**

Use one RED→GREEN micro-cycle apiece for setup-node/setup-python exact majors, Node/Python versions, dependency installation, test commands, offline exclusions, ignored `source/`, README recovery sequence, `local-only` wording and no tracked blob extensions.

CI uses `actions/checkout@v7`, `actions/setup-node@v7`, `actions/setup-python@v6`, Node 22, Python 3.12, `npm ci` in `digitizer/`, `poppler-utils`, `pip install -r requirements-source-assets.txt`, `node --test`, all Python tests, and this tracked-blob guard:

```bash
tracked="$(git ls-files -- 'data/**/source/**' 'data/**/*.pdf' 'data/**/*.svg' 'data/**/*.png' 'data/**/*.jpg' 'data/**/*.jpeg' 'data/**/*.gif' 'data/**/*.webp' 'data/**/*.bmp' 'data/**/*.tif' 'data/**/*.tiff' 'data/**/*.avif')"
test -z "$tracked"
```

CI is offline with respect to manufacturer hosts: it runs the tracked Node contract and Python tests against injected/local fixtures, but does not run `fetch`, `bootstrap`, or repository `verify --all`, because ignored blobs do not exist in a fresh CI clone. Add a contract assertion that the workflow contains none of those network／local-blob commands.

Document the fresh-clone sequence exactly:

```bash
python3 -m pip install -r requirements-source-assets.txt
python3 scripts/source_assets.py fetch --all
python3 scripts/source_assets.py derive --all
python3 scripts/source_assets.py verify --all
```

- [ ] **Step 5: Run complete local verification**

```bash
python3 -m pip install -r requirements-source-assets.txt
(cd digitizer && node --test)
python3 scripts/test_source_assets.py
python3 scripts/test_overlay_verify.py
python3 scripts/test_trace_cli.py
python3 scripts/source_assets.py verify --all
! rg -n '"availability"\s*:\s*"pending"' data/*/source-manifest.json
test -z "$(git ls-files -- 'data/**/source/**' 'data/**/*.pdf' 'data/**/*.svg' 'data/**/*.png' 'data/**/*.jpg' 'data/**/*.jpeg' 'data/**/*.gif' 'data/**/*.webp' 'data/**/*.bmp' 'data/**/*.tif' 'data/**/*.tiff' 'data/**/*.avif')"
git diff --check
```

Expected: all tests PASS; source verify returns `0` with `failed=0`; no pending manifests; no tracked manufacturer blob; clean diff check.

- [ ] **Step 6: Commit and update the issue／PR evidence**

```bash
git add .github/workflows/ci.yml .gitignore README.md data/README.md skills/digitize-frequency-response/SKILL.md skills/digitize-polar-pattern/SKILL.md digitizer/test/source-manifest.test.js
git commit -m "docs: document local source artifact lifecycle (#19)"
git push origin idd/19-official-data-boundary
```

Post the exact test commands, counts, unavailable reasons, local artifact totals, final commit SHA, and confirmation that Git tracks zero vendor blobs to issue #19／PR #20.
