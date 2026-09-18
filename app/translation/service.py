"""Translation orchestration: batch + context + concurrency + checkpoints + validation."""

from __future__ import annotations

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.media.srt import TIMECODE_RE, format_timestamp, parse_timestamp, validate_srt_text
from app.config import settings
from app.translation.base import TransContext, TransCue

logger = logging.getLogger('translation-service')


def parse_srt_cues(path: Path) -> list[dict]:
    """Parse strict SRT into [{id, start_ms, end_ms, timecode, text}].

    `timecode` is the raw `HH:MM:SS,mmm --> HH:MM:SS,mmm` line, kept for
    byte-for-byte comparison. The translation model never sees timestamps;
    the backend owns them end-to-end.
    """
    text = path.read_bytes().decode('utf-8').lstrip('\ufeff').replace('\r\n', '\n').replace('\r', '\n')
    blocks = [b for b in text.split('\n\n') if b.strip()]
    cues: list[dict] = []
    for block in blocks:
        lines = block.split('\n')
        idx = int(lines[0].strip())
        timecode = lines[1].strip()
        start_s, end_s = [p.strip() for p in timecode.split('-->')]
        cues.append({
            'id': idx,
            'start_ms': int(round(parse_timestamp(start_s) * 1000)),
            'end_ms': int(round(parse_timestamp(end_s) * 1000)),
            'timecode': timecode,
            'text': '\n'.join(lines[2:]).strip(),
        })
    cues.sort(key=lambda c: c['id'])
    return cues


def _glossary(workdir: Path | None = None) -> dict[str, dict[str, str]]:
    """Merge env glossary with per-job workdir/glossary.json (job wins)."""
    result: dict[str, dict[str, str]] = {'characters': {}, 'relationships': {}, 'pronouns': {}}
    raw = (settings.translation_glossary or '').strip()
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                for section in result:
                    section_data = data.get(section, {})
                    if isinstance(section_data, dict):
                        result[section].update({str(k): str(v if not isinstance(v, dict) else v.get('vi', v))
                                                          for k, v in section_data.items()})
        except json.JSONDecodeError:
            pass
    if workdir is not None:
        gp = workdir / 'glossary.json'
        if gp.exists():
            try:
                data = json.loads(gp.read_text(encoding='utf-8'))
                if isinstance(data, dict):
                    for section in result:
                        section_data = data.get(section, {})
                        if isinstance(section_data, dict):
                            for k, v in section_data.items():
                                result[section][str(k)] = str(v.get('vi', v)) if isinstance(v, dict) else str(v)
            except (json.JSONDecodeError, OSError):
                pass
    return result


def _chunk_path(workdir: Path, index: int) -> Path:
    d = workdir / 'vi_chunks'
    d.mkdir(parents=True, exist_ok=True)
    return d / f'chunk_{index:04d}.json'


def count_checkpoint_models(workdir: Path, total_batches: int, primary_model: str) -> tuple[int, int]:
    """Recount per-model batch stats from checkpoints (resume path)."""
    primary_batches = 0
    fallback_batches = 0
    for index in range(total_batches):
        cp = _chunk_path(workdir, index)
        if not cp.exists():
            continue
        try:
            saved = json.loads(cp.read_text(encoding='utf-8'))
            model = saved.get('_model', primary_model) if isinstance(saved, dict) and 'cues' in saved else primary_model
            if model == primary_model:
                primary_batches += 1
            else:
                fallback_batches += 1
        except (json.JSONDecodeError, OSError, AttributeError):
            pass
    return primary_batches, fallback_batches


