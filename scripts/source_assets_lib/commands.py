import copy
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
from typing import Callable
from urllib.parse import urlsplit

from .model import (
    ContractError,
    IntegrityError,
    Policy,
    SourceAssetError,
    load_manifest,
    validate_manifest,
)
from .network import ABSOLUTE_MAX_SIZE, DownloadRequest, RemoteHTTPError, Transport
from .storage import (
    ArtifactStore,
    ensure_local_directory,
    inspect_local_file,
    link_local_file_if_absent,
    local_file_exists,
    remove_local_file,
    resolve_local_path,
)


@dataclass(frozen=True)
class OperationSummary:
    available: int
    unavailable: int
    failed: int
    exit_code: int


@dataclass(frozen=True)
class Selector:
    mic_slug: str | None

    def __post_init__(self) -> None:
        if self.mic_slug is not None and re.fullmatch(
            r"[a-z0-9]+(?:-[a-z0-9]+)*",
            self.mic_slug,
        ) is None:
            raise ContractError(f"麥克風 slug 格式無效：{self.mic_slug}")

    @classmethod
    def all(cls) -> "Selector":
        return cls(None)

    @classmethod
    def mic(cls, slug: str) -> "Selector":
        return cls(slug)


@dataclass(frozen=True)
class CommandContext:
    transport: Transport
    store: ArtifactStore
    now: Callable[[], datetime]


class _BootstrapRemoteHTTPFailure(Exception):
    def __init__(self, error: RemoteHTTPError, had_orphan: bool):
        self.error = error
        self.had_orphan = had_orphan
        super().__init__(str(error))


def bootstrap(
    root: Path,
    selector: Selector,
    accept_new: bool,
    context: CommandContext,
) -> OperationSummary:
    if not accept_new:
        return OperationSummary(available=0, unavailable=0, failed=1, exit_code=1)
    try:
        operation_timestamp = _utc_timestamp(context.now())
    except ContractError as error:
        return OperationSummary(available=0, unavailable=0, failed=1, exit_code=error.exit_code)
    available = 0
    unavailable = 0
    failed = 0
    exit_code = 0
    try:
        policy = _load_policy(root)
    except SourceAssetError as error:
        return OperationSummary(available=0, unavailable=0, failed=1, exit_code=error.exit_code)
    try:
        manifest_paths = _manifest_paths(root, selector)
    except (SourceAssetError, OSError) as error:
        classified = _classify_local_error(error)
        return OperationSummary(0, 0, 1, classified.exit_code)
    if not manifest_paths:
        return OperationSummary(0, 0, 1, 1)
    for manifest_path in manifest_paths:
        try:
            mic_dir = _validated_mic_directory(root, manifest_path)
            document = load_manifest(manifest_path, allow_pending=True)
            _validate_bootstrap_manifest(document, mic_dir, policy)
            _ensure_source_directory(mic_dir, root)
        except (SourceAssetError, OSError) as error:
            error = _classify_local_error(error)
            failed += 1
            exit_code = max(exit_code, error.exit_code)
            continue
        for artifact_snapshot in tuple(document["artifacts"]):
            artifact = next(
                item
                for item in document["artifacts"]
                if item["id"] == artifact_snapshot["id"]
            )
            if artifact["availability"] == "available":
                available += 1
                continue
            if artifact["availability"] == "unavailable":
                unavailable += 1
                continue
            if not (
                artifact["role"] == "original"
                and artifact["availability"] == "pending"
            ):
                continue
            try:
                target = resolve_local_path(mic_dir, artifact["local_path"])
                request = DownloadRequest(
                    url=artifact["source_url"],
                    allowed_hosts=frozenset(artifact["allowed_hosts"]),
                    expected_media_type=artifact["media_type"],
                    expected_size=None,
                    max_size=ABSOLUTE_MAX_SIZE,
                    expected_sha256=None,
                )
                _result, file_digest = _download_bootstrap_blob(
                    context,
                    root,
                    target,
                    artifact["media_type"],
                    request,
                )
            except _BootstrapRemoteHTTPFailure as failure:
                error = failure.error
                if not failure.had_orphan and error.status in {404, 410}:
                    updated_document = copy.deepcopy(document)
                    updated_artifact = next(
                        item
                        for item in updated_document["artifacts"]
                        if item["id"] == artifact["id"]
                    )
                    updated_artifact["availability"] = "unavailable"
                    updated_artifact["checked_at"] = operation_timestamp
                    updated_artifact["reason"] = f"遠端 HTTP {error.status}"
                    updated_artifact.pop("local_path", None)
                    updated_artifact.pop("media_type", None)
                    try:
                        context.store.publish_manifest(manifest_path, updated_document)
                    except (SourceAssetError, OSError) as manifest_error:
                        manifest_error = _classify_local_error(manifest_error)
                        failed += 1
                        exit_code = max(exit_code, manifest_error.exit_code)
                        recovered_document = _recover_bootstrap_document(
                            manifest_path,
                            mic_dir,
                            policy,
                        )
                        if recovered_document is None:
                            break
                        document = recovered_document
                    else:
                        document = updated_document
                        unavailable += 1
                else:
                    failed += 1
                    exit_code = max(exit_code, error.exit_code)
                continue
            except (SourceAssetError, OSError) as error:
                error = _classify_local_error(error)
                failed += 1
                exit_code = max(exit_code, error.exit_code)
                continue
            updated_document = copy.deepcopy(document)
            updated_artifact = next(
                item
                for item in updated_document["artifacts"]
                if item["id"] == artifact["id"]
            )
            updated_artifact["availability"] = "available"
            updated_artifact["size_bytes"] = file_digest.size_bytes
            updated_artifact["sha256"] = file_digest.sha256
            updated_artifact["retrieved_at"] = operation_timestamp
            try:
                context.store.publish_manifest(manifest_path, updated_document)
            except (SourceAssetError, OSError) as error:
                error = _classify_local_error(error)
                failed += 1
                exit_code = max(exit_code, error.exit_code)
                recovered_document = _recover_bootstrap_document(
                    manifest_path,
                    mic_dir,
                    policy,
                )
                if recovered_document is None:
                    break
                document = recovered_document
                continue
            document = updated_document
            available += 1
    return OperationSummary(
        available=available,
        unavailable=unavailable,
        failed=failed,
        exit_code=exit_code,
    )


