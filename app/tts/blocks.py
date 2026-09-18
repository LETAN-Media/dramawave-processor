"""Speech-block grouping for voice generation.

The SRT (829 cues, IDs, timestamps) is IMMUTABLE. Blocks are an audio-only
structure: consecutive cues with near-continuous dialogue are spoken as one
natural TTS turn, placed at the block's absolute start.
"""

from __future__ import annotations

from dataclasses import dataclass, field

STRONG_END_PUNCT = ('.', '!', '?', '…')


@dataclass
class SpeechBlock:
    block_id: int  # 1-based, in timeline order
    start_ms: int  # == first cue start_ms
    end_ms: int  # == last cue end_ms
    cue_ids: list[int] = field(default_factory=list)
    subtitle_texts: list[str] = field(default_factory=list)  # original VI texts, untouched
    tts_text: str = ''  # voice text (may be compressed independently of SRT)

    @property
    def available_ms(self) -> int:
        return max(0, self.end_ms - self.start_ms)


def _ends_sentence(text: str) -> bool:
    t = (text or '').strip()
    return bool(t) and t[-1] in STRONG_END_PUNCT


def join_texts(texts: list[str]) -> str:
    """Join cue texts for TTS with light punctuation normalization (SRT untouched)."""
    parts: list[str] = []
    for t in texts:
        t = ' '.join(str(t or '').replace('\u200b', '').replace('\ufeff', '').split())
        if t:
            parts.append(t)
    return ' '.join(parts)


def build_blocks(
    cues: list[dict],
    *,
    max_gap_ms: int = 250,
    target_ms: int = 4500,
    max_ms: int = 8000,
) -> list[SpeechBlock]:
    """Group consecutive cues into speech blocks.

    New block when: gap > max_gap_ms (real silence kept), or adding the cue
    would exceed max_ms, or block reached target-ish duration at sentence end.
    """
    blocks: list[SpeechBlock] = []
    current: list[dict] = []

    def _flush() -> None:
        if not current:
            return
        texts = [' '.join(str(c.get('text', c.get('vi_text', '')) or '').split()) for c in current]
        texts = [t for t in texts if t]
        blocks.append(SpeechBlock(
            block_id=len(blocks) + 1,
            start_ms=current[0]['start_ms'],
            end_ms=current[-1]['end_ms'],
            cue_ids=[c['id'] for c in current],
            subtitle_texts=texts,
            tts_text=join_texts(texts),
        ))

    for cue in sorted(cues, key=lambda c: c['id']):
        if current:
            prev = current[-1]
            gap = cue['start_ms'] - prev['end_ms']
            if gap > max_gap_ms:
                _flush()
                current = []
            elif cue['end_ms'] - current[0]['start_ms'] > max_ms:
                _flush()
                current = []
        current.append(cue)
        dur = current[-1]['end_ms'] - current[0]['start_ms']
        last_text = ' '.join(str(current[-1].get('text', current[-1].get('vi_text', '')) or '').split())
        if dur >= target_ms and _ends_sentence(last_text):
            _flush()
            current = []
    _flush()
    # Renumber defensively (ids are 1-based timeline order).
    for i, b in enumerate(blocks, start=1):
        b.block_id = i
    return blocks
