import re
with open('app/services/episodes.py', 'r') as f:
    content = f.read()

replacement = """def sync_episodes(series: Series, infos: list[EpisodeInfo]) -> list[Episode]:
    \"\"\"Upsert episodes (no duplicates on re-resolve). Returns rows in order.\"\"\"
    import json
    rows: list[Episode] = []
    with SessionLocal.begin() as db:
        for info in infos:
            row = db.execute(select(Episode).where(
                Episode.series_id == series.id,
                Episode.provider_episode_id == info.provider_episode_id)).scalars().first()
            if row is None:
                row = Episode(series_id=series.id, provider_episode_id=info.provider_episode_id,
                              episode_number=info.episode_number, title=info.title,
                              duration=info.duration, locked=info.locked,
                              status='locked' if info.locked else 'discovered',
                              episode_metadata=json.dumps(info.metadata) if info.metadata else None)
                db.add(row)
                db.flush()
            else:
                row.episode_number = info.episode_number
                row.title = info.title or row.title
                row.duration = info.duration if info.duration is not None else row.duration
                row.locked = info.locked
                row.episode_metadata = json.dumps(info.metadata) if info.metadata else row.episode_metadata
                if info.locked and row.status not in ('ready',):
"""

content = re.sub(r'def sync_episodes.*?if info\.locked and row\.status not in \(\'ready\',\):', replacement, content, flags=re.DOTALL)

with open('app/services/episodes.py', 'w') as f:
    f.write(content)
