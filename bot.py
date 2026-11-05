import os
import re
import json
import time
import base64
import asyncio
import threading
import subprocess
from pathlib import Path
from urllib.parse import urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import unescape

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

CHANNEL_RAW = os.getenv("CHANNEL_ID", "")
CHANNEL_ID = (
    int(CHANNEL_RAW)
    if CHANNEL_RAW.lstrip("-").isdigit()
    else CHANNEL_RAW
)

PAGES_FILE = os.getenv("PAGES_FILE", "final_data.txt")
START_FROM = max(1, int(os.getenv("START_FROM", "1")))

# Automatically clean quotes or spaces from environment variables
GROUP_LINK = os.getenv("GROUP_LINK", "").strip().strip('"').strip("'")
JOIN_BUTTON_TEXT = os.getenv("JOIN_BUTTON_TEXT", "Join Group")

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY", "").strip()
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")
GITHUB_PROGRESS_FILE = os.getenv(
    "GITHUB_PROGRESS_FILE", "already_uploaded.txt"
)

PORT = int(os.getenv("PORT", "10000"))
MAX_RETRIES = max(1, int(os.getenv("MAX_RETRIES", "2")))
RETRY_DELAY = max(3, int(os.getenv("RETRY_DELAY", "10")))

# Download performance
DOWNLOAD_WORKERS = max(
    1, min(12, int(os.getenv("DOWNLOAD_WORKERS", "6")))
)

DOWNLOAD_CHUNK_MB = max(
    4, min(32, int(os.getenv("DOWNLOAD_CHUNK_MB", "16")))
)

DOWNLOAD_CHUNK = DOWNLOAD_CHUNK_MB * 1024 * 1024

# Telegram upload performance
UPLOAD_PART_SIZE_KB = 512

WORK_DIR = Path(os.getenv("WORK_DIR", "temp_downloads"))
WORK_DIR.mkdir(parents=True, exist_ok=True)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/156.0.0.0 Safari/537.36"
)

PAGE_REFERER = os.getenv(
    "PAGE_REFERER", "https://bindasmood.com/"
)


# ============================================================
# LOGGING
# ============================================================

def log(message):
    print(
        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}",
        flush=True
    )


def clean_text(value, limit=900):
    value = str(value or "")
    value = re.sub(r"[\x00-\x1f]+", " ", value)
    return " ".join(value.split())[:limit]


def format_size(size):
    size = float(size or 0)

    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.2f} {unit}"
        size /= 1024

    return "0 B"


def format_duration(seconds):
    seconds = int(seconds or 0)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)

    if hours:
        return f"{hours:02}:{minutes:02}:{secs:02}"

    return f"{minutes:02}:{secs:02}"


# ============================================================
# FLASK HEALTH SERVER
# ============================================================

app = Flask(__name__)


@app.get("/")
def health():
    return "Telegram uploader is running.", 200


def run_server():
    log("[SERVER] Starting health server.")
    app.run(host="0.0.0.0", port=PORT, threaded=True)


# ============================================================
# TELEGRAM CLIENT
# ============================================================

client = TelegramClient(
    StringSession(SESSION_STRING),
    API_ID,
    API_HASH,
    connection_retries=5,
    request_retries=5,
    auto_reconnect=True
)


# ============================================================
# FILE HELPERS
# ============================================================

def load_urls():
    path = Path(PAGES_FILE)

    if not path.exists():
        raise FileNotFoundError(f"{PAGES_FILE} not found.")

    with path.open("r", encoding="utf-8-sig") as f:
        return [
            line.strip()
            for line in f
            if line.strip()
            and not line.lstrip().startswith("#")
        ]


def cleanup_files():
    for item in WORK_DIR.iterdir():
        if item.is_file():
            try:
                item.unlink()
            except Exception as exc:
                log(f"[CLEANUP ERROR] {item.name}: {exc}")


# ============================================================
# GITHUB PROGRESS
# ============================================================

def github_api_url():
    if not GITHUB_REPOSITORY or "/" not in GITHUB_REPOSITORY:
        return None

    owner, repo = GITHUB_REPOSITORY.split("/", 1)

    return (
        f"https://api.github.com/repos/{owner}/{repo}"
        f"/contents/{GITHUB_PROGRESS_FILE}"
    )


