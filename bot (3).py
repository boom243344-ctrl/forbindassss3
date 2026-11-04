import asyncio
import base64
import os
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from flask import Flask
from PIL import Image
from telethon import TelegramClient, Button
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession
from telethon.tl.types import DocumentAttributeVideo
import yt_dlp


# ============================================================
# CONFIGURATION — set these as environment variables on Vorao
# ============================================================

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
SESSION_STRING = os.getenv("SESSION_STRING", "")
CHANNEL_ID_RAW = os.getenv("CHANNEL_ID", "")
CHANNEL_ID = int(CHANNEL_ID_RAW) if CHANNEL_ID_RAW.lstrip("-").isdigit() else CHANNEL_ID_RAW

PAGES_FILE = os.getenv("PAGES_FILE", "final_data.txt")
START_FROM = max(1, int(os.getenv("START_FROM", "1")))

GROUP_LINK = os.getenv("GROUP_LINK", "https://t.me/your_group")
JOIN_BUTTON_TEXT = os.getenv("JOIN_BUTTON_TEXT", "Join Group")

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY", "").strip()  # owner/repository
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main").strip() or "main"
GITHUB_PROGRESS_FILE = os.getenv("GITHUB_PROGRESS_FILE", "already_uploaded.txt")

PORT = int(os.getenv("PORT", "10000"))
RETRY_DELAY = max(5, int(os.getenv("RETRY_DELAY", "20")))
MAX_RETRIES = max(1, int(os.getenv("MAX_RETRIES", "1")))

WORK_DIR = Path(os.getenv("WORK_DIR", "temp_downloads"))
WORK_DIR.mkdir(parents=True, exist_ok=True)
VIDEO_PATH = WORK_DIR / "video.%(ext)s"
THUMB_PATH = WORK_DIR / "poster"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# This service is intended for content you own or are authorized to download
# and redistribute. It does not bypass DRM, paywalls, or access controls.


# ============================================================
# HEALTH CHECK SERVER (for hosting platforms)
# ============================================================

web_app = Flask(__name__)


@web_app.get("/")
def health():
    return "Telegram uploader is running.", 200


def run_web_server():
    web_app.run(host="0.0.0.0", port=PORT)


# ============================================================
# TELEGRAM CLIENT
# ============================================================

client = TelegramClient(
    StringSession(SESSION_STRING),
    API_ID,
    API_HASH,
)


# ============================================================
# GENERAL HELPERS
# ============================================================

def clean_text(value, limit=900):
    value = str(value or "")
    value = re.sub(r"[\x00-\x1f]+", " ", value)
    return " ".join(value.split())[:limit]


def format_duration(seconds):
    if not seconds:
        return "Unknown"
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def format_size(size):
    if not size:
        return "Unknown"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.2f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return "Unknown"


def load_urls():
    path = Path(PAGES_FILE)
    if not path.exists():
        raise FileNotFoundError(f"{PAGES_FILE} was not found.")
    with path.open("r", encoding="utf-8-sig") as file:
        return [line.strip() for line in file if line.strip() and not line.lstrip().startswith("#")]


def github_api_url():
    if not GITHUB_REPOSITORY or "/" not in GITHUB_REPOSITORY:
        return None
    owner, repo = GITHUB_REPOSITORY.split("/", 1)
    return (
        f"https://api.github.com/repos/{owner}/{repo}/contents/"
        f"{GITHUB_PROGRESS_FILE}"
    )