def fetch(
    root: Path,
    selector: Selector,
    context: CommandContext,
) -> OperationSummary:
    available = 0
    unavailable = 0
    failed = 0
    exit_code = 0
    try:
        policy = _load_policy(root)
    except SourceAssetError as error:
        return OperationSummary(0, 0, 1, error.exit_code)
    try:
        manifest_paths = _manifest_paths(root, selector)
    except (SourceAssetError, OSError) as error:
        error = _classify_local_error(error)
        return OperationSummary(0, 0, 1, error.exit_code)
    if not manifest_paths:
        return OperationSummary(0, 0, 1, 1)
    for manifest_path in manifest_paths:
        try:
            mic_dir = _validated_mic_directory(root, manifest_path)
            document = load_manifest(manifest_path, allow_pending=False)
            validate_manifest(document, mic_dir, policy, allow_pending=False)
            _ensure_source_directory(mic_dir, root)
        except (SourceAssetError, OSError) as error:
            error = _classify_local_error(error)
            failed += 1
            exit_code = max(exit_code, error.exit_code)
            continue
        for artifact in document["artifacts"]:
            if artifact["role"] != "original":
                continue
            if artifact["availability"] == "unavailable":
                unavailable += 1
                continue
            try:
                target = resolve_local_path(mic_dir, artifact["local_path"])
                if local_file_exists(target, root):
                    _verify_file(target, artifact, root)
                    available += 1
                    continue
                request = DownloadRequest(
                    url=artifact["source_url"],
                    allowed_hosts=frozenset(artifact["allowed_hosts"]),
                    expected_media_type=artifact["media_type"],
                    expected_size=artifact["size_bytes"],
                    max_size=artifact["size_bytes"],
                    expected_sha256=artifact["sha256"],
                )
                _result, file_digest = context.store.publish_blob(
                    target,
                    artifact["media_type"],
                    lambda handle, request=request: context.transport.download(request, handle),
                )
                if (
                    file_digest.size_bytes != artifact["size_bytes"]
                    or file_digest.sha256 != artifact["sha256"]
                ):
                    raise IntegrityError(f"下載內容與 manifest 不符：{target}")
            except (SourceAssetError, OSError) as error:
                error = _classify_local_error(error)
                failed += 1
                exit_code = max(exit_code, error.exit_code)
            else:
                available += 1
    return OperationSummary(available, unavailable, failed, exit_code)