def translate_cues(
    cues: list[dict],
    workdir: Path,
    *,
    job_id: str | None = None,
    provider=None,
    on_chunk=None,
    style: str = 'AUTO',
    source_language: str = 'zh',
) -> tuple[dict[int, str], dict]:
    """Translate all cues with batching + resume.

    Returns (mapping, info) where info holds batches/seconds/primary_model/
    fallback_model/primary_batches/fallback_batches/retries. Each batch
    independently tries primary first, then fallbacks.
    """
    from app.translation.providers_toolnet import OpenAICompatibleProvider

    provider = provider or OpenAICompatibleProvider()
    batch_size = max(1, settings.translation_batch_size)
    batches = [cues[i:i + batch_size] for i in range(0, len(cues), batch_size)]
    total_batches = len(batches)
    mapping: dict[int, str] = {}
    by_id = {c['id']: c for c in cues}
    glossary = _glossary(workdir)
    started = time.monotonic()
    primary_model = getattr(provider, 'primary_model', provider.name)
    fallback_models = getattr(provider, 'fallback_models', [])
    fallback_model = fallback_models[0] if fallback_models else ''
    primary_batches = 0
    fallback_batches = 0

    # Load checkpoints for resume.
    pending: list[tuple[int, list[dict]]] = []
    for index, batch in enumerate(batches):
        cp = _chunk_path(workdir, index)
        if cp.exists():
            try:
                saved = json.loads(cp.read_text(encoding='utf-8'))
                # New format: {"_model": ..., "cues": {...}}; legacy: plain {id: text}.
                if isinstance(saved, dict) and 'cues' in saved and isinstance(saved['cues'], dict):
                    saved_map, saved_model = saved['cues'], str(saved.get('_model', primary_model))
                else:
                    saved_map, saved_model = saved, primary_model
                ok = all(str(c['id']) in saved_map or c['id'] in saved_map for c in batch)
                if ok:
                    for c in batch:
                        mapping[c['id']] = saved_map.get(str(c['id']), saved_map.get(c['id']))
                    if saved_model == primary_model:
                        primary_batches += 1
                    else:
                        fallback_batches += 1
                    continue
            except (json.JSONDecodeError, OSError, AttributeError):
                pass
        pending.append((index, batch))

    logger.info('translate start job_id=%s cues=%s batches=%s pending=%s provider=%s',
                job_id, len(cues), total_batches, len(pending), provider.name)

    def _run(item: tuple[int, list[dict]]) -> tuple[int, dict[int, str], str]:
        index, batch = item
        # Context: previous {id, zh, vi} cues (up to N) before this batch.
        # Never retranslated — context only.
        first_id = batch[0]['id']
        prev: list[tuple] = []
        for i in range(max(1, first_id - settings.translation_context_cues), first_id):
            src = by_id.get(i)
            vi = mapping.get(i, '')
            if src is not None and vi:
                prev.append((TransCue(cue_id=i, start_ms=src['start_ms'], end_ms=src['end_ms'], text=src['text']), vi))
        ctx = TransContext(previous=prev, glossary=glossary, style=style,
                           source_language=source_language)
        transcues = [TransCue(cue_id=c['id'], start_ms=c['start_ms'], end_ms=c['end_ms'], text=c['text']) for c in batch]
        if hasattr(provider, 'translate_batch_with_model'):
            result, used = provider.translate_batch_with_model(transcues, ctx)
        else:
            result, used = provider.translate_batch(transcues, ctx), primary_model
        _chunk_path(workdir, index).write_text(
            json.dumps({'_model': used, 'cues': {str(k): v for k, v in result.items()}}, ensure_ascii=False),
            encoding='utf-8')
        if on_chunk:
            on_chunk(index, total_batches)
        return index, result, used

    # Batches must commit in order for coherent context; run sequentially in
    # small concurrent windows would scramble context, so translate windows of
    # CONCURRENCY batches sharing the same snapshot context, then commit ordered.
    conc = max(1, settings.translation_concurrency)
    model_used = primary_model
    with ThreadPoolExecutor(max_workers=conc) as pool:
        for window_start in range(0, len(pending), conc):
            window = pending[window_start:window_start + conc]
            # Snapshot context per batch from currently committed mapping.
            results = list(pool.map(_run, window))
            for _, result, used in sorted(results):
                mapping.update(result)
                if used == primary_model:
                    primary_batches += 1
                else:
                    fallback_batches += 1
                model_used = used

    elapsed = time.monotonic() - started
    missing = [c['id'] for c in cues if not mapping.get(c['id'], '').strip()]
    if missing:
        raise RuntimeError(f'TRANSLATION_INCOMPLETE: {len(missing)} cues missing e.g. {missing[:10]}')
    info = {
        'batches': total_batches,
        'seconds': elapsed,
        'primary_model': primary_model,
        'fallback_model': fallback_model,
        'primary_batches': primary_batches,
        'fallback_batches': fallback_batches,
        'failed_batches': 0,
        'retries': int(getattr(provider, 'retry_count', 0) or 0),
    }
    logger.info('translate done job_id=%s cues=%s batches=%s primary=%s fallback=%s retries=%s seconds=%.1f',
                job_id, len(mapping), total_batches, primary_batches, fallback_batches, info['retries'], elapsed)
    return mapping, info


