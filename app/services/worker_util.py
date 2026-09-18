"""Shared worker primitives: heartbeat, lease renewal (dormant-pipeline friendly).

Kept generic so translation/TTS/voice services keep working independently.
"""

import logging
import socket
import threading
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timezone

from app.config import settings
from app.db import SessionLocal
from app.models import WorkerHeartbeat

logger = logging.getLogger('dramawave-worker')


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def update_heartbeat(worker_id: str, status: str, current_job_id: str | None = None) -> None:
    with SessionLocal.begin() as db:
        row = db.get(WorkerHeartbeat, worker_id)
        if row is None:
            row = WorkerHeartbeat(worker_id=worker_id, hostname=socket.gethostname())
            db.add(row)
        row.hostname = socket.gethostname()
        row.status = status
        row.current_job_id = current_job_id
        row.last_seen_at = utcnow()


def renew_job_lease(job_id: str, worker_id: str, active_states: tuple) -> None:
    """Renew a legacy Job lease while it stays in one of active_states."""
    from datetime import timedelta

    from app.db import SessionLocal as _SessionLocal
    from app.models import Job as _Job

    with _SessionLocal.begin() as db:
        job = db.get(_Job, job_id)
        if job and job.lease_owner == worker_id and job.status in active_states:
            job.lease_until = utcnow() + timedelta(seconds=settings.job_lease_seconds)


@contextmanager
def lease_renewer(job_id: str, worker_id: str, renew: Callable[[], None]):
    """Periodically run renew() (lease + heartbeat) until the block exits."""
    stop = threading.Event()

    def run() -> None:
        interval = max(5, min(settings.worker_heartbeat_seconds, settings.job_lease_seconds // 3))
        while not stop.wait(interval):
            try:
                renew()
            except Exception:
                pass

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=2)
