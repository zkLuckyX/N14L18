"""Render-friendly Telegram media downloader bot.

Features: yt-dlp supported public URLs, quality selection, MP3, subtitles,
playlists (bounded), history/stats, progress, recursive file splitting, health route.

Important: no downloader can guarantee every site/video will always work. Private,
DRM-protected, geo-blocked, deleted, login-required, or extractor-broken media can fail.
"""
from __future__ import annotations

import asyncio
import html
import ipaddress
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, Update, BotCommand,
    InputMediaPhoto, InputMediaVideo,
)
from telegram.constants import ChatAction
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# --------------------------- Configuration ---------------------------
TOKEN = os.environ.get("BOT_TOKEN", "").strip()
if not TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Add it in Render Environment settings.")

# Telegram's standard Bot API currently accepts uploads up to 50 MB. Use a margin.
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_MIB", "45")) * 1024 * 1024
MAX_SOURCE_BYTES = int(os.environ.get("MAX_SOURCE_MIB", "1024")) * 1024 * 1024
MAX_PLAYLIST = max(1, min(int(os.environ.get("MAX_PLAYLIST", "10")), 20))
MAX_BATCH_LINKS = max(1, min(int(os.environ.get("MAX_BATCH_LINKS", "10")), 20))
BOT_VERSION = "v10-sequential-queue"
USER_COOLDOWN = max(0, int(os.environ.get("USER_COOLDOWN_SECONDS", "15")))
DOWNLOAD_TIMEOUT = max(90, int(os.environ.get("DOWNLOAD_TIMEOUT_SECONDS", "480")))
FFMPEG_TIMEOUT = max(60, int(os.environ.get("FFMPEG_TIMEOUT_SECONDS", "240")))
# Full software transcoding on Render Free is expensive. Limit it separately and
# prefer stream-copy/remux before ever encoding video frames.
VIDEO_TRANSCODE_TIMEOUT = max(30, min(240, int(os.environ.get("VIDEO_TRANSCODE_TIMEOUT_SECONDS", "120"))))
VIDEO_TRANSCODE_MAX_HEIGHT = max(360, min(1080, int(os.environ.get("VIDEO_TRANSCODE_MAX_HEIGHT", "720"))))
AUTO_DELETE_SECONDS = max(60, int(os.environ.get("AUTO_DELETE_SECONDS", "600")))
MAX_PARTS = 128
MAX_SPLIT_DEPTH = 8
WORK_DIR = os.environ.get("WORK_DIR", "/tmp")
STATS_FILE = os.environ.get("STATS_FILE", "/tmp/dlbot_stats.json")
HISTORY_FILE = os.environ.get("HISTORY_FILE", "/tmp/dlbot_history.json")
ADMIN_IDS = {
    int(value.strip()) for value in os.environ.get("ADMIN_USER_IDS", "").split(",")
    if value.strip().isdigit()
}

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
VIDEO_EXTS = {".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".mpeg", ".mpg", ".ts"}
AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".opus", ".ogg", ".wav", ".flac", ".weba"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}
MEDIA_EXTS = VIDEO_EXTS | AUDIO_EXTS | IMAGE_EXTS

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# httpx logs complete Telegram Bot API request URLs, which include the secret token.
# Never allow those URLs into application/Render logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

class _SecretRedactionFilter(logging.Filter):
    """Redact bot tokens even if a third-party exception contains an API URL."""
    _token_url = re.compile(r"bot[0-9]{5,}:[A-Za-z0-9_-]{20,}")

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        message = self._token_url.sub("bot[REDACTED]", message)
        if TOKEN:
            message = message.replace(TOKEN, "[REDACTED]")
        record.msg = message
        record.args = ()
        return True

_root_logger = logging.getLogger()
for _handler in _root_logger.handlers:
    _handler.addFilter(_SecretRedactionFilter())
log = logging.getLogger("media-bot")

_STATE_LOCK = threading.RLock()
_last_job: dict[int, float] = {}
_user_quality: dict[int, int | None] = {}
_active_users: set[int] = set()
_PENDING: dict[str, dict[str, Any]] = {}
_DELETE_TASKS: dict[tuple[int, int], asyncio.Task] = {}
BOT_STARTED = False
BOT_READY = False
LAST_TELEGRAM_OK = 0.0
SERVICE_BOOT_TIME = time.monotonic()

WELCOME = (
    "Hi! Send a public video or reel URL and I'll try to download it.\n\n"
    "Commands:\n"
    "/audio URL — download audio as MP3\n"
    "/sub URL — download available English/Hindi subtitles\n"
    "/playlist URL — download up to the configured playlist limit\n"
    f"Send up to {MAX_BATCH_LINKS} links in one message for sequential downloads.\n"
    "/q — choose a preferred video quality\n"
    "/auto — return to automatic quality selection\n"
    "/stats — view your download statistics\n"
    "/history — show your last five links\n"
    "/again NUMBER — download a recent link again\n"
    "/disk — show temporary disk usage\n\n"
    "Please download only media you own or have permission to save."
)

# --------------------------- Persistent-ish JSON state ---------------------------
def _read_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, ValueError, TypeError):
        return default


def _write_json_atomic(path: str, payload: Any) -> None:
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    temp_path = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(temp_path, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp_path, path)


def load_stats() -> dict[str, Any]:
    with _STATE_LOCK:
        value = _read_json(STATS_FILE, {})
        return value if isinstance(value, dict) else {}


def bump_stats(uid: int, kind: str, byte_count: int) -> None:
    """Count one source item; fields absent in older JSON files are added lazily."""
    with _STATE_LOCK:
        stats = _read_json(STATS_FILE, {})
        if not isinstance(stats, dict):
            stats = {}
        user = stats.setdefault(str(uid), {})
        defaults = {"videos": 0, "audios": 0, "images": 0, "files": 0,
                    "bundles": 0, "parts": 0, "mb": 0.0}
        for name, value in defaults.items():
            user.setdefault(name, value)
        key = {"audio": "audios", "image": "images", "video": "videos"}.get(kind, "files")
        user[key] = int(user.get(key, 0)) + 1
        if key != "files":
            user["files"] = int(user.get("files", 0)) + 1
        user["mb"] = round(float(user.get("mb", 0.0)) + max(0, byte_count) / 1048576, 1)
        _write_json_atomic(STATS_FILE, stats)


def bump_bundle_stats(uid: int, part_count: int) -> None:
    with _STATE_LOCK:
        stats = _read_json(STATS_FILE, {})
        if not isinstance(stats, dict):
            stats = {}
        user = stats.setdefault(str(uid), {})
        user.setdefault("bundles", 0)
        user.setdefault("parts", 0)
        user["bundles"] = int(user.get("bundles", 0)) + 1
        user["parts"] = int(user.get("parts", 0)) + max(0, int(part_count))
        _write_json_atomic(STATS_FILE, stats)


def add_history(uid: int, url: str) -> None:
    with _STATE_LOCK:
        history = _read_json(HISTORY_FILE, {})
        if not isinstance(history, dict):
            history = {}
        items = history.get(str(uid), [])
        if not isinstance(items, list):
            items = []
        items = [item for item in items if item != url]
        items.insert(0, url)
        history[str(uid)] = items[:5]
        _write_json_atomic(HISTORY_FILE, history)


def get_history(uid: int) -> list[str]:
    with _STATE_LOCK:
        history = _read_json(HISTORY_FILE, {})
        items = history.get(str(uid), []) if isinstance(history, dict) else []
        return items[:5] if isinstance(items, list) else []

# --------------------------- URL and subprocess helpers ---------------------------
def first_url(text: str | None) -> str | None:
    match = URL_RE.search(text or "")
    if not match:
        return None
    return match.group(0).rstrip(".,!;:)]}")


def extract_urls(text: str | None, limit: int | None = None) -> list[str]:
    """Return distinct HTTP(S) URLs in message order, trimming common punctuation."""
    cap = limit or MAX_BATCH_LINKS + 1
    urls: list[str] = []
    for match in URL_RE.finditer(text or ""):
        candidate = match.group(0).rstrip(".,!;:)]}")
        if candidate and candidate not in urls:
            urls.append(candidate)
        if len(urls) >= cap:
            break
    return urls


def _is_youtube_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    return host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com")


def _is_youtube_playlist_url(url: str) -> bool:
    """Identify regular YouTube playlist URLs, including watch URLs carrying list=."""
    if not _is_youtube_url(url):
        return False
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    return parsed.path.rstrip("/").lower() == "/playlist" or bool(query.get("list"))


def _youtube_video_id(url: str) -> str | None:
    """Extract a compact YouTube video ID so menu callbacks survive process restarts."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    candidate = ""
    if host == "youtu.be":
        candidate = parsed.path.strip("/").split("/", 1)[0]
    elif host == "youtube.com" or host.endswith(".youtube.com"):
        query = parse_qs(parsed.query)
        candidate = (query.get("v") or [""])[0]
        if not candidate:
            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) >= 2 and parts[0].lower() in {"shorts", "embed", "live", "v"}:
                candidate = parts[1]
    return candidate if re.fullmatch(r"[A-Za-z0-9_-]{6,20}", candidate or "") else None


def validate_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Please provide a valid public HTTP or HTTPS URL.")
    if parsed.username or parsed.password:
        raise ValueError("URLs containing embedded usernames or passwords are not accepted.")
    host = parsed.hostname.rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith((".local", ".internal", ".localhost")):
        raise ValueError("Local or internal URLs are not supported.")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None  # ordinary hostname, not a literal IP address
    if ip is not None and not ip.is_global:
        raise ValueError("Private or reserved IP addresses are not supported.")
    return url


def _base_env() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("DENO_DIR", "/tmp/deno")
    env.setdefault("XDG_CACHE_HOME", "/tmp/xdg")
    return env


def _cookie_args(workdir: str) -> list[str]:
    """Read an optional YouTube cookie secret; Instagram cookies are intentionally unused."""
    raw = os.environ.get("YOUTUBE_COOKIES", "").strip()
    if not raw:
        return []
    cookie_file = os.path.join(workdir, "cookies.txt")
    with open(cookie_file, "w", encoding="utf-8") as stream:
        stream.write(raw.replace("\\n", "\n"))
    os.chmod(cookie_file, 0o600)
    return ["--cookies", cookie_file]

def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=4)
    except Exception:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except Exception:
            pass


def _run_streaming_command(
    command: list[str], timeout: int, progress: dict[str, Any] | None = None
) -> tuple[int, list[str]]:
    """Run a command with a deadline, draining merged output to avoid pipe deadlocks."""
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        bufsize=1,
        env=_base_env(),
        start_new_session=(os.name == "posix"),
    )
    tail: deque[str] = deque(maxlen=60)

    def reader() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            line = line.rstrip()
            tail.append(line)
            if progress is not None and "PROGRESS|" in line:
                try:
                    payload = line.split("PROGRESS|", 1)[1].split("|")
                    progress["pct"] = payload[0].strip()
                    progress["spd"] = payload[1].strip() if len(payload) > 1 else ""
                    progress["done"] = int(float(payload[2] or 0)) if len(payload) > 2 else 0
                    progress["total"] = int(float(payload[3] or 0)) if len(payload) > 3 else 0
                except (ValueError, IndexError):
                    pass

    thread = threading.Thread(target=reader, daemon=True, name="subprocess-output")
    thread.start()
    deadline = time.monotonic() + timeout
    timed_out = False
    exceeded_size_limit = False
    while process.poll() is None:
        if progress is not None and int(progress.get("done", 0) or 0) > MAX_SOURCE_BYTES:
            exceeded_size_limit = True
            _stop_process(process)
            break
        if time.monotonic() >= deadline:
            timed_out = True
            _stop_process(process)
            break
        time.sleep(0.2)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _stop_process(process)
    thread.join(timeout=5)
    if timed_out:
        raise TimeoutError(f"Process exceeded its {timeout}-second time limit")
    if exceeded_size_limit:
        raise RuntimeError(f"The download exceeded the configured {MAX_SOURCE_BYTES // 1048576} MiB safety limit.")
    return int(process.returncode or 0), list(tail)


def _run_capture(command: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
        env=_base_env(),
        check=False,
    )


def _clean_ytdlp_error(lines: list[str]) -> str:
    useful = [line.strip() for line in lines if line.strip()]
    text = useful[-1] if useful else "The source site did not provide a downloadable format."
    text = re.sub(r"https?://\S+", "[URL]", text)
    return text[:240]

# --------------------------- yt-dlp extraction ---------------------------
def _is_instagram_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    return host == "instagram.com" or host.endswith(".instagram.com")


def _instagram_url_candidates(url: str) -> list[str]:
    """Try the supplied public Instagram URL, then a canonical tracking-free URL.

    This is a conservative URL normalization retry, not an authentication bypass.
    Private/login-gated content remains inaccessible without legitimate access.
    """
    candidates = [url]
    parsed = urlparse(url)
    match = re.search(r"/(reel|reels|p|tv)/([A-Za-z0-9_-]+)", parsed.path, re.IGNORECASE)
    if match:
        kind, shortcode = match.group(1).lower(), match.group(2)
        # 'reels' is commonly shared as 'reel'; keep canonical public route simple.
        if kind == "reels":
            kind = "reel"
        candidates.append(f"https://www.instagram.com/{kind}/{shortcode}/")
    return list(dict.fromkeys(candidates))


def _media_template(workdir: str) -> str:
    # A bounded title is part of the actual saved filename; ID + sequence prevent collisions.
    return os.path.join(workdir, "%(title).80B [%(id)s]_%(autonumber)03d.%(ext)s")


def _title_from_path(path: str) -> str:
    """Recover the descriptive title from our output template when metadata tags are absent."""
    stem = Path(path).stem
    match = re.match(r"^(.*?)\s+\[[^\]]+\]_\d{3}$", stem)
    if match and match.group(1).strip():
        return match.group(1).strip()[:200]
    legacy = re.sub(r"^media_\d+_", "", stem)
    return (legacy or stem or "Downloaded media")[:200]




def _safe_filename_title(title: str) -> str:
    title = re.sub(r'[\x00-\x1f<>:"/\\|?*]+', "_", title)
    title = re.sub(r"\s+", " ", title).strip(" ._-_")
    return (title or "Downloaded media")[:120]


def _rename_media_file(path: str, filename_stem: str) -> str:
    """Give normalized/remuxed outputs a stable title-based filename."""
    source = Path(path)
    target = source.with_name(f"{filename_stem}{source.suffix.lower()}")
    if source.resolve() == target.resolve():
        return str(source)
    if target.exists():
        target = source.with_name(f"{filename_stem}_{uuid.uuid4().hex[:6]}{source.suffix.lower()}")
    os.replace(source, target)
    return str(target)


def _is_instagram_post_url(url: str) -> bool:
    """True for /p/ links, which can contain image-only or mixed carousels."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    return (host == "instagram.com" or host.endswith(".instagram.com")) and bool(
        re.search(r"^/p/[A-Za-z0-9_-]+/?$", parsed.path, re.IGNORECASE)
    )


