"""Kitty downloads audio from the Internet Archive and nowhere else.

``download_audio`` keeps the original script's shape: yt-dlp format
``bestaudio/best``, ``noplaylist``, and the same DRM / failure handling.
A validator runs before yt-dlp is called, and yt-dlp itself is limited to
the archive.org extractor.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import yt_dlp
from yt_dlp.utils import DownloadError

ALLOWED_HOSTS = frozenset({"archive.org", "www.archive.org"})
ALLOWED_PREFIXES = frozenset({"details", "download"})
# yt-dlp matches allowed_extractors as regular expressions against IE_NAME.
# The dot is escaped so a lookalike name cannot slip through.
ARCHIVE_EXTRACTOR_PATTERN = r"archive\.org"
ARCHIVE_EXTRACTOR_KEY = "ArchiveOrg"
ARCHIVE_EXTRACTOR_NAME = "archive.org"
_MAX_URL_LENGTH = 4096
_NETLOC_RE = re.compile(
    r"(?:www\.archive\.org|archive\.org)\.?(?::443)?",
    re.IGNORECASE,
)
_HINT = (
    "Kitty only downloads from the Internet Archive. "
    "Use an https://archive.org or https://www.archive.org link "
    "under /details/ or /download/."
)


class ArchiveUrlError(ValueError):
    """The URL is not an allowed Internet Archive details or download link."""


def _msg(reason: str) -> str:
    return f"{reason} {_HINT}"


def _fully_unquote(value: str) -> str:
    """Decode percent-escapes repeatedly so ``%252e%252e`` cannot hide ``..``."""
    current = value
    for _ in range(5):
        decoded = unquote(current)
        if decoded == current:
            return current
        current = decoded
    return current


def _bad_chars(value: str) -> bool:
    return any(
        character == "\\" or unicodedata.category(character) in {"Cc", "Cf"}
        for character in value
    )


def _allowed_host(hostname: str | None) -> str | None:
    if not hostname or not hostname.isascii():
        return None
    host = hostname.lower()
    if host.endswith("."):
        host = host[:-1]
    if host not in ALLOWED_HOSTS:
        return None
    return host


def _segments(path: str) -> list[str]:
    if _bad_chars(path):
        raise ArchiveUrlError(_msg("That link contains characters Kitty won't accept."))
    decoded = _fully_unquote(path)
    if _bad_chars(decoded) or any(character.isspace() and character != " " for character in decoded):
        raise ArchiveUrlError(_msg("That link contains characters Kitty won't accept."))
    segments: list[str] = []
    for segment in decoded.split("/"):
        if segment == "":
            continue
        normalized = unicodedata.normalize("NFKC", segment)
        if normalized == ".":
            continue
        if (
            normalized == ".."
            or "/" in normalized
            or "\\" in normalized
            or _bad_chars(normalized)
        ):
            raise ArchiveUrlError(_msg("That path isn't allowed."))
        segments.append(segment)
    if len(segments) < 2 or segments[0].casefold() not in ALLOWED_PREFIXES:
        raise ArchiveUrlError(
            _msg("The path must be under /details/ or /download/.")
        )
    if not segments[1].strip("."):
        raise ArchiveUrlError(_msg("The path must be under /details/ or /download/."))
    return segments


def validate_archive_url(url: str) -> str:
    """Return a canonical archive.org URL, or raise ArchiveUrlError.

    Accepted URLs are https, host ``archive.org`` or ``www.archive.org``,
    and a path under ``/details/`` or ``/download/``. Lookalike hosts,
    userinfo (``user@host``), and non-443 ports are rejected.
    """
    if not isinstance(url, str):
        raise ArchiveUrlError(_msg("Enter an Internet Archive link."))
    candidate = url.strip()
    if not candidate:
        raise ArchiveUrlError(_msg("Enter an Internet Archive link."))
    if len(candidate) > _MAX_URL_LENGTH:
        raise ArchiveUrlError(_msg("That link is too long."))
    if any(character.isspace() for character in candidate) or _bad_chars(candidate):
        raise ArchiveUrlError(_msg("That link contains characters Kitty won't accept."))
    if "\\" in candidate:
        raise ArchiveUrlError(_msg("That link contains characters Kitty won't accept."))
    if not candidate.lower().startswith("https://"):
        raise ArchiveUrlError(_msg("Only https links are allowed."))

    try:
        parts = urlsplit(candidate)
    except ValueError as exc:
        raise ArchiveUrlError(
            _msg("That host isn't archive.org or www.archive.org. Lookalike hosts are blocked.")
        ) from exc

    if parts.scheme.lower() != "https":
        raise ArchiveUrlError(_msg("Only https links are allowed."))

    netloc = parts.netloc
    if (
        "@" in netloc
        or parts.username is not None
        or parts.password is not None
    ):
        raise ArchiveUrlError(
            _msg("Links with a username, password, or extra @ are blocked.")
        )
    if "%" in netloc or "\\" in netloc:
        raise ArchiveUrlError(
            _msg("That host isn't archive.org or www.archive.org. Lookalike hosts are blocked.")
        )

    try:
        port = parts.port
    except ValueError as exc:
        raise ArchiveUrlError(
            _msg("Only the standard https port (443) is allowed.")
        ) from exc

    host = _allowed_host(parts.hostname)
    if host is None:
        raise ArchiveUrlError(
            _msg("That host isn't archive.org or www.archive.org. Lookalike hosts are blocked.")
        )
    if port not in (None, 443) or (":" in netloc and port != 443):
        raise ArchiveUrlError(_msg("Only the standard https port (443) is allowed."))
    if not _NETLOC_RE.fullmatch(netloc):
        raise ArchiveUrlError(
            _msg("That host isn't archive.org or www.archive.org. Lookalike hosts are blocked.")
        )

    segments = _segments(parts.path or "")
    if parts.query and (
        _bad_chars(parts.query) or _bad_chars(_fully_unquote(parts.query))
    ):
        raise ArchiveUrlError(_msg("That link contains characters Kitty won't accept."))

    prefix = segments[0].casefold()
    quoted_rest = [quote(segment, safe="-._~") for segment in segments[1:]]
    canonical_path = "/" + prefix + "/" + "/".join(quoted_rest)
    canonical = urlunsplit(("https", host, canonical_path, parts.query, ""))
    return canonical


def _to_details_url(canonical: str) -> str:
    """Point /download/ links at /details/ so the archive.org extractor matches.

    ArchiveOrgIE only claims /details/ and /embed/ URLs. Direct /download/
    file links are the same item, so they are rewritten after validation and
    checked again. The host is unchanged.
    """
    parts = urlsplit(canonical)
    path = parts.path
    if path == "/download" or path.startswith("/download/"):
        path = "/details" + path[len("/download") :]
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


def _extractor_is_archive(info: object) -> bool:
    if not isinstance(info, dict):
        return False
    key = info.get("extractor_key")
    name = info.get("extractor")
    if not isinstance(key, str) or not isinstance(name, str):
        return False
    return key == ARCHIVE_EXTRACTOR_KEY and name.lower() == ARCHIVE_EXTRACTOR_NAME


def download_audio(
    url,
    output_path,
    progress_callback: Callable[[dict], None] | None = None,
    status_callback: Callable[[str], None] | None = None,
    saved_paths: list[str] | None = None,
):
    """Download one Internet Archive audio item.

    Returns True on success and False when the link is refused, the track is
    DRM-protected, or yt-dlp reports a failure. Nothing calls yt-dlp until
    ``validate_archive_url`` has accepted the link.
    """

    def report(message: str) -> None:
        print(message)
        if status_callback is not None:
            status_callback(message)

    try:
        canonical = validate_archive_url(url)
        fetch_url = validate_archive_url(_to_details_url(canonical))
    except ArchiveUrlError as exc:
        report(f"Download failed: {exc}")
        return False

    def _hook(status: dict) -> None:
        if (
            isinstance(status, dict)
            and status.get("status") == "finished"
            and saved_paths is not None
        ):
            filename = status.get("filename")
            if isinstance(filename, str) and filename:
                saved_paths.append(filename)
        if progress_callback is not None and isinstance(status, dict):
            progress_callback(status)

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": output_path,
        "noplaylist": True,
        "quiet": False,
        "allowed_extractors": [ARCHIVE_EXTRACTOR_PATTERN],
        "progress_hooks": [_hook],
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(fetch_url, download=False, ie_key=ARCHIVE_EXTRACTOR_KEY)
            if not _extractor_is_archive(info):
                report(
                    "Download failed: Kitty refused this link because the "
                    "Internet Archive extractor did not accept it."
                )
                return False
            result = ydl.download([fetch_url])
        if result not in (0, None):
            report("Download failed: The download did not finish.")
            return False
        report(f"Successfully downloaded: {url}")
        return True
    except DownloadError as exc:
        error_message = str(exc)
        if "DRM protected" in error_message:
            report(f"Skipping DRM-protected track: {url}")
            return False
        report(f"Download failed: {error_message}")
        return False
    except Exception as exc:
        report(f"Unexpected error: {exc}")
        return False


def main() -> None:
    url = input("Enter URL: ")
    output_path = "downloads/%(title)s.%(ext)s"
    download_audio(url, output_path)


if __name__ == "__main__":
    main()
