Pro Video Downloader Bot — Render Free Web Service

PACKAGE FILES
- bot.py: Telegram bot, public URL downloads through yt-dlp, quality selection,
  MP3 metadata, subtitles, capped playlists/Instagram carousels, progress bar,
  recursive file splitting, stats/history, and automatic text cleanup.
- requirements.txt: Python dependencies.
- Dockerfile: Python, FFmpeg, and Deno runtime installation.
- render.yaml: Render Blueprint with /healthz health checks and 10-minute cleanup setting.

FEATURES ADDED
- After 10 minutes, bot-generated text replies, status/progress messages, quality menus,
  help messages, and source URL/command text are scheduled for deletion. Uploaded video,
  audio, photo, and document media are never put on the deletion timer.
- Text cleanup is scheduled once per message (duplicate handlers do not create duplicate timers).
  It requires Telegram to allow deletion; in group chats this depends on the bot's permissions.
- MP3s are converted with embedded metadata. The bot reads MP3 title/artist tags and passes
  them to Telegram's audio fields; uploader/channel is used as a metadata fallback when needed.
- Instagram carousel posts are capped by MAX_PLAYLIST and eligible photo/video items are
  delivered as Telegram media albums of up to 10 items, preserving extractor order. If Telegram
  rejects an album, the bot attempts to send the items individually. Some Instagram posts require
  a valid logged-in session; configure INSTAGRAM_COOKIES only for media you are allowed to save.
- YouTube quality menus include a thumbnail preview when the thumbnail CDN is reachable.
  Downloaded video is normalized to MP4 with H.264/AAC and fast-start metadata, and the bot creates
  a small JPEG preview and sends it as a Telegram video (supports_streaming=true). WebM files are
  not silently sent as generic documents; if conversion fails, the bot reports an error instead.
- Instagram authentication failures now return a clear message instead of implying a successful
  download. Add INSTAGRAM_COOKIES in Render as a secret containing a Netscape-format cookie export
  if a post requires login. Cookie data is sensitive and can grant access to your account; never
  commit it to GitHub or share it, and revoke/replace the session if it is exposed.
- Download progress shows a visual bar, percentage, transfer speed, and downloaded/total MiB
  when available.
- /stats reports bundles, videos, audio files, images, source files, Telegram parts, and total
  processed MiB. /history retains the five most recent source URLs; /again NUMBER reruns one.

DEPLOY
1. Extract the ZIP and upload bot.py, requirements.txt, Dockerfile, render.yaml, and this README
   to the root of a GitHub repository.
2. In Render, choose New -> Blueprint and connect the repository, or create a Web Service using
   the Dockerfile. Use the Free plan only if its sleep/storage limitations are acceptable.
3. Add BOT_TOKEN in Render Environment settings. Never commit the token to GitHub.
4. Deploy and check build/runtime logs. Test /start, then a short public video and /audio.
5. Optional: set ADMIN_USER_IDS to comma-separated Telegram user IDs to enable /debug.
6. Optional: set YOUTUBE_COOKIES or INSTAGRAM_COOKIES to Netscape-format cookie-file contents
   only when a site requires login for media you own or have permission to save. Treat cookies as
   highly sensitive secrets; never commit them to GitHub. Instagram cookies are not required for
   every post and cannot make a private post available unless the account has access to it.

USEFUL ENVIRONMENT SETTINGS
- AUTO_DELETE_SECONDS=600 (10 minutes; minimum accepted value is 60 seconds)
- MAX_UPLOAD_MIB=45 (keeps upload parts below the configured upload cap)
- MAX_SOURCE_MIB=1024 (per-file safety cap; protects temporary storage)
- MAX_PLAYLIST=10 (playlist/gallery item cap, hard maximum 20)
- USER_COOLDOWN_SECONDS=15
- DOWNLOAD_TIMEOUT_SECONDS=1200
- FFMPEG_TIMEOUT_SECONDS=600

IMPORTANT LIMITATIONS
- No downloader can guarantee that every site/video will always work. Extractors change, and
  private, deleted, region-restricted, DRM-protected, login-required, or site-blocked items may fail.
- Large videos and MP3s are split recursively. Every final part is size-checked before upload; if
  splitting cannot safely finish, the bot reports failure rather than silently dropping parts.
  Video re-encoding fallback can be slow and may exceed Free-tier CPU limits.
- Telegram's normal Bot API upload limit is 50 MB per uploaded media file; this package uses a
  45 MiB margin. Split success still depends on media duration, codecs, FFmpeg, and available CPU.
- Render Free web services can spin down after inactivity, may restart, and have usage limits.
  A health endpoint and process retry loop cannot guarantee 24/7 uptime or restart a service
  disabled/suspended by the hosting platform.
- Render Free uses ephemeral storage. Stats/history JSON files under /tmp may disappear after
  restart, redeploy, or spin-down. Use persistent external storage if this data must survive.
- The auto-delete timer is in-process; if the service restarts before a timer fires, that timer
  will not survive the restart. A message can remain after an interrupted restart until deleted.
- Download only content you own or have permission to save.

VALIDATION PERFORMED FOR THIS PACKAGE REVISION
- Python syntax compilation passed.
- Local FFmpeg/FFprobe test generated a VP9/Opus WebM, converted it to H.264/AAC MP4, verified
  the output codecs, generated a JPEG thumbnail, and confirmed prepared videos use MP4 plus preview.
- Previous helper-level tests covered MP3 title/artist metadata, stats compatibility, Instagram URL
  classification, and message-delete scheduling. This code update was not tested against live
  Telegram APIs, a live YouTube download, an authenticated Instagram account, or Render deployment.