def save_github_record(index, status, title, url, details=""):

    api_url = github_api_url()

    if not GITHUB_TOKEN or not api_url:
        log("[GITHUB] Configuration missing.")
        return False

    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "X-GitHub-Api-Version": "2022-11-28"
    }

    line = (
        f"{index}\t{status}\t"
        f"{clean_text(title, 250)}\t"
        f"{clean_text(url, 1000)}\t"
        f"{clean_text(details, 400)}"
    )

    for attempt in range(1, 7):

        try:
            # Always fetch the latest SHA before updating.
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

                content = base64.b64decode(
                    payload.get("content", "")
                    .replace("\n", "")
                ).decode("utf-8", errors="replace")

                old_lines = [
                    row for row in content.splitlines()
                    if row.strip()
                ]

            elif response.status_code != 404:
                response.raise_for_status()

            # Replace this item's old record, preserving others.
            prefix = f"{index}\t"

            old_lines = [
                row for row in old_lines
                if not row.startswith(prefix)
            ]

            new_content = (
                "\n".join(old_lines + [line]) + "\n"
            )

            body = {
                "message": f"Process item {index}: {status}",
                "content": base64.b64encode(
                    new_content.encode("utf-8")
                ).decode("ascii"),
                "branch": GITHUB_BRANCH
            }

            if sha:
                body["sha"] = sha

            saved = requests.put(
                api_url,
                headers=headers,
                json=body,
                timeout=30
            )

            if saved.status_code in (409, 422):
                log(
                    f"[GITHUB] Conflict on item {index}; "
                    f"retry {attempt}/6."
                )
                time.sleep(min(attempt * 2, 10))
                continue

            saved.raise_for_status()

            log(f"[GITHUB] Saved item {index}: {status}")
            return True

        except Exception as exc:
            log(f"[GITHUB ERROR] Attempt {attempt}/6: {exc}")

            if attempt < 6:
                time.sleep(min(attempt * 2, 10))

    return False


# ============================================================
# PAGE METADATA EXTRACTION
# ============================================================

def extract_page_metadata(page_url, session):

    log("[PAGE] Fetching webpage.")

    response = session.get(
        page_url,
        headers={"Referer": PAGE_REFERER},
        timeout=(20, 60),
        allow_redirects=True
    )

    log(f"[PAGE] HTTP {response.status_code}")
    response.raise_for_status()

    html = response.text

    normalized = (
        unescape(html)
        .replace(r"\/", "/")
        .replace(r"\u0026", "&")
    )

    # Find direct MP4 URLs.
    pattern = (
        r"""(?:https?:)?//[^\"'<>\s]+?\.mp4(?:\?[^\"'<>\s]*)?"""
        r"""|(?:\.\.?/|/)?[^\"'<>\s]+?\.mp4(?:\?[^\"'<>\s]*)?"""
    )

    matches = re.findall(
        pattern,
        normalized,
        re.IGNORECASE
    )

    video_urls = []

    for value in matches:
        value = value.strip().rstrip("),;")

        if value.startswith("//"):
            value = "https:" + value

        candidate = urljoin(response.url, value)

        if candidate not in video_urls:
            video_urls.append(candidate)

    # Extract OpenGraph title.
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

    # Extract poster.
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
    log(f"[PAGE] Video sources: {len(video_urls)}")
    log(f"[PAGE] Poster: {'Found' if poster_url else 'Not found'}")

    return title, video_urls, poster_url, response.url


# ============================================================
# DOWNLOAD PROGRESS
# ============================================================

