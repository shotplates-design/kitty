from pathlib import Path

from app import collect_audio_files, mime_for, safe_download_name

ROOT = Path(__file__).resolve().parents[1]


def test_ui_uses_server_download_not_yt_dlp_directly():
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    assert "download_audio(" in source
    assert "st.download_button" in source
    assert 'st.button("Download"' in source
    assert "yt_dlp" not in source


def test_project_checklist_files():
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in gitignore
    assert ".env.*" in gitignore
    assert "downloads/" in gitignore
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "pytest" in workflow
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    assert "streamlit" in requirements
    assert "yt-dlp" in requirements
    assert "pytest" in requirements
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "streamlit community cloud" in readme.lower()
    assert "home screen" in readme.lower()
    assert "$0" in readme


def test_safe_download_name_strips_controls_and_paths():
    assert safe_download_name("/tmp/kitty/Song.mp3") == "Song.mp3"
    assert safe_download_name("folder/line\nbreak.mp3") == "line_break.mp3"
    assert safe_download_name("   ") == "audio"


def test_mime_keeps_original_type():
    assert mime_for(Path("track.mp3")) == "audio/mpeg"
    assert mime_for(Path("track.flac")) == "audio/flac"
    assert mime_for(Path("track.unknownext")) == "application/octet-stream"


def test_collect_audio_files_stays_inside_the_temp_dir(tmp_path):
    jail = tmp_path / "jail"
    jail.mkdir()
    song = jail / "song.mp3"
    song.write_bytes(b"mp3")
    part = jail / "song.mp3.part"
    part.write_bytes(b"partial")
    thumb = jail / "cover.jpg"
    thumb.write_bytes(b"jpg")
    video = jail / "clip.mp4"
    video.write_bytes(b"mp4")
    outside = tmp_path / "outside.mp3"
    outside.write_bytes(b"nope")
    link = jail / "escape.mp3"
    link.symlink_to(outside)

    found = collect_audio_files(jail, [str(outside), str(song), str(link)])
    assert found == [song, video]