def _collect_media_files(workdir: str, printed_paths: list[str] | None = None) -> list[str]:
    """Collect completed media outputs only; ignore partials and sidecar files."""
    found: set[str] = set()
    for line in printed_paths or []:
        candidate = Path(line.strip().strip('"'))
        try:
            if candidate.is_file() and candidate.stat().st_size > 0 and candidate.suffix.lower() in MEDIA_EXTS and not candidate.name.endswith((".part", ".ytdl")):
                found.add(str(candidate.resolve()))
        except OSError:
            continue
    for path in Path(workdir).rglob("*"):
        try:
            if path.is_file() and path.stat().st_size > 0 and path.suffix.lower() in MEDIA_EXTS and not path.name.endswith((".part", ".ytdl")):
                found.add(str(path.resolve()))
        except OSError:
            continue
    return sorted(found)


def _download_instagram_thumbnail_fallback(
    url: str, workdir: str, progress: dict[str, Any] | None = None,
) -> tuple[list[str], str]:
    """Best-effort public thumbnail/image fallback for image-only /p/ posts.

    yt-dlp is video-focused, but its maintainer recommends --write-thumbnail with
    --ignore-no-formats-error for Instagram posts whose entries are photos. This
    runs only when the main extractor produced no media, so video thumbnails from
    a mixed carousel are never accidentally sent as if they were original photos.
    It uses no cookies and does not bypass an Instagram login wall.
    """
    thumb_dir = os.path.join(workdir, "ytdlp_thumbnail_fallback")
    shutil.rmtree(thumb_dir, ignore_errors=True)
    os.makedirs(thumb_dir, exist_ok=True)
    failures: list[str] = []
    for candidate in _instagram_url_candidates(url):
        command = [
            "yt-dlp", "--ignore-config", "--no-warnings", "--no-progress",
            "--ignore-no-formats-error", "--write-thumbnail", "--skip-download",
            "--yes-playlist", "--playlist-end", str(MAX_PLAYLIST),
            "--socket-timeout", "12", "--retries", "1", "--extractor-retries", "1",
            "--impersonate", os.environ.get("INSTAGRAM_IMPERSONATE", "chrome"),
            "-o", os.path.join(thumb_dir, "%(autonumber)03d_%(id)s.%(ext)s"),
            candidate,
        ]
        if progress is not None:
            progress.update(stage="Trying public Instagram thumbnail fallback", pct="", spd="", done=0, total=0)
        try:
            return_code, tail = _run_streaming_command(command, min(DOWNLOAD_TIMEOUT, 60), None)
        except FileNotFoundError:
            return [], "yt-dlp is not installed"
        except (TimeoutError, OSError, subprocess.SubprocessError) as exc:
            failures.append(type(exc).__name__)
            continue
        images = [
            path for path in _collect_media_files(thumb_dir, tail)
            if Path(path).suffix.lower() in IMAGE_EXTS and os.path.getsize(path) <= MAX_SOURCE_BYTES
        ]
        if images:
            log.info("Instagram public thumbnail fallback retrieved %d image(s)", len(images))
            return images, ""
        failures.append(_clean_ytdlp_error(tail) if tail else f"yt-dlp exited {return_code} without thumbnails")
    shutil.rmtree(thumb_dir, ignore_errors=True)
    return [], (failures[-1] if failures else "No public thumbnails were returned")


def _download_instagram_images(
    url: str,
    workdir: str,
    progress: dict[str, Any] | None = None,
    *,
    use_thumbnail_fallback: bool = False,
) -> tuple[list[str], str]:
    """Best-effort anonymous photo/carousel fallback without cookies.

    gallery-dl is tried first. For image-only posts with no video/media output,
    yt-dlp's write-thumbnail fallback is tried as a second public-access path.
    Both are bounded, and neither bypasses a login wall. If Instagram blocks the
    server or exposes no image URL, downloading without authenticated access cannot
    be guaranteed.
    """
    if not _is_instagram_post_url(url):
        return [], "Not an Instagram /p/ post"
    gallery_dir = os.path.join(workdir, "gallerydl_images")
    shutil.rmtree(gallery_dir, ignore_errors=True)
    os.makedirs(gallery_dir, exist_ok=True)
    command = [
        "gallery-dl", "--no-input", "--no-colors", "--quiet",
        "--directory", gallery_dir,
        "--filename", "{post_shortcode}_{num:>03}.{extension}",
        "--range", f"1-{MAX_PLAYLIST}",
        "--filter", "extension in ('jpg', 'jpeg', 'png', 'webp', 'avif')",
        url,
    ]
    if progress is not None:
        progress.update(stage="Fetching Instagram carousel photos", pct="", spd="", done=0, total=0)
    try:
        return_code, tail = _run_streaming_command(command, min(DOWNLOAD_TIMEOUT, 75), None)
    except FileNotFoundError:
        return_code, tail = 127, ["gallery-dl is not installed"]
    except (TimeoutError, OSError, subprocess.SubprocessError) as exc:
        return_code, tail = 1, [f"gallery-dl failed ({type(exc).__name__})"]

    images = [path for path in _collect_media_files(gallery_dir, tail)
              if Path(path).suffix.lower() in IMAGE_EXTS]
    oversized = [path for path in images if os.path.getsize(path) > MAX_SOURCE_BYTES]
    if oversized:
        return [], f"Instagram image exceeds the configured {MAX_SOURCE_BYTES // 1048576} MiB source limit"
    if images:
        log.info("Instagram anonymous image fallback retrieved %d photo(s)", len(images))
        return images, ""

    gallery_detail = _clean_ytdlp_error(tail) if tail else f"gallery-dl exited {return_code} without images"
    if use_thumbnail_fallback:
        thumb_images, thumb_error = _download_instagram_thumbnail_fallback(url, workdir, progress)
        if thumb_images:
            return thumb_images, ""
        return [], f"gallery-dl: {gallery_detail}; public thumbnail fallback: {thumb_error}"
    return [], gallery_detail


def _download_media(
    url: str,
    workdir: str,
    fmt: str,
    *,
    mp3: bool = False,
    subtitles: bool = False,
    progress: dict[str, Any] | None = None,
) -> list[str]:
    os.makedirs(workdir, exist_ok=True)
    is_instagram = _is_instagram_url(url)
    instagram_post = _is_instagram_post_url(url)
    command_base = [
        "yt-dlp", "--ignore-config", "--no-warnings", "--newline",
        "--retries", "2", "--fragment-retries", "2", "--extractor-retries", "1",
        "--file-access-retries", "2", "--socket-timeout", "15",
        "--concurrent-fragments", "4",
        "--remote-components", "ejs:github",
        "--print", "after_move:filepath",
        "-o", _media_template(workdir),
    ]
    # Instagram posts can mix video slides and photos. yt-dlp can save the video
    # slides while ignoring photo slides that have no video format.
    if is_instagram:
        command_base += ["--impersonate", os.environ.get("INSTAGRAM_IMPERSONATE", "chrome"), "--ignore-errors"]
    else:
        command_base += _cookie_args(workdir)
    if not is_instagram:
        command_base.append("--no-playlist")
    else:
        command_base += ["--yes-playlist", "--playlist-end", str(MAX_PLAYLIST)]
    if progress is not None:
        command_base += [
            "--progress-template",
            "download:PROGRESS|%(progress._percent_str)s|%(progress._speed_str)s|%(progress.downloaded_bytes)s|%(progress.total_bytes)s",
        ]
    if subtitles:
        command_base += ["--write-subs", "--write-auto-subs", "--sub-langs", "en,hi",
                         "--convert-subs", "srt", "--skip-download"]
    else:
        # Embeds title/uploader metadata where the source container supports it.
        command_base += ["--embed-metadata"]
    if mp3:
        command_base += [
            "-x", "--audio-format", "mp3", "--audio-quality", "0",
            "--parse-metadata", "%(uploader|)s:%(meta_artist)s",
        ]
    elif fmt and fmt != "best":
        command_base += ["-f", fmt]
    if not mp3 and not subtitles and not is_instagram:
        command_base += ["--merge-output-format", "mp4"]

    candidates = _instagram_url_candidates(url) if is_instagram else [url]
    return_code = 1
    tail: list[str] = []
    files: list[str] = []
    for candidate_index, candidate in enumerate(candidates):
        if candidate_index:
            # Remove partial downloads from the previous URL variant before retrying.
            for path in Path(workdir).rglob("*"):
                try:
                    if path.is_file() and (path.name.startswith("media_") or path.name.endswith((".part", ".ytdl"))):
                        path.unlink()
                except OSError:
                    pass
        command = command_base + [candidate]
        if progress is not None:
            progress["stage"] = "Downloading"
        timeout = min(DOWNLOAD_TIMEOUT, 150) if is_instagram else (DOWNLOAD_TIMEOUT if not subtitles else 240)
        try:
            return_code, tail = _run_streaming_command(command, timeout, progress)
        except TimeoutError as exc:
            if not is_instagram or candidate_index == len(candidates) - 1:
                raise
            return_code, tail = 124, [str(exc)]
        files = _collect_media_files(workdir, tail) if not subtitles else [
            str(path.resolve()) for path in Path(workdir).rglob("*")
            if path.is_file() and path.suffix.lower() in {".srt", ".vtt", ".ass", ".ssa"}
        ]
        # For Instagram, a playlist may return non-zero for photo-only slides but
        # still have produced valid video files. Keep those files instead of losing
        # the whole post; gallery-dl below handles its image slides separately.
        if files and (return_code == 0 or is_instagram):
            break
        if candidate_index < len(candidates) - 1:
            continue
        if return_code != 0:
            break

    if not subtitles and is_instagram:
        had_no_video_formats = any("no video formats found" in line.lower() for line in tail)
        # Add photo/carousel slides when yt-dlp reports entries with no video format.
        # For image-only posts, this is also the primary fallback after yt-dlp yields no files.
        if instagram_post and (not files or had_no_video_formats):
            photo_files, photo_error = _download_instagram_images(
                url, workdir, progress, use_thumbnail_fallback=not bool(files)
            )
            if photo_files:
                files = sorted(set(files + photo_files))
            elif not files:
                summary = (
                    "Instagram returned no video formats. This often means the /p/ post contains photos, "
                    "not video streams. The bot tried its no-cookie image fallback, but Instagram did not "
                    "expose downloadable public media. If Instagram requires sign-in or rate-limits the server, "
                    "a no-cookie download cannot be guaranteed."
                )
                log.warning("Instagram photo fallback did not return media (%s)", photo_error[:180])
                raise RuntimeError(summary)
            else:
                # Some carousel video slides were saved but its photo slides were not accessible anonymously.
                if progress is not None:
                    progress["instagram_gallery_warning"] = "Some photo slides were not available anonymously."
                log.warning("Instagram partial carousel: photo fallback unavailable (%s)", photo_error[:180])

    if not files:
        if return_code != 0 and not is_instagram:
            raise RuntimeError(_clean_ytdlp_error(tail))
        tail_message = _clean_ytdlp_error(tail)
        if is_instagram and not subtitles and "no video formats found" in tail_message.lower():
            raise RuntimeError(
                "Instagram exposed no video formats and the image fallback found no public files. "
                "The post may be image-only or inaccessible to anonymous requests."
            )
        raise RuntimeError(tail_message or "No media files were produced.")
    for path in files:
        if os.path.getsize(path) > MAX_SOURCE_BYTES:
            raise RuntimeError(
                f"The source file exceeds the configured {MAX_SOURCE_BYTES // 1048576} MiB safety limit."
            )
    return files

