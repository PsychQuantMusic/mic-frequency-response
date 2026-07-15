from dataclasses import dataclass
import ipaddress
from typing import BinaryIO, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .model import ContractError, RemoteError


SIGNED_QUERY_KEYS = frozenset({"token", "sig", "signature", "expires"})


def is_signed_query_key(key: str) -> bool:
    normalized = key.casefold()
    return (
        normalized in SIGNED_QUERY_KEYS
        or normalized.startswith("x-amz-")
        or normalized.startswith("x-goog-")
    )


def _redact_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname or ""
        port = parsed.port
    except (TypeError, ValueError):
        return "<invalid URL>"
    netloc = hostname if port in {None, 443} else f"{hostname}:{port}"
    query = urlencode(
        [
            (key, "<redacted>" if is_signed_query_key(key) else value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        ]
    )
    return urlunsplit((parsed.scheme, netloc, parsed.path, query, ""))


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
    def __init__(self, status: int, url: str):
        self.status = status
        self.redacted_url = _redact_url(url)
        super().__init__(f"遠端 HTTP {status}：{self.redacted_url}")


class HTTPResponse(Protocol):
    status: int

    def getheader(self, name: str, default: str | None = None) -> str | None: ...

    def read(self, amount: int | None = None) -> bytes: ...


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


class Transport(Protocol):
    def download(self, request: DownloadRequest, target: BinaryIO) -> DownloadResult: ...


class URLPolicy:
    @staticmethod
    def parse(url: str, allowed_hosts: frozenset[str]) -> ValidatedURL:
        if not isinstance(url, str) or any(ord(character) < 0x20 or ord(character) == 0x7F for character in url):
            raise ContractError("URL 不得包含控制字元")
        parsed = urlsplit(url)
        if parsed.scheme != "https":
            raise ContractError("URL 必須使用 HTTPS")
        if parsed.username is not None or parsed.password is not None:
            raise ContractError("URL 不得包含 user-info")
        if parsed.fragment:
            raise ContractError("URL 不得包含 fragment")
        raw_hostname = (parsed.hostname or "").rstrip(".")
        try:
            hostname = raw_hostname.encode("idna").decode("ascii").lower()
        except UnicodeError as error:
            raise ContractError("URL hostname 無法正規化為 IDNA") from error
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            pass
        else:
            raise ContractError("URL hostname 不得是 IP literal")
        try:
            port = parsed.port or 443
        except ValueError as error:
            raise ContractError("URL port 無效") from error
        if port != 443:
            raise ContractError("URL port 必須是 443")
        if hostname not in allowed_hosts:
            raise ContractError(f"URL hostname 不在 allowlist：{hostname}")
        if any(is_signed_query_key(key) for key, _ in parse_qsl(parsed.query, keep_blank_values=True)):
            raise ContractError("URL 不得包含 signed credential query")
        request_target = parsed.path or "/"
        if parsed.query:
            request_target += f"?{parsed.query}"
        normalized_url = urlunsplit(("https", hostname, parsed.path, parsed.query, ""))
        return ValidatedURL(normalized_url, hostname, port, request_target, normalized_url)


def resolve_public_addresses(hostname: str) -> tuple[str, ...]:
    raise NotImplementedError


class PinnedHTTPSClient:
    def __init__(
        self,
        resolver: Resolver = resolve_public_addresses,
        connector: Connector | None = None,
        *,
        timeout_seconds: float = 30.0,
        max_redirects: int = 5,
    ) -> None:
        self.resolver = resolver
        self.connector = connector
        self.timeout_seconds = timeout_seconds
        self.max_redirects = max_redirects

    def download(self, request: DownloadRequest, target: BinaryIO) -> DownloadResult:
        raise NotImplementedError
