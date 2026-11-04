
import asyncio
import base64
import os
import re
import threading
import time
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import unescape
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from flask import Flask
from PIL import Image
from telethon import TelegramClient, Button
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession
from telethon.tl.types import DocumentAttributeVideo


# ============================================================
# CONFIGURATION
# ============================================================

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
SESSION_STRING = os.getenv("SESSION_STRING", "")

CHANNEL_ID_RAW = os.getenv("CHANNEL_ID", "")
CHANNEL_ID = (
    int(CHANNEL_ID_RAW)
    if CHANNEL_ID_RAW.lstrip("-").isdigit()
    else CHANNEL_ID_RAW
)

PAGES_FILE = os.getenv("PAGES_FILE", "final_data.txt")
START_FROM = max(1, int(os.getenv("START_FROM", "1")))

GROUP_LINK = os.getenv("GROUP_LINK", "https://t.me/your_group")
JOIN_BUTTON_TEXT = os.getenv("JOIN_BUTTON_TEXT", "Join Group")

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY", "").strip()
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main").strip() or "main"
GITHUB_PROGRESS_FILE = os.getenv(
    "GITHUB_PROGRESS_FILE",
    "already_uploaded.txt"
)

PORT = int(os.getenv("PORT", "10000"))
RETRY_DELAY = max(5, int(os.getenv("RETRY_DELAY", "20")))
MAX_RETRIES = max(1, int(os.getenv("MAX_RETRIES", "1")))

# SPEED SETTINGS
DOWNLOAD_WORKERS = max(
    1, min(16, int(os.getenv("DOWNLOAD_WORKERS", "8")))
)

DOWNLOAD_CHUNK_MB = max(
    2, min(32, int(os.getenv("DOWNLOAD_CHUNK_MB", "8")))
)

TELEGRAM_UPLOAD_WORKERS = max(
    1, min(16, int(os.getenv("TELEGRAM_UPLOAD_WORKERS", "8")))
)

WORK_DIR = Path(os.getenv("WORK_DIR", "temp_downloads"))
WORK_DIR.mkdir(parents=True, exist_ok=True)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/156.0.0.0 Safari/537.36"
)


# ============================================================
# LOGGING
# ============================================================

def log(message):
    print(
        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}",
        flush=True
    )


# ============================================================
# FLASK HEALTH SERVER
# ============================================================

web_app = Flask(__name__)


@web_app.get("/")
def health():
    return "Telegram uploader is running.", 200


def run_web_server():
    log("[SERVER] Starting health server.")
    web_app.run(host="0.0.0.0", port=PORT)


# ============================================================
# TELEGRAM CLIENT
# ============================================================

