from dataclasses import dataclass
import hashlib
import http.client
import ipaddress
import socket
import ssl
from typing import BinaryIO, Protocol
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
import xml.etree.ElementTree as ElementTree

from .model import ContractError, IntegrityError, RemoteError


SIGNED_QUERY_KEYS = frozenset({"token", "sig", "signature", "expires"})
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
ABSOLUTE_MAX_SIZE = 100 * 1024 * 1024
NAT64_WELL_KNOWN_PREFIX = ipaddress.IPv6Network("64:ff9b::/96")


def is_signed_query_key(key: str) -> bool:
    normalized = key.casefold()
    return (
        normalized in SIGNED_QUERY_KEYS
        or normalized.startswith("x-amz-")
        or normalized.startswith("x-goog-")
    )


def _contains_control(value: str) -> bool:
    return any(ord(character) <= 0x20 or ord(character) == 0x7F for character in value)


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


def _canonical_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    parsed = ipaddress.ip_address(value)
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        return parsed.ipv4_mapped
    return parsed


def _is_allowed_public_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    if (
        not address.is_global
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
        or address.is_loopback
        or address.is_private
        or address.is_link_local
    ):
        return False
    embedded_ipv4 = None
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            embedded_ipv4 = address.ipv4_mapped
        elif address in NAT64_WELL_KNOWN_PREFIX:
            embedded_ipv4 = ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    return embedded_ipv4 is None or _is_allowed_public_address(embedded_ipv4)


class _StreamingSignatureVerifier:
    def __init__(self, media_type: str):
        if media_type not in {"application/pdf", "image/png", "image/svg+xml"}:
            raise ContractError(f"不支援的 media_type：{media_type}")
        self.media_type = media_type
        self.prefix = bytearray()
        self.scan_tail = b""
        self.root_tag: str | None = None
        self.parser = (
            ElementTree.XMLPullParser(events=("start",))
            if media_type == "image/svg+xml"
            else None
        )

    def feed(self, chunk: bytes) -> None:
        if len(self.prefix) < 8:
            self.prefix.extend(chunk[: 8 - len(self.prefix)])
        if self.parser is None:
            return
        declaration_scan = (self.scan_tail + chunk).replace(b"\x00", b"").upper()
        if b"<!DOCTYPE" in declaration_scan:
            raise IntegrityError("SVG 不得包含 DOCTYPE")
        if b"<!ENTITY" in declaration_scan:
            raise IntegrityError("SVG 不得包含 ENTITY")
        self.scan_tail = (self.scan_tail + chunk)[-64:]
        try:
            self.parser.feed(chunk)
            self._consume_root()
        except (ElementTree.ParseError, LookupError) as error:
            raise IntegrityError("SVG XML 無法解析") from error

    def finish(self) -> None:
        if self.media_type == "application/pdf":
            if not bytes(self.prefix).startswith(b"%PDF-"):
                raise IntegrityError("下載內容不是有效的 PDF")
            return
        if self.media_type == "image/png":
            if bytes(self.prefix) != b"\x89PNG\r\n\x1a\n":
                raise IntegrityError("下載內容不是有效的 PNG")
            return
        try:
            self.parser.close()
            self._consume_root()
        except (ElementTree.ParseError, LookupError) as error:
            raise IntegrityError("SVG XML 無法解析") from error
        if self.root_tag is None or self.root_tag.rsplit("}", 1)[-1] != "svg":
            raise IntegrityError("SVG 根元素必須是 svg")

    def _consume_root(self) -> None:
        if self.root_tag is not None:
            return
        for _event, element in self.parser.read_events():
            self.root_tag = element.tag
            break


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


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        *,
        ip_address: str,
        server_hostname: str,
        port: int,
        timeout_seconds: float,
        context: ssl.SSLContext,
    ) -> None:
        super().__init__(
            server_hostname,
            port=port,
            timeout=timeout_seconds,
            context=context,
        )
        self._ip_address = ip_address
        self._server_hostname = server_hostname

    @property
    def peer_ip(self) -> str:
        if self.sock is None:
            raise OSError("HTTPS socket 尚未連線")
        return self.sock.getpeername()[0]

    def connect(self) -> None:
        raw_socket = socket.create_connection(
            (self._ip_address, self.port),
            self.timeout,
            self.source_address,
        )
        try:
            self.sock = self._context.wrap_socket(
                raw_socket,
                server_hostname=self._server_hostname,
            )
        except BaseException:
            raw_socket.close()
            raise