def verify(root: Path, selector: Selector) -> OperationSummary:
    available = 0
    unavailable = 0
    failed = 0
    exit_code = 0
    try:
        policy = _load_policy(root)
    except SourceAssetError as error:
        return OperationSummary(0, 0, 1, error.exit_code)
    try:
        manifest_paths = _manifest_paths(root, selector)
    except (SourceAssetError, OSError) as error:
        error = _classify_local_error(error)
        return OperationSummary(0, 0, 1, error.exit_code)
    if not manifest_paths:
        return OperationSummary(0, 0, 1, 1)
    for manifest_path in manifest_paths:
        try:
            mic_dir = _validated_mic_directory(root, manifest_path)
            document = load_manifest(manifest_path, allow_pending=False)
            validate_manifest(document, mic_dir, policy, allow_pending=False)
        except (SourceAssetError, OSError) as error:
            error = _classify_local_error(error)
            failed += 1
            exit_code = max(exit_code, error.exit_code)
            continue
        for artifact in document["artifacts"]:
            if artifact["availability"] == "unavailable":
                unavailable += 1
                continue
            try:
                target = resolve_local_path(mic_dir, artifact["local_path"])
                if not local_file_exists(target, root):
                    raise IntegrityError(f"available artifact 缺少本機檔案：{target}")
                _verify_file(target, artifact, root)
            except (SourceAssetError, OSError) as error:
                error = _classify_local_error(error)
                failed += 1
                exit_code = max(exit_code, error.exit_code)
            else:
                available += 1
    return OperationSummary(available, unavailable, failed, exit_code)


def _manifest_paths(root: Path, selector: Selector) -> list[Path]:
    data = root / "data"
    try:
        if selector.mic_slug is not None:
            return [data / selector.mic_slug / "source-manifest.json"]
        candidates = []
        with os.scandir(data) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False) or entry.is_symlink():
                    candidates.append(data / entry.name / "source-manifest.json")
        return sorted(candidates)
    except OSError as error:
        raise ContractError(f"無法列舉來源 manifest：{data}") from error


def _validated_mic_directory(root: Path, manifest_path: Path) -> Path:
    data_dir = root / "data"
    mic_dir = manifest_path.parent
    try:
        if data_dir.is_symlink() or mic_dir.is_symlink() or manifest_path.is_symlink():
            raise ContractError(f"來源 manifest 路徑不得經過 symlink：{manifest_path}")
        if mic_dir.parent.resolve(strict=True) != data_dir.resolve(strict=True):
            raise ContractError(f"來源 manifest 不在 data 直屬資料夾：{manifest_path}")
    except OSError as error:
        raise ContractError(f"無法解析來源 manifest 路徑：{manifest_path}") from error
    return mic_dir


