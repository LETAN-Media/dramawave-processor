"""OpenAI-compatible translation provider (ToolNet default, OpenAI/others ready).

Primary TRANSLATION_MODEL, then TRANSLATION_FALLBACK_MODEL, tried IN ORDER
for EVERY batch independently. API key is never logged.

Note: alims-intl.llm only responds in SSE streaming mode, so requests use
stream:true and accumulate deltas (with an overall timeout).
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request

from app.config import settings
from app.translation.base import TransContext, TransCue, TranslationProvider

logger = logging.getLogger('translation-toolnet')

# -- Primary-model circuit breaker + call stats (process-global) --------------
# After TRANSLATION_PRIMARY_FAILURE_THRESHOLD consecutive transport timeouts,
# the primary model is skipped for TRANSLATION_PRIMARY_COOLDOWN_SECONDS and the
# fallback is used immediately — no more 120s waits per request.
import threading as _threading

_BREAKER_LOCK = _threading.Lock()
_BREAKER = {'model': None, 'failures': 0, 'opened_until': 0.0}
_AI_STATS = {'primary_calls': 0, 'primary_timeouts': 0,
             'fallback_calls': 0, 'fallback_errors': 0}


def _is_transport_failure(exc: Exception) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    msg = str(exc)
    return ('TRANSLATION_HTTP' in msg or 'curl exit 28' in msg
            or 'stream deadline' in msg)


def primary_unhealthy(model: str) -> bool:
    """True when the breaker is open for this primary model."""
    now = time.monotonic()
    with _BREAKER_LOCK:
        if _BREAKER['model'] != model:
            return False
        if now >= _BREAKER['opened_until']:
            if _BREAKER['opened_until']:
                logger.info('translation breaker closed model=%s (cooldown over)', model)
            _BREAKER['model'] = None
            _BREAKER['failures'] = 0
            _BREAKER['opened_until'] = 0.0
            return False
        return True


def _record_success(model: str, is_primary: bool) -> None:
    with _BREAKER_LOCK:
        if is_primary and _BREAKER['model'] == model:
            _BREAKER['model'] = None
            _BREAKER['failures'] = 0
            _BREAKER['opened_until'] = 0.0


def _record_transport_failure(model: str, is_primary: bool) -> None:
    with _BREAKER_LOCK:
        if is_primary:
            _AI_STATS['primary_timeouts'] += 1
            if _BREAKER['model'] != model:
                _BREAKER['model'] = model
                _BREAKER['failures'] = 0
            _BREAKER['failures'] += 1
            threshold = max(1, int(settings.translation_primary_failure_threshold or 1))
            if _BREAKER['failures'] >= threshold:
                cooldown = max(60, int(settings.translation_primary_cooldown_seconds or 600))
                _BREAKER['opened_until'] = time.monotonic() + cooldown
                logger.warning('translation breaker OPEN model=%s failures=%s cooldown=%ss',
                               model, _BREAKER['failures'], cooldown)
        else:
            _AI_STATS['fallback_errors'] += 1


def _record_call(is_primary: bool) -> None:
    with _BREAKER_LOCK:
        if is_primary:
            _AI_STATS['primary_calls'] += 1
        else:
            _AI_STATS['fallback_calls'] += 1


def ai_stats_snapshot() -> dict:
    with _BREAKER_LOCK:
        return dict(_AI_STATS)


def breaker_snapshot() -> dict:
    with _BREAKER_LOCK:
        return dict(_BREAKER)


def reset_ai_circuit() -> None:
    """Reset breaker + stats (tests only)."""
    with _BREAKER_LOCK:
        _BREAKER.update(model=None, failures=0, opened_until=0.0)
        for k in _AI_STATS:
            _AI_STATS[k] = 0

SYSTEM_PROMPT_TEMPLATE = (
    'Bạn là một biên dịch viên chuyên nghiệp chuyên dịch phụ đề phim và drama Trung Quốc sang tiếng Việt.\n'
    'Nhiệm vụ của bạn là dịch nội dung từng cue từ tiếng Trung sang tiếng Việt tự nhiên, hiện đại và phù hợp ngữ cảnh phim.\n'
    'QUY TẮC BẮT BUỘC:\n'
    '1. Mỗi input ID phải có chính xác một output ID tương ứng.\n'
    '2. Giữ nguyên ID tuyệt đối. Không được: thêm ID, xóa ID, đổi ID, merge ID, split ID.\n'
    '3. Không xử lý hoặc tạo timecode. Backend sẽ chịu trách nhiệm hoàn toàn về timecode.\n'
    '4. Chỉ dịch text_zh → text_vi.\n'
    '5. Dịch đúng ý lời thoại. Không: thêm nội dung, bỏ ý quan trọng, giải thích, chú thích, ghi chú người dịch.\n'
    '6. Văn phong: tiếng Việt hiện đại, tự nhiên, dễ nghe, phù hợp drama/phim Trung Quốc, tránh văn dịch máy cứng nhắc.\n'
    '7. Xưng hô phải dựa vào ngữ cảnh và quan hệ nhân vật (ví dụ: anh/em, chị/em, tôi/cậu, tôi/anh, con/mẹ, con/bố, '
    'cháu/ông, cháu/bà). Không dịch đại từ Trung Quốc một cách máy móc.\n'
    '8. Giữ xưng hô nhất quán xuyên suốt phim dựa trên previous_context, character_glossary, relationship_glossary.\n'
    '9. Tên riêng: khi chắc chắn tên Trung Quốc thì dùng cách đọc Hán-Việt phù hợp (ví dụ: 北京 → Bắc Kinh). '
    'Không tự bịa tên nếu không chắc chắn. Giữ cách gọi tên nhất quán xuyên suốt phim.\n'
    '10. duration_ms là thời lượng hiển thị. Bản dịch cần đủ ngắn để đọc và nói kịp (mục tiêu CPS <= __CPS__).\n'
    '11. Nếu câu quá dài: rút gọn cách diễn đạt nhưng giữ nguyên ý, tiếp tục tối ưu cho tự nhiên và ngắn hơn. '
    'Không được chia cue, đổi ID, đổi timing.\n'
    '12. Subtitle tối đa 2 dòng, mục tiêu mỗi dòng <= 40 ký tự. Nếu cần xuống dòng dùng \\n trong text_vi.\n'
    '13. Không để lại chữ Trung Quốc trong text_vi trừ tên/ký hiệu cần giữ nguyên.\n'
    '14. Không trả Markdown, không giải thích. Chỉ trả JSON array hợp lệ, ví dụ:\n'
    '[{"id": 1, "text_vi": "Năm đó, bố mẹ tôi qua đời."}, {"id": 2, "text_vi": "Công ty sắp phá sản."}]\n'
    'Mỗi input ID phải xuất hiện đúng một lần trong output.'
)


def system_prompt(source_language: str = 'zh') -> str:
    """Official translation prompt with live CPS target (single source of truth).

    source_language parametrizes the prompt (zh/en/ko/ja/...); zh behavior
    is byte-identical to the production-tested original.
    """
    try:
        target = settings.cps_target
        cps = int(target) if float(target).is_integer() else target
    except (TypeError, ValueError):
        cps = 20
    text = SYSTEM_PROMPT_TEMPLATE.replace('__CPS__', str(cps))
    lang = (source_language or 'zh').strip().lower()
    if lang == 'zh':
        return text
    names = {'en': ('Anh', 'tiếng Anh'), 'ko': ('Hàn Quốc', 'tiếng Hàn'),
             'ja': ('Nhật Bản', 'tiếng Nhật')}
    name, tongue = names.get(lang, (lang, lang))
    text = text.replace('Trung Quốc', name).replace('tiếng Trung', tongue).replace('Trung', name)
    text = text.replace(
        'dùng cách đọc Hán-Việt phù hợp (ví dụ: 北京 → Bắc Kinh). Không tự bịa tên nếu không chắc chắn.',
        'giữ nguyên cách viết tên đã thống nhất trong glossary. Không tự bịa tên nếu không chắc chắn.')
    text = text.replace(
        'Không để lại chữ Trung Quốc trong text_vi trừ tên/ký hiệu cần giữ nguyên.',
        'Không để lại chữ ngoại ngữ nguồn trong text_vi trừ tên/ký hiệu cần giữ nguyên.')
    return text


def _post_chat_stream(messages: list[dict], model: str, timeout: int, max_tokens: int) -> str:
    """POST chat/completions with stream:true, accumulate SSE deltas.

    Uses curl --max-time (total wall-clock cap, trickle-proof). urllib's
    per-recv timeout resets on any byte, which lets a degraded gateway hang
    workers forever; --max-time cannot be reset by trickles.
    """
    import shutil
    import subprocess
    import tempfile

    if not settings.translation_api_key:
        raise RuntimeError('TRANSLATION_API_KEY is not configured')
    body = {
        'model': model,
        'stream': True,
        'messages': messages,
        'temperature': 0.2,
        'max_tokens': max_tokens,
    }
    total_timeout = max(10, int(timeout))
    if shutil.which('curl') is None:
        return _post_chat_stream_urllib(messages, model, total_timeout, max_tokens)
    url = settings.translation_base_url.rstrip('/') + '/chat/completions'
    chunks: list[str] = []
    try:
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=True) as bf, \
                tempfile.NamedTemporaryFile('w', suffix='.curlcfg', delete=True) as cf:
            import os

            json.dump(body, bf)
            bf.flush()
            # Key via config file (0600) so it never appears in process listings.
            os.chmod(cf.name, 0o600)
            cf.write('header = "Content-Type: application/json"\n')
            cf.write('header = "Authorization: Bearer ' + settings.translation_api_key.replace('"', '') + '"\n')
            cf.flush()
            proc = subprocess.run(
                ['curl', '-sS', '-N', '--fail-with-body', '--max-time', str(total_timeout),
                 '-X', 'POST', url, '-K', cf.name,
                 '--data-binary', '@' + bf.name],
                capture_output=True, text=True, timeout=total_timeout + 15,
            )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f'stream deadline {total_timeout}s exceeded') from exc
    except FileNotFoundError as exc:
        raise RuntimeError(f'TRANSLATION_HTTP: curl missing: {exc}') from exc
    output = proc.stdout or ''
    if proc.returncode != 0:
        # HTTP 4xx/5xx (quota/429/unavailable) land here via --fail-with-body.
        detail = ((proc.stdout or '') + ' ' + (proc.stderr or '')).strip()[-500:]
        raise RuntimeError(f'TRANSLATION_HTTP: curl exit {proc.returncode}: {detail}')
    for ln in output.splitlines():
        ln = ln.strip()
        if not ln.startswith('data:'):
            continue
        data = ln[5:].strip()
        if data == '[DONE]':
            break
        try:
            evt = json.loads(data)
        except json.JSONDecodeError:
            continue
        try:
            delta = evt['choices'][0]['delta'].get('content') or ''
        except (KeyError, IndexError, TypeError):
            try:
                delta = evt['choices'][0]['message'].get('content') or ''
            except (KeyError, IndexError, TypeError, AttributeError):
                delta = ''
        if delta:
            chunks.append(delta)
    content = ''.join(chunks)
    content = '\n'.join(ln for ln in content.splitlines() if not ln.strip().startswith('data:'))
    if not content.strip():
        raise RuntimeError('TRANSLATION_EMPTY_RESPONSE')
    return content


def _post_chat_stream_urllib(messages: list[dict], model: str, timeout: int, max_tokens: int) -> str:
    """Fallback transport when curl is unavailable (per-recv timeout semantics)."""
    if not settings.translation_api_key:
        raise RuntimeError('TRANSLATION_API_KEY is not configured')
    body = {
        'model': model,
        'stream': True,
        'messages': messages,
        'temperature': 0.2,
        'max_tokens': max_tokens,
    }
    req = urllib.request.Request(
        settings.translation_base_url.rstrip('/') + '/chat/completions',
        data=json.dumps(body).encode('utf-8'),
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + settings.translation_api_key},
    )
    deadline = time.monotonic() + max(10, timeout)
    chunks: list[str] = []
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            buf = ''
            while True:
                if time.monotonic() > deadline:
                    raise TimeoutError(f'stream deadline {timeout}s exceeded')
                line = resp.readline()
                if not line:
                    break
                try:
                    text = line.decode('utf-8', errors='replace')
                except Exception:
                    continue
                buf += text
                while '\n' in buf:
                    ln, buf = buf.split('\n', 1)
                    ln = ln.strip()
                    if not ln.startswith('data:'):
                        continue
                    data = ln[5:].strip()
                    if data == '[DONE]':
                        break
                    try:
                        evt = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    try:
                        delta = evt['choices'][0]['delta'].get('content') or ''
                    except (KeyError, IndexError, TypeError):
                        # Some gateways send full message objects per chunk.
                        try:
                            delta = evt['choices'][0]['message'].get('content') or ''
                        except (KeyError, IndexError, TypeError, AttributeError):
                            delta = ''
                    if delta:
                        chunks.append(delta)
                else:
                    continue
                break
    except TimeoutError:
        raise
    except Exception as exc:
        raise RuntimeError(f'TRANSLATION_HTTP: {type(exc).__name__} {str(exc)[:200]}') from exc
    content = ''.join(chunks)
    # Strip stray SSE tails.
    content = '\n'.join(ln for ln in content.splitlines() if not ln.strip().startswith('data:'))
    if not content.strip():
        raise RuntimeError('TRANSLATION_EMPTY_RESPONSE')
    return content


def _extract_mapping(raw: str, expected_ids: list[int]) -> dict[int, str]:
    """Accept [{id, text_vi}] or {cues:[{id, text_vi}]}. Strict schema validation."""
    text = raw.strip()
    if text.startswith('```'):
        text = text.strip('`').strip()
        if text.lower().startswith('json'):
            text = text[4:].strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f'TRANSLATION_BAD_JSON: {exc}') from exc
    items = payload if isinstance(payload, list) else payload.get('cues') if isinstance(payload, dict) else None
    if isinstance(payload, dict) and items is None and 'id' in payload:
        items = [payload]  # bare single-cue object {"id","text_vi"} (compression path)
    if not isinstance(items, list):
        raise RuntimeError('TRANSLATION_BAD_JSON: missing cues list')
    mapping: dict[int, str] = {}
    for item in items:
        if not isinstance(item, dict):
            raise RuntimeError('TRANSLATION_BAD_SCHEMA: cue must be an object')
        try:
            cid = int(item.get('id'))
        except (TypeError, ValueError):
            raise RuntimeError('TRANSLATION_BAD_SCHEMA: cue id must be an integer') from None
        if cid in mapping:
            raise RuntimeError(f'TRANSLATION_DUPLICATE_ID: {cid}')
        tv = item.get('text_vi', item.get('text', None))
        if not isinstance(tv, str) or not tv.strip():
            raise RuntimeError(f'TRANSLATION_EMPTY: cue {cid}')
        mapping[cid] = tv.strip()
    unknown = [cid for cid in mapping if cid not in set(expected_ids)]
    if unknown:
        raise RuntimeError(f'TRANSLATION_UNKNOWN_IDS: {unknown[:10]}')
    missing = [i for i in expected_ids if i not in mapping]
    if missing:
        raise RuntimeError(f'TRANSLATION_INCOMPLETE: missing ids {missing[:10]}')
    return mapping


def _text_key(source_language: str) -> str:
    return 'text_zh' if (source_language or 'zh').strip().lower() == 'zh' else 'text_source'


def _user_payload(cues: list[TransCue], context: TransContext) -> str:
    lang = (getattr(context, 'source_language', None) or 'zh').strip().lower()
    key = _text_key(lang)
    return json.dumps({
        'source_language': lang,
        'translation_style': (getattr(context, 'style', None) or 'AUTO'),
        'previous_context': [
            {'id': c.cue_id, 'zh': c.text, 'vi': v} for c, v in context.previous
        ],
        'glossary': {
            'characters': context.glossary.get('characters', {}),
            'relationships': context.glossary.get('relationships', {}),
            'pronouns': context.glossary.get('pronouns', {}),
        },
        'current_cues': [{'id': c.cue_id, key: c.text,
                          'duration_ms': max(0, c.end_ms - c.start_ms)} for c in cues],
    }, ensure_ascii=False)


class OpenAICompatibleProvider(TranslationProvider):
    name = 'toolnet'

    def __init__(self) -> None:
        import threading

        models = [settings.translation_model] + [
            m.strip() for m in (settings.translation_fallback_model or '').split(',') if m.strip()
        ]
        self.models: list[str] = list(dict.fromkeys(models))  # dedupe, order kept
        self._retry_lock = threading.Lock()
        self.retry_count = 0  # failed attempts (retried or failed over)

    @property
    def primary_model(self) -> str:
        return self.models[0]

    @property
    def fallback_models(self) -> list[str]:
        return self.models[1:]

    def translate_batch_with_model(self, cues: list[TransCue], context: TransContext,
                                     source_language: str = 'zh') -> tuple[dict[int, str], str]:
        """Translate one batch, trying primary then fallbacks. Returns (mapping, model_used)."""
        if not cues:
            return {}, self.primary_model
        lang = (source_language or getattr(context, 'source_language', None) or 'zh').strip().lower()
        messages = [
            {'role': 'system', 'content': system_prompt(lang)},
            {'role': 'user', 'content': _user_payload(cues, context)},
        ]
        max_tokens = max(1000, 80 * len(cues))
        last_err: Exception | None = None
        for idx, model in enumerate(self.models):
            is_primary = idx == 0
            if is_primary and primary_unhealthy(model):
                logger.warning('translate batch skipping unhealthy primary model=%s', model)
                continue
            for attempt in range(max(1, settings.translation_max_retries) + 1):
                try:
                    t0 = time.monotonic()
                    _record_call(is_primary)
                    raw = _post_chat_stream(messages, model, settings.translation_timeout, max_tokens)
                    mapping = _extract_mapping(raw, [c.cue_id for c in cues])
                    _record_success(model, is_primary)
                    logger.info('translate batch ok model=%s cues=%s elapsed=%.1fs', model, len(cues), time.monotonic() - t0)
                    return mapping, model
                except Exception as exc:  # noqa: BLE001 - failover across models
                    last_err = exc
                    if _is_transport_failure(exc):
                        _record_transport_failure(model, is_primary)
                    with self._retry_lock:
                        self.retry_count += 1
                    logger.warning('translate batch failed model=%s attempt=%s error=%s', model, attempt + 1, str(exc)[:300])
                    time.sleep(min(2 * (attempt + 1), 8))
        raise RuntimeError(f'TRANSLATION_FAILED: {last_err}')

    def _run_chain(self, messages: list[dict], expected_ids: list[int], max_tokens: int,
                     timeout: int, primary_retries: int, fallback_retries: int,
                     label: str) -> tuple[dict[int, str], str]:
        """Try primary then fallbacks with per-role retry budgets + breaker skip."""
        last_err: Exception | None = None
        for idx, model in enumerate(self.models):
            is_primary = idx == 0
            if is_primary and primary_unhealthy(model):
                logger.warning('%s skipping unhealthy primary model=%s', label, model)
                continue
            attempts = 1 + max(0, primary_retries if is_primary else fallback_retries)
            for attempt in range(attempts):
                try:
                    t0 = time.monotonic()
                    _record_call(is_primary)
                    raw = _post_chat_stream(messages, model, timeout, max_tokens)
                    mapping = _extract_mapping(raw, expected_ids)
                    _record_success(model, is_primary)
                    logger.info('%s ok model=%s cues=%s elapsed=%.1fs',
                                label, model, len(expected_ids), time.monotonic() - t0)
                    return mapping, model
                except Exception as exc:  # noqa: BLE001
                    last_err = exc
                    if _is_transport_failure(exc):
                        _record_transport_failure(model, is_primary)
                    with self._retry_lock:
                        self.retry_count += 1
                    logger.warning('%s failed model=%s attempt=%s error=%s',
                                   label, model, attempt + 1, str(exc)[:200])
                    time.sleep(2)
        raise RuntimeError(f'{label}: {last_err}')

    def compress_batch(self, items: list[tuple[int, str, int]], max_attempts: int = 2,
                       *, timeout: int | None = None, primary_retries: int | None = None,
                       fallback_retries: int | None = None) -> tuple[dict[int, str], str]:
        """Compress up to ~10 cues in one model call. Returns ({cue_id: shorter}, model_used).

        Optional fast-path overrides (Voice QA): short timeout, 0 primary
        retries so a sick primary costs seconds, not minutes.
        """
        lines = []
        for cid, text, dur in items:
            budget = max(8, int(dur / 1000 * self._cps_num()))
            lines.append({'id': cid, 'text_vi': text, 'duration_ms': dur, 'max_chars': budget})
        messages = [
            {'role': 'system', 'content': system_prompt()},
            {'role': 'user', 'content': json.dumps({
                'task': 'compress_batch',
                'instruction': (
                    'Rewrite EACH Vietnamese subtitle below more concisely while preserving the core meaning, '
                    'character names, pronouns and context. Respect each cue\'s max_chars HARD LIMIT '
                    f'(derived from duration and CPS <= {self._cps()}). Shorter is better. '
                    'Summarize aggressively when needed, but never invent content. '
                    'Return JSON only: [{"id": ..., "text_vi": "..."}, ...] with exactly one entry per input cue.'
                ),
                'cues': lines,
            }, ensure_ascii=False)},
        ]
        return self._run_chain(
            messages, [cid for cid, _, _ in items], 2000,
            max(10, int(timeout if timeout is not None else settings.translation_timeout)),
            max_attempts if primary_retries is None else primary_retries,
            max_attempts if fallback_retries is None else fallback_retries,
            'compress batch')

    @staticmethod
    def _cps() -> str:
        try:
            target = settings.cps_target
            return str(int(target) if float(target).is_integer() else target)
        except (TypeError, ValueError):
            return '20'

    @staticmethod
    def _cps_num() -> float:
        try:
            return max(1.0, float(settings.cps_target))
        except (TypeError, ValueError):
            return 20.0

    def compress_text(self, cue_id: int, text_vi: str, duration_ms: int, max_attempts: int = 2,
                        *, timeout: int | None = None, primary_retries: int | None = None,
                        fallback_retries: int | None = None) -> str:
        """Rewrite one Vietnamese cue more concisely (CPS). Same model chain."""
        try:
            cps = settings.cps_target
        except (TypeError, ValueError):
            cps = 20
        budget = max(8, int(duration_ms / 1000 * self._cps_num()))
        messages = [
            {'role': 'system', 'content': system_prompt()},
            {'role': 'user', 'content': json.dumps({
                'task': 'compress',
                'instruction': (
                    'Rewrite this Vietnamese subtitle more concisely while preserving the core meaning, '
                    'character names, pronouns and context. '
                    f'Target spoken/display duration: {duration_ms} ms. Maximum target CPS: {cps}. '
                    f'HARD LIMIT: at most {budget} characters including spaces; shorter is better. '
                    'Summarize aggressively if needed, never invent content. '
                    'Keep it natural. Do not change the ID. Return JSON only: {"id": %d, "text_vi": "..."}' % cue_id
                ),
                'id': cue_id,
                'text_vi': text_vi,
                'duration_ms': duration_ms,
            }, ensure_ascii=False)},
        ]
        mapping, _used = self._run_chain(
            messages, [cue_id], 500,
            max(10, int(timeout if timeout is not None else settings.translation_timeout)),
            max_attempts if primary_retries is None else primary_retries,
            max_attempts if fallback_retries is None else fallback_retries,
            f'compress cue {cue_id}')
        return mapping[cue_id]

    def translate_batch(self, cues: list[TransCue], context: TransContext) -> dict[int, str]:
        mapping, _ = self.translate_batch_with_model(cues, context)
        return mapping
