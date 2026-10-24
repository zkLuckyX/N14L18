"""
Pro Video Downloader Bot v2  (Telegram, yt-dlp based)
Features: all-platform download, quality ladder 1080->720->480->360,
auto-split 49MB+, playlist mode, subtitles, trim, mp3, quality choice buttons,
per-user stats, rate limit, auto-update yt-dlp, deno auto-install, keepalive.

Run:  BOT_TOKEN=*** python3 bot.py
"""
import os, re, json, time, shutil, tempfile, subprocess, logging, asyncio, threading
from concurrent.futures import ThreadPoolExecutor

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dlbot")

TOKEN = os.environ["BOT_TOKEN"]
MAX_BYTES = 49 * 1024 * 1024
URL_RE = re.compile(r"https?://\S+", re.I)
STATS_FILE = "/tmp/dlbot_stats.json"
MAX_PLAYLIST = 10
COOLDOWN = 20  # seconds between downloads per user

YO = ("yo! kisi bhi platform ka video/reel link bhejo (instagram, youtube, tiktok, "
      "twitter/x, facebook, pinterest, reddit...), best quality me dunga\n\n"
      "commands:\n/audio <url> - sirf mp3\n/sub <url> - subtitles ke sath\n"
      "/trim <url> <start> <end> - jaise /trim <url> 0:00 1:00\n"
      "/playlist <url> - poori playlist (max 10)\n/q - quality chuno (1080/720/480/360)\n"
      "/stats - teri downloads\n/auto - auto quality wapas")

# ---------- stats ----------
_stats_lock = threading.Lock()
def load_stats():
    try:
        return json.load(open(STATS_FILE))
    except Exception:
        return {}
def bump_stats(uid, kind, size=0):
    with _stats_lock:
        s = load_stats()
        u = s.setdefault(str(uid), {"videos": 0, "audios": 0, "mb": 0.0})
        u["videos" if kind == "video" else "audios"] += 1
        u["mb"] += round(size / 1048576, 1)
        json.dump(s, open(STATS_FILE, "w"))

# ---------- rate limit ----------
_last_dl = {}
def rate_ok(uid):
    now = time.time()
    last = _last_dl.get(uid, 0)
    if now - last < COOLDOWN:
        return int(COOLDOWN - (now - last))
    _last_dl[uid] = now
    return 0

# ---------- user quality pref (in-memory) ----------
user_quality = {}   # uid -> int or None (auto)
pending = {}        # uid -> {"url":..., "opts":[(label, fmt), ...]} youtube menu ke liye

def is_youtube(url):
    return bool(re.search(r"(youtube\.com|youtu\.be)", url, re.I))

def get_meta(url):
    """title, duration, thumb url, formats (sirf <=1080p video + audio)"""
    td = tempfile.mkdtemp()
    try:
        cmd = ["yt-dlp", "-J", "--no-playlist", "--no-warnings", "--remote-components", "ejs:github"] + cookie_args(td) + [url]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180, env=BASE_ENV)
        if r.returncode != 0:
            return None
        info = json.loads(r.stdout)
        title = info.get("title", "video")
        dur = int(info.get("duration") or 0)
        thumb = info.get("thumbnail")
        vids, auds = {}, []
        for f in info.get("formats", []):
            h = f.get("height")
            if f.get("vcodec") not in (None, "none") and h and h <= 1080:
                size = f.get("filesize") or f.get("filesize_approx") or 0
                cur = vids.get(h)
                # avc1 (h264) prefer, warna sabse bada size wala
                better = cur is None or (f.get("vcodec","").startswith("avc1") and not cur[1].startswith("avc1")) or (size > cur[0] and f.get("vcodec","").startswith("avc1") == cur[1].startswith("avc1"))
                if better:
                    vids[h] = (size, f.get("vcodec",""), f.get("format_id"))
            elif f.get("vcodec") in (None, "none") and f.get("acodec") not in (None, "none"):
                auds.append((f.get("abr") or 0, f.get("format_id")))
        return {"title": title, "dur": dur, "thumb": thumb, "vids": vids, "auds": auds}
    except Exception as e:
        log.warning("meta fail: %s", e)
        return None
    finally:
        shutil.rmtree(td, ignore_errors=True)

