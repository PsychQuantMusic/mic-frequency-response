from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import BinaryIO, Callable, Protocol
import xml.etree.ElementTree as ElementTree

from .model import ContractError, IntegrityError


BlobProducer = Callable[[BinaryIO], object]


@dataclass(frozen=True)
class FileDigest:
    size_bytes: int
    sha256: str


def resolve_local_path(mic_dir: Path, local_path: str) -> Path:
    components = local_path.split("/")
    if (
        len(components) != 2
        or components[0] != "source"
        or components[1] in {"", ".", ".."}
        or "\\" in local_path
    ):
        raise ContractError(f"local_path 必須是 source/{{filename}}：{local_path}")
    candidate = mic_dir / local_path
    if (mic_dir / "source").is_symlink() or candidate.is_symlink():
        raise ContractError(f"local_path 不得經過 symlink：{local_path}")
    try:
        candidate.resolve(strict=False).relative_to(mic_dir.resolve(strict=True))
    except (FileNotFoundError, ValueError):
        raise ContractError(f"local_path 逃出麥克風資料夾：{local_path}") from None
    return candidate


def digest_file(path: Path) -> FileDigest:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return FileDigest(size, digest.hexdigest())


def validate_signature(path: Path, media_type: str) -> None:
    supported = {"application/pdf", "image/png", "image/svg+xml"}
    if media_type not in supported:
        raise ContractError(f"不支援的 media_type：{media_type}")
    content = path.read_bytes()
    if media_type == "application/pdf" and not content.startswith(b"%PDF-"):
        raise IntegrityError(f"檔案不是有效的 PDF：{path}")
    if media_type == "image/png" and not content.startswith(b"\x89PNG\r\n\x1a\n"):
        raise IntegrityError(f"檔案不是有效的 PNG：{path}")
    declaration_scan = content.replace(b"\x00", b"").upper()
    if media_type == "image/svg+xml" and b"<!DOCTYPE" in declaration_scan:
        raise IntegrityError(f"SVG 不得包含 DOCTYPE：{path}")
    if media_type == "image/svg+xml" and b"<!ENTITY" in declaration_scan:
        raise IntegrityError(f"SVG 不得包含 ENTITY：{path}")
    if media_type == "image/svg+xml":
        try:
            root = ElementTree.fromstring(content)
        except ElementTree.ParseError as error:
            raise IntegrityError(f"SVG XML 無法解析：{path}") from error
        if root.tag.rsplit("}", 1)[-1] != "svg":
            raise IntegrityError(f"SVG 根元素必須是 svg：{path}")
    return None


class ArtifactStore(Protocol):
    def publish_blob(
        self,
        target: Path,
        media_type: str,
        producer: BlobProducer,
    ) -> tuple[object, FileDigest]: ...

    def publish_manifest(self, target: Path, document: dict) -> None: ...


class AtomicArtifactStore:
    def publish_blob(
        self,
        target: Path,
        media_type: str,
        producer: BlobProducer,
    ) -> tuple[object, FileDigest]:
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=target.parent,
        )
        temp_path = Path(temp_name)
        try:
            os.fchmod(fd, 0o600)
            handle = os.fdopen(fd, "w+b")
            fd = -1
            with handle:
                result = producer(handle)
                handle.flush()
                os.fsync(handle.fileno())

            validate_signature(temp_path, media_type)
            file_digest = digest_file(temp_path)
            try:
                os.replace(temp_path, target)
            except OSError as error:
                raise ContractError(f"artifact replace 失敗：{target}") from error
            try:
                _fsync_directory(target.parent)
            except OSError as error:
                raise ContractError(f"父資料夾 fsync 失敗：{target.parent}") from error
            return result, file_digest
        finally:
            if fd >= 0:
                os.close(fd)
            temp_path.unlink(missing_ok=True)

    def publish_manifest(self, target: Path, document: dict) -> None:
        payload = (
            json.dumps(
                document,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=target.parent,
        )
        temp_path = Path(temp_name)
        try:
            os.fchmod(fd, 0o600)
            handle = os.fdopen(fd, "w+b")
            fd = -1
            with handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())

            try:
                os.replace(temp_path, target)
            except OSError as error:
                raise ContractError(f"manifest replace 失敗：{target}") from error
            try:
                _fsync_directory(target.parent)
            except OSError as error:
                raise ContractError(f"父資料夾 fsync 失敗：{target.parent}") from error
        finally:
            if fd >= 0:
                os.close(fd)
            temp_path.unlink(missing_ok=True)


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
