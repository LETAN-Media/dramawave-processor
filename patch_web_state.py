import re
with open('app/web/routes.py', 'r') as f:
    content = f.read()

replacement = """def _series_episode_state(series_id: str) -> list[dict]:
    import json
    with SessionLocal() as db:
        eps = list(db.execute(select(Episode).where(Episode.series_id == series_id)
                              .order_by(Episode.episode_number)).scalars().all())
        jobs = {j.episode_id: j for j in db.execute(
            select(EpisodeJob).where(EpisodeJob.episode_id.in_([e.id for e in eps]))
        ).scalars().all()} if eps else {}
        from app.models import YouTubePublication

        yt = {}
        if jobs:
            for pub in db.execute(select(YouTubePublication).where(
                    YouTubePublication.job_id.in_([j.id for j in jobs.values()])
            )).scalars().all():
                yt.setdefault(pub.job_id, []).append(pub.upload_status)
        out = []
        for ep in eps:
            job = jobs.get(ep.id)
            states = yt.get(job.id, []) if job else []
            if any(s == 'published' for s in states):
                yt_state: str | None = 'published'
            elif any(s in ('uploading', 'processing', 'queued') for s in states):
                yt_state = 'uploading'
            elif any(s == 'failed' for s in states):
                yt_state = 'failed'
            else:
                yt_state = None
                
            try:
                meta = json.loads(ep.episode_metadata) if ep.episode_metadata else {}
            except Exception:
                meta = {}
            
            out.append({
                'id': ep.id,
                'number': ep.episode_number,
                'title': ep.title,
                'locked': ep.locked,
                'sources': meta.get('sources') or [],
                'status': ep.status,
                'progress': job.progress if job else 0,
                'job_id': job.id if job else None,
                'yt_status': yt_state,
            })
        return out
"""

content = re.sub(r'def _series_episode_state.*?return out\n', replacement, content, flags=re.DOTALL)

with open('app/web/routes.py', 'w') as f:
    f.write(content)