class DownloadProgress:

    def __init__(self, total):
        self.total = total
        self.downloaded = 0
        self.last = -5
        self.lock = threading.Lock()

    def add(self, amount):
        with self.lock:
            self.downloaded += amount

            if not self.total:
                return

            percent = int(
                self.downloaded * 100 / self.total
            )

            step = (percent // 5) * 5

            if step >= self.last + 5 or percent >= 100:
                self.last = step

                log(
                    f"[DOWNLOAD] {percent}% | "
                    f"{format_size(self.downloaded)} / "
                    f"{format_size(self.total)}"
                )


# ============================================================
# PARALLEL RANGE DOWNLOAD
# ============================================================

def download_parallel(video_url, video_file, headers):

    probe_headers = dict(headers)
    probe_headers["Range"] = "bytes=0-0"

    log("[CDN] Checking byte-range support.")

    with requests.get(
        video_url,
        headers=probe_headers,
        stream=True,
        timeout=(20, 60)
    ) as response:

        log(f"[CDN] Probe HTTP {response.status_code}")

        if response.status_code != 206:
            return False

        content_range = response.headers.get(
            "Content-Range", ""
        )

        match = re.match(
            r"bytes\s+0-0/(\d+)",
            content_range,
            re.I
        )

        if not match:
            return False

        total_size = int(match.group(1))

        content_type = response.headers.get(
            "Content-Type", ""
        ).lower()

        if "text/html" in content_type:
            raise RuntimeError("CDN returned HTML instead of video.")

        if total_size <= 0:
            raise RuntimeError("Invalid video size.")

    # Create range chunks.
    ranges = []

    for start in range(0, total_size, DOWNLOAD_CHUNK):
        end = min(
            start + DOWNLOAD_CHUNK - 1,
            total_size - 1
        )
        ranges.append((start, end))

    workers = min(DOWNLOAD_WORKERS, len(ranges))

    log(
        f"[DOWNLOAD] Parallel download: "
        f"{workers} workers | "
        f"{len(ranges)} chunks | "
        f"{format_size(total_size)}"
    )

    fd = os.open(
        str(video_file),
        os.O_CREAT | os.O_WRONLY | os.O_TRUNC,
        0o644
    )

    os.ftruncate(fd, total_size)

    progress = DownloadProgress(total_size)

    def fetch_range(byte_range):

        start, end = byte_range
        expected = end - start + 1
        received = 0

        range_headers = dict(headers)
        range_headers["Range"] = f"bytes={start}-{end}"

        with requests.get(
            video_url,
            headers=range_headers,
            stream=True,
            timeout=(30, 180)
        ) as response:

            if response.status_code != 206:
                raise RuntimeError(
                    f"Range request returned HTTP "
                    f"{response.status_code}"
                )

            actual = response.headers.get(
                "Content-Range", ""
            )

            if not actual.lower().startswith(
                f"bytes {start}-{end}/"
            ):
                raise RuntimeError(
                    f"Unexpected Content-Range: {actual}"
                )

            for chunk in response.iter_content(
                chunk_size=1024 * 1024
            ):

                if not chunk:
                    continue

                offset = start + received
                view = memoryview(chunk)

                while view:
                    written = os.pwrite(
                        fd,
                        view,
                        offset
                    )

                    if written <= 0:
                        raise OSError("Disk write failed.")

                    view = view[written:]
                    offset += written

                received += len(chunk)
                progress.add(len(chunk))

        if received != expected:
            raise RuntimeError(
                f"Incomplete range: {received}/{expected}"
            )

    try:

        with ThreadPoolExecutor(
            max_workers=workers
        ) as pool:

            futures = [
                pool.submit(fetch_range, part)
                for part in ranges
            ]

            for future in as_completed(futures):
                future.result()

        os.fsync(fd)

    finally:
        os.close(fd)

    if video_file.stat().st_size != total_size:
        raise RuntimeError("Final downloaded file size mismatch.")

    log("[DOWNLOAD] Parallel download completed.")
    return True


# ============================================================
# NORMAL STREAM DOWNLOAD FALLBACK
# ============================================================

def download_single(video_url, video_file, headers):

    log("[DOWNLOAD] Using normal streaming mode.")

    with requests.get(
        video_url,
        headers=headers,
        stream=True,
        timeout=(30, 180)
    ) as response:

        log(f"[CDN] HTTP {response.status_code}")
        response.raise_for_status()

        if response.status_code != 200:
            raise RuntimeError(
                "CDN did not return a complete video response."
            )

        content_type = response.headers.get(
            "Content-Type", ""
        ).lower()

        if "text/html" in content_type:
            raise RuntimeError("CDN returned HTML.")

        total = int(
            response.headers.get("Content-Length", 0)
        )

        progress = DownloadProgress(total)

        with video_file.open("wb") as output:

            for chunk in response.iter_content(
                chunk_size=4 * 1024 * 1024
            ):

                if chunk:
                    output.write(chunk)
                    progress.add(len(chunk))

    if video_file.stat().st_size == 0:
        raise RuntimeError("Downloaded file is empty.")

    if total and video_file.stat().st_size != total:
        raise RuntimeError("Incomplete download.")

    log("[DOWNLOAD] Streaming download completed.")


# ============================================================
# VIDEO INFORMATION
# ============================================================

def probe_video(video_file):

    try:

        result = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries",
                "format=duration:stream=width,height",
                "-of", "json",
                str(video_file)
            ],
            capture_output=True,
            text=True,
            timeout=45,
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
            f"[VIDEO] {format_duration(duration)} | "
            f"{width}x{height}"
        )

        return duration, width, height

    except Exception as exc:
        log(f"[VIDEO] Metadata unavailable: {exc}")
        return 0, 0, 0