def save_github_record(index, status, title, source_url, details=""):
    """Append one record to the configured GitHub text file after each attempt."""
    api_url = github_api_url()
    if not GITHUB_TOKEN or not api_url:
        print("[GitHub] Not configured; record was not saved remotely.", flush=True)
        return False

    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    safe_title = clean_text(title, 300).replace("\t", " ")
    safe_url = clean_text(source_url, 1000).replace("\t", " ")
    safe_details = clean_text(details, 500).replace("\t", " ")
    line = f"{index}\t{status}\t{safe_title}\t{safe_url}"
    if safe_details:
        line += f"\t{safe_details}"

    try:
        response = requests.get(
            api_url,
            headers=headers,
            params={"ref": GITHUB_BRANCH},
            timeout=25,
        )

        sha = None
        old_lines = []
        if response.status_code == 200:
            payload = response.json()
            sha = payload.get("sha")
            encoded = payload.get("content", "").replace("\n", "")
            old_content = base64.b64decode(encoded).decode("utf-8", errors="replace")
            old_lines = [row for row in old_content.splitlines() if row.strip()]
        elif response.status_code != 404:
            response.raise_for_status()

        # Keep prior records. A repeated index is replaced, not duplicated.
        prefix = f"{index}\t"
        old_lines = [row for row in old_lines if not row.startswith(prefix)]
        new_content = ("\n".join(old_lines + [line]) + "\n").encode("utf-8")

        body = {
            "message": f"Process item {index}: {status}",
            "content": base64.b64encode(new_content).decode("ascii"),
            "branch": GITHUB_BRANCH,
        }
        if sha:
            body["sha"] = sha

        saved = requests.put(api_url, headers=headers, json=body, timeout=25)
        saved.raise_for_status()
        print(f"[GitHub] Saved item {index}: {status}", flush=True)
        return True
    except Exception as exc:
        print(f"[GitHub] Save failed for item {index}: {exc}", flush=True)
        return False


# ============================================================
# VIDEO DOWNLOAD + METADATA
# ============================================================

