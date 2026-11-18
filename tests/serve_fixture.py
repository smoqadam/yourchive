"""Disposable, deterministic media library for browser QA, run only in Docker."""
import json
import subprocess
from pathlib import Path
import shutil
import uvicorn
from app import main

main.initialize()
video_id = 'fixture0001'
folder = main.MEDIA / 'videos' / video_id
folder.mkdir(parents=True, exist_ok=True)
video = folder / f'{video_id}.webm'
subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi',
                '-i', 'testsrc2=size=640x360:rate=24', '-t', '12', '-c:v', 'libvpx-vp9',
                '-deadline', 'realtime', '-cpu-used', '8', '-b:v', '300k',
                '-pix_fmt', 'yuv420p', str(video)], check=True)
subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i', str(video),
                '-frames:v', '1', str(video.with_suffix('.jpg'))], check=True)
info = {'id':video_id, 'title':'A quiet place to learn', 'channel':'The Creative Library',
        'channel_id':'UCfixture1', 'description':'An offline fixture for testing video, captions, and playback resume.',
        'duration':12, 'width':640, 'height':360, 'upload_date':'20260101',
        'playlist_id':'PLfixture1', 'playlist_title':'Things worth keeping'}
video.with_suffix('.info.json').write_text(json.dumps(info))
video.with_suffix('.en.vtt').write_text('WEBVTT\n\n00:00.000 --> 00:10.000\nA caption preserved with your video.\n')
main.import_video(video)
for index, title in enumerate(['Building a personal archive', 'Learning something new every day'], start=2):
    ident=f'fixture000{index}'
    target=main.MEDIA / 'videos' / ident
    target.mkdir(parents=True, exist_ok=True)
    path=target / f'{ident}.webm'
    shutil.copyfile(video,path)
    shutil.copyfile(video.with_suffix('.jpg'),path.with_suffix('.jpg'))
    path.with_suffix('.info.json').write_text(json.dumps({**info,'id':ident,'title':title,'channel':'Small Wonders','channel_id':'UCfixture2'}))
    main.import_video(path)
uvicorn.run(main.app, host='0.0.0.0', port=8000)
