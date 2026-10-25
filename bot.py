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
from urllib.parse import urlparse

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
USER_COOLDOWN = max(0, int(os.environ.get("USER_COOLDOWN_SECONDS", "15")))
DOWNLOAD_TIMEOUT = max(120, int(os.environ.get("DOWNLOAD_TIMEOUT_SECONDS", "1200")))
FFMPEG_TIMEOUT = max(120, int(os.environ.get("FFMPEG_TIMEOUT_SECONDS", "600")))
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


def _cookie_args(workdir: str, env_name: str = "YOUTUBE_COOKIES") -> list[str]:
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return []
    # Set YOUTUBE_COOKIES to the Netscape cookie file contents; represent newlines as \n if needed.
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


def _media_template(workdir: str) -> str:
    # id and autonumber avoid collisions for posts containing multiple media items.
    return os.path.join(workdir, "media_%(autonumber)03d_%(id)s.%(ext)s")


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
    command = [
        "yt-dlp", "--ignore-config", "--no-warnings", "--newline",
        "--retries", "3", "--fragment-retries", "3", "--extractor-retries", "2",
        "--file-access-retries", "2", "--socket-timeout", "20",
        "--concurrent-fragments", "4", "--trim-filenames", "100",
        "--remote-components", "ejs:github",
        "-o", _media_template(workdir),
    ]
    command += _cookie_args(workdir, "INSTAGRAM_COOKIES" if is_instagram else "YOUTUBE_COOKIES")
    if not is_instagram:
        command.append("--no-playlist")
    else:
        # Instagram gallery posts are extractor playlists; retain item order and cap work.
        command += ["--yes-playlist", "--playlist-end", str(MAX_PLAYLIST)]
    if progress is not None:
        command += [
            "--progress-template",
            "download:PROGRESS|%(progress._percent_str)s|%(progress._speed_str)s|%(progress.downloaded_bytes)s|%(progress.total_bytes)s",
        ]
    if subtitles:
        command += ["--write-subs", "--write-auto-subs", "--sub-langs", "en,hi",
                    "--convert-subs", "srt", "--skip-download", "--no-warnings"]
    elif mp3:
        # yt-dlp embeds source title/artist/uploader metadata into the converted MP3.
        command += [
            "-x", "--audio-format", "mp3", "--audio-quality", "0",
            # If source music metadata lacks an artist, keep the channel/uploader as a useful fallback.
            "--parse-metadata", "%(uploader|)s:%(meta_artist)s", "--embed-metadata",
        ]
    else:
        if fmt and fmt != "best":
            command += ["-f", fmt]

    command.append(url)
    return_code, tail = _run_streaming_command(command, DOWNLOAD_TIMEOUT if not subtitles else 300, progress)
    if return_code != 0:
        raise RuntimeError(_clean_ytdlp_error(tail))

    if subtitles:
        files = [str(Path(workdir) / name) for name in os.listdir(workdir)
                 if name.lower().endswith((".srt", ".vtt", ".ass", ".ssa"))]
    else:
        files = [str(Path(workdir) / name) for name in os.listdir(workdir)
                 if name.startswith("media_") and Path(name).suffix.lower() in MEDIA_EXTS]
    files = sorted(files)
    if not files:
        raise RuntimeError(_clean_ytdlp_error(tail) or "No media files were produced.")
    # Enforce a disk safety ceiling after download.
    for path in files:
        if os.path.getsize(path) > MAX_SOURCE_BYTES:
            raise RuntimeError(
                f"The source file exceeds the configured {MAX_SOURCE_BYTES // 1048576} MiB safety limit."
            )
    return files


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


def _format_chain(user_id: int, force_fmt: str | None = None) -> list[str]:
    if force_fmt:
        match = re.search(r"height<=?(\d+)", force_fmt)
        max_height = int(match.group(1)) if match else 1080
        heights = [height for height in [1080, 720, 480, 360] if height <= max_height]
        selectors = [force_fmt]
    else:
        preferred = _user_quality.get(user_id)
        max_height = preferred or 1080
        heights = [height for height in [1080, 720, 480, 360] if height <= max_height]
        selectors = []
    for height in heights:
        # Prefer MP4/M4A when offered, then allow another compatible container.
        selector = (
            f"bv*[height<={height}][ext=mp4]+ba[ext=m4a]/"
            f"b[height<={height}][ext=mp4]/"
            f"bv*[height<={height}]+ba/b[height<={height}]"
        )
        if selector not in selectors:
            selectors.append(selector)
    # The fallback remains within the selected maximum; it never silently jumps to 4K.
    selectors.append(f"best[height<={max_height}]")
    return list(dict.fromkeys(selectors))

