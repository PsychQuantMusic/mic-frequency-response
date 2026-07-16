#!/usr/bin/env python3

from contextlib import nullcontext, redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import source_assets as source_assets_cli
import source_assets_lib.commands as commands_module
import source_assets_lib.storage as storage_module
import source_assets_lib.network as network_module
from source_assets_lib.network import (
    DownloadRequest,
    DownloadResult,
    PinnedHTTPSClient,
    PinnedTLSConnector,
    RemoteHTTPError,
    URLPolicy,
    resolve_public_addresses,
)
from source_assets_lib.commands import (
    CommandContext,
    OperationSummary,
    Selector,
    bootstrap,
    fetch,
    verify,
)
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


def _pdf_request(
    *,
    url="https://pubs.shure.com/manual.pdf",
    allowed_hosts=frozenset({"pubs.shure.com"}),
    body=b"%PDF-1.7\n",
    expected_size=None,
    max_size=100 * 1024 * 1024,
    expected_sha256=None,
):
    return DownloadRequest(
        url=url,
        allowed_hosts=allowed_hosts,
        expected_media_type="application/pdf",
        expected_size=len(body) if expected_size is None else expected_size,
        max_size=max_size,
        expected_sha256=hashlib.sha256(body).hexdigest()
        if expected_sha256 is None
        else expected_sha256,
    )


def _svg_request(
    *,
    body=b'<svg xmlns="http://www.w3.org/2000/svg"/>',
    url="https://www.neumann.com/svg/Zz09PT0=",
):
    return DownloadRequest(
        url=url,
        allowed_hosts=frozenset({"www.neumann.com"}),
        expected_media_type="image/svg+xml",
        expected_size=len(body),
        max_size=100 * 1024 * 1024,
        expected_sha256=hashlib.sha256(body).hexdigest(),
    )


def _png_request(body):
    return DownloadRequest(
        url="https://pubs.shure.com/page.png",
        allowed_hosts=frozenset({"pubs.shure.com"}),
        expected_media_type="image/png",
        expected_size=len(body),
        max_size=100 * 1024 * 1024,
        expected_sha256=hashlib.sha256(body).hexdigest(),
    )


def _unverified_pdf_request(body, *, max_size=100 * 1024 * 1024):
    return DownloadRequest(
        url="https://pubs.shure.com/manual.pdf",
        allowed_hosts=frozenset({"pubs.shure.com"}),
        expected_media_type="application/pdf",
        expected_size=None,
        max_size=max_size,
        expected_sha256=hashlib.sha256(body).hexdigest(),
    )


@dataclass
class ResponseFixture:
    status: int = 200
    headers: dict[str, str] | None = None
    body: bytes = b"%PDF-1.7\n"
    peer_ip: str = "93.184.216.34"
    request_error: Exception | None = None
    response_error: Exception | None = None
    read_error: Exception | None = None

    def __post_init__(self):
        if self.headers is None:
            self.headers = {}


class _HTTPResponseFixture:
    def __init__(self, fixture):
        self.status = fixture.status
        self._headers = {key.casefold(): value for key, value in fixture.headers.items()}
        self._body = io.BytesIO(fixture.body)
        self._read_error = fixture.read_error

    def getheader(self, name, default=None):
        return self._headers.get(name.casefold(), default)

    def read(self, amount=None):
        if self._read_error is not None:
            raise self._read_error
        return self._body.read(-1 if amount is None else amount)


class _ConnectedHTTPSFixture:
    def __init__(self, fixture):
        self.fixture = fixture
        self.requests = []
        self.closed = False

    @property
    def peer_ip(self):
        return self.fixture.peer_ip

    def request(self, method, target, headers):
        if self.fixture.request_error is not None:
            raise self.fixture.request_error
        self.requests.append((method, target, headers))

    def getresponse(self):
        if self.fixture.response_error is not None:
            raise self.fixture.response_error
        return _HTTPResponseFixture(self.fixture)

    def close(self):
        self.closed = True


class ConnectorFixture:
    def __init__(self, *fixtures):
        self.fixtures = list(fixtures or (ResponseFixture(),))
        self.connect_calls = []
        self.connections = []

    def connect(self, **kwargs):
        self.connect_calls.append(kwargs)
        fixture = self.fixtures[len(self.connect_calls) - 1]
        connection = _ConnectedHTTPSFixture(fixture)
        self.connections.append(connection)
        return connection


_UNSET_WRITE_RESULT = object()


class _WriterFixture:
    def __init__(self, *, result=_UNSET_WRITE_RESULT, error=None):
        self.result = result
        self.error = error

    def write(self, chunk):
        if self.error is not None:
            raise self.error
        if self.result is _UNSET_WRITE_RESULT:
            return len(chunk)
        return self.result


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


class StaticTransport:
    def __init__(self, body=b"%PDF-1.7\n"):
        self.body = body
        self.calls = []

    def download(self, request, target):
        self.calls.append(request)
        target.write(self.body)
        return DownloadResult(
            final_url=request.url,
            size_bytes=len(self.body),
            sha256=hashlib.sha256(self.body).hexdigest(),
            content_type=request.expected_media_type,
            status=200,
        )


class ErrorTransport:
    def __init__(self, error):
        self.error = error

    def download(self, request, target):
        raise self.error


class SequenceTransport:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def download(self, request, target):
        self.calls.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        target.write(outcome)
        return DownloadResult(
            final_url=request.url,
            size_bytes=len(outcome),
            sha256=hashlib.sha256(outcome).hexdigest(),
            content_type=request.expected_media_type,
            status=200,
        )


class RecordingArtifactStore:
    def __init__(self):
        self.delegate = storage_module.AtomicArtifactStore()
        self.events = []

    def publish_blob(self, target, media_type, producer):
        result = self.delegate.publish_blob(target, media_type, producer)
        self.events.append(("blob", target))
        return result

    def publish_manifest(self, target, document):
        self.events.append(("manifest", target))
        return self.delegate.publish_manifest(target, document)


class FailingManifestStore:
    def __init__(self):
        self.delegate = storage_module.AtomicArtifactStore()

    def publish_blob(self, target, media_type, producer):
        return self.delegate.publish_blob(target, media_type, producer)

    def publish_manifest(self, target, document):
        raise ContractError("manifest 寫入失敗")


class RawFailingStore:
    def __init__(self, *, blob_errors=(), manifest_error=None):
        self.delegate = storage_module.AtomicArtifactStore()
        self.blob_errors = list(blob_errors)
        self.manifest_error = manifest_error

    def publish_blob(self, target, media_type, producer):
        if self.blob_errors:
            error = self.blob_errors.pop(0)
            if error is not None:
                raise error
        return self.delegate.publish_blob(target, media_type, producer)

    def publish_manifest(self, target, document):
        if self.manifest_error is not None:
            raise self.manifest_error
        return self.delegate.publish_manifest(target, document)


def _make_pending_lifecycle_repo(root, *, create_source=True):
    root = Path(root)
    config = root / "config"
    config.mkdir()
    (config / "source-hosts.json").write_text(
        json.dumps({"schema_version": 1, "hosts": ["pubs.shure.com"]}),
        encoding="utf-8",
    )
    (config / "source-url-redactions.json").write_text(
        json.dumps({"schema_version": 1, "entries": []}),
        encoding="utf-8",
    )
    document = _load_fixture("pending-pdf.json")
    mic_dir = _make_mic_dir(root, document)
    if create_source:
        (mic_dir / "source").mkdir()
    manifest = mic_dir / "source-manifest.json"
    manifest.write_text(json.dumps(document), encoding="utf-8")
    return mic_dir, manifest


def _make_available_svg_repo(root, *, body=b'<svg xmlns="http://www.w3.org/2000/svg"/>', present=False):
    mic_dir, manifest = _make_pending_lifecycle_repo(root)
    document = json.loads(manifest.read_text(encoding="utf-8"))
    artifact = document["artifacts"][0]
    artifact.update(
        {
            "availability": "available",
            "local_path": "source/original.svg",
            "media_type": "image/svg+xml",
            "size_bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
            "retrieved_at": "2026-07-15T12:00:00Z",
        }
    )
    manifest.write_text(json.dumps(document), encoding="utf-8")
    target = mic_dir / "source" / "original.svg"
    if present:
        target.write_bytes(body)
    return mic_dir, manifest, target, body


def _make_unavailable_repo(root):
    mic_dir, manifest = _make_pending_lifecycle_repo(root)
    document = json.loads(manifest.read_text(encoding="utf-8"))
    artifact = document["artifacts"][0]
    artifact["availability"] = "unavailable"
    artifact["checked_at"] = "2026-07-15T12:00:00Z"
    artifact["reason"] = "遠端 HTTP 404"
    artifact.pop("local_path")
    artifact.pop("media_type")
    manifest.write_text(json.dumps(document), encoding="utf-8")
    return mic_dir, manifest


class InterfaceExistenceTests(unittest.TestCase):
    def test_production_modules_exist(self):
        library = Path(__file__).parent / "source_assets_lib"
        expected = [
            library / "__init__.py",
            library / "commands.py",
            library / "model.py",
            library / "network.py",
            library / "storage.py",
            Path(__file__).parent / "source_assets.py",
        ]
        missing_paths = [str(path) for path in expected if not path.is_file()]
        self.assertEqual(missing_paths, [])