def _download_youtube_thumbnail(url: str, root_dir: str) -> str | None:
    """Fetch an image from a YouTube thumbnail CDN and normalize it to a small JPEG.

    The host allow-list prevents using extractor-supplied thumbnail URLs as an arbitrary
    server-side fetch target. The caller removes the returned temporary file after sending.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    allowed = host in {"i.ytimg.com", "img.youtube.com"} or host.endswith(".ytimg.com")
    if parsed.scheme != "https" or not allowed:
        return None
    os.makedirs(root_dir, exist_ok=True)
    nonce = uuid.uuid4().hex
    raw_path = os.path.join(root_dir, f"yt-thumb-{nonce}.source")
    jpg_path = os.path.join(root_dir, f"yt-thumb-{nonce}.jpg")
    try:
        request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(request, timeout=12) as response:
            content_type = response.headers.get_content_type().lower()
            if not content_type.startswith("image/"):
                return None
            data = response.read(2 * 1024 * 1024 + 1)
        if not data or len(data) > 2 * 1024 * 1024:
            return None
        with open(raw_path, "wb") as stream:
            stream.write(data)
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", raw_path,
            "-frames:v", "1", "-vf", "scale=640:640:force_original_aspect_ratio=decrease",
            "-q:v", "4", "-update", "1", jpg_path,
        ]
        if _run_ffmpeg(command, timeout=30) and os.path.isfile(jpg_path) and 0 < os.path.getsize(jpg_path) <= 200 * 1024:
            return jpg_path
        return None
    except (OSError, TimeoutError, subprocess.SubprocessError) as exc:
        log.info("Could not fetch YouTube thumbnail (%s)", type(exc).__name__)
        return None
    finally:
        try:
            os.remove(raw_path)
        except OSError:
            pass


def _get_video_meta(url: str) -> dict[str, Any] | None:
    with tempfile.TemporaryDirectory(prefix="dlmeta_", dir=WORK_DIR) as td:
        command = ["yt-dlp", "--ignore-config", "--dump-single-json", "--no-playlist", "--skip-download",
                   "--no-warnings", "--remote-components", "ejs:github"]
        command += _cookie_args(td)
        command.append(url)
        try:
            result = _run_capture(command, 90)
            if result.returncode != 0:
                return None
            info = json.loads(result.stdout)
            vids: dict[int, tuple[int, str]] = {}
            for fmt in info.get("formats", []):
                height = fmt.get("height")
                vcodec = fmt.get("vcodec")
                if not height or height > 1080 or not vcodec or vcodec == "none":
                    continue
                size = int(fmt.get("filesize") or fmt.get("filesize_approx") or 0)
                current = vids.get(int(height))
                if current is None or size > current[0]:
                    vids[int(height)] = (size, str(fmt.get("vcodec", "")))
            return {
                "title": str(info.get("title") or "Video")[:200],
                "duration": int(info.get("duration") or 0),
                "thumbnail": info.get("thumbnail"),
                "heights": sorted(vids.keys(), reverse=True),
                "sizes": {str(k): v[0] for k, v in vids.items()},
            }
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, ValueError) as exc:
            log.info("Could not read format metadata: %s", type(exc).__name__)
            return None


def _quality_selector(height: int) -> str:
    """Prefer Telegram-friendly H.264/AAC before a potentially expensive transcode.

    The requested height is a ceiling: if 1080p is only available as VP9/AV1,
    prefer an available lower-resolution AVC stream over forcing a slow transcode.
    """
    h = max(144, min(int(height), 1080))
    return (
        f"bv*[height={h}][ext=mp4][vcodec^=avc1]+ba[ext=m4a][acodec^=mp4a]/"
        f"b[height={h}][ext=mp4][vcodec^=avc1]/"
        f"bv*[height<={h}][ext=mp4][vcodec^=avc1]+ba[ext=m4a][acodec^=mp4a]/"
        f"b[height<={h}][ext=mp4][vcodec^=avc1]/"
        f"bv*[height<={h}][ext=mp4]+ba[ext=m4a]/"
        f"b[height<={h}][ext=mp4]/"
        f"bv*[height<={h}]+ba/b[height<={h}]"
    )

def _format_chain(user_id: int, force_fmt: str | None = None) -> list[str]:
    if force_fmt:
        match = re.search(r"height[<]?=?([0-9]+)", force_fmt)
        max_height = int(match.group(1)) if match else 1080
        heights = [h for h in [1080, 720, 480, 360] if h < max_height]
        # Honour the selected menu item first; only step down if its download or
        # preparation fails. Never retry the same top resolution several times.
        return list(dict.fromkeys([force_fmt] + [_quality_selector(h) for h in heights]))
    preferred = _user_quality.get(user_id)
    max_height = preferred or 1080
    heights = [h for h in [1080, 720, 480, 360] if h <= max_height]
    selectors = [_quality_selector(h) for h in heights]
    selectors.append(f"best[height<={max_height}]" if max_height else "best")
    return list(dict.fromkeys(selectors))

# --------------------------- FFprobe / splitting ---------------------------
def _probe_media(path: str) -> dict[str, Any]:
    try:
        result = _run_capture([
            "ffprobe", "-v", "error", "-show_entries",
            "stream=codec_type,codec_name,width,height:format=duration,format_name:format_tags=title,artist,album,performer,album_artist,uploader",
            "-of", "json", path,
        ], 30)
        if result.returncode != 0:
            return {}
        payload = json.loads(result.stdout or "{}")
        streams = payload.get("streams", [])
        video = next((stream for stream in streams if stream.get("codec_type") == "video"), {})
        audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), {})
        fmt = payload.get("format", {})
        duration = float(fmt.get("duration") or 0)
        raw_tags = fmt.get("tags") or {}
        tags = {str(key).lower(): str(value) for key, value in raw_tags.items() if value is not None}
        artist = tags.get("artist") or tags.get("performer") or tags.get("album_artist") or tags.get("uploader") or ""
        title = tags.get("title") or ""
        return {
            "duration": duration,
            "has_video": bool(video),
            "has_audio": bool(audio),
            "video_codec": str(video.get("codec_name") or ""),
            "audio_codec": str(audio.get("codec_name") or ""),
            "width": int(video.get("width") or 0),
            "height": int(video.get("height") or 0),
            "format_name": str(fmt.get("format_name") or ""),
            "title": title,
            "artist": artist,
            "tags": tags,
        }
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, ValueError):
        return {}


def _run_ffmpeg(command: list[str], timeout: int = FFMPEG_TIMEOUT) -> bool:
    try:
        result = _run_capture(command, timeout)
        if result.returncode != 0:
            log.warning("FFmpeg failed: %s", (result.stderr or result.stdout or "unknown error")[-300:])
            return False
        return True
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("FFmpeg could not finish: %s", type(exc).__name__)
        return False


def _run_ffmpeg_with_progress(
    command: list[str], *, timeout: int, progress: dict[str, Any] | None, duration: float
) -> bool:
    """Run FFmpeg with machine-readable progress so conversion doesn't look hung."""
    if not command or len(command) < 2:
        return False
    # `output` is the final argument for the generated command; insert progress
    # output options immediately before it, where FFmpeg's output options apply.
    monitored = command[:-1] + ["-progress", "pipe:1", "-nostats", command[-1]]
    try:
        process = subprocess.Popen(
            monitored, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            errors="replace", bufsize=1, env=_base_env(),
            start_new_session=(os.name == "posix"),
        )
    except OSError as exc:
        log.warning("FFmpeg could not start (%s)", type(exc).__name__)
        return False

    tail: deque[str] = deque(maxlen=40)
    def reader() -> None:
        assert process.stdout is not None
        current: dict[str, str] = {}
        for raw_line in process.stdout:
            line = raw_line.strip()
            if not line:
                continue
            tail.append(line)
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            current[key] = value
            if key in {"out_time_us", "out_time_ms"} and progress is not None and duration > 0:
                try:
                    seconds = int(value) / 1_000_000
                    pct = max(0.0, min(100.0, seconds * 100.0 / duration))
                    progress["pct"] = f"{pct:.1f}%"
                    # Conversion speed is reported separately below, and no fake MiB
                    # counters are shown for FFmpeg's time-based progress.
                    progress.pop("done", None)
                    progress.pop("total", None)
                except (ValueError, ZeroDivisionError):
                    pass
            elif key == "speed" and progress is not None:
                progress["spd"] = value
            elif key == "progress":
                current.clear()

    thread = threading.Thread(target=reader, daemon=True, name="ffmpeg-progress")
    thread.start()
    deadline = time.monotonic() + timeout
    timed_out = False
    while process.poll() is None:
        if time.monotonic() >= deadline:
            timed_out = True
            _stop_process(process)
            break
        time.sleep(0.25)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _stop_process(process)
    thread.join(timeout=5)
    if timed_out:
        raise TimeoutError(f"FFmpeg conversion exceeded its {timeout}-second time limit")
    if process.returncode != 0:
        log.warning("FFmpeg conversion failed: %s", " | ".join(tail)[-300:])
        return False
    if progress is not None:
        progress["pct"] = "100%"
        progress["spd"] = ""
    return True

