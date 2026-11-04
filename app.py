"""Kitty, a phone-friendly Internet Archive audio saver."""

from __future__ import annotations

import shutil
import tempfile
import unicodedata
from pathlib import Path

import streamlit as st

from kitty import download_audio

AUDIO_EXTENSIONS = {
    ".mp3",
    ".m4a",
    ".aac",
    ".flac",
    ".ogg",
    ".opus",
    ".wav",
    ".aiff",
    ".aif",
    ".wma",
    ".oga",
    ".mpga",
    ".webm",
    ".mka",
}
SKIP_SUFFIXES = {
    ".part",
    ".ytdl",
    ".temp",
    ".jpg",
    ".jpeg",
    ".png",
    ".json",
    ".vtt",
    ".srt",
    ".info",
}
MIME_TYPES = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".opus": "audio/opus",
    ".wav": "audio/wav",
    ".aiff": "audio/aiff",
    ".aif": "audio/aiff",
    ".wma": "audio/x-ms-wma",
    ".webm": "audio/webm",
    ".mka": "audio/x-matroska",
    ".mp4": "video/mp4",
    ".ogv": "video/ogg",
}

PAGE_STYLE = """
<style>
  section.main > div.block-container {
    max-width: 32rem;
    padding-top: 1.1rem;
    padding-bottom: 2.5rem;
  }
  .kitty-hero { text-align: center; margin-bottom: 0.4rem; }
  .kitty-emoji { font-size: 3.2rem; line-height: 1; }
  .kitty-hero h1 {
    font-size: 2.4rem;
    margin: 0.15rem 0 0.2rem;
    letter-spacing: -0.03em;
  }
  .kitty-hero p {
    margin: 0;
    color: #7a5344;
    font-size: 1.05rem;
  }
  div[data-testid="stTextInput"] input {
    font-size: 16px !important;
    min-height: 3rem;
    border-radius: 14px;
  }
  div.stButton > button,
  div.stDownloadButton > button,
  .stButton button,
  .stDownloadButton button {
    width: 100%;
    min-height: 3.15rem;
    border-radius: 999px;
    font-size: 1.05rem;
    font-weight: 700;
  }
  div[data-testid="stCaptionContainer"] { text-align: center; }
</style>
"""


def safe_download_name(path: str) -> str:
    """File name for the phone, without path pieces or control characters."""
    cleaned: list[str] = []
    for character in Path(path).name:
        if character in {"/", "\\"} or unicodedata.category(character) in {"Cc", "Cf"}:
            cleaned.append("_")
        else:
            cleaned.append(character)
    name = "".join(cleaned).strip()
    return name or "audio"


def mime_for(path: Path) -> str:
    return MIME_TYPES.get(path.suffix.lower(), "application/octet-stream")


def collect_audio_files(root: Path, reported: list[str]) -> list[Path]:
    """Keep finished files that actually live inside ``root``."""
    root_resolved = root.resolve()
    candidates = [Path(entry) for entry in reported]
    if root_resolved.is_dir():
        candidates.extend(path for path in root_resolved.rglob("*") if path.is_file())

    seen: set[Path] = set()
    kept: list[Path] = []
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if not resolved.is_file() or not resolved.is_relative_to(root_resolved):
            continue
        if resolved.suffix.lower() in SKIP_SUFFIXES or resolved.name.endswith(".part"):
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        kept.append(resolved)

    kept.sort(
        key=lambda path: (
            0 if path.suffix.lower() in AUDIO_EXTENSIONS else 1,
            path.name.casefold(),
        )
    )
    return kept


def _unique_name(name: str, used: set[str]) -> str:
    if name not in used:
        used.add(name)
        return name
    stem = Path(name).stem
    suffix = Path(name).suffix
    index = 2
    while True:
        candidate = f"{stem} ({index}){suffix}"
        if candidate not in used:
            used.add(candidate)
            return candidate
        index += 1


def open_saved_audio(path: Path):
    """Open the file when Save is tapped, instead of reading it into memory."""

    def _open():
        return path.open("rb")

    return _open