client = TelegramClient(
    StringSession(SESSION_STRING),
    API_ID,
    API_HASH
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

    return (
        f"{hours:02d}:{minutes:02d}:{secs:02d}"
        if hours
        else f"{minutes:02d}:{secs:02d}"
    )


def format_size(size):
    if not size:
        return "Unknown"

    value = float(size)

    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return (
                f"{value:.2f} {unit}"
                if unit != "B"
                else f"{int(value)} B"
            )

        value /= 1024

    return "Unknown"


def load_urls():
    path = Path(PAGES_FILE)

    if not path.exists():
        raise FileNotFoundError(
            f"{PAGES_FILE} was not found."
        )

    with path.open("r", encoding="utf-8-sig") as file:
        return [
            line.strip()
            for line in file
            if line.strip()
            and not line.lstrip().startswith("#")
        ]


def cleanup_files():
    log("[CLEANUP] Removing temporary files.")

    for item in WORK_DIR.iterdir():
        if item.is_file():
            try:
                item.unlink()
                log(f"[CLEANUP] Deleted: {item.name}")
            except Exception as exc:
                log(f"[CLEANUP ERROR] {item.name}: {exc}")


# ============================================================
# GITHUB PROGRESS LOGGER
# ============================================================

def github_api_url():
    if not GITHUB_REPOSITORY or "/" not in GITHUB_REPOSITORY:
        return None

    owner, repo = GITHUB_REPOSITORY.split("/", 1)

    return (
        f"https://api.github.com/repos/{owner}/{repo}/contents/"
        f"{GITHUB_PROGRESS_FILE}"
    )


def save_github_record(index, status, title, source_url, details=""):
    """
    Saves every result to GitHub.
    Existing records are preserved.
    GitHub log is not used to skip queue items.
    """

    api_url = github_api_url()

    if not GITHUB_TOKEN or not api_url:
        log("[GITHUB] Not configured; record was not saved remotely.")
        return False

    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "X-GitHub-Api-Version": "2022-11-28"
    }

    safe_title = clean_text(title, 300).replace("\t", " ")
    safe_url = clean_text(source_url, 1000).replace("\t", " ")
    safe_details = clean_text(details, 500).replace("\t", " ")

    line = (
        f"{index}\t{status}\t{safe_title}\t{safe_url}"
    )

    if safe_details:
        line += f"\t{safe_details}"

    for attempt in range(1, 6):

        try:
            log(
                f"[GITHUB] Reading current file "
                f"(attempt {attempt}/5)."
            )

            response = requests.get(
                api_url,
                headers=headers,
                params={"ref": GITHUB_BRANCH},
                timeout=30
            )

            sha = None
            old_lines = []

            if response.status_code == 200:
                payload = response.json()
                sha = payload.get("sha")

                encoded = payload.get(
                    "content", ""
                ).replace("\n", "")

                old_content = base64.b64decode(
                    encoded
                ).decode("utf-8", errors="replace")

                old_lines = [
                    row
                    for row in old_content.splitlines()
                    if row.strip()
                ]

            elif response.status_code != 404:
                response.raise_for_status()

            prefix = f"{index}\t"

            old_lines = [
                row for row in old_lines
                if not row.startswith(prefix)
            ]

            new_content = (
                "\n".join(old_lines + [line]) + "\n"
            ).encode("utf-8")

            body = {
                "message": f"Process item {index}: {status}",
                "content": base64.b64encode(
                    new_content
                ).decode("ascii"),
                "branch": GITHUB_BRANCH
            }

            if sha:
                body["sha"] = sha

            log(f"[GITHUB] Updating item {index}: {status}")

            saved = requests.put(
                api_url,
                headers=headers,
                json=body,
                timeout=30
            )

            if saved.status_code in (409, 422) and attempt < 5:
                log(
                    f"[GITHUB] SHA conflict for item {index}; "
                    f"refreshing and retrying."
                )
                time.sleep(min(attempt * 2, 8))
                continue

            saved.raise_for_status()

            log(
                f"[GITHUB] Successfully saved item "
                f"{index}: {status}"
            )

            return True

        except requests.RequestException as exc:

            if attempt < 5:
                log(
                    f"[GITHUB] Attempt {attempt}/5 failed: {exc}"
                )
                time.sleep(min(attempt * 2, 8))
                continue

            log(f"[GITHUB ERROR] {exc}")
            return False

        except Exception as exc:
            log(f"[GITHUB ERROR] {exc}")
            return False

    return False


# ============================================================
# EXTRACT WEBPAGE INFORMATION
# ============================================================

def extract_page_metadata(page_url, session):

    log("[PAGE] Requesting webpage.")

    response = session.get(
        page_url,
        timeout=(20, 45),
        allow_redirects=True
    )

    log(f"[PAGE] HTTP status: {response.status_code}")

    response.raise_for_status()

    html = response.text

    log(f"[PAGE] HTML received: {format_size(len(html))}")

    normalized = (
        unescape(html)
        .replace(r"\/", "/")
        .replace(r"\u0026", "&")
    )

    pattern = (
        r"""(?:https?:)?//[^\"'<>\s]+?\.mp4(?:\?[^\"'<>\s]*)?"""
        r"""|(?:\.\.?/|/)?[^\"'<>\s]+?\.mp4(?:\?[^\"'<>\s]*)?"""
    )

    matches = re.findall(
        pattern,
        normalized,
        flags=re.IGNORECASE
    )

    video_urls = []

    for value in matches:

        value = value.strip().rstrip("),;")

        if value.startswith("//"):
            value = "https:" + value

        candidate = urljoin(response.url, value)

        if candidate not in video_urls:
            video_urls.append(candidate)

    title_match = re.search(
        r'<meta[^>]+property=["\']og:title["\']'
        r'[^>]+content=["\']([^"\']+)',
        html,
        re.I
    )

    if not title_match:
        title_match = re.search(
            r"<title[^>]*>(.*?)</title>",
            html,
            re.I | re.S
        )

    title = clean_text(
        unescape(title_match.group(1))
        if title_match
        else Path(urlparse(page_url).path).stem,
        250
    )

    image_match = re.search(
        r'<meta[^>]+property=["\']og:image["\']'
        r'[^>]+content=["\']([^"\']+)',
        html,
        re.I
    )

    poster_url = (
        urljoin(response.url, unescape(image_match.group(1)))
        if image_match
        else None
    )

    log(f"[PAGE] Title: {title}")
    log(f"[PAGE] MP4 links found: {len(video_urls)}")
    log(
        f"[PAGE] Poster found: "
        f"{'YES' if poster_url else 'NO'}"
    )

    return title, video_urls, poster_url, response.url