def _normalize_video_for_telegram(
    path: str, root_dir: str, source_index: int, progress: dict[str, Any] | None = None
) -> str:
    """Prefer fast H.264/AAC MP4 remux; bound transcoding and never discard a valid source.

    Full video transcoding is the last resort. If it cannot complete within the short
    Render-friendly deadline, return a source file that fits Telegram's upload cap:
    MP4 is still sent as a video; other containers are delivered as documents rather
    than making the user wait through repeated 4-minute transcodes with no result.
    """
    info = _probe_media(path)
    ext = Path(path).suffix.lower()
    size = os.path.getsize(path) if os.path.isfile(path) else 0
    has_video = bool(info.get("has_video"))
    if not has_video and ext not in VIDEO_EXTS:
        return path

    os.makedirs(root_dir, exist_ok=True)
    output = os.path.join(root_dir, f"streamable_{source_index:03d}.mp4")
    try:
        os.remove(output)
    except OSError:
        pass

    video_codec = str(info.get("video_codec") or "").lower()
    audio_codec = str(info.get("audio_codec") or "").lower()
    duration = float(info.get("duration") or 0)
    if progress is not None:
        progress.update(stage="Preparing video", pct="0%", spd="", done=0, total=0)

    def original_fallback(reason: str) -> str:
        if size > 0:
            log.warning("Using original media fallback for %s (%s)", Path(path).name, reason)
            if progress is not None:
                progress.update(stage="Using original file", pct="100%", spd="")
            # Oversized sources continue into the normal splitting pipeline below.
            return path
        raise RuntimeError(f"Video conversion failed ({reason}) and the source file is empty or missing.")

    # If the probe fails on an MP4, do not burn CPU attempting a blind full transcode.
    # Send the file as a video and let Telegram/client playback determine compatibility.
    if not info:
        if ext == ".mp4":
            return original_fallback("media metadata could not be read")
        log.warning("FFprobe metadata unavailable for %s; attempting bounded conversion", Path(path).name)

    # AVC/H.264 + AAC can be remuxed in seconds without decoding video frames.
    # Even an MP4 source goes through this stream-copy step so +faststart is set for
    # Telegram streaming; this is not a re-encode and should be much faster than decode/encode.
    if has_video and video_codec == "h264" and audio_codec in {"", "aac"}:
        remux = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", path,
            "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn", "-c", "copy",
            "-movflags", "+faststart", "-map_metadata", "0", output,
        ]
        if _run_ffmpeg(remux, timeout=min(60, FFMPEG_TIMEOUT)) and os.path.isfile(output) and os.path.getsize(output) > 0:
            if progress is not None:
                progress.update(stage="Preparing video", pct="100%", spd="")
            return output
        try:
            os.remove(output)
        except OSError:
            pass
        # A valid MP4 source is safer than following a failed stream-copy with a 4-minute transcode.
        if ext == ".mp4":
            return original_fallback("fast remux failed")

    # Preserve H.264 video bit-for-bit when only the audio codec needs conversion.
    if has_video and video_codec == "h264" and audio_codec not in {"", "aac"}:
        audio_fix = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", path,
            "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "96k",
            "-movflags", "+faststart", "-map_metadata", "0", output,
        ]
        if _run_ffmpeg(audio_fix, timeout=min(90, FFMPEG_TIMEOUT)) and os.path.isfile(output) and os.path.getsize(output) > 0:
            final_info = _probe_media(output)
            if final_info.get("has_video") and final_info.get("video_codec") == "h264":
                return output
        try:
            os.remove(output)
        except OSError:
            pass
        if ext == ".mp4":
            return original_fallback("audio conversion failed")

    # True last resort: transcode to a maximum 720p H.264/AAC MP4. This is far faster
    # on a free-tier CPU than trying to preserve 1080p VP9/AV1. Already-compatible
    # 1080p sources bypass this branch entirely.
    max_h = VIDEO_TRANSCODE_MAX_HEIGHT
    max_w = int(round(max_h * 16 / 9))
    transcode = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", path,
        "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn",
        "-vf", f"scale=w='min({max_w},iw)':h='min({max_h},ih)':force_original_aspect_ratio=decrease:force_divisible_by=2",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "25", "-pix_fmt", "yuv420p",
        "-threads", "2", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart",
        "-map_metadata", "0", output,
    ]
    try:
        ok = _run_ffmpeg_with_progress(
            transcode, timeout=VIDEO_TRANSCODE_TIMEOUT, progress=progress, duration=duration
        )
    except TimeoutError as exc:
        log.warning("Video transcode deadline reached for %s", Path(path).name)
        try:
            os.remove(output)
        except OSError:
            pass
        return original_fallback(str(exc))

    if not ok or not os.path.isfile(output) or os.path.getsize(output) <= 0:
        try:
            os.remove(output)
        except OSError:
            pass
        return original_fallback("H.264/AAC transcode did not complete")

    final_info = _probe_media(output)
    if not final_info.get("has_video") or final_info.get("video_codec") != "h264":
        try:
            os.remove(output)
        except OSError:
            pass
        return original_fallback("transcode output was not valid H.264 video")
    if final_info.get("has_audio") and final_info.get("audio_codec") != "aac":
        try:
            os.remove(output)
        except OSError:
            pass
        return original_fallback("transcode output audio was not AAC")
    return output

def _create_video_thumbnail(path: str, root_dir: str, source_index: int, part_index: int) -> str | None:
    """Make a small JPEG preview Telegram can display before the uploaded video is opened."""
    output = os.path.join(root_dir, f"preview_{source_index:03d}_{part_index:03d}.jpg")
    info = _probe_media(path)
    duration = float(info.get("duration") or 0)
    seek = min(1.0, max(0.0, duration * 0.1))
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{seek:.3f}",
        "-i", path, "-map", "0:v:0", "-frames:v", "1",
        "-vf", "scale=320:320:force_original_aspect_ratio=decrease",
        "-q:v", "5", "-update", "1", output,
    ]
    if _run_ffmpeg(command, timeout=45) and os.path.isfile(output) and 0 < os.path.getsize(output) <= 200 * 1024:
        return output
    try:
        os.remove(output)
    except OSError:
        pass
    log.info("Could not create preview thumbnail for %s", Path(path).name)
    return None


def _copy_split_attempt(path: str, attempt_dir: str, segment_seconds: int, kind: str) -> list[str]:
    os.makedirs(attempt_dir, exist_ok=True)
    ext = Path(path).suffix.lower()
    if kind == "audio":
        audio_containers = {
            ".mp3": (".mp3", "mp3"), ".m4a": (".m4a", "ipod"),
            ".aac": (".aac", "adts"), ".opus": (".opus", "opus"),
            ".ogg": (".ogg", "ogg"), ".wav": (".wav", "wav"),
            ".flac": (".flac", "flac"), ".weba": (".webm", "webm"),
            ".webm": (".webm", "webm"),
        }
        if ext not in audio_containers:
            return []
        out_ext, container = audio_containers[ext]
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", path,
                   "-map", "0:a:0", "-vn", "-c:a", "copy", "-f", "segment",
                   "-segment_time", str(segment_seconds), "-reset_timestamps", "1",
                   "-segment_format", container, os.path.join(attempt_dir, f"part_%03d{out_ext}")]
    else:
        # MP4 segments may be sent as Telegram videos; Matroska is a safe fallback for other codecs.
        prefer_mp4 = ext in {".mp4", ".m4v", ".mov"}
        out_ext, container = (".mp4", "mp4") if prefer_mp4 else (".mkv", "matroska")
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", path,
                   "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn", "-c", "copy",
                   "-f", "segment", "-segment_time", str(segment_seconds),
                   "-reset_timestamps", "1", "-segment_format", container,
                   *(["-segment_format_options", "movflags=+faststart"] if container == "mp4" else []),
                   os.path.join(attempt_dir, f"part_%03d{out_ext}")]
        if prefer_mp4 and not _run_ffmpeg(command):
            # Retrying with Matroska preserves codecs that the MP4 container cannot carry.
            shutil.rmtree(attempt_dir, ignore_errors=True)
            os.makedirs(attempt_dir, exist_ok=True)
            out_ext, container = ".mkv", "matroska"
            command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", path,
                       "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn", "-c", "copy",
                       "-f", "segment", "-segment_time", str(segment_seconds),
                       "-reset_timestamps", "1", "-segment_format", container,
                       os.path.join(attempt_dir, f"part_%03d{out_ext}")]
            if not _run_ffmpeg(command):
                return []
        elif not prefer_mp4 and not _run_ffmpeg(command):
            return []
        return sorted(str(Path(attempt_dir) / name) for name in os.listdir(attempt_dir)
                      if name.startswith("part_") and Path(name).suffix.lower() == out_ext)
    if not _run_ffmpeg(command):
        return []
    return sorted(str(Path(attempt_dir) / name) for name in os.listdir(attempt_dir)
                  if name.startswith("part_") and Path(name).suffix.lower() == out_ext)


def _reencode_split_attempt(path: str, attempt_dir: str, segment_seconds: int) -> list[str]:
    """Last-resort video split: re-encode with keyframes to make boundaries more predictable."""
    os.makedirs(attempt_dir, exist_ok=True)
    output_pattern = os.path.join(attempt_dir, "part_%03d.mp4")
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", path,
        "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn",
        "-vf", "scale='min(1280,iw)':-2", "-c:v", "libx264", "-preset", "ultrafast",
        "-crf", "28", "-threads", "1", "-c:a", "aac", "-b:a", "96k",
        "-force_key_frames", f"expr:gte(t,n_forced*{segment_seconds})",
        "-f", "segment", "-segment_time", str(segment_seconds), "-reset_timestamps", "1",
        "-segment_format", "mp4", "-segment_format_options", "movflags=+faststart", output_pattern,
    ]
    if not _run_ffmpeg(command, timeout=FFMPEG_TIMEOUT):
        return []
    return sorted(str(Path(attempt_dir) / name) for name in os.listdir(attempt_dir)
                  if name.startswith("part_") and Path(name).suffix.lower() == ".mp4")


def _split_oversized(
    path: str,
    root_dir: str,
    kind: str,
    depth: int = 0,
) -> list[str]:
    """Return every part under the upload threshold or fail explicitly; never silently drop parts."""
    size = os.path.getsize(path)
    if size <= MAX_UPLOAD_BYTES:
        return [path]
    if depth >= MAX_SPLIT_DEPTH:
        raise RuntimeError("The file could not be split below Telegram's upload limit after several attempts.")
    if kind == "image":
        raise RuntimeError("This image is larger than the upload limit and cannot be split.")

    info = _probe_media(path)
    duration = float(info.get("duration") or 0)
    if duration <= 1:
        raise RuntimeError("FFprobe could not determine a usable duration for splitting this file.")

    last_error = "FFmpeg did not create smaller segments."
    for scale in (0.70, 0.52, 0.38, 0.27, 0.18):
        estimated = duration * (MAX_UPLOAD_BYTES / max(size, 1)) * scale
        seg_time = max(1, min(int(duration) - 1, int(estimated)))
        attempt_dir = tempfile.mkdtemp(prefix=f"split_{depth}_", dir=root_dir)
        parts = _copy_split_attempt(path, attempt_dir, seg_time, kind)
        # Retry with more conservative segments if stream-copy cut points cannot make progress.
        good_shape = len(parts) >= 2 and all(os.path.getsize(part) < size for part in parts)
        if good_shape:
            try:
                final: list[str] = []
                for part in parts:
                    final.extend(_split_oversized(part, attempt_dir, kind, depth + 1))
                    if len(final) > MAX_PARTS:
                        raise RuntimeError(f"Splitting would create more than {MAX_PARTS} parts.")
                if final and all(os.path.getsize(part) <= MAX_UPLOAD_BYTES for part in final):
                    if len(final) > MAX_PARTS:
                        raise RuntimeError(f"Splitting would create more than {MAX_PARTS} parts.")
                    return final
            except RuntimeError as exc:
                last_error = str(exc)
        else:
            last_error = "FFmpeg did not create at least two smaller segments."
        shutil.rmtree(attempt_dir, ignore_errors=True)

    if kind == "video":
        # One re-encode attempt is CPU-expensive; only use it after stream-copy splitting fails.
        attempt_dir = tempfile.mkdtemp(prefix=f"split_encode_{depth}_", dir=root_dir)
        seg_time = max(1, int(duration * (MAX_UPLOAD_BYTES / max(size, 1)) * 0.35))
        parts = _reencode_split_attempt(path, attempt_dir, seg_time)
        if len(parts) >= 2 and all(os.path.getsize(part) < size for part in parts):
            try:
                final = []
                for part in parts:
                    final.extend(_split_oversized(part, attempt_dir, kind, depth + 1))
                    if len(final) > MAX_PARTS:
                        raise RuntimeError(f"Splitting would create more than {MAX_PARTS} parts.")
                if final and all(os.path.getsize(part) <= MAX_UPLOAD_BYTES for part in final):
                    if len(final) > MAX_PARTS:
                        raise RuntimeError(f"Splitting would create more than {MAX_PARTS} parts.")
                    return final
            except RuntimeError as exc:
                last_error = str(exc)
        shutil.rmtree(attempt_dir, ignore_errors=True)
    raise RuntimeError(f"Could not safely split this file: {last_error}")