class PinnedTLSConnector:
    def connect(
        self,
        *,
        ip_address: str,
        server_hostname: str,
        port: int,
        timeout_seconds: float,
    ) -> ConnectedHTTPS:
        connection = _PinnedHTTPSConnection(
            ip_address=ip_address,
            server_hostname=server_hostname,
            port=port,
            timeout_seconds=timeout_seconds,
            context=ssl.create_default_context(),
        )
        connection.connect()
        return connection


class Resolver(Protocol):
    def __call__(self, hostname: str) -> tuple[str, ...]: ...


class Transport(Protocol):
    def download(self, request: DownloadRequest, target: BinaryIO) -> DownloadResult: ...


class URLPolicy:
    @staticmethod
    def parse(url: str, allowed_hosts: frozenset[str]) -> ValidatedURL:
        if not isinstance(url, str) or _contains_control(url):
            raise ContractError("URL 不得包含控制字元")
        try:
            parsed = urlsplit(url)
            username = parsed.username
            password = parsed.password
            raw_hostname = (parsed.hostname or "").rstrip(".")
            port = parsed.port or 443
        except ValueError as error:
            raise ContractError("URL authority 或 port 無效") from error
        if parsed.scheme != "https":
            raise ContractError("URL 必須使用 HTTPS")
        if username is not None or password is not None:
            raise ContractError("URL 不得包含 user-info")
        if parsed.fragment:
            raise ContractError("URL 不得包含 fragment")
        if not raw_hostname:
            raise ContractError("URL hostname 不得為空")
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
    answers = socket.getaddrinfo(
        hostname,
        443,
        type=socket.SOCK_STREAM,
        proto=socket.IPPROTO_TCP,
    )
    addresses = []
    for answer in answers:
        address = answer[4][0]
        if address not in addresses:
            addresses.append(address)
    return tuple(addresses)


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
        self.connector = connector if connector is not None else PinnedTLSConnector()
        self.timeout_seconds = timeout_seconds
        self.max_redirects = max_redirects

    def download(self, request: DownloadRequest, target: BinaryIO) -> DownloadResult:
        return self._download(request, target)

    def _download(self, request: DownloadRequest, target: BinaryIO) -> DownloadResult:
        current_url = request.url
        redirects = 0
        seen_urls: set[str] = set()
        while True:
            validated = URLPolicy.parse(current_url, request.allowed_hosts)
            if validated.normalized_url in seen_urls:
                raise ContractError("HTTP redirect loop")
            seen_urls.add(validated.normalized_url)
            try:
                addresses = self.resolver(validated.hostname)
            except OSError as error:
                raise RemoteError(f"DNS 查詢失敗：{validated.hostname}") from error
            if not addresses:
                raise ContractError(f"DNS 未回傳位址：{validated.hostname}")
            try:
                parsed_addresses = tuple(ipaddress.ip_address(address) for address in addresses)
            except ValueError as error:
                raise ContractError(f"DNS 回傳無效位址：{validated.hostname}") from error
            if not all(_is_allowed_public_address(address) for address in parsed_addresses):
                raise ContractError(f"DNS 位址必須全部是公網位址：{validated.hostname}")
            if self.connector is None:
                raise ContractError("尚未設定 HTTPS connector")
            selected_ip = str(parsed_addresses[0])
            try:
                connection = self.connector.connect(
                    ip_address=selected_ip,
                    server_hostname=validated.hostname,
                    port=validated.port,
                    timeout_seconds=self.timeout_seconds,
                )
            except OSError as error:
                raise RemoteError(f"HTTPS 連線失敗：{validated.redacted_display}") from error
            redirect_url = None
            try:
                try:
                    peer = _canonical_ip(connection.peer_ip)
                except ValueError as error:
                    raise ContractError("HTTPS peer 回傳無效 IP") from error
                except (OSError, http.client.HTTPException) as error:
                    raise RemoteError(
                        f"無法取得 HTTPS peer：{validated.redacted_display}"
                    ) from error
                if peer != _canonical_ip(selected_ip):
                    raise ContractError("HTTPS peer 與 DNS pinned IP 不符")
                try:
                    connection.request(
                        "GET",
                        validated.request_target,
                        {"Host": validated.hostname, "Accept-Encoding": "identity"},
                    )
                    response = connection.getresponse()
                except (OSError, http.client.HTTPException) as error:
                    raise RemoteError(
                        f"HTTPS request 失敗：{validated.redacted_display}"
                    ) from error
                if response.status in REDIRECT_STATUSES:
                    location = response.getheader("Location")
                    if location is None:
                        raise ContractError("HTTP redirect 缺少 Location")
                    if _contains_control(location):
                        raise ContractError("HTTP redirect Location 不得包含控制字元")
                    if redirects >= self.max_redirects:
                        raise ContractError("HTTP redirect 超過允許跳數")
                    redirect_url = urljoin(validated.normalized_url, location)
                else:
                    if response.status != 200:
                        raise RemoteHTTPError(response.status, validated.normalized_url)
                    if response.getheader("Content-Range") is not None:
                        raise ContractError("HTTP 200 response 不得包含 Content-Range")
                    content_encoding = response.getheader("Content-Encoding")
                    if content_encoding is not None and content_encoding.strip().casefold() != "identity":
                        raise ContractError("HTTP Content-Encoding 必須是 identity")
                    content_length_header = response.getheader("Content-Length")
                    declared_length = None
                    if content_length_header is not None:
                        if (
                            not content_length_header.isascii()
                            or not content_length_header.isdigit()
                        ):
                            raise ContractError("HTTP Content-Length 必須是十進位整數")
                        try:
                            content_length = int(content_length_header, 10)
                        except ValueError as error:
                            raise ContractError("HTTP Content-Length 必須是十進位整數") from error
                        if content_length < 0:
                            raise ContractError("HTTP Content-Length 不得為負數")
                        declared_length = content_length
                        if content_length > min(request.max_size, ABSOLUTE_MAX_SIZE):
                            raise IntegrityError("HTTP Content-Length 超過檔案上限")
                        if (
                            request.expected_size is not None
                            and content_length != request.expected_size
                        ):
                            raise IntegrityError("HTTP Content-Length 與 manifest size_bytes 不符")
                    digest = hashlib.sha256()
                    size = 0
                    signature = _StreamingSignatureVerifier(request.expected_media_type)
                    effective_limit = min(request.max_size, ABSOLUTE_MAX_SIZE)
                    if request.expected_size is not None:
                        effective_limit = min(effective_limit, request.expected_size)
                    if declared_length is not None:
                        effective_limit = min(effective_limit, declared_length)
                    while True:
                        try:
                            chunk = response.read(
                                min(64 * 1024, effective_limit - size + 1)
                            )
                        except (OSError, http.client.HTTPException) as error:
                            raise RemoteError(
                                f"HTTPS response 讀取失敗：{validated.redacted_display}"
                            ) from error
                        if not chunk:
                            break
                        if size + len(chunk) > effective_limit:
                            raise IntegrityError("下載內容超過允許大小")
                        signature.feed(chunk)
                        try:
                            written = target.write(chunk)
                        except OSError as error:
                            raise ContractError("無法寫入本機暫存檔") from error
                        if written != len(chunk):
                            raise ContractError("本機暫存檔發生短寫入")
                        digest.update(chunk)
                        size += len(chunk)
                    if request.expected_size is not None and size != request.expected_size:
                        raise IntegrityError("下載大小與 manifest size_bytes 不符")
                    if declared_length is not None and size != declared_length:
                        raise IntegrityError("下載大小與 HTTP Content-Length 不符")
                    signature.finish()
                    sha256 = digest.hexdigest()
                    if request.expected_sha256 is not None and sha256 != request.expected_sha256:
                        raise IntegrityError("下載 SHA-256 與 manifest 不符")
                    return DownloadResult(
                        final_url=validated.normalized_url,
                        size_bytes=size,
                        sha256=sha256,
                        content_type=response.getheader("Content-Type"),
                        status=response.status,
                    )
            finally:
                connection.close()
            redirects += 1
            current_url = redirect_url
