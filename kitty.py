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
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import yt_dlp
from yt_dlp.networking.exceptions import RequestError
from yt_dlp.utils import DownloadError

from pinned_http import PinnedArchiveRH

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
# Phone-sized caps so one item cannot fill Streamlit Cloud's memory or disk.
MAX_FILE_BYTES = 80 * 1024 * 1024
MAX_TOTAL_BYTES = 100 * 1024 * 1024
_PARTIAL_SUFFIXES = {".part", ".ytdl", ".temp"}


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


def install_pinned_handler(ydl) -> None:
    """Replace yt-dlp's HTTP stack with the pinned archive.org handler."""
    build = getattr(ydl, "build_request_director", None)
    if build is None or getattr(ydl, "_kitty_pinned", False):
        return

    def build_pinned(handlers, preferences=None):
        return build([PinnedArchiveRH], preferences)

    ydl.build_request_director = build_pinned
    ydl._kitty_pinned = True
    if "_request_director" in getattr(ydl, "__dict__", {}):
        ydl._request_director.close()
        del ydl._request_director


def _requested_filename(canonical: str) -> str | None:
    """File path after the item id, when the link names one file."""
    segments = [unquote(segment) for segment in urlsplit(canonical).path.split("/") if segment]
    if len(segments) < 3:
        return None
    return "/".join(segments[2:])


def _entry_names(entry: dict) -> set[str]:
    names: set[str] = set()
    for key in ("id", "display_id", "title"):
        value = entry.get(key)
        if isinstance(value, str) and value:
            names.add(value)
            names.add(value.rsplit("/", 1)[-1])
    for fmt in entry.get("formats") or []:
        if not isinstance(fmt, dict):
            continue
        raw_url = fmt.get("url")
        if isinstance(raw_url, str):
            path = unquote(urlsplit(raw_url).path)
            names.add(path.rsplit("/", 1)[-1])
    url = entry.get("url")
    if isinstance(url, str) and not url.startswith("http"):
        names.add(url.rsplit("/", 1)[-1])
    elif isinstance(url, str):
        names.add(unquote(urlsplit(url).path).rsplit("/", 1)[-1])
    return {name for name in names if name}


def _entry_matches(entry: dict, requested: str) -> bool:
    requested_name = requested.rsplit("/", 1)[-1]
    names = _entry_names(entry)
    folded = {name.casefold() for name in names}
    return (
        requested in names
        or requested_name in names
        or requested.casefold() in folded
        or requested_name.casefold() in folded
    )


def _as_video(entry: dict, parent: dict) -> dict:
    video = dict(entry)
    video["_type"] = "video"
    for key in ("extractor", "extractor_key", "webpage_url"):
        if not video.get(key) and parent.get(key):
            video[key] = parent[key]
    return video


def select_download(info: dict, requested_file: str | None) -> dict | None:
    """Keep one video dict from an item page or a named file."""
    if info.get("_type") not in {"playlist", "multi_video"}:
        return info
    entries = [entry for entry in info.get("entries") or [] if isinstance(entry, dict)]
    if requested_file:
        matches = [entry for entry in entries if _entry_matches(entry, requested_file)]
        if len(matches) != 1:
            exact = [
                entry
                for entry in matches
                if requested_file in _entry_names(entry)
                or requested_file.rsplit("/", 1)[-1] in _entry_names(entry)
            ]
            if len(exact) == 1:
                matches = exact
            else:
                return None
        return _as_video(matches[0], info)
    if not entries:
        return None
    return _as_video(entries[0], info)


def declared_size(info: dict) -> int | None:
    for key in ("filesize", "filesize_approx"):
        value = info.get(key)
        if isinstance(value, int) and value >= 0:
            return value
    return None


def _media_files(root: Path) -> set[Path]:
    if not root.exists():
        return set()
    found: set[Path] = set()
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix in _PARTIAL_SUFFIXES or path.name.endswith(".part"):
            continue
        found.add(path.resolve())
    return found