def _prepare_files(paths: list[str], root_dir: str, mp3: bool, progress: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    source_count = len(paths)
    for source_index, original_path in enumerate(paths, start=1):
        original_size = os.path.getsize(original_path)
        path = original_path
        ext = Path(path).suffix.lower()
        info = _probe_media(path)
        source_title = str(info.get("title") or _title_from_path(path))[:200]
        if ext in IMAGE_EXTS:
            kind = "image"
        elif mp3 or ext == ".mp3":
            kind = "audio"
        elif info.get("has_video") or (not info and ext in VIDEO_EXTS):
            kind = "video"
        elif info.get("has_audio") or ext in AUDIO_EXTS:
            kind = "audio"
        elif ext in VIDEO_EXTS:
            kind = "video"
        else:
            kind = "document"

        # Convert when fast remux is possible; if transcode is too expensive, the
        # normalizer keeps a deliverable original instead of silently losing the result.
        if kind == "video":
            path = _normalize_video_for_telegram(path, root_dir, source_index, progress=progress)
        output_info = _probe_media(path)
        source_title = str(output_info.get("title") or source_title or _title_from_path(original_path))[:200]
        safe_title = _safe_filename_title(source_title)
        path = _rename_media_file(path, f"{source_index:02d} - {safe_title}")
        output_ext = Path(path).suffix.lower()
        streamable_mp4 = (
            output_ext == ".mp4" and output_info.get("has_video")
            and output_info.get("video_codec") == "h264"
            and output_info.get("audio_codec") in {"", "aac"}
        )
        conversion_fallback = kind == "video" and not streamable_mp4
        if os.path.getsize(path) <= MAX_UPLOAD_BYTES:
            file_parts = [path]
        elif kind in {"video", "audio"}:
            file_parts = _split_oversized(path, root_dir, kind)
            named_parts: list[str] = []
            for split_index, split_path in enumerate(file_parts, start=1):
                named_parts.append(_rename_media_file(
                    split_path, f"{source_index:02d} - {safe_title} - Part {split_index}-of-{len(file_parts)}"
                ))
            file_parts = named_parts
        else:
            raise RuntimeError(f"File {Path(path).name} exceeds Telegram's upload limit and cannot be split.")
        if len(prepared) + len(file_parts) > MAX_PARTS:
            raise RuntimeError(f"This download would create more than {MAX_PARTS} upload parts.")
        for part_index, part in enumerate(file_parts, start=1):
            if os.path.getsize(part) > MAX_UPLOAD_BYTES:
                raise RuntimeError("Internal size check failed: a part is still too large to upload.")
            pinfo = _probe_media(part)
            part_ext = Path(part).suffix.lower()
            is_video = bool(pinfo.get("has_video")) or (not pinfo and part_ext in VIDEO_EXTS)
            is_audio = not is_video and (bool(pinfo.get("has_audio")) or part_ext in AUDIO_EXTS)
            thumbnail = _create_video_thumbnail(part, root_dir, source_index, part_index) if is_video and part_ext == ".mp4" else None
            prepared.append({
                "path": part, "kind": kind, "video": is_video, "audio": is_audio,
                "width": pinfo.get("width"), "height": pinfo.get("height"),
                "duration": int(pinfo.get("duration") or 0),
                "title": str(output_info.get("title") or source_title or _title_from_path(original_path))[:200],
                "artist": str(pinfo.get("artist") or "")[:100],
                "thumbnail": thumbnail,
                "conversion_fallback": conversion_fallback,
                "source_index": source_index, "source_count": source_count,
                "part_index": part_index, "part_count": len(file_parts),
                "source_bytes": original_size,
            })
    return prepared

# --------------------------- Telegram sending ---------------------------
async def _delete_after_delay(message, delay_seconds: int, key: tuple[int, int]) -> None:
    try:
        await asyncio.sleep(delay_seconds)
        try:
            await message.delete()
            log.debug("Auto-deleted message chat=%s id=%s", message.chat_id, message.message_id)
        except (BadRequest, TelegramError) as exc:
            # Message may have been manually deleted or chat permissions may prevent deletion.
            log.debug("Auto-delete skipped chat=%s id=%s (%s)",
                      getattr(message, "chat_id", "?"), getattr(message, "message_id", "?"), type(exc).__name__)
        except Exception as exc:
            log.warning("Unexpected auto-delete error (%s)", type(exc).__name__)
    finally:
        _DELETE_TASKS.pop(key, None)


def schedule_auto_delete(context: ContextTypes.DEFAULT_TYPE, message, delay_seconds: int | None = None) -> None:
    """Schedule deletion of a text/menu/source-link message, never a media upload."""
    delay = AUTO_DELETE_SECONDS if delay_seconds is None else max(1, int(delay_seconds))
    try:
        key = (int(message.chat_id), int(message.message_id))
        existing = _DELETE_TASKS.get(key)
        if existing is not None and not existing.done():
            return
        task = context.application.create_task(
            _delete_after_delay(message, delay, key),
            name=f"auto-delete-{message.message_id}"
        )
        _DELETE_TASKS[key] = task
    except Exception as exc:
        log.warning("Could not schedule message cleanup (%s)", type(exc).__name__)


async def reply_text_auto(context: ContextTypes.DEFAULT_TYPE, message, text: str, *args, **kwargs):
    """Send any bot text/menu/status and make it expire after the configured delay."""
    sent = await message.reply_text(text, *args, **kwargs)
    schedule_auto_delete(context, sent)
    return sent


async def safe_edit(message, text: str) -> None:
    try:
        await message.edit_text(text)
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            log.debug("Could not edit status message: %s", exc)
    except TelegramError as exc:
        log.debug("Could not edit status message: %s", type(exc).__name__)


async def _upload_retry(send_callable):
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            return await send_callable()
        except RetryAfter as exc:
            last_error = exc
            raw_delay = getattr(exc, "retry_after", 2)
            delay = raw_delay.total_seconds() if hasattr(raw_delay, "total_seconds") else float(raw_delay)
            await asyncio.sleep(min(delay + 1, 30))
        except (TimedOut, NetworkError) as exc:
            last_error = exc
            if attempt < 2:
                await asyncio.sleep(2 ** (attempt + 1))
        except TelegramError:
            raise
    raise RuntimeError(f"Telegram upload failed after retries: {type(last_error).__name__ if last_error else 'unknown error'}")


async def _send_prepared(message, item: dict[str, Any], caption: str) -> None:
    path = item["path"]
    size = os.path.getsize(path)
    if size > MAX_UPLOAD_BYTES:
        raise RuntimeError(f"Refusing to upload an oversized part: {Path(path).name}")
    ext = Path(path).suffix.lower()
    timeouts = {"read_timeout": 300, "write_timeout": 300, "connect_timeout": 30, "pool_timeout": 60}
    bot = message.get_bot()
    chat_id = message.chat_id

    if item["video"] and ext == ".mp4":
        async def send_video():
            with open(path, "rb") as stream:
                thumbnail_path = item.get("thumbnail")
                if thumbnail_path and os.path.isfile(thumbnail_path):
                    with open(thumbnail_path, "rb") as thumb_stream:
                        return await bot.send_video(
                            chat_id=chat_id,
                            video=stream,
                            thumbnail=thumb_stream,
                            caption=caption[:1024] if caption else None,
                            duration=item.get("duration") or None,
                            width=item.get("width") or None,
                            height=item.get("height") or None,
                            supports_streaming=True,
                            **timeouts,
                        )
                return await bot.send_video(
                    chat_id=chat_id,
                    video=stream,
                    caption=caption[:1024] if caption else None,
                    duration=item.get("duration") or None,
                    width=item.get("width") or None,
                    height=item.get("height") or None,
                    supports_streaming=True,
                    **timeouts,
                )
        await _upload_retry(send_video)
        return

    if item["audio"] and ext == ".mp3":
        async def send_audio():
            with open(path, "rb") as stream:
                return await bot.send_audio(
                    chat_id=chat_id,
                    audio=stream,
                    title=(item.get("title") or Path(path).stem)[:64],
                    performer=(item.get("artist") or None),
                    filename=Path(path).name,
                    caption=caption[:1024] if caption else None,
                    **timeouts,
                )
        await _upload_retry(send_audio)
        return

    if item["kind"] == "image" and size <= 9 * 1024 * 1024:
        async def send_photo():
            with open(path, "rb") as stream:
                return await bot.send_photo(chat_id=chat_id, photo=stream, caption=caption[:1024] if caption else None, **timeouts)
        try:
            await _upload_retry(send_photo)
            return
        except BadRequest:
            pass

    async def send_document():
        with open(path, "rb") as stream:
            return await bot.send_document(
                chat_id=chat_id,
                document=stream,
                filename=Path(path).name,
                caption=caption[:1024] if caption else None,
                **timeouts,
            )
    await _upload_retry(send_document)

async def _send_instagram_album(message, items: list[dict[str, Any]], total_sources: int) -> None:
    """Send a run of carousel photos/videos as one Telegram album, reopening streams on retries."""
    if len(items) < 2 or len(items) > 10:
        raise ValueError("Telegram albums must contain between 2 and 10 items.")

    async def send_album():
        streams = []
        media = []
        try:
            for index, item in enumerate(items, start=1):
                path = item["path"]
                stream = open(path, "rb")
                streams.append(stream)
                title = str(item.get("title") or "Instagram carousel")[:160]
                caption = (f"{title}\nInstagram carousel · {int(item.get('source_index') or index)}/{total_sources}"
                           if index == 1 else None)
                if item["kind"] == "image":
                    media.append(InputMediaPhoto(media=stream, caption=caption))
                elif item["video"] and Path(path).suffix.lower() == ".mp4":
                    thumbnail_path = item.get("thumbnail")
                    if thumbnail_path and os.path.isfile(thumbnail_path):
                        thumb_stream = open(thumbnail_path, "rb")
                        streams.append(thumb_stream)
                    else:
                        thumb_stream = None
                    media.append(InputMediaVideo(
                        media=stream, thumbnail=thumb_stream, caption=caption,
                        duration=item.get("duration") or None,
                        width=item.get("width") or None,
                        height=item.get("height") or None,
                        supports_streaming=True,
                    ))
                else:
                    raise ValueError("Unsupported media type for album")
            return await message.get_bot().send_media_group(chat_id=message.chat_id, media=media)
        finally:
            for stream in streams:
                try:
                    stream.close()
                except Exception:
                    pass

    try:
        await _upload_retry(send_album)
    except BadRequest as exc:
        # A source may produce a valid standalone file that Telegram rejects in an album.
        log.info("Instagram album upload was rejected; falling back to separate media (%s)", str(exc)[:120])
        for index, item in enumerate(items, start=1):
            source_index = int(item.get("source_index") or index)
            item_title = str(item.get("title") or "Instagram carousel")[:160]
            await _send_prepared(message, item, f"{item_title} · Instagram carousel {source_index}/{total_sources}")


def _album_eligible(item: dict[str, Any]) -> bool:
    if int(item.get("source_count", 1)) < 2 or int(item.get("part_count", 1)) != 1:
        return False
    path = str(item.get("path", ""))
    ext = Path(path).suffix.lower()
    if item.get("kind") == "image":
        return ext in {".jpg", ".jpeg", ".png"} and os.path.getsize(path) <= 9 * 1024 * 1024
    return bool(item.get("video")) and ext == ".mp4"


# --------------------------- Download flow and rate limiting ---------------------------
def _rate_remaining(uid: int) -> int:
    now = time.monotonic()
    if len(_last_job) > 10000:
        cutoff = now - USER_COOLDOWN - 60
        for old_uid, timestamp in list(_last_job.items()):
            if timestamp < cutoff:
                _last_job.pop(old_uid, None)
    last = _last_job.get(uid, 0.0)
    remaining = USER_COOLDOWN - (now - last)
    if remaining > 0:
        return int(remaining + 0.99)
    _last_job[uid] = now
    return 0


async def _progress_reporter(status, progress: dict[str, Any], stop_event: asyncio.Event) -> None:
    """Edit one status message with a compact live bar, percentage, and transfer speed."""
    last_text = ""
    while not stop_event.is_set():
        await asyncio.sleep(4)
        raw_pct = str(progress.get("pct") or "")
        if not raw_pct:
            continue
        match = re.search(r"-?\d+(?:\.\d+)?", raw_pct.replace(",", ""))
        if not match:
            continue
        percent = max(0.0, min(100.0, float(match.group(0))))
        width = 12
        filled = min(width, max(0, int(percent / 100 * width)))
        bar = "█" * filled + "░" * (width - filled)
        stage = str(progress.get("stage") or "Downloading")
        text = f"{stage}… [{bar}] {percent:.1f}%"
        if progress.get("spd"):
            text += f"\nSpeed: {progress['spd']}"
        done = int(progress.get("done") or 0)
        total = int(progress.get("total") or 0)
        if total > 0:
            text += f" · {done / 1048576:.1f}/{total / 1048576:.1f} MiB"
        if text != last_text:
            await safe_edit(status, text)
            last_text = text


async def _run_pipeline(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    url: str,
    *,
    mp3: bool = False,
    subtitles: bool = False,
    force_fmt: str | None = None,
    status=None,
    enforce_cooldown: bool = False,
) -> bool:
    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return False
    uid = user.id
    try:
        url = validate_url(url)
    except ValueError as exc:
        await reply_text_auto(context, message, str(exc))
        return False

    if status is None:
        status = await message.reply_text(
            "Fetching subtitles…" if subtitles else "Preparing your MP3…" if mp3 else "Starting download…"
        )
    add_history(uid, url)
    # Remove the user's source-link/command text later; uploaded media messages are not scheduled.
    schedule_auto_delete(context, message)
    loop = asyncio.get_running_loop()
    pool = context.bot_data["pool"]

    try:
        with tempfile.TemporaryDirectory(prefix="dlbot_", dir=WORK_DIR) as workdir:
            if subtitles:
                subdir = os.path.join(workdir, "subtitles")
                try:
                    files = await loop.run_in_executor(
                        pool,
                        lambda: _download_media(url, subdir, "best", subtitles=True),
                    )
                    sent = 0
                    oversized_subtitles = [name for name in files if os.path.getsize(name) > MAX_UPLOAD_BYTES]
                    for subfile in files[:6]:
                        if os.path.getsize(subfile) > MAX_UPLOAD_BYTES:
                            continue
                        async def send_sub(file_path=subfile):
                            with open(file_path, "rb") as stream:
                                return await message.get_bot().send_document(
                                    chat_id=message.chat_id, document=stream, filename=Path(file_path).name
                                )
                        await _upload_retry(send_sub)
                        sent += 1
                    if sent:
                        suffix = " Oversized subtitle files were not sent." if oversized_subtitles else ""
                        await safe_edit(status, f"Finished. Sent {sent} subtitle file(s).{suffix}")
                        schedule_auto_delete(context, status)
                        return True
                    if oversized_subtitles:
                        await safe_edit(status, "Subtitle files were found, but they exceed the Telegram upload limit.")
                        schedule_auto_delete(context, status)
                        return False
                    await safe_edit(status, "No subtitles were available in the requested languages.")
                    schedule_auto_delete(context, status)
                    return False
                finally:
                    shutil.rmtree(subdir, ignore_errors=True)

            chain = (
                ["best[ext=mp4]/best"] if _is_instagram_url(url) and not subtitles
                else (["best[ext=mp4]/best"] if mp3 else _format_chain(uid, force_fmt))
            )
            last_error = "No compatible format was found."
            for index, fmt in enumerate(chain, start=1):
                attempt_dir = os.path.join(workdir, f"attempt_{index}")
                progress: dict[str, Any] = {}
                stop_event = asyncio.Event()
                reporter = asyncio.create_task(_progress_reporter(status, progress, stop_event))
                try:
                    progress["stage"] = "Downloading"
                    await safe_edit(status, f"Downloading… (attempt {index}/{len(chain)})")
                    files = await loop.run_in_executor(
                        pool,
                        lambda d=attempt_dir, f=fmt: _download_media(
                            url, d, f, mp3=mp3, progress=progress
                        ),
                    )
                except (RuntimeError, TimeoutError, OSError, subprocess.SubprocessError) as exc:
                    last_error = str(exc)[:240] or type(exc).__name__
                    log.warning("Download attempt %s failed (%s)", index, type(exc).__name__)
                    stop_event.set()
                    reporter.cancel()
                    try:
                        await reporter
                    except asyncio.CancelledError:
                        pass
                    shutil.rmtree(attempt_dir, ignore_errors=True)
                    continue

                try:
                    progress.update(stage="Preparing video", pct="0%", spd="", done=0, total=0)
                    await safe_edit(status, "Download complete. Preparing a playable video…")
                    prepared = await loop.run_in_executor(
                        pool,
                        lambda: _prepare_files(files, attempt_dir, mp3, progress),
                    )
                except (RuntimeError, TimeoutError, OSError, subprocess.SubprocessError) as exc:
                    last_error = str(exc)[:240] or type(exc).__name__
                    log.warning("Splitting/preparation failed on attempt %s (%s)", index, type(exc).__name__)
                    stop_event.set()
                    reporter.cancel()
                    try:
                        await reporter
                    except asyncio.CancelledError:
                        pass
                    shutil.rmtree(attempt_dir, ignore_errors=True)
                    continue
                else:
                    stop_event.set()
                    reporter.cancel()
                    try:
                        await reporter
                    except asyncio.CancelledError:
                        pass

                sent = 0
                total_count = len(prepared)
                upload_error: str | None = None
                is_instagram = _is_instagram_url(url)
                part_index = 0
                while part_index < total_count:
                    item = prepared[part_index]
                    await safe_edit(status, f"Uploading file {part_index + 1}/{total_count} to Telegram…")
                    # Keep gallery order and send eligible Instagram carousel media as albums.
                    if is_instagram and _album_eligible(item):
                        batch = []
                        for candidate in prepared[part_index:part_index + 10]:
                            if not _album_eligible(candidate):
                                break
                            batch.append(candidate)
                        if len(batch) >= 2:
                            try:
                                await context.bot.send_chat_action(
                                    chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_PHOTO
                                )
                                await _send_instagram_album(message, batch, int(item.get("source_count") or len(batch)))
                                sent += len(batch)
                                part_index += len(batch)
                                if part_index < total_count:
                                    await asyncio.sleep(1.05)
                                continue
                            except Exception as exc:
                                upload_error = type(exc).__name__
                                log.exception("Instagram carousel album upload failed")
                                break
                    media_title = str(item.get("title") or _title_from_path(item["path"]) or "Downloaded media")[:180]
                    if item["part_count"] > 1:
                        caption = f"{media_title} — Part {item['part_index']}/{item['part_count']}"
                    elif item["source_count"] > 1:
                        caption = f"{media_title} — Media {item['source_index']}/{item['source_count']}"
                    elif item.get("conversion_fallback"):
                        caption = f"{media_title}\nOriginal format (playback support depends on your app)"
                    elif item.get("video") and item.get("height"):
                        caption = f"{media_title}\nDownloaded · {int(item['height'])}p"
                    else:
                        caption = media_title
                    try:
                        await context.bot.send_chat_action(
                            chat_id=update.effective_chat.id,
                            action=ChatAction.UPLOAD_VIDEO if item["video"] else ChatAction.UPLOAD_DOCUMENT,
                        )
                        await _send_prepared(message, item, caption)
                        sent += 1
                        await safe_edit(status, f"Uploaded {sent}/{total_count} file(s)…")
                        # Telegram limits repeated messages in one chat; pace multi-part uploads.
                        part_index += 1
                        if part_index < total_count:
                            await asyncio.sleep(1.05)
                    except Exception as exc:
                        upload_error = type(exc).__name__
                        log.exception("Upload failed for prepared part %s/%s", part_index + 1, total_count)
                        break

                if upload_error:
                    await safe_edit(status, f"Downloaded {sent}/{total_count} file(s), but an upload failed ({upload_error}). Please retry.")
                    schedule_auto_delete(context, status)
                    return False

                counted_sources: set[int] = set()
                for item in prepared:
                    source_index = int(item["source_index"])
                    if source_index in counted_sources:
                        continue
                    counted_sources.add(source_index)
                    source_parts = [part for part in prepared if int(part["source_index"]) == source_index]
                    source_bytes = sum(os.path.getsize(part["path"]) for part in source_parts)
                    source_kind = item.get("kind", "video")
                    if mp3 or item.get("audio"):
                        source_kind = "audio"
                    bump_stats(uid, source_kind, source_bytes)
                bump_bundle_stats(uid, sent)
                final_note = f"Done! Sent {sent} file(s) successfully."
                if progress.get("instagram_gallery_warning"):
                    final_note += " Some photo slides could not be fetched anonymously."
                await safe_edit(status, final_note)
                schedule_auto_delete(context, status)
                return True

            if _is_instagram_url(url):
                lowered = last_error.lower()
                if any(token in lowered for token in ("empty media response", "login", "cookies", "private", "not available", "requested content is not available")):
                    last_error = (
                        "Instagram did not expose this reel to anonymous server requests. The bot tried the original and a clean public URL without requiring cookies, but Instagram may be login-gating or rate-limiting this post. "
                        "Try again later or use a reel that is publicly accessible without signing in; the bot cannot guarantee downloads when Instagram blocks unauthenticated access."
                    )
            await safe_edit(status, f"Download failed: {last_error}")
            schedule_auto_delete(context, status)
            return False
    except Exception as exc:
        log.exception("Unexpected download pipeline failure")
        await safe_edit(status, f"Something went wrong ({type(exc).__name__}). Please try again later.")
        if status is not None:
            schedule_auto_delete(context, status)
        return False


async def download_job(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    url: str,
    *,
    mp3: bool = False,
    subtitles: bool = False,
    force_fmt: str | None = None,
) -> None:
    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return
    uid = user.id
    schedule_auto_delete(context, message)
    # No await before this check/set: handlers execute this critical section on one event loop.
    if uid in _active_users:
        await reply_text_auto(context, message, "You already have a job running. Please wait for it to finish.")
        return
    wait = _rate_remaining(uid)
    if wait:
        await reply_text_auto(context, message, f"Please wait {wait} second(s) before starting another download.")
        return
    _active_users.add(uid)
    try:
        async with context.bot_data["download_semaphore"]:
            await _run_pipeline(update, context, url, mp3=mp3, subtitles=subtitles, force_fmt=force_fmt)
    finally:
        _active_users.discard(uid)

def _expand_playlist_links(url: str) -> list[str]:
    """Resolve a bounded YouTube playlist to individual URLs using yt-dlp flat metadata."""
    with tempfile.TemporaryDirectory(prefix="dlqueue_playlist_", dir=WORK_DIR) as td:
        command = ["yt-dlp", "--ignore-config", "--dump-single-json", "--flat-playlist",
                   "--playlist-end", str(MAX_PLAYLIST), "--no-warnings", "--remote-components", "ejs:github"]
        command += _cookie_args(td)
        command.append(url)
        result = _run_capture(command, 120)
        if result.returncode != 0:
            detail = _clean_ytdlp_error((result.stdout + "\n" + result.stderr).splitlines())
            raise RuntimeError(detail or "Could not read this playlist.")
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError("The playlist response was not valid JSON.") from exc
        entries = payload.get("entries") or []
        links: list[str] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            target = entry.get("webpage_url") or entry.get("original_url")
            if not target:
                raw = entry.get("url")
                if isinstance(raw, str) and raw.startswith(("http://", "https://")):
                    target = raw
            if not target and entry.get("id"):
                extractor = str(entry.get("ie_key") or entry.get("extractor_key") or "").lower()
                if "youtube" in extractor or _is_youtube_url(url):
                    target = f"https://www.youtube.com/watch?v={entry['id']}"
            if target and target not in links:
                links.append(str(target))
            if len(links) >= MAX_PLAYLIST:
                break
        if not links:
            raise RuntimeError("No public items were found in this playlist. It may be private or unsupported.")
        return links


async def download_batch_job(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    urls: list[str],
) -> None:
    """Reserve this user's queue and serialize it with all other downloads."""
    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return
    uid = user.id
    if uid in _active_users:
        await reply_text_auto(context, message, "You already have a queue running. Please wait for it to finish.")
        return
    wait = _rate_remaining(uid)
    if wait:
        await reply_text_auto(context, message, f"Please wait {wait} second(s) before starting a new queue.")
        return
    _active_users.add(uid)
    try:
        async with context.bot_data["download_semaphore"]:
            await _download_batch_locked(update, context, urls)
    finally:
        _active_users.discard(uid)


async def _download_batch_locked(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    urls: list[str],
) -> None:
    message = update.effective_message
    setup_status = await reply_text_auto(context, message, "Preparing your sequential download queue…")
    expanded: list[str] = []
    setup_failures = 0
    pool = context.bot_data["pool"]
    for position, raw_url in enumerate(urls, start=1):
        if len(expanded) >= MAX_BATCH_LINKS:
            break
        try:
            url = validate_url(raw_url)
            if _is_youtube_playlist_url(url):
                await safe_edit(setup_status, f"Reading playlist link {position}/{len(urls)}…")
                playlist_links = await asyncio.get_running_loop().run_in_executor(
                    pool, lambda link=url: _expand_playlist_links(link)
                )
                remaining = MAX_BATCH_LINKS - len(expanded)
                expanded.extend(playlist_links[:remaining])
            else:
                expanded.append(url)
        except (ValueError, RuntimeError, OSError, subprocess.SubprocessError, TimeoutError) as exc:
            setup_failures += 1
            log.warning("Could not queue URL %s (%s)", position, type(exc).__name__)
    if not expanded:
        await safe_edit(setup_status, "No downloadable links could be queued. Check that the links are public and valid.")
        schedule_auto_delete(context, setup_status)
        return
    await safe_edit(setup_status, f"Queue ready: {len(expanded)} link(s). Downloads will run one at a time.")
    schedule_auto_delete(context, setup_status)
    successes = 0
    for index, link in enumerate(expanded, start=1):
        # Do not reply to the original user message here: its auto-delete timer may
        # have elapsed while earlier links were downloading.
        item_status = await context.bot.send_message(
            chat_id=message.chat_id,
            text=f"Queue item {index}/{len(expanded)}: starting…",
        )
        ok = await _run_pipeline(update, context, link, status=item_status)
        successes += int(ok)
        if index < len(expanded):
            await asyncio.sleep(1.05)
    summary = f"Queue finished: {successes}/{len(expanded)} link(s) downloaded successfully."
    if setup_failures:
        summary += f" {setup_failures} playlist/URL item(s) could not be added."
    summary_message = await context.bot.send_message(chat_id=message.chat_id, text=summary)
    schedule_auto_delete(context, summary_message)


# --------------------------- Commands ---------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        schedule_auto_delete(context, update.effective_message)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🎵 MP3 help", callback_data="help_mp3"),
        InlineKeyboardButton("⚙️ Quality", callback_data="quality_menu"),
        InlineKeyboardButton("📊 Stats", callback_data="help_stats"),
    ]])
    await reply_text_auto(context, update.effective_message, WELCOME, reply_markup=keyboard)


