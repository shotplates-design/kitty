# Kitty

Kitty is a small phone-friendly web app that saves **Internet Archive audio** onto your iPhone. Paste an `archive.org` link, tap **Download**, then tap **Save audio**. You get the original file. Kitty does not re-encode it.

Kitty will only fetch `https://archive.org` or `https://www.archive.org` links under `/details/` or `/download/`. Other sites, lookalike hosts, username tricks, and other ports are refused on the server before yt-dlp runs. yt-dlp is also limited to its `archive.org` extractor.

The hosting cost is **$0**. [Streamlit Community Cloud](https://streamlit.io/cloud) runs the app for free, and Kitty does not use paid APIs or accounts.

## Run locally

Use Python 3.12.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

Open the URL Streamlit prints (usually `http://localhost:8501`). On your phone, use the same Wi-Fi and your computer's LAN address if you want to try it before deploying.

Run the tests:

```bash
pytest
```

The command-line helper is still there, and it uses the same checks:

```bash
python kitty.py
```

Files from that command land in `downloads/`.

## Deploy on Streamlit Community Cloud

Community Cloud is free. No secrets and no API keys are required.

1. Push this repo to GitHub. Your Streamlit account needs access to it.
2. Sign in at [share.streamlit.io](https://share.streamlit.io) with GitHub.
3. Click **Create app**. When asked if you already have an app, choose **Yup, I have an app**.
4. Pick this repository, the `main` branch, and main file `app.py`.
5. In **Advanced settings**, leave the Python version at **3.12** (Community Cloud's default, also pinned in `runtime.txt`).
6. Click **Deploy** and wait until the app shows a healthy URL.

Official guide: [Deploy your app on Community Cloud](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/deploy).

Pushing to `main` updates the deployed app. There is nothing to pay for.

## Add Kitty to your iPhone home screen

1. Open the Community Cloud URL in **Safari**.
2. Tap the **Share** button (the square with the arrow).
3. Scroll the sheet and tap **Add to Home Screen**.
4. Name it **Kitty** and tap **Add**.

Open Kitty from the new icon. Paste a link such as `https://archive.org/details/…` or `https://archive.org/download/…/file.mp3`, tap **Download**, then tap **Save audio**. iOS saves the original file (for example `.mp3` or `.flac`) through the share sheet or the Files app. Use Safari if another browser does not offer Add to Home Screen.

Very large items can run out of memory on the free Cloud plan. A single track or a direct file link is the comfortable size.