def directory_size(root: Path) -> int:
    if not root.exists():
        return 0
    total = 0
    for path in root.rglob("*"):
        if path.is_file():
            total += path.stat().st_size
    return total


def cleanup_partials(root: Path) -> None:
    if not root.is_dir():
        return
    for path in root.rglob("*"):
        if path.is_file() and (
            path.suffix in _PARTIAL_SUFFIXES or path.name.endswith(".part")
        ):
            path.unlink(missing_ok=True)


def _output_directory(output_path: str) -> Path:
    return Path(output_path).parent


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

    requested_file = _requested_filename(canonical)
    output_dir = _output_directory(output_path)
    running_sizes: dict[str, int] = {}
    finished = {"ok": False}

    def _hook(status: dict) -> None:
        if isinstance(status, dict) and status.get("status") == "finished":
            finished["ok"] = True
        if isinstance(status, dict) and status.get("status") == "downloading":
            key = status.get("filename") or status.get("tmpfilename") or "current"
            amount = status.get("downloaded_bytes") or 0
            if isinstance(key, str) and isinstance(amount, int):
                running_sizes[key] = amount
                if amount > MAX_FILE_BYTES or sum(running_sizes.values()) > MAX_TOTAL_BYTES:
                    raise DownloadError(
                        "That audio is larger than Kitty's limit "
                        f"of {MAX_FILE_BYTES} bytes per file and {MAX_TOTAL_BYTES} bytes overall."
                    )
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
        "max_filesize": MAX_FILE_BYTES,
    }
    if requested_file is None:
        # A bare /details/ page is a playlist of every file. Keep one.
        ydl_opts["playlist_items"] = "1"

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            install_pinned_handler(ydl)
            info = ydl.extract_info(fetch_url, download=False, ie_key=ARCHIVE_EXTRACTOR_KEY)
            if not _extractor_is_archive(info):
                report(
                    "Download failed: Kitty refused this link because the "
                    "Internet Archive extractor did not accept it."
                )
                return False
            chosen = select_download(info, requested_file)
            if chosen is None:
                report("Download failed: That file isn't in this Internet Archive item.")
                return False
            size = declared_size(chosen)
            if size is not None and size > MAX_FILE_BYTES:
                report(
                    "Download failed: That audio is larger than Kitty's limit "
                    f"of {MAX_FILE_BYTES} bytes per file and {MAX_TOTAL_BYTES} bytes overall."
                )
                return False
            before = directory_size(output_dir)
            before_files = _media_files(output_dir)
            ydl.process_info(chosen)
            added = directory_size(output_dir) - before
            new_files = _media_files(output_dir) - before_files
            if added > MAX_TOTAL_BYTES:
                for path in new_files:
                    path.unlink(missing_ok=True)
                cleanup_partials(output_dir)
                report(
                    "Download failed: That audio is larger than Kitty's limit "
                    f"of {MAX_FILE_BYTES} bytes per file and {MAX_TOTAL_BYTES} bytes overall."
                )
                return False
            if not finished["ok"] and not new_files:
                report("Download failed: The download did not finish.")
                return False
        report(f"Successfully downloaded: {url}")
        return True
    except DownloadError as exc:
        cleanup_partials(output_dir)
        error_message = str(exc)
        if "DRM protected" in error_message:
            report(f"Skipping DRM-protected track: {url}")
            return False
        report(f"Download failed: {error_message}")
        return False
    except RequestError as exc:
        cleanup_partials(output_dir)
        report(f"Download failed: {exc}")
        return False
    except Exception as exc:
        cleanup_partials(output_dir)
        report(f"Unexpected error: {exc}")
        return False


def main() -> None:
    url = input("Enter URL: ")
    output_path = "downloads/%(title)s.%(ext)s"
    download_audio(url, output_path)


if __name__ == "__main__":
    main()