async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        schedule_auto_delete(context, update.effective_message)
    url = first_url(update.effective_message.text)
    if not url:
        await reply_text_auto(context, update.effective_message, "Usage: /audio https://example.com/video")
        return
    await download_job(update, context, url, mp3=True)


async def handle_sub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        schedule_auto_delete(context, update.effective_message)
    url = first_url(update.effective_message.text)
    if not url:
        await reply_text_auto(context, update.effective_message, "Usage: /sub https://example.com/video")
        return
    await download_job(update, context, url, subtitles=True)


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message:
        schedule_auto_delete(context, message)
    text = message.text or "" if message else ""
    if text.startswith("/"):
        return
    urls = extract_urls(text)
    if not urls:
        await reply_text_auto(context, message, "I couldn't find a valid URL. Please send a full HTTP/HTTPS link.")
        return
    if len(urls) > MAX_BATCH_LINKS:
        urls = urls[:MAX_BATCH_LINKS]
        await reply_text_auto(context, message, f"The queue is limited to {MAX_BATCH_LINKS} links per message; the first {MAX_BATCH_LINKS} will be processed.")
    if update.effective_user and update.effective_user.id in _active_users:
        await reply_text_auto(context, message, "You already have a download running. Your new links were not queued; send them again after the current queue finishes.")
        return
    if len(urls) > 1:
        await download_batch_job(update, context, urls)
        return
    url = urls[0]
    if _is_youtube_playlist_url(url):
        # A single playlist URL starts the bounded, sequential playlist flow automatically.
        await handle_playlist(update, context)
    elif _is_youtube_url(url):
        await youtube_menu(update, context, url)
    else:
        await download_job(update, context, url)


