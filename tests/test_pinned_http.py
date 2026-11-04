import io
import socket

import pytest

from pinned_http import (
    ArchiveRequestError,
    PinnedArchiveRH,
    _PinnedConnection,
    pin_archive_url,
)
from yt_dlp.networking.common import Request, Response


class _Logger:
    def debug(self, message):
        return None

    def warning(self, message):
        return None

    def error(self, message):
        return None

    def stdout(self, message):
        return None

    def stderr(self, message):
        return None


def _answer(ip, port=443, family=socket.AF_INET):
    if family == socket.AF_INET6:
        sockaddr = (ip, port, 0, 0)
    else:
        sockaddr = (ip, port)
    return [(family, socket.SOCK_STREAM, 6, "", sockaddr)]


def _resolver(mapping):
    def resolve(host, port, *args, **kwargs):
        if host not in mapping:
            raise socket.gaierror(host)
        value = mapping[host]
        if isinstance(value, BaseException):
            raise value
        if callable(value):
            return value(host, port)
        return _answer(value, port)

    return resolve


@pytest.mark.parametrize(
    "host",
    ["ia801609.us.archive.org", "dn710807.ca.archive.org"],
)
def test_storage_host_with_a_public_address_is_pinned(host):
    pinned = pin_archive_url(
        f"https://{host}/0/items/item/file.mp3",
        resolve=_resolver({host: "1.1.1.1"}),
    )
    assert pinned.ip == "1.1.1.1"
    assert pinned.sockaddr == ("1.1.1.1", 443)
    assert pinned.host == host


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.com/download/item/file.mp3",
        "https://archive.org.evil.com/details/item",
        "https://web.archive.org/web/2020/https://example.com",
        "http://127.0.0.1/secret",
        "https://127.0.0.1/secret",
        "https://us.archive.org/download/item/file.mp3",
        "https://ca.archive.org/download/item/file.mp3",
        "https://dn710807.ca.archive.org.evil.com/file.mp3",
        "https://user:pass@archive.org/details/item",
        "https://archive.org:8443/details/item",
        "file:///etc/passwd",
    ],
)
def test_disallowed_hops_are_refused_before_connect(url):
    def explode(host, port, *args, **kwargs):
        raise AssertionError(f"resolved {host}")

    with pytest.raises(ArchiveRequestError, match="Kitty refused"):
        pin_archive_url(url, resolve=explode)


@pytest.mark.parametrize(
    ("ip", "kind"),
    [
        ("127.0.0.1", "loopback"),
        ("10.1.2.3", "private"),
        ("192.168.0.9", "private"),
        ("169.254.1.1", "link-local"),
        ("0.0.0.0", "private"),
        ("100.64.1.1", "private"),
        ("::1", "loopback"),
        ("fe80::1", "link-local"),
        ("::ffff:127.0.0.1", "loopback"),
    ],
)
def test_non_public_answers_are_refused(ip, kind):
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    with pytest.raises(ArchiveRequestError, match=kind):
        pin_archive_url(
            "https://archive.org/details/item",
            resolve=_resolver({"archive.org": lambda host, port: _answer(ip, port, family)}),
        )


def test_mixed_public_and_loopback_answers_are_refused():
    def resolve(host, port, *args, **kwargs):
        return _answer("1.1.1.1", port) + _answer("127.0.0.1", port)

    with pytest.raises(ArchiveRequestError, match="loopback"):
        pin_archive_url("https://archive.org/details/item", resolve=resolve)


