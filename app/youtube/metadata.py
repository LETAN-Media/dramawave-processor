"""YouTube metadata: deterministic template + optional AI enhancement.

Episode number ALWAYS comes from the DB and is enforced in the final title —
AI output is never allowed to change it. No playback/M3U8/API URLs or VPS
paths are ever included. AI failure falls back to the template (upload never
fails just because metadata AI failed).
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger('youtube-metadata')

TITLE_LIMIT = 100
DESCRIPTION_LIMIT = 4900
TAGS_LIMIT_CHARS = 450
MAX_TAGS = 15


def template_metadata(series_title: str | None, episode_number: int,
                      episode_count: int | None, style: str | None = None,
                      series_description: str | None = None) -> dict:
    name = (series_title or 'Phim bộ').strip()
    ep_label = f'Tập {episode_number}'
    total = f'/{episode_count}' if episode_count else ''
    title = f'{name} - {ep_label}'
    lines = [f'{name} {ep_label}{total} - Thuyết minh tiếng Việt.']
    if series_description:
        lines.append(series_description.strip()[:800])
    lines.append('')
    lines.append(f'#phimbo #phimngan #{_slug(name)} #tap{episode_number} #thuyetminh #phimhay')
    description = '\n'.join(lines)[:DESCRIPTION_LIMIT]
    tags = _tags_for(name, style, episode_number)
    return {'title': title, 'description': description, 'tags': tags}


def _slug(name: str) -> str:
    import re
    import unicodedata

    norm = unicodedata.normalize('NFD', name).encode('ascii', 'ignore').decode()
    slug = re.sub(r'[^a-z0-9]+', '', norm.lower())[:24]
    return slug or 'phim'


def _tags_for(name: str, style: str | None, episode_number: int) -> list[str]:
    base = ['phim bộ', 'phim ngắn', 'thuyết minh', 'phim hay', f'tập {episode_number}', name.strip()]
    if (style or '').upper() == 'XIANXIA':
        base += ['tiên hiệp', 'xianxia']
    elif (style or '').upper() == 'NINETIES':
        base += ['phim xưa']
    out: list[str] = []
    for t in base:
        t = t.strip()
        if t and t not in out:
            out.append(t)
    return out[:MAX_TAGS]


def _enforce_episode_number(title: str, episode_number: int, fallback_title: str) -> str:
    """Keep AI title only if it carries the exact episode label; else template."""
    import re

    if len(title) > TITLE_LIMIT:
        title = title[:TITLE_LIMIT].rstrip()
    if re.search(rf'(?i)\btập\s*{episode_number}\b', title):
        return title
    return fallback_title


def _sanitize(meta: dict, episode_number: int, fallback: dict) -> dict:
    title = str(meta.get('title') or '').strip() or fallback['title']
    title = _enforce_episode_number(title, episode_number, fallback['title'])
    description = str(meta.get('description') or '').strip() or fallback['description']
    description = description[:DESCRIPTION_LIMIT]
    tags: list[str] = []
    raw_tags = meta.get('tags') or []
    if isinstance(raw_tags, list):
        for t in raw_tags:
            if isinstance(t, str) and t.strip() and len(tags) < MAX_TAGS:
                tags.append(t.strip()[:30])
    if not tags:
        tags = fallback['tags']
    total = sum(len(t) for t in tags)
    while len(tags) > 1 and total > TAGS_LIMIT_CHARS:
        total -= len(tags.pop())
    return {'title': title, 'description': description, 'tags': tags}


def ai_metadata(series_title: str | None, episode_number: int, episode_count: int | None,
                style: str | None = None, series_description: str | None = None,
                story_sample: str | None = None) -> dict:
    """AI-enhanced metadata with template fallback. Never raises for upload flow."""
    fallback = template_metadata(series_title, episode_number, episode_count, style,
                                 series_description)
    try:
        from app.translation.providers_toolnet import (
            OpenAICompatibleProvider,
            _post_chat_stream,
        )
        from app.config import settings
    except ImportError as exc:
        logger.warning('youtube metadata AI unavailable: %s', type(exc).__name__)
        return fallback
    provider = OpenAICompatibleProvider()
    total = f'/{episode_count}' if episode_count else ''
    messages = [
        {'role': 'system', 'content': (
            'Bạn viết metadata YouTube tiếng Việt cho phim bộ đã thuyết minh. '
            'Chỉ trả JSON hợp lệ duy nhất: {"title": "...", "description": "...", "tags": ["..."]}. '
            f'Tiêu đề BẮT BUỘC chứa đúng chuỗi "Tập {episode_number}", tối đa 100 ký tự, '
            'không clickbait vô căn cứ, không thêm tình tiết không có trong phim. '
            'Không đưa URL, link, đường dẫn file hay tên nền tảng nguồn vào mô tả. '
            'Tags: tối đa 15 chuỗi ngắn liên quan nội dung.')},
        {'role': 'user', 'content': json.dumps({
            'series_title': series_title, 'episode_number': episode_number,
            'episode_count': episode_count, 'style': style or 'AUTO',
            'series_description': (series_description or '')[:800],
            'story_sample': (story_sample or '')[:800],
        }, ensure_ascii=False)},
    ]
    last_err: Exception | None = None
    for model in provider.models:
        try:
            raw = _post_chat_stream(messages, model, settings.translation_timeout, 800)
            text = raw.strip()
            if text.startswith('```'):
                text = text.strip('`').strip()
                if text.lower().startswith('json'):
                    text = text[4:].strip()
            data = json.loads(text)
            if not isinstance(data, dict):
                raise ValueError('not an object')
            out = _sanitize(data, episode_number, fallback)
            logger.info('youtube metadata AI ok model=%s', model)
            return out
        except Exception as exc:  # noqa: BLE001 - next model, then template
            last_err = exc
            logger.warning('youtube metadata AI failed model=%s error=%s',
                           model, str(exc)[:200])
    logger.warning('youtube metadata using template fallback: %s',
                   str(last_err)[:200] if last_err else 'unknown')
    return fallback


def build_metadata(series_title: str | None, episode_number: int, episode_count: int | None,
                   style: str | None = None, series_description: str | None = None,
                   story_sample: str | None = None, mode: str = 'auto') -> dict:
    """mode 'auto' tries AI then template; anything else uses the template."""
    if (mode or 'auto').strip().lower() == 'auto':
        return ai_metadata(series_title, episode_number, episode_count, style,
                           series_description, story_sample)
    return template_metadata(series_title, episode_number, episode_count, style,
                             series_description)
