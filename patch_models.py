with open('app/models.py', 'r') as f:
    content = f.read()

replacement = """    final_path: Mapped[str | None] = mapped_column(Text)

    source_provider: Mapped[str | None] = mapped_column(String(40))
    source_provider_series_id: Mapped[str | None] = mapped_column(String(120))
    source_provider_episode_id: Mapped[str | None] = mapped_column(String(120))
    source_type: Mapped[str | None] = mapped_column(String(20))
    source_quality: Mapped[str | None] = mapped_column(String(20))
    source_fallback_count: Mapped[int] = mapped_column(Integer, default=0)
"""

content = content.replace("    final_path: Mapped[str | None] = mapped_column(Text)", replacement)

with open('app/models.py', 'w') as f:
    f.write(content)
