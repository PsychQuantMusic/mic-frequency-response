#!/usr/bin/env python3

import json
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
    Policy,
    RemoteError,
    SourceAssetError,
    digest_file,
    load_manifest,
    resolve_local_path,
    validate_manifest,
    validate_signature,
)


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_FIXTURES = REPOSITORY_ROOT / "digitizer" / "test" / "fixtures" / "source-manifests"


def _load_fixture(name):
    return json.loads((MANIFEST_FIXTURES / name).read_text(encoding="utf-8"))


def _make_mic_dir(root, document, curves=("frequency-response--typical.csv",)):
    mic_dir = Path(root) / "data" / document["mic_slug"]
    mic_dir.mkdir(parents=True)
    curve_lines = "\n".join(f"  - file: {curve}" for curve in curves)
    (mic_dir / "meta.yaml").write_text(f"curves:\n{curve_lines}\n", encoding="utf-8")
    for curve in curves:
        (mic_dir / curve).write_text("frequency_hz,db\n1000,0\n", encoding="utf-8")
    return mic_dir


def _policy(*, redactions=None):
    return Policy(
        hosts=frozenset(
            {
                "pubs.shure.com",
                "www.neumann.com",
            }
        ),
        redactions={} if redactions is None else redactions,
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

    def test_validate_signature_rejects_utf16_svg_doctype(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "chart.svg"
            path.write_bytes(
                (
                    '<?xml version="1.0" encoding="utf-16"?>'
                    '<!DOCTYPE svg [<!ENTITY x "unsafe">]>'
                    '<svg xmlns="http://www.w3.org/2000/svg">&x;</svg>'
                ).encode("utf-16")
            )

            with self.assertRaisesRegex(IntegrityError, "DOCTYPE"):
                validate_signature(path, "image/svg+xml")

    def test_validate_signature_requires_svg_root_element(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "chart.svg"
            path.write_bytes(b'<html xmlns="http://www.w3.org/1999/xhtml"/>')

            with self.assertRaises(IntegrityError):
                validate_signature(path, "image/svg+xml")

    def test_validate_signature_reports_unknown_svg_encoding_as_integrity_error(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "chart.svg"
            path.write_bytes(
                b'<?xml version="1.0" encoding="x-unknown"?>'
                b'<svg xmlns="http://www.w3.org/2000/svg"/>'
            )

            caught = None
            try:
                validate_signature(path, "image/svg+xml")
            except Exception as error:
                caught = (type(error), getattr(error, "exit_code", None), str(error))

            self.assertEqual(
                caught,
                (IntegrityError, 2, f"SVG XML 無法解析：{path}"),
            )

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

    def test_digest_file_reports_open_failure_as_contract_error(self):
        path = Path("/not-opened/artifact.bin")
        with patch.object(Path, "open", side_effect=OSError("open failure")):
            with self.assertRaisesRegex(ContractError, "無法讀取檔案") as caught:
                digest_file(path)

        self.assertEqual(caught.exception.exit_code, 1)

    def test_digest_file_reports_read_failure_as_contract_error(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "artifact.bin"
            path.write_bytes(b"abc")

            class FailingReadHandle:
                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

                def read(self, _size):
                    raise OSError("read failure")

            with patch.object(Path, "open", return_value=FailingReadHandle()):
                with self.assertRaisesRegex(ContractError, "無法讀取檔案") as caught:
                    digest_file(path)

            self.assertEqual(caught.exception.exit_code, 1)

    def test_validate_signature_reports_read_failure_as_contract_error(self):
        path = Path("/not-read/chart.svg")
        with patch.object(Path, "read_bytes", side_effect=OSError("read failure")):
            with self.assertRaisesRegex(ContractError, "無法讀取檔案") as caught:
                validate_signature(path, "image/svg+xml")

        self.assertEqual(caught.exception.exit_code, 1)


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

    def test_publish_blob_preserves_existing_source_asset_error(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "manual.pdf"
            target.write_bytes(b"old target")
            expected = IntegrityError("已分類的完整性錯誤")

            def failing_producer(_handle):
                raise expected

            caught = None
            try:
                AtomicArtifactStore().publish_blob(
                    target,
                    "application/pdf",
                    failing_producer,
                )
            except SourceAssetError as error:
                caught = error

            self.assertIs(caught, expected)
            self.assertEqual(
                (
                    target.read_bytes(),
                    list(target.parent.glob(f".{target.name}.*.tmp")),
                ),
                (b"old target", []),
            )

    def test_publish_blob_reports_file_fsync_failure_as_contract_error(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "manual.pdf"
            target.write_bytes(b"old target")

            def producer(handle):
                handle.write(b"%PDF-new")

            caught = None
            with patch.object(
                storage_module.os,
                "fsync",
                side_effect=OSError("file fsync failure"),
            ):
                try:
                    AtomicArtifactStore().publish_blob(
                        target,
                        "application/pdf",
                        producer,
                    )
                except Exception as error:
                    caught = (type(error), getattr(error, "exit_code", None), str(error))

            self.assertEqual(
                (
                    caught,
                    target.read_bytes(),
                    list(target.parent.glob(f".{target.name}.*.tmp")),
                ),
                (
                    (ContractError, 1, f"artifact 檔案 fsync 失敗：{target}"),
                    b"old target",
                    [],
                ),
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

    def test_publish_manifest_cleans_temp_and_preserves_target_on_replace_failure(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "source-manifest.json"
            target.write_bytes(b"old manifest\n")
            caught = None

            with patch.object(
                storage_module.os,
                "replace",
                side_effect=OSError("replace failure"),
            ):
                try:
                    AtomicArtifactStore().publish_manifest(target, {"schema_version": 1})
                except Exception as error:
                    caught = (type(error), str(error))

            self.assertEqual(
                (
                    caught,
                    target.read_bytes(),
                    list(target.parent.glob(f".{target.name}.*.tmp")),
                ),
                (
                    (ContractError, f"manifest replace 失敗：{target}"),
                    b"old manifest\n",
                    [],
                ),
            )

    def test_publish_manifest_reports_file_fsync_failure_as_contract_error(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "source-manifest.json"
            target.write_bytes(b"old manifest\n")
            caught = None

            with patch.object(
                storage_module.os,
                "fsync",
                side_effect=OSError("file fsync failure"),
            ):
                try:
                    AtomicArtifactStore().publish_manifest(
                        target,
                        {"schema_version": 1},
                    )
                except Exception as error:
                    caught = (type(error), getattr(error, "exit_code", None), str(error))

            self.assertEqual(
                (
                    caught,
                    target.read_bytes(),
                    list(target.parent.glob(f".{target.name}.*.tmp")),
                ),
                (
                    (ContractError, 1, f"manifest 檔案 fsync 失敗：{target}"),
                    b"old manifest\n",
                    [],
                ),
            )


class ManifestContractTests(unittest.TestCase):
    def test_load_manifest_reads_json_object(self):
        expected = _load_fixture("available-pdf.json")
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "source-manifest.json"
            path.write_text(json.dumps(expected), encoding="utf-8")

            self.assertEqual(load_manifest(path, allow_pending=False), expected)

    def test_load_manifest_rejects_pending_unless_explicitly_allowed(self):
        document = _load_fixture("pending-pdf.json")
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "source-manifest.json"
            path.write_text(json.dumps(document), encoding="utf-8")

            with self.assertRaises(ContractError):
                load_manifest(path, allow_pending=False)

    def test_load_manifest_rejects_duplicate_json_keys(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "source-manifest.json"
            path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")

            with self.assertRaises(ContractError):
                load_manifest(path, allow_pending=False)

    def test_load_manifest_rejects_invalid_json_and_non_object(self):
        with tempfile.TemporaryDirectory() as td:
            invalid = Path(td) / "invalid.json"
            invalid.write_text("{", encoding="utf-8")
            non_object = Path(td) / "non-object.json"
            non_object.write_text("[]", encoding="utf-8")

            for path in (invalid, non_object):
                with self.subTest(path=path.name), self.assertRaises(ContractError):
                    load_manifest(path, allow_pending=False)

    def test_validate_manifest_rejects_unknown_top_level_field(self):
        document = _load_fixture("derived-chain.json")
        document["unknown"] = True
        with tempfile.TemporaryDirectory() as td:
            mic_dir = _make_mic_dir(td, document)

            with self.assertRaises(ContractError):
                validate_manifest(document, mic_dir, _policy(), allow_pending=False)

    def test_validate_manifest_requires_schema_version_one(self):
        document = _load_fixture("derived-chain.json")
        document["schema_version"] = 2
        with tempfile.TemporaryDirectory() as td:
            mic_dir = _make_mic_dir(td, document)

            with self.assertRaises(ContractError):
                validate_manifest(document, mic_dir, _policy(), allow_pending=False)

    def test_validate_manifest_rejects_invalid_mic_slug(self):
        document = _load_fixture("derived-chain.json")
        document["mic_slug"] = "Shure_SM58"
        with tempfile.TemporaryDirectory() as td:
            mic_dir = _make_mic_dir(td, document)

            with self.assertRaises(ContractError):
                validate_manifest(document, mic_dir, _policy(), allow_pending=False)

    def test_validate_manifest_rejects_invalid_reference_variants(self):
        invalid_references = [
            [{"role": "download-page", "url": "https://pubs.shure.com/product"}],
            [{"role": "product-page", "url": "http://pubs.shure.com/product"}],
            [
                {
                    "role": "product-page",
                    "url": "https://pubs.shure.com/product",
                    "note": "unknown",
                }
            ],
            "not-an-array",
        ]
        accepted = []
        for index, references in enumerate(invalid_references):
            document = _load_fixture("derived-chain.json")
            document["references"] = references
            with tempfile.TemporaryDirectory() as td:
                mic_dir = _make_mic_dir(td, document)
                try:
                    validate_manifest(document, mic_dir, _policy(), allow_pending=False)
                except ContractError:
                    continue
                accepted.append(index)

        self.assertEqual(accepted, [])

    def test_load_manifest_reports_non_string_reference_role_as_contract_error(self):
        document = _load_fixture("available-pdf.json")
        document["references"] = [
            {"role": [], "url": "https://pubs.shure.com/product"}
        ]
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "source-manifest.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            caught_type = None
            try:
                load_manifest(path, allow_pending=False)
            except Exception as error:
                caught_type = type(error)

            self.assertIs(caught_type, ContractError)

    def test_load_manifest_requires_artifacts_array(self):
        document = _load_fixture("available-pdf.json")
        document["artifacts"] = "not-an-array"
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "source-manifest.json"
            path.write_text(json.dumps(document), encoding="utf-8")

            with self.assertRaises(ContractError):
                load_manifest(path, allow_pending=False)

    def test_load_manifest_rejects_unknown_artifact_fields_and_variants(self):
        cases = []
        for index in range(3):
            document = _load_fixture("derived-chain.json")
            document["artifacts"][index]["unknown"] = True
            cases.append(document)
        pending = _load_fixture("pending-pdf.json")
        pending["artifacts"][0]["unknown"] = True
        cases.append(pending)
        unavailable = _load_fixture("unavailable-redacted.json")
        unavailable["artifacts"][0]["unknown"] = True
        cases.append(unavailable)
        invalid_variant = _load_fixture("derived-chain.json")
        invalid_variant["artifacts"][0]["availability"] = "ignored"
        cases.append(invalid_variant)

        accepted = []
        with tempfile.TemporaryDirectory() as td:
            for index, document in enumerate(cases):
                path = Path(td) / f"manifest-{index}.json"
                path.write_text(json.dumps(document), encoding="utf-8")
                try:
                    load_manifest(path, allow_pending=True)
                except ContractError:
                    continue
                accepted.append(index)

        self.assertEqual(accepted, [])

    def test_load_manifest_rejects_invalid_artifact_values(self):
        cases = []

        def add(name, index, field, value):
            document = _load_fixture(name)
            document["artifacts"][index][field] = value
            cases.append(document)

        add("available-pdf.json", 0, "id", "")
        add("available-pdf.json", 0, "size_bytes", True)
        add("available-pdf.json", 0, "sha256", "A" * 64)
        add("available-pdf.json", 0, "redistribution", "public")
        add(
            "available-pdf.json",
            0,
            "curve_files",
            ["frequency-response--typical.csv", "frequency-response--typical.csv"],
        )
        add("available-pdf.json", 0, "allowed_hosts", [])
        add("available-pdf.json", 0, "local_path", "source/original.svg")
        add("available-pdf.json", 0, "source_url", "http://pubs.shure.com/manual.pdf")
        add("unavailable-redacted.json", 0, "reason", "")
        add("unavailable-redacted.json", 0, "redaction_ref", "")
        add("derived-chain.json", 1, "derived_from", "")
        add("derived-chain.json", 2, "curve_files", ["nested/curve.csv"])

        accepted = []
        with tempfile.TemporaryDirectory() as td:
            for index, document in enumerate(cases):
                path = Path(td) / f"manifest-{index}.json"
                path.write_text(json.dumps(document), encoding="utf-8")
                try:
                    load_manifest(path, allow_pending=True)
                except ContractError:
                    continue
                accepted.append(index)

        self.assertEqual(accepted, [])

    def test_load_manifest_rejects_invalid_derivation_values(self):
        cases = []

        def page_case(mutator):
            document = _load_fixture("derived-chain.json")
            mutator(document["artifacts"][1]["derivation"])
            cases.append(document)

        def crop_case(mutator):
            document = _load_fixture("derived-chain.json")
            mutator(document["artifacts"][2]["derivation"])
            cases.append(document)

        page_case(lambda value: value.pop("page"))
        page_case(lambda value: value.update(unknown=True))
        page_case(lambda value: value.update(page=True))
        page_case(lambda value: value.update(render_dpi=299))
        page_case(lambda value: value.update(render_tool=""))
        crop_case(lambda value: value.update(crop_box_px=[0, 0, 100]))
        crop_case(lambda value: value.update(crop_box_px=[0, 0, True, 100]))
        crop_case(lambda value: value.update(crop_box_px=[0, -1, 100, 100]))
        crop_case(lambda value: value.update(unknown=True))
        crop_case(lambda value: value.update(crop_tool=""))

        accepted = []
        with tempfile.TemporaryDirectory() as td:
            for index, document in enumerate(cases):
                path = Path(td) / f"manifest-{index}.json"
                path.write_text(json.dumps(document), encoding="utf-8")
                try:
                    load_manifest(path, allow_pending=False)
                except ContractError:
                    continue
                accepted.append(index)

        self.assertEqual(accepted, [])

    def test_validate_manifest_rejects_duplicate_local_path(self):
        document = _load_fixture("derived-chain.json")
        document["artifacts"][2]["local_path"] = document["artifacts"][1]["local_path"]
        with tempfile.TemporaryDirectory() as td:
            mic_dir = _make_mic_dir(td, document)

            with self.assertRaises(ContractError):
                validate_manifest(document, mic_dir, _policy(), allow_pending=False)

    def test_validate_manifest_rejects_duplicate_artifact_id(self):
        document = _load_fixture("derived-chain.json")
        duplicate = {
            "id": "duplicate-source",
            "role": "original",
            "availability": "unavailable",
            "source_url": "https://pubs.shure.com/missing.pdf",
            "curve_files": ["frequency-response--typical.csv"],
            "checked_at": "2026-07-15T12:00:00Z",
            "reason": "not found",
            "redistribution": "local-only",
            "allowed_hosts": ["pubs.shure.com"],
        }
        document["artifacts"].extend([duplicate, {**duplicate, "reason": "gone"}])
        with tempfile.TemporaryDirectory() as td:
            mic_dir = _make_mic_dir(td, document)

            with self.assertRaisesRegex(ContractError, "id"):
                validate_manifest(document, mic_dir, _policy(), allow_pending=False)

    def test_validate_manifest_requires_slug_to_match_directory(self):
        document = _load_fixture("derived-chain.json")
        with tempfile.TemporaryDirectory() as td:
            mic_dir = _make_mic_dir(td, document)
            wrong_dir = mic_dir.with_name("different-microphone")
            mic_dir.rename(wrong_dir)

            with self.assertRaises(ContractError):
                validate_manifest(document, wrong_dir, _policy(), allow_pending=False)

    def test_validate_manifest_applies_strict_local_path_contract(self):
        document = _load_fixture("derived-chain.json")
        document["artifacts"][0]["local_path"] = "source/nested/original.pdf"
        with tempfile.TemporaryDirectory() as td:
            mic_dir = _make_mic_dir(td, document)

            with self.assertRaises(ContractError):
                validate_manifest(document, mic_dir, _policy(), allow_pending=False)

    def test_validate_manifest_enforces_exact_host_policy(self):
        cases = []
        wrong_entry_host = _load_fixture("derived-chain.json")
        wrong_entry_host["artifacts"][0]["allowed_hosts"] = ["www.neumann.com"]
        cases.append(wrong_entry_host)

        unknown_source_host = _load_fixture("derived-chain.json")
        unknown_source_host["artifacts"][0]["source_url"] = "https://evil.example/manual.pdf"
        unknown_source_host["artifacts"][0]["allowed_hosts"] = ["evil.example"]
        cases.append(unknown_source_host)

        unknown_reference_host = _load_fixture("derived-chain.json")
        unknown_reference_host["references"] = [
            {"role": "product-page", "url": "https://evil.example/product"}
        ]
        cases.append(unknown_reference_host)

        accepted = []
        for index, document in enumerate(cases):
            with tempfile.TemporaryDirectory() as td:
                mic_dir = _make_mic_dir(td, document)
                try:
                    validate_manifest(document, mic_dir, _policy(), allow_pending=False)
                except ContractError:
                    continue
                accepted.append(index)

        self.assertEqual(accepted, [])

    def test_validate_manifest_enforces_derived_parent_roles(self):
        svg_parent = _load_fixture("derived-chain.json")
        svg_parent["artifacts"][0]["local_path"] = "source/original.svg"
        svg_parent["artifacts"][0]["media_type"] = "image/svg+xml"

        crop_from_original = _load_fixture("derived-chain.json")
        original = crop_from_original["artifacts"][0]
        crop = crop_from_original["artifacts"][2]
        crop["derived_from"] = original["id"]
        crop["derived_from_sha256"] = original["sha256"]

        accepted = []
        for index, document in enumerate((svg_parent, crop_from_original)):
            with tempfile.TemporaryDirectory() as td:
                mic_dir = _make_mic_dir(td, document)
                try:
                    validate_manifest(document, mic_dir, _policy(), allow_pending=False)
                except ContractError:
                    continue
                accepted.append(index)

        self.assertEqual(accepted, [])

    def test_validate_manifest_requires_declared_curve_files_to_exist_in_meta(self):
        extra_curve = _load_fixture("derived-chain.json")
        extra_curve["artifacts"][0]["curve_files"].append("frequency-response--ghost.csv")

        accepted = []
        with tempfile.TemporaryDirectory() as td:
            mic_dir = _make_mic_dir(td, extra_curve)
            try:
                validate_manifest(extra_curve, mic_dir, _policy(), allow_pending=False)
            except ContractError:
                pass
            else:
                accepted.append("extra")

        missing_file = _load_fixture("derived-chain.json")
        with tempfile.TemporaryDirectory() as td:
            mic_dir = _make_mic_dir(td, missing_file)
            (mic_dir / "frequency-response--typical.csv").unlink()
            try:
                validate_manifest(missing_file, mic_dir, _policy(), allow_pending=False)
            except ContractError:
                pass
            else:
                accepted.append("missing")

        self.assertEqual(accepted, [])

    def test_validate_manifest_enforces_crop_subset_and_pdf_crop_coverage(self):
        wrong_subset = _load_fixture("derived-chain.json")
        wrong_subset["artifacts"][2]["curve_files"] = ["frequency-response--other.csv"]
        wrong_subset["artifacts"].append(
            {
                "id": "other-source",
                "role": "original",
                "availability": "unavailable",
                "source_url": "https://pubs.shure.com/other.pdf",
                "curve_files": ["frequency-response--other.csv"],
                "checked_at": "2026-07-15T12:00:00Z",
                "reason": "not found",
                "redistribution": "local-only",
                "allowed_hosts": ["pubs.shure.com"],
            }
        )

        no_crop = _load_fixture("available-pdf.json")
        accepted = []
        with tempfile.TemporaryDirectory() as td:
            mic_dir = _make_mic_dir(
                td,
                wrong_subset,
                curves=(
                    "frequency-response--typical.csv",
                    "frequency-response--other.csv",
                ),
            )
            try:
                validate_manifest(wrong_subset, mic_dir, _policy(), allow_pending=False)
            except ContractError:
                pass
            else:
                accepted.append("subset")

        with tempfile.TemporaryDirectory() as td:
            mic_dir = _make_mic_dir(td, no_crop)
            try:
                validate_manifest(no_crop, mic_dir, _policy(), allow_pending=False)
            except ContractError:
                pass
            else:
                accepted.append("coverage")

        self.assertEqual(accepted, [])

    def test_validate_manifest_rejects_invalid_utc_timestamp(self):
        document = _load_fixture("derived-chain.json")
        document["artifacts"][0]["retrieved_at"] = "2026-02-31T12:00:00Z"
        with tempfile.TemporaryDirectory() as td:
            mic_dir = _make_mic_dir(td, document)

            with self.assertRaises(ContractError):
                validate_manifest(document, mic_dir, _policy(), allow_pending=False)

    def test_validate_manifest_rejects_derived_graph_cycle(self):
        document = _load_fixture("derived-chain.json")
        page = document["artifacts"][1]
        crop = document["artifacts"][2]
        page["derived_from"] = crop["id"]
        crop["derived_from"] = page["id"]
        with tempfile.TemporaryDirectory() as td:
            mic_dir = _make_mic_dir(td, document)

            with self.assertRaisesRegex(ContractError, "循環"):
                validate_manifest(document, mic_dir, _policy(), allow_pending=False)

    def test_validate_manifest_rejects_parent_hash_mismatch(self):
        document = _load_fixture("derived-chain.json")
        document["artifacts"][1]["derived_from_sha256"] = "0" * 64
        with tempfile.TemporaryDirectory() as td:
            mic_dir = _make_mic_dir(td, document)

            with self.assertRaises(IntegrityError):
                validate_manifest(document, mic_dir, _policy(), allow_pending=False)

    def test_validate_manifest_requires_original_curve_coverage(self):
        document = _load_fixture("derived-chain.json")
        with tempfile.TemporaryDirectory() as td:
            mic_dir = _make_mic_dir(
                td,
                document,
                curves=(
                    "frequency-response--typical.csv",
                    "frequency-response--other.csv",
                ),
            )

            with self.assertRaisesRegex(ContractError, "覆蓋"):
                validate_manifest(document, mic_dir, _policy(), allow_pending=False)

    def test_validate_manifest_propagates_stale_state_to_descendants(self):
        document = _load_fixture("derived-chain.json")
        document["artifacts"][1]["derived_from_sha256"] = "0" * 64
        with tempfile.TemporaryDirectory() as td:
            mic_dir = _make_mic_dir(td, document)

            with self.assertRaisesRegex(IntegrityError, "frequency-response-chart"):
                validate_manifest(document, mic_dir, _policy(), allow_pending=False)

    def test_validate_manifest_rejects_unavailable_missing_redaction_reference(self):
        document = _load_fixture("unavailable-redacted.json")
        with tempfile.TemporaryDirectory() as td:
            mic_dir = _make_mic_dir(td, document)

            with self.assertRaisesRegex(ContractError, "redaction_ref"):
                validate_manifest(document, mic_dir, _policy(), allow_pending=False)

    def test_validate_manifest_rejects_wrong_redaction_record(self):
        document = _load_fixture("unavailable-redacted.json")
        reference_id = document["artifacts"][0]["redaction_ref"]
        record = _load_fixture("redaction-no-stable-endpoint.json")["entries"][0]
        policies = [
            _policy(redactions={reference_id: {**record, "disposition": "replaced"}}),
            _policy(redactions={reference_id: {**record, "mic_slug": "other-mic"}}),
            _policy(redactions={reference_id: {**record, "id": "other-record"}}),
        ]

        accepted = []
        for index, policy in enumerate(policies):
            with tempfile.TemporaryDirectory() as td:
                mic_dir = _make_mic_dir(td, document)
                try:
                    validate_manifest(document, mic_dir, policy, allow_pending=False)
                except ContractError:
                    continue
                accepted.append(index)

        self.assertEqual(accepted, [])

    def test_validate_manifest_requires_same_mic_redaction_mapping(self):
        document = _load_fixture("derived-chain.json")
        no_endpoint = _load_fixture("redaction-no-stable-endpoint.json")["entries"][0]
        no_endpoint = {
            **no_endpoint,
            "id": "unreferenced-redaction",
            "mic_slug": document["mic_slug"],
        }
        replaced = _load_fixture("redaction-replaced.json")["entries"][0]
        replaced = {
            **replaced,
            "id": "missing-canonical-mapping",
            "mic_slug": document["mic_slug"],
            "canonical_url": "https://pubs.shure.com/canonical.pdf",
        }
        policies = [
            _policy(redactions={no_endpoint["id"]: no_endpoint}),
            _policy(redactions={replaced["id"]: replaced}),
        ]

        accepted = []
        for index, policy in enumerate(policies):
            with tempfile.TemporaryDirectory() as td:
                mic_dir = _make_mic_dir(td, document)
                try:
                    validate_manifest(document, mic_dir, policy, allow_pending=False)
                except ContractError:
                    continue
                accepted.append(index)

        self.assertEqual(accepted, [])

    def test_pending_gate_applies_to_load_and_direct_validation(self):
        document = _load_fixture("pending-pdf.json")
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "source-manifest.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            mic_dir = _make_mic_dir(td, document)

            self.assertEqual(load_manifest(path, allow_pending=True), document)
            validate_manifest(document, mic_dir, _policy(), allow_pending=True)
            with self.assertRaises(ContractError):
                validate_manifest(document, mic_dir, _policy(), allow_pending=False)

    def test_validate_manifest_accepts_complete_chain_in_any_artifact_order(self):
        document = _load_fixture("derived-chain.json")
        document["artifacts"].reverse()
        with tempfile.TemporaryDirectory() as td:
            mic_dir = _make_mic_dir(td, document)

            validate_manifest(document, mic_dir, _policy(), allow_pending=False)

    def test_validate_manifest_accepts_available_svg_without_crop(self):
        document = _load_fixture("available-pdf.json")
        document["artifacts"][0]["local_path"] = "source/original.svg"
        document["artifacts"][0]["media_type"] = "image/svg+xml"
        with tempfile.TemporaryDirectory() as td:
            mic_dir = _make_mic_dir(td, document)

            validate_manifest(document, mic_dir, _policy(), allow_pending=False)

    def test_validate_manifest_accepts_no_stable_endpoint_redaction(self):
        document = _load_fixture("unavailable-redacted.json")
        record = _load_fixture("redaction-no-stable-endpoint.json")["entries"][0]
        policy = _policy(redactions={record["id"]: record})
        with tempfile.TemporaryDirectory() as td:
            mic_dir = _make_mic_dir(td, document)

            validate_manifest(document, mic_dir, policy, allow_pending=False)

    def test_error_exit_codes_match_cli_contract(self):
        self.assertEqual(
            (ContractError.exit_code, IntegrityError.exit_code, RemoteError.exit_code),
            (1, 2, 2),
        )


if __name__ == "__main__":
    unittest.main()
