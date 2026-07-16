from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import re
from urllib.parse import urlsplit


class SourceAssetError(Exception):
    exit_code = 1


class ContractError(SourceAssetError):
    exit_code = 1


class IntegrityError(SourceAssetError):
    exit_code = 2


class RemoteError(SourceAssetError):
    exit_code = 2


@dataclass(frozen=True)
class Policy:
    hosts: frozenset[str]
    redactions: dict[str, dict]


_UTC_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z"
)
_HTTPS_URL = re.compile(
    r"https://[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*"
    r"(?::[0-9]{1,5})?(?:/[^\x00-\x20\x7f?#]*)?(?:\?[^\x00-\x20\x7f#]*)?"
)
_SHA256 = re.compile(r"[0-9a-f]{64}")
_CURVE_FILE = re.compile(r"[^/]+\.csv")


def _validate_timestamp(value: object, field: str) -> None:
    if not isinstance(value, str) or _UTC_TIMESTAMP.fullmatch(value) is None:
        raise ContractError(f"{field} 必須是 RFC 3339 UTC timestamp")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise ContractError(f"{field} 不是有效的 UTC timestamp：{value}") from None


def _load_meta_curve_files(mic_dir: Path) -> set[str]:
    meta_path = mic_dir / "meta.yaml"
    try:
        lines = meta_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise ContractError(f"無法讀取 meta.yaml：{meta_path}") from error

    curves: set[str] = set()
    in_curves = False
    for line in lines:
        if re.fullmatch(r"curves:\s*(?:#.*)?", line):
            in_curves = True
            continue
        if in_curves and re.match(r"[A-Za-z_]", line):
            break
        if not in_curves:
            continue
        entry = re.match(r" {2}-\s*(.*)", line)
        if entry is None:
            continue
        file_match = re.search(r"(?:^|[{,]\s*)file:\s*[\"']?([^,}\"'\s]+)", entry.group(1))
        if file_match is not None:
            curves.add(file_match.group(1))

    if not curves:
        raise ContractError(f"meta.yaml 的 curves[] 不得為空：{meta_path}")
    return curves


def _object_without_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ContractError(f"JSON object 欄位不得重複：{key}")
        result[key] = value
    return result


def _require_object_fields(
    value: object,
    *,
    required: set[str],
    allowed: set[str],
    label: str,
) -> dict:
    if not isinstance(value, dict):
        raise ContractError(f"{label} 必須是 JSON object")
    missing = required - value.keys()
    if missing:
        raise ContractError(f"{label} 缺少必要欄位：{', '.join(sorted(missing))}")
    unknown = value.keys() - allowed
    if unknown:
        raise ContractError(f"{label} 含未知欄位：{', '.join(sorted(unknown))}")
    return value


def _validate_https_url(value: object, field: str) -> None:
    if not isinstance(value, str) or _HTTPS_URL.fullmatch(value) is None:
        raise ContractError(f"{field} 必須是合法的 HTTPS URL")


def _validate_references(value: object) -> None:
    if not isinstance(value, list):
        raise ContractError("references 必須是 JSON array")
    roles = {"product-page", "landing-page", "documentation-page"}
    for index, raw_reference in enumerate(value):
        reference = _require_object_fields(
            raw_reference,
            required={"role", "url"},
            allowed={"role", "url"},
            label=f"references[{index}]",
        )
        if not isinstance(reference["role"], str) or reference["role"] not in roles:
            raise ContractError(f"references[{index}].role 無效：{reference['role']}")
        _validate_https_url(reference["url"], f"references[{index}].url")


_ARTIFACT_FIELDS = {
    ("original", "pending"): {
        "id",
        "role",
        "availability",
        "source_url",
        "local_path",
        "media_type",
        "curve_files",
        "redistribution",
        "allowed_hosts",
    },
    ("original", "available"): {
        "id",
        "role",
        "availability",
        "source_url",
        "local_path",
        "media_type",
        "curve_files",
        "size_bytes",
        "sha256",
        "retrieved_at",
        "redistribution",
        "allowed_hosts",
    },
    ("rendered-page", "available"): {
        "id",
        "role",
        "availability",
        "local_path",
        "derived_from",
        "derived_from_sha256",
        "derivation",
        "media_type",
        "size_bytes",
        "sha256",
        "generated_at",
        "redistribution",
    },
    ("chart-crop", "available"): {
        "id",
        "role",
        "availability",
        "local_path",
        "derived_from",
        "derived_from_sha256",
        "curve_files",
        "derivation",
        "media_type",
        "size_bytes",
        "sha256",
        "generated_at",
        "redistribution",
    },
}
_UNAVAILABLE_BASE_FIELDS = {
    "id",
    "role",
    "availability",
    "curve_files",
    "checked_at",
    "reason",
    "redistribution",
}


