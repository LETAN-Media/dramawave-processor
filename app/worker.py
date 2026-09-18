import logging
import signal
import sys
import threading
import time

from app.config import settings
from app.db import init_db
from app.services.episodes import claim_episode_job, process_episode_job
from app.services.worker_util import update_heartbeat
from app.youtube.service import claim_upload, run_upload


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s %(message)s',
)
logger = logging.getLogger('dramawave-worker')

running = True


def stop_handler(*_args) -> None:
    global running
    running = False


def worker_loop(slot: int) -> None:
    wid = f'{settings.worker_id}:{slot}' if settings.episode_concurrency > 1 else settings.worker_id
    while running:
        try:
            update_heartbeat(settings.worker_id, 'idle')
            job_id = claim_episode_job(wid)
            if not job_id:
                pump_youtube_uploads(wid)
                time.sleep(settings.worker_poll_seconds)
                continue

            update_heartbeat(settings.worker_id, 'busy', job_id)
            logger.info('processing episode job=%s', job_id)
            try:
                process_episode_job(job_id, wid)
                logger.info('episode job ready=%s', job_id)
            except Exception:
                logger.exception('episode job failed=%s', job_id)
            pump_youtube_uploads(wid)
        except Exception:
            logger.exception('worker loop error')
            time.sleep(settings.worker_poll_seconds)


def pump_youtube_uploads(wid: str) -> None:
    """Run queued YouTube publications sequentially (concurrency + interval enforced)."""
    while running:
        try:
            pub_id = claim_upload()
        except Exception:
            logger.exception('youtube claim error')
            return
        if not pub_id:
            return
        logger.info('uploading youtube pub=%s', pub_id[:8])
        try:
            run_upload(pub_id)
        except Exception:
            logger.exception('youtube upload error pub=%s', pub_id[:8])


def main() -> int:
    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)
    init_db()
    concurrency = max(1, settings.episode_concurrency)
    logger.info('worker started id=%s slots=%s', settings.worker_id, concurrency)
    threads = [threading.Thread(target=worker_loop, args=(i,), daemon=True)
               for i in range(concurrency)]
    for thread in threads:
        thread.start()
    try:
        while running:
            time.sleep(1)
    except KeyboardInterrupt:
        pass

    update_heartbeat(settings.worker_id, 'stopped')
    return 0


if __name__ == '__main__':
    sys.exit(main())