def test_redirect_to_localhost_is_not_followed(monkeypatch):
    lookups = []

    def resolve(host, port, *args, **kwargs):
        lookups.append(host)
        if host == "archive.org":
            return _answer("1.1.1.1", port)
        if host == "127.0.0.1":
            return _answer("127.0.0.1", port)
        raise AssertionError(host)

    monkeypatch.setattr("pinned_http.socket.getaddrinfo", resolve)
    exchanged = []

    def exchange(self, pinned, url, method, headers, data, timeout):
        exchanged.append((pinned.ip, url))
        if pinned.ip != "1.1.1.1":
            raise AssertionError(f"connected to {pinned.ip}")
        return Response(
            io.BytesIO(b""),
            url=url,
            headers={"Location": "http://127.0.0.1/secret"},
            status=302,
        )

    monkeypatch.setattr(PinnedArchiveRH, "_exchange", exchange)
    handler = PinnedArchiveRH(logger=_Logger())
    with pytest.raises(ArchiveRequestError, match="127.0.0.1"):
        handler.send(Request("https://archive.org/download/item/file.mp3"))
    assert exchanged == [("1.1.1.1", "https://archive.org/download/item/file.mp3")]
    assert "127.0.0.1" not in lookups


def test_redirect_to_storage_uses_the_pinned_address(monkeypatch):
    def resolve(host, port, *args, **kwargs):
        addresses = {
            "archive.org": "1.1.1.1",
            "ia800.us.archive.org": "8.8.8.8",
        }
        return _answer(addresses[host], port)

    monkeypatch.setattr("pinned_http.socket.getaddrinfo", resolve)
    exchanged = []

    def exchange(self, pinned, url, method, headers, data, timeout):
        exchanged.append((pinned.ip, pinned.sockaddr, url))
        if pinned.ip == "1.1.1.1":
            return Response(
                io.BytesIO(b""),
                url=url,
                headers={"Location": "https://ia800.us.archive.org/0/items/item/file.mp3"},
                status=302,
            )
        return Response(io.BytesIO(b"audio"), url=url, headers={}, status=200)

    monkeypatch.setattr(PinnedArchiveRH, "_exchange", exchange)
    handler = PinnedArchiveRH(logger=_Logger())
    response = handler.send(Request("https://archive.org/download/item/file.mp3"))
    assert response.status == 200
    assert response.read() == b"audio"
    assert exchanged[0][0] == "1.1.1.1"
    assert exchanged[1][0] == "8.8.8.8"
    assert exchanged[1][1] == ("8.8.8.8", 443)


def test_dns_rebind_cannot_change_the_connected_address(monkeypatch):
    calls = {"count": 0}

    def resolve(host, port, *args, **kwargs):
        calls["count"] += 1
        if calls["count"] > 1:
            return _answer("127.0.0.1", port)
        return _answer("1.1.1.1", port)

    monkeypatch.setattr("pinned_http.socket.getaddrinfo", resolve)
    seen = []

    def exchange(self, pinned, url, method, headers, data, timeout):
        seen.append(pinned.sockaddr)
        return Response(io.BytesIO(b"ok"), url=url, headers={}, status=200)

    monkeypatch.setattr(PinnedArchiveRH, "_exchange", exchange)
    handler = PinnedArchiveRH(logger=_Logger())
    response = handler.send(Request("https://www.archive.org/details/item"))
    assert response.status == 200
    assert seen == [("1.1.1.1", 443)]
    assert calls["count"] == 1


def test_connection_dials_the_pinned_sockaddr(monkeypatch):
    connected = []
    wrapped = []

    class FakeSocket:
        def __init__(self, family, socktype):
            self.family = family
            self.connected = None

        def settimeout(self, timeout):
            self.timeout = timeout

        def connect(self, address):
            self.connected = address
            connected.append(address)

        def close(self):
            return None

    class Context:
        def wrap_socket(self, sock, server_hostname=None):
            wrapped.append((sock.connected, server_hostname))
            return sock

    monkeypatch.setattr(socket, "socket", FakeSocket)
    connection = _PinnedConnection(
        "archive.org",
        443,
        timeout=5,
        pinned=pin_archive_url(
            "https://archive.org/details/item",
            resolve=_resolver({"archive.org": "1.1.1.1"}),
        ),
        context=Context(),
    )
    connection.connect()
    assert connected == [("1.1.1.1", 443)]
    assert wrapped == [(("1.1.1.1", 443), "archive.org")]


def test_request_director_installs_only_the_pinned_handler():
    import yt_dlp

    from kitty import install_pinned_handler

    ydl = yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True})
    install_pinned_handler(ydl)
    assert list(ydl._request_director.handlers) == ["PinnedArchive"]
