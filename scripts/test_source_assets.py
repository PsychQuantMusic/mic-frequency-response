#!/usr/bin/env python3

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import source_assets_lib.storage as storage_module
from source_assets_lib import (
    AtomicArtifactStore,
    ContractError,
    FileDigest,
    IntegrityError,
    digest_file,
    resolve_local_path,
    validate_signature,
)


class _RecordingBinaryHandle:
    def __init__(self, handle, events):
        self._handle = handle
        self._events = events

    def __enter__(self):
        self._handle.__enter__()
        return self

    def __exit__(self, *args):
        return self._handle.__exit__(*args)

    def __getattr__(self, name):
        return getattr(self._handle, name)

    def write(self, content):
        self._events.append("write")
        return self._handle.write(content)

    def flush(self):
        self._events.append("flush")
        return self._handle.flush()


class InterfaceExistenceTests(unittest.TestCase):
    def test_production_modules_exist(self):
        library = Path(__file__).parent / "source_assets_lib"
        expected = [library / "__init__.py", library / "model.py", library / "storage.py"]
        missing_paths = [str(path) for path in expected if not path.is_file()]
        self.assertEqual(missing_paths, [])


class StorageContractTests(unittest.TestCase):
    def test_resolve_local_path_rejects_parent_escape(self):
        with tempfile.TemporaryDirectory() as td:
            mic = Path(td) / "data" / "mic"
            mic.mkdir(parents=True)
            with self.assertRaises(ContractError):
                resolve_local_path(mic, "source/../meta.yaml")

    def test_resolve_local_path_rejects_symlink_escape(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            mic = root / "data" / "mic"
            outside = root / "outside"
            mic.mkdir(parents=True)
            outside.mkdir()
            (mic / "source").symlink_to(outside, target_is_directory=True)

            with self.assertRaises(ContractError):
                resolve_local_path(mic, "source/manual.pdf")

    def test_resolve_local_path_requires_exact_source_basename(self):
        with tempfile.TemporaryDirectory() as td:
            mic = Path(td) / "data" / "mic"
            mic.mkdir(parents=True)
            invalid_paths = [
                "other/manual.pdf",
                "source/nested/manual.pdf",
                str(mic / "source" / "manual.pdf"),
                r"source\manual.pdf",
                "source/./manual.pdf",
            ]
            accepted = []
            for local_path in invalid_paths:
                try:
                    resolve_local_path(mic, local_path)
                except ContractError:
                    continue
                accepted.append(local_path)

            self.assertEqual(accepted, [])

    def test_resolve_local_path_rejects_existing_target_symlink(self):
        with tempfile.TemporaryDirectory() as td:
            mic = Path(td) / "data" / "mic"
            source = mic / "source"
            source.mkdir(parents=True)
            real_file = source / "real.pdf"
            real_file.write_bytes(b"%PDF-real")
            (source / "manual.pdf").symlink_to(real_file.name)

            with self.assertRaises(ContractError):
                resolve_local_path(mic, "source/manual.pdf")

    def test_validate_signature_rejects_non_pdf_content(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "manual.pdf"
            path.write_bytes(b"<html>not a PDF</html>")

            with self.assertRaises(IntegrityError):
                validate_signature(path, "application/pdf")

    def test_validate_signature_rejects_non_png_content(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "page.png"
            path.write_bytes(b"not a PNG")

            with self.assertRaises(IntegrityError):
                validate_signature(path, "image/png")

    def test_validate_signature_rejects_svg_doctype_case_insensitively(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "chart.svg"
            path.write_bytes(b'<!doctype svg><svg xmlns="http://www.w3.org/2000/svg"/>')

            with self.assertRaisesRegex(IntegrityError, "DOCTYPE"):
                validate_signature(path, "image/svg+xml")

    def test_validate_signature_rejects_svg_entity_case_insensitively(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "chart.svg"
            path.write_bytes(b'<!entity x "unsafe"><svg xmlns="http://www.w3.org/2000/svg"/>')

            with self.assertRaisesRegex(IntegrityError, "ENTITY"):
                validate_signature(path, "image/svg+xml")

    def test_validate_signature_requires_svg_root_element(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "chart.svg"
            path.write_bytes(b'<html xmlns="http://www.w3.org/1999/xhtml"/>')

            with self.assertRaises(IntegrityError):
                validate_signature(path, "image/svg+xml")

    def test_validate_signature_rejects_unknown_media_type(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "artifact.bin"
            path.write_bytes(b"content")

            with self.assertRaises(ContractError):
                validate_signature(path, "application/octet-stream")

    def test_digest_file_returns_size_and_sha256(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "artifact.bin"
            path.write_bytes(b"abc")

            self.assertEqual(
                digest_file(path),
                FileDigest(
                    size_bytes=3,
                    sha256="ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
                ),
            )

    def test_publish_blob_is_atomic_durable_and_uses_same_directory_temp(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "manual.pdf"
            content = b"%PDF-1.7\nfixture\n"
            events = []
            temp_paths = []
            real_fdopen = os.fdopen
            real_fsync = os.fsync
            real_mkstemp = tempfile.mkstemp
            real_replace = os.replace
            fsync_calls = 0

            def recording_fdopen(*args, **kwargs):
                return _RecordingBinaryHandle(real_fdopen(*args, **kwargs), events)

            def recording_mkstemp(*args, **kwargs):
                fd, name = real_mkstemp(*args, **kwargs)
                temp_paths.append(Path(name))
                return fd, name

            def recording_fsync(fd):
                nonlocal fsync_calls
                fsync_calls += 1
                events.append("file fsync" if fsync_calls == 1 else "directory fsync")
                return real_fsync(fd)

            def recording_replace(source, destination):
                events.append("os.replace")
                return real_replace(source, destination)

            def producer(handle):
                handle.write(content)
                return "producer-result"

            with (
                patch.object(storage_module.os, "fdopen", recording_fdopen),
                patch.object(storage_module.os, "fsync", recording_fsync),
                patch.object(storage_module.os, "replace", recording_replace),
                patch.object(storage_module.tempfile, "mkstemp", recording_mkstemp),
            ):
                result = AtomicArtifactStore().publish_blob(target, "application/pdf", producer)

            self.assertEqual(
                (
                    events,
                    [path.parent for path in temp_paths],
                    result,
                    target.read_bytes() if target.exists() else None,
                    list(target.parent.glob(f".{target.name}.*.tmp")),
                ),
                (
                    ["write", "flush", "file fsync", "os.replace", "directory fsync"],
                    [target.parent],
                    (
                        "producer-result",
                        FileDigest(
                            size_bytes=17,
                            sha256="58346148e907c6d42d5efbb6ac681765701d53d09413067fe67e2b7ea9294e86",
                        ),
                    ),
                    content,
                    [],
                ),
            )

    def test_publish_blob_cleans_temp_and_preserves_target_on_producer_failure(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "manual.pdf"
            target.write_bytes(b"old target")

            def failing_producer(handle):
                handle.write(b"%PDF-partial")
                raise RuntimeError("producer failure")

            caught = None
            try:
                AtomicArtifactStore().publish_blob(
                    target,
                    "application/pdf",
                    failing_producer,
                )
            except RuntimeError as error:
                caught = str(error)

            self.assertEqual(
                (
                    caught,
                    target.read_bytes(),
                    list(target.parent.glob(f".{target.name}.*.tmp")),
                ),
                ("producer failure", b"old target", []),
            )

    def test_publish_blob_cleans_temp_and_preserves_target_on_validator_failure(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "manual.pdf"
            target.write_bytes(b"old target")

            def producer(handle):
                handle.write(b"%PDF-new")

            caught = None
            with patch.object(
                storage_module,
                "validate_signature",
                side_effect=IntegrityError("validator failure"),
            ):
                try:
                    AtomicArtifactStore().publish_blob(
                        target,
                        "application/pdf",
                        producer,
                    )
                except IntegrityError as error:
                    caught = str(error)

            self.assertEqual(
                (
                    caught,
                    target.read_bytes(),
                    list(target.parent.glob(f".{target.name}.*.tmp")),
                ),
                ("validator failure", b"old target", []),
            )

    def test_publish_blob_cleans_temp_and_preserves_target_on_replace_failure(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "manual.pdf"
            target.write_bytes(b"old target")

            def producer(handle):
                handle.write(b"%PDF-new")

            caught = None
            with patch.object(
                storage_module.os,
                "replace",
                side_effect=OSError("replace failure"),
            ):
                try:
                    AtomicArtifactStore().publish_blob(
                        target,
                        "application/pdf",
                        producer,
                    )
                except Exception as error:
                    caught = (type(error), str(error))

            self.assertEqual(
                (
                    caught,
                    target.read_bytes(),
                    list(target.parent.glob(f".{target.name}.*.tmp")),
                ),
                (
                    (ContractError, f"artifact replace 失敗：{target}"),
                    b"old target",
                    [],
                ),
            )

    def test_publish_blob_reports_directory_fsync_failure_after_replacement(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "manual.pdf"
            target.write_bytes(b"old target")
            new_content = b"%PDF-new"

            def producer(handle):
                handle.write(new_content)

            caught = None
            with patch.object(
                storage_module,
                "_fsync_directory",
                side_effect=OSError("directory fsync failure"),
            ):
                try:
                    AtomicArtifactStore().publish_blob(
                        target,
                        "application/pdf",
                        producer,
                    )
                except Exception as error:
                    caught = (type(error), str(error))

            self.assertEqual(
                (
                    caught,
                    target.read_bytes(),
                    list(target.parent.glob(f".{target.name}.*.tmp")),
                ),
                (
                    (ContractError, f"父資料夾 fsync 失敗：{target.parent}"),
                    new_content,
                    [],
                ),
            )

    def test_publish_manifest_writes_canonical_utf8_json_durably(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "source-manifest.json"
            events = []
            temp_paths = []
            real_fdopen = os.fdopen
            real_fsync = os.fsync
            real_mkstemp = tempfile.mkstemp
            real_replace = os.replace
            fsync_calls = 0

            def recording_fdopen(*args, **kwargs):
                return _RecordingBinaryHandle(real_fdopen(*args, **kwargs), events)

            def recording_mkstemp(*args, **kwargs):
                fd, name = real_mkstemp(*args, **kwargs)
                temp_paths.append(Path(name))
                return fd, name

            def recording_fsync(fd):
                nonlocal fsync_calls
                fsync_calls += 1
                events.append("file fsync" if fsync_calls == 1 else "directory fsync")
                return real_fsync(fd)

            def recording_replace(source, destination):
                events.append("os.replace")
                return real_replace(source, destination)

            with (
                patch.object(storage_module.os, "fdopen", recording_fdopen),
                patch.object(storage_module.os, "fsync", recording_fsync),
                patch.object(storage_module.os, "replace", recording_replace),
                patch.object(storage_module.tempfile, "mkstemp", recording_mkstemp),
            ):
                AtomicArtifactStore().publish_manifest(
                    target,
                    {"z": "終", "a": [2, 1]},
                )

            self.assertEqual(
                (
                    events,
                    [path.parent for path in temp_paths],
                    target.read_bytes() if target.exists() else None,
                    list(target.parent.glob(f".{target.name}.*.tmp")),
                ),
                (
                    ["write", "flush", "file fsync", "os.replace", "directory fsync"],
                    [target.parent],
                    b'{"a":[2,1],"z":"\xe7\xb5\x82"}\n',
                    [],
                ),
            )


if __name__ == "__main__":
    unittest.main()
