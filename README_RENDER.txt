NabaLucky Media Bot — Render deployment bundle

FILES
- bot.py: Telegram media bot with YouTube quality menu, thumbnails, MP3 metadata,
  public Instagram best-effort downloads, carousel albums, progress updates, 10-minute
  cleanup for source/text/status messages, stats/history, and splitting for large media.
- requirements.txt: Python Telegram bot dependency plus current yt-dlp release line.
- Dockerfile: Python 3.12, FFmpeg, Deno, and Python dependency installation.
- render.yaml: Render Blueprint and safe upload/source defaults.

IMPORTANT BEHAVIOUR
- User source link/command text is scheduled for deletion after 10 minutes.
- Download status/progress stays visible while work is running and is scheduled for deletion
  10 minutes AFTER the job completes. This avoids deleting the only status while a download
  or upload is still in progress.
- Uploaded videos, audio, photos, albums, and documents are not auto-deleted by this timer.
- Video delivery prefers existing H.264/AAC MP4 streams and remuxes compatible streams without
  re-encoding, setting fast-start metadata for streaming. Other codecs get one bounded, ultrafast
  conversion attempt capped at 720p to avoid long Render Free CPU jobs. If that conversion times
  out, the source is still delivered: MP4 as a video, other formats as a document, with an honest
  original-format caption rather than silently losing the download. A progress indicator is shown.
- YouTube format selection prefers AVC/H.264 video plus AAC audio to avoid slow 1080p transcoding.
  If a selected format fails to download or prepare, the bot may step down to a lower quality.
- Instagram download tries the supplied URL and one canonical tracking-free URL without login
  cookies. It uses yt-dlp's curl-cffi browser impersonation (default target: chrome) for public
  requests, with bounded timeouts. This can help when Instagram blocks bare scripted requests,
  but login-gated/private posts and server/IP rate limits still cannot be guaranteed anonymously.
- Instagram galleries are capped by MAX_PLAYLIST and sent as Telegram albums when supported.
- MP3 files include embedded metadata when available and are sent as Telegram audio.
- The bot avoids sending WebM video as a generic document when it can convert it to MP4.

DEPLOY
1. Upload bot.py, requirements.txt, Dockerfile, render.yaml, and this README to the GitHub
   repository root. Replace the old bot.py with this version.
2. Update the EXISTING Render service. Do not create a second active polling service with the same
   token. The logs supplied for the previous build show a native Python start command; render.yaml
   defines Docker. If you choose Docker, change the existing service's runtime/settings to Docker
   and let the Dockerfile install FFmpeg/Deno. If keeping native Python, ensure Build Command installs
   requirements.txt and the system already has FFmpeg/ffprobe/Deno.
3. Because the previously shared log contains the bot token in Telegram API URLs, revoke/regenerate
   BOT_TOKEN in @BotFather, then replace it in Render Environment settings before redeploying.
   Never commit the token to GitHub. Only one active polling instance should use the new token.
4. Rebuild/deploy and inspect logs. HTTP request logging is suppressed and a redaction filter is
   applied as defense in depth so Telegram tokens are not printed in exception/request URLs. Test with a short public YouTube clip first, then a public
   Instagram reel that opens without signing in.
5. Optionally use YOUTUBE_COOKIES only for YouTube content you own or have permission to access.
   This version intentionally does not load Instagram cookies.

ENVIRONMENT VARIABLES
- BOT_TOKEN: required secret
- MAX_UPLOAD_MIB=45: upload chunk safety threshold
- MAX_SOURCE_MIB=1024: source file safety ceiling (per input media)
- MAX_PLAYLIST=10: maximum gallery/playlist items (hard limit is 20)
- USER_COOLDOWN_SECONDS=15
- AUTO_DELETE_SECONDS=600
- DOWNLOAD_TIMEOUT_SECONDS=480
- FFMPEG_TIMEOUT_SECONDS=240 (split/remux operations)
- VIDEO_TRANSCODE_TIMEOUT_SECONDS=120 (full re-encode only)
- VIDEO_TRANSCODE_MAX_HEIGHT=720 (maximum height for fallback re-encoding)
- INSTAGRAM_IMPERSONATE=chrome (uses curl-cffi, no Instagram login cookies)
- ADMIN_USER_IDS: optional comma-separated Telegram user IDs for admin-only debug features
- YOUTUBE_COOKIES: optional Netscape-format cookie contents for authorized YouTube access only

KNOWN LIMITATIONS
- Instagram's public endpoints change frequently. Current yt-dlp includes recent extractor updates,
  but anonymous downloading remains conditional on Instagram allowing access from the host/IP.
  If Instagram returns an empty media response for anonymous access, the bot must report the error
  rather than promising a file it could not retrieve.
- Telegram Bot API standard media upload limits apply. This package keeps each upload part below
  the configured 45 MiB margin. Large videos/audio may be split; CPU and disk limits still apply.
- Render Free has ephemeral local storage: JSON stats/history in /tmp can be lost after restart,
  redeploy, or spin-down. Use an external database if history must persist.
- Full conversion of VP9/AV1 to H.264 can be CPU-intensive. The bot now remuxes compatible streams,
  prefers H.264 formats even if it must step down below the selected maximum height, and limits
  full conversion to a configurable 120-second/720p attempt before delivering the original as a
  fallback. This avoids repeating several slow transcodes without sending the user any file.
- The local checks in this package do not replace a live test on your own Telegram chat and Render
  service. Third-party sites may change after deployment.