def human(n):
    if not n:
        return "?"
    return f"{n/1048576:.1f} MB" if n >= 1048576 else f"{n/1024:.0f} KB"

# ---------- yt-dlp ----------
def cookie_args(td):
    env = os.environ.get("YOUTUBE_COOKIES", "").strip()
    if not env:
        return []
    cf = os.path.join(td, "cookies.txt")
    open(cf, "w").write(env.replace("\\n", "\n"))
    return ["--cookies", cf]

BASE_ENV = dict(os.environ)
BASE_ENV.setdefault("DENO_DIR", "/tmp/deno")
BASE_ENV.setdefault("XDG_CACHE_HOME", "/tmp/xdg")

def run_ytdlp(url, td, fmt, mp3=False, subs=False, extra=None):
    out = os.path.join(td, "file.%(ext)s")
    cmd = ["yt-dlp"] + cookie_args(td) + ["-o", out, "--remote-components", "ejs:github",
           "-N", "16", "--retries", "5", "--fragment-retries", "5",
           "--file-access-retries", "5", "--concurrent-fragments", "8",
           "--no-warnings", "--socket-timeout", "20"]
    if subs:
        cmd += ["--write-subs", "--write-auto-subs", "--sub-langs", "en,hi",
                "--convert-subs", "srt", "--skip-download"]
        r = subprocess.run(cmd + [url], capture_output=True, timeout=300, env=BASE_ENV)
        subs_files = [os.path.join(td, f) for f in os.listdir(td) if f.endswith(".srt") or f.endswith(".vtt")]
        return None, subs_files
    if extra:  # playlist etc
        cmd += extra
    if mp3:
        cmd += ["-x", "--audio-format", "mp3", "--audio-quality", "0"]
    else:
        if fmt != "default":
            cmd += ["-f", fmt]
        cmd += ["--merge-output-format", "mp4"]
    r = subprocess.run(cmd + [url], capture_output=True, timeout=900, env=BASE_ENV)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or b"").decode(errors="ignore").strip()[-300:])
    files = [f for f in os.listdir(td) if f.startswith("file.")]
    if not files:
        raise RuntimeError("no output file")
    return os.path.join(td, sorted(files)[0]), []

def probe(path):
    try:
        pr = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                             "-show_entries", "stream=width,height:format=duration",
                             "-of", "json", path], capture_output=True, text=True, timeout=30)
        info = json.loads(pr.stdout or "{}")
        dur = int(float(info.get("format", {}).get("duration", 0))) or None
        st = (info.get("streams") or [{}])[0]
        return dur, st.get("width"), st.get("height")
    except Exception:
        return None, None, None

def make_thumb(path, td):
    try:
        thumb = os.path.join(td, "thumb.jpg")
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "1", "-i", path,
                        "-vframes", "1", "-vf", "scale=320:-2", thumb],
                       capture_output=True, timeout=60)
        return thumb if os.path.exists(thumb) else None
    except Exception:
        return None

def split_video(path, td):
    """49MB+ video ko time-based hisso me todo (ffmpeg segment, ~40MB/hissa)"""
    parts = []
    try:
        dur, _, _ = probe(path)
        size = os.path.getsize(path)
        if not dur or size < 40 * 1024 * 1024:
            return parts
        seg_time = max(30, int(dur * 40 * 1024 * 1024 / size))
        r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", path, "-c", "copy",
                            "-f", "segment", "-segment_time", str(seg_time),
                            "-reset_timestamps", "1", os.path.join(td, "part%02d.mp4")],
                           capture_output=True, timeout=900)
        if r.returncode == 0:
            parts = sorted(os.path.join(td, f) for f in os.listdir(td) if f.startswith("part"))
    except Exception:
        pass
    return parts

def send_video(update, ctx, path, td, caption=None):
    dur, wid, hei = probe(path)
    thumb = make_thumb(path, td)
    with open(path, "rb") as f:
        return update.effective_message.reply_video(f, caption=caption, thumbnail=thumb, duration=dur,
                                          width=wid, height=hei, supports_streaming=True,
                                          read_timeout=600, write_timeout=600, connect_timeout=300)

