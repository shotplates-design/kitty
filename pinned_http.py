"""HTTP handler that only talks to Internet Archive addresses.

Every hop is checked: the host must be archive.org, www.archive.org, or
*.us.archive.org, and every resolved address must be a public unicast
address. The connection uses the address from that lookup, so a later DNS
answer cannot swap in a loopback or private address.
"""

from __future__ import annotations

import ipaddress
import socket
import ssl
import urllib.parse
from dataclasses import dataclass
from email.message import Message
from http import HTTPStatus
from http.client import HTTPConnection, HTTPException, HTTPResponse

from yt_dlp.networking._helper import make_ssl_context
from yt_dlp.networking.common import Request, RequestHandler, Response
from yt_dlp.networking.exceptions import RequestError, TransportError

MAX_REDIRECTS = 10
_ALLOWED_EXACT_HOSTS = frozenset({"archive.org", "www.archive.org"})
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
# File bytes 302 to a storage node. Older nodes are ia######.us.archive.org.
# Current /download/ redirects use dn######.ca.archive.org.
_STORAGE_SUFFIXES = (".us.archive.org", ".ca.archive.org")


@dataclass(frozen=True)
class PinnedAddress:
    """One resolved address that has already passed the public-address check."""

    host: str
    ip: str
    port: int
    family: socket.AddressFamily
    sockaddr: tuple


class ArchiveRequestError(RequestError):
    """A hop tried to leave the Internet Archive or use a non-public address."""


def _reason(url: str, detail: str) -> str:
    return f"Kitty refused {url}: {detail}"


def _status_phrase(status: int) -> str | None:
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return None


def _dns_label(label: str) -> bool:
    if not label or len(label) > 63:
        return False
    if label.startswith("-") or label.endswith("-"):
        return False
    return all(character.isalnum() or character == "-" for character in label)


def _hostname_allowed(hostname: str | None) -> bool:
    if not hostname or not hostname.isascii():
        return False
    host = hostname.lower().rstrip(".")
    if host in _ALLOWED_EXACT_HOSTS:
        return True
    for suffix in _STORAGE_SUFFIXES:
        if not host.endswith(suffix):
            continue
        prefix = host[: -len(suffix)]
        return bool(prefix) and all(_dns_label(part) for part in prefix.split("."))
    return False


def _blocked_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return _blocked_address(ip.ipv4_mapped)
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link-local"
    if ip.is_private or ip.is_multicast or ip.is_unspecified or ip.is_reserved or not ip.is_global:
        return "private"
    return None