def build_vi_srt(cues: list[dict], mapping: dict[int, str], max_line_chars: int = 40) -> str:
    """Build strict VI SRT preserving exact index + timestamps.

    AI changes text ONLY. Timestamps are copied byte-for-byte from the
    parsed Chinese cues (never reformatted), so output timecodes are
    identical to source.zh.srt by construction. Text keeps at most 2 lines.
    """
    lines: list[str] = []
    for cue in sorted(cues, key=lambda c: c['id']):
        text = mapping[cue['id']]
        text = _clean_vi_text(text)
        if not text:
            raise RuntimeError(f'TRANSLATION_EMPTY: cue {cue["id"]}')
        body = wrap_vi_lines(text, max_line_chars)
        timecode = cue.get('timecode') or (
            f'{format_timestamp(cue["start_ms"] / 1000)} --> {format_timestamp(cue["end_ms"] / 1000)}')
        lines.append(f'{cue["id"]}\n{timecode}\n{body}')
    return '\n\n'.join(lines) + '\n'


def _clean_vi_text(text: str) -> str:
    parts = [re.sub(r'\s+', ' ', p.replace('\u200b', '').replace('\ufeff', '').strip()).strip()
             for p in str(text or '').replace('\r\n', '\n').split('\n')]
    parts = [p for p in parts if p]
    if len(parts) <= 2:
        return '\n'.join(parts)
    return ' '.join(parts)  # wrap_vi_lines will re-break to 2 lines


def wrap_vi_lines(text: str, max_chars: int = 40) -> str:
    """Wrap to max 2 lines at a natural break. Content unchanged, only breaks added."""
    lines = [ln.strip() for ln in text.split('\n') if ln.strip()]
    if len(lines) >= 2:
        # Keep author's breaks, re-balance only if needed.
        if len(lines) == 2:
            return '\n'.join(lines)
        merged = ' '.join(lines)
    else:
        merged = lines[0] if lines else ''
    if not merged or len(merged) <= max_chars:
        return merged
    # Prefer breaking after comma/punct near the middle.
    best = -1
    for idx, ch in enumerate(merged):
        if ch in ',;:….!?…' and 0 < idx < len(merged) - 1:
            if best < 0 or abs(idx + 1 - len(merged) / 2) < abs(best + 1 - len(merged) / 2):
                best = idx
    if best > 0:
        return f'{merged[:best + 1].strip()}\n{merged[best + 1:].strip()}'
    # Fall back to nearest space to the middle.
    spaces = [i for i, ch in enumerate(merged) if ch == ' ']
    if spaces:
        mid = min(spaces, key=lambda i: abs(i - len(merged) / 2))
        if 0 < mid < len(merged) - 1:
            return f'{merged[:mid].strip()}\n{merged[mid + 1:].strip()}'
    hard = max_chars
    return f'{merged[:hard].strip()}\n{merged[hard:].strip()}'


def compute_cps(text_vi: str, duration_ms: int) -> float:
    """CPS = visible chars (no newlines) / seconds."""
    visible = text_vi.replace('\n', '')
    secs = max(0.001, duration_ms / 1000)
    return len(visible) / secs


def validate_vi_against_zh(zh_path: Path, vi_text: str) -> int:
    """Timestamp-immutable validation. Returns cue count.

    The model only ever receives id+text; the backend merges translations
    back into ORIGINAL timestamps. Any timecode difference (byte-for-byte)
    fails the job with TRANSLATION_TIMECODE_MISMATCH — TTS never runs.
    """
    zh_cues = parse_srt_cues(zh_path)
    vi_count = validate_srt_text(vi_text)
    if vi_count != len(zh_cues):
        raise RuntimeError(f'TRANSLATION_COUNT_MISMATCH: zh={len(zh_cues)} vi={vi_count}')
    vi_blocks = [b for b in vi_text.replace('\r\n', '\n').split('\n\n') if b.strip()]
    for zh, block in zip(zh_cues, vi_blocks):
        lines = block.split('\n')
        if int(lines[0].strip()) != zh['id']:
            raise RuntimeError(f'TRANSLATION_INDEX_MISMATCH at cue {zh["id"]}')
        if lines[1].strip() != zh['timecode']:
            raise RuntimeError(
                f'TRANSLATION_TIMECODE_MISMATCH at cue {zh["id"]}: '
                f'expected {zh["timecode"]!r} got {lines[1].strip()!r}')
        if not '\n'.join(lines[2:]).strip():
            raise RuntimeError(f'TRANSLATION_EMPTY: cue {zh["id"]}')
    # No Chinese chars accidentally left.
    if re.search(r'[\u4e00-\u9fff]', vi_text):
        logger.warning('translated SRT still contains CJK characters')
    return vi_count


def estimate_speech_ms(text: str) -> int:
    """Heuristic Vietnamese speech duration for pre-checks."""
    cps = max(1.0, settings.vi_chars_per_sec)
    return int(len(text.strip()) / cps * 1000)