def clips_from(paths: list[Path]) -> list[dict[str, object]]:
    used: set[str] = set()
    clips: list[dict[str, object]] = []
    for path in paths:
        clips.append(
            {
                "name": _unique_name(safe_download_name(str(path)), used),
                "path": path,
                "mime": mime_for(path),
            }
        )
    return clips


def _session_dir() -> Path:
    stored = st.session_state.get("kitty_dir")
    if stored:
        directory = Path(stored)
        directory.mkdir(parents=True, exist_ok=True)
        return directory
    directory = Path(tempfile.mkdtemp(prefix="kitty-"))
    st.session_state.kitty_dir = str(directory)
    return directory


def _clear_session_files(directory: Path) -> None:
    if not directory.is_dir():
        return
    for path in directory.iterdir():
        if path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path, ignore_errors=True)


def main() -> None:
    st.set_page_config(
        page_title="Kitty",
        page_icon="🐱",
        layout="centered",
        initial_sidebar_state="collapsed",
    )
    st.markdown(PAGE_STYLE, unsafe_allow_html=True)
    st.markdown(
        """
        <div class="kitty-hero">
          <div class="kitty-emoji" aria-hidden="true">🐱</div>
          <h1>Kitty</h1>
          <p>Save Internet Archive audio to your phone.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.caption("Only archive.org. Original file, no re-encoding.")

    if "banner" not in st.session_state:
        st.session_state.banner = None
    if "clips" not in st.session_state:
        st.session_state.clips = None

    url = st.text_input(
        "Internet Archive link",
        placeholder="https://archive.org/details/…",
        help="Paste an https://archive.org/details/ or /download/ link.",
    )

    if st.button("Download", type="primary", width="stretch"):
        st.session_state.banner = None
        st.session_state.clips = None
        status = st.empty()
        progress_slot = st.empty()
        status.info("Checking the link…")
        notes: list[str] = []
        saved: list[str] = []
        progress = {"bar": None}

        def on_progress(update: dict) -> None:
            if progress["bar"] is None:
                progress["bar"] = progress_slot.progress(0)
            bar = progress["bar"]
            if update.get("status") == "downloading":
                total = update.get("total_bytes") or update.get("total_bytes_estimate") or 0
                done = update.get("downloaded_bytes") or 0
                if total:
                    bar.progress(min(float(done) / float(total), 1.0))
                status.info("Downloading from the Internet Archive…")
            elif update.get("status") == "finished":
                bar.progress(1.0)
                status.info("Preparing the original file…")

        directory = _session_dir()
        _clear_session_files(directory)
        ok = download_audio(
            url,
            str(directory / "%(title)s.%(ext)s"),
            progress_callback=on_progress,
            status_callback=notes.append,
            saved_paths=saved,
        )
        message = notes[-1] if notes else "Download failed."
        clips = None
        if ok:
            files = collect_audio_files(directory, saved)
            if not files:
                ok = False
                message = (
                    "Download failed: Kitty finished, but no audio file was saved."
                )
            else:
                clips = clips_from(files)
                if progress["bar"] is None:
                    progress_slot.progress(1.0)
        st.session_state.banner = {"ok": ok, "text": message}
        st.session_state.clips = clips
        status.empty()

    banner = st.session_state.banner
    if banner:
        if banner["ok"]:
            st.success(banner["text"])
        else:
            st.error(banner["text"])

    clips = st.session_state.clips or []
    if clips:
        labels = [str(clip["name"]) for clip in clips]
        selected = labels[0]
        if len(clips) > 1:
            selected = st.selectbox("Choose a file", labels, index=0)
        clip = next(item for item in clips if item["name"] == selected)
        st.download_button(
            "Save audio",
            data=open_saved_audio(Path(clip["path"])),
            file_name=str(clip["name"]),
            mime=str(clip["mime"]),
            type="primary",
            width="stretch",
            key="save-audio",
        )
        st.caption("Saves the original file to your phone. Kitty does not convert it.")


if __name__ == "__main__":
    main()