def _load_policy(root: Path) -> Policy:
    hosts_document = _load_config_object(root / "config" / "source-hosts.json")
    if set(hosts_document) != {"schema_version", "hosts"}:
        raise ContractError("source-hosts.json 欄位無效")
    if type(hosts_document["schema_version"]) is not int or hosts_document["schema_version"] != 1:
        raise ContractError("source-hosts.json schema_version 必須是 1")
    hosts = hosts_document["hosts"]
    if (
        not isinstance(hosts, list)
        or not hosts
        or any(
            not isinstance(host, str)
            or re.fullmatch(
                r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*",
                host,
            )
            is None
            for host in hosts
        )
        or len(hosts) != len(set(hosts))
    ):
        raise ContractError("source-hosts.json hosts 無效")

    redactions_document = _load_config_object(
        root / "config" / "source-url-redactions.json"
    )
    if set(redactions_document) != {"schema_version", "entries"}:
        raise ContractError("source-url-redactions.json 欄位無效")
    if (
        type(redactions_document["schema_version"]) is not int
        or redactions_document["schema_version"] != 1
    ):
        raise ContractError("source-url-redactions.json schema_version 必須是 1")
    entries = redactions_document["entries"]
    if not isinstance(entries, list):
        raise ContractError("source-url-redactions.json entries 必須是 array")
    redactions = {}
    for entry in entries:
        _validate_redaction_entry(entry)
        if entry["id"] in redactions:
            raise ContractError(f"redaction id 不得重複：{entry['id']}")
        redactions[entry["id"]] = entry
    return Policy(frozenset(hosts), redactions)


def _load_config_object(path: Path) -> dict:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ContractError(f"無法讀取設定 JSON：{path}") from error
    if not isinstance(document, dict):
        raise ContractError(f"設定 JSON 頂層必須是 object：{path}")
    return document