def _validate_artifact_fields(raw_artifact: object, index: int) -> dict:
    label = f"artifacts[{index}]"
    if not isinstance(raw_artifact, dict):
        raise ContractError(f"{label} 必須是 JSON object")
    if "role" not in raw_artifact or "availability" not in raw_artifact:
        raise ContractError(f"{label} 缺少 role 或 availability")
    if not isinstance(raw_artifact["role"], str) or not isinstance(
        raw_artifact["availability"],
        str,
    ):
        raise ContractError(f"{label}.role 與 availability 必須是字串")
    variant = (raw_artifact["role"], raw_artifact["availability"])
    if variant == ("original", "unavailable"):
        has_url = "source_url" in raw_artifact or "allowed_hosts" in raw_artifact
        has_redaction = "redaction_ref" in raw_artifact
        if has_url == has_redaction:
            raise ContractError(f"{label} 必須恰有一種 unavailable 來源定位")
        locator = {"source_url", "allowed_hosts"} if has_url else {"redaction_ref"}
        fields = _UNAVAILABLE_BASE_FIELDS | locator
    else:
        fields = _ARTIFACT_FIELDS.get(variant)
        if fields is None:
            raise ContractError(f"{label} 的 role/availability 組合無效：{variant}")
    return _require_object_fields(
        raw_artifact,
        required=fields,
        allowed=fields,
        label=label,
    )


def _require_nonempty_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContractError(f"{field} 必須是非空字串")
    return value


def _validate_curve_files(value: object, field: str) -> None:
    if not isinstance(value, list) or not value:
        raise ContractError(f"{field} 必須是非空 array")
    if any(not isinstance(item, str) or _CURVE_FILE.fullmatch(item) is None for item in value):
        raise ContractError(f"{field} 只能包含 CSV basename")
    if len(value) != len(set(value)):
        raise ContractError(f"{field} 不得重複")


def _validate_allowed_hosts(value: object, field: str) -> None:
    if not isinstance(value, list) or not value:
        raise ContractError(f"{field} 必須是非空 array")
    if any(not isinstance(item, str) or not item for item in value):
        raise ContractError(f"{field} 只能包含非空字串")
    if len(value) != len(set(value)):
        raise ContractError(f"{field} 不得重複")


def _validate_media_pair(artifact: dict, index: int) -> None:
    local_path = artifact.get("local_path")
    media_type = artifact.get("media_type")
    role = artifact["role"]
    if role == "original":
        valid = (
            isinstance(local_path, str)
            and (
                (re.fullmatch(r"source/.+\.pdf", local_path) and media_type == "application/pdf")
                or (re.fullmatch(r"source/.+\.svg", local_path) and media_type == "image/svg+xml")
            )
        )
    else:
        valid = (
            isinstance(local_path, str)
            and re.fullmatch(r"source/.+\.png", local_path) is not None
            and media_type == "image/png"
        )
    if not valid:
        raise ContractError(f"artifacts[{index}] 的 local_path/media_type 組合無效")


