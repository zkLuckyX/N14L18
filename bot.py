"""
Pro Video Downloader Bot  (Telegram, yt-dlp based)
Supports: instagram, youtube, tiktok, twitter/x, facebook, snapchat,
pinterest, reddit, twitch vods, dailymotion, vimeo, and 1000+ more sites.

Run:  BOT_TOKEN=xxx python3 bot.py
Deps: pip install -r requirements.txt
"""
import os, re, time, shutil, tempfile, subprocess, logging, asyncio
from concurrent.futures import ThreadPoolExecutor

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dlbot")

TOKEN = os.environ["BOT_TOKEN"]
MAX_BYTES = 49 * 1024 * 1024          # telegram bot api upload cap
YO = "yo! kisi bhi platform ka video/reel link bhejo (instagram, youtube, tiktok, twitter/x, facebook, pinterest, reddit...), best quality me dunga. /audio se sirf sound bhi nikal sakta hoon"

URL_RE = re.compile(r"https?://\S+", re.I)

# format fallback chain: try best quality combos, then any best single file
FORMAT_CHAIN = ["default", "bv*+ba/b", "best", "worst"]

def first_url(text):
    m = URL_RE.search(text or "")
    return m.group(0) if m else None

async def start(update: Update, ctx):
    await update.message.reply_text(YO)

def run_ytdlp(url, td, fmt, mp3=False):
    out = os.path.join(td, "file.%(ext)s")
    cookie_env = os.environ.get("YOUTUBE_COOKIES", "").strip()
    cookie_args = []
    if cookie_env:
        cf = os.path.join(td, "cookies.txt")
        with open(cf, "w") as fh:
            fh.write(cookie_env.replace("\\n", "\n"))
        cookie_args = ["--cookies", cf]
    cmd = ["yt-dlp"] + cookie_args + ["-o", out, "--no-playlist", "-N", "16",
           "--retries", "5", "--fragment-retries", "5",
           "--file-access-retries", "5", "--concurrent-fragments", "8",
           "--no-warnings", "--socket-timeout", "20",
           "--extractor-args", "youtube:player_client=android,android_vr,web_safari", url]
    if mp3:
        cmd += ["-x", "--audio-format", "mp3", "--audio-quality", "0"]
    else:
        if fmt != "default":
            cmd += ["-f", fmt]
        cmd += ["--merge-output-format", "mp4", "--max-filesize", "49M"]
    env = dict(os.environ)
    env.setdefault("DENO_DIR", "/tmp/deno")
    env.setdefault("XDG_CACHE_HOME", "/tmp/xdg")
    r = subprocess.run(cmd, capture_output=True, timeout=900, env=env)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or b"").decode(errors="ignore").strip())
    files = os.listdir(td)
    if not files:
        raise RuntimeError("no output file")
    return os.path.join(td, files[0])

def ytdlp_version_check():
    # khud ko latest rakho: purane version hi aadhe errors ki jad hai
    subprocess.run(["pip", "install", "-q", "-U", "yt-dlp"], capture_output=True, timeout=120)
    if not shutil.which("deno"):
        import stat as _stat
        for dest in ("/opt/render/project/src/.deno/bin", os.path.expanduser("~/.deno/bin"), "/tmp/deno-bin"):
            try:
                os.makedirs(dest, exist_ok=True)
                dl = "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip"
                zp = os.path.join(dest, "deno.zip")
                r = subprocess.run(["curl", "-fsSL", dl, "-o", zp], capture_output=True, timeout=300)
                if r.returncode != 0:
                    log.warning("deno download fail: %s", (r.stderr or b"").decode(errors="ignore")[-150:])
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