def _validate_redaction_entry(entry: object) -> None:
    if not isinstance(entry, dict):
        raise ContractError("redaction entry 必須是 object")
    common = {
        "id",
        "mic_slug",
        "source_field",
        "legacy_url_sha256",
        "checked_at",
        "disposition",
    }
    disposition = entry.get("disposition")
    required = common | ({"canonical_url"} if disposition == "replaced" else set())
    if disposition not in {"replaced", "no-stable-endpoint"} or set(entry) != required:
        raise ContractError("redaction entry 欄位或 disposition 無效")
    for field in ("id", "source_field"):
        if not isinstance(entry[field], str) or not entry[field]:
            raise ContractError(f"redaction {field} 必須是非空字串")
    if not isinstance(entry["mic_slug"], str) or re.fullmatch(
        r"[a-z0-9]+(?:-[a-z0-9]+)*",
        entry["mic_slug"],
    ) is None:
        raise ContractError("redaction mic_slug 無效")
    if not isinstance(entry["legacy_url_sha256"], str) or re.fullmatch(
        r"[0-9a-f]{64}",
        entry["legacy_url_sha256"],
    ) is None:
        raise ContractError("redaction legacy_url_sha256 無效")
    if not isinstance(entry["checked_at"], str):
        raise ContractError("redaction checked_at 無效")
    try:
        datetime.strptime(entry["checked_at"], "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise ContractError("redaction checked_at 無效") from None
    if disposition == "replaced":
        url = entry["canonical_url"]
        if (
            not isinstance(url, str)
            or urlsplit(url).scheme != "https"
            or not urlsplit(url).hostname
            or urlsplit(url).query
            or urlsplit(url).fragment
        ):
            raise ContractError("redaction canonical_url 無效")


def _ensure_source_directory(mic_dir: Path, root: Path) -> Path:
    source_dir = mic_dir / "source"
    ensure_local_directory(source_dir, root)
    return source_dir


def _download_bootstrap_blob(
    context: CommandContext,
    root: Path,
    target: Path,
    media_type: str,
    request: DownloadRequest,
):
    producer = lambda handle: context.transport.download(request, handle)
    candidate = target.parent / (
        f".{target.name}.orphan-{secrets.token_hex(16)}.candidate"
    )
    primary_error = None
    try:
        result, candidate_digest = context.store.publish_blob(
            candidate,
            media_type,
            producer,
        )
        if link_local_file_if_absent(candidate, target, root):
            installed_digest = inspect_local_file(target, media_type, root)
            if installed_digest != candidate_digest:
                raise IntegrityError(f"發布後 artifact 內容不符：{target}")
            return result, installed_digest
        orphan_digest = inspect_local_file(target, media_type, root)
        if orphan_digest != candidate_digest:
            raise IntegrityError(f"既有 orphan 與重新下載內容不符：{target}")
        return result, orphan_digest
    except RemoteHTTPError as error:
        try:
            had_orphan = local_file_exists(target, root)
        except BaseException as state_error:
            primary_error = state_error
            raise
        wrapped = _BootstrapRemoteHTTPFailure(error, had_orphan)
        primary_error = wrapped
        raise wrapped from error
    except BaseException as error:
        primary_error = error
        raise
    finally:
        try:
            remove_local_file(candidate, root)
        except SourceAssetError as cleanup_error:
            if primary_error is None:
                raise
            if hasattr(primary_error, "add_note"):
                primary_error.add_note(f"orphan 比對檔清理失敗：{cleanup_error}")


def _recover_bootstrap_document(
    manifest_path: Path,
    mic_dir: Path,
    policy: Policy,
) -> dict | None:
    try:
        recovered = load_manifest(manifest_path, allow_pending=True)
        _validate_bootstrap_manifest(recovered, mic_dir, policy)
    except (SourceAssetError, OSError):
        return None
    return recovered


def _validate_bootstrap_manifest(document: dict, mic_dir: Path, policy: Policy) -> None:
    staged = copy.deepcopy(document)
    used_ids = {artifact["id"] for artifact in staged["artifacts"]}
    used_paths = {
        artifact["local_path"]
        for artifact in staged["artifacts"]
        if "local_path" in artifact
    }
    for index, original in enumerate(tuple(staged["artifacts"])):
        if not (
            original["role"] == "original"
            and original["availability"] == "available"
            and original.get("media_type") == "application/pdf"
        ):
            continue
        suffix = index
        while (
            f"bootstrap-validation-page-{suffix}" in used_ids
            or f"bootstrap-validation-crop-{suffix}" in used_ids
            or f"source/.bootstrap-validation-page-{suffix}.png" in used_paths
            or f"source/.bootstrap-validation-crop-{suffix}.png" in used_paths
        ):
            suffix += 1
        page_id = f"bootstrap-validation-page-{suffix}"
        crop_id = f"bootstrap-validation-crop-{suffix}"
        page_path = f"source/.bootstrap-validation-page-{suffix}.png"
        crop_path = f"source/.bootstrap-validation-crop-{suffix}.png"
        timestamp = original["retrieved_at"]
        page_sha = original["sha256"]
        staged["artifacts"].extend(
            [
                {
                    "id": page_id,
                    "role": "rendered-page",
                    "availability": "available",
                    "local_path": page_path,
                    "derived_from": original["id"],
                    "derived_from_sha256": original["sha256"],
                    "derivation": {
                        "page": 1,
                        "render_dpi": 300,
                        "render_tool": "bootstrap-validation",
                        "render_tool_version": "1",
                    },
                    "media_type": "image/png",
                    "size_bytes": 1,
                    "sha256": page_sha,
                    "generated_at": timestamp,
                    "redistribution": "local-only",
                },
                {
                    "id": crop_id,
                    "role": "chart-crop",
                    "availability": "available",
                    "local_path": crop_path,
                    "derived_from": page_id,
                    "derived_from_sha256": page_sha,
                    "curve_files": list(original["curve_files"]),
                    "derivation": {
                        "crop_box_px": [0, 0, 1, 1],
                        "crop_tool": "bootstrap-validation",
                        "crop_tool_version": "1",
                    },
                    "media_type": "image/png",
                    "size_bytes": 1,
                    "sha256": page_sha,
                    "generated_at": timestamp,
                    "redistribution": "local-only",
                },
            ]
        )
        used_ids.update({page_id, crop_id})
        used_paths.update({page_path, crop_path})
    validate_manifest(staged, mic_dir, policy, allow_pending=True)


def _verify_file(target: Path, artifact: dict, root: Path) -> None:
    file_digest = inspect_local_file(target, artifact["media_type"], root)
    if (
        file_digest.size_bytes != artifact["size_bytes"]
        or file_digest.sha256 != artifact["sha256"]
    ):
        raise IntegrityError(f"本機 artifact 與 manifest 不符：{target}")


def _classify_local_error(error: SourceAssetError | OSError) -> SourceAssetError:
    if isinstance(error, SourceAssetError):
        return error
    return ContractError("本機檔案系統操作失敗")


def _utc_timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ContractError("命令時鐘必須回傳 aware datetime")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