async def youtube_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str) -> None:
    try:
        url = validate_url(url)
    except ValueError as exc:
        await reply_text_auto(context, update.effective_message, str(exc))
        return
    # Source URL messages are temporary too; only bot text is cleaned by the helper below.
    schedule_auto_delete(context, update.effective_message)
    status = await update.effective_message.reply_text("Reading available video qualities…")
    meta = await asyncio.get_running_loop().run_in_executor(context.bot_data["pool"], lambda: _get_video_meta(url))
    if not meta:
        try:
            await status.delete()
        except TelegramError:
            pass
        await download_job(update, context, url)
        return

    options: list[tuple[str, str]] = []
    for height in meta["heights"]:
        size = int(meta["sizes"].get(str(height), 0))
        label_size = f" — {size / 1048576:.1f} MiB" if size else ""
        selector = _quality_selector(height)
        options.append((f"🎬 {height}p{label_size}", selector))
    options += [("🎵 Download MP3", "__MP3__"), ("⚡ Automatic quality", "__AUTO__")]
    if not options:
        try:
            await status.delete()
        except TelegramError:
            pass
        await download_job(update, context, url)
        return

    token = uuid.uuid4().hex[:8]
    video_id = _youtube_video_id(url)
    _PENDING[token] = {"url": url, "uid": update.effective_user.id, "options": options,
                      "created": time.monotonic(), "message_id": None}
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for index, (label, fmt) in enumerate(options):
        if video_id:
            owner = update.effective_user.id
            if fmt == "__MP3__":
                callback_data = f"ym:{video_id}:{owner}"
            elif fmt == "__AUTO__":
                callback_data = f"ya:{video_id}:{owner}"
            else:
                height_match = re.search(r"(1080|720|480|360|240|144)", label)
                height = int(height_match.group(1)) if height_match else 1080
                callback_data = f"yq:{video_id}:{height}:{owner}"
        else:
            # Fallback for unusual YouTube URL forms. These legacy tokens are process-local.
            callback_data = f"pick_{token}_{index}"
        row.append(InlineKeyboardButton(label, callback_data=callback_data))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    title = html.escape(meta["title"])
    duration = int(meta.get("duration") or 0)
    caption = f"<b>{title}</b>"
    if duration:
        caption += f"\nDuration: {duration // 60}:{duration % 60:02d}"
    caption += "\nQuality buttons select a maximum resolution; compatible H.264 streams are preferred for faster playback."
    try:
        await status.delete()
    except TelegramError:
        pass
    keyboard = InlineKeyboardMarkup(rows)
    sent = None
    thumbnail_url = str(meta.get("thumbnail") or "")
    thumbnail_path = None
    if thumbnail_url:
        thumbnail_path = await asyncio.get_running_loop().run_in_executor(
            context.bot_data["pool"], lambda: _download_youtube_thumbnail(thumbnail_url, WORK_DIR)
        )
    if thumbnail_path:
        try:
            with open(thumbnail_path, "rb") as photo_stream:
                sent = await update.effective_message.reply_photo(
                    photo=photo_stream,
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                    connect_timeout=20,
                    read_timeout=30,
                    write_timeout=30,
                )
            schedule_auto_delete(context, sent)
        except Exception as exc:
            log.info("Could not send YouTube thumbnail preview (%s); using text menu", type(exc).__name__)
        finally:
            try:
                os.remove(thumbnail_path)
            except OSError:
                pass
    if sent is None:
        sent = await reply_text_auto(
            context, update.effective_message, caption,
            parse_mode="HTML", reply_markup=keyboard
        )
    _PENDING[token]["message_id"] = sent.message_id
    # Bound memory if the process has many old menus.
    cutoff = time.monotonic() - AUTO_DELETE_SECONDS
    for old_token in list(_PENDING):
        if _PENDING[old_token].get("created", 0) < cutoff:
            _PENDING.pop(old_token, None)


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    # Self-contained YouTube callbacks remain valid if Render restarts and loses RAM state.
    if data.startswith(("yq:", "ya:", "ym:")):
        try:
            fields = data.split(":")
            action = fields[0]
            video_id = fields[1]
            if not re.fullmatch(r"[A-Za-z0-9_-]{6,20}", video_id):
                raise ValueError("invalid video id")
            if action == "yq:" or action == "yq":
                if len(fields) != 4:
                    raise ValueError("invalid quality callback")
                height, owner = int(fields[2]), int(fields[3])
            else:
                if len(fields) != 3:
                    raise ValueError("invalid callback")
                owner = int(fields[2])
                height = 0
            if owner != query.from_user.id:
                await reply_text_auto(context, query.message, "This download menu belongs to another user.")
                return
            source_url = f"https://www.youtube.com/watch?v={video_id}"
            if action == "ym":
                await download_job(update, context, source_url, mp3=True)
            elif action == "ya":
                _user_quality[query.from_user.id] = None
                await download_job(update, context, source_url)
            elif action == "yq":
                await download_job(update, context, source_url, force_fmt=_quality_selector(height))
            return
        except (ValueError, IndexError):
            await reply_text_auto(context, query.message, "This menu button is invalid. Send the video link again.")
            return
    if data == "quality_menu":
        await send_quality_menu(query.message, context)
        return
    if data == "help_mp3":
        await reply_text_auto(context, query.message, "To download audio, send: /audio https://example.com/video")
        return
    if data == "help_stats":
        await reply_text_auto(context, query.message, "Use /stats to view your download statistics.")
        return
    if data.startswith("setq_"):
        value = data.split("_", 1)[1]
        _user_quality[query.from_user.id] = None if value == "auto" else int(value)
        await reply_text_auto(context, query.message, "Quality preference set to automatic." if value == "auto"
                                       else f"Quality preference set to {value}p.")
        return
    if not data.startswith("pick_"):
        return
    try:
        _, token, index_text = data.split("_", 2)
        index = int(index_text)
    except (ValueError, TypeError):
        await reply_text_auto(context, query.message, "This menu is invalid. Send the link again.")
        return
    entry = _PENDING.get(token)
    if (not entry or entry.get("uid") != query.from_user.id or
            entry.get("message_id") != getattr(query.message, "message_id", None) or
            time.monotonic() - entry.get("created", 0) > AUTO_DELETE_SECONDS):
        await reply_text_auto(context, query.message, "This menu has expired. Please send the link again.")
        return
    try:
        label, fmt = entry["options"][index]
    except (IndexError, TypeError):
        await reply_text_auto(context, query.message, "That option is no longer available. Send the link again.")
        return
    # Keep this menu reusable until its normal 10-minute expiry. Users may
    # select another resolution, automatic quality, or MP3 from the same menu.
    if fmt == "__MP3__":
        await download_job(update, context, entry["url"], mp3=True)
    elif fmt == "__AUTO__":
        _user_quality[query.from_user.id] = None
        await download_job(update, context, entry["url"])
    else:
        await download_job(update, context, entry["url"], force_fmt=fmt)


async def send_quality_menu(message, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("1080p", callback_data="setq_1080"),
         InlineKeyboardButton("720p", callback_data="setq_720")],
        [InlineKeyboardButton("480p", callback_data="setq_480"),
         InlineKeyboardButton("360p", callback_data="setq_360")],
        [InlineKeyboardButton("Automatic", callback_data="setq_auto")],
    ])
    await reply_text_auto(context, message, "Choose your preferred maximum video quality:", reply_markup=keyboard)