async def safe_edit(msg, text):
    try:
        await msg.edit_text(text)
    except Exception:
        pass

async def cleanup(status, update):
    try:
        await status.delete()
    except Exception:
        pass
    try:
        await update.message.delete()
    except Exception:
        pass

# ---------- main download pipeline ----------
async def download_job(update, ctx, url, mp3=False, subs=False, trim=None, force_fmt=None):
    uid = update.effective_user.id
    msg = update.effective_message      # button se aaye to bhi chalega
    chat_id = update.effective_chat.id
    wait = rate_ok(uid)
    if wait:
        await msg.reply_text(f"bhai thoda ruk, {wait}s me dobara bhej")
        return
    log.info("JOB start uid=%s url=%s mp3=%s subs=%s fmt=%s", uid, url[:60], mp3, subs, force_fmt)
    status = await msg.reply_text(
        "subtitles nikal raha hoon..." if subs else
        "audio nikal raha hoon..." if mp3 else "download start...")
    await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)
    loop = asyncio.get_running_loop()

    try:  # subtitles path
        if subs:
            _, sub_files = await loop.run_in_executor(ctx.bot_data["pool"],
                lambda: run_ytdlp(url, tempfile.mkdtemp(), None, subs=True))
            if sub_files:
                for sf in sub_files[:4]:
                    with open(sf, "rb") as fh:
                        await msg.reply_document(fh)
                await cleanup(status, update)
            else:
                await safe_edit(status, "is video ki koi subtitle nahi mili")
            return

        # quality: user choice -> ladder
        if force_fmt:
            chain = [force_fmt]
        else:
            q = user_quality.get(uid)
            if q:
                chain = [f"bv*[height<={q}]+ba/b[height<={q}]", "b", "default"]
            else:
                chain = ["bv*[height<=1080]+ba/b[height<=1080]",
                         "bv*[height<=720]+ba/b[height<=720]",
                         "bv*[height<=480]+ba/b[height<=480]",
                         "bv*[height<=360]+ba/b[height<=360]", "b", "default"]

        last_err = "unknown"
        for fmt in chain:
            td = tempfile.mkdtemp()
            try:
                log.info("JOB downloading uid=%s fmt=%s", uid, fmt[:40])
                path, _ = await loop.run_in_executor(ctx.bot_data["pool"],
                    lambda f=fmt: run_ytdlp(url, td, f, mp3, extra=trim))
                size = os.path.getsize(path)
                log.info("JOB downloaded uid=%s size=%.1fMB", uid, size/1048576)

                if size > MAX_BYTES:
                    await safe_edit(status, "video badi hai, hisso me tod raha hoon (quality same rahegi)...")
                    parts = await loop.run_in_executor(ctx.bot_data["pool"], lambda: split_video(path, td))
                    ok_parts = [p for p in parts if os.path.getsize(p) <= MAX_BYTES]
                    if ok_parts:
                        log.info("JOB split uid=%s parts=%s", uid, len(ok_parts))
                        for i, p in enumerate(ok_parts, 1):
                            await send_video(update, ctx, p, td, caption=f"part {i}/{len(ok_parts)}")
                        bump_stats(uid, "video", size)
                        await cleanup(status, update)
                        return
                    last_err = "split fail, chhoti quality try kar raha hoon"
                    await safe_edit(status, last_err)
                    continue

                await safe_edit(status, "upload kar raha hoon...")
                log.info("JOB uploading uid=%s size=%.1fMB", uid, size/1048576)
                await ctx.bot.send_chat_action(chat_id, ChatAction.UPLOAD_VIDEO)
                if mp3:
                    with open(path, "rb") as f:
                        await msg.reply_audio(f, thumbnail=make_thumb(path, td),
                                                         read_timeout=600, write_timeout=600)
                else:
                    await send_video(update, ctx, path, td)
                bump_stats(uid, "audio" if mp3 else "video", size)
                log.info("JOB DONE uid=%s", uid)
                await cleanup(status, update)
                return
            except subprocess.TimeoutExpired:
                last_err = "time out, video bahut badi/lambi hai"
                break
            except Exception as e:
                last_err = str(e).splitlines()[-1][:180] if str(e) else "unknown"
                log.error("try %s fail: %s", fmt, last_err)
            finally:
                shutil.rmtree(td, ignore_errors=True)
        log.error("JOB FAILED uid=%s err=%s", uid, last_err)
        await safe_edit(status, "download fail: " + last_err)
    finally:
        pass

