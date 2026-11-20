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
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager, contextmanager
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError, field_validator

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
JELLYFIN_DEFAULTS = {'enabled': False, 'url': '', 'api_key': '', 'media_path': '/media', 'last_sync': '', 'sync_state': 'Not configured', 'sync_error': ''}
jellyfin_lock = threading.Lock()


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
        if 'jellyfin_id' not in [r['name'] for r in c.execute('PRAGMA table_info(playlists)')]:
            c.execute('ALTER TABLE playlists ADD COLUMN jellyfin_id TEXT')
        settings = json.loads(c.execute('SELECT value FROM settings WHERE id=1').fetchone()[0])
        settings.setdefault('jellyfin', JELLYFIN_DEFAULTS.copy())
        for key, value in JELLYFIN_DEFAULTS.items():
            settings['jellyfin'].setdefault(key, value)
        c.execute('UPDATE settings SET value=? WHERE id=1', (json.dumps(settings),))


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


def jellyfin_call(config, method, endpoint, body=None):
    base = config['url'].rstrip('/')
    url = base + endpoint
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={
        'X-Emby-Token': config['api_key'], 'Accept': 'application/json',
        'Content-Type': 'application/json', 'X-Emby-Authorization': 'MediaBrowser Client="Yourchive", Device="Server", DeviceId="yourchive", Version="1.0"'})
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            payload = response.read()
            return json.loads(payload) if payload else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read(2048).decode(errors='replace')
        raise RuntimeError(f'Jellyfin returned HTTP {exc.code}: {detail}') from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f'Cannot reach Jellyfin: {exc}') from exc


def write_jellyfin_metadata(video):
    filepath = safe_media(video['filepath'])
    root = ET.Element('movie')
    ET.SubElement(root, 'title').text = video['title']
    ET.SubElement(root, 'originaltitle').text = video['title']
    ET.SubElement(root, 'plot').text = video.get('description') or ''
    ET.SubElement(root, 'studio').text = video.get('channel') or 'YouTube'
    ET.SubElement(root, 'uniqueid', {'type': 'youtube', 'default': 'true'}).text = video['id']
    if video.get('upload_date'):
        date = video['upload_date']
        if re.fullmatch(r'\d{8}', date):
            date = f'{date[:4]}-{date[4:6]}-{date[6:]}'
        ET.SubElement(root, 'premiered').text = date
        if len(date) >= 4 and date[:4].isdigit():
            ET.SubElement(root, 'year').text = date[:4]
    if video.get('duration'):
        ET.SubElement(root, 'runtime').text = str(round(video['duration'] / 60))
    ET.SubElement(root, 'tag').text = 'YouTube'
    ET.indent(root)
    target = filepath.with_suffix('.nfo')
    target.write_bytes(ET.tostring(root, encoding='utf-8', xml_declaration=True))
    return target