class LifecycleCommandTests(unittest.TestCase):
    def test_selector_mic_rejects_path_traversal_and_non_slug_values(self):
        accepted = []
        for value in ("../outside", "shure/sm58", "", "Shure-SM58", "."):
            try:
                Selector.mic(value)
            except ContractError:
                continue
            accepted.append(value)

        self.assertEqual(accepted, [])

    def test_bootstrap_changes_pending_pdf_to_available(self):
        with tempfile.TemporaryDirectory() as td:
            mic_dir, manifest = _make_pending_lifecycle_repo(td)
            transport = StaticTransport()
            context = CommandContext(
                transport=transport,
                store=storage_module.AtomicArtifactStore(),
                now=lambda: datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
            )

            summary = bootstrap(Path(td), Selector.all(), True, context)

            document = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(summary.exit_code, 0)
            self.assertEqual(document["artifacts"][0]["availability"], "available")
            self.assertEqual((mic_dir / "source" / "original.pdf").read_bytes(), transport.body)

    def test_bootstrap_publishes_artifact_before_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            mic_dir, manifest = _make_pending_lifecycle_repo(td)
            store = RecordingArtifactStore()
            context = CommandContext(
                transport=StaticTransport(),
                store=store,
                now=lambda: datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
            )

            summary = bootstrap(Path(td), Selector.all(), True, context)

            self.assertEqual(summary.exit_code, 0)
            self.assertEqual([event[0] for event in store.events], ["blob", "manifest"])
            self.assertEqual(store.events[0][1].parent, mic_dir / "source")
            self.assertEqual(store.events[1], ("manifest", manifest))
            self.assertTrue((mic_dir / "source" / "original.pdf").is_file())

    def test_bootstrap_classifies_http_not_found_as_unavailable(self):
        observed = []
        for status in (404, 410):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as td:
                _mic_dir, manifest = _make_pending_lifecycle_repo(td)
                context = CommandContext(
                    transport=ErrorTransport(
                        RemoteHTTPError(status, "https://pubs.shure.com/manual.pdf")
                    ),
                    store=storage_module.AtomicArtifactStore(),
                    now=lambda: datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
                )

                try:
                    summary = bootstrap(Path(td), Selector.all(), True, context)
                except SourceAssetError as error:
                    observed.append(type(error).__name__)
                else:
                    document = json.loads(manifest.read_text(encoding="utf-8"))
                    artifact = document["artifacts"][0]
                    observed.append(
                        (
                            summary,
                            artifact["availability"],
                            artifact.get("checked_at"),
                            "local_path" in artifact,
                        )
                    )

        self.assertEqual(
            observed,
            [
                (OperationSummary(0, 1, 0, 0), "unavailable", "2026-07-15T12:00:00Z", False),
                (OperationSummary(0, 1, 0, 0), "unavailable", "2026-07-15T12:00:00Z", False),
            ],
        )

    def test_bootstrap_classifies_unavailable_manifest_write_failure(self):
        with tempfile.TemporaryDirectory() as td:
            _mic_dir, manifest = _make_pending_lifecycle_repo(td)
            original = manifest.read_bytes()
            context = CommandContext(
                transport=ErrorTransport(
                    RemoteHTTPError(404, "https://pubs.shure.com/manual.pdf")
                ),
                store=FailingManifestStore(),
                now=lambda: datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
            )
            try:
                summary = bootstrap(Path(td), Selector.all(), True, context)
            except SourceAssetError as error:
                summary = type(error).__name__

            self.assertEqual(summary, OperationSummary(0, 0, 1, 1))
            self.assertEqual(manifest.read_bytes(), original)

    def test_bootstrap_keeps_transient_remote_failures_pending(self):
        failures = [
            RemoteHTTPError(status, "https://pubs.shure.com/manual.pdf")
            for status in (401, 403, 429, 500, 503)
        ] + [RemoteError("遠端連線逾時")]
        observed = []
        for failure in failures:
            with self.subTest(failure=str(failure)), tempfile.TemporaryDirectory() as td:
                _mic_dir, manifest = _make_pending_lifecycle_repo(td)
                original = manifest.read_bytes()
                context = CommandContext(
                    transport=ErrorTransport(failure),
                    store=storage_module.AtomicArtifactStore(),
                    now=lambda: datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
                )
                try:
                    summary = bootstrap(Path(td), Selector.all(), True, context)
                except SourceAssetError as error:
                    observed.append(type(error).__name__)
                else:
                    observed.append((summary, manifest.read_bytes() == original))

        self.assertEqual(
            observed,
            [(OperationSummary(0, 0, 1, 2), True)] * len(failures),
        )

    def test_bootstrap_requires_accept_new_without_contacting_remote(self):
        with tempfile.TemporaryDirectory() as td:
            _mic_dir, manifest = _make_pending_lifecycle_repo(td)
            original = manifest.read_bytes()
            transport = StaticTransport()
            context = CommandContext(
                transport=transport,
                store=storage_module.AtomicArtifactStore(),
                now=lambda: datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
            )

            summary = bootstrap(Path(td), Selector.all(), False, context)

            self.assertEqual(summary, OperationSummary(0, 0, 1, 1))
            self.assertEqual(transport.calls, [])
            self.assertEqual(manifest.read_bytes(), original)

    def test_bootstrap_creates_missing_source_directory_before_atomic_publish(self):
        with tempfile.TemporaryDirectory() as td:
            mic_dir, _manifest = _make_pending_lifecycle_repo(td, create_source=False)
            transport = StaticTransport()
            context = CommandContext(
                transport=transport,
                store=storage_module.AtomicArtifactStore(),
                now=lambda: datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
            )
            try:
                summary = bootstrap(Path(td), Selector.all(), True, context)
            except OSError as error:
                observed = type(error).__name__
            else:
                observed = (
                    summary,
                    (mic_dir / "source").is_dir(),
                    (mic_dir / "source" / "original.pdf").read_bytes(),
                )

            self.assertEqual(
                observed,
                (OperationSummary(1, 0, 0, 0), True, transport.body),
            )

    def test_bootstrap_rejects_unsafe_source_directory_without_network(self):
        observed = []
        for kind in ("file", "symlink"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as td:
                mic_dir, manifest = _make_pending_lifecycle_repo(td, create_source=False)
                source_dir = mic_dir / "source"
                if kind == "file":
                    source_dir.write_text("not a directory", encoding="utf-8")
                else:
                    outside = Path(td) / "outside"
                    outside.mkdir()
                    source_dir.symlink_to(outside, target_is_directory=True)
                original = manifest.read_bytes()
                transport = StaticTransport()
                context = CommandContext(
                    transport=transport,
                    store=storage_module.AtomicArtifactStore(Path(td)),
                    now=lambda: datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
                )
                try:
                    summary = bootstrap(Path(td), Selector.all(), True, context)
                except SourceAssetError as error:
                    observed.append(type(error).__name__)
                else:
                    observed.append(
                        (summary, transport.calls, manifest.read_bytes() == original)
                    )

        self.assertEqual(
            observed,
            [(OperationSummary(0, 0, 1, 1), [], True)] * 2,
        )

    def test_bootstrap_rejects_symlinked_microphone_directory_without_network(self):
        with tempfile.TemporaryDirectory() as td:
            mic_dir, _manifest = _make_pending_lifecycle_repo(td)
            outside_parent = Path(td) / "outside"
            outside_parent.mkdir()
            outside_mic = outside_parent / mic_dir.name
            mic_dir.rename(outside_mic)
            mic_dir.symlink_to(outside_mic, target_is_directory=True)
            transport = StaticTransport()
            context = CommandContext(
                transport=transport,
                store=storage_module.AtomicArtifactStore(),
                now=lambda: datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
            )

            summary = bootstrap(Path(td), Selector.all(), True, context)

            self.assertEqual(summary, OperationSummary(0, 0, 1, 1))
            self.assertEqual(transport.calls, [])
            self.assertFalse((outside_mic / "source" / "original.pdf").exists())

    def test_bootstrap_rejects_source_and_microphone_symlink_swap_without_external_writes(self):
        for swapped_component in ("source", "microphone"):
            with self.subTest(swapped_component=swapped_component), tempfile.TemporaryDirectory() as td:
                mic_dir, manifest = _make_pending_lifecycle_repo(td)
                original_manifest = manifest.read_bytes()
                outside = Path(td) / "outside"
                outside.mkdir()
                parked = Path(td) / f"parked-{swapped_component}"
                if swapped_component == "source":
                    outside_target = outside
                else:
                    outside_target = outside / mic_dir.name
                    (outside_target / "source").mkdir(parents=True)
                transport = StaticTransport()
                context = CommandContext(
                    transport=transport,
                    store=storage_module.AtomicArtifactStore(),
                    now=lambda: datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
                )
                real_open = os.open
                swapped = False

                def swapping_open(path, flags, mode=0o777, *, dir_fd=None):
                    nonlocal swapped
                    path_text = os.fspath(path)
                    is_current_temp_create = (
                        dir_fd is None
                        and Path(path_text).parent == mic_dir / "source"
                        and Path(path_text).name.startswith(".original.pdf.")
                    )
                    is_hardened_component_open = (
                        dir_fd is not None
                        and (
                            (swapped_component == "source" and path_text == "source")
                            or (
                                swapped_component == "microphone"
                                and path_text == mic_dir.name
                            )
                        )
                    )
                    if not swapped and (is_current_temp_create or is_hardened_component_open):
                        swapped = True
                        if swapped_component == "source":
                            (mic_dir / "source").rename(parked)
                            (mic_dir / "source").symlink_to(
                                outside_target,
                                target_is_directory=True,
                            )
                        else:
                            mic_dir.rename(parked)
                            mic_dir.symlink_to(outside_target, target_is_directory=True)
                    if dir_fd is None:
                        return real_open(path, flags, mode)
                    return real_open(path, flags, mode, dir_fd=dir_fd)

                with patch.object(storage_module.os, "open", swapping_open):
                    try:
                        summary = bootstrap(Path(td), Selector.all(), True, context)
                    except OSError as error:
                        summary = type(error).__name__

                self.assertEqual(summary, OperationSummary(0, 0, 1, 1))
                self.assertTrue(swapped)
                self.assertEqual(transport.calls, [])
                self.assertEqual(
                    [path for path in outside.rglob("*") if path.is_file()],
                    [],
                )
                self.assertEqual(
                    (parked / "source-manifest.json").read_bytes()
                    if swapped_component == "microphone"
                    else manifest.read_bytes(),
                    original_manifest,
                )

    def test_bootstrap_classifies_raw_local_store_errors_and_continues_batch(self):
        with tempfile.TemporaryDirectory() as td:
            _mic_dir, _manifest = _make_pending_lifecycle_repo(td)
            data_dir = Path(td) / "data"
            template = data_dir / "shure-sm58"
            for slug in ("a-local-error", "b-success"):
                destination = data_dir / slug
                shutil.copytree(template, destination)
                manifest = destination / "source-manifest.json"
                document = json.loads(manifest.read_text(encoding="utf-8"))
                document["mic_slug"] = slug
                manifest.write_text(json.dumps(document), encoding="utf-8")
            shutil.rmtree(template)
            transport = StaticTransport()
            context = CommandContext(
                transport=transport,
                store=RawFailingStore(blob_errors=[PermissionError("denied"), None]),
                now=lambda: datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
            )

            try:
                summary = bootstrap(Path(td), Selector.all(), True, context)
            except OSError as error:
                summary = type(error).__name__

            self.assertEqual(summary, OperationSummary(1, 0, 1, 1))
            self.assertEqual(len(transport.calls), 1)
            states = {
                slug: json.loads(
                    (data_dir / slug / "source-manifest.json").read_text(encoding="utf-8")
                )["artifacts"][0]["availability"]
                for slug in ("a-local-error", "b-success")
            }
            self.assertEqual(
                states,
                {"a-local-error": "pending", "b-success": "available"},
            )

    def test_bootstrap_all_counts_unreadable_microphone_and_continues(self):
        with tempfile.TemporaryDirectory() as td:
            _mic_dir, _manifest = _make_pending_lifecycle_repo(td)
            data_dir = Path(td) / "data"
            template = data_dir / "shure-sm58"
            readable = data_dir / "a-readable"
            unreadable = data_dir / "z-unreadable"
            for destination in (readable, unreadable):
                shutil.copytree(template, destination)
                manifest = destination / "source-manifest.json"
                document = json.loads(manifest.read_text(encoding="utf-8"))
                document["mic_slug"] = destination.name
                manifest.write_text(json.dumps(document), encoding="utf-8")
            shutil.rmtree(template)
            transport = StaticTransport()
            context = CommandContext(
                transport=transport,
                store=storage_module.AtomicArtifactStore(),
                now=lambda: datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
            )

            unreadable.chmod(0)
            try:
                summary = bootstrap(Path(td), Selector.all(), True, context)
            finally:
                unreadable.chmod(0o700)

            self.assertEqual(summary, OperationSummary(1, 0, 1, 1))
            self.assertEqual(len(transport.calls), 1)

    def test_bootstrap_classifies_raw_manifest_publish_error(self):
        with tempfile.TemporaryDirectory() as td:
            mic_dir, manifest = _make_pending_lifecycle_repo(td)
            original_manifest = manifest.read_bytes()
            context = CommandContext(
                transport=StaticTransport(),
                store=RawFailingStore(manifest_error=PermissionError("denied")),
                now=lambda: datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
            )

            try:
                summary = bootstrap(Path(td), Selector.all(), True, context)
            except OSError as error:
                summary = type(error).__name__

            self.assertEqual(summary, OperationSummary(0, 0, 1, 1))
            self.assertEqual(manifest.read_bytes(), original_manifest)
            self.assertEqual(
                (mic_dir / "source" / "original.pdf").read_bytes(),
                b"%PDF-1.7\n",
            )

    def test_bootstrap_classifies_source_directory_io_failure_without_network(self):
        with tempfile.TemporaryDirectory() as td:
            _mic_dir, manifest = _make_pending_lifecycle_repo(td)
            original = manifest.read_bytes()
            transport = StaticTransport()
            context = CommandContext(
                transport=transport,
                store=storage_module.AtomicArtifactStore(),
                now=lambda: datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
            )
            with patch.object(
                storage_module.os,
                "mkdir",
                side_effect=OSError("permission denied"),
            ):
                try:
                    summary = bootstrap(Path(td), Selector.all(), True, context)
                except OSError as error:
                    summary = type(error).__name__

            self.assertEqual(summary, OperationSummary(0, 0, 1, 1))
            self.assertEqual(transport.calls, [])
            self.assertEqual(manifest.read_bytes(), original)

    def test_bootstrap_rejects_naive_clock_before_download(self):
        with tempfile.TemporaryDirectory() as td:
            _mic_dir, manifest = _make_pending_lifecycle_repo(td)
            original = manifest.read_bytes()
            transport = StaticTransport()
            context = CommandContext(
                transport=transport,
                store=storage_module.AtomicArtifactStore(),
                now=lambda: datetime(2026, 7, 15, 12, 0),
            )

            summary = bootstrap(Path(td), Selector.all(), True, context)

            self.assertEqual(summary, OperationSummary(0, 0, 1, 1))
            self.assertEqual(transport.calls, [])
            self.assertEqual(manifest.read_bytes(), original)

    def test_bootstrap_fails_closed_on_invalid_policy_config(self):
        observed = []
        kinds = (
            "missing",
            "malformed",
            "wrong-version",
            "bool-version",
            "invalid-hosts",
            "unknown-host-field",
            "invalid-redactions",
            "unknown-redaction-field",
            "duplicate-redaction-id",
        )
        for kind in kinds:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as td:
                _mic_dir, manifest = _make_pending_lifecycle_repo(td)
                original = manifest.read_bytes()
                hosts_path = Path(td) / "config" / "source-hosts.json"
                redactions_path = Path(td) / "config" / "source-url-redactions.json"
                if kind == "missing":
                    hosts_path.unlink()
                elif kind == "malformed":
                    hosts_path.write_text("{", encoding="utf-8")
                elif kind == "wrong-version":
                    hosts_path.write_text(
                        json.dumps({"schema_version": 2, "hosts": ["pubs.shure.com"]}),
                        encoding="utf-8",
                    )
                elif kind == "bool-version":
                    hosts_path.write_text(
                        json.dumps({"schema_version": True, "hosts": ["pubs.shure.com"]}),
                        encoding="utf-8",
                    )
                elif kind == "invalid-hosts":
                    hosts_path.write_text(
                        json.dumps({"schema_version": 1, "hosts": "pubs.shure.com"}),
                        encoding="utf-8",
                    )
                elif kind == "unknown-host-field":
                    hosts_path.write_text(
                        json.dumps(
                            {
                                "schema_version": 1,
                                "hosts": ["pubs.shure.com"],
                                "unknown": True,
                            }
                        ),
                        encoding="utf-8",
                    )
                elif kind == "invalid-redactions":
                    redactions_path.write_text(
                        json.dumps({"schema_version": 1, "entries": [{"id": "incomplete"}]}),
                        encoding="utf-8",
                    )
                else:
                    record = _load_fixture("redaction-no-stable-endpoint.json")["entries"][0]
                    record = {**record, "mic_slug": "shure-sm58"}
                    if kind == "unknown-redaction-field":
                        record["raw_url"] = "https://example.invalid/?token=secret"
                        entries = [record]
                    else:
                        entries = [record, dict(record)]
                    redactions_path.write_text(
                        json.dumps({"schema_version": 1, "entries": entries}),
                        encoding="utf-8",
                    )
                transport = StaticTransport()
                context = CommandContext(
                    transport=transport,
                    store=storage_module.AtomicArtifactStore(),
                    now=lambda: datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
                )
                try:
                    summary = bootstrap(Path(td), Selector.all(), True, context)
                except BaseException as error:
                    observed.append(type(error).__name__)
                else:
                    observed.append(
                        (summary, transport.calls, manifest.read_bytes() == original)
                    )

        self.assertEqual(
            observed,
            [(OperationSummary(0, 0, 1, 1), [], True)] * len(kinds),
        )

    def test_bootstrap_continues_batch_and_uses_numeric_max_exit_code(self):
        with tempfile.TemporaryDirectory() as td:
            _mic_dir, _manifest = _make_pending_lifecycle_repo(td)
            data_dir = Path(td) / "data"
            template = data_dir / "shure-sm58"
            for slug in ("a-remote", "b-success", "c-unsafe"):
                destination = data_dir / slug
                shutil.copytree(template, destination)
                manifest = destination / "source-manifest.json"
                document = json.loads(manifest.read_text(encoding="utf-8"))
                document["mic_slug"] = slug
                manifest.write_text(json.dumps(document), encoding="utf-8")
            shutil.rmtree(template)
            shutil.rmtree(data_dir / "c-unsafe" / "source")
            (data_dir / "c-unsafe" / "source").write_text("unsafe", encoding="utf-8")
            body = b"%PDF-1.7\nsuccess\n"
            transport = SequenceTransport([RemoteError("遠端連線失敗"), body])
            context = CommandContext(
                transport=transport,
                store=storage_module.AtomicArtifactStore(),
                now=lambda: datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
            )

            summary = bootstrap(Path(td), Selector.all(), True, context)

            self.assertEqual(summary, OperationSummary(1, 0, 2, 2))
            self.assertEqual(len(transport.calls), 2)
            states = {
                slug: json.loads(
                    (data_dir / slug / "source-manifest.json").read_text(encoding="utf-8")
                )["artifacts"][0]["availability"]
                for slug in ("a-remote", "b-success", "c-unsafe")
            }
            self.assertEqual(
                states,
                {"a-remote": "pending", "b-success": "available", "c-unsafe": "pending"},
            )

    def test_bootstrap_preserves_mismatched_orphan_and_pending_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            mic_dir, manifest = _make_pending_lifecycle_repo(td)
            orphan = mic_dir / "source" / "original.pdf"
            orphan_body = b"%PDF-1.7\nexisting orphan\n"
            orphan.write_bytes(orphan_body)
            original_manifest = manifest.read_bytes()
            transport = StaticTransport(b"%PDF-1.7\ndifferent remote\n")
            context = CommandContext(
                transport=transport,
                store=storage_module.AtomicArtifactStore(),
                now=lambda: datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
            )

            summary = bootstrap(Path(td), Selector.all(), True, context)

            self.assertEqual(summary, OperationSummary(0, 0, 1, 2))
            self.assertEqual(orphan.read_bytes(), orphan_body)
            self.assertEqual(manifest.read_bytes(), original_manifest)
            self.assertEqual(len(transport.calls), 1)
            self.assertEqual(list(orphan.parent.glob(".*.tmp")), [])

    def test_bootstrap_does_not_classify_orphan_as_unavailable_on_remote_404(self):
        with tempfile.TemporaryDirectory() as td:
            mic_dir, manifest = _make_pending_lifecycle_repo(td)
            orphan = mic_dir / "source" / "original.pdf"
            orphan_body = b"%PDF-1.7\nexisting orphan\n"
            orphan.write_bytes(orphan_body)
            original_manifest = manifest.read_bytes()
            context = CommandContext(
                transport=ErrorTransport(
                    RemoteHTTPError(404, "https://pubs.shure.com/manual.pdf")
                ),
                store=storage_module.AtomicArtifactStore(),
                now=lambda: datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
            )

            summary = bootstrap(Path(td), Selector.all(), True, context)

            self.assertEqual(summary, OperationSummary(0, 0, 1, 2))
            self.assertEqual(orphan.read_bytes(), orphan_body)
            self.assertEqual(manifest.read_bytes(), original_manifest)

    def test_bootstrap_adopts_only_an_exact_redownloaded_orphan(self):
        with tempfile.TemporaryDirectory() as td:
            mic_dir, manifest = _make_pending_lifecycle_repo(td)
            orphan = mic_dir / "source" / "original.pdf"
            orphan_body = b"%PDF-1.7\nexact orphan\n"
            orphan.write_bytes(orphan_body)
            transport = StaticTransport(orphan_body)
            context = CommandContext(
                transport=transport,
                store=storage_module.AtomicArtifactStore(),
                now=lambda: datetime(2026, 7, 15, 20, 0, tzinfo=timezone.utc),
            )

            summary = bootstrap(Path(td), Selector.all(), True, context)
            artifact = json.loads(manifest.read_text(encoding="utf-8"))["artifacts"][0]

            self.assertEqual(summary, OperationSummary(1, 0, 0, 0))
            self.assertEqual(orphan.read_bytes(), orphan_body)
            self.assertEqual(artifact["availability"], "available")
            self.assertEqual(artifact["size_bytes"], len(orphan_body))
            self.assertEqual(artifact["sha256"], hashlib.sha256(orphan_body).hexdigest())
            self.assertEqual(artifact["retrieved_at"], "2026-07-15T20:00:00Z")

    def test_bootstrap_rerun_after_manifest_directory_fsync_does_not_redownload(self):
        with tempfile.TemporaryDirectory() as td:
            _mic_dir, manifest = _make_pending_lifecycle_repo(td)
            transport = StaticTransport()
            context = CommandContext(
                transport=transport,
                store=storage_module.AtomicArtifactStore(),
                now=lambda: datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
            )
            with patch.object(
                storage_module,
                "_fsync_directory",
                side_effect=[
                    None,
                    None,
                    OSError("manifest directory fsync failed"),
                ],
            ):
                try:
                    first = bootstrap(Path(td), Selector.all(), True, context)
                except SourceAssetError as error:
                    first = type(error).__name__

            second = bootstrap(Path(td), Selector.all(), True, context)
            artifact = json.loads(manifest.read_text(encoding="utf-8"))["artifacts"][0]

            self.assertEqual(first, OperationSummary(0, 0, 1, 1))
            self.assertEqual(second, OperationSummary(1, 0, 0, 0))
            self.assertEqual(len(transport.calls), 1)
            self.assertEqual(artifact["availability"], "available")

    def test_bootstrap_keeps_committed_manifest_state_when_later_artifact_continues(self):
        with tempfile.TemporaryDirectory() as td:
            _mic_dir, manifest = _make_pending_lifecycle_repo(td)
            document = json.loads(manifest.read_text(encoding="utf-8"))
            second = {
                **document["artifacts"][0],
                "id": "official-manual-two",
                "source_url": "https://pubs.shure.com/manual-two.pdf",
                "local_path": "source/original-two.pdf",
            }
            document["artifacts"].append(second)
            manifest.write_text(json.dumps(document), encoding="utf-8")
            first_body = b"%PDF-1.7\nfirst\n"
            second_body = b"%PDF-1.7\nsecond\n"
            transport = SequenceTransport([first_body, second_body])
            context = CommandContext(
                transport=transport,
                store=storage_module.AtomicArtifactStore(),
                now=lambda: datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
            )

            with patch.object(
                storage_module,
                "_fsync_directory",
                side_effect=[
                    None,
                    None,
                    OSError("first manifest directory fsync failed"),
                    None,
                    None,
                    None,
                ],
            ):
                summary = bootstrap(Path(td), Selector.all(), True, context)

            states = {
                artifact["id"]: artifact["availability"]
                for artifact in json.loads(
                    manifest.read_text(encoding="utf-8")
                )["artifacts"]
            }
            self.assertEqual(summary, OperationSummary(1, 0, 1, 1))
            self.assertEqual(len(transport.calls), 2)
            self.assertEqual(
                states,
                {
                    "official-specification": "available",
                    "official-manual-two": "available",
                },
            )

    def test_bootstrap_stops_manifest_when_committed_state_cannot_be_recovered(self):
        with tempfile.TemporaryDirectory() as td:
            _mic_dir, manifest = _make_pending_lifecycle_repo(td)
            document = json.loads(manifest.read_text(encoding="utf-8"))
            second = {
                **document["artifacts"][0],
                "id": "official-manual-two",
                "source_url": "https://pubs.shure.com/manual-two.pdf",
                "local_path": "source/original-two.pdf",
            }
            document["artifacts"].append(second)
            manifest.write_text(json.dumps(document), encoding="utf-8")
            transport = SequenceTransport(
                [b"%PDF-1.7\nfirst\n", b"%PDF-1.7\nsecond\n"]
            )
            context = CommandContext(
                transport=transport,
                store=storage_module.AtomicArtifactStore(),
                now=lambda: datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
            )
            real_load_manifest = commands_module.load_manifest
            load_calls = 0

            def fail_recovery_load(*args, **kwargs):
                nonlocal load_calls
                load_calls += 1
                if load_calls == 2:
                    raise PermissionError("recovery denied")
                return real_load_manifest(*args, **kwargs)

            with (
                patch.object(
                    storage_module,
                    "_fsync_directory",
                    side_effect=[
                        None,
                        None,
                        OSError("first manifest directory fsync failed"),
                        None,
                        None,
                        None,
                    ],
                ),
                patch.object(
                    commands_module,
                    "load_manifest",
                    side_effect=fail_recovery_load,
                ),
            ):
                summary = bootstrap(Path(td), Selector.all(), True, context)

            states = {
                artifact["id"]: artifact["availability"]
                for artifact in json.loads(
                    manifest.read_text(encoding="utf-8")
                )["artifacts"]
            }
            self.assertEqual(summary, OperationSummary(0, 0, 1, 1))
            self.assertEqual(len(transport.calls), 1)
            self.assertEqual(
                states,
                {
                    "official-specification": "available",
                    "official-manual-two": "pending",
                },
            )

    def test_bootstrap_does_not_mark_unavailable_when_orphan_appears_before_failure_check(self):
        with tempfile.TemporaryDirectory() as td:
            mic_dir, manifest = _make_pending_lifecycle_repo(td)
            orphan = mic_dir / "source" / "original.pdf"
            orphan_body = b"%PDF-1.7\nracing orphan\n"
            original_manifest = manifest.read_bytes()
            real_local_file_exists = commands_module.local_file_exists
            existence_checks = 0

            def create_orphan_on_failure_check(path, root):
                nonlocal existence_checks
                existence_checks += 1
                if existence_checks == 1:
                    orphan.write_bytes(orphan_body)
                return real_local_file_exists(path, root)

            context = CommandContext(
                transport=ErrorTransport(
                    RemoteHTTPError(404, "https://pubs.shure.com/manual.pdf")
                ),
                store=storage_module.AtomicArtifactStore(),
                now=lambda: datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
            )

            with patch.object(
                commands_module,
                "local_file_exists",
                side_effect=create_orphan_on_failure_check,
            ):
                summary = bootstrap(Path(td), Selector.all(), True, context)

            self.assertEqual(existence_checks, 1)
            self.assertEqual(summary, OperationSummary(0, 0, 1, 2))
            self.assertEqual(orphan.read_bytes(), orphan_body)
            self.assertEqual(manifest.read_bytes(), original_manifest)

    def test_bootstrap_never_overwrites_orphan_created_during_download(self):
        with tempfile.TemporaryDirectory() as td:
            mic_dir, manifest = _make_pending_lifecycle_repo(td)
            orphan = mic_dir / "source" / "original.pdf"
            orphan_body = b"%PDF-1.7\nlate orphan\n"
            remote_body = b"%PDF-1.7\nremote body\n"
            original_manifest = manifest.read_bytes()

            class OrphanCreatingTransport:
                def download(self, request, target):
                    orphan.write_bytes(orphan_body)
                    target.write(remote_body)
                    return DownloadResult(
                        final_url=request.url,
                        size_bytes=len(remote_body),
                        sha256=hashlib.sha256(remote_body).hexdigest(),
                        content_type=request.expected_media_type,
                        status=200,
                    )

            context = CommandContext(
                transport=OrphanCreatingTransport(),
                store=storage_module.AtomicArtifactStore(),
                now=lambda: datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
            )

            summary = bootstrap(Path(td), Selector.all(), True, context)

            self.assertEqual(summary, OperationSummary(0, 0, 1, 2))
            self.assertEqual(orphan.read_bytes(), orphan_body)
            self.assertEqual(manifest.read_bytes(), original_manifest)

    def test_bootstrap_404_keeps_orphan_created_during_download_pending(self):
        with tempfile.TemporaryDirectory() as td:
            mic_dir, manifest = _make_pending_lifecycle_repo(td)
            orphan = mic_dir / "source" / "original.pdf"
            orphan_body = b"%PDF-1.7\nlate orphan\n"
            original_manifest = manifest.read_bytes()

            class OrphanCreating404Transport:
                def download(self, request, target):
                    orphan.write_bytes(orphan_body)
                    raise RemoteHTTPError(404, request.url)

            context = CommandContext(
                transport=OrphanCreating404Transport(),
                store=storage_module.AtomicArtifactStore(),
                now=lambda: datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
            )

            summary = bootstrap(Path(td), Selector.all(), True, context)

            self.assertEqual(summary, OperationSummary(0, 0, 1, 2))
            self.assertEqual(orphan.read_bytes(), orphan_body)
            self.assertEqual(manifest.read_bytes(), original_manifest)

    def test_fetch_restores_missing_available_original_from_pinned_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            _mic_dir, manifest, target, body = _make_available_svg_repo(td)
            original_manifest = manifest.read_bytes()
            transport = StaticTransport(body)
            context = CommandContext(
                transport=transport,
                store=storage_module.AtomicArtifactStore(),
                now=lambda: datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
            )

            summary = fetch(Path(td), Selector.all(), context)

            self.assertEqual(summary, OperationSummary(1, 0, 0, 0))
            self.assertEqual(target.read_bytes(), body)
            self.assertEqual(manifest.read_bytes(), original_manifest)
            self.assertEqual(len(transport.calls), 1)
            request = transport.calls[0]
            self.assertEqual(request.expected_size, len(body))
            self.assertEqual(request.max_size, len(body))
            self.assertEqual(request.expected_sha256, hashlib.sha256(body).hexdigest())

    def test_fetch_classifies_raw_local_store_file_not_found_error(self):
        with tempfile.TemporaryDirectory() as td:
            _mic_dir, manifest, target, body = _make_available_svg_repo(td)
            original_manifest = manifest.read_bytes()
            transport = StaticTransport(body)
            context = CommandContext(
                transport=transport,
                store=RawFailingStore(blob_errors=[FileNotFoundError("gone")]),
                now=lambda: datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
            )

            try:
                summary = fetch(Path(td), Selector.all(), context)
            except OSError as error:
                summary = type(error).__name__

            self.assertEqual(summary, OperationSummary(0, 0, 1, 1))
            self.assertFalse(target.exists())
            self.assertEqual(manifest.read_bytes(), original_manifest)
            self.assertEqual(transport.calls, [])

    def test_fetch_never_contacts_remote_or_overwrites_an_existing_target(self):
        observed = []
        for state in ("exact", "mismatch"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as td:
                _mic_dir, manifest, target, body = _make_available_svg_repo(
                    td,
                    present=True,
                )
                if state == "mismatch":
                    target.write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg"><text>x</text></svg>')
                original_target = target.read_bytes()
                original_manifest = manifest.read_bytes()
                transport = StaticTransport(body)
                context = CommandContext(
                    transport=transport,
                    store=storage_module.AtomicArtifactStore(),
                    now=lambda: datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
                )

                summary = fetch(Path(td), Selector.all(), context)
                observed.append(
                    (
                        summary,
                        transport.calls,
                        target.read_bytes() == original_target,
                        manifest.read_bytes() == original_manifest,
                    )
                )

        self.assertEqual(
            observed,
            [
                (OperationSummary(1, 0, 0, 0), [], True, True),
                (OperationSummary(0, 0, 1, 2), [], True, True),
            ],
        )

    def test_verify_is_offline_and_read_only_for_valid_local_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            mic_dir, manifest, target, body = _make_available_svg_repo(
                td,
                present=True,
            )
            before = {
                path.relative_to(td): (path.stat().st_mtime_ns, path.read_bytes())
                for path in Path(td).rglob("*")
                if path.is_file()
            }

            with (
                patch.object(
                    network_module,
                    "resolve_public_addresses",
                    side_effect=AssertionError("verify 不得解析 DNS"),
                ),
                patch.object(
                    network_module.socket,
                    "getaddrinfo",
                    side_effect=AssertionError("verify 不得建立網路連線"),
                ),
            ):
                summary = verify(Path(td), Selector.all())

            after = {
                path.relative_to(td): (path.stat().st_mtime_ns, path.read_bytes())
                for path in Path(td).rglob("*")
                if path.is_file()
            }
            self.assertEqual(summary, OperationSummary(1, 0, 0, 0))
            self.assertEqual(after, before)
            self.assertEqual(target.read_bytes(), body)
            self.assertEqual(manifest.parent, mic_dir)

    def test_verify_classifies_local_integrity_failures_and_unavailable_entries(self):
        observed = []
        for state in ("missing", "hash-mismatch", "signature-mismatch"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as td:
                _mic_dir, _manifest, target, _body = _make_available_svg_repo(
                    td,
                    present=state != "missing",
                )
                if state == "hash-mismatch":
                    target.write_bytes(
                        b'<svg xmlns="http://www.w3.org/2000/svg"><text>x</text></svg>'
                    )
                elif state == "signature-mismatch":
                    target.write_bytes(b"not an svg")
                observed.append(verify(Path(td), Selector.all()))

        with tempfile.TemporaryDirectory() as td:
            _make_unavailable_repo(td)
            unavailable_summary = verify(Path(td), Selector.all())

        self.assertEqual(observed, [OperationSummary(0, 0, 1, 2)] * 3)
        self.assertEqual(unavailable_summary, OperationSummary(0, 1, 0, 0))

    def test_lifecycle_commands_fail_when_selector_has_no_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            config = Path(td) / "config"
            config.mkdir()
            (config / "source-hosts.json").write_text(
                json.dumps({"schema_version": 1, "hosts": ["pubs.shure.com"]}),
                encoding="utf-8",
            )
            (config / "source-url-redactions.json").write_text(
                json.dumps({"schema_version": 1, "entries": []}),
                encoding="utf-8",
            )
            transport = StaticTransport()
            context = CommandContext(
                transport=transport,
                store=storage_module.AtomicArtifactStore(),
                now=lambda: datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
            )

            summaries = (
                bootstrap(Path(td), Selector.all(), True, context),
                fetch(Path(td), Selector.all(), context),
                verify(Path(td), Selector.all()),
            )

            self.assertEqual(summaries, (OperationSummary(0, 0, 1, 1),) * 3)
            self.assertEqual(transport.calls, [])


class CLITests(unittest.TestCase):
    def test_cli_requires_exactly_one_selector_and_bootstrap_accept_new(self):
        observed = []
        cases = (
            ["verify"],
            ["verify", "--all", "--mic", "shure-sm58"],
            ["bootstrap", "--all"],
        )
        for argv in cases:
            with redirect_stderr(io.StringIO()):
                try:
                    source_assets_cli.main(argv)
                except SystemExit as error:
                    observed.append(error.code)
                else:
                    observed.append(None)

        self.assertEqual(observed, [2, 2, 2])

    def test_cli_rejects_invalid_mic_slug_as_an_argument_error(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            try:
                source_assets_cli.main(["verify", "--mic", "../outside"])
            except SystemExit as error:
                observed = error.code
            except SourceAssetError as error:
                observed = type(error).__name__
            else:
                observed = None

        self.assertEqual(observed, 2)
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_cli_prints_exact_summary_and_propagates_verify_exit_code(self):
        with tempfile.TemporaryDirectory() as td:
            _make_available_svg_repo(td, present=True)
            stdout = io.StringIO()
            stderr = io.StringIO()

            with (
                patch.object(source_assets_cli, "REPOSITORY_ROOT", Path(td), create=True),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                exit_code = source_assets_cli.main(["verify", "--all"])

            self.assertEqual(exit_code, 0)
            self.assertEqual(stdout.getvalue(), "available=1 unavailable=0 failed=0\n")
            self.assertEqual(stderr.getvalue(), "")

    def test_cli_prints_exact_no_manifest_error_only_to_stderr(self):
        with tempfile.TemporaryDirectory() as td:
            config = Path(td) / "config"
            config.mkdir()
            (config / "source-hosts.json").write_text(
                json.dumps({"schema_version": 1, "hosts": ["pubs.shure.com"]}),
                encoding="utf-8",
            )
            (config / "source-url-redactions.json").write_text(
                json.dumps({"schema_version": 1, "entries": []}),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            stderr = io.StringIO()

            with (
                patch.object(source_assets_cli, "REPOSITORY_ROOT", Path(td)),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                exit_code = source_assets_cli.main(["verify", "--all"])

            self.assertEqual(exit_code, 1)
            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(
                stderr.getvalue(),
                "找不到 source-manifest.json；請先執行來源 manifest 遷移\n",
            )

    def test_cli_wires_production_context_for_bootstrap_and_fetch(self):
        observed = []
        for argv in (
            ["bootstrap", "--all", "--accept-new"],
            ["fetch", "--all"],
        ):
            with self.subTest(command=argv[0]), tempfile.TemporaryDirectory() as td:
                _make_available_svg_repo(td, present=True)
                stdout = io.StringIO()
                stderr = io.StringIO()
                with (
                    patch.object(source_assets_cli, "REPOSITORY_ROOT", Path(td)),
                    redirect_stdout(stdout),
                    redirect_stderr(stderr),
                ):
                    exit_code = source_assets_cli.main(argv)
                observed.append((exit_code, stdout.getvalue(), stderr.getvalue()))

        self.assertEqual(
            observed,
            [(0, "available=1 unavailable=0 failed=0\n", "")] * 2,
        )

    def test_cli_never_prints_signed_credential_values(self):
        secret = "do-not-print-this-token"
        with tempfile.TemporaryDirectory() as td:
            _mic_dir, manifest = _make_pending_lifecycle_repo(td)
            document = json.loads(manifest.read_text(encoding="utf-8"))
            document["artifacts"][0]["source_url"] += f"?token={secret}"
            manifest.write_text(json.dumps(document), encoding="utf-8")
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                patch.object(source_assets_cli, "REPOSITORY_ROOT", Path(td)),
                patch.object(
                    network_module.socket,
                    "getaddrinfo",
                    side_effect=AssertionError("signed URL 不得進行 DNS"),
                ),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                exit_code = source_assets_cli.main(
                    ["bootstrap", "--all", "--accept-new"]
                )

            combined = stdout.getvalue() + stderr.getvalue()
            self.assertEqual(exit_code, 1)
            self.assertEqual(stdout.getvalue(), "available=0 unavailable=0 failed=1\n")
            self.assertEqual(stderr.getvalue(), "")
            self.assertNotIn(secret, combined)

    def test_cli_propagates_integrity_exit_code_two(self):
        with tempfile.TemporaryDirectory() as td:
            _mic_dir, _manifest, target, _body = _make_available_svg_repo(
                td,
                present=True,
            )
            target.write_bytes(
                b'<svg xmlns="http://www.w3.org/2000/svg"><text>changed</text></svg>'
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                patch.object(source_assets_cli, "REPOSITORY_ROOT", Path(td)),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                exit_code = source_assets_cli.main(["verify", "--all"])

            self.assertEqual(exit_code, 2)
            self.assertEqual(stdout.getvalue(), "available=0 unavailable=0 failed=1\n")
            self.assertEqual(stderr.getvalue(), "")

    def test_cli_classifies_manifest_probe_oserror(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.object(
                source_assets_cli.os,
                "stat",
                side_effect=PermissionError("manifest probe denied"),
            ),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            try:
                exit_code = source_assets_cli.main(
                    ["verify", "--mic", "shure-sm58"]
                )
            except OSError as error:
                exit_code = type(error).__name__

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout.getvalue(), "available=0 unavailable=0 failed=1\n")
        self.assertEqual(stderr.getvalue(), "")


class NetworkPolicyTests(unittest.TestCase):
    def test_parse_rejects_empty_hostname_even_if_allowlist_contains_empty_string(self):
        with self.assertRaises(ContractError):
            URLPolicy.parse(
                "https:///manual.pdf",
                frozenset({""}),
            )

    def test_parse_converts_malformed_authority_to_contract_error(self):
        with self.assertRaises(ContractError):
            URLPolicy.parse(
                "https://[not-an-ip/manual.pdf",
                frozenset({"pubs.shure.com"}),
            )

    def test_parse_converts_out_of_range_port_to_contract_error(self):
        with self.assertRaises(ContractError):
            URLPolicy.parse(
                "https://pubs.shure.com:99999/manual.pdf",
                frozenset({"pubs.shure.com"}),
            )

    def test_parse_rejects_explicit_zero_port(self):
        with self.assertRaises(ContractError):
            URLPolicy.parse(
                "https://pubs.shure.com:0/manual.pdf",
                frozenset({"pubs.shure.com"}),
            )

    def test_parse_rejects_explicit_empty_port(self):
        with self.assertRaises(ContractError):
            URLPolicy.parse(
                "https://pubs.shure.com:/manual.pdf",
                frozenset({"pubs.shure.com"}),
            )

    def test_parse_rejects_percent_encoded_signed_query_key_without_leaking_value(self):
        with self.assertRaises(ContractError) as caught:
            URLPolicy.parse(
                "https://pubs.shure.com/manual.pdf?%74oken=top-secret",
                frozenset({"pubs.shure.com"}),
            )

        self.assertNotIn("top-secret", str(caught.exception))

    def test_production_connector_pins_ip_but_preserves_tls_server_hostname(self):
        class RawSocket:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        class TLSSocket:
            def __init__(self):
                self.closed = False

            def getpeername(self):
                return ("93.184.216.34", 443)

            def close(self):
                self.closed = True

        class Context:
            def __init__(self, tls_socket):
                self.tls_socket = tls_socket
                self.calls = []

            def wrap_socket(self, raw_socket, *, server_hostname):
                self.calls.append((raw_socket, server_hostname))
                return self.tls_socket

        raw_socket = RawSocket()
        tls_socket = TLSSocket()
        context = Context(tls_socket)
        with (
            patch.object(network_module.ssl, "create_default_context", return_value=context),
            patch.object(network_module.socket, "create_connection", return_value=raw_socket) as connect,
        ):
            connection = PinnedTLSConnector().connect(
                ip_address="93.184.216.34",
                server_hostname="pubs.shure.com",
                port=443,
                timeout_seconds=30.0,
            )

        connect.assert_called_once_with(("93.184.216.34", 443), 30.0, None)
        self.assertEqual(context.calls, [(raw_socket, "pubs.shure.com")])
        self.assertEqual(connection.peer_ip, "93.184.216.34")
        connection.close()
        self.assertTrue(tls_socket.closed)

    def test_pinned_https_client_uses_production_connector_by_default(self):
        client = PinnedHTTPSClient()

        self.assertIsInstance(client.connector, PinnedTLSConnector)

    def test_resolve_public_addresses_returns_unique_socket_addresses(self):
        answers = [
            (2, 1, 6, "", ("93.184.216.34", 443)),
            (2, 1, 6, "", ("93.184.216.34", 443)),
            (10, 1, 6, "", ("2606:2800:220:1:248:1893:25c8:1946", 443, 0, 0)),
        ]
        with patch.object(network_module.socket, "getaddrinfo", return_value=answers) as lookup:
            result = resolve_public_addresses("example.com")

        self.assertEqual(
            result,
            ("93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"),
        )
        lookup.assert_called_once_with(
            "example.com",
            443,
            type=network_module.socket.SOCK_STREAM,
            proto=network_module.socket.IPPROTO_TCP,
        )

    def test_parse_rejects_control_characters_before_urlsplit_normalization(self):
        with self.assertRaises(ContractError):
            URLPolicy.parse(
                "https://pubs.shure.com/manual.pdf\r\nX-Injected: yes",
                frozenset({"pubs.shure.com"}),
            )

    def test_parse_rejects_raw_spaces_before_urlsplit_normalization(self):
        with self.assertRaises(ContractError):
            URLPolicy.parse(
                " https://pubs.shure.com/manual.pdf",
                frozenset({"pubs.shure.com"}),
            )

    def test_remote_http_error_redacts_credential_query_values(self):
        error = RemoteHTTPError(
            403,
            "https://pubs.shure.com/manual.pdf?token=top-secret&id=968996",
        )

        self.assertEqual(error.status, 403)
        self.assertNotIn("top-secret", str(error))
        self.assertNotIn("top-secret", error.redacted_url)
        self.assertIn("token=%3Credacted%3E", error.redacted_url)
        self.assertIn("id=968996", error.redacted_url)

    def test_parse_rejects_http_downgrade(self):
        with self.assertRaises(ContractError):
            URLPolicy.parse(
                "http://pubs.shure.com/manual.pdf",
                frozenset({"pubs.shure.com"}),
            )

    def test_parse_rejects_non_443_port(self):
        with self.assertRaises(ContractError):
            URLPolicy.parse(
                "https://pubs.shure.com:8443/manual.pdf",
                frozenset({"pubs.shure.com"}),
            )

    def test_parse_rejects_ip_literals(self):
        urls_and_hosts = (
            ("https://93.184.216.34/manual.pdf", frozenset({"93.184.216.34"})),
            ("https://[2606:2800:220:1:248:1893:25c8:1946]/manual.pdf", frozenset({"2606:2800:220:1:248:1893:25c8:1946"})),
        )
        accepted = []
        for url, hosts in urls_and_hosts:
            try:
                URLPolicy.parse(url, hosts)
            except ContractError:
                continue
            accepted.append(url)

        self.assertEqual(accepted, [])

    def test_parse_rejects_fragment(self):
        with self.assertRaises(ContractError):
            URLPolicy.parse(
                "https://pubs.shure.com/manual.pdf#page=3",
                frozenset({"pubs.shure.com"}),
            )

    def test_parse_accepts_harmless_query_and_preserves_request_target(self):
        parsed = URLPolicy.parse(
            "https://pubs.shure.com/manual.pdf?id=968996",
            frozenset({"pubs.shure.com"}),
        )

        self.assertEqual(parsed.request_target, "/manual.pdf?id=968996")
        self.assertEqual(parsed.normalized_url, "https://pubs.shure.com/manual.pdf?id=968996")

    def test_parse_rejects_signed_credential_query_keys_case_insensitively(self):
        query_keys = ("token", "SIG", "Signature", "expires", "X-Amz-Signature", "x-goog-token")
        accepted = []
        for key in query_keys:
            try:
                URLPolicy.parse(
                    f"https://pubs.shure.com/manual.pdf?{key}=secret",
                    frozenset({"pubs.shure.com"}),
                )
            except ContractError:
                continue
            accepted.append(key)

        self.assertEqual(accepted, [])

    def test_parse_normalizes_hostname_to_lowercase_idna_without_trailing_dot(self):
        parsed = URLPolicy.parse(
            "https://BÜCHER.Example./manual.pdf",
            frozenset({"xn--bcher-kva.example"}),
        )

        self.assertEqual(parsed.hostname, "xn--bcher-kva.example")
        self.assertEqual(parsed.normalized_url, "https://xn--bcher-kva.example/manual.pdf")

    def test_parse_requires_exact_allowed_host(self):
        with self.assertRaises(ContractError):
            URLPolicy.parse(
                "https://cdn.pubs.shure.com/manual.pdf",
                frozenset({"pubs.shure.com"}),
            )

    def test_parse_rejects_user_info(self):
        with self.assertRaises(ContractError):
            URLPolicy.parse(
                "https://user:password@pubs.shure.com/manual.pdf",
                frozenset({"pubs.shure.com"}),
            )


class PinnedHTTPSTests(unittest.TestCase):
    def _assert_target_write_contract_error(self, writer):
        connector = ConnectorFixture()
        with self.assertRaises(ContractError) as caught:
            PinnedHTTPSClient(
                resolver=lambda _host: ("93.184.216.34",),
                connector=connector,
            ).download(_pdf_request(), writer)
        self.assertEqual(caught.exception.exit_code, 1)
        self.assertTrue(connector.connections[0].closed)

    def test_target_write_oserror_is_local_contract_error(self):
        self._assert_target_write_contract_error(
            _WriterFixture(error=OSError("disk failed"))
        )

    def test_target_write_timeout_is_local_contract_error(self):
        self._assert_target_write_contract_error(
            _WriterFixture(error=TimeoutError("local writer timeout"))
        )

    def test_target_write_none_is_contract_error(self):
        self._assert_target_write_contract_error(_WriterFixture(result=None))

    def test_target_write_zero_is_contract_error(self):
        self._assert_target_write_contract_error(_WriterFixture(result=0))

    def test_target_write_short_result_is_contract_error(self):
        self._assert_target_write_contract_error(_WriterFixture(result=8))

    def test_bad_status_line_becomes_redacted_remote_error_and_closes(self):
        connector = ConnectorFixture(
            ResponseFixture(
                response_error=network_module.http.client.BadStatusLine("top-secret")
            )
        )

        with self.assertRaises(RemoteError) as caught:
            PinnedHTTPSClient(
                resolver=lambda _host: ("93.184.216.34",),
                connector=connector,
            ).download(_pdf_request(), io.BytesIO())

        self.assertEqual(caught.exception.exit_code, 2)
        self.assertNotIn("top-secret", str(caught.exception))
        self.assertTrue(connector.connections[0].closed)

    def test_incomplete_read_becomes_remote_error_and_closes(self):
        connector = ConnectorFixture(
            ResponseFixture(
                read_error=network_module.http.client.IncompleteRead(b"partial", 10)
            )
        )

        with self.assertRaises(RemoteError) as caught:
            PinnedHTTPSClient(
                resolver=lambda _host: ("93.184.216.34",),
                connector=connector,
            ).download(_pdf_request(), io.BytesIO())

        self.assertEqual(caught.exception.exit_code, 2)
        self.assertTrue(connector.connections[0].closed)

    def test_download_rejects_special_and_embedded_nonpublic_addresses(self):
        addresses = (
            "240.0.0.1",
            "224.0.0.1",
            "0.0.0.0",
            "127.0.0.1",
            "10.0.0.1",
            "169.254.1.1",
            "::ffff:127.0.0.1",
            "64:ff9b::7f00:1",
        )
        accepted = []
        connected = []
        for address in addresses:
            connector = ConnectorFixture()
            try:
                PinnedHTTPSClient(
                    resolver=lambda _host, value=address: (value,),
                    connector=connector,
                ).download(_pdf_request(), io.BytesIO())
            except ContractError:
                pass
            else:
                accepted.append(address)
            if connector.connect_calls:
                connected.append(address)

        self.assertEqual(accepted, [])
        self.assertEqual(connected, [])

    def test_content_length_caps_stream_without_expected_size(self):
        body = b"%PDF-1.7\nextra"
        declared_size = len(body) - 1
        connector = ConnectorFixture(
            ResponseFixture(headers={"Content-Length": str(declared_size)}, body=body)
        )
        target = io.BytesIO()

        with self.assertRaises(IntegrityError):
            PinnedHTTPSClient(
                resolver=lambda _host: ("93.184.216.34",),
                connector=connector,
            ).download(_unverified_pdf_request(body), target)

        self.assertLessEqual(len(target.getvalue()), declared_size)

    def test_content_length_requires_exact_eof_without_expected_size(self):
        body = b"%PDF-1.7\n"
        connector = ConnectorFixture(
            ResponseFixture(headers={"Content-Length": str(len(body) + 1)}, body=body)
        )

        with self.assertRaises(IntegrityError):
            PinnedHTTPSClient(
                resolver=lambda _host: ("93.184.216.34",),
                connector=connector,
            ).download(_unverified_pdf_request(body), io.BytesIO())

    def test_download_rejects_svg_doctype_and_entity_declarations(self):
        bodies = (
            b'<!doctype svg><svg xmlns="http://www.w3.org/2000/svg"/>',
            b'<!ENTITY x "unsafe"><svg xmlns="http://www.w3.org/2000/svg"/>',
            (
                '<?xml version="1.0" encoding="utf-16"?>'
                '<!DOCTYPE svg [<!ENTITY x "unsafe">]>'
                '<svg xmlns="http://www.w3.org/2000/svg">&x;</svg>'
            ).encode("utf-16"),
        )
        accepted = []
        for body in bodies:
            try:
                PinnedHTTPSClient(
                    resolver=lambda _host: ("93.184.216.34",),
                    connector=ConnectorFixture(ResponseFixture(body=body)),
                ).download(_svg_request(body=body), io.BytesIO())
            except IntegrityError:
                continue
            accepted.append(body)

        self.assertEqual(accepted, [])

    def test_download_validates_png_signature_from_bytes(self):
        valid = b"\x89PNG\r\n\x1a\nrest"
        invalid = b"not a png"
        accepted = []
        for body in (valid, invalid):
            connector = ConnectorFixture(ResponseFixture(body=body))
            try:
                PinnedHTTPSClient(
                    resolver=lambda _host: ("93.184.216.34",),
                    connector=connector,
                ).download(_png_request(body), io.BytesIO())
            except IntegrityError:
                continue
            accepted.append(body)

        self.assertEqual(accepted, [valid])

    def test_download_rejects_noncanonical_content_length_before_writing(self):
        connector = ConnectorFixture(ResponseFixture(headers={"Content-Length": "+10"}))
        target = io.BytesIO()

        with self.assertRaises(ContractError):
            PinnedHTTPSClient(
                resolver=lambda _host: ("93.184.216.34",),
                connector=connector,
            ).download(_pdf_request(), target)

        self.assertEqual(target.getvalue(), b"")

    def test_download_classifies_socket_http_failures_and_always_closes(self):
        fixtures = (
            ResponseFixture(request_error=OSError("request failed")),
            ResponseFixture(response_error=OSError("response failed")),
            ResponseFixture(read_error=OSError("read failed")),
        )
        unclassified = []
        unclosed = []
        for fixture in fixtures:
            connector = ConnectorFixture(fixture)
            client = PinnedHTTPSClient(
                resolver=lambda _host: ("93.184.216.34",),
                connector=connector,
            )
            try:
                client.download(_pdf_request(), io.BytesIO())
            except RemoteError:
                pass
            except Exception as error:
                unclassified.append(type(error))
            if not connector.connections[0].closed:
                unclosed.append(fixture)

        self.assertEqual(unclassified, [])
        self.assertEqual(unclosed, [])

    def test_download_classifies_connect_failure_as_remote_error(self):
        class FailingConnector:
            def connect(self, **_kwargs):
                raise OSError("connect failed")

        client = PinnedHTTPSClient(
            resolver=lambda _host: ("93.184.216.34",),
            connector=FailingConnector(),
        )

        with self.assertRaises(RemoteError):
            client.download(_pdf_request(), io.BytesIO())

    def test_download_rejects_redirect_to_host_outside_allowlist_before_dns(self):
        connector = ConnectorFixture(
            ResponseFixture(
                status=302,
                headers={"Location": "https://evil.example/final.pdf"},
                body=b"",
            )
        )
        resolver_calls = []

        def resolver(hostname):
            resolver_calls.append(hostname)
            return ("93.184.216.34",)

        with self.assertRaises(ContractError):
            PinnedHTTPSClient(resolver=resolver, connector=connector).download(
                _pdf_request(), io.BytesIO()
            )

        self.assertEqual(resolver_calls, ["pubs.shure.com"])

    def test_download_follows_allowed_cross_host_redirect_with_new_pin(self):
        connector = ConnectorFixture(
            ResponseFixture(
                status=302,
                headers={"Location": "https://www.neumann.com/final.pdf"},
                body=b"",
            ),
            ResponseFixture(peer_ip="93.184.216.35"),
        )
        addresses = {
            "pubs.shure.com": ("93.184.216.34",),
            "www.neumann.com": ("93.184.216.35",),
        }

        result = PinnedHTTPSClient(
            resolver=lambda hostname: addresses[hostname],
            connector=connector,
        ).download(
            _pdf_request(allowed_hosts=frozenset(addresses)),
            io.BytesIO(),
        )

        self.assertEqual(result.final_url, "https://www.neumann.com/final.pdf")
        self.assertEqual(
            [call["ip_address"] for call in connector.connect_calls],
            ["93.184.216.34", "93.184.216.35"],
        )
        self.assertEqual(
            [call["server_hostname"] for call in connector.connect_calls],
            ["pubs.shure.com", "www.neumann.com"],
        )

    def test_download_accepts_ipv4_mapped_peer_for_same_pinned_address(self):
        connector = ConnectorFixture(ResponseFixture(peer_ip="::ffff:93.184.216.34"))

        result = PinnedHTTPSClient(
            resolver=lambda _host: ("93.184.216.34",),
            connector=connector,
        ).download(_pdf_request(), io.BytesIO())

        self.assertEqual(result.status, 200)

    def test_download_rejects_206_before_writing(self):
        connector = ConnectorFixture(
            ResponseFixture(status=206, headers={"Content-Range": "bytes 0-9/10"})
        )
        target = io.BytesIO()

        with self.assertRaises(RemoteHTTPError) as caught:
            PinnedHTTPSClient(
                resolver=lambda _host: ("93.184.216.34",),
                connector=connector,
            ).download(_pdf_request(), target)

        self.assertEqual(caught.exception.status, 206)
        self.assertEqual(target.getvalue(), b"")

    def test_download_rejects_redirect_without_location(self):
        connector = ConnectorFixture(ResponseFixture(status=302, body=b""))

        with self.assertRaises(ContractError):
            PinnedHTTPSClient(
                resolver=lambda _host: ("93.184.216.34",),
                connector=connector,
            ).download(_pdf_request(), io.BytesIO())

    def test_download_enforces_configured_redirect_limit(self):
        connector = ConnectorFixture(
            ResponseFixture(status=302, headers={"Location": "/one.pdf"}, body=b"")
        )

        with self.assertRaises(ContractError):
            PinnedHTTPSClient(
                resolver=lambda _host: ("93.184.216.34",),
                connector=connector,
                max_redirects=0,
            ).download(_pdf_request(), io.BytesIO())

    def test_download_classifies_dns_failure_as_remote_error(self):
        def resolver(_hostname):
            raise OSError("dns failed")

        client = PinnedHTTPSClient(resolver=resolver, connector=ConnectorFixture())

        with self.assertRaises(RemoteError):
            client.download(_pdf_request(), io.BytesIO())

    def test_download_classifies_read_timeout_as_remote_error_and_closes(self):
        connector = ConnectorFixture(ResponseFixture(read_error=TimeoutError("slow")))
        client = PinnedHTTPSClient(
            resolver=lambda _host: ("93.184.216.34",),
            connector=connector,
        )

        with self.assertRaises(RemoteError):
            client.download(_pdf_request(), io.BytesIO())

        self.assertTrue(connector.connections[0].closed)

    def test_redirect_rejects_user_info_after_urljoin_before_dns(self):
        connector = ConnectorFixture(
            ResponseFixture(
                status=302,
                headers={"Location": "https://user:secret@pubs.shure.com/final.pdf"},
                body=b"",
            )
        )
        resolver_calls = []

        def resolver(hostname):
            resolver_calls.append(hostname)
            return ("93.184.216.34",)

        with self.assertRaises(ContractError) as caught:
            PinnedHTTPSClient(resolver=resolver, connector=connector).download(
                _pdf_request(), io.BytesIO()
            )

        self.assertEqual(resolver_calls, ["pubs.shure.com"])
        self.assertNotIn("secret", str(caught.exception))

    def test_redirect_rejects_fragment_after_urljoin_before_dns(self):
        connector = ConnectorFixture(
            ResponseFixture(status=302, headers={"Location": "/final.pdf#page=2"}, body=b"")
        )
        resolver_calls = []

        def resolver(hostname):
            resolver_calls.append(hostname)
            return ("93.184.216.34",)

        with self.assertRaises(ContractError):
            PinnedHTTPSClient(resolver=resolver, connector=connector).download(
                _pdf_request(), io.BytesIO()
            )

        self.assertEqual(resolver_calls, ["pubs.shure.com"])

    def test_redirect_rejects_control_characters_before_urljoin(self):
        connector = ConnectorFixture(
            ResponseFixture(
                status=302,
                headers={"Location": "/final.pdf\r\nX-Injected: yes"},
                body=b"",
            )
        )
        resolver_calls = []

        def resolver(hostname):
            resolver_calls.append(hostname)
            return ("93.184.216.34",)

        with self.assertRaises(ContractError):
            PinnedHTTPSClient(resolver=resolver, connector=connector).download(
                _pdf_request(), io.BytesIO()
            )

        self.assertEqual(resolver_calls, ["pubs.shure.com"])

    def test_redirect_rejects_signed_query_before_dns_without_leaking_secret(self):
        connector = ConnectorFixture(
            ResponseFixture(
                status=302,
                headers={"Location": "/final.pdf?X-Amz-Signature=top-secret"},
                body=b"",
            )
        )
        resolver_calls = []

        def resolver(hostname):
            resolver_calls.append(hostname)
            return ("93.184.216.34",)

        with self.assertRaises(ContractError) as caught:
            PinnedHTTPSClient(resolver=resolver, connector=connector).download(
                _pdf_request(), io.BytesIO()
            )

        self.assertEqual(resolver_calls, ["pubs.shure.com"])
        self.assertNotIn("top-secret", str(caught.exception))

    def test_download_accepts_svg_bytes_at_opaque_url_regardless_of_content_type(self):
        body = b'<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0"/></svg>'
        connector = ConnectorFixture(
            ResponseFixture(headers={"Content-Type": "text/plain"}, body=body)
        )
        result = PinnedHTTPSClient(
            resolver=lambda _host: ("93.184.216.34",),
            connector=connector,
        ).download(_svg_request(body=body), io.BytesIO())

        self.assertEqual(result.size_bytes, len(body))
        self.assertEqual(result.content_type, "text/plain")
        self.assertEqual(result.final_url, "https://www.neumann.com/svg/Zz09PT0=")

    def test_download_rejects_non_svg_body_for_svg_media_type(self):
        body = b"<html>error</html>"
        connector = ConnectorFixture(ResponseFixture(body=body))
        client = PinnedHTTPSClient(
            resolver=lambda _host: ("93.184.216.34",),
            connector=connector,
        )

        with self.assertRaises(IntegrityError):
            client.download(_svg_request(body=body), io.BytesIO())

    def test_download_rejects_html_body_disguised_as_pdf(self):
        body = b"<html>error</html>"
        connector = ConnectorFixture(
            ResponseFixture(headers={"Content-Type": "application/pdf"}, body=body)
        )
        client = PinnedHTTPSClient(
            resolver=lambda _host: ("93.184.216.34",),
            connector=connector,
        )

        with self.assertRaises(IntegrityError):
            client.download(_pdf_request(body=body), io.BytesIO())

    def test_download_rejects_sha256_mismatch_after_eof(self):
        connector = ConnectorFixture(ResponseFixture())
        client = PinnedHTTPSClient(
            resolver=lambda _host: ("93.184.216.34",),
            connector=connector,
        )

        with self.assertRaises(IntegrityError):
            client.download(
                _pdf_request(expected_sha256="0" * 64),
                io.BytesIO(),
            )

    def test_download_rejects_short_body_at_eof(self):
        body = b"%PDF-1.7\n"
        connector = ConnectorFixture(ResponseFixture(body=body))
        client = PinnedHTTPSClient(
            resolver=lambda _host: ("93.184.216.34",),
            connector=connector,
        )

        with self.assertRaises(IntegrityError):
            client.download(_pdf_request(body=body, expected_size=len(body) + 1), io.BytesIO())

    def test_download_stops_when_stream_exceeds_expected_size(self):
        body = b"%PDF-1.7\nextra"
        connector = ConnectorFixture(ResponseFixture(body=body))
        target = io.BytesIO()
        client = PinnedHTTPSClient(
            resolver=lambda _host: ("93.184.216.34",),
            connector=connector,
        )

        with self.assertRaises(IntegrityError):
            client.download(_pdf_request(expected_size=10), target)

        self.assertLessEqual(len(target.getvalue()), 10)

    def test_download_rejects_content_length_mismatch_before_writing(self):
        connector = ConnectorFixture(ResponseFixture(headers={"Content-Length": "999"}))
        target = io.BytesIO()
        client = PinnedHTTPSClient(
            resolver=lambda _host: ("93.184.216.34",),
            connector=connector,
        )

        with self.assertRaises(IntegrityError):
            client.download(_pdf_request(), target)

        self.assertEqual(target.getvalue(), b"")

    def test_download_rejects_non_identity_content_encoding_before_writing(self):
        connector = ConnectorFixture(ResponseFixture(headers={"Content-Encoding": "gzip"}))
        target = io.BytesIO()
        client = PinnedHTTPSClient(
            resolver=lambda _host: ("93.184.216.34",),
            connector=connector,
        )

        with self.assertRaises(ContractError):
            client.download(_pdf_request(), target)

        self.assertEqual(target.getvalue(), b"")

    def test_download_rejects_200_with_content_range_before_writing(self):
        connector = ConnectorFixture(
            ResponseFixture(headers={"Content-Range": "bytes 0-10/20"})
        )
        target = io.BytesIO()
        client = PinnedHTTPSClient(
            resolver=lambda _host: ("93.184.216.34",),
            connector=connector,
        )

        with self.assertRaises(ContractError):
            client.download(_pdf_request(), target)

        self.assertEqual(target.getvalue(), b"")

    def test_download_rejects_final_status_other_than_200_before_writing(self):
        connector = ConnectorFixture(ResponseFixture(status=404, body=b"not found"))
        target = io.BytesIO()
        client = PinnedHTTPSClient(
            resolver=lambda _host: ("93.184.216.34",),
            connector=connector,
        )

        with self.assertRaises(RemoteHTTPError) as caught:
            client.download(_pdf_request(), target)

        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(target.getvalue(), b"")
        self.assertTrue(connector.connections[0].closed)

    def test_download_rejects_redirect_loop_before_repeating_request(self):
        connector = ConnectorFixture(
            ResponseFixture(status=302, headers={"Location": "/second.pdf"}, body=b""),
            ResponseFixture(status=302, headers={"Location": "/manual.pdf"}, body=b""),
        )
        client = PinnedHTTPSClient(
            resolver=lambda _host: ("93.184.216.34",),
            connector=connector,
        )

        with self.assertRaises(ContractError):
            client.download(_pdf_request(), io.BytesIO())

        self.assertEqual(len(connector.connect_calls), 2)

    def test_download_follows_relative_redirect_and_re_resolves_same_host(self):
        connector = ConnectorFixture(
            ResponseFixture(status=302, headers={"Location": "/final.pdf"}, body=b""),
            ResponseFixture(),
        )
        resolver_calls = []

        def resolver(hostname):
            resolver_calls.append(hostname)
            return ("93.184.216.34",)

        result = PinnedHTTPSClient(resolver=resolver, connector=connector).download(
            _pdf_request(), io.BytesIO()
        )

        self.assertEqual(resolver_calls, ["pubs.shure.com", "pubs.shure.com"])
        self.assertEqual(result.final_url, "https://pubs.shure.com/final.pdf")
        self.assertEqual(
            [connection.requests[0][1] for connection in connector.connections],
            ["/manual.pdf", "/final.pdf"],
        )
        self.assertTrue(all(connection.closed for connection in connector.connections))

    def test_download_preserves_tls_hostname_host_header_and_resolves_once(self):
        connector = ConnectorFixture(ResponseFixture(headers={"Content-Type": "application/pdf"}))
        resolver_calls = []

        def resolver(hostname):
            resolver_calls.append(hostname)
            return ("93.184.216.34",)

        client = PinnedHTTPSClient(resolver=resolver, connector=connector)
        target = io.BytesIO()

        result = client.download(_pdf_request(), target)

        self.assertEqual(resolver_calls, ["pubs.shure.com"])
        self.assertEqual(
            connector.connect_calls,
            [
                {
                    "ip_address": "93.184.216.34",
                    "server_hostname": "pubs.shure.com",
                    "port": 443,
                    "timeout_seconds": 30.0,
                }
            ],
        )
        self.assertEqual(
            connector.connections[0].requests,
            [
                (
                    "GET",
                    "/manual.pdf",
                    {"Host": "pubs.shure.com", "Accept-Encoding": "identity"},
                )
            ],
        )
        self.assertEqual(target.getvalue(), b"%PDF-1.7\n")
        self.assertEqual(result.final_url, "https://pubs.shure.com/manual.pdf")
        self.assertTrue(connector.connections[0].closed)

    def test_download_rejects_peer_different_from_pinned_ip(self):
        connector = ConnectorFixture(ResponseFixture(peer_ip="93.184.216.35"))
        client = PinnedHTTPSClient(
            resolver=lambda _host: ("93.184.216.34",),
            connector=connector,
        )

        with self.assertRaises(ContractError):
            client.download(_pdf_request(), io.BytesIO())

        self.assertTrue(connector.connections[0].closed)

    def test_download_rejects_when_any_resolved_address_is_not_public(self):
        connector = ConnectorFixture()
        client = PinnedHTTPSClient(
            resolver=lambda _host: ("93.184.216.34", "127.0.0.1"),
            connector=connector,
        )

        with self.assertRaises(ContractError):
            client.download(_pdf_request(), io.BytesIO())

        self.assertEqual(connector.connect_calls, [])


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

    def test_resolve_local_path_converts_resolve_oserror_to_contract_error(self):
        with tempfile.TemporaryDirectory() as td:
            mic = Path(td) / "data" / "mic"
            (mic / "source").mkdir(parents=True)

            with patch.object(
                Path,
                "resolve",
                side_effect=PermissionError("resolve denied"),
            ):
                try:
                    resolve_local_path(mic, "source/manual.pdf")
                except Exception as error:
                    observed = (type(error), getattr(error, "exit_code", None))
                else:
                    observed = None

            self.assertEqual(observed, (ContractError, 1))

    def test_resolve_local_path_converts_symlink_probe_oserror_to_contract_error(self):
        with tempfile.TemporaryDirectory() as td:
            mic = Path(td) / "data" / "mic"
            (mic / "source").mkdir(parents=True)

            with patch.object(
                Path,
                "is_symlink",
                side_effect=PermissionError("lstat denied"),
            ):
                try:
                    resolve_local_path(mic, "source/manual.pdf")
                except Exception as error:
                    observed = (type(error), getattr(error, "exit_code", None))
                else:
                    observed = None

            self.assertEqual(observed, (ContractError, 1))

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

    def test_atomic_store_converts_parent_and_temp_open_failures_to_contract_error(self):
        cases = (
            ("blob", "parent", PermissionError("parent denied")),
            ("blob", "temp", FileNotFoundError("parent disappeared")),
            ("manifest", "parent", PermissionError("parent denied")),
            ("manifest", "temp", FileNotFoundError("parent disappeared")),
        )
        for operation, stage, failure in cases:
            with (
                self.subTest(operation=operation, stage=stage),
                tempfile.TemporaryDirectory() as td,
            ):
                target = Path(td) / (
                    "manual.pdf" if operation == "blob" else "source-manifest.json"
                )
                old_content = b"old target"
                target.write_bytes(old_content)
                real_open = os.open

                def failing_open(path, flags, mode=0o777, *, dir_fd=None):
                    should_fail = (
                        (stage == "parent" and flags & getattr(os, "O_DIRECTORY", 0))
                        or (stage == "temp" and flags & os.O_CREAT)
                    )
                    if should_fail:
                        raise failure
                    if dir_fd is None:
                        return real_open(path, flags, mode)
                    return real_open(path, flags, mode, dir_fd=dir_fd)

                with patch.object(storage_module.os, "open", failing_open):
                    try:
                        if operation == "blob":
                            AtomicArtifactStore().publish_blob(
                                target,
                                "application/pdf",
                                lambda handle: handle.write(b"%PDF-new"),
                            )
                        else:
                            AtomicArtifactStore().publish_manifest(
                                target,
                                {"schema_version": 1},
                            )
                    except Exception as error:
                        observed = (type(error), getattr(error, "exit_code", None))
                    else:
                        observed = None

                self.assertEqual(observed, (ContractError, 1))
                self.assertEqual(target.read_bytes(), old_content)
                self.assertEqual(
                    list(target.parent.glob(f".{target.name}.*.tmp")),
                    [],
                )

    def test_atomic_store_converts_fdopen_and_producer_oserror_to_contract_error(self):
        cases = ("blob-fdopen", "blob-producer", "manifest-fdopen")
        for operation in cases:
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as td:
                target = Path(td) / (
                    "source-manifest.json"
                    if operation == "manifest-fdopen"
                    else "manual.pdf"
                )
                target.write_bytes(b"old target")
                patches = (
                    patch.object(
                        storage_module.os,
                        "fdopen",
                        side_effect=PermissionError("fdopen denied"),
                    )
                    if operation.endswith("fdopen")
                    else nullcontext()
                )
                with patches:
                    try:
                        if operation == "manifest-fdopen":
                            AtomicArtifactStore().publish_manifest(
                                target,
                                {"schema_version": 1},
                            )
                        else:
                            def producer(handle):
                                if operation == "blob-producer":
                                    raise PermissionError("producer write denied")
                                handle.write(b"%PDF-new")

                            AtomicArtifactStore().publish_blob(
                                target,
                                "application/pdf",
                                producer,
                            )
                    except Exception as error:
                        observed = (type(error), getattr(error, "exit_code", None))
                    else:
                        observed = None

                self.assertEqual(observed, (ContractError, 1))
                self.assertEqual(target.read_bytes(), b"old target")
                self.assertEqual(
                    list(target.parent.glob(f".{target.name}.*.tmp")),
                    [],
                )

    def test_publish_blob_rejects_parent_swap_after_producer_before_replace(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "data" / "mic" / "source"
            source.mkdir(parents=True)
            target = source / "manual.pdf"
            target.write_bytes(b"old target")
            parked = root / "parked-source"
            outside = root / "outside"
            outside.mkdir()

            def swapping_producer(handle):
                handle.write(b"%PDF-new")
                source.rename(parked)
                source.symlink_to(outside, target_is_directory=True)

            try:
                AtomicArtifactStore(root).publish_blob(
                    target,
                    "application/pdf",
                    swapping_producer,
                )
            except Exception as error:
                observed = (type(error), getattr(error, "exit_code", None))
            else:
                observed = None

            self.assertEqual(observed, (ContractError, 1))
            self.assertEqual((parked / "manual.pdf").read_bytes(), b"old target")
            self.assertEqual(
                [path for path in outside.rglob("*") if path.is_file()],
                [],
            )
            self.assertEqual(list(parked.glob(".manual.pdf.*.tmp")), [])

    def test_atomic_store_rejects_intermediate_microphone_symlink_swap(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            mic = root / "data" / "mic"
            source = mic / "source"
            source.mkdir(parents=True)
            target = source / "manual.pdf"
            parked = root / "parked-mic"
            outside_mic = root / "outside" / "mic"
            (outside_mic / "source").mkdir(parents=True)
            real_open = os.open
            swapped = False

            def swapping_open(path, flags, mode=0o777, *, dir_fd=None):
                nonlocal swapped
                if not swapped and dir_fd is not None and os.fspath(path) == "mic":
                    swapped = True
                    mic.rename(parked)
                    mic.symlink_to(outside_mic, target_is_directory=True)
                if dir_fd is None:
                    return real_open(path, flags, mode)
                return real_open(path, flags, mode, dir_fd=dir_fd)

            with patch.object(storage_module.os, "open", swapping_open):
                try:
                    AtomicArtifactStore().publish_blob(
                        target,
                        "application/pdf",
                        lambda handle: handle.write(b"%PDF-new"),
                    )
                except Exception as error:
                    observed = (type(error), getattr(error, "exit_code", None))
                else:
                    observed = None

            self.assertEqual(observed, (ContractError, 1))
            self.assertTrue(swapped)
            self.assertEqual(
                [path for path in outside_mic.rglob("*") if path.is_file()],
                [],
            )

    def test_publish_manifest_rejects_microphone_swap_before_replace(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            mic = root / "data" / "mic"
            mic.mkdir(parents=True)
            target = mic / "source-manifest.json"
            target.write_bytes(b"old manifest\n")
            parked = root / "parked-mic"
            outside_mic = root / "outside" / "mic"
            outside_mic.mkdir(parents=True)
            outside_manifest = outside_mic / "source-manifest.json"
            outside_manifest.write_bytes(b"outside sentinel\n")
            real_fsync = os.fsync
            swapped = False

            def swapping_fsync(fd):
                nonlocal swapped
                result = real_fsync(fd)
                if not swapped:
                    swapped = True
                    mic.rename(parked)
                    mic.symlink_to(outside_mic, target_is_directory=True)
                return result

            with patch.object(storage_module.os, "fsync", swapping_fsync):
                try:
                    AtomicArtifactStore(root).publish_manifest(
                        target,
                        {"schema_version": 1},
                    )
                except Exception as error:
                    observed = (type(error), getattr(error, "exit_code", None))
                else:
                    observed = None

            self.assertEqual(observed, (ContractError, 1))
            self.assertTrue(swapped)
            self.assertEqual(
                (parked / "source-manifest.json").read_bytes(),
                b"old manifest\n",
            )
            self.assertEqual(outside_manifest.read_bytes(), b"outside sentinel\n")
            self.assertEqual(list(parked.glob(".source-manifest.json.*.tmp")), [])

    def test_atomic_store_rejects_parent_swap_inside_replace(self):
        for operation in ("blob", "manifest"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                mic = root / "data" / "mic"
                source = mic / "source"
                source.mkdir(parents=True)
                replacement_mic = root / "replacement-mic"
                replacement_source = replacement_mic / "source"
                replacement_source.mkdir(parents=True)
                if operation == "blob":
                    target = source / "manual.pdf"
                    replacement_target = replacement_source / "manual.pdf"
                    target.write_bytes(b"%PDF-old\n")
                    replacement_target.write_bytes(b"%PDF-replacement\n")
                else:
                    target = mic / "source-manifest.json"
                    replacement_target = replacement_mic / "source-manifest.json"
                    target.write_bytes(b"old manifest\n")
                    replacement_target.write_bytes(b"replacement manifest\n")
                parked = root / "parked-mic"
                real_replace = os.replace
                swapped = False

                def swapping_replace(
                    source_name,
                    target_name,
                    *,
                    src_dir_fd=None,
                    dst_dir_fd=None,
                ):
                    nonlocal swapped
                    if not swapped:
                        swapped = True
                        mic.rename(parked)
                        replacement_mic.rename(mic)
                    return real_replace(
                        source_name,
                        target_name,
                        src_dir_fd=src_dir_fd,
                        dst_dir_fd=dst_dir_fd,
                    )

                with patch.object(storage_module.os, "replace", swapping_replace):
                    try:
                        if operation == "blob":
                            AtomicArtifactStore(root).publish_blob(
                                target,
                                "application/pdf",
                                lambda handle: handle.write(b"%PDF-new\n"),
                            )
                        else:
                            AtomicArtifactStore(root).publish_manifest(
                                target,
                                {"schema_version": 1},
                            )
                    except Exception as error:
                        observed = (type(error), getattr(error, "exit_code", None))
                    else:
                        observed = None

                self.assertTrue(swapped)
                self.assertEqual(observed, (ContractError, 1))
                self.assertEqual(
                    target.read_bytes(),
                    b"%PDF-replacement\n"
                    if operation == "blob"
                    else b"replacement manifest\n",
                )

    def test_no_clobber_link_rejects_parent_swap_inside_link(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            mic = root / "data" / "mic"
            source = mic / "source"
            source.mkdir(parents=True)
            candidate = source / ".manual.pdf.candidate"
            candidate.write_bytes(b"%PDF-candidate\n")
            target = source / "manual.pdf"
            replacement_mic = root / "replacement-mic"
            (replacement_mic / "source").mkdir(parents=True)
            parked = root / "parked-mic"
            real_link = os.link
            swapped = False

            def swapping_link(
                source_name,
                target_name,
                *,
                src_dir_fd=None,
                dst_dir_fd=None,
                follow_symlinks=True,
            ):
                nonlocal swapped
                if not swapped:
                    swapped = True
                    mic.rename(parked)
                    replacement_mic.rename(mic)
                return real_link(
                    source_name,
                    target_name,
                    src_dir_fd=src_dir_fd,
                    dst_dir_fd=dst_dir_fd,
                    follow_symlinks=follow_symlinks,
                )

            with patch.object(storage_module.os, "link", swapping_link):
                try:
                    storage_module.link_local_file_if_absent(candidate, target, root)
                except Exception as error:
                    observed = (type(error), getattr(error, "exit_code", None))
                else:
                    observed = None

            self.assertTrue(swapped)
            self.assertEqual(observed, (ContractError, 1))
            self.assertFalse((mic / "source" / "manual.pdf").exists())
            self.assertEqual(
                (parked / "source" / "manual.pdf").read_bytes(),
                b"%PDF-candidate\n",
            )


    def test_publish_blob_is_atomic_durable_and_uses_same_directory_temp(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "manual.pdf"
            content = b"%PDF-1.7\nfixture\n"
            events = []
            temp_parent_inodes = []
            real_fdopen = os.fdopen
            real_fsync = os.fsync
            real_open = os.open
            real_replace = os.replace
            fsync_calls = 0

            def recording_fdopen(*args, **kwargs):
                return _RecordingBinaryHandle(real_fdopen(*args, **kwargs), events)

            def recording_open(path, flags, mode=0o777, *, dir_fd=None):
                if flags & os.O_CREAT:
                    temp_parent_inodes.append(os.fstat(dir_fd).st_ino)
                if dir_fd is None:
                    return real_open(path, flags, mode)
                return real_open(path, flags, mode, dir_fd=dir_fd)

            def recording_fsync(fd):
                nonlocal fsync_calls
                fsync_calls += 1
                events.append("file fsync" if fsync_calls == 1 else "directory fsync")
                return real_fsync(fd)

            def recording_replace(source, destination, **kwargs):
                events.append("os.replace")
                return real_replace(source, destination, **kwargs)

            def producer(handle):
                handle.write(content)
                return "producer-result"

            with (
                patch.object(storage_module.os, "fdopen", recording_fdopen),
                patch.object(storage_module.os, "fsync", recording_fsync),
                patch.object(storage_module.os, "open", recording_open),
                patch.object(storage_module.os, "replace", recording_replace),
            ):
                result = AtomicArtifactStore().publish_blob(target, "application/pdf", producer)

            self.assertEqual(
                (
                    events,
                    temp_parent_inodes,
                    result,
                    target.read_bytes() if target.exists() else None,
                    list(target.parent.glob(f".{target.name}.*.tmp")),
                ),
                (
                    ["write", "flush", "file fsync", "os.replace", "directory fsync"],
                    [target.parent.stat().st_ino],
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
                "_validate_signature_content",
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

    def test_publish_blob_cleanup_oserror_does_not_mask_integrity_error(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "manual.pdf"
            target.write_bytes(b"old target")
            expected = IntegrityError("primary integrity failure")

            with (
                patch.object(
                    storage_module,
                    "_validate_signature_content",
                    side_effect=expected,
                ),
                patch.object(
                    storage_module.os,
                    "unlink",
                    side_effect=PermissionError("cleanup denied"),
                ),
            ):
                try:
                    AtomicArtifactStore().publish_blob(
                        target,
                        "application/pdf",
                        lambda handle: handle.write(b"%PDF-new"),
                    )
                except SourceAssetError as error:
                    caught = error
                else:
                    caught = None

            self.assertIs(caught, expected)
            self.assertEqual(target.read_bytes(), b"old target")
            self.assertTrue(
                any("清理暫存資源失敗" in note for note in getattr(caught, "__notes__", ()))
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
            temp_parent_inodes = []
            real_fdopen = os.fdopen
            real_fsync = os.fsync
            real_open = os.open
            real_replace = os.replace
            fsync_calls = 0

            def recording_fdopen(*args, **kwargs):
                return _RecordingBinaryHandle(real_fdopen(*args, **kwargs), events)

            def recording_open(path, flags, mode=0o777, *, dir_fd=None):
                if flags & os.O_CREAT:
                    temp_parent_inodes.append(os.fstat(dir_fd).st_ino)
                if dir_fd is None:
                    return real_open(path, flags, mode)
                return real_open(path, flags, mode, dir_fd=dir_fd)

            def recording_fsync(fd):
                nonlocal fsync_calls
                fsync_calls += 1
                events.append("file fsync" if fsync_calls == 1 else "directory fsync")
                return real_fsync(fd)

            def recording_replace(source, destination, **kwargs):
                events.append("os.replace")
                return real_replace(source, destination, **kwargs)

            with (
                patch.object(storage_module.os, "fdopen", recording_fdopen),
                patch.object(storage_module.os, "fsync", recording_fsync),
                patch.object(storage_module.os, "open", recording_open),
                patch.object(storage_module.os, "replace", recording_replace),
            ):
                AtomicArtifactStore().publish_manifest(
                    target,
                    {"z": "終", "a": [2, 1]},
                )

            self.assertEqual(
                (
                    events,
                    temp_parent_inodes,
                    target.read_bytes() if target.exists() else None,
                    list(target.parent.glob(f".{target.name}.*.tmp")),
                ),
                (
                    ["write", "flush", "file fsync", "os.replace", "directory fsync"],
                    [target.parent.stat().st_ino],
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
