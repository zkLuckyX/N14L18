# N14L18

<p align="center">
  <strong>A self-hosted Telegram media downloader</strong><br />
  Quality selection · Sequential queues · Playlist processing · Automatic message cleanup
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white" alt="Python 3.12" />
  <img src="https://img.shields.io/badge/Container-Docker-2496ED?logo=docker&logoColor=white" alt="Docker" />
  <img src="https://img.shields.io/badge/Downloader-yt--dlp-5A9FD4" alt="yt-dlp" />
  <img src="https://img.shields.io/badge/Interface-Telegram-26A5E4?logo=telegram&logoColor=white" alt="Telegram" />
</p>

N14L18 is a Telegram bot for downloading supported public media links through an interactive chat workflow. It combines selectable video quality, audio extraction, bounded playlist processing, sequential multi-link jobs, title-aware filenames, progress updates, and temporary-message cleanup in one self-hosted service.

> **Important:** Platform compatibility depends on the source, its access restrictions, and the installed extractor version. This project does not bypass private-account restrictions, login walls, DRM, or other access controls. Only download and share media you are authorized to use.

## Highlights

| Capability | Details |
| --- | --- |
| **Interactive quality selection** | Choose an available video resolution or use automatic quality selection. Options depend on the source. |
| **MP3 audio** | Extract audio from supported links when the source and format allow it. |
| **Sequential link queue** | Send multiple URLs in one message; the bot processes them in order, with a configurable limit of 10 links by default. |
| **Playlist processing** | Process a bounded playlist sequentially. The default is 10 items; configuration is capped at 20. |
| **Title-aware output** | Use media titles in filenames and Telegram captions where supported. Split files include part numbering. |
| **Large-file handling** | Prepare compatible media and split supported oversized outputs into smaller parts when possible. |
| **Progress and previews** | Display download status, supported thumbnails/previews, and completion summaries. |
| **History and statistics** | Review recent links and per-user processing statistics. These files are temporary unless you provide durable storage. |
| **Temporary-message cleanup** | Automatically schedule eligible link, menu, status, and error messages for deletion. Media files deliberately remain in chat. |
| **Webhook-ready deployment** | Includes a Docker image setup and Render Blueprint configuration, with webhook mode intended for Render deployments. |

## Supported Sources

- **YouTube** — video quality choices, automatic quality, audio extraction, and bounded playlist processing where supported.
- **Instagram** — best-effort downloads of publicly accessible reels, videos, and carousel media. Instagram may limit or block anonymous requests, including requests from hosting-provider IP addresses.
- **Other public URLs supported by yt-dlp** — may work, but support is not guaranteed or individually verified.

Private, deleted, region-restricted, login-required, DRM-protected, or unsupported media may fail. Source platforms can change without notice.

## Quick Start

### 1. Create a Telegram bot