# ============================================================
# POSTER DOWNLOAD
# ============================================================

def download_poster(session, poster_url, page_url):

    if not poster_url:
        return None

    try:

        log("[POSTER] Downloading thumbnail.")

        response = session.get(
            poster_url,
            headers={"Referer": page_url},
            timeout=(15, 45)
        )

        response.raise_for_status()

        raw = WORK_DIR / "poster_source"
        output = WORK_DIR / "poster.jpg"

        raw.write_bytes(response.content)

        with Image.open(raw) as img:
            img.convert("RGB").save(
                output,
                "JPEG",
                quality=88
            )

        raw.unlink(missing_ok=True)

        log("[POSTER] Thumbnail ready.")
        return output

    except Exception as exc:
        log(f"[POSTER] Failed: {exc}")
        return None


# ============================================================
# MAIN DOWNLOAD FUNCTION
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

    title, video_urls, poster_url, final_page_url = (
        extract_page_metadata(page_url, session)
    )

    if not video_urls:
        raise RuntimeError(
            "No direct MP4 link found in webpage."
        )

    video_file = WORK_DIR / "video.mp4"

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Encoding": "identity",
        "Referer": final_page_url,
        "Connection": "keep-alive"
    }

    downloaded = False
    last_error = None

    for number, video_url in enumerate(video_urls, 1):

        log(
            f"[CDN] Trying source {number}/{len(video_urls)}: "
            f"{urlparse(video_url).netloc}"
        )

        try:

            downloaded = download_parallel(
                video_url,
                video_file,
                headers
            )

            if not downloaded:
                download_single(
                    video_url,
                    video_file,
                    headers
                )

                downloaded = True

            if downloaded:
                break

        except Exception as exc:

            last_error = exc

            log(f"[CDN ERROR] {exc}")

            video_file.unlink(missing_ok=True)

            downloaded = False

    if not downloaded:
        raise RuntimeError(
            f"All video sources failed: {last_error}"
        )

    thumb = download_poster(
        session,
        poster_url,
        final_page_url
    )

    duration, width, height = probe_video(video_file)

    size = video_file.stat().st_size

    log(
        f"[VIDEO READY] {title} | "
        f"{format_size(size)}"
    )

    session.close()

    return {
        "path": video_file,
        "thumbnail": thumb,
        "title": title,
        "duration": duration,
        "width": width,
        "height": height,
        "size": size
    }


# ============================================================
# TELEGRAM UPLOAD PROGRESS
# ============================================================

class UploadProgress:

    def __init__(self):
        self.last = -5

    def __call__(self, current, total):

        if not total:
            return

        percent = int(current * 100 / total)
        step = (percent // 5) * 5

        if step >= self.last + 5 or percent >= 100:

            self.last = step

            log(
                f"[TELEGRAM UPLOAD] {percent}% | "
                f"{format_size(current)} / "
                f"{format_size(total)}"
            )


# ============================================================
# TELEGRAM UPLOAD
# ============================================================

async def upload_to_telegram(metadata):

    title = clean_text(metadata["title"], 900)

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

    # Title as caption text above the video
    caption = title

    # Join button right below caption
    buttons = None

    if GROUP_LINK.startswith("https://t.me/"):
        buttons = [[
            Button.url(JOIN_BUTTON_TEXT, GROUP_LINK)
        ]]
    else:
        log(f"[CONFIG] Invalid or missing GROUP_LINK ({GROUP_LINK}); button omitted.")

    log(
        f"[TELEGRAM] Uploading {title} "
        f"({format_size(metadata['size'])})"
    )

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
        progress_callback=UploadProgress(),
        part_size_kb=UPLOAD_PART_SIZE_KB
    )

    log("[TELEGRAM] Upload successfully completed.")


