from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import sys
from typing import BinaryIO, Callable, Protocol
import xml.etree.ElementTree as ElementTree

from .model import ContractError, IntegrityError, SourceAssetError


BlobProducer = Callable[[BinaryIO], object]


@dataclass(frozen=True)
class FileDigest:
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class DirectoryIdentity:
    device: int
    inode: int


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
    try:
        if (mic_dir / "source").is_symlink() or candidate.is_symlink():
            raise ContractError(f"local_path 不得經過 symlink：{local_path}")
        candidate.resolve(strict=False).relative_to(mic_dir.resolve(strict=True))
    except (OSError, ValueError):
        raise ContractError(f"local_path 逃出麥克風資料夾：{local_path}") from None
    return candidate


def digest_file(path: Path) -> FileDigest:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
    except OSError as error:
        raise ContractError(f"無法讀取檔案：{path}") from error
    return FileDigest(size, digest.hexdigest())


def validate_signature(path: Path, media_type: str) -> None:
    try:
        content = path.read_bytes()
    except OSError as error:
        raise ContractError(f"無法讀取檔案：{path}") from error
    _validate_signature_content(content, media_type, path)


def _validate_signature_content(content: bytes, media_type: str, path: Path) -> None:
    supported = {"application/pdf", "image/png", "image/svg+xml"}
    if media_type not in supported:
        raise ContractError(f"不支援的 media_type：{media_type}")
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
        except (ElementTree.ParseError, LookupError) as error:
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
        *,
        expected_parent_identity: DirectoryIdentity | None = None,
    ) -> tuple[object, FileDigest]: ...

    def publish_manifest(
        self,
        target: Path,
        document: dict,
        *,
        expected_parent_identity: DirectoryIdentity | None = None,
    ) -> None: ...