def pin_archive_url(url: str, *, resolve=None) -> PinnedAddress:
    """Resolve ``url`` once and return the public address to connect to."""
    if resolve is None:
        resolve = socket.getaddrinfo
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError as exc:
        raise ArchiveRequestError(_reason(url, "the URL is not a valid archive.org link")) from exc
    if parts.scheme not in {"http", "https"}:
        raise ArchiveRequestError(_reason(url, "only http and https are allowed"))
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise ArchiveRequestError(_reason(url, "links with a username or password are blocked"))
    if not _hostname_allowed(parts.hostname):
        raise ArchiveRequestError(
            _reason(
                url,
                "only archive.org, www.archive.org, and *.us.archive.org are allowed",
            )
        )
    port = parts.port or (443 if parts.scheme == "https" else 80)
    if port not in {80, 443}:
        raise ArchiveRequestError(_reason(url, f"port {port} is not allowed"))

    host = parts.hostname.lower().rstrip(".")
    try:
        answers = resolve(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ArchiveRequestError(_reason(url, f"could not resolve {host}")) from exc
    if not answers:
        raise ArchiveRequestError(_reason(url, f"could not resolve {host}"))

    pinned: PinnedAddress | None = None
    for family, _socktype, _proto, _canon, sockaddr in answers:
        ip_text = sockaddr[0]
        try:
            parsed = ipaddress.ip_address(ip_text)
        except ValueError as exc:
            raise ArchiveRequestError(_reason(url, f"address {ip_text} is not usable")) from exc
        kind = _blocked_address(parsed)
        if kind is not None:
            raise ArchiveRequestError(_reason(url, f"address {ip_text} is {kind}"))
        if pinned is None:
            pinned = PinnedAddress(
                host=host,
                ip=ip_text,
                port=port,
                family=family,
                sockaddr=sockaddr,
            )
    assert pinned is not None
    return pinned


class _PinnedConnection(HTTPConnection):
    """HTTP connection that dials a previously checked address."""

    def __init__(self, host: str, port: int, timeout: float, pinned: PinnedAddress, context: ssl.SSLContext | None):
        super().__init__(host, port, timeout=timeout)
        self._pinned = pinned
        self._context = context

    def connect(self) -> None:
        sock = socket.socket(self._pinned.family, socket.SOCK_STREAM)
        if self.timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
            sock.settimeout(self.timeout)
        try:
            sock.connect(self._pinned.sockaddr)
            if self._context is not None:
                sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise
        self.sock = sock


class PinnedArchiveRH(RequestHandler):
    """yt-dlp handler that checks and pins every request, including redirects."""

    _SUPPORTED_URL_SCHEMES = ("http", "https")
    _SUPPORTED_PROXY_SCHEMES = None
    _SUPPORTED_FEATURES = None

    def _check_extensions(self, extensions: dict) -> None:
        super()._check_extensions(extensions)
        extensions.pop("cookiejar", None)
        extensions.pop("timeout", None)
        extensions.pop("legacy_ssl", None)
        extensions.pop("keep_header_casing", None)

    def _reject_proxy(self, request: Request) -> None:
        proxies = self._get_proxies(request)
        for key, value in proxies.items():
            if key == "no" or not value:
                continue
            raise ArchiveRequestError("Kitty does not send Internet Archive requests through a proxy.")

    def _exchange(
        self,
        pinned: PinnedAddress,
        url: str,
        method: str,
        headers: dict,
        data: bytes | None,
        timeout: float,
    ) -> Response:
        parts = urllib.parse.urlsplit(url)
        context = None
        if parts.scheme == "https":
            context = make_ssl_context(verify=self.verify)
        connection = _PinnedConnection(
            pinned.host,
            pinned.port,
            timeout,
            pinned,
            context,
        )
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        try:
            connection.request(method, path, body=data, headers=headers)
            raw = connection.getresponse()
        except (OSError, HTTPException, ssl.SSLError) as exc:
            connection.close()
            raise TransportError(cause=exc) from exc
        return _StreamingResponse(raw, connection, url)

    def _send(self, request: Request) -> Response:
        self._reject_proxy(request)
        url = request.url
        method = request.method
        data = request.data if isinstance(request.data, bytes) else None
        headers = dict(self._get_headers(request))
        headers["Accept-Encoding"] = "identity"
        timeout = self._calculate_timeout(request)
        for _hop in range(MAX_REDIRECTS):
            pinned = pin_archive_url(url)
            response = self._exchange(pinned, url, method, headers, data, timeout)
            status = response.status
            if status not in _REDIRECT_STATUSES:
                return response
            location = response.get_header("Location")
            response.close()
            if not location:
                raise ArchiveRequestError(_reason(url, "the redirect has no Location"))
            url = urllib.parse.urljoin(url, location)
            if status in {301, 302, 303}:
                method = "GET"
                data = None
                headers.pop("Content-Length", None)
                headers.pop("Content-Type", None)
        raise ArchiveRequestError(_reason(request.url, "too many redirects"))


class _StreamingResponse(Response):
    """Response that leaves the body on the socket until yt-dlp reads it."""

    def __init__(self, raw: HTTPResponse, connection: HTTPConnection, url: str):
        headers = Message()
        for name, value in raw.getheaders():
            headers[name] = value
        super().__init__(
            fp=raw,
            headers=headers,
            url=url,
            status=raw.status,
            reason=raw.reason or _status_phrase(raw.status),
        )
        self._connection = connection

    def close(self) -> None:
        super().close()
        self._connection.close()