# ============================================================
# MAIN PROCESSING
# ============================================================

async def main():

    if not all([
        API_ID,
        API_HASH,
        SESSION_STRING,
        CHANNEL_RAW
    ]):
        raise RuntimeError(
            "Missing Telegram environment variables."
        )

    urls = load_urls()

    total = len(urls)

    log("==========================================")
    log("[BOT] TELEGRAM VIDEO UPLOADER STARTED")
    log(f"[QUEUE] Total URLs: {total}")
    log(f"[QUEUE] Starting from: {START_FROM}")
    log(f"[SPEED] Download workers: {DOWNLOAD_WORKERS}")
    log(f"[SPEED] Download chunk: {DOWNLOAD_CHUNK_MB} MB")
    log(f"[SPEED] Upload part: {UPLOAD_PART_SIZE_KB} KB")
    log("==========================================")

    if START_FROM > total:
        log("[QUEUE] START_FROM exceeds queue length.")
        return

    await client.connect()

    if not await client.is_user_authorized():
        raise RuntimeError("Telegram session is not authorized.")

    log("[TELEGRAM] Connected successfully.")

    uploaded_count = 0
    failed_count = 0

    try:

        for index, page_url in enumerate(urls, start=1):

            if index < START_FROM:
                continue

            log("")
            log("==========================================")
            log(f"[{index}/{total}] PROCESSING ITEM")
            log(f"[SOURCE] {page_url}")
            log("==========================================")

            title = f"URL item {index}"
            status = "FAILED"
            details = ""

            try:

                for attempt in range(1, MAX_RETRIES + 1):

                    try:

                        log(
                            f"[PROCESS] Attempt "
                            f"{attempt}/{MAX_RETRIES}"
                        )

                        metadata = await asyncio.to_thread(
                            download_video,
                            page_url
                        )

                        title = metadata["title"]

                        log("[PROCESS] Download completed.")

                        await upload_to_telegram(metadata)

                        status = "UPLOADED"
                        uploaded_count += 1

                        log(f"[SUCCESS] {title}")

                        break

                    except FloodWaitError as exc:

                        details = (
                            f"Telegram FloodWait: {exc.seconds}s"
                        )

                        if attempt < MAX_RETRIES:

                            wait = exc.seconds + 3

                            log(
                                f"[TELEGRAM] Waiting {wait} seconds."
                            )

                            await asyncio.sleep(wait)

                        else:
                            raise

                    except Exception as exc:

                        details = clean_text(exc, 400)

                        log(f"[PROCESS ERROR] {details}")

                        if attempt < MAX_RETRIES:

                            log(
                                f"[RETRY] Waiting {RETRY_DELAY}s."
                            )

                            await asyncio.sleep(RETRY_DELAY)

                        else:
                            raise

            except Exception as exc:

                status = "FAILED"
                details = clean_text(exc, 400)
                failed_count += 1

                log(f"[FAILED] Item {index}: {details}")

            finally:

                cleanup_files()

            log(f"[RESULT] {index}: {status}")

            # Save both successful and failed records.
            await asyncio.to_thread(
                save_github_record,
                index,
                status,
                title,
                page_url,
                details
            )

            log(
                f"[SUMMARY] Uploaded: {uploaded_count} | "
                f"Failed: {failed_count}"
            )

            log(f"[QUEUE] Item {index} completed.")

    finally:

        await client.disconnect()

        log("[TELEGRAM] Disconnected.")

    log("==========================================")
    log("[BOT] PROCESSING FINISHED")
    log(f"[SUMMARY] Uploaded: {uploaded_count}")
    log(f"[SUMMARY] Failed: {failed_count}")
    log("==========================================")


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    threading.Thread(
        target=run_server,
        daemon=True
    ).start()

    try:
        asyncio.run(main())

    except KeyboardInterrupt:
        log("[BOT] Stopped manually.")

    except Exception as exc:
        log(f"[FATAL ERROR] {exc}")
        raise
