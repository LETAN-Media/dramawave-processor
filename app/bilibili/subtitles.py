import http.cookiejar
import html
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

import requests

from app.bilibili.cookies import resolve_cookies_file, yt_dlp_cookie_args
from app.config import settings


logger = logging.getLogger('bilibili-subtitles')


TAG_RE = re.compile(r'<[^>]+>')


@dataclass
class SubtitleResult:
    path: Path
    language: str
    cue_count: int
    source: str


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update({'User-Agent': settings.bilibili_user_agent, 'Referer': 'https://www.bilibili.com/'})
    cookie_path = resolve_cookies_file()
    if cookie_path is not None:
        jar = http.cookiejar.MozillaCookieJar(str(cookie_path))
        try:
            jar.load(ignore_discard=True, ignore_expires=True)
            session.cookies.update(jar)
        except Exception:
            pass
    return session


def _is_chinese_language(code: str, name: str = '') -> bool:
    value = f'{code} {name}'.lower()
    tokens = ('zh', 'chinese', '中文', '简体', '繁體', '繁体', 'ai-zh')
    return any(token in value for token in tokens)


def _lang_rank(code: str) -> int:
    code_lower = code.lower()
    for index, preferred in enumerate(settings.subtitle_preferred_languages):
        if preferred.lower() in code_lower:
            return index
    return 999


def _clean_text(text: str) -> str:
    text = html.unescape(TAG_RE.sub('', text or ''))
    text = text.replace('\u200b', '').replace('\ufeff', '').strip()
    return re.sub(r'\s+', ' ', text)


