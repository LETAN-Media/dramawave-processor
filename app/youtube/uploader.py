"""Resumable YouTube upload: chunked from disk, bounded retries, quota-aware.

Never reads the whole MP4 into RAM (MediaFileUpload streams from disk).
Retriable: network timeouts + HTTP 500/502/503/504 with exponential backoff.
Not retried: quota errors, auth errors, client errors.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from app.config import settings
from app.youtube.client import YouTubeApiError, is_quota_error, is_retryable_http

logger = logging.getLogger('youtube-upload')

CHUNK_SIZE = 8 * 1024 * 1024
MAX_ATTEMPTS = 5
PROCESS_POLL_ROUNDS = 30
PROCESS_POLL_INTERVAL = 10


class YouTubeUploadError(YouTubeApiError):
    pass


def _sleep_backoff(attempt: int) -> None:
    time.sleep(min(2 ** attempt, 60) + 1)


def resumable_insert(service, video_path: Path, title: str, description: str,
                     tags: list[str], privacy: str,
                     on_progress=None) -> str:
    """Upload and return the YouTube video id. Raises YouTubeUploadError."""
    from googleapiclient.http import MediaFileUpload

    size = video_path.stat().st_size
    if size <= 0:
        raise YouTubeUploadError('YOUTUBE_UPLOAD_FAILED', 'empty video file')
    body = {
        'snippet': {'title': title, 'description': description, 'tags': tags,
                    'categoryId': '24'},
        'status': {'privacyStatus': privacy, 'selfDeclaredMadeForKids': False},
    }
    media = MediaFileUpload(str(video_path), mimetype='video/mp4',
                            resumable=True, chunksize=CHUNK_SIZE)
    last_err: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            request = service.videos().insert(part='snippet,status', body=body, media_body=media)
            response = None
            while response is None:
                try:
                    _, response = request.next_chunk()
                except Exception as chunk_exc:
                    if is_quota_error(chunk_exc):
                        raise YouTubeUploadError('YOUTUBE_QUOTA_EXCEEDED', 'quota exceeded') from chunk_exc
                    raise
                if on_progress and size:
                    try:
                        sent = request.resumable_progress or 0
                        on_progress(min(99, int(sent * 100 / size)))
                    except Exception:
                        pass
            video_id = ((response or {}).get('id') or '').strip()
            if not video_id:
                raise YouTubeUploadError('YOUTUBE_UPLOAD_FAILED', 'no video id returned')
            logger.info('youtube upload ok video_id=%s size=%s', video_id[:8] + '…', size)
            return video_id
        except YouTubeUploadError:
            raise
        except Exception as exc:
            if is_quota_error(exc):
                raise YouTubeUploadError('YOUTUBE_QUOTA_EXCEEDED', 'quota exceeded') from exc
            last_err = exc
            retryable = is_retryable_http(exc) or isinstance(
                exc, (TimeoutError, ConnectionError, OSError))
            logger.warning('youtube upload attempt=%s retryable=%s error=%s',
                           attempt + 1, retryable, type(exc).__name__)
            if not retryable or attempt + 1 >= MAX_ATTEMPTS:
                break
            _sleep_backoff(attempt)
    raise YouTubeUploadError('YOUTUBE_UPLOAD_FAILED',
                             f'{type(last_err).__name__}' if last_err else 'unknown')


def wait_processing(service, video_id: str) -> str:
    """Poll until YouTube finishes processing. Returns 'processed'."""
    last = 'uploaded'
    for _ in range(PROCESS_POLL_ROUNDS):
        try:
            resp = service.videos().list(part='status', id=video_id).execute()
        except Exception as exc:
            if is_quota_error(exc):
                raise YouTubeUploadError('YOUTUBE_QUOTA_EXCEEDED', 'quota exceeded') from exc
            if is_retryable_http(exc):
                time.sleep(PROCESS_POLL_INTERVAL)
                continue
            raise YouTubeUploadError('YOUTUBE_UPLOAD_FAILED', type(exc).__name__) from exc
        items = (resp or {}).get('items') or []
        status = ((items[0] if items else {}).get('status') or {})
        last = str(status.get('uploadStatus') or last)
        if last == 'processed':
            return last
        if last in ('failed', 'rejected'):
            raise YouTubeUploadError('YOUTUBE_VIDEO_PROCESSING_FAILED', last)
        time.sleep(PROCESS_POLL_INTERVAL)
    logger.warning('youtube processing still pending video_id=%s state=%s', video_id[:8] + '…', last)
    return last


def upload_video(service, video_path: Path, title: str, description: str,
                 tags: list[str], privacy: str, on_progress=None) -> tuple[str, str]:
    """Full upload + processing wait. Returns (video_id, processing_status)."""
    video_id = resumable_insert(service, video_path, title, description, tags,
                                privacy, on_progress=on_progress)
    processing = wait_processing(service, video_id)
    return video_id, processing