class AtomicArtifactStore:
    def __init__(self, trusted_root: Path | None = None):
        self._trusted_root = (
            None if trusted_root is None else _lexical_absolute(trusted_root)
        )

    def publish_blob(
        self,
        target: Path,
        media_type: str,
        producer: BlobProducer,
        *,
        expected_parent_identity: DirectoryIdentity | None = None,
    ) -> tuple[object, FileDigest]:
        target = _lexical_absolute(target)
        parent_fd = _open_parent_directory(target, self._trusted_root)
        temp_fd = -1
        temp_name: str | None = None
        try:
            _assert_open_directory_identity(
                parent_fd,
                expected_parent_identity,
                target.parent,
            )
            temp_fd, temp_name = _create_temp_file(parent_fd, target.name)
            try:
                handle = os.fdopen(temp_fd, "w+b")
            except OSError as error:
                raise ContractError(f"artifact temp 開啟失敗：{target}") from error
            temp_fd = -1
            try:
                with handle:
                    try:
                        result = producer(handle)
                    except SourceAssetError:
                        raise
                    except OSError as error:
                        raise ContractError(f"artifact 寫入失敗：{target}") from error
                    try:
                        handle.flush()
                    except OSError as error:
                        raise ContractError(f"artifact flush 失敗：{target}") from error
                    try:
                        os.fsync(handle.fileno())
                    except OSError as error:
                        raise ContractError(f"artifact 檔案 fsync 失敗：{target}") from error
                    try:
                        handle.seek(0)
                        content = handle.read()
                    except OSError as error:
                        raise ContractError(f"artifact temp 讀取失敗：{target}") from error

                    _validate_signature_content(content, media_type, target)
                    file_digest = FileDigest(
                        size_bytes=len(content),
                        sha256=hashlib.sha256(content).hexdigest(),
                    )
                    _assert_parent_still_attached(
                        target,
                        self._trusted_root,
                        parent_fd,
                    )
                    try:
                        os.replace(
                            temp_name,
                            target.name,
                            src_dir_fd=parent_fd,
                            dst_dir_fd=parent_fd,
                        )
                    except OSError as error:
                        raise ContractError(f"artifact replace 失敗：{target}") from error
                    temp_name = None
                    try:
                        _fsync_directory(parent_fd)
                    except OSError as error:
                        raise ContractError(
                            f"父資料夾 fsync 失敗：{target.parent}"
                        ) from error
                    _assert_parent_still_attached(
                        target,
                        self._trusted_root,
                        parent_fd,
                    )
            except SourceAssetError:
                raise
            except OSError as error:
                raise ContractError(f"artifact 檔案操作失敗：{target}") from error
            return result, file_digest
        finally:
            _cleanup_publication(
                parent_fd=parent_fd,
                temp_fd=temp_fd,
                temp_name=temp_name,
                target=target,
            )

    def publish_manifest(
        self,
        target: Path,
        document: dict,
        *,
        expected_parent_identity: DirectoryIdentity | None = None,
    ) -> None:
        target = _lexical_absolute(target)
        try:
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
        except (TypeError, ValueError, UnicodeError) as error:
            raise ContractError(f"manifest JSON 無法序列化：{target}") from error
        parent_fd = _open_parent_directory(target, self._trusted_root)
        temp_fd = -1
        temp_name: str | None = None
        try:
            _assert_open_directory_identity(
                parent_fd,
                expected_parent_identity,
                target.parent,
            )
            temp_fd, temp_name = _create_temp_file(parent_fd, target.name)
            try:
                handle = os.fdopen(temp_fd, "w+b")
            except OSError as error:
                raise ContractError(f"manifest temp 開啟失敗：{target}") from error
            temp_fd = -1
            try:
                with handle:
                    try:
                        written = handle.write(payload)
                    except OSError as error:
                        raise ContractError(f"manifest 寫入失敗：{target}") from error
                    if written != len(payload):
                        raise ContractError(f"manifest 寫入不完整：{target}")
                    try:
                        handle.flush()
                    except OSError as error:
                        raise ContractError(f"manifest flush 失敗：{target}") from error
                    try:
                        os.fsync(handle.fileno())
                    except OSError as error:
                        raise ContractError(f"manifest 檔案 fsync 失敗：{target}") from error
                    _assert_parent_still_attached(
                        target,
                        self._trusted_root,
                        parent_fd,
                    )
                    try:
                        os.replace(
                            temp_name,
                            target.name,
                            src_dir_fd=parent_fd,
                            dst_dir_fd=parent_fd,
                        )
                    except OSError as error:
                        raise ContractError(f"manifest replace 失敗：{target}") from error
                    temp_name = None
                    try:
                        _fsync_directory(parent_fd)
                    except OSError as error:
                        raise ContractError(
                            f"父資料夾 fsync 失敗：{target.parent}"
                        ) from error
                    _assert_parent_still_attached(
                        target,
                        self._trusted_root,
                        parent_fd,
                    )
            except SourceAssetError:
                raise
            except OSError as error:
                raise ContractError(f"manifest 檔案操作失敗：{target}") from error
        finally:
            _cleanup_publication(
                parent_fd=parent_fd,
                temp_fd=temp_fd,
                temp_name=temp_name,
                target=target,
            )


def ensure_local_directory(directory: Path, trusted_root: Path) -> None:
    directory = _lexical_absolute(directory)
    parent_fd = _open_parent_directory(directory, _lexical_absolute(trusted_root))
    child_fd = -1
    created = False
    primary_error = None
    try:
        try:
            os.mkdir(directory.name, 0o700, dir_fd=parent_fd)
            created = True
        except FileExistsError:
            pass
        except OSError as error:
            raise ContractError(f"無法建立資料夾：{directory}") from error
        try:
            child_fd = os.open(directory.name, _directory_flags(), dir_fd=parent_fd)
        except OSError as error:
            raise ContractError(f"路徑必須是安全資料夾：{directory}") from error
        if created:
            try:
                _fsync_directory(parent_fd)
            except OSError as error:
                raise ContractError(f"父資料夾 fsync 失敗：{directory.parent}") from error
    except BaseException as error:
        primary_error = error
        raise
    finally:
        _close_descriptors(
            (child_fd, parent_fd),
            f"關閉資料夾失敗：{directory}",
            primary_error,
        )


