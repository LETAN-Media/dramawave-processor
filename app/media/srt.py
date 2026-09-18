"""Strict Chinese SRT normalization + validation.

Output must look exactly like:

1
00:00:00,200 --> 00:00:01,666
那年父母离世

2
00:00:01,666 --> 00:00:03,000
公司濒临破产
"""

import re
from pathlib import Path

TIMECODE_RE = re.compile(r'^\d{2}:\d{2}:\d{2},\d{3} --> \d{2}:\d{2}:\d{2},\d{3}$')

# Preferred punctuation split for Chinese.
SPLIT_PUNCT = ('，', '。', '！', '？', '；', '…', ',', '.', '!', '?', ';')

DEFAULT_MIN_DURATION_S = 0.3
DEFAULT_MAX_CHARS_PER_LINE = 24
DEFAULT_MAX_LINES = 2


def format_timestamp(seconds: float) -> str:
    ms_total = max(0, int(round(float(seconds) * 1000)))
    hours, rem = divmod(ms_total, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, millis = divmod(rem, 1000)
    return f'{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}'


def parse_timestamp(value: str) -> float:
    h, m, rest = value.split(':')
    s, ms = rest.split(',')
    if len(ms) != 3 or not ms.isdigit():
        raise ValueError(f'milliseconds must be exactly 3 digits: {value!r}')
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def _clean_text(text: str) -> str:
    t = (text or '').replace('\u200b', '').replace('\ufeff', '').strip()
    t = re.sub(r'\s+', ' ', t).strip()
    # Never emit speaker/confidence prefixes.
    return t


def _split_text_on_punct(text: str) -> list[str]:
    """Split Chinese text keeping punctuation with preceding chunk."""
    parts: list[str] = []
    buf = ''
    for ch in text:
        buf += ch
        if ch in SPLIT_PUNCT:
            if buf.strip():
                parts.append(buf.strip())
            buf = ''
    if buf.strip():
        parts.append(buf.strip())
    return [p for p in parts if p] or ([text.strip()] if text.strip() else [])


def _hard_split_by_chars(text: str, max_total: int) -> list[str]:
    return [text[i:i + max_total] for i in range(0, len(text), max_total)]


def _distribute_time(start: float, end: float, chunks: list[str]) -> list[tuple[float, float, str]]:
    total = sum(len(c) for c in chunks) or 1
    dur = max(0.0, end - start)
    out = []
    cursor = start
    for i, ch in enumerate(chunks):
        frac = len(ch) / total
        c_dur = dur * frac
        c_end = cursor + c_dur if i < len(chunks) - 1 else end
        out.append((cursor, c_end, ch))
        cursor = c_end
    return out


def _segment_to_raw_cues(seg: dict, max_total: int) -> list[tuple[float, float, str]]:
    """Convert one whisper segment to raw (start, end, text) cues.

    Uses word timestamps when available for tighter timing.
    """
    try:
        start = float(seg.get('start', 0) or 0)
    except (TypeError, ValueError):
        start = 0.0
    try:
        end = float(seg.get('end', start + 1) or start + 1)
    except (TypeError, ValueError):
        end = start + 1.0
    if end <= start:
        end = start + 0.5
    text = _clean_text(str(seg.get('text', '') or ''))
    if not text:
        return []
    words = seg.get('words') or seg.get('word_timestamps') or []
    # Normalize word list: accept dicts with start/end + word/text.
    norm_words: list[tuple[float, float, str]] = []
    if isinstance(words, list):
        for w in words:
            if not isinstance(w, dict):
                continue
            wt = str(w.get('word', w.get('text', '')) or '').strip()
            if not wt:
                continue
            try:
                ws = float(w.get('start', start))
                we = float(w.get('end', ws + 0.2))
            except (TypeError, ValueError):
                continue
            if we <= ws:
                we = ws + 0.2
            norm_words.append((ws, we, wt))

    if norm_words:
        # Group words into cues: flush on punctuation or max_total overflow.
        cues: list[tuple[float, float, str]] = []
        buf_words: list[tuple[float, float, str]] = []
        buf_len = 0

        def _flush():
            nonlocal buf_words, buf_len
            if not buf_words:
                return
            cs = buf_words[0][0]
            ce = buf_words[-1][1]
            # Chinese: concatenate without spaces.
            joined = ''.join(w for _, _, w in buf_words).strip()
            joined = _clean_text(joined)
            if joined:
                cues.append((cs, ce, joined))
            buf_words = []
            buf_len = 0

        for ws, we, wt in norm_words:
            # If adding this word exceeds max, flush first.
            if buf_words and buf_len + len(wt) > max_total:
                _flush()
            buf_words.append((ws, we, wt))
            buf_len += len(wt)
            # Flush after sentence punctuation.
            if wt and wt[-1] in SPLIT_PUNCT:
                _flush()
        _flush()
        # Fallback: if word grouping produced nothing, use text split.
        if cues:
            # Further split any cue still too long (should be rare).
            final: list[tuple[float, float, str]] = []
            for cs, ce, ct in cues:
                if len(ct) <= max_total:
                    final.append((cs, ce, ct))
                else:
                    chunks = _hard_split_by_chars(ct, max_total)
                    final.extend(_distribute_time(cs, ce, chunks))
            return final
        # fall through to text-based splitting

    # Text-based splitting (no usable word timestamps).
    chunks = _split_text_on_punct(text)
    # Enforce max_total per cue.
    split_chunks: list[str] = []
    for ch in chunks:
        if len(ch) <= max_total:
            split_chunks.append(ch)
        else:
            split_chunks.extend(_hard_split_by_chars(ch, max_total))
    if not split_chunks:
        return []
    if len(split_chunks) == 1:
        return [(start, end, split_chunks[0])]
    return _distribute_time(start, end, split_chunks)


def _merge_short_cues(
    cues: list[tuple[float, float, str]], min_dur: float, max_total: int
) -> list[tuple[float, float, str]]:
    if not cues:
        return cues
    merged = [c for c in cues]
    i = 0
    while i < len(merged):
        s, e, t = merged[i]
        dur = e - s
        if dur >= min_dur or len(merged) == 1:
            i += 1
            continue
        if i < len(merged) - 1:
            ns, ne, nt = merged[i + 1]
            combined = (t + nt).strip()
            # If combined too long, just extend duration instead of merging.
            if len(combined) > max_total:
                e2 = min(ns, s + min_dur) if ns > s else s + min_dur
                if e2 <= s:
                    e2 = s + min_dur
                merged[i] = (s, e2, t)
                i += 1
            else:
                merged[i + 1] = (s, ne, combined)
                merged.pop(i)
                # don't increment: re-check merged cue at same index
        else:
            # Last cue: merge backward.
            ps, pe, pt = merged[i - 1]
            combined = (pt + t).strip()
            if len(combined) > max_total:
                merged[i] = (s, s + min_dur, t)
                i += 1
            else:
                merged[i - 1] = (ps, e, combined)
                merged.pop(i)
    return merged


def _fix_overlaps(cues: list[tuple[float, float, str]], min_dur: float) -> list[tuple[float, float, str]]:
    """Sort by start; enforce start<end and next.start >= prev.start (prefer >= prev.end)."""
    cues = sorted(cues, key=lambda c: (c[0], c[1]))
    fixed: list[tuple[float, float, str]] = []
    for s, e, t in cues:
        s = max(0.0, float(s))
        e = float(e)
        if not e > s:
            e = s + min_dur
        if fixed:
            ps, pe, pt = fixed[-1]
            if s < ps:
                s = ps  # next.start >= previous.start
                if not e > s:
                    e = s + min_dur
            if s < pe:
                # Clamp to previous end (contiguous timing allowed: == is valid).
                s = pe
                if not e > s:
                    e = s + min_dur
        fixed.append((s, e, t))
    return fixed


def _wrap_to_lines(text: str, max_chars: int, max_lines: int = 2) -> str:
    text = text.strip()
    if len(text) <= max_chars or max_lines <= 1:
        return text
    # Try punctuation split near middle for 2 balanced lines.
    best = -1
    for idx, ch in enumerate(text):
        if ch in SPLIT_PUNCT and idx > 0:
            # Prefer split close to half.
            if abs(idx + 1 - len(text) / 2) < abs(best + 1 - len(text) / 2) if best >= 0 else True:
                best = idx
    if 0 < best < len(text) - 1:
        line1, line2 = text[:best + 1].strip(), text[best + 1:].strip()
        if len(line1) <= max_chars and len(line2) <= max_chars:
            return f'{line1}\n{line2}'
    # Hard split.
    line1, line2 = text[:max_chars].strip(), text[max_chars:].strip()
    if not line2:
        return line1
    if len(line2) > max_chars:
        # Should not happen if upstream splitting enforced max_total,
        # but truncate defensively into 2 lines.
        line2 = line2[:max_chars].strip()
    return f'{line1}\n{line2}'


def normalize_segments_to_srt(
    segments: list[dict],
    *,
    min_duration_ms: int = 300,
    max_chars_per_line: int = 24,
    max_lines: int = 2,
) -> str:
    """Whisper segments -> strict SRT. Never dump raw segments directly."""
    min_dur = max(0.1, (min_duration_ms or 300) / 1000.0)
    max_total = max(8, max_chars_per_line * max_lines)
    raw: list[tuple[float, float, str]] = []
    for seg in segments or []:
        if not isinstance(seg, dict):
            continue
        raw.extend(_segment_to_raw_cues(seg, max_total))
    # Drop empties.
    raw = [(s, e, _clean_text(t)) for s, e, t in raw if _clean_text(t)]
    if not raw:
        return ''
    raw = _merge_short_cues(raw, min_dur, max_total)
    raw = _fix_overlaps(raw, min_dur)
    # Final: wrap, renumber, strict formatting with LF.
    lines: list[str] = []
    for idx, (s, e, t) in enumerate(raw, start=1):
        body = _wrap_to_lines(t, max_chars_per_line, max_lines)
        lines.append(f'{idx}\n{format_timestamp(s)} --> {format_timestamp(e)}\n{body}')
    return '\n\n'.join(lines) + '\n'


def validate_srt_text(text: str) -> int:
    """Validate strict SRT text. Returns cue count or raises INVALID_SRT_FORMAT."""
    try:
        raw = text.replace('\r\n', '\n').replace('\r', '\n')
    except Exception as exc:
        raise RuntimeError(f'INVALID_SRT_FORMAT: unreadable text: {exc}') from exc
    if '\n\n\n' in raw:
        raise RuntimeError('INVALID_SRT_FORMAT: multiple blank lines between cues')
    blocks = [b for b in raw.split('\n\n') if b.strip() != '']
    if not blocks:
        raise RuntimeError('INVALID_SRT_FORMAT: no cues found')
    expected = 1
    prev_start: float | None = None
    prev_end: float | None = None
    for block in blocks:
        lines = block.split('\n')
        if len(lines) < 3:
            raise RuntimeError(f'INVALID_SRT_FORMAT: malformed block (need index+timecode+text): {block[:200]!r}')
        try:
            index = int(lines[0].strip())
        except ValueError as exc:
            raise RuntimeError(f'INVALID_SRT_FORMAT: cue index invalid: {lines[0]!r}') from exc
        if index != expected:
            raise RuntimeError(f'INVALID_SRT_FORMAT: numbering not sequential: got {index}, expected {expected}')
        timecode = lines[1].strip()
        if not TIMECODE_RE.match(timecode):
            # Distinguish dot vs comma for clearer error.
            if '.' in timecode and '-->' in timecode:
                raise RuntimeError(f'INVALID_SRT_FORMAT: use comma for milliseconds: {timecode!r}') from None
            raise RuntimeError(f'INVALID_SRT_FORMAT: timecode invalid: {timecode!r}') from None
        start_s, end_s = timecode.split(' --> ')
        try:
            start = parse_timestamp(start_s)
            end = parse_timestamp(end_s)
        except ValueError as exc:
            raise RuntimeError(f'INVALID_SRT_FORMAT: {exc}') from exc
        if not end > start:
            raise RuntimeError(f'INVALID_SRT_FORMAT: start must be < end: {timecode!r}')
        body = '\n'.join(lines[2:])
        if not body.strip():
            raise RuntimeError(f'INVALID_SRT_FORMAT: empty cue text at index {index}')
        # Text lines: max 2 lines.
        text_lines = body.strip().split('\n')
        if len(text_lines) > 2:
            raise RuntimeError(f'INVALID_SRT_FORMAT: cue {index} has more than 2 lines')
        if prev_start is not None and prev_end is not None:
            if not start >= prev_start:
                raise RuntimeError(f'INVALID_SRT_FORMAT: cue {index} starts before previous cue')
            if start < prev_end - 1e-3:
                # Allow tiny float tolerance but not real overlaps like 1-5 then 2-4.
                # Contiguous (==) is valid.
                raise RuntimeError(f'INVALID_SRT_FORMAT: cue {index} overlaps previous cue')
        prev_start, prev_end = start, end
        expected += 1
    return len(blocks)


def validate_srt(path: Path) -> int:
    """Validate SRT file (UTF-8, no BOM required, LF). Returns cue count."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise RuntimeError(f'INVALID_SRT_FORMAT: cannot read file: {exc}') from exc
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise RuntimeError(f'INVALID_SRT_FORMAT: not valid UTF-8: {exc}') from exc
    if text.startswith('\ufeff'):
        text = text.lstrip('\ufeff')
    count = validate_srt_text(text)
    if count <= 0:
        raise RuntimeError('INVALID_SRT_FORMAT: cue count must be > 0')
    return count


def write_srt(path: Path, srt: str) -> int:
    """Write strict SRT (UTF-8, no BOM, LF) and validate by re-parsing. Returns cue count."""
    normalized = srt.replace('\r\n', '\n').replace('\r', '\n')
    if not normalized.endswith('\n'):
        normalized += '\n'
    path.write_text(normalized, encoding='utf-8', newline='\n')
    return validate_srt(path)