def download_video(page_url):
    """Use yt-dlp site extractors; no DRM or access-control bypass."""
    for item in WORK_DIR.iterdir():
        if item.is_file():
            item.unlink(missing_ok=True)

    options = {
        "outtmpl": str(VIDEO_PATH),
        "format": "best[ext=mp4]/best",
        "merge_output_format": "mp4",
        "writethumbnail": True,
        "noplaylist": True,
        "retries": 2,
        "fragment_retries": 2,
        "socket_timeout": 30,
        "http_headers": {"User-Agent": USER_AGENT},
        "quiet": True,
        "no_warnings": True,
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(page_url, download=True)
        prepared = ydl.prepare_filename(info)

    candidates = [Path(prepared), *WORK_DIR.glob("video.*")]
    video_file = next(
        (p for p in candidates if p.exists() and p.is_file()
         and p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov", ".m4v"}),
        None,
    )
    if video_file is None:
        raise RuntimeError("yt-dlp completed but the downloaded video file was not found.")

    # Convert the downloaded poster to JPEG for Telegram's thumbnail parameter.
    thumb_file = None
    image_candidates = [
        p for p in WORK_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
        and p != video_file
    ]
    if image_candidates:
        try:
            with Image.open(image_candidates[0]) as image:
                image.convert("RGB").save(WORK_DIR / "poster.jpg", "JPEG", quality=90)
            thumb_file = WORK_DIR / "poster.jpg"
        except Exception as exc:
            print(f"[Thumbnail] Could not convert poster: {exc}", flush=True)

    title = clean_text(info.get("title") or video_file.stem, 250)
    duration = int(info.get("duration") or 0)
    width = int(info.get("width") or 0)
    height = int(info.get("height") or 0)
    size = video_file.stat().st_size

    return {
        "path": video_file,
        "thumbnail": thumb_file,
        "title": title,
        "duration": duration,
        "width": width,
        "height": height,
        "size": size,
    }


# ============================================================
# TELEGRAM UPLOAD
# ============================================================

def upload_progress(current, total):
    if total <= 0:
        return
    percent = int(current * 100 / total)
    previous = getattr(upload_progress, "last_percent", -5)
    step = (percent // 5) * 5
    if step >= previous + 5 or percent >= 100:
        upload_progress.last_percent = step
        print(
            f"[Upload] {format_size(current)} / {format_size(total)} ({percent}%)",
            flush=True,
        )


async def upload_to_telegram(metadata, source_url):
    title = metadata["title"]
    caption = (
        f"**{title}**\n\n"
        f"📦 Size: `{format_size(metadata['size'])}`\n"
        f"⏱ Duration: `{format_duration(metadata['duration'])}`"
    )

    attributes = []
    if metadata["duration"] > 0 and metadata["width"] > 0 and metadata["height"] > 0:
        attributes.append(
            DocumentAttributeVideo(
                duration=metadata["duration"],
                w=metadata["width"],
                h=metadata["height"],
                supports_streaming=True,
            )
        )

    buttons = None
    if GROUP_LINK.startswith(("https://t.me/", "http://t.me/")):
        buttons = [[Button.url(JOIN_BUTTON_TEXT, GROUP_LINK)]]

    upload_progress.last_percent = -5
    await client.send_file(
        entity=CHANNEL_ID,
        file=str(metadata["path"]),
        thumb=str(metadata["thumbnail"]) if metadata["thumbnail"] else None,
        caption=caption,
        parse_mode="md",
        supports_streaming=True,
        attributes=attributes or None,
        buttons=buttons,
        progress_callback=upload_progress,
    )


# ============================================================
# MAIN PROCESSING LOOP
# ============================================================

async def main():
    if not API_ID or not API_HASH or not SESSION_STRING or not CHANNEL_ID_RAW:
        raise RuntimeError(
            "Missing API_ID, API_HASH, SESSION_STRING, or CHANNEL_ID environment variable."
        )
    if not GROUP_LINK.startswith(("https://t.me/", "http://t.me/")):
        print("[Config] GROUP_LINK is not a Telegram link; Join Group button will be omitted.")

    urls = load_urls()
    total = len(urls)
    print(f"[Queue] Loaded {total} URLs from {PAGES_FILE}.", flush=True)

    if START_FROM > total:
        print(f"[Queue] START_FROM={START_FROM} is beyond the {total} available URLs.")
        return

    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError("Telegram session is not authorized. Generate a valid SESSION_STRING.")

    print("[Telegram] Connected. Starting processing.", flush=True)

    try:
        for index, page_url in enumerate(urls, start=1):
            if index < START_FROM:
                continue

            title = f"URL item {index}"
            print(f"\n[{index}/{total}] Processing: {page_url}", flush=True)
            status = "SKIPPED"
            details = ""

            try:
                for attempt in range(1, MAX_RETRIES + 1):
                    try:
                        print(f"[Download] Attempt {attempt}/{MAX_RETRIES}", flush=True)
                        metadata = await asyncio.to_thread(download_video, page_url)
                        title = metadata["title"]
                        print(
                            f"[Video] {title} | {format_size(metadata['size'])} | "
                            f"{format_duration(metadata['duration'])}",
                            flush=True,
                        )
                        await upload_to_telegram(metadata, page_url)
                        status = "UPLOADED"
                        print(f"[Success] Uploaded: {title}", flush=True)
                        break
                    except FloodWaitError as exc:
                        details = f"Telegram FloodWait: wait {exc.seconds}s"
                        if attempt < MAX_RETRIES:
                            wait_seconds = exc.seconds + 3
                            print(f"[Telegram] FloodWait; waiting {wait_seconds}s.", flush=True)
                            await asyncio.sleep(wait_seconds)
                        else:
                            raise
                    except Exception as exc:
                        details = str(exc)
                        if attempt < MAX_RETRIES:
                            print(f"[Retry] {details}", flush=True)
                            await asyncio.sleep(RETRY_DELAY)
                        else:
                            raise

            except Exception as exc:
                status = "SKIPPED"
                details = clean_text(exc, 500)
                print(f"[Failed] {title}: {details}", flush=True)

            finally:
                # Remove downloaded video, poster and any other temporary files.
                for temp_path in WORK_DIR.iterdir():
                    if temp_path.is_file():
                        try:
                            temp_path.unlink()
                        except Exception as exc:
                            print(f"[Cleanup] Could not delete {temp_path}: {exc}", flush=True)

            # Save both successful and failed attempts; this log is never read
            # to decide whether an item should be skipped.
            save_github_record(index, status, title, page_url, details)

    finally:
        await client.disconnect()

    print("\n[Finished] Queue processing completed.", flush=True)


if __name__ == "__main__":
    threading.Thread(target=run_web_server, daemon=True).start()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Stopped by user.", flush=True)
    except Exception as error:
        print(f"[Fatal] {error}", flush=True)
