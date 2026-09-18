import re
with open('app/web/routes.py', 'r') as f:
    content = f.read()

replacement = """def _import_series(pid: str) -> tuple[Series, list[Episode]]:
    \"\"\"Fetch live resolver data and sync into local DB. Raises HTTPException.\"\"\"
    from app.sources.base import SeriesInfo

    try:
        data = api.get_series(pid)
    except api.DramaApiError as exc:
        raise HTTPException(status_code=502, detail=_friendly_api_error(exc)) from exc
    info = SeriesInfo(
        provider='multi', provider_series_id=str(data.get('canonical_series_id') or pid),
        title=str(data.get('canonical_title') or pid),
        description=str(data.get('description') or ''),
        cover_url=str(data.get('cover_url') or ''),
        episode_count=data.get('episode_count'),
        source_url=f'cw:{pid}' if not pid.startswith('cw:') else pid,
        metadata=dict(data.get('metadata') or {}))
    series = episode_service.get_or_create_series(info, info.source_url)
    try:
        raw = api.list_episodes(pid)
    except api.DramaApiError as exc:
        raise HTTPException(status_code=502, detail=_friendly_api_error(exc)) from exc
    from app.sources.base import EpisodeInfo

    infos = []
    for e in raw.get('episodes') or []:
        sources = e.get('sources') or []
        free = any(s.get('status') == 'free' for s in sources)
        infos.append(EpisodeInfo(
            provider_episode_id=str(e.get('episode_number') or 0),
            episode_number=int(e.get('episode_number') or 0),
            title=f"Episode {e.get('episode_number') or 0}",
            duration=None,
            locked=not free,
            source_url=info.source_url,
            metadata={'sources': sources}))
    rows = episode_service.sync_series_episodes(series, infos)
    return series, rows
"""

content = re.sub(r'def _import_series.*?return series, rows\n', replacement, content, flags=re.DOTALL)

with open('app/web/routes.py', 'w') as f:
    f.write(content)