# ============================================================
# VIDEO INFORMATION
# ============================================================

def probe_video(path):

    log("[INFO] Checking video information.")

    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries",
                "format=duration:stream=width,height",
                "-of", "json",
                str(path)
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True
        )

        data = json.loads(result.stdout or "{}")

        stream = (data.get("streams") or [{}])[0]

        duration = int(
            float(
                (data.get("format") or {}).get("duration") or 0
            )
        )

        width = int(stream.get("width") or 0)
        height = int(stream.get("height") or 0)

        log(
            f"[INFO] Duration: {format_duration(duration)} | "
            f"Resolution: {width}x{height}"
        )

        return duration, width, height

    except Exception as exc:
        log(f"[INFO] ffprobe unavailable or failed: {exc}")
        return 0, 0, 0


# ============================================================
# PARALLEL VIDEO DOWNLOADER
# ============================================================

def download_video(page_url):

    cleanup_files()

    session = requests.Session()

    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Encoding": "identity",
        "Connection": "keep-alive"
    })

    log("[1/6] Fetching webpage.")

    title, video_urls, poster_url, final_page_url = (
        extract_page_metadata(page_url, session)
    )

    if not video_urls:
        raise RuntimeError(
            "No direct MP4 URL found in webpage HTML. "
            "The player may load its source dynamically."
        )

    referer = (
        "https://bindasmood.com/"
        if "bindasmood.com" in urlparse(final_page_url).netloc.lower()
        else final_page_url
    )

    common_headers = {
        "Referer": referer,
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Encoding": "identity",
        "Connection": "keep-alive"
    }

    video_file = WORK_DIR / "video.mp4"
    last_error = None
    downloaded_ok = False

    for source_no, video_url in enumerate(video_urls, start=1):

        log(
            f"[CDN] Source {source_no}/{len(video_urls)}: "
            f"{urlparse(video_url).netloc}"
        )

        try:
            # Test HTTP byte-range support.
            probe_headers = dict(common_headers)
            probe_headers["Range"] = "bytes=0-0"

            with requests.get(
                video_url,
                headers=probe_headers,
                stream=True,
                timeout=(20, 60),
                allow_redirects=True
            ) as probe:

                log(f"[CDN] Range probe HTTP {probe.status_code}")

                probe.raise_for_status()

                content_type = probe.headers.get(
                    "Content-Type", ""
                ).lower()

                content_range = probe.headers.get(
                    "Content-Range", ""
                )

                match = re.match(
                    r"bytes\s+0-0/(\d+)",
                    content_range,
                    re.I
                )

                total_size = int(match.group(1)) if match else 0

                range_supported = (
                    probe.status_code == 206
                    and bool(total_size)
                )

                if "text/html" in content_type:
                    raise RuntimeError(
                        "CDN returned HTML instead of video."
                    )

                for _ in probe.iter_content(chunk_size=1):
                    break

            # ------------------------------------------------
            # PARALLEL DOWNLOAD
            # ------------------------------------------------

            if (
                range_supported
                and DOWNLOAD_WORKERS > 1
                and total_size > 1
            ):

                workers = min(
                    DOWNLOAD_WORKERS,
                    max(
                        1,
                        (total_size + 1)
                        // (DOWNLOAD_CHUNK_MB * 1024 * 1024)
                    )
                )

                chunk_size = max(
                    DOWNLOAD_CHUNK_MB * 1024 * 1024,
                    (total_size + workers - 1) // workers
                )

                ranges = []
                start_byte = 0

                while start_byte < total_size:

                    end_byte = min(
                        total_size - 1,
                        start_byte + chunk_size - 1
                    )

                    ranges.append((start_byte, end_byte))
                    start_byte = end_byte + 1

                actual_workers = min(workers, len(ranges))

                log(
                    f"[DOWNLOAD] Parallel mode enabled. "
                    f"Size={format_size(total_size)}, "
                    f"workers={actual_workers}, "
                    f"parts={len(ranges)}"
                )

                fd = os.open(
                    str(video_file),
                    os.O_CREAT | os.O_WRONLY | os.O_TRUNC,
                    0o644
                )

                os.ftruncate(fd, total_size)

                progress_lock = threading.Lock()

                progress = {
                    "bytes": 0,
                    "last": -1
                }

                def fetch_range(byte_range):

                    start_byte, end_byte = byte_range

                    headers = dict(common_headers)

                    headers["Range"] = (
                        f"bytes={start_byte}-{end_byte}"
                    )

                    expected = end_byte - start_byte + 1
                    received = 0

                    with requests.get(
                        video_url,
                        headers=headers,
                        stream=True,
                        timeout=(20, 120),
                        allow_redirects=True
                    ) as part_response:

                        if part_response.status_code != 206:
                            raise RuntimeError(
                                f"Range {start_byte}-{end_byte} "
                                f"returned HTTP "
                                f"{part_response.status_code}, "
                                f"expected 206."
                            )

                        part_response.raise_for_status()

                        actual_range = part_response.headers.get(
                            "Content-Range", ""
                        )

                        if not actual_range.lower().startswith(
                            f"bytes {start_byte}-{end_byte}/"
                        ):
                            raise RuntimeError(
                                f"Unexpected Content-Range: "
                                f"{actual_range}"
                            )

                        for data in part_response.iter_content(
                            chunk_size=1024 * 1024
                        ):

                            if not data:
                                continue

                            view = memoryview(data)
                            offset = start_byte + received

                            while view:

                                written = os.pwrite(
                                    fd,
                                    view,
                                    offset
                                )

                                if written <= 0:
                                    raise OSError(
                                        "Could not write downloaded range."
                                    )

                                view = view[written:]
                                offset += written

                            received += len(data)

                            with progress_lock:

                                progress["bytes"] += len(data)

                                percent = int(
                                    progress["bytes"] * 100 / total_size
                                )

                                if (
                                    percent >= progress["last"] + 5
                                    or percent == 100
                                ):

                                    progress["last"] = (
                                        percent // 5
                                    ) * 5

                                    log(
                                        f"[DOWNLOAD] {percent}% | "
                                        f"{format_size(progress['bytes'])} / "
                                        f"{format_size(total_size)}"
                                    )

                    if received != expected:
                        raise RuntimeError(
                            f"Range {start_byte}-{end_byte} "
                            f"incomplete: {received}/{expected} bytes."
                        )

                    return received

                try:

                    with ThreadPoolExecutor(
                        max_workers=actual_workers
                    ) as pool:

                        futures = [
                            pool.submit(fetch_range, byte_range)
                            for byte_range in ranges
                        ]

                        for future in as_completed(futures):
                            future.result()

                    os.fsync(fd)

                finally:
                    os.close(fd)

                if video_file.stat().st_size != total_size:
                    raise RuntimeError(
                        "Parallel download produced an incorrect file size."
                    )

                downloaded_ok = True

                log(
                    f"[DOWNLOAD] Parallel download complete: "
                    f"{format_size(total_size)}"
                )

            # ------------------------------------------------
            # SINGLE CONNECTION FALLBACK
            # ------------------------------------------------

            else:

                log(
                    "[DOWNLOAD] Byte ranges unavailable. "
                    "Using normal streaming download."
                )

                with requests.get(
                    video_url,
                    headers=common_headers,
                    stream=True,
                    timeout=(30, 180),
                    allow_redirects=True
                ) as response:

                    log(f"[CDN] HTTP {response.status_code}")

                    response.raise_for_status()

                    content_type = response.headers.get(
                        "Content-Type", ""
                    ).lower()

                    if "text/html" in content_type:
                        raise RuntimeError(
                            "CDN returned HTML instead of video."
                        )

                    total_size = int(
                        response.headers.get("Content-Length", 0)
                    )

                    downloaded = 0
                    last_percent = -1

                    with video_file.open("wb") as output:

                        for chunk in response.iter_content(
                            chunk_size=4 * 1024 * 1024
                        ):

                            if not chunk:
                                continue

                            output.write(chunk)
                            downloaded += len(chunk)

                            if total_size:

                                percent = int(
                                    downloaded * 100 / total_size
                                )

                                if (
                                    percent >= last_percent + 5
                                    or percent == 100
                                ):

                                    last_percent = (
                                        percent // 5
                                    ) * 5

                                    log(
                                        f"[DOWNLOAD] {percent}% | "
                                        f"{format_size(downloaded)} / "
                                        f"{format_size(total_size)}"
                                    )

                    if downloaded == 0:
                        raise RuntimeError(
                            "Downloaded file is empty."
                        )

                    if total_size and downloaded != total_size:
                        raise RuntimeError(
                            f"Incomplete download: "
                            f"{downloaded}/{total_size} bytes."
                        )

                downloaded_ok = True

            if downloaded_ok:
                break

        except Exception as exc:

            last_error = exc

            video_file.unlink(missing_ok=True)

            log(f"[CDN ERROR] {exc}")

    if not downloaded_ok:
        raise RuntimeError(
            f"Could not download any MP4 source: {last_error}"
        )

    # --------------------------------------------------------
    # THUMBNAIL
    # --------------------------------------------------------

    thumb_file = None

    if poster_url:

        try:
            log("[THUMBNAIL] Downloading poster.")

            poster_response = session.get(
                poster_url,
                headers={"Referer": final_page_url},
                timeout=(15, 30)
            )

            poster_response.raise_for_status()

            raw_poster = WORK_DIR / "poster_source"

            raw_poster.write_bytes(poster_response.content)

            with Image.open(raw_poster) as image:

                image.convert("RGB").save(
                    WORK_DIR / "poster.jpg",
                    "JPEG",
                    quality=88
                )

            raw_poster.unlink(missing_ok=True)

            thumb_file = WORK_DIR / "poster.jpg"

            log("[THUMBNAIL] Ready.")

        except Exception as exc:
            log(f"[THUMBNAIL] Skipped: {exc}")

    # --------------------------------------------------------
    # VIDEO METADATA
    # --------------------------------------------------------

    duration, width, height = probe_video(video_file)

    size = video_file.stat().st_size

    log(
        f"[VIDEO] Ready: {format_size(size)} | "
        f"{format_duration(duration)} | "
        f"{width}x{height}"
    )

    return {
        "path": video_file,
        "thumbnail": thumb_file,
        "title": title or video_file.stem,
        "duration": duration,
        "width": width,
        "height": height,
        "size": size
    }


