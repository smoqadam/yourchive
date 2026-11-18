"""Offline integration tests: real SQLite and HTTP stack, fixture downloader process."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from app import main as archive


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patches = [patch.object(archive, 'DATA', self.root / 'data'),
                        patch.object(archive, 'MEDIA', self.root / 'media'),
                        patch.dict(os.environ, {'ARCHIVE_PASSWORD': ''})]
        for p in self.patches:
            p.start()
        archive.stop.clear()
        archive.initialize()
        # API tests don't start the background worker unless explicitly requested.
        self.client = TestClient(archive.app)

    def tearDown(self):
        self.client.close()
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def fixture(self, video_id='abc12345678', playlist='PL1'):
        folder = archive.MEDIA / 'videos' / video_id
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f'{video_id}.mp4'
        path.write_bytes(b'0123456789' * 100)
        info = {'id': video_id, 'title': '<script>Archive test</script>', 'channel': 'A creator',
                'channel_id': 'UC1', 'duration': 123, 'height': 1080, 'description': 'Description',
                'playlist_id': playlist, 'playlist_title': 'A playlist'}
        path.with_suffix('.info.json').write_text(json.dumps(info))
        path.with_suffix('.en.vtt').write_text('WEBVTT\n\n00:00.000 --> 00:02.000\nHello\n')
        path.with_suffix('.jpg').write_bytes(b'thumbnail')
        return path

    def job(self, url='https://www.youtube.com/watch?v=abc12345678'):
        response = self.client.post('/api/jobs', json={'url': url})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()['id']

    def test_url_validation_and_duplicate_queue(self):
        for url in ['file:///etc/passwd', 'https://youtube.com.evil.org/watch?v=x',
                    'http://youtube.com/watch?v=x', 'https://user:pass@youtube.com/watch?v=x',
                    'https://youtube.com:123/watch?v=x', 'https://example.com/video']:
            self.assertEqual(self.client.post('/api/jobs', json={'url': url}).status_code, 422)
        self.job()
        self.assertEqual(self.client.post('/api/jobs', json={'url':'https://www.youtube.com/watch?v=abc12345678'}).status_code, 409)
        self.job('https://www.youtube.com/playlist?list=PL123')
        self.job('https://www.youtube.com/@example/videos')

    def test_import_playlists_progress_and_delete(self):
        path = self.fixture()
        self.assertTrue(archive.import_video(path))
        library = self.client.get('/api/library').json()
        self.assertEqual(len(library['videos']), 1)
        self.assertEqual(library['playlists'][0]['count'], 1)
        response = self.client.patch('/api/videos/abc12345678', json={'favorite':True, 'position':23, 'watched':True})
        self.assertEqual(response.status_code, 200)
        archive.import_video(path)
        detail = self.client.get('/api/videos/abc12345678').json()
        self.assertEqual(detail['position'], 23)
        self.assertTrue(detail['favorite'])
        self.assertEqual(detail['subtitles'][0]['language'], 'en')
        self.assertEqual(len(detail['files']), 4)
        self.assertEqual(self.client.patch('/api/videos/abc12345678',json={'position':-1}).status_code,422)
        exported = self.client.get('/api/export')
        self.assertIn('attachment', exported.headers['content-disposition'])
        self.assertEqual(len(exported.json()['videos']), 1)
        self.assertEqual(self.client.delete('/api/videos/abc12345678').status_code, 200)
        self.assertFalse(path.parent.exists())
        self.assertEqual(self.client.get('/api/library').json()['playlists'], [])

    def test_media_ranges_and_traversal(self):
        path = self.fixture()
        archive.import_video(path)
        url = '/media/' + str(path.relative_to(archive.MEDIA))
        result = self.client.get(url, headers={'Range':'bytes=10-19'})
        self.assertEqual(result.status_code, 206)
        self.assertEqual(result.content, b'0123456789')
        self.assertEqual(result.headers['content-range'], 'bytes 10-19/1000')
        with self.assertRaises(archive.HTTPException):
            archive.safe_media('../data/archive.db')
        (archive.MEDIA / 'secret').symlink_to(archive.DATA / 'archive.db')
        self.assertEqual(self.client.get('/media/secret').status_code, 404)

    def test_settings_and_origin_protection(self):
        options = {'quality':'2160','languages':'en.*,en,de','auto_subtitles':False}
        self.assertEqual(self.client.put('/api/settings',json=options).json(),options)
        self.assertEqual(self.client.get('/api/settings').json(), options)
        self.assertEqual(self.client.put('/api/settings',json={**options,'quality':'100000'}).status_code,422)
        self.assertEqual(self.client.post('/api/jobs',json={'url':'https://youtu.be/abc'},headers={'Origin':'https://evil.test'}).status_code,403)
        job_id=self.job()
        with archive.db() as c:
            self.assertEqual(json.loads(c.execute('SELECT options FROM jobs WHERE id=?',(job_id,)).fetchone()[0]),options)

    def test_auth_protects_media_and_api(self):
        with patch.dict(os.environ, {'ARCHIVE_PASSWORD':'password'}):
            self.assertEqual(self.client.get('/api/health').status_code,200)
            for url in ['/', '/api/library','/api/export','/media/test']:
                self.assertEqual(self.client.get(url).status_code,401)
            self.assertEqual(self.client.get('/api/library',auth=('user','wrong')).status_code,401)
            self.assertEqual(self.client.get('/api/library',auth=('user','password')).status_code,200)

    def test_queue_cancel_retry_and_restart_recovery(self):
        job_id = self.job()
        self.assertEqual(self.client.post(f'/api/jobs/{job_id}/cancel').status_code,200)
        self.assertEqual(self.client.get('/api/jobs').json()[0]['status'],'cancelled')
        self.assertEqual(self.client.post(f'/api/jobs/{job_id}/retry').status_code,200)
        archive.job_update(job_id,status='running')
        archive.initialize()
        self.assertEqual(self.client.get('/api/jobs').json()[0]['status'],'queued')
        self.assertEqual(self.client.post(f'/api/jobs/{job_id}/retry').status_code,409)

    def test_download_process_imports_and_reports_failures(self):
        path = self.fixture()
        job_id=self.job()
        payload=json.dumps({'filepath':str(path),'playlist_id':'PL2','playlist_title':'Second collection'})
        script=f"print('YOURCHIVE_PROGRESS:50.0%',flush=True); print('YOURCHIVE_FILE:'+{payload!r},flush=True)"
        with archive.db() as c:
            job=dict(c.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone())
        with patch.object(archive,'download_command',return_value=[sys.executable,'-c',script]):
            archive.run_job(job)
        result=self.client.get('/api/jobs').json()[0]
        self.assertEqual(result['status'],'completed',result)
        self.assertEqual(result['completed'],1)
        self.assertEqual(self.client.get('/api/library').json()['playlists'][0]['id'],'PL2')
        second=self.job('https://youtu.be/failed')
        with archive.db() as c:
            job=dict(c.execute('SELECT * FROM jobs WHERE id=?',(second,)).fetchone())
        with patch.object(archive,'download_command',return_value=[sys.executable,'-c',"print('ERROR: Fixture error'); raise SystemExit(1)"]):
            archive.run_job(job)
        result=self.client.get('/api/jobs').json()[0]
        self.assertEqual(result['status'],'failed')
        self.assertIn('Fixture error',result['error'])

    def test_completed_media_protected_while_jobs_pending(self):
        archive.import_video(self.fixture())
        job_id=self.job()
        self.assertEqual(self.client.delete('/api/videos/abc12345678').status_code,409)
        self.client.post(f'/api/jobs/{job_id}/cancel')
        self.assertEqual(self.client.delete('/api/videos/abc12345678').status_code,200)

    def test_cancelled_job_cannot_be_claimed_by_worker(self):
        job_id=self.job()
        with archive.db() as c:
            job=dict(c.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone())
        self.client.post(f'/api/jobs/{job_id}/cancel')
        with patch.object(archive.subprocess,'Popen') as spawn:
            archive.run_job(job)
            spawn.assert_not_called()

    def test_downloader_flags(self):
        command=archive.download_command({'url':'https://youtu.be/abc','options':json.dumps(archive.DEFAULTS)})
        self.assertIn('--write-info-json',command)
        self.assertIn('--write-subs',command)
        self.assertIn('--write-auto-subs',command)
        self.assertIn('--ignore-config',command)
        self.assertEqual(command[-2:],[ '--','https://youtu.be/abc'])

    def test_same_video_can_belong_to_multiple_playlists(self):
        path = self.fixture()
        archive.import_video(path)
        archive.import_video(path, {'playlist_id':'PL2', 'playlist_title':'Another playlist'})
        library = self.client.get('/api/library').json()
        self.assertEqual(len(library['videos']), 1)
        self.assertEqual(len(library['memberships']), 2)

    def test_running_cancellation_stops_process(self):
        job_id = self.job()
        with archive.db() as c:
            job = dict(c.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone())
        script = "import time; print('YOURCHIVE_PROGRESS:1.0%',flush=True); time.sleep(60)"
        with patch.object(archive, 'download_command', return_value=[sys.executable, '-c', script]):
            thread = threading.Thread(target=archive.run_job, args=(job,))
            thread.start()
            deadline = time.monotonic() + 5
            while archive.active_job_id != job_id and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(archive.active_job_id, job_id)
            self.assertEqual(self.client.post(f'/api/jobs/{job_id}/cancel').status_code, 200)
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(self.client.get('/api/jobs').json()[0]['status'], 'cancelled')

    def test_actual_downloader_with_local_media_and_captions(self):
        # Test the real yt-dlp + FFmpeg pipeline without a YouTube dependency.
        import functools
        import http.server
        source = self.root / 'source'
        source.mkdir()
        archive.subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y',
            '-f','lavfi','-i','color=c=green:s=160x90:r=5',
            '-f','lavfi','-i','sine=frequency=440','-t','1',
            '-c:v','libx264','-c:a','aac','-pix_fmt','yuv420p',str(source/'clip.mp4')], check=True)
        (source/'captions.vtt').write_text('WEBVTT\n\n00:00.000 --> 00:01.000\nSaved caption\n')
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(source))
        server = http.server.ThreadingHTTPServer(('127.0.0.1',0),handler)
        server_thread=threading.Thread(target=server.serve_forever,daemon=True)
        server_thread.start()
        base=f'http://127.0.0.1:{server.server_port}'
        info={'id':'localtest01','title':'Local pipeline fixture','extractor':'generic',
              'extractor_key':'Generic','webpage_url':base+'/clip.mp4',
              'url':base+'/clip.mp4','ext':'mp4','vcodec':'avc1','acodec':'mp4a.40.2',
              'height':90,'width':160,'duration':1,
              'subtitles':{'en':[{'url':base+'/captions.vtt','ext':'vtt'}]}}
        info_path=source/'input.info.json'
        info_path.write_text(json.dumps(info))
        job_id=self.job()
        with archive.db() as c:
            job=dict(c.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone())
        command=archive.download_command(job)[:-2]+['--load-info-json',str(info_path)]
        try:
            with patch.object(archive,'download_command',return_value=command):
                archive.run_job(job)
            result=self.client.get('/api/jobs').json()[0]
            self.assertEqual(result['status'],'completed',result)
            detail=self.client.get('/api/videos/localtest01').json()
            self.assertEqual(detail['title'],'Local pipeline fixture')
            self.assertEqual(detail['subtitles'][0]['language'],'en')
            self.assertTrue(any(f['name'].endswith('.info.json') for f in detail['files']))
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join()


if __name__ == '__main__':
    unittest.main()