# ---------- commands ----------
async def start(update: Update, ctx):
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🎵 mp3", callback_data="mp3help"),
        InlineKeyboardButton("⚙️ quality", callback_data="qmenu"),
        InlineKeyboardButton("📊 stats", callback_data="stathelp")]])
    await update.message.reply_text(YO, reply_markup=kb)

async def handle_audio(update: Update, ctx):
    url = first_url(update.message.text)
    if not url:
        await update.message.reply_text("link bhejo jaise: /audio <url>")
        return
    await download_job(update, ctx, url, mp3=True)

async def handle_sub(update: Update, ctx):
    url = first_url(update.message.text)
    if not url:
        await update.message.reply_text("link bhejo jaise: /sub <url>")
        return
    await download_job(update, ctx, url, subs=True)

async def handle_trim(update: Update, ctx):
    parts = update.message.text.split()
    url = first_url(update.message.text)
    if len(parts) < 3 or not url:
        await update.message.reply_text("aise bhej: /trim <url> 0:00 1:00")
        return
    try:
        def secs(s):
            if ":" in s:
                p = [int(x) for x in s.split(":")]
                return sum(v * 60 ** i for i, v in enumerate(reversed(p)))
            return int(s)
        ss, to = secs(parts[-2]), secs(parts[-1])
        if ss >= to or to - ss > 600:
            raise ValueError
        await download_job(update, ctx, url, trim=["--download-sections", f"*{ss}-{to}"])
    except Exception:
        await update.message.reply_text("time samajh nahi aaya. jaise: /trim <url> 0:00 1:00 (max 10 min)")

async def handle_playlist(update: Update, ctx):
    url = first_url(update.message.text)
    if not url:
        await update.message.reply_text("playlist ka link bhej: /playlist <url>")
        return
    status = await update.message.reply_text("playlist padh raha hoon...")
    td = tempfile.mkdtemp()
    try:
        cmd = ["yt-dlp", "--flat-playlist", "--print", "url", "--no-warnings"] + cookie_args(td) + [url]
        r = await asyncio.get_running_loop().run_in_executor(ctx.bot_data["pool"],
            lambda: subprocess.run(cmd, capture_output=True, text=True, timeout=180, env=BASE_ENV))
        links = [l.strip() for l in (r.stdout or "").splitlines() if l.strip()][:MAX_PLAYLIST]
        if not links:
            await safe_edit(status, "playlist nahi mili ya khali hai")
            return
        await safe_edit(status, f"{len(links)} videos mile, ek ek karke bhej raha hoon...")
        for i, l in enumerate(links, 1):
            try:
                await download_job(update, ctx, l, mp3=False)
            except Exception as e:
                log.error("playlist item %s fail: %s", i, e)
        await safe_edit(status, "playlist khatam ✅")
    finally:
        shutil.rmtree(td, ignore_errors=True)

async def handle_stats(update: Update, ctx):
    s = load_stats().get(str(update.effective_user.id), {"videos": 0, "audios": 0, "mb": 0})
    await update.message.reply_text(
        f"tera score:\n🎥 videos: {s['videos']}\n🎵 audios: {s['audios']}\n📦 total: {s['mb']} MB")

async def qmenu_cb(update: Update, ctx):
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(h, callback_data=f"setq_{h[:-1]}")
        for h in ("1080p", "720p", "480p")],
        [InlineKeyboardButton("360p", callback_data="setq_360"),
         InlineKeyboardButton("auto", callback_data="setq_auto")]])
    await update.message.reply_text("quality chun (auto = bot khud best dega):", reply_markup=kb)