def _validate_derivation(artifact: dict, index: int) -> None:
    label = f"artifacts[{index}].derivation"
    if artifact["role"] == "rendered-page":
        fields = {"page", "render_dpi", "render_tool", "render_tool_version"}
        derivation = _require_object_fields(
            artifact["derivation"],
            required=fields,
            allowed=fields,
            label=label,
        )
        if type(derivation["page"]) is not int or derivation["page"] < 1:
            raise ContractError(f"{label}.page 必須是正整數")
        dpi = derivation["render_dpi"]
        if isinstance(dpi, bool) or not isinstance(dpi, (int, float)) or dpi != 300:
            raise ContractError(f"{label}.render_dpi 必須是 300")
        _require_nonempty_string(derivation["render_tool"], f"{label}.render_tool")
        _require_nonempty_string(
            derivation["render_tool_version"],
            f"{label}.render_tool_version",
        )
    else:
        fields = {"crop_box_px", "crop_tool", "crop_tool_version"}
        derivation = _require_object_fields(
            artifact["derivation"],
            required=fields,
            allowed=fields,
            label=label,
        )
        crop_box = derivation["crop_box_px"]
        if (
            not isinstance(crop_box, list)
            or len(crop_box) != 4
            or any(type(item) is not int or item < 0 for item in crop_box)
        ):
            raise ContractError(f"{label}.crop_box_px 必須是四個非負整數")
        _require_nonempty_string(derivation["crop_tool"], f"{label}.crop_tool")
        _require_nonempty_string(
            derivation["crop_tool_version"],
            f"{label}.crop_tool_version",
        )


def _validate_artifact_values(artifact: dict, index: int) -> None:
    label = f"artifacts[{index}]"
    _require_nonempty_string(artifact["id"], f"{label}.id")
    if artifact["redistribution"] != "local-only":
        raise ContractError(f"{label}.redistribution 必須是 local-only")
    if "source_url" in artifact:
        _validate_https_url(artifact["source_url"], f"{label}.source_url")
    if "allowed_hosts" in artifact:
        _validate_allowed_hosts(artifact["allowed_hosts"], f"{label}.allowed_hosts")
    if "curve_files" in artifact:
        _validate_curve_files(artifact["curve_files"], f"{label}.curve_files")
    if "redaction_ref" in artifact:
        _require_nonempty_string(artifact["redaction_ref"], f"{label}.redaction_ref")
    if "reason" in artifact:
        _require_nonempty_string(artifact["reason"], f"{label}.reason")
    if "local_path" in artifact:
        _validate_media_pair(artifact, index)
    if "size_bytes" in artifact and (
        type(artifact["size_bytes"]) is not int or artifact["size_bytes"] < 1
    ):
        raise ContractError(f"{label}.size_bytes 必須是正整數")
    for field in ("sha256", "derived_from_sha256"):
        if field in artifact and (
            not isinstance(artifact[field], str) or _SHA256.fullmatch(artifact[field]) is None
        ):
            raise ContractError(f"{label}.{field} 必須是小寫 SHA-256")
    if "derived_from" in artifact:
        _require_nonempty_string(artifact["derived_from"], f"{label}.derived_from")
    if "derivation" in artifact:
        _validate_derivation(artifact, index)
    for field in ("retrieved_at", "generated_at", "checked_at"):
        if field in artifact:
            _validate_timestamp(artifact[field], f"{label}.{field}")


def _validate_manifest_shape(document: object) -> dict:
    fields = {"schema_version", "mic_slug", "references", "artifacts"}
    manifest = _require_object_fields(
        document,
        required=fields,
        allowed=fields,
        label="manifest",
    )
    version = manifest["schema_version"]
    if isinstance(version, bool) or not isinstance(version, (int, float)) or version != 1:
        raise ContractError("schema_version 必須是 1")
    mic_slug = manifest["mic_slug"]
    if not isinstance(mic_slug, str) or re.fullmatch(
        r"[a-z0-9]+(?:-[a-z0-9]+)*",
        mic_slug,
    ) is None:
        raise ContractError(f"mic_slug 格式無效：{mic_slug}")
    _validate_references(manifest["references"])
    if not isinstance(manifest["artifacts"], list):
        raise ContractError("artifacts 必須是 JSON array")
    for index, artifact in enumerate(manifest["artifacts"]):
        artifact = _validate_artifact_fields(artifact, index)
        _validate_artifact_values(artifact, index)
    return manifest


def load_manifest(path: Path, allow_pending: bool) -> dict:
    try:
        document = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_object_without_duplicate_keys,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ContractError(f"無法讀取 manifest JSON：{path}") from error
    if not isinstance(document, dict):
        raise ContractError(f"manifest 頂層必須是 JSON object：{path}")
    _validate_manifest_shape(document)
    _enforce_pending_gate(document, allow_pending)
    return document


