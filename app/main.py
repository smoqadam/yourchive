from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

log = logging.getLogger('yourchive')
DATA = Path(os.environ.get('DATA_DIR', './data')).resolve()
MEDIA = Path(os.environ.get('MEDIA_DIR', './media')).resolve()
STATIC = Path(__file__).parent / 'static'
stop = threading.Event()
wake = threading.Event()
process_lock = threading.Lock()
active_process: subprocess.Popen | None = None
active_job_id: str | None = None
DEFAULTS = {'quality': '1080', 'languages': 'en.*,en', 'auto_subtitles': True}


@contextmanager
def db():
    connection = sqlite3.connect(DATA / 'archive.db', timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys=ON')
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def initialize():
    DATA.mkdir(parents=True, exist_ok=True)
    MEDIA.mkdir(parents=True, exist_ok=True)
    with db() as c:
        c.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS videos (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, channel TEXT NOT NULL,
            channel_id TEXT, description TEXT, duration REAL DEFAULT 0, upload_date TEXT,
            width INTEGER, height INTEGER, filepath TEXT NOT NULL, thumbnail TEXT,
            filesize INTEGER DEFAULT 0, added_at REAL NOT NULL, favorite INTEGER DEFAULT 0,
            position REAL DEFAULT 0, watched INTEGER DEFAULT 0, metadata TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS playlists (id TEXT PRIMARY KEY, title TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS playlist_videos (
            playlist_id TEXT REFERENCES playlists(id) ON DELETE CASCADE,
            video_id TEXT REFERENCES videos(id) ON DELETE CASCADE,
            PRIMARY KEY (playlist_id, video_id));
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, url TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
            created_at REAL NOT NULL, updated_at REAL NOT NULL, progress REAL DEFAULT 0,
            detail TEXT DEFAULT '', error TEXT DEFAULT '', options TEXT NOT NULL,
            completed INTEGER DEFAULT 0, cancel_requested INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
        ''')
        c.execute('INSERT OR IGNORE INTO settings VALUES (1, ?)', (json.dumps(DEFAULTS),))
        c.execute("UPDATE jobs SET status='cancelled', detail='Cancelled before restart' WHERE status='running' AND cancel_requested=1")
        c.execute("UPDATE jobs SET status='queued', detail='Resuming after restart', cancel_requested=0 WHERE status='running'")


class Options(BaseModel):
    quality: str = '1080'
    languages: str = Field(default='en.*,en', min_length=1, max_length=100)
    auto_subtitles: bool = True

    @field_validator('quality')
    @classmethod
    def quality_valid(cls, v):
        if v not in ('720', '1080', '1440', '2160', 'best'):
            raise ValueError('Choose a supported quality')
        return v

    @field_validator('languages')
    @classmethod
    def languages_valid(cls, v):
        if not re.fullmatch(r'[a-zA-Z0-9.,_*\-]+', v):
            raise ValueError('Use language codes separated by commas, or all')
        return v


class JobInput(BaseModel):
    url: str = Field(max_length=2048)
    options: Options | None = None

    @field_validator('url')
    @classmethod
    def url_valid(cls, value):
        value = value.strip()
        p = urlparse(value)
        if p.scheme != 'https' or p.hostname not in ('youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtu.be', 'music.youtube.com') or p.username or p.password or p.port not in (None, 443):
            raise ValueError('Enter an HTTPS YouTube video, playlist, or channel URL')
        if not p.path or p.path == '/':
            raise ValueError('Enter a video, playlist, or channel URL')
        return value


class VideoUpdate(BaseModel):
    favorite: bool | None = None
    position: float | None = Field(default=None, ge=0, le=1e7)
    watched: bool | None = None


def get_settings():
    with db() as c:
        return json.loads(c.execute('SELECT value FROM settings WHERE id=1').fetchone()[0])


def job_update(job_id, **values):
    values['updated_at'] = time.time()
    with db() as c:
        c.execute(f"UPDATE jobs SET {', '.join(k + '=?' for k in values)} WHERE id=?", (*values.values(), job_id))


def safe_media(relative: str) -> Path:
    result = (MEDIA / relative).resolve()
    if not result.is_relative_to(MEDIA) or not result.is_file():
        raise HTTPException(404, 'File not found')
    return result


def import_video(path: Path, context: dict | None = None):
    path = path.resolve()
    if not path.is_relative_to(MEDIA) or not path.is_file():
        return False
    info_path = path.with_suffix('.info.json')
    if not info_path.exists():
        return False
    info = json.loads(info_path.read_text())
    if context is not None:
        # A reused media file can belong to another playlist. Its saved info JSON
        # still describes its first download, so use this extraction's context.
        info.update({key: context.get(key) for key in ('playlist_id', 'playlist_title')})
    video_id = info.get('id', '')
    if not re.fullmatch(r'[\w-]+', video_id):
        return False
    thumbnail = next((p for p in path.parent.glob(path.stem + '.*') if p.suffix.lower() in ('.jpg', '.png', '.webp')), None)
    with db() as c:
        c.execute('''INSERT INTO videos
            (id,title,channel,channel_id,description,duration,upload_date,width,height,filepath,thumbnail,filesize,added_at,metadata)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
            title=excluded.title,channel=excluded.channel,description=excluded.description,
            filepath=excluded.filepath,thumbnail=excluded.thumbnail,filesize=excluded.filesize,
            width=excluded.width,height=excluded.height,metadata=excluded.metadata''', (
            video_id, info.get('title') or video_id, info.get('channel') or info.get('uploader') or 'Unknown channel',
            info.get('channel_id') or info.get('uploader_id'), info.get('description', ''), info.get('duration') or 0,
            info.get('upload_date'), info.get('width'), info.get('height'), str(path.relative_to(MEDIA)),
            str(thumbnail.relative_to(MEDIA)) if thumbnail else None, path.stat().st_size, time.time(), json.dumps(info)))
        if info.get('playlist_id'):
            c.execute('INSERT INTO playlists VALUES (?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title',
                      (info['playlist_id'], info.get('playlist_title') or info.get('playlist') or info['playlist_id']))
            c.execute('INSERT OR IGNORE INTO playlist_videos VALUES (?,?)', (info['playlist_id'], video_id))
    return True


def download_command(job):
    options = json.loads(job['options'])
    quality = '' if options['quality'] == 'best' else f"[height<={options['quality']}]"
    # Prefer browser-compatible H.264/AAC; fall back when YouTube offers other codecs.
    fmt = f'bv*{quality}[vcodec^=avc1]+ba[acodec^=mp4a]/b{quality}[ext=mp4]/bv*{quality}+ba/b{quality}'
    cmd = [sys.executable, '-m', 'yt_dlp', '--ignore-config', '--yes-playlist', '--ignore-errors',
           '--no-overwrites', '--no-post-overwrites', '--newline', '--progress', '--no-simulate',
           '--no-colors', '--socket-timeout', '30', '--retries', '3', '--fragment-retries', '3',
           '--match-filters', '!is_live', '--format', fmt, '--merge-output-format', 'mp4',
           '--write-info-json', '--write-description', '--write-thumbnail', '--convert-thumbnails', 'jpg',
           '--write-subs', '--sub-langs', options['languages'], '--sub-format', 'vtt/best', '--convert-subs', 'vtt',
           '--embed-metadata', '--embed-chapters', '--js-runtimes', 'node',
           '--paths', str(MEDIA), '--output', 'videos/%(id)s/%(id)s.%(ext)s',
           '--output', 'pl_infojson:playlists/%(id)s.info.json',
           '--progress-template', 'download:YOURCHIVE_PROGRESS:%(progress._percent_str)s',
           '--print', 'after_move:YOURCHIVE_FILE:%()j']
    if options['auto_subtitles']:
        cmd += ['--write-auto-subs']
    cookiefile = os.environ.get('YTDLP_COOKIES_FILE')
    if cookiefile:
        cmd += ['--cookies', cookiefile]
    return cmd + ['--', job['url']]


def run_job(job):
    global active_process, active_job_id
    errors = []
    completed = 0
    proc = None
    with db() as c:
        claimed = c.execute("UPDATE jobs SET status='running', progress=0, error='', detail='Reading source…' WHERE id=? AND status='queued' AND cancel_requested=0", (job['id'],)).rowcount
    if not claimed:
        return
    try:
        with process_lock:
            if stop.is_set():
                return
            with db() as c:
                cancelled = c.execute('SELECT cancel_requested FROM jobs WHERE id=?', (job['id'],)).fetchone()[0]
            if cancelled:
                job_update(job['id'], status='cancelled')
                return
            proc = subprocess.Popen(download_command(job), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, bufsize=1, start_new_session=True)
            active_process = proc
            active_job_id = job['id']
        assert proc.stdout
        for raw in proc.stdout:
            line = raw.strip()
            if line.startswith('YOURCHIVE_FILE:'):
                item = json.loads(line.split(':', 1)[1])
                if import_video(Path(item['filepath']), item):
                    completed += 1
                    job_update(job['id'], completed=completed, detail=f'{completed} video(s) archived', progress=0)
            elif line.startswith('YOURCHIVE_PROGRESS:'):
                match = re.search(r'([\d.]+)%', line)
                if match:
                    job_update(job['id'], progress=min(100, float(match[1])), detail='Downloading video…')
            elif line.startswith('ERROR:'):
                errors.append(line[-2000:])
            elif line.startswith(('[download]', '[Merger]', '[Metadata]', '[SubtitlesConvertor]')):
                job_update(job['id'], detail=line[:300])
            with db() as c:
                cancel = c.execute('SELECT cancel_requested FROM jobs WHERE id=?', (job['id'],)).fetchone()[0]
            if cancel or stop.is_set():
                terminate(proc)
                break
        code = proc.wait()
        with db() as c:
            cancel = c.execute('SELECT cancel_requested FROM jobs WHERE id=?', (job['id'],)).fetchone()[0]
        if stop.is_set():
            job_update(job['id'], status='queued', detail='Waiting to resume')
        elif cancel:
            job_update(job['id'], status='cancelled', detail='Cancelled; completed videos are kept')
        elif code != 0 or errors:
            job_update(job['id'], status='failed', error='\n'.join(errors[-5:]) or f'Downloader exited with code {code}', detail=f'{completed} video(s) archived before errors')
        else:
            job_update(job['id'], status='completed', progress=100, detail=f'{completed} video(s) archived')
    except Exception as exc:
        log.exception('Download failed')
        job_update(job['id'], status='failed', error=str(exc)[:2000])
    finally:
        with process_lock:
            if proc and proc.poll() is None:
                terminate(proc)
            if proc and proc.stdout:
                proc.stdout.close()
            active_process = None
            active_job_id = None


def terminate(proc):
    import signal
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
    except ProcessLookupError:
        pass


def worker():
    while not stop.is_set():
        with db() as c:
            job = c.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
        if job:
            run_job(dict(job))
        else:
            wake.wait(2)
            wake.clear()


@asynccontextmanager
async def lifespan(app):
    initialize()
    stop.clear()
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    yield
    stop.set()
    wake.set()
    with process_lock:
        if active_process:
            terminate(active_process)
    thread.join(timeout=10)


app = FastAPI(title='Yourchive', lifespan=lifespan, docs_url=None, redoc_url=None)


@app.middleware('http')
async def guard(request: Request, call_next):
    password = os.environ.get('ARCHIVE_PASSWORD', '')
    if password and request.url.path != '/api/health':
        try:
            scheme, token = request.headers.get('authorization', '').split(' ', 1)
            username, supplied = base64.b64decode(token, validate=True).decode().split(':', 1)
            authorized = scheme.lower() == 'basic' and hmac.compare_digest(hashlib.sha256(supplied.encode()).digest(), hashlib.sha256(password.encode()).digest())
        except (ValueError, UnicodeError):
            authorized = False
        if not authorized:
            return JSONResponse({'detail': 'Authentication required'}, 401, headers={'WWW-Authenticate': 'Basic realm="Yourchive"'})
    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
        origin = request.headers.get('origin')
        if request.headers.get('sec-fetch-site') == 'cross-site' or (origin and urlparse(origin).netloc != request.headers.get('host')):
            return JSONResponse({'detail': 'Cross-origin writes are not allowed'}, 403)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'same-origin'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Content-Security-Policy'] = "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; media-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
    return response


@app.get('/api/health')
def health():
    return {'status': 'ok'}


@app.get('/api/library')
def library():
    with db() as c:
        videos = [dict(r) for r in c.execute('SELECT id,title,channel,channel_id,duration,upload_date,height,filesize,added_at,favorite,position,watched,thumbnail FROM videos ORDER BY added_at DESC')]
        playlists = [dict(r) for r in c.execute('SELECT p.*, count(pv.video_id) AS count FROM playlists p JOIN playlist_videos pv ON p.id=pv.playlist_id GROUP BY p.id ORDER BY p.title')]
        memberships = [dict(r) for r in c.execute('SELECT * FROM playlist_videos')]
    disk = shutil.disk_usage(MEDIA)
    return {'videos': videos, 'playlists': playlists, 'memberships': memberships,
            'storage': {'total': disk.total, 'free': disk.free, 'videos': sum(v['filesize'] for v in videos)}}


@app.get('/api/videos/{video_id}')
def video(video_id: str):
    with db() as c:
        row = c.execute('SELECT * FROM videos WHERE id=?', (video_id,)).fetchone()
    if not row:
        raise HTTPException(404, 'Video not found')
    result = dict(row)
    path = safe_media(result['filepath'])
    result['subtitles'] = [{'language': p.name[len(path.stem) + 1:-4], 'file': str(p.relative_to(MEDIA))} for p in sorted(path.parent.glob(path.stem + '.*.vtt'))]
    result['files'] = [{'name': p.name, 'file': str(p.relative_to(MEDIA)), 'size': p.stat().st_size} for p in sorted(path.parent.iterdir()) if p.is_file() and p.suffix not in ('.part', '.ytdl')]
    result['metadata'] = json.loads(result['metadata'])
    return result


@app.patch('/api/videos/{video_id}')
def update_video(video_id: str, update: VideoUpdate):
    values = update.model_dump(exclude_none=True)
    with db() as c:
        if not c.execute('SELECT 1 FROM videos WHERE id=?', (video_id,)).fetchone():
            raise HTTPException(404, 'Video not found')
        if values:
            c.execute(f"UPDATE videos SET {', '.join(k + '=?' for k in values)} WHERE id=?", (*values.values(), video_id))
    return {'ok': True}


@app.delete('/api/videos/{video_id}')
def delete_video(video_id: str):
    # Deleting while the worker writes could leave partial files or stale records.
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        if c.execute("SELECT 1 FROM jobs WHERE status IN ('running','queued')").fetchone():
            raise HTTPException(409, 'Wait for downloads to finish or cancel them before deleting videos')
        row = c.execute('SELECT filepath FROM videos WHERE id=?', (video_id,)).fetchone()
        if not row:
            raise HTTPException(404, 'Video not found')
        parent = (MEDIA / row['filepath']).resolve().parent
        if parent.parent != MEDIA / 'videos' or parent.name != video_id:
            raise HTTPException(400, 'Invalid archive path')
        shutil.rmtree(parent, ignore_errors=False)
        c.execute('DELETE FROM videos WHERE id=?', (video_id,))
    return {'ok': True}


@app.get('/api/jobs')
def jobs():
    with db() as c:
        return [dict(r) for r in c.execute('SELECT * FROM jobs ORDER BY created_at DESC LIMIT 200')]


@app.post('/api/jobs', status_code=201)
def create_job(payload: JobInput):
    now = time.time()
    job_id = uuid4().hex
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        if c.execute("SELECT 1 FROM jobs WHERE url=? AND status IN ('queued','running')", (payload.url,)).fetchone():
            raise HTTPException(409, 'This source is already in the queue')
        c.execute('INSERT INTO jobs (id,url,created_at,updated_at,options) VALUES (?,?,?,?,?)',
                  (job_id, payload.url, now, now, json.dumps(payload.options.model_dump() if payload.options else get_settings())))
    wake.set()
    return {'id': job_id}


@app.post('/api/jobs/{job_id}/{action}')
def job_action(job_id: str, action: str):
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not row:
            raise HTTPException(404, 'Download not found')
        if action == 'cancel' and row['status'] in ('queued', 'running'):
            c.execute("UPDATE jobs SET cancel_requested=1, status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END WHERE id=?", (job_id,))
        elif action == 'retry' and row['status'] in ('failed', 'cancelled'):
            if c.execute("SELECT 1 FROM jobs WHERE url=? AND status IN ('queued','running')", (row['url'],)).fetchone():
                raise HTTPException(409, 'This source is already in the queue')
            c.execute("UPDATE jobs SET status='queued', cancel_requested=0, progress=0, completed=0, error='', detail='' WHERE id=?", (job_id,))
        else:
            raise HTTPException(409, 'This action is not available')
    if action == 'cancel' and row['status'] == 'running':
        with process_lock:
            if active_process and active_job_id == job_id:
                terminate(active_process)
    wake.set()
    return {'ok': True}


@app.get('/api/settings')
def settings():
    return get_settings()


@app.put('/api/settings')
def save_settings(options: Options):
    with db() as c:
        c.execute('UPDATE settings SET value=? WHERE id=1', (options.model_dump_json(),))
    return options


@app.get('/api/export')
def export():
    with db() as c:
        data = {'videos': [dict(r) for r in c.execute('SELECT * FROM videos')],
                'playlists': [dict(r) for r in c.execute('SELECT * FROM playlists')],
                'memberships': [dict(r) for r in c.execute('SELECT * FROM playlist_videos')]}
    return JSONResponse(data, headers={'Content-Disposition': 'attachment; filename="yourchive-library.json"'})


@app.get('/media/{path:path}')
def media(path: str, download: bool = False):
    file = safe_media(path)
    return FileResponse(file, filename=file.name if download else None,
                        media_type='text/vtt' if file.suffix == '.vtt' else None)


app.mount('/', StaticFiles(directory=STATIC, html=True), name='frontend')