async def download_job(update, ctx, url, mp3=False):
    chat_id = update.effective_chat.id
    status = await update.message.reply_text("download start..." if not mp3 else "audio nikal raha hoon...")
    await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)
    loop = asyncio.get_running_loop()
    last_err = "unknown"
    for fmt in FORMAT_CHAIN:
        td = tempfile.mkdtemp()
        try:
            path = await loop.run_in_executor(ctx.bot_data["pool"],
                lambda f=fmt: run_ytdlp(url, td, f, mp3))
            size = os.path.getsize(path)
            if size > MAX_BYTES:
                last_err = "50MB+ video: chhoti quality try kar raha hoon..."
                try:
                    await status.edit_text(last_err)
                except Exception:
                    pass
                continue
            await status.edit_text("upload kar raha hoon...")
            await ctx.bot.send_chat_action(chat_id, ChatAction.UPLOAD_VIDEO if not mp3 else ChatAction.UPLOAD_VOICE)
            with open(path, "rb") as f:
                if mp3:
                    await update.message.reply_audio(f, read_timeout=300, write_timeout=300, connect_timeout=300)
                else:
                    await update.message.reply_video(f, caption=None, read_timeout=300, write_timeout=300, connect_timeout=300)
            await status.delete()
            try:
                await update.message.delete()
            except Exception:
                pass
            return
        except subprocess.TimeoutExpired:
            last_err = "time out, video bahut badi hai"
            break
        except Exception as e:
            last_err = str(e).splitlines()[-1][:180] if str(e) else "unknown"
            log.error("try %s fail: %s", fmt, last_err)
        finally:
            shutil.rmtree(td, ignore_errors=True)
    await status.edit_text("download fail: " + last_err)

async def handle_video(update: Update, ctx):
    if update.message.text.startswith("/"):
        return
    url = first_url(update.message.text)
    if not url:
        await update.message.reply_text("ye link nahi lag raa, pura url bhejo")
        return
    await download_job(update, ctx, url, mp3=False)

async def handle_audio(update: Update, ctx):
    url = first_url(update.message.text)
    if not url:
        await update.message.reply_text("link bhejo jaise: /audio <url>")
        return
    await download_job(update, ctx, url, mp3=True)



# --- render keepalive: chhota http server taaki free tier sleep na ho ---
import threading as _th, http.server as _hs, socketserver as _ss, os as _os
def _keepalive():
    port = int(_os.environ.get("PORT", 8080))
    class H(_hs.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b"alive")
        def log_message(self, *a): pass
    _ss.TCPServer.allow_reuse_address = True
    try:
        _ss.TCPServer(("0.0.0.0", port), H).serve_forever()
    except Exception as e:
        log.warning(f"keepalive off: {e}")
_th.Thread(target=_keepalive, daemon=True).start()

async def debug_yt(update: Update, ctx):
    url = first_url(update.message.text)
    if not url:
        await update.message.reply_text("/debug <youtube link>")
        return
    await update.message.reply_text("render ke andar se check kar raha hoon...")
    td = tempfile.mkdtemp()
    try:
        cookie_env = os.environ.get("YOUTUBE_COOKIES", "").strip()
        cf = ""
        if cookie_env:
            cf = os.path.join(td, "c.txt")
            open(cf, "w").write(cookie_env.replace("\\n", "\n"))
        cmd = ["yt-dlp", "-F", "--no-playlist", "--verbose"]
        if cf:
            cmd += ["--cookies", cf]
        cmd.append(url)
        env = dict(os.environ); env["DENO_DIR"] = "/tmp/deno"; env["XDG_CACHE_HOME"] = "/tmp/xdg"
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env)
        out = (r.stdout + "\n" + r.stderr)[-3500:]
        deno = subprocess.run(["bash", "-lc", "which deno && deno --version"], capture_output=True, text=True)
        await update.message.reply_text("DENO: " + (deno.stdout or deno.stderr)[-200:] + "\n\n" + out)
    finally:
        shutil.rmtree(td, ignore_errors=True)

def main():
    ytdlp_version_check()
    app = Application.builder().token(TOKEN).concurrent_updates(True).build()
    app.bot_data["pool"] = ThreadPoolExecutor(max_workers=2)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("audio", handle_audio))
    app.add_handler(CommandHandler("debug", debug_yt))
    app.add_handler(MessageHandler(filters.TEXT, handle_video))
    log.info("bot live")
    app.run_polling()

if __name__ == "__main__":
    main()
