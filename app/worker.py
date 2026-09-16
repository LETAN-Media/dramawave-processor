import logging
import signal
import sys
import time

from app.config import settings
from app.db import init_db
from app.services.jobs import claim_next_job, process_job, update_heartbeat


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s %(message)s',
)
logger = logging.getLogger('bilibili-worker')

running = True


def stop_handler(*_args) -> None:
    global running
    running = False


def main() -> int:
    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)
    init_db()
    logger.info('worker started id=%s', settings.worker_id)

    while running:
        try:
            update_heartbeat(settings.worker_id, 'idle')
            job_id = claim_next_job(settings.worker_id)
            if not job_id:
                time.sleep(settings.worker_poll_seconds)
                continue

            update_heartbeat(settings.worker_id, 'busy', job_id)
            logger.info('processing job=%s', job_id)
            try:
                process_job(job_id, settings.worker_id)
                logger.info('job ready=%s', job_id)
            except Exception:
                logger.exception('job failed=%s', job_id)
        except Exception:
            logger.exception('worker loop error')
            time.sleep(settings.worker_poll_seconds)

    update_heartbeat(settings.worker_id, 'stopped')
    return 0


if __name__ == '__main__':
    sys.exit(main())