def capture_local_directory_identity(
    directory: Path,
    trusted_root: Path,
) -> DirectoryIdentity:
    directory = _lexical_absolute(directory)
    trusted_root = _lexical_absolute(trusted_root)
    parent_fd = _open_parent_directory(directory / ".identity", trusted_root)
    primary_error = None
    try:
        try:
            metadata = os.fstat(parent_fd)
        except OSError as error:
            raise ContractError(f"無法取得資料夾身分：{directory}") from error
        return DirectoryIdentity(metadata.st_dev, metadata.st_ino)
    except BaseException as error:
        primary_error = error
        raise
    finally:
        _close_descriptors(
            (parent_fd,),
            f"關閉資料夾失敗：{directory}",
            primary_error,
        )


def local_file_exists(
    path: Path,
    trusted_root: Path,
    *,
    expected_parent_identity: DirectoryIdentity | None = None,
) -> bool:
    path = _lexical_absolute(path)
    parent_fd = _open_parent_directory(path, _lexical_absolute(trusted_root))
    primary_error = None
    try:
        _assert_open_directory_identity(
            parent_fd,
            expected_parent_identity,
            path.parent,
        )
        try:
            metadata = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            exists = False
        except OSError as error:
            raise ContractError(f"無法檢查本機檔案：{path}") from error
        else:
            exists = True
        if exists and not stat.S_ISREG(metadata.st_mode):
            raise ContractError(f"本機 artifact 必須是 regular file：{path}")
        _assert_parent_still_attached(
            path,
            _lexical_absolute(trusted_root),
            parent_fd,
        )
        return exists
    except BaseException as error:
        primary_error = error
        raise
    finally:
        _close_descriptors((parent_fd,), f"關閉資料夾失敗：{path.parent}", primary_error)


def inspect_local_file(
    path: Path,
    media_type: str,
    trusted_root: Path,
    *,
    expected_parent_identity: DirectoryIdentity | None = None,
) -> FileDigest:
    path = _lexical_absolute(path)
    parent_fd = _open_parent_directory(path, _lexical_absolute(trusted_root))
    file_fd = -1
    primary_error = None
    try:
        _assert_open_directory_identity(
            parent_fd,
            expected_parent_identity,
            path.parent,
        )
        try:
            file_fd = os.open(path.name, _file_read_flags(), dir_fd=parent_fd)
            metadata = os.fstat(file_fd)
        except OSError as error:
            raise ContractError(f"無法安全開啟本機檔案：{path}") from error
        if not stat.S_ISREG(metadata.st_mode):
            raise ContractError(f"本機 artifact 必須是 regular file：{path}")
        try:
            with os.fdopen(file_fd, "rb") as handle:
                file_fd = -1
                content = handle.read()
        except OSError as error:
            raise ContractError(f"無法讀取檔案：{path}") from error
        _validate_signature_content(content, media_type, path)
        _assert_parent_still_attached(
            path,
            _lexical_absolute(trusted_root),
            parent_fd,
        )
        return FileDigest(len(content), hashlib.sha256(content).hexdigest())
    except BaseException as error:
        primary_error = error
        raise
    finally:
        _close_descriptors(
            (file_fd, parent_fd),
            f"關閉本機檔案失敗：{path}",
            primary_error,
        )


