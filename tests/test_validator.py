import pytest

from kitty import ArchiveUrlError, validate_archive_url
from tests.url_cases import INVALID_URLS, VALID_URLS


@pytest.mark.parametrize("url", VALID_URLS)
def test_accepts_archive_links(url):
    canonical = validate_archive_url(url)
    assert canonical.startswith("https://")
    assert "archive.org/" in canonical
    assert "/details/" in canonical or "/download/" in canonical
    assert "@" not in canonical.split("/", 3)[2]
    assert validate_archive_url(canonical) == canonical


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://archive.org/details/foo", "https://archive.org/details/foo"),
        ("https://www.archive.org/details/foo/", "https://www.archive.org/details/foo"),
        ("HTTPS://ARCHIVE.ORG/details/Foo_Bar", "https://archive.org/details/Foo_Bar"),
        ("https://archive.org:443/details/foo", "https://archive.org/details/foo"),
        ("https://archive.org./details/foo", "https://archive.org/details/foo"),
        (
            "https://archive.org/download/item/My%20File.mp3",
            "https://archive.org/download/item/My%20File.mp3",
        ),
        ("https://archive.org/DETAILS/Foo", "https://archive.org/details/Foo"),
        (
            "https://archive.org/details/foo?start=10",
            "https://archive.org/details/foo?start=10",
        ),
        ("https://archive.org/details/foo#chapter", "https://archive.org/details/foo"),
        ("  https://archive.org/details/foo  ", "https://archive.org/details/foo"),
        ("https://archive.org//details//foo", "https://archive.org/details/foo"),
        ("https://archive.org/details/foo/./bar", "https://archive.org/details/foo/bar"),
        ("https://archive.org/details/gd..1977", "https://archive.org/details/gd..1977"),
        ("https://archive.org/details/caf%C3%A9", "https://archive.org/details/caf%C3%A9"),
        (
            "https://www.archive.org:443/download/foo/bar.ogg",
            "https://www.archive.org/download/foo/bar.ogg",
        ),
        ("https://archive.org/details/foo@bar", "https://archive.org/details/foo%40bar"),
    ],
)
def test_canonical_form(url, expected):
    assert validate_archive_url(url) == expected


@pytest.mark.parametrize("url", INVALID_URLS)
def test_rejects_invalid_and_bypass_urls(url):
    with pytest.raises(ArchiveUrlError, match="Internet Archive"):
        validate_archive_url(url)


@pytest.mark.parametrize("value", [None, 12, b"https://archive.org/details/foo"])
def test_rejects_non_strings(value):
    with pytest.raises(ArchiveUrlError, match="Internet Archive"):
        validate_archive_url(value)


@pytest.mark.parametrize(
    ("url", "snippet"),
    [
        ("http://archive.org/details/foo", "Only https"),
        ("https://archive.org.evil.com/details/foo", "Lookalike"),
        ("https://user:pass@archive.org/details/foo", "username"),
        ("https://evil.com@archive.org/details/foo", "extra @"),
        ("https://archive.org:8080/details/foo", "port"),
        ("https://archive.org/search?query=cats", "/details/"),
        ("https://archive.org/details/foo/../../etc/passwd", "isn't allowed"),
        ("https://archive.org/embed/foo", "/details/"),
    ],
)
def test_rejection_messages_are_specific(url, snippet):
    with pytest.raises(ArchiveUrlError, match=snippet):
        validate_archive_url(url)
