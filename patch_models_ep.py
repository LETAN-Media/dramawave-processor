with open('app/models.py', 'r') as f:
    content = f.read()

replacement = """    title: Mapped[str | None] = mapped_column(Text)
    episode_metadata: Mapped[str | None] = mapped_column(Text)
"""

content = content.replace("    title: Mapped[str | None] = mapped_column(Text)\n", replacement)

with open('app/models.py', 'w') as f:
    f.write(content)
