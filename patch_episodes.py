import re
with open('app/services/episodes.py', 'r') as f:
    content = f.read()

download_block = """                playback = provider.resolve_episode(match, quality=(j.requested_quality or None))
                t0 = time.monotonic()
                try:
                    result = provider.download_episode(playback, video_path)
                except SourceError as dl_exc:
                    # Retry downloading. With multi-provider, auto will find a fallback
                    logger.info('dramawave playback failed job=%s error=%s, re-resolving', job_id[:8], str(dl_exc))
                    playback = provider.resolve_episode(match, quality=(j.requested_quality or None))
                    result = provider.download_episode(playback, video_path)
                    
                with SessionLocal.begin() as db:
                    j = db.get(EpisodeJob, job_id)
                    j.original_path = str(result.path)
                    j.playback_type = result.playback_type
                    j.quality = result.quality
                    j.download_seconds = time.monotonic() - t0
                    j.progress = 45
                    if playback.metadata:
                        j.source_provider = playback.metadata.get('selected_provider')
                        j.source_provider_series_id = playback.metadata.get('provider_series_id')
                        j.source_provider_episode_id = playback.metadata.get('provider_episode_id')
                        j.source_type = playback.metadata.get('type')
                        j.source_quality = playback.metadata.get('quality')
"""

content = re.sub(r'                playback = provider\.resolve_episode\(.*?j\.progress = 45', download_block, content, flags=re.DOTALL)

with open('app/services/episodes.py', 'w') as f:
    f.write(content)