async def on_button(update: Update, ctx):
    q = update.callback_query
    await q.answer()
    d = q.data
    if d == "qmenu":
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(h, callback_data=f"setq_{h[:-1]}")
            for h in ("1080p", "720p", "480p")],
            [InlineKeyboardButton("360p", callback_data="setq_360"),
             InlineKeyboardButton("auto", callback_data="setq_auto")]])
        await q.message.reply_text("quality chun:", reply_markup=kb)
    elif d == "mp3help":
        await q.message.reply_text("mp3 ke liye: /audio <link>")
    elif d == "stathelp":
        await q.message.reply_text("stats dekhne ko: /stats")
    elif d.startswith("ck_"):
        p = pending.get(q.from_user.id, {})
        opts = p.get("opts", [])
        url = p.get("url")
        try:
            label, fmt = opts[int(d.split("_")[1])]
        except Exception:
            await q.message.reply_text("ye menu purana ho gaya, link dobara bhej")
            return
        if not url:
            await q.message.reply_text("link dobara bhej bhai")
            return
        if fmt == "__AUTO__":
            user_quality[q.from_user.id] = None
            await q.message.reply_text("download start (auto best)...")
            await download_job(update, ctx, url, mp3=False)
            return
        if fmt == "__MP3__":
            await q.message.reply_text("audio nikal raha hoon...")
            await download_job(update, ctx, url, mp3=True)
            return
        user_quality[q.from_user.id] = None  # ek baar ki choice, default auto hi rahe
        await q.message.reply_text(f"download start ({label})...")
        await download_job(update, ctx, url, mp3=False, force_fmt=fmt)
    elif d.startswith("setq_"):
        v = d.split("_")[1]
        user_quality[q.from_user.id] = None if v == "auto" else int(v)
        await q.message.reply_text("quality set: " + ("auto" if v == "auto" else v + "p"))

async def handle_video(update: Update, ctx):
    if update.message.text.startswith("/"):
        return
    url = first_url(update.message.text)
    if not url:
        await update.message.reply_text("ye link nahi lag raa, pura url bhejo")
        return
    if is_youtube(url):
        await youtube_menu(update, ctx, url)
        return
    await download_job(update, ctx, url, mp3=False)

async def youtube_menu(update, ctx, url):
    status = await update.message.reply_text("video padh raha hoon...")
    meta = await asyncio.get_running_loop().run_in_executor(ctx.bot_data["pool"],
        lambda: get_meta(url))
    if not meta:
        await safe_edit(status, "video ki detail nahi mili, direct best quality bhej raha hoon...")
        await download_job(update, ctx, url, mp3=False)
        return
    heights = sorted(meta["vids"].keys(), reverse=True)
    opts = []
    for h in heights:
        size, vc, fid = meta["vids"][h]
        label = f"🎥 {h}p [{human(size)}]" + (" ⚡" if h <= 720 else "")
        opts.append((label, f"bv*[height<={h}]+ba/b[height<={h}]"))
    opts.append(("🎵 audio (mp3)", "__MP3__"))
    opts.append(("⚡ auto best", "__AUTO__"))
    pending[update.effective_user.id] = {"url": url, "opts": opts}

    kb_rows, row = [], []
    for label, fmt in opts:
        idx = len(opts) - opts.index((label, fmt)) - 1  # not used, idx via enum below
    rows, flat = [], []
    for label, fmt in opts:
        flat.append((label, fmt))
    opts = flat
    for i, (label, fmt) in enumerate(opts):
        row.append(InlineKeyboardButton(label, callback_data=f"ck_{i}"))
        if len(row) == 2:
            kb_rows.append(row); row = []
    if row:
        kb_rows.append(row)
    dur = meta["dur"]
    cap = f"<b>{meta['title'][:200]}</b>"
    if dur:
        cap += f"\n⏱ {dur//60}:{dur%60:02d}"
    try:
        await status.delete()
    except Exception:
        pass
    try:
        if meta["thumb"]:
            await update.message.reply_photo(meta["thumb"], caption=cap, parse_mode="HTML",
                                             reply_markup=InlineKeyboardMarkup(kb_rows))
            return
    except Exception:
        pass
    await update.message.reply_text(cap, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb_rows))

