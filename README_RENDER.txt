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
  re-encoding. Other video codecs are converted to H.264/AAC MP4 with fast-start metadata and
  a generated thumbnail. A conversion progress indicator is shown where FFmpeg supports it.
- YouTube format selection prefers AVC/H.264 video plus AAC audio to avoid slow 1080p transcoding.
  If a selected format fails to download or prepare, the bot may step down to a lower quality.
- Instagram download tries the supplied URL and one canonical tracking-free URL, anonymously,
  with bounded timeouts. Instagram cookies are neither required nor read by this version. This is
  best-effort only: Instagram can login-gate, rate-limit, or block anonymous server requests, so
  no code can guarantee every reel without valid public access. Private/login-only content fails
  with a clear error rather than being reported as downloaded.
- Instagram galleries are capped by MAX_PLAYLIST and sent as Telegram albums when supported.
- MP3 files include embedded metadata when available and are sent as Telegram audio.
- The bot avoids sending WebM video as a generic document when it can convert it to MP4.

DEPLOY
1. Upload bot.py, requirements.txt, Dockerfile, render.yaml, and this README to the GitHub
   repository root. Replace the old bot.py with this version.
2. In Render, update the existing service or deploy the repository's Blueprint.
3. Set BOT_TOKEN in Render Environment settings. Never commit the token to GitHub.
4. Rebuild/deploy and inspect logs. Test with a short public YouTube clip first, then a public
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
- FFMPEG_TIMEOUT_SECONDS=240
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
- Full conversion of VP9/AV1 to H.264 can be CPU-intensive. Compatible H.264/AAC content is now
  remuxed to avoid unnecessary re-encoding; non-compatible content has a bounded FFmpeg timeout.
- The local checks in this package do not replace a live test on your own Telegram chat and Render
  service. Third-party sites may change after deployment.

v6 note:
- Instagram/yt-dlp final output paths are collected from after_move:filepath and
  a recursive media-file fallback, fixing cases where an Instagram gallery reports
  playlist completion but top-level filename matching misses its media files.
- curl-cffi is installed for yt-dlp's optional browser impersonation support.
- Anonymous Instagram access remains best-effort; private/login-gated posts may fail.
