import re

import pytest

from kitty import ARCHIVE_EXTRACTOR_KEY, download_audio
from tests.url_cases import INVALID_URLS
from yt_dlp.utils import DownloadError


class FakeYoutubeDL:
    instances = []

    def __init__(self, opts):
        self.opts = opts
        self.extract_calls = []
        self.download_calls = []
        self.info = {
            "extractor_key": "ArchiveOrg",
            "extractor": "archive.org",
            "id": "foo",
            "title": "Foo",
        }
        self.download_error = None
        self.extract_error = None
        self.download_result = 0
        FakeYoutubeDL.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def extract_info(self, url, download=True, ie_key=None, **kwargs):
        self.extract_calls.append(
            {"url": url, "download": download, "ie_key": ie_key}
        )
        if self.extract_error is not None:
            raise self.extract_error
        return self.info

    def download(self, urls):
        self.download_calls.append(list(urls))
        if self.download_error is not None:
            raise self.download_error
        for hook in self.opts.get("progress_hooks") or []:
            hook(
                {
                    "status": "downloading",
                    "downloaded_bytes": 5,
                    "total_bytes": 10,
                }
            )
            hook({"status": "finished", "filename": "downloads/Foo.mp3"})
        return self.download_result


@pytest.fixture
def fake_ytdl(monkeypatch):
    FakeYoutubeDL.instances = []
    monkeypatch.setattr("kitty.yt_dlp.YoutubeDL", FakeYoutubeDL)
    return FakeYoutubeDL


def use_fake(monkeypatch, **attrs):
    def build(opts):
        ydl = FakeYoutubeDL(opts)
        for key, value in attrs.items():
            setattr(ydl, key, value)
        return ydl

    monkeypatch.setattr("kitty.yt_dlp.YoutubeDL", build)


def test_download_audio_uses_archive_extractor_options(fake_ytdl):
    events = []
    saved = []
    ok = download_audio(
        "https://archive.org:443/details/Foo",
        "downloads/%(title)s.%(ext)s",
        progress_callback=events.append,
        saved_paths=saved,
    )
    assert ok is True
    assert len(fake_ytdl.instances) == 1
    ydl = fake_ytdl.instances[0]
    opts = ydl.opts
    assert opts["format"] == "bestaudio/best"
    assert opts["noplaylist"] is True
    assert opts["quiet"] is False
    assert opts["outtmpl"] == "downloads/%(title)s.%(ext)s"
    assert opts["allowed_extractors"] == [r"archive\.org"]
    pattern = opts["allowed_extractors"][0]
    assert re.fullmatch(pattern, "archive.org")
    assert re.fullmatch(pattern, "archiveXorg") is None
    assert re.fullmatch(pattern, "web.archive:youtube") is None
    assert "nocheckcertificate" not in opts
    assert ydl.extract_calls == [
        {
            "url": "https://archive.org/details/Foo",
            "download": False,
            "ie_key": ARCHIVE_EXTRACTOR_KEY,
        }
    ]
    assert ydl.download_calls == [["https://archive.org/details/Foo"]]
    assert events[0]["status"] == "downloading"
    assert saved == ["downloads/Foo.mp3"]


def test_download_links_are_rewritten_to_details(fake_ytdl):
    ok = download_audio(
        "https://www.archive.org/download/item/My%20File.mp3",
        "out/%(title)s.%(ext)s",
    )
    assert ok is True
    expected = "https://www.archive.org/details/item/My%20File.mp3"
    ydl = fake_ytdl.instances[0]
    assert ydl.extract_calls[0]["url"] == expected
    assert ydl.download_calls == [[expected]]


def test_wrong_extractor_never_downloads(fake_ytdl, monkeypatch):
    messages = []
    use_fake(monkeypatch, info={"extractor_key": "Generic", "extractor": "generic"})
    ok = download_audio(
        "https://archive.org/details/foo",
        "downloads/%(title)s.%(ext)s",
        status_callback=messages.append,
    )
    assert ok is False
    assert messages[-1].startswith("Download failed:")
    assert "Internet Archive extractor" in messages[-1]
    assert FakeYoutubeDL.instances[0].download_calls == []


@pytest.mark.parametrize("url", INVALID_URLS)
def test_invalid_urls_do_not_call_yt_dlp(url, fake_ytdl):
    messages = []
    assert download_audio(url, "downloads/%(title)s.%(ext)s", status_callback=messages.append) is False
    assert fake_ytdl.instances == []
    assert messages
    assert "Internet Archive" in messages[-1]


@pytest.mark.parametrize("value", [None, 5])
def test_non_strings_do_not_call_yt_dlp(value, fake_ytdl):
    assert download_audio(value, "downloads/%(title)s.%(ext)s") is False
    assert fake_ytdl.instances == []


def test_drm_is_skipped(fake_ytdl, monkeypatch):
    messages = []
    use_fake(monkeypatch, download_error=DownloadError("ERROR: DRM protected"))
    ok = download_audio(
        "https://archive.org/details/drm-track",
        "downloads/%(title)s.%(ext)s",
        status_callback=messages.append,
    )
    assert ok is False
    assert any(message.startswith("Skipping DRM-protected track:") for message in messages)
    assert not any(message.startswith("Successfully") for message in messages)


def test_drm_during_extract_is_skipped(fake_ytdl, monkeypatch):
    messages = []
    use_fake(monkeypatch, extract_error=DownloadError("This item is DRM protected"))
    ok = download_audio(
        "https://archive.org/details/drm-track",
        "out.mp3",
        status_callback=messages.append,
    )
    assert ok is False
    assert "Skipping DRM-protected track:" in messages[-1]
    assert FakeYoutubeDL.instances[-1].download_calls == []


def test_other_download_errors_are_reported(fake_ytdl, monkeypatch):
    messages = []
    use_fake(monkeypatch, download_error=DownloadError("HTTP Error 404: Not Found"))
    ok = download_audio(
        "https://archive.org/details/missing",
        "out.mp3",
        status_callback=messages.append,
    )
    assert ok is False
    assert messages[-1].startswith("Download failed:")
    assert "404" in messages[-1]
    assert "Skipping DRM" not in messages[-1]


def test_unexpected_errors_are_reported(fake_ytdl, monkeypatch):
    messages = []
    use_fake(monkeypatch, extract_error=RuntimeError("disk full"))
    ok = download_audio(
        "https://archive.org/details/foo",
        "out.mp3",
        status_callback=messages.append,
    )
    assert ok is False
    assert messages[-1] == "Unexpected error: disk full"


def test_nonzero_exit_is_a_failure(fake_ytdl, monkeypatch):
    messages = []
    use_fake(monkeypatch, download_result=1)
    assert download_audio("https://archive.org/details/foo", "out.mp3", status_callback=messages.append) is False
    assert messages[-1] == "Download failed: The download did not finish."