async def handle_quality(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        schedule_auto_delete(context, update.effective_message)
    await send_quality_menu(update.effective_message, context)


async def handle_auto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        schedule_auto_delete(context, update.effective_message)
    _user_quality[update.effective_user.id] = None
    await reply_text_auto(context, update.effective_message, "Automatic quality selection is enabled.")


async def handle_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        schedule_auto_delete(context, update.effective_message)
    stats = load_stats().get(str(update.effective_user.id), {"videos": 0, "audios": 0, "mb": 0.0})
    await reply_text_auto(context, update.effective_message,
        f"Your download statistics:\nBundles: {stats.get('bundles', 0)}\n"
        f"Videos: {stats.get('videos', 0)}\nAudio files: {stats.get('audios', 0)}\n"
        f"Images: {stats.get('images', 0)}\nFiles: {stats.get('files', 0)}\n"
        f"Telegram parts sent: {stats.get('parts', 0)}\nTotal processed: {stats.get('mb', 0.0)} MiB"
    )


async def handle_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        schedule_auto_delete(context, update.effective_message)
    history = get_history(update.effective_user.id)
    if not history:
        await reply_text_auto(context, update.effective_message, "You don't have any recent links yet.")
        return
    lines = [f"{index}. {url[:150]}" for index, url in enumerate(history, start=1)]
    await reply_text_auto(context, update.effective_message, 
        "Your last five links:\n" + "\n".join(lines) + "\n\nUse /again NUMBER to download one again."
    )


async def handle_again(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        schedule_auto_delete(context, update.effective_message)
    parts = (update.effective_message.text or "").split()
    history = get_history(update.effective_user.id)
    if len(parts) != 2 or not parts[1].isdigit() or not 1 <= int(parts[1]) <= len(history):
        await reply_text_auto(context, update.effective_message, f"Usage: /again 1 (available range: 1–{max(len(history), 1)})")
        return
    await download_job(update, context, history[int(parts[1]) - 1])


async def handle_playlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    url = first_url(message.text)
    if not url:
        await reply_text_auto(context, message, "Usage: /playlist https://example.com/playlist")
        return
    user = update.effective_user
    if user.id in _active_users:
        await reply_text_auto(context, message, "You already have a job running. Please wait for it to finish.")
        return
    try:
        url = validate_url(url)
    except ValueError as exc:
        await reply_text_auto(context, message, str(exc))
        return
    schedule_auto_delete(context, message)
    wait = _rate_remaining(user.id)
    if wait:
        await reply_text_auto(context, message, f"Please wait {wait} second(s) before starting another download.")
        return
    _active_users.add(user.id)
    status = await reply_text_auto(context, message, "Reading the playlist…")
    try:
        async with context.bot_data["download_semaphore"]:
            with tempfile.TemporaryDirectory(prefix="dlplaylist_", dir=WORK_DIR) as td:
                command = ["yt-dlp", "--ignore-config", "--dump-single-json", "--flat-playlist",
                           "--playlist-end", str(MAX_PLAYLIST), "--no-warnings", "--remote-components", "ejs:github"]
                command += _cookie_args(td)
                command.append(url)
                try:
                    result = await asyncio.get_running_loop().run_in_executor(
                        context.bot_data["pool"], lambda: _run_capture(command, 120)
                    )
                    payload = json.loads(result.stdout) if result.returncode == 0 else {}
                    entries = payload.get("entries") or []
                    links = []
                    for entry in entries:
                        target = entry.get("webpage_url") or entry.get("original_url")
                        if not target:
                            raw = entry.get("url")
                            if isinstance(raw, str) and raw.startswith(("http://", "https://")):
                                target = raw
                        if target:
                            links.append(target)
                    links = links[:MAX_PLAYLIST]
                except (ValueError, OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
                    log.warning("Playlist extraction failed (%s)", type(exc).__name__)
                    links = []
            if not links:
                await safe_edit(status, "I couldn't read this playlist. It may be private, unsupported, or empty.")
                return
            await safe_edit(status, f"Found {len(links)} item(s). Starting sequential downloads…")
            successes = 0
            for index, link in enumerate(links, start=1):
                await safe_edit(status, f"Playlist item {index}/{len(links)}: downloading…")
                # Process sequentially as part of one user job; do not reapply the user cooldown per item.
                ok = await _run_pipeline(update, context, link, status=status)
                successes += int(ok)
                if index < len(links):
                    await asyncio.sleep(1.05)
            await safe_edit(status, f"Playlist finished: {successes}/{len(links)} item(s) sent successfully.")
    finally:
        _active_users.discard(user.id)


async def handle_disk(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        schedule_auto_delete(context, update.effective_message)
    try:
        stat = os.statvfs(WORK_DIR)
        total = stat.f_blocks * stat.f_frsize
        free = stat.f_bavail * stat.f_frsize
        used = total - free
        await reply_text_auto(context, update.effective_message, 
            f"Temporary disk usage:\nUsed: {used / 1048576:.0f} MiB\n"
            f"Free: {free / 1048576:.0f} MiB\nTotal: {total / 1048576:.0f} MiB\n"
            "This storage is temporary on Render Free."
        )
    except OSError as exc:
        await reply_text_auto(context, update.effective_message, f"Unable to read disk information ({type(exc).__name__}).")


async def handle_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        schedule_auto_delete(context, update.effective_message)
    if not ADMIN_IDS or update.effective_user.id not in ADMIN_IDS:
        await reply_text_auto(context, update.effective_message, "This diagnostic command is disabled for this account.")
        return
    url = first_url(update.effective_message.text)
    if not url:
        await reply_text_auto(context, update.effective_message, "Usage: /debug https://youtube.com/watch?v=...")
        return
    try:
        url = validate_url(url)
    except ValueError as exc:
        await reply_text_auto(context, update.effective_message, str(exc))
        return
    await reply_text_auto(context, update.effective_message, "Checking downloader and JavaScript runtime…")
    with tempfile.TemporaryDirectory(prefix="dldebug_", dir=WORK_DIR) as td:
        command = ["yt-dlp", "--ignore-config", "-F", "--no-playlist", "--verbose", "--remote-components", "ejs:github"]
        command += _cookie_args(td)
        command.append(url)
        try:
            result = await asyncio.get_running_loop().run_in_executor(
                context.bot_data["pool"], lambda: _run_capture(command, 90)
            )
            version = _run_capture(["yt-dlp", "--version"], 10).stdout.strip()
            out = (result.stdout + "\n" + result.stderr)[-2800:]
            await reply_text_auto(context, update.effective_message, f"yt-dlp: {version}\nExit code: {result.returncode}\n\n{out}")
        except (OSError, subprocess.TimeoutExpired) as exc:
            await reply_text_auto(context, update.effective_message, f"Diagnostic failed ({type(exc).__name__}).")

# --------------------------- Health endpoint and startup recovery ---------------------------
def start_health_server() -> None:
    port = int(os.environ.get("PORT", "10000"))

    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path not in {"/", "/health", "/healthz"}:
                self.send_response(404)
                self.end_headers()
                return
            now = time.monotonic()
            healthy = (BOT_READY and LAST_TELEGRAM_OK and now - LAST_TELEGRAM_OK < 600)
            still_starting = (not BOT_READY and now - SERVICE_BOOT_TIME < 180)
            status = 200 if healthy or still_starting else 503
            body = (b"ok\n" if status == 200 else b"telegram-unhealthy\n")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    def serve() -> None:
        try:
            ThreadingHTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()
        except Exception:
            log.exception("Health endpoint failed to start")

    threading.Thread(target=serve, daemon=True, name="health-server").start()


async def _telegram_heartbeat(app: Application) -> None:
    global LAST_TELEGRAM_OK
    while True:
        try:
            await app.bot.get_me(connect_timeout=10, read_timeout=10, pool_timeout=10)
            LAST_TELEGRAM_OK = time.monotonic()
        except Exception as exc:
            log.warning("Telegram heartbeat failed (%s)", type(exc).__name__)
        await asyncio.sleep(120)


async def post_init(app: Application) -> None:
    global BOT_READY, LAST_TELEGRAM_OK
    await app.bot.set_my_commands([
        BotCommand("start", "Show help and commands"),
        BotCommand("q", "Choose preferred video quality"),
        BotCommand("auto", "Enable automatic quality selection"),
        BotCommand("audio", "Download audio as MP3"),
        BotCommand("sub", "Download available subtitles"),
        BotCommand("playlist", "Download a playlist (limited items)"),
        BotCommand("stats", "Show your download statistics"),
        BotCommand("history", "Show recent links"),
        BotCommand("again", "Download a recent link again"),
        BotCommand("disk", "Show temporary disk usage"),
    ])
    BOT_READY = True
    LAST_TELEGRAM_OK = time.monotonic()
    # post_init runs before Application.start(); use a regular asyncio task and
    # cancel/await it in post_stop to avoid PTB's not-yet-running task warning.
    app.bot_data["heartbeat_task"] = asyncio.create_task(
        _telegram_heartbeat(app), name="telegram-heartbeat"
    )
    log.info("Telegram commands registered")


async def post_stop(app: Application) -> None:
    task = app.bot_data.pop("heartbeat_task", None)
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def build_application() -> Application:
    app = (Application.builder().token(TOKEN).concurrent_updates(4)
           .post_init(post_init).post_stop(post_stop).build())
    app.bot_data["pool"] = ThreadPoolExecutor(max_workers=2, thread_name_prefix="media-worker")
    app.bot_data["download_semaphore"] = asyncio.Semaphore(1)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("audio", handle_audio))
    app.add_handler(CommandHandler("sub", handle_sub))
    app.add_handler(CommandHandler("playlist", handle_playlist))
    app.add_handler(CommandHandler("stats", handle_stats))
    app.add_handler(CommandHandler("auto", handle_auto))
    app.add_handler(CommandHandler("q", handle_quality))
    app.add_handler(CommandHandler("history", handle_history))
    app.add_handler(CommandHandler("again", handle_again))
    app.add_handler(CommandHandler("disk", handle_disk))
    app.add_handler(CommandHandler("debug", handle_debug))
    app.add_handler(CallbackQueryHandler(handle_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_video))

    async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        error = context.error
        error_type = type(error).__name__ if error else "unknown"
        if error_type == "Conflict":
            log.critical(
                "Telegram polling conflict: another process/service is using this bot token. "
                "Keep exactly one active polling instance and rotate the token if it was exposed."
            )
        else:
            log.error("Telegram handler error: %s", error_type)
    app.add_error_handler(on_error)
    return app


def check_runtime() -> None:
    for executable in ("yt-dlp", "ffmpeg", "ffprobe"):
        if shutil.which(executable) is None:
            raise RuntimeError(f"Required executable '{executable}' was not found in PATH.")
    result = _run_capture(["yt-dlp", "--version"], 20)
    log.info("Bot version: %s", BOT_VERSION)
    log.info("yt-dlp version: %s", result.stdout.strip() or "unknown")
    deno = shutil.which("deno")
    log.info("Deno runtime: %s", deno or "not detected (some YouTube downloads may fail)")


def main() -> None:
    os.makedirs(WORK_DIR, exist_ok=True)
    check_runtime()
    start_health_server()
    delay = 2
    global BOT_READY, LAST_TELEGRAM_OK, SERVICE_BOOT_TIME
    while True:
        app = None
        pool = None
        BOT_READY = False
        LAST_TELEGRAM_OK = 0.0
        SERVICE_BOOT_TIME = time.monotonic()
        try:
            app = build_application()
            pool = app.bot_data["pool"]
            log.info("Starting Telegram bot polling")
            app.run_polling(bootstrap_retries=-1, drop_pending_updates=False)
            log.warning("Polling stopped cleanly; exiting restart loop")
            break
        except KeyboardInterrupt:
            log.info("Shutdown requested")
            break
        except Exception:
            log.exception("Bot process loop failed; retrying in %s seconds", delay)
            time.sleep(delay)
            delay = min(delay * 2, 60)
        finally:
            BOT_READY = False
            if pool is not None:
                pool.shutdown(wait=False, cancel_futures=True)
        delay = 2


if __name__ == "__main__":
    main()