def first_url(text):
    m = URL_RE.search(text or "")
    return m.group(0) if m else None

def ytdlp_version_check():
    subprocess.run(["pip", "install", "-q", "-U", "yt-dlp"], capture_output=True, timeout=120)
    if not shutil.which("deno"):
        for dest in ("/opt/render/project/src/.deno/bin", os.path.expanduser("~/.deno/bin"), "/tmp/deno-bin"):
            try:
                os.makedirs(dest, exist_ok=True)
                dl = "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip"
                zp = os.path.join(dest, "deno.zip")
                r = subprocess.run(["curl", "-fsSL", dl, "-o", zp], capture_output=True, timeout=300)
                if r.returncode != 0:
                    continue
                subprocess.run(["unzip", "-o", zp, "-d", dest], capture_output=True, timeout=60)
                os.chmod(os.path.join(dest, "deno"), 0o755)
                os.environ["PATH"] = dest + ":" + os.environ.get("PATH", "")
                v = subprocess.run([os.path.join(dest, "deno"), "--version"], capture_output=True, text=True, timeout=10)
                if v.returncode == 0:
                    log.info("deno ready: %s", v.stdout.strip().splitlines()[0])
                    break
            except Exception as e:
                log.warning("deno install fail at %s: %s", dest, e)
    v = subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True)
    log.info("yt-dlp %s", v.stdout.strip())

# --- render keepalive ---
def _keepalive():
    import http.server as _hs, socketserver as _ss
    port = int(os.environ.get("PORT", 8080))
    class H(_hs.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b"alive")
        def log_message(self, *a): pass
    _ss.TCPServer.allow_reuse_address = True
    try:
        _ss.TCPServer(("0.0.0.0", port), H).serve_forever()
    except Exception as e:
        log.warning(f"keepalive off: {e}")
threading.Thread(target=_keepalive, daemon=True).start()

async def debug_yt(update: Update, ctx):
    url = first_url(update.message.text)
    if not url:
        await update.message.reply_text("/debug <youtube link>")
        return
    await update.message.reply_text("render ke andar se check kar raha hoon...")
    td = tempfile.mkdtemp()
    try:
        cmd = ["yt-dlp", "-F", "--no-playlist", "--verbose", "--remote-components", "ejs:github"] + cookie_args(td) + [url]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=BASE_ENV)
        out = (r.stdout + "\n" + r.stderr)[-3500:]
        deno = subprocess.run(["bash", "-lc", "which deno && deno --version"], capture_output=True, text=True)
        await update.message.reply_text("DENO: " + (deno.stdout or deno.stderr)[-200:] + "\n\n" + out)
    finally:
        shutil.rmtree(td, ignore_errors=True)

def main():
    ytdlp_version_check()
    app = Application.builder().token(TOKEN).concurrent_updates(True).build()
    app.bot_data["pool"] = ThreadPoolExecutor(max_workers=4)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("audio", handle_audio))
    app.add_handler(CommandHandler("sub", handle_sub))
    app.add_handler(CommandHandler("trim", handle_trim))
    app.add_handler(CommandHandler("playlist", handle_playlist))
    app.add_handler(CommandHandler("stats", handle_stats))
    app.add_handler(CommandHandler("q", qmenu_cb))
    app.add_handler(CommandHandler("debug", debug_yt))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT, handle_video))

    async def _set_cmds(app_):
        from telegram import BotCommand
        await app_.bot.set_my_commands([
            BotCommand("start", "bot chalu / menu"),
            BotCommand("q", "quality chuno"),
            BotCommand("audio", "mp3 nikalo"),
            BotCommand("sub", "subtitles ke sath"),
            BotCommand("trim", "hissa download karo"),
            BotCommand("playlist", "poori playlist"),
            BotCommand("stats", "tera score"),
        ])
    app.post_init = _set_cmds
    log.info("bot live")
    app.run_polling()

if __name__ == "__main__":
    main()