def _srt_timestamp(seconds: float) -> str:
    ms = max(0, int(round(seconds * 1000)))
    hours, rem = divmod(ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, millis = divmod(rem, 1000)
    return f'{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}'


def cues_to_srt(cues: list[dict]) -> str:
    lines: list[str] = []
    index = 1
    for cue in cues:
        start = float(cue.get('from', cue.get('start', 0)) or 0)
        end = float(cue.get('to', cue.get('end', start + 1)) or start + 1)
        text = _clean_text(str(cue.get('content', cue.get('text', ''))))
        if not text:
            continue
        if end <= start:
            end = start + 0.5
        lines.extend([str(index), f'{_srt_timestamp(start)} --> {_srt_timestamp(end)}', text, ''])
        index += 1
    return '\n'.join(lines).strip() + '\n'


def _download_bilibili_json_subtitle(url: str) -> tuple[list[dict], str]:
    response = _session().get(urljoin('https:', url), timeout=settings.http_timeout_seconds)
    response.raise_for_status()
    payload = response.json()
    body = payload.get('body') or payload.get('data', {}).get('body') or []
    if not body:
        raise RuntimeError('Subtitle JSON contains no cues')
    return body, response.url


def _extract_via_player_api(bvid: str, cid: str, workdir: Path) -> SubtitleResult | None:
    response = _session().get(
        'https://api.bilibili.com/x/player/v2',
        params={'bvid': bvid, 'cid': cid},
        timeout=settings.http_timeout_seconds,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get('code') != 0:
        return None
    tracks = ((payload.get('data') or {}).get('subtitle') or {}).get('subtitles') or []
    chinese = [t for t in tracks if _is_chinese_language(str(t.get('lan', '')), str(t.get('lan_doc', '')))]
    chinese.sort(key=lambda t: _lang_rank(str(t.get('lan', ''))))
    for track in chinese:
        subtitle_url = track.get('subtitle_url')
        if not subtitle_url:
            continue
        cues, _ = _download_bilibili_json_subtitle(subtitle_url)
        # Normalize via strict SRT pipeline (split long, min duration, renumber).
        try:
            from app.bilibili.srt import normalize_segments_to_srt, write_srt

            segments = [
                {
                    'start': float(c.get('from', c.get('start', 0)) or 0),
                    'end': float(c.get('to', c.get('end', 0)) or 0),
                    'text': str(c.get('content', c.get('text', '')) or ''),
                }
                for c in cues
            ]
            srt = normalize_segments_to_srt(
                segments,
                min_duration_ms=settings.srt_min_duration_ms,
                max_chars_per_line=settings.srt_max_chars_per_line,
                max_lines=settings.srt_max_lines,
            )
        except Exception:
            srt = cues_to_srt(cues)
        if not srt.strip():
            continue
        path = workdir / 'source.zh.srt'
        try:
            cue_count = write_srt(path, srt)
        except Exception:
            path.write_text(srt, encoding='utf-8')
            cue_count = srt.count(' --> ')
        return SubtitleResult(path=path, language=str(track.get('lan') or 'zh'), cue_count=cue_count, source='BILIBILI_API')
    return None


def _parse_remote_subtitle(url: str) -> str:
    response = _session().get(urljoin('https:', url), timeout=settings.http_timeout_seconds)
    response.raise_for_status()
    content_type = (response.headers.get('content-type') or '').lower()
    text = response.text
    if 'json' in content_type or text.lstrip().startswith('{'):
        payload = response.json()
        body = payload.get('body') or payload.get('data', {}).get('body') or []
        return cues_to_srt(body)
    if 'WEBVTT' in text[:100].upper():
        return vtt_to_srt(text)
    if ' --> ' in text:
        return normalize_existing_srt(text)
    raise RuntimeError('Unsupported subtitle format')


def normalize_existing_srt(text: str) -> str:
    text = text.replace('\r\n', '\n').replace('\r', '\n').strip()
    return text + '\n'


def vtt_to_srt(text: str) -> str:
    blocks = re.split(r'\n\s*\n', text.replace('\r\n', '\n'))
    cues = []
    for block in blocks:
        lines = [line.strip() for line in block.split('\n') if line.strip()]
        timing_index = next((i for i, line in enumerate(lines) if '-->' in line), None)
        if timing_index is None:
            continue
        timing = lines[timing_index].split('-->')
        start = _parse_vtt_ts(timing[0].strip().split()[0])
        end = _parse_vtt_ts(timing[1].strip().split()[0])
        content = ' '.join(lines[timing_index + 1 :])
        cues.append({'from': start, 'to': end, 'content': content})
    return cues_to_srt(cues)


def _parse_vtt_ts(value: str) -> float:
    parts = value.replace(',', '.').split(':')
    if len(parts) == 3:
        h, m, s = parts
        return int(h) * 3600 + int(m) * 60 + float(s)
    if len(parts) == 2:
        m, s = parts
        return int(m) * 60 + float(s)
    return float(parts[0])


def _extract_via_ytdlp(url: str, workdir: Path) -> SubtitleResult | None:
    from yt_dlp import YoutubeDL

    options = {
        'quiet': True,
        'skip_download': True,
        'noplaylist': True,
        'user_agent': settings.bilibili_ytdlp_user_agent,
        'referer': 'https://www.bilibili.com/',
    }
    options.update(yt_dlp_cookie_args())
    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)

    tracks: list[tuple[int, str, str, str]] = []
    for source_name in ('subtitles', 'automatic_captions'):
        mapping = info.get(source_name) or {}
        for lang, formats in mapping.items():
            if not _is_chinese_language(lang):
                continue
            for fmt in formats or []:
                remote = fmt.get('url')
                if remote:
                    tracks.append((_lang_rank(lang), lang, remote, source_name.upper()))
    tracks.sort(key=lambda item: item[0])
    for _, lang, remote, source in tracks:
        try:
            srt = _parse_remote_subtitle(remote)
        except Exception:
            continue
        if not srt.strip():
            continue
        path = workdir / 'source.zh.srt'
        path.write_text(srt, encoding='utf-8')
        return SubtitleResult(path=path, language=lang, cue_count=srt.count(' --> '), source=f'YTDLP_{source}')
    return None


SRT_TIME_RE = re.compile(r'^\d{2}:\d{2}:\d{2},\d{3} --> \d{2}:\d{2}:\d{2},\d{3}$')


def _srt_to_seconds(value: str) -> float:
    h, m, rest = value.split(':')
    s, ms = rest.split(',')
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def validate_srt(path: Path) -> int:
    """Validate normalized SRT. Returns cue count. Raises RuntimeError on invalid."""
    try:
        text = path.read_text(encoding='utf-8')
    except UnicodeDecodeError as exc:
        raise RuntimeError(f'SRT is not valid UTF-8: {exc}') from exc
    blocks = [b.strip() for b in text.replace('\r\n', '\n').replace('\r', '\n').split('\n\n') if b.strip()]
    if not blocks:
        raise RuntimeError('SRT has no cues')
    expected_index = 1
    cue_count = 0
    empty_text_blocks = 0
    for block in blocks:
        lines = block.split('\n')
        if len(lines) < 3:
            raise RuntimeError(f'SRT block malformed (need index+timecode+text): {block[:200]!r}')
        try:
            index = int(lines[0].strip())
        except ValueError as exc:
            raise RuntimeError(f'SRT cue index invalid: {lines[0]!r}') from exc
        if index != expected_index:
            raise RuntimeError(f'SRT cue index not sequential: got {index}, expected {expected_index}')
        timecode = lines[1].strip()
        if not SRT_TIME_RE.match(timecode):
            raise RuntimeError(f'SRT timecode invalid: {timecode!r}')
        start_s, end_s = timecode.split(' --> ')
        start = _srt_to_seconds(start_s)
        end = _srt_to_seconds(end_s)
        if not end > start:
            raise RuntimeError(f'SRT start must be < end: {timecode!r}')
        body = '\n'.join(lines[2:]).strip()
        if not body:
            empty_text_blocks += 1
        expected_index += 1
        cue_count += 1
    if cue_count == 0:
        raise RuntimeError('SRT has no cues')
    if empty_text_blocks > cue_count // 2:
        raise RuntimeError(f'SRT has too many empty cues: {empty_text_blocks}/{cue_count}')
    return cue_count


def extract_chinese_subtitle(
    url: str,
    bvid: str,
    cid: str | None,
    workdir: Path,
    *,
    job_id: str | None = None,
) -> SubtitleResult:
    workdir.mkdir(parents=True, exist_ok=True)
    logger.info('subtitle discovery start job_id=%s stage=extracting_subtitles bvid=%s cid=%s', job_id, bvid, cid)
    if cid:
        try:
            result = _extract_via_player_api(bvid, cid, workdir)
            if result:
                validated = validate_srt(result.path)
                result.cue_count = validated
                logger.info(
                    'subtitle found job_id=%s stage=extracting_subtitles bvid=%s cid=%s source=%s lang=%s cues=%s',
                    job_id, bvid, cid, result.source, result.language, result.cue_count,
                )
                return result
        except Exception as exc:
            logger.warning('subtitle bilibili-api failed job_id=%s bvid=%s cid=%s error=%s', job_id, bvid, cid, str(exc)[:1000])
    try:
        result = _extract_via_ytdlp(url, workdir)
    except Exception as exc:
        logger.warning('subtitle ytdlp discovery failed job_id=%s bvid=%s cid=%s error=%s', job_id, bvid, cid, str(exc)[:1000])
        result = None
    if result:
        validated = validate_srt(result.path)
        result.cue_count = validated
        logger.info(
            'subtitle found job_id=%s stage=extracting_subtitles bvid=%s cid=%s source=%s lang=%s cues=%s',
            job_id, bvid, cid, result.source, result.language, result.cue_count,
        )
        return result
    logger.info('subtitle not found job_id=%s stage=extracting_subtitles bvid=%s cid=%s source=NONE', job_id, bvid, cid)
    raise RuntimeError('NO_SUBTITLE_FOUND: no Chinese subtitle track was found')
