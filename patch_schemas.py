with open('app/schemas.py', 'r') as f:
    content = f.read()

replacement = """    playback_type: str | None = None
    source_provider: str | None = None
    source_provider_series_id: str | None = None
    source_provider_episode_id: str | None = None
    source_type: str | None = None
    source_quality: str | None = None
    source_fallback_count: int | None = None
"""

content = content.replace("    playback_type: str | None = None", replacement)

with open('app/schemas.py', 'w') as f:
    f.write(content)