def link_local_file_if_absent(
    candidate: Path,
    target: Path,
    trusted_root: Path,
    *,
    expected_parent_identity: DirectoryIdentity | None = None,
) -> bool:
    candidate = _lexical_absolute(candidate)
    target = _lexical_absolute(target)
    if candidate.parent != target.parent or candidate.name in {"", ".", ".."}:
        raise ContractError("candidate 與 target 必須位於同一安全資料夾")
    parent_fd = _open_parent_directory(target, _lexical_absolute(trusted_root))
    primary_error = None
    try:
        _assert_open_directory_identity(
            parent_fd,
            expected_parent_identity,
            target.parent,
        )
        try:
            candidate_metadata = os.stat(
                candidate.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except OSError as error:
            raise ContractError(f"無法檢查候選 artifact：{candidate}") from error
        if not stat.S_ISREG(candidate_metadata.st_mode):
            raise ContractError(f"候選 artifact 必須是 regular file：{candidate}")
        _assert_parent_still_attached(target, _lexical_absolute(trusted_root), parent_fd)
        linked = True
        try:
            os.link(
                candidate.name,
                target.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            linked = False
        except OSError as error:
            raise ContractError(f"無法以 no-clobber 方式發布 artifact：{target}") from error
        try:
            _fsync_directory(parent_fd)
        except OSError as error:
            raise ContractError(f"父資料夾 fsync 失敗：{target.parent}") from error
        _assert_parent_still_attached(
            target,
            _lexical_absolute(trusted_root),
            parent_fd,
        )
        return linked
    except BaseException as error:
        primary_error = error
        raise
    finally:
        _close_descriptors(
            (parent_fd,),
            f"關閉資料夾失敗：{target.parent}",
            primary_error,
        )


def remove_local_file(
    path: Path,
    trusted_root: Path,
    *,
    expected_parent_identity: DirectoryIdentity | None = None,
) -> None:
    path = _lexical_absolute(path)
    parent_fd = _open_parent_directory(path, _lexical_absolute(trusted_root))
    primary_error = None
    try:
        _assert_open_directory_identity(
            parent_fd,
            expected_parent_identity,
            path.parent,
        )
        try:
            os.unlink(path.name, dir_fd=parent_fd)
        except FileNotFoundError:
            return
        except OSError as error:
            raise ContractError(f"無法移除本機檔案：{path}") from error
    except BaseException as error:
        primary_error = error
        raise
    finally:
        _close_descriptors((parent_fd,), f"關閉資料夾失敗：{path.parent}", primary_error)


def _lexical_absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def _file_read_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


def _open_parent_directory(target: Path, trusted_root: Path | None) -> int:
    directory = target.parent
    if target.name in {"", ".", ".."}:
        raise ContractError(f"發布目標名稱無效：{target}")
    if trusted_root is None:
        trusted_root = _infer_source_repository_root(target)
    if trusted_root is None:
        anchor = directory
        components: tuple[str, ...] = ()
    else:
        anchor = trusted_root
        try:
            components = directory.relative_to(trusted_root).parts
        except ValueError:
            raise ContractError(f"發布目標逃出 trusted root：{target}") from None
    opened: list[int] = []
    try:
        opened.append(os.open(anchor, _directory_flags()))
        for component in components:
            if component in {"", ".", ".."} or "/" in component or "\\" in component:
                raise ContractError(f"發布路徑 component 無效：{target}")
            opened.append(
                os.open(component, _directory_flags(), dir_fd=opened[-1])
            )
        result = opened[-1]
        try:
            _close_descriptors(
                tuple(reversed(opened[:-1])),
                f"關閉路徑資料夾失敗：{directory}",
                None,
            )
        except SourceAssetError as error:
            _close_descriptors(
                (result,),
                f"關閉路徑資料夾失敗：{directory}",
                error,
            )
            opened.clear()
            raise
        opened.clear()
        return result
    except SourceAssetError:
        _close_descriptors(tuple(reversed(opened)), "關閉路徑資料夾失敗", sys.exc_info()[1])
        raise
    except OSError as error:
        _close_descriptors(tuple(reversed(opened)), "關閉路徑資料夾失敗", error)
        raise ContractError(f"無法安全開啟父資料夾：{directory}") from error


def _infer_source_repository_root(target: Path) -> Path | None:
    parts = target.parent.parts
    data_indexes = [index for index, component in enumerate(parts) if component == "data"]
    if not data_indexes:
        return None
    data_index = data_indexes[-1]
    if data_index == 0:
        return Path(".")
    return Path(*parts[:data_index])


def _create_temp_file(parent_fd: int, target_name: str) -> tuple[int, str]:
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    for _attempt in range(100):
        try:
            name = f".{target_name}.{secrets.token_hex(16)}.tmp"
        except OSError as error:
            raise ContractError(f"無法產生暫存檔名：{target_name}") from error
        try:
            return os.open(name, flags, 0o600, dir_fd=parent_fd), name
        except FileExistsError:
            continue
        except OSError as error:
            raise ContractError(f"無法建立同資料夾暫存檔：{target_name}") from error
    raise ContractError(f"無法取得唯一暫存檔名：{target_name}")


def _assert_parent_still_attached(
    target: Path,
    trusted_root: Path | None,
    original_fd: int,
) -> None:
    replacement_fd = _open_parent_directory(target, trusted_root)
    primary_error = None
    try:
        try:
            original = os.fstat(original_fd)
            replacement = os.fstat(replacement_fd)
        except OSError as error:
            raise ContractError(f"無法確認發布資料夾身分：{target.parent}") from error
        if (original.st_dev, original.st_ino) != (replacement.st_dev, replacement.st_ino):
            raise ContractError(f"發布資料夾在操作期間遭替換：{target.parent}")
    except BaseException as error:
        primary_error = error
        raise
    finally:
        _close_descriptors(
            (replacement_fd,),
            f"關閉資料夾失敗：{target.parent}",
            primary_error,
        )


def _assert_open_directory_identity(
    directory_fd: int,
    expected: DirectoryIdentity | None,
    directory: Path,
) -> None:
    if expected is None:
        return
    try:
        metadata = os.fstat(directory_fd)
    except OSError as error:
        raise ContractError(f"無法確認資料夾身分：{directory}") from error
    if (metadata.st_dev, metadata.st_ino) != (expected.device, expected.inode):
        raise ContractError(f"資料夾在操作期間遭替換：{directory}")


def _cleanup_publication(
    *,
    parent_fd: int,
    temp_fd: int,
    temp_name: str | None,
    target: Path,
) -> None:
    primary_error = sys.exc_info()[1]
    cleanup_errors: list[OSError] = []
    if temp_fd >= 0:
        try:
            os.close(temp_fd)
        except OSError as error:
            cleanup_errors.append(error)
    if temp_name is not None:
        try:
            os.unlink(temp_name, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        except OSError as error:
            cleanup_errors.append(error)
    try:
        os.close(parent_fd)
    except OSError as error:
        cleanup_errors.append(error)
    if not cleanup_errors:
        return
    if primary_error is not None:
        if hasattr(primary_error, "add_note"):
            primary_error.add_note(f"清理暫存資源失敗：{cleanup_errors[0]}")
        return
    raise ContractError(f"清理暫存資源失敗：{target}") from cleanup_errors[0]


def _close_descriptors(
    descriptors: tuple[int, ...],
    message: str,
    primary_error: BaseException | None,
) -> None:
    first_error = None
    for descriptor in descriptors:
        if descriptor < 0:
            continue
        try:
            os.close(descriptor)
        except OSError as error:
            if first_error is None:
                first_error = error
    if first_error is None:
        return
    if primary_error is not None:
        if hasattr(primary_error, "add_note"):
            primary_error.add_note(f"{message}：{first_error}")
        return
    raise ContractError(message) from first_error


def _fsync_directory(directory_fd: int) -> None:
    os.fsync(directory_fd)