1. Open [@BotFather](https://t.me/BotFather) in Telegram.
2. Create a bot and copy its API token.
3. Store the token as a secret environment variable named `BOT_TOKEN`. **Never commit the token to GitHub.**

### 2. Deploy with Render

This repository includes `Dockerfile` and `render.yaml` for a Docker-based Render deployment.

1. Push `bot.py`, `Dockerfile`, `requirements.txt`, and `render.yaml` to your repository.
2. In Render, create or sync the Blueprint using `render.yaml` and confirm the service uses the Docker runtime.
3. Add `BOT_TOKEN` under the service's environment variables.
4. Keep `TELEGRAM_MODE=webhook` for the Render web-service configuration. `RENDER_EXTERNAL_URL` is used automatically; set `WEBHOOK_URL` only if your hosting setup does not supply it.
5. For more reliable recovery of pending auto-deletions after restarts or redeploys, configure `DATABASE_URL` with a PostgreSQL connection string.
6. Deploy and inspect the logs for the current bot version, webhook registration, health check, and auto-delete watchdog startup.

**Run only one active bot instance per token when using polling.** Duplicate polling instances can cause Telegram `409 Conflict` errors. If switching to webhook mode, stop any older polling service using the same token.

### 3. Run locally

Requirements: Python 3.12, FFmpeg, Deno, and the dependencies in `requirements.txt`. The Dockerfile installs the system tools for the container environment.

```bash
python -m venv .venv
```

Activate the virtual environment, then install dependencies:

```bash
pip install -r requirements.txt
```

Set the required environment variables in your shell or local secret manager. For local development, use polling mode:

```bash
# Linux / macOS example
export BOT_TOKEN="YOUR_TELEGRAM_BOT_TOKEN"
export TELEGRAM_MODE="polling"
python bot.py
```

On Windows PowerShell, environment variables can be set for the current session with `$env:BOT_TOKEN="YOUR_TELEGRAM_BOT_TOKEN"` and `$env:TELEGRAM_MODE="polling"` before running `python bot.py`.

Do not put real tokens, cookies, database passwords, or other credentials in source code or committed `.env` files.

## How to Use

1. Open your bot in Telegram and send `/start`.
2. Send a supported public media URL to see the available download workflow.
3. Select a video quality, automatic quality, or audio option when offered.
4. For multiple links, send several URLs in one message. They are processed sequentially up to the configured batch limit.
5. For a playlist, use `/playlist <URL>`.
6. Downloaded media remains in chat. Temporary bot messages are scheduled for cleanup after the configured period.

### Commands

| Command | Purpose |
| --- | --- |
| `/start` | Open the bot's welcome/help menu. |
| `/q` | Open the video quality menu. |
| `/auto` | Enable automatic quality selection. |
| `/audio <URL>` | Download audio as MP3 when supported. |
| `/sub <URL>` | Request subtitles when the source provides them. |
| `/playlist <URL>` | Process a supported playlist sequentially, within the configured limit. |
| `/stats` | Show your processing statistics. |
| `/history` | Show recent links saved for your account. |
| `/again <number>` | Retry a link from your recent history, for example `/again 1`. |
| `/disk` | Show temporary working-disk usage. |
| `/debug <URL>` | Run restricted diagnostics; requires the user ID to be listed in `ADMIN_USER_IDS`. |

Commands and results may be limited by the hosting environment, source availability, and current configuration.

## Configuration

Set configuration in your hosting dashboard or local environment. Do not hard-code secrets.

| Variable | Default | Purpose |
| --- | --- | --- |
| `BOT_TOKEN` | Required | Telegram bot token from BotFather. |
| `TELEGRAM_MODE` | `webhook` on Render; otherwise `polling` | Select the Telegram update transport. |
| `WEBHOOK_URL` | `RENDER_EXTERNAL_URL` when available | Optional HTTPS base URL for webhook deployment; do not append the webhook path. |
| `TELEGRAM_WEBHOOK_PATH` | `telegram` | HTTP route suffix used by the webhook. |
| `TELEGRAM_WEBHOOK_SECRET` | Derived from the bot token if omitted | Optional separate webhook secret; use letters, digits, `_` or `-`, up to 256 characters. |
| `DATABASE_URL` | Optional | PostgreSQL connection string for restart-resilient pending-message deletion records. Recommended for hosted deployments. |
| `AUTO_DELETE_SECONDS` | `600` | Lifetime for eligible temporary messages; default is 10 minutes. |
| `MAX_UPLOAD_MIB` | `45` | Target maximum size for each Telegram upload part. |
| `MAX_SOURCE_MIB` | `1024` | Maximum accepted source size in MiB. |
| `MAX_PLAYLIST` | `10` | Playlist item limit; hard-capped at 20. |
| `MAX_BATCH_LINKS` | `10` | Maximum URLs processed from one message; hard-capped at 20. |
| `USER_COOLDOWN_SECONDS` | `15` | Per-user cooldown between jobs. |
| `DOWNLOAD_TIMEOUT_SECONDS` | `480` | Download operation timeout in seconds. |
| `FFMPEG_TIMEOUT_SECONDS` | `240` | FFmpeg operation timeout in seconds. |
| `ADMIN_USER_IDS` | Empty | Comma-separated Telegram user IDs allowed to use restricted diagnostics. |
| `YOUTUBE_COOKIES` | Optional | Authorized YouTube cookie data in the expected Netscape format. Treat it as a secret; this build intentionally does not load Instagram cookies. |
| `AUTO_DELETE_STATE_FILE` | `/tmp/dlbot_auto_delete.json` | Local fallback for pending deletions; local storage may not survive instance replacement. |
| `STATS_FILE` / `HISTORY_FILE` | Files under `/tmp` | Local statistics and history files; they are temporary unless storage is changed. |

The service also reads `VIDEO_TRANSCODE_TIMEOUT_SECONDS`, `VIDEO_TRANSCODE_MAX_HEIGHT`, `WORK_DIR`, and `LOG_LEVEL` for advanced tuning. Review `bot.py` before changing advanced settings.

## Automatic Message Cleanup

- Temporary source-link/command messages, menus, status updates, and eligible error/help messages are scheduled for deletion.
- The default expiry is **600 seconds (10 minutes)**. Active progress edits can renew the expiry; final status messages receive a fresh expiry when the job completes.
- Downloaded videos, audio, photos, and documents are **not** automatically deleted.
- The bot logs successful deletions and reasons for many failures. Telegram can reject a deletion, and a process may be unable to recover a message ID that was never persisted before an abrupt shutdown.
- `DATABASE_URL` is recommended for recovery across hosting restarts/redeploys. The local JSON fallback alone is not durable on ephemeral hosting filesystems.

## Technology Stack

- **Python 3.12** — application runtime
- **python-telegram-bot** — Telegram Bot API integration
- **yt-dlp** — media extraction and download backend
- **FFmpeg / FFprobe** — stream processing, media preparation, and splitting
- **Deno** — runtime dependency used by supported extractor flows
- **gallery-dl** — best-effort support for supported image/gallery sources
- **PostgreSQL** — optional durable storage for pending auto-deletion records
- **Docker and Render** — container packaging and example deployment configuration

## Troubleshooting

**The bot does not respond**
- Check the hosting logs and confirm the correct `BOT_TOKEN` is configured.
- Confirm the deployed version and runtime match the repository configuration.
- Make sure no duplicate polling service is using the same bot token.

**A quality menu or download fails**
- Check whether the source URL is public and supported.
- Try automatic quality selection if a specific format is unavailable.
- Review the logs for extractor, FFmpeg, timeout, or source-access errors.

**Instagram media is unavailable**
- Public Instagram extraction is best-effort. Rate limits, login requirements, content type, and hosting IP restrictions can prevent downloads.
- Private media and login-wall bypass are not supported.

**Temporary messages remain in chat**
- Verify that the latest version is deployed and that the auto-delete watchdog starts in the logs.
- Look for `Auto-delete scheduled`, `Auto-delete succeeded`, or `Telegram rejected auto-delete` entries.
- Configure `DATABASE_URL` if pending deletion recovery must survive instance replacement.

**Media conversion is slow or fails**
- Large or modern-codec files can be CPU-intensive. The bot uses bounded processing and may fall back to sending a compatible original file when full conversion is not practical.
- Hosting CPU, memory, disk, network, and runtime limits still apply.

## Security and Responsible Use

- Store `BOT_TOKEN`, webhook secrets, database credentials, and cookie data only in secret environment variables.
- If a token is exposed, revoke it through BotFather and update the hosting environment immediately.
- Do not run unknown shell scripts from untrusted URLs as part of the build process.
- Use authorized cookies only for accounts and content you are permitted to access.
- Respect creators' rights, platform terms, and applicable law. This project is a downloader utility, not a mechanism for bypassing access controls.

## Limitations

- Extractor support can change when third-party platforms change their websites or APIs.
- Instagram availability is not guaranteed.
- The default upload target is 45 MiB per part, and source size defaults to 1024 MiB. Large files may be split, but splitting is not guaranteed to succeed for every media type.
- Render Free storage is ephemeral; statistics/history files in `/tmp` can disappear after restart or redeploy.
- Full video transcoding may be CPU-intensive and is time-limited.
- Live behavior must be verified in the target hosting environment; offline/mock checks cannot guarantee every external service will work.

## Contributing

Bug reports and improvement proposals should include the expected behavior, actual behavior, reproducible steps, and relevant logs with tokens, cookies, user data, and other secrets removed. Please avoid sharing private media URLs or credentials in public issues.

## License

No `LICENSE` file is currently specified in this repository. Add a license before presenting the project as open source or granting permission for reuse and redistribution.

---

<p align="center"><strong>N14L18</strong><br />Built for a clean, queue-based Telegram media workflow.</p>