def sync_jellyfin():
    config = get_settings().get('jellyfin', {})
    if not config.get('enabled'):
        return {'synced': 0, 'playlists': 0, 'message': 'Jellyfin sync is disabled'}
    if not all(config.get(k) for k in ('url', 'api_key', 'media_path')):
        raise RuntimeError('Set the Jellyfin URL, API key, and shared media path first')
    if not jellyfin_lock.acquire(blocking=False):
        return {'synced': 0, 'playlists': 0, 'message': 'A Jellyfin sync is already running'}
    try:
        with db() as c:
            videos = [dict(r) for r in c.execute('SELECT * FROM videos ORDER BY added_at')]
            playlists = [dict(r) for r in c.execute('SELECT p.id,p.title,p.jellyfin_id,pv.video_id,v.metadata FROM playlists p JOIN playlist_videos pv ON pv.playlist_id=p.id JOIN videos v ON v.id=pv.video_id ORDER BY p.title')]
        for video in videos:
            write_jellyfin_metadata(video)
        # Resolve the archive's already-indexed files to Jellyfin item IDs.
        jellyfin_call(config, 'POST', '/Library/Refresh')
        expected_paths = {os.path.normpath(str(Path(config['media_path']) / v['filepath'])) for v in videos}
        items_by_path = {}
        deadline = time.monotonic() + 30
        while True:
            items_by_path = {}
            start = 0
            while True:
                page = jellyfin_call(config, 'GET', f'/Items?Recursive=true&Fields=Path&StartIndex={start}&Limit=1000')
                items = page.get('Items', [])
                for item in items:
                    if item.get('Path'):
                        items_by_path[os.path.normpath(item['Path'])] = item['Id']
                start += len(items)
                if not items or start >= page.get('TotalRecordCount', 0):
                    break
            if expected_paths.issubset(items_by_path) or time.monotonic() >= deadline:
                break
            time.sleep(1)
        media_root = Path(config['media_path'])
        video_items = {}
        for video in videos:
            expected = os.path.normpath(str(media_root / video['filepath']))
            if expected in items_by_path:
                video_items[video['id']] = items_by_path[expected]
        # Keep Jellyfin playlists owned by Yourchive in sync, preserving order.
        groups = {}
        for membership in playlists:
            groups.setdefault(membership['id'], {'title':membership['title'], 'jellyfin_id':membership['jellyfin_id'], 'entries':[]})['entries'].append((membership['video_id'], json.loads(membership['metadata']).get('playlist_index') or 0))
        synced_lists = 0
        user = jellyfin_call(config, 'GET', '/Users/Me')
        user_id = user['Id']
        for source_id, group in groups.items():
            entries = sorted(group['entries'], key=lambda entry: entry[1] or 2**31)
            ids = [video_items[video_id] for video_id, _ in entries if video_id in video_items]
            playlist_id = group['jellyfin_id']
            if not playlist_id:
                params = {'Name':f"YouTube · {group['title']}", 'MediaType':'Video', 'UserId':user_id}
                if ids:
                    params['Ids'] = ','.join(ids)
                query = urllib.parse.urlencode(params)
                created = jellyfin_call(config, 'POST', '/Playlists?' + query)
                playlist_id = created.get('Id') or created.get('PlaylistId')
                if not playlist_id:
                    raise RuntimeError(f"Jellyfin did not return an ID for playlist {group['title']}")
                with db() as c:
                    c.execute('UPDATE playlists SET jellyfin_id=? WHERE id=?', (playlist_id, source_id))
            elif ids:
                existing = jellyfin_call(config, 'GET', f'/Playlists/{playlist_id}/Items?UserId={urllib.parse.quote(user_id)}')
                existing_ids = {item['Id'] for item in existing.get('Items', [])}
                new_ids = [item_id for item_id in ids if item_id not in existing_ids]
                if new_ids:
                    query = urllib.parse.urlencode({'Ids':','.join(new_ids), 'UserId':user_id})
                    jellyfin_call(config, 'POST', f'/Playlists/{playlist_id}/Items?' + query)
            synced_lists += 1
        return {'synced':len(video_items), 'playlists':synced_lists, 'message':f"Synced {len(video_items)} videos and {synced_lists} playlists"}
    finally:
        jellyfin_lock.release()


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
            c.execute('INSERT INTO playlists (id,title) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title',
                      (info['playlist_id'], info.get('playlist_title') or info.get('playlist') or info['playlist_id']))
            c.execute('INSERT OR IGNORE INTO playlist_videos VALUES (?,?)', (info['playlist_id'], video_id))
    write_jellyfin_metadata({'id':video_id, 'title':info.get('title') or video_id,
        'description':info.get('description', ''), 'channel':info.get('channel') or info.get('uploader') or 'Unknown channel',
        'duration':info.get('duration'), 'upload_date':info.get('upload_date'), 'filepath':str(path.relative_to(MEDIA))})
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
            if completed and get_settings().get('jellyfin', {}).get('enabled'):
                try:
                    result = sync_jellyfin()
                    job_update(job['id'], detail=f"{completed} video(s) archived · {result['message']}")
                except Exception as sync_error:
                    log.exception('Jellyfin sync failed')
                    job_update(job['id'], detail=f'{completed} video(s) archived · Jellyfin sync failed', error=str(sync_error)[:2000])
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
def save_settings(payload: dict):
    try:
        options = Options.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(422, exc.errors(include_context=False, include_url=False)) from exc
    jellyfin = payload.get('jellyfin', {})
    enabled = bool(jellyfin.get('enabled', False))
    url = str(jellyfin.get('url', '')).strip().rstrip('/')
    api_key = str(jellyfin.get('api_key', '')).strip()
    media_path = str(jellyfin.get('media_path', '/media')).strip()
    if enabled:
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise HTTPException(422, 'Enter a valid Jellyfin server URL')
        if not api_key or not media_path.startswith('/') or '..' in Path(media_path).parts:
            raise HTTPException(422, 'Enter an API key and an absolute shared media path')
    current = get_settings().get('jellyfin', JELLYFIN_DEFAULTS.copy())
    current.update({'enabled':enabled, 'url':url, 'api_key':api_key, 'media_path':media_path})
    result = {**options.model_dump(), 'jellyfin':current}
    with db() as c:
        c.execute('UPDATE settings SET value=? WHERE id=1', (json.dumps(result),))
    return result


@app.post('/api/jellyfin/sync')
def jellyfin_sync():
    config = get_settings().get('jellyfin', {})
    if not config.get('enabled'):
        raise HTTPException(409, 'Enable Jellyfin sync and save your connection first')
    try:
        result = sync_jellyfin()
        with db() as c:
            settings = json.loads(c.execute('SELECT value FROM settings WHERE id=1').fetchone()[0])
            settings['jellyfin'].update({'last_sync':time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime()), 'sync_state':result['message'], 'sync_error':''})
            c.execute('UPDATE settings SET value=? WHERE id=1', (json.dumps(settings),))
        return result
    except Exception as exc:
        with db() as c:
            settings = json.loads(c.execute('SELECT value FROM settings WHERE id=1').fetchone()[0])
            settings['jellyfin'].update({'sync_state':'Sync failed', 'sync_error':str(exc)[:500]})
            c.execute('UPDATE settings SET value=? WHERE id=1', (json.dumps(settings),))
        raise HTTPException(502, str(exc)) from exc


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
