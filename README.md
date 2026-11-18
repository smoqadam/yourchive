# Yourchive

A self-hosted YouTube archive with a minimal, responsive interface. Paste a video,
playlist, or channel URL and keep its videos, available subtitles, thumbnails,
descriptions, and original metadata on your own server.

## Start with Docker

Docker Engine/Desktop and Docker Compose are the only host dependencies.

```sh
cp .env.example .env
docker compose up -d --build
```

Open **http://localhost:8000** and choose **Add to archive**.

The default binding is local-only. To access from another device, set
`BIND_ADDRESS=0.0.0.0` and `ARCHIVE_PASSWORD` in `.env`, then run
`docker compose up -d`. Authentication uses HTTP Basic; any username works.
Use an HTTPS reverse proxy for access beyond a trusted local machine. Preserve
the original `Host` header through the proxy so same-origin checks work.

## Included

- Video, playlist, and channel downloads through yt-dlp, with FFmpeg for merging.
- Persistent, sequential download queue with progress, cancellation, error details,
  retries, and restart recovery. Completed videos survive a partial source failure.
- Original JSON metadata, description files, local thumbnails, and WebVTT subtitles.
- Configurable maximum quality, subtitle languages, and automatic captions.
- Browse all videos, channels, playlists, or favorites; search and sort; grid/list views.
- In-browser playback with captions, playback resume, and watched/unwatched filters.
- Individual archive file downloads, library catalog export, and confirmed deletion.
- Disk availability and archived video size, optional password protection, and health checks.
- No CDN, external font requests, frontend build step, or hosted-service dependency.

## Docker development and tests

All dependencies and development processes run inside Docker. Source and tests are
mounted read-only into the development container; Python edits reload automatically.
Reload the browser for frontend edits.

```sh
docker compose -f compose.yaml -f compose.dev.yaml up -d --build
docker compose -f compose.yaml -f compose.dev.yaml exec yourchive python -m unittest discover -s tests -v
docker compose -f compose.yaml -f compose.dev.yaml exec yourchive node --check app/static/app.js
docker compose logs -f yourchive
```

The integration tests use temporary databases and fixture subprocesses, without
contacting YouTube or changing your archive. They cover URL validation, playlist
membership, media byte ranges, favorites/progress, deletion, authentication,
cross-origin protection, queue transitions, and downloader output handling. A local
HTTP fixture also exercises the actual yt-dlp/FFmpeg video and subtitle pipeline.

Run the browser checks in their own disposable Docker project:

```sh
docker compose -p yourchive-qa -f compose.qa.yaml up --build --abort-on-container-exit --exit-code-from browser
docker compose -p yourchive-qa -f compose.qa.yaml down
```

The QA archive uses temporary container storage and never mounts the real archive
volumes. Desktop and mobile screenshots are written to `artifacts/` in this project.

To return to the production image:

```sh
docker compose up -d --build --force-recreate
```

## Persistent storage

| Volume | Contents |
| --- | --- |
| `archive-data` mounted at `/data` | SQLite catalog, queue, preferences, watch progress |
| `archive-media` mounted at `/media` | Videos, thumbnails, subtitles, original metadata |

Media uses stable paths: `/media/videos/VIDEO_ID/VIDEO_ID.ext`.
Playlist metadata is stored under `/media/playlists/`.
Re-adding a source reuses existing files; a video can belong to multiple playlists.
Changing download quality does not replace an existing video: delete it first if you
want to download it at another quality.

For an existing storage disk, replace the media volume with a bind mount, such as
`./media:/media`. That directory must be writable by UID/GID **1000:1000**.
Keep the database on a local filesystem with SQLite-compatible locking.

Back up **both volumes**, with the service stopped for a consistent snapshot.
Restore both to the same mount paths. A catalog JSON export is useful for inspection
and portability but is not a complete backup or an importable restore format.
`docker compose down` preserves data; `docker compose down -v` deletes the volumes.

## Download behavior

Only HTTPS YouTube URLs are accepted. A watch URL with a `list` parameter archives
that playlist; remove `list` to archive just the video. Channel downloads run once;
re-add a channel later to collect new uploads. There is no scheduled subscription
polling in this version. Live streams in progress are skipped.

Subtitles are saved when available in the requested languages. Defaults are
`en.*,en`; use `all` or codes such as `en.*,en,de`. YouTube may not provide captions
for every video, and automatically generated captions can be incomplete.

The downloader prefers H.264/AAC for browser compatibility. When these formats are
unavailable it falls back to other codecs; your browser may need an external player
for those files. Use **Video file** to download them. Downloads resume from partial
files when supported by the source. Cancelling keeps completed and partial files;
retry to continue.

YouTube can require account cookies or reject requests based on rate limits or IP
reputation. Those failures appear in Downloads. If cookies are needed, mount a
Netscape-format cookie file writable by the container user and set
`YTDLP_COOKIES_FILE` to its container path. Keep the file private and out of source
control. yt-dlp can update its cookie jar, so a read-only cookie mount may fail.

yt-dlp and its JavaScript challenge components are installed in the image; Node.js
is included as the JavaScript runtime. For downloader updates, update its version
in `requirements.txt`, then rebuild with `docker compose build --no-cache` and run
`docker compose up -d`. See the [yt-dlp documentation](https://github.com/yt-dlp/yt-dlp)
for source-specific limitations and cookie guidance.

## Architecture

FastAPI serves the API, static frontend, and media with byte-range support.
SQLite stores catalog and queue state. A single background worker launches yt-dlp
as a subprocess, imports completed files, and terminates the process group on
cancellation. FFmpeg merges streams and converts subtitles/thumbnails.

Run **one application instance with one Uvicorn worker** against each pair of
volumes. The queue is designed for a personal archive, not distributed workers.
The API schema is available at `/openapi.json`.

## Verification status

Verified inside Docker:

- Production and development image builds, healthy production container, and HTTP service startup.
- All 13 integration tests, including the actual yt-dlp/FFmpeg subtitle pipeline.
- Chromium browser checks for library navigation, search, favorites, playback,
  captions, watch progress, settings, URL validation, deletion, and mobile layout.
- A live YouTube download of a short public video, producing an MP4, two English
  WebVTT caption files, thumbnail, description, and JSON metadata.
- JavaScript syntax validation and desktop/mobile screenshot review.

The live check used temporary storage and left the real library untouched. YouTube
availability and restrictions can still vary by video, account, and server IP.
To repeat the optional live check from the development container:

```sh
docker compose -f compose.yaml -f compose.dev.yaml exec yourchive python -m tests.live_download
```