def _enforce_pending_gate(document: dict, allow_pending: bool) -> None:
    if allow_pending:
        return
    artifacts = document.get("artifacts", [])
    if isinstance(artifacts, list) and any(
        isinstance(artifact, dict) and artifact.get("availability") == "pending"
        for artifact in artifacts
    ):
        raise ContractError("manifest 不得包含 pending artifact")


def validate_manifest(
    document: dict,
    mic_dir: Path,
    policy: Policy,
    allow_pending: bool,
) -> None:
    from .storage import resolve_local_path

    _validate_manifest_shape(document)
    _enforce_pending_gate(document, allow_pending)
    if document["mic_slug"] != mic_dir.name:
        raise ContractError(
            f"mic_slug 必須與資料夾名稱相同：{document['mic_slug']} != {mic_dir.name}"
        )
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    for artifact in document.get("artifacts", []):
        artifact_id = artifact["id"]
        if artifact_id in seen_ids:
            raise ContractError(f"artifact id 不得重複：{artifact_id}")
        seen_ids.add(artifact_id)
        local_path = artifact.get("local_path")
        if local_path is None:
            continue
        resolve_local_path(mic_dir, local_path)
        if local_path in seen_paths:
            raise ContractError(f"local_path 不得重複：{local_path}")
        seen_paths.add(local_path)

    for reference in document["references"]:
        hostname = urlsplit(reference["url"]).hostname
        if hostname not in policy.hosts:
            raise ContractError(f"reference URL host 不在全域 policy：{hostname}")

    for artifact in document["artifacts"]:
        if "allowed_hosts" not in artifact:
            continue
        unknown_hosts = set(artifact["allowed_hosts"]) - policy.hosts
        if unknown_hosts:
            raise ContractError(
                f"allowed_hosts 不在全域 policy：{', '.join(sorted(unknown_hosts))}"
            )
        hostname = urlsplit(artifact["source_url"]).hostname
        if hostname not in artifact["allowed_hosts"]:
            raise ContractError(
                f"source_url hostname 必須列於 allowed_hosts：{hostname}"
            )

    artifacts_by_id = {
        artifact.get("id"): artifact
        for artifact in document.get("artifacts", [])
        if isinstance(artifact.get("id"), str)
    }
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(artifact_id: str) -> None:
        if artifact_id in visiting:
            raise ContractError(f"derived graph 不得形成循環：{artifact_id}")
        if artifact_id in visited:
            return
        visiting.add(artifact_id)
        parent_id = artifacts_by_id[artifact_id].get("derived_from")
        if isinstance(parent_id, str) and parent_id in artifacts_by_id:
            visit(parent_id)
        visiting.remove(artifact_id)
        visited.add(artifact_id)

    for artifact_id in artifacts_by_id:
        visit(artifact_id)

    children: dict[str, set[str]] = {artifact_id: set() for artifact_id in artifacts_by_id}
    stale_artifacts: set[str] = set()
    for artifact in document.get("artifacts", []):
        parent_id = artifact.get("derived_from")
        if parent_id is None:
            continue
        if parent_id not in artifacts_by_id:
            raise ContractError(f"derived_from 不存在：{parent_id}")
        parent = artifacts_by_id[parent_id]
        if parent.get("availability") != "available":
            raise ContractError(f"derived_from 必須指向 available artifact：{parent_id}")
        if artifact["role"] == "rendered-page" and not (
            parent["role"] == "original" and parent.get("media_type") == "application/pdf"
        ):
            raise ContractError("rendered-page 的 parent 必須是 available PDF original")
        if artifact["role"] == "chart-crop" and parent["role"] != "rendered-page":
            raise ContractError("chart-crop 的 parent 必須是 rendered-page")
        artifact_id = artifact.get("id")
        if isinstance(artifact_id, str):
            children[parent_id].add(artifact_id)
        if artifact.get("derived_from_sha256") != parent.get("sha256"):
            if isinstance(artifact_id, str):
                stale_artifacts.add(artifact_id)

    pending_stale = list(stale_artifacts)
    while pending_stale:
        stale_parent = pending_stale.pop()
        for child_id in children[stale_parent]:
            if child_id not in stale_artifacts:
                stale_artifacts.add(child_id)
                pending_stale.append(child_id)
    if stale_artifacts:
        stale = ", ".join(sorted(stale_artifacts))
        raise IntegrityError(f"stale artifact 推導鏈：{stale}")

    meta_curves = _load_meta_curve_files(mic_dir)
    covered_by_originals: set[str] = set()
    for artifact in document.get("artifacts", []):
        artifact_curves = set(artifact.get("curve_files", []))
        unknown_curves = artifact_curves - meta_curves
        if unknown_curves:
            raise ContractError(
                f"curve_files 不在 meta.yaml curves[]：{', '.join(sorted(unknown_curves))}"
            )
        for curve_file in artifact_curves:
            if not (mic_dir / curve_file).is_file():
                raise ContractError(f"curve_files 指向不存在的 CSV：{curve_file}")
        if artifact.get("role") == "original":
            covered_by_originals.update(artifact_curves)
    missing_coverage = meta_curves - covered_by_originals
    if missing_coverage:
        missing = ", ".join(sorted(missing_coverage))
        raise ContractError(f"original.curve_files 未覆蓋 meta.yaml curves[]：{missing}")

    crop_coverage: dict[str, set[str]] = {}
    for artifact in document["artifacts"]:
        if artifact["role"] != "chart-crop":
            continue
        page = artifacts_by_id[artifact["derived_from"]]
        original = artifacts_by_id[page["derived_from"]]
        crop_curves = set(artifact["curve_files"])
        original_curves = set(original["curve_files"])
        if not crop_curves <= original_curves:
            raise ContractError(
                f"chart-crop.curve_files 必須是 ancestor original 的子集合：{artifact['id']}"
            )
        crop_coverage.setdefault(original["id"], set()).update(crop_curves)

    for artifact in document["artifacts"]:
        if not (
            artifact["role"] == "original"
            and artifact["availability"] == "available"
            and artifact["media_type"] == "application/pdf"
        ):
            continue
        missing_crops = set(artifact["curve_files"]) - crop_coverage.get(
            artifact["id"],
            set(),
        )
        if missing_crops:
            raise ContractError(
                "available PDF original 缺少 descendant chart-crop 覆蓋："
                + ", ".join(sorted(missing_crops))
            )

    for artifact in document.get("artifacts", []):
        redaction_ref = artifact.get("redaction_ref")
        if redaction_ref is None:
            continue
        if redaction_ref not in policy.redactions:
            raise ContractError(f"redaction_ref 不存在：{redaction_ref}")
        record = policy.redactions[redaction_ref]
        if not isinstance(record, dict) or record.get("id") != redaction_ref:
            raise ContractError(f"redaction_ref 與 record id 不一致：{redaction_ref}")
        if record.get("disposition") != "no-stable-endpoint":
            raise ContractError(
                f"redaction_ref 必須指向 no-stable-endpoint：{redaction_ref}"
            )
        if record.get("mic_slug") != document["mic_slug"]:
            raise ContractError(f"redaction_ref 的 mic_slug 不符：{redaction_ref}")

    manifest_urls = {reference["url"] for reference in document["references"]}
    manifest_urls.update(
        artifact["source_url"]
        for artifact in document["artifacts"]
        if "source_url" in artifact
    )
    referenced_redactions = {
        artifact["redaction_ref"]
        for artifact in document["artifacts"]
        if "redaction_ref" in artifact
    }
    for record_id, record in policy.redactions.items():
        if not isinstance(record, dict) or record.get("mic_slug") != document["mic_slug"]:
            continue
        if record.get("id") != record_id:
            raise ContractError(f"redaction registry key 與 id 不一致：{record_id}")
        disposition = record.get("disposition")
        if disposition == "no-stable-endpoint":
            if record_id not in referenced_redactions:
                raise ContractError(f"no-stable-endpoint redaction 未被引用：{record_id}")
        elif disposition == "replaced":
            if record.get("canonical_url") not in manifest_urls:
                raise ContractError(f"replaced canonical_url 未映射至 manifest：{record_id}")
        else:
            raise ContractError(f"redaction disposition 無效：{record_id}")
    return None
