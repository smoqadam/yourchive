"""Opt-in live smoke test, using temporary storage inside a Docker container."""
import json
from pathlib import Path
import tempfile
import threading
from app import main

with tempfile.TemporaryDirectory(prefix='yourchive-live-') as folder:
    main.DATA = Path(folder) / 'data'
    main.MEDIA = Path(folder) / 'media'
    main.initialize()
    job_id = main.create_job(main.JobInput(
        url='https://www.youtube.com/watch?v=jNQXAC9IVRw',
        options=main.Options(quality='720', languages='en.*,en'),
    ))['id']
    with main.db() as c:
        job = dict(c.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())

    def cancel():
        try:
            main.job_action(job_id, 'cancel')
        except main.HTTPException:
            pass

    timer = threading.Timer(120, cancel)
    timer.start()
    try:
        main.run_job(job)
    finally:
        timer.cancel()
    result = main.jobs()[0]
    print(json.dumps({key: result[key] for key in ('status', 'completed', 'detail', 'error')}, indent=2))
    library = main.library()
    print(f"Archived {len(library['videos'])} video(s) in temporary container storage")
    if library['videos']:
        detail = main.video(library['videos'][0]['id'])
        print('Files:', ', '.join(f['name'] for f in detail['files']))
    raise SystemExit(0 if result['status'] == 'completed' and library['videos'] else 1)