# --------------------------- FFprobe / splitting ---------------------------
def _probe_media(path: str) -> dict[str, Any]:
    try:
        result = _run_capture([
            "ffprobe", "-v", "error", "-show_entries",
            "stream=codec_type,width,height:format=duration,format_name:format_tags=title,artist,album,performer,album_artist,uploader",
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


def _prepare_files(paths: list[str], root_dir: str, mp3: bool) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    source_count = len(paths)
    for source_index, path in enumerate(paths, start=1):
        ext = Path(path).suffix.lower()
        info = _probe_media(path)
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
        if os.path.getsize(path) <= MAX_UPLOAD_BYTES:
            file_parts = [path]
        elif kind in {"video", "audio"}:
            file_parts = _split_oversized(path, root_dir, kind)
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
            prepared.append({
                "path": part, "kind": kind, "video": is_video, "audio": is_audio,
                "width": pinfo.get("width"), "height": pinfo.get("height"),
                "duration": int(pinfo.get("duration") or 0),
                "title": str(pinfo.get("title") or Path(path).stem)[:200],
                "artist": str(pinfo.get("artist") or "")[:100],
                "source_index": source_index, "source_count": source_count,
                "part_index": part_index, "part_count": len(file_parts),
                "source_bytes": os.path.getsize(path),
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


def _ensure_streamable_mp4(path: str) -> str:
    """Transcode a non-mp4 video (webm/mkv/avi) into a Telegram-streamable mp4."""
    out_path = str(Path(path).with_suffix(".mp4"))
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", path,
        "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        "-c:a", "aac", "-b:a", "128k", out_path,
    ]
    if not _run_ffmpeg(command) or not os.path.exists(out_path):
        return path
    if os.path.getsize(out_path) > MAX_UPLOAD_BYTES:
        os.remove(out_path)
        return path
    try:
        os.remove(path)
    except OSError:
        pass
    return out_path


async def _send_prepared(message, item: dict[str, Any], caption: str) -> None:
    path = item["path"]
    ext = Path(path).suffix.lower()
    # Videos that are not mp4 arrive unplayable in Telegram; convert before sending.
    if item.get("video") and ext != ".mp4":
        loop = asyncio.get_running_loop()
        converted = await loop.run_in_executor(loop.get_default_executor(), _ensure_streamable_mp4, path)
        if converted != path:
            item["path"] = converted
            item["width"], item["height"] = None, None
            ext = ".mp4"
            path = converted
    size = os.path.getsize(path)
    if size > MAX_UPLOAD_BYTES:
        raise RuntimeError(f"Refusing to upload an oversized part: {Path(path).name}")
    timeouts = {"read_timeout": 300, "write_timeout": 300, "connect_timeout": 30, "pool_timeout": 60}
    bot = message.get_bot()
    chat_id = message.chat_id

    if item["video"] and ext == ".mp4":
        async def send_video():
            with open(path, "rb") as stream:
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
        try:
            await _upload_retry(send_video)
            return
        except BadRequest as exc:
            # Some MP4s use codecs Telegram cannot preview; a document fallback still delivers the file.
            log.info("Video preview upload failed; trying document fallback (%s)", str(exc)[:120])

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
                caption = (f"Instagram carousel · {int(item.get('source_index') or index)}/{total_sources}"
                           if index == 1 else None)
                if item["kind"] == "image":
                    media.append(InputMediaPhoto(media=stream, caption=caption))
                elif item["video"] and Path(path).suffix.lower() == ".mp4":
                    media.append(InputMediaVideo(
                        media=stream, caption=caption,
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
            await _send_prepared(message, item, f"Instagram carousel · {source_index}/{total_sources}")


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
        text = f"Downloading… [{bar}] {percent:.1f}%"
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
        status = await reply_text_auto(context, message, 
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
                        return True
                    if oversized_subtitles:
                        await safe_edit(status, "Subtitle files were found, but they exceed the Telegram upload limit.")
                        return False
                    await safe_edit(status, "No subtitles were available in the requested languages.")
                    return False
                finally:
                    shutil.rmtree(subdir, ignore_errors=True)

            chain = ["best[ext=mp4]/best"] if mp3 else _format_chain(uid, force_fmt)
            last_error = "No compatible format was found."
            for index, fmt in enumerate(chain, start=1):
                attempt_dir = os.path.join(workdir, f"attempt_{index}")
                progress: dict[str, Any] = {}
                stop_event = asyncio.Event()
                reporter = asyncio.create_task(_progress_reporter(status, progress, stop_event))
                try:
                    await safe_edit(status, f"Downloading… (quality attempt {index}/{len(chain)})")
                    files = await loop.run_in_executor(
                        pool,
                        lambda d=attempt_dir, f=fmt: _download_media(
                            url, d, f, mp3=mp3, progress=progress
                        ),
                    )
                except (RuntimeError, TimeoutError, OSError, subprocess.SubprocessError) as exc:
                    last_error = str(exc)[:240] or type(exc).__name__
                    log.warning("Download attempt %s failed (%s)", index, type(exc).__name__)
                    shutil.rmtree(attempt_dir, ignore_errors=True)
                    continue
                finally:
                    stop_event.set()
                    reporter.cancel()
                    try:
                        await reporter
                    except asyncio.CancelledError:
                        pass

                try:
                    await safe_edit(status, "Download complete. Preparing files for Telegram…")
                    prepared = await loop.run_in_executor(
                        pool,
                        lambda: _prepare_files(files, attempt_dir, mp3),
                    )
                except (RuntimeError, TimeoutError, OSError, subprocess.SubprocessError) as exc:
                    last_error = str(exc)[:240] or type(exc).__name__
                    log.warning("Splitting/preparation failed on attempt %s (%s)", index, type(exc).__name__)
                    shutil.rmtree(attempt_dir, ignore_errors=True)
                    continue

                sent = 0
                total_count = len(prepared)
                upload_error: str | None = None
                is_instagram = _is_instagram_url(url)
                part_index = 0
                while part_index < total_count:
                    item = prepared[part_index]
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
                    if item["part_count"] > 1:
                        caption = f"Media {item['source_index']}/{item['source_count']} — Part {item['part_index']}/{item['part_count']}"
                    elif item["source_count"] > 1:
                        caption = f"Media {item['source_index']}/{item['source_count']}"
                    else:
                        caption = "Downloaded successfully"
                    try:
                        await context.bot.send_chat_action(
                            chat_id=update.effective_chat.id,
                            action=ChatAction.UPLOAD_VIDEO if item["video"] else ChatAction.UPLOAD_DOCUMENT,
                        )
                        await _send_prepared(message, item, caption)
                        sent += 1
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
                await safe_edit(status, f"Done! Sent {sent} file(s) successfully.")
                return True

            await safe_edit(status, f"Download failed after retries: {last_error}")
            return False
    except Exception as exc:
        log.exception("Unexpected download pipeline failure")
        await safe_edit(status, f"Something went wrong ({type(exc).__name__}). Please try again later.")
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
    if update.effective_message:
        schedule_auto_delete(context, update.effective_message)
    text = update.effective_message.text or ""
    if text.startswith("/"):
        return
    url = first_url(text)
    if not url:
        await reply_text_auto(context, update.effective_message, "I couldn't find a valid URL. Please send a full HTTP/HTTPS link.")
        return
    if _active_users and update.effective_user and update.effective_user.id in _active_users:
        await reply_text_auto(context, update.effective_message, "You already have a download running. Please wait.")
        return
    host = (urlparse(url).hostname or "").lower()
    if host == "youtube.com" or host.endswith(".youtube.com") or host == "youtu.be":
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
    status = await reply_text_auto(context, update.effective_message, "Reading available video qualities…")
    meta = await asyncio.get_running_loop().run_in_executor(context.bot_data["pool"], lambda: _get_video_meta(url))
    if not meta:
        await safe_edit(status, "Could not read quality options. Starting an automatic download instead…")
        await download_job(update, context, url)
        return

    options: list[tuple[str, str]] = []
    for height in meta["heights"]:
        size = int(meta["sizes"].get(str(height), 0))
        label_size = f" — {size / 1048576:.1f} MiB" if size else ""
        options.append((f"🎬 {height}p{label_size}", f"bv*[height<={height}]+ba/b[height<={height}]"))
    options += [("🎵 Download MP3", "__MP3__"), ("⚡ Automatic quality", "__AUTO__")]
    if not options:
        await safe_edit(status, "No quality menu is available. Starting an automatic download…")
        await download_job(update, context, url)
        return

    token = uuid.uuid4().hex[:8]
    _PENDING[token] = {"url": url, "uid": update.effective_user.id, "options": options,
                      "created": time.monotonic(), "message_id": None}
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for index, (label, _) in enumerate(options):
        row.append(InlineKeyboardButton(label, callback_data=f"pick_{token}_{index}"))
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
    try:
        await status.delete()
    except TelegramError:
        pass
    markup = InlineKeyboardMarkup(rows)
    sent = None
    thumb = str(meta.get("thumbnail") or "")
    if thumb:
        try:
            sent = await update.effective_message.reply_photo(
                photo=thumb, caption=caption, parse_mode="HTML", reply_markup=markup
            )
        except TelegramError as exc:
            log.info("Thumbnail menu failed, using text menu (%s)", str(exc)[:100])
    if sent is None:
        sent = await reply_text_auto(context, update.effective_message,
                                     caption, parse_mode="HTML", reply_markup=markup)
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
    _PENDING.pop(token, None)
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
    app.create_task(_telegram_heartbeat(app), name="telegram-heartbeat")
    log.info("Telegram commands registered")


def build_application() -> Application:
    app = Application.builder().token(TOKEN).concurrent_updates(4).post_init(post_init).build()
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
        log.error("Telegram handler error: %s", type(error).__name__ if error else "unknown")
    app.add_error_handler(on_error)
    return app


def check_runtime() -> None:
    for executable in ("yt-dlp", "ffmpeg", "ffprobe"):
        if shutil.which(executable) is None:
            raise RuntimeError(f"Required executable '{executable}' was not found in PATH.")
    result = _run_capture(["yt-dlp", "--version"], 20)
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