# ============================================================
# TELEGRAM UPLOAD PROGRESS
# ============================================================

def upload_progress(current, total):

    if total <= 0:
        return

    percent = int(current * 100 / total)

    previous = getattr(
        upload_progress,
        "last_percent",
        -5
    )

    step = (percent // 5) * 5

    if step >= previous + 5 or percent >= 100:

        upload_progress.last_percent = step

        log(
            f"[TELEGRAM UPLOAD] {percent}% | "
            f"{format_size(current)} / {format_size(total)}"
        )


# ============================================================
# TELEGRAM UPLOAD
# ============================================================

async def upload_to_telegram(metadata, source_url):

    # Empty caption: no title, file size, or duration text.
    caption = ""

    attributes = []

    if (
        metadata["duration"] > 0
        and metadata["width"] > 0
        and metadata["height"] > 0
    ):
        attributes.append(
            DocumentAttributeVideo(
                duration=metadata["duration"],
                w=metadata["width"],
                h=metadata["height"],
                supports_streaming=True
            )
        )

    buttons = None

    if GROUP_LINK.startswith(("https://t.me/", "http://t.me/")):
        buttons = [
            [Button.url(JOIN_BUTTON_TEXT, GROUP_LINK)]
        ]
    else:
        log("[CONFIG] Invalid GROUP_LINK; Join Group button omitted.")

    log(
        f"[TELEGRAM] Starting upload: {format_size(metadata['size'])}. "
        "Using Telethon send_file upload."
    )

    upload_progress.last_percent = -5

    # send_file uploads the local path directly. Do not pass `workers`:
    # this Telethon version's upload_file() does not accept that argument.
    await client.send_file(
        entity=CHANNEL_ID,
        file=str(metadata["path"]),
        thumb=(
            str(metadata["thumbnail"])
            if metadata["thumbnail"]
            else None
        ),
        caption=caption,
        supports_streaming=True,
        attributes=attributes or None,
        buttons=buttons,
        progress_callback=upload_progress
    )

    log("[TELEGRAM] Upload and message send completed.")


# ============================================================
# MAIN PROCESSING LOOP
# ============================================================

async def main():

    if (
        not API_ID
        or not API_HASH
        or not SESSION_STRING
        or not CHANNEL_ID_RAW
    ):

        raise RuntimeError(
            "Missing API_ID, API_HASH, SESSION_STRING "
            "or CHANNEL_ID environment variable."
        )

    if not GROUP_LINK.startswith(
        ("https://t.me/", "http://t.me/")
    ):

        log(
            "[CONFIG] GROUP_LINK is not a Telegram link. "
            "Join button will be omitted."
        )

    urls = load_urls()

    total = len(urls)

    log("========================================")
    log("[BOT] TELEGRAM VIDEO UPLOADER STARTED")
    log(f"[QUEUE] Total URLs: {total}")
    log(f"[QUEUE] Starting item: {START_FROM}")
    log(
        f"[SPEED] Download workers: {DOWNLOAD_WORKERS}"
    )
    log(
        f"[SPEED] Download chunk: {DOWNLOAD_CHUNK_MB} MB"
    )
    log(
        f"[SPEED] Telegram upload workers: "
        f"{TELEGRAM_UPLOAD_WORKERS}"
    )
    log("========================================")

    if START_FROM > total:
        log(
            "[QUEUE] START_FROM is beyond available URLs."
        )
        return

    await client.connect()

    if not await client.is_user_authorized():
        raise RuntimeError(
            "Telegram session is not authorized."
        )

    log("[TELEGRAM] Successfully connected.")

    uploaded_count = 0
    skipped_count = 0

    try:

        for index, page_url in enumerate(urls, start=1):

            if index < START_FROM:
                continue

            log("")
            log("========================================")
            log(f"[{index}/{total}] NEW ITEM")
            log(f"[SOURCE] {page_url}")
            log("========================================")

            title = f"URL item {index}"
            status = "SKIPPED"
            details = ""

            try:

                for attempt in range(1, MAX_RETRIES + 1):

                    try:

                        log(
                            f"[PROCESS] Download attempt "
                            f"{attempt}/{MAX_RETRIES}"
                        )

                        metadata = await asyncio.to_thread(
                            download_video,
                            page_url
                        )

                        title = metadata["title"]

                        log(
                            "[PROCESS] Video downloaded successfully."
                        )

                        await upload_to_telegram(
                            metadata,
                            page_url
                        )

                        status = "UPLOADED"
                        uploaded_count += 1

                        log(
                            f"[SUCCESS] {title} uploaded."
                        )

                        break

                    except FloodWaitError as exc:

                        details = (
                            f"Telegram FloodWait: {exc.seconds}s"
                        )

                        if attempt < MAX_RETRIES:

                            wait_seconds = exc.seconds + 3

                            log(
                                f"[TELEGRAM] FloodWait. "
                                f"Waiting {wait_seconds} seconds."
                            )

                            await asyncio.sleep(wait_seconds)

                        else:
                            raise

                    except Exception as exc:

                        details = clean_text(exc, 500)

                        log(f"[PROCESS ERROR] {details}")

                        if attempt < MAX_RETRIES:

                            log(
                                f"[RETRY] Waiting {RETRY_DELAY} seconds."
                            )

                            await asyncio.sleep(RETRY_DELAY)

                        else:
                            raise

            except Exception as exc:

                status = "SKIPPED"
                details = clean_text(exc, 500)
                skipped_count += 1

                log(
                    f"[FAILED] Item {index}: {details}"
                )

            finally:

                cleanup_files()

            log(f"[RESULT] Item {index}: {status}")

            save_github_record(
                index,
                status,
                title,
                page_url,
                details
            )

            log(
                f"[SUMMARY] Uploaded: {uploaded_count} | "
                f"Skipped: {skipped_count}"
            )

            log(f"[QUEUE] Finished item {index}.")

    finally:

        await client.disconnect()

        log("[TELEGRAM] Disconnected.")

    log("========================================")
    log("[BOT] ALL SELECTED ITEMS PROCESSED")
    log(f"[SUMMARY] Uploaded: {uploaded_count}")
    log(f"[SUMMARY] Skipped: {skipped_count}")
    log("========================================")


# ============================================================
# START APPLICATION
# ============================================================

if __name__ == "__main__":

    threading.Thread(
        target=run_web_server,
        daemon=True
    ).start()

    try:

        asyncio.run(main())

    except KeyboardInterrupt:

        log("[BOT] Stopped manually.")

    except Exception as error:

        log(f"[FATAL ERROR] {error}")
        raise
