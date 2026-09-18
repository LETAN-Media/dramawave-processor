# dramawave-processor

End-to-end DramaWave short-drama processor on VPS Docker:

```
DramaWave
        ↓
dramawave-api.onrender.com          (stateless resolver API: search/series/episodes/playback JSON)
        ↓ JSON/M3U8
VPS Processor (this repo)
        ↓
Download (FFmpeg HLS remux)
        ↓
ASR (JianYing primary for zh → faster-whisper fallback / Whisper primary otherwise)
        ↓
Translation (ToolNet zh/en/ko/ja → Vietnamese, timecode-immutable)
        ↓
TTS (Edge speech-block voice, absolute timeline)
        ↓
Render (cover burnt-in subs, burn VI SRT, mix voice + original audio)
        ↓
final.vi.mp4
```

Each episode is processed independently (`series → episodes → per-episode jobs`).

## Architecture

- FastAPI: control plane only; never performs long work inside HTTP requests.
- PostgreSQL: durable series/episode/job state, leases, worker heartbeat.
- Worker: claims episode jobs (default 2 slots), renews leases, resumes after restart.
- Storage abstraction: local for development; S3/R2 for production.
- The processor NEVER calls upstream DramaWave directly. The only DramaWave
  source is the resolver API (`DRAMAWAVE_API_BASE_URL`). If upstream changes,
  only `LETAN-Media/DramaWave-API` needs a fix.

## Quick start

```bash
cp .env.example .env
# edit API keys (never commit .env)
docker compose up -d --build
curl -s http://127.0.0.1:8100/health | python3 -m json.tool
```

## Typical flow

```bash
API_KEY=...; B=http://127.0.0.1:8100
# search + resolve
curl -s "$B/v1/drama/search?q=love" -H "X-API-Key: $API_KEY"
# process episodes 1-4 of a series (series row id from resolve)
curl -s -X POST "$B/v1/series/<series_id>/process" -H "X-API-Key: $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"from_episode":1,"to_episode":4,"quality":"1080p","target_language":"vi"}'
# progress
curl -s "$B/v1/drama/series/<series_id>/jobs" -H "X-API-Key: $API_KEY"
# artifacts
curl -s -o final.vi.mp4 "$B/v1/jobs/<job_id>/final" -H "X-API-Key: $API_KEY"
```

## Per-episode stages

`queued → resolving_playback → downloading → extracting_audio → detecting_language
→ transcribing → validating_source_srt → translating → validating_translation
→ generating_tts → syncing_voice → rendering → validating_final → completed`
(`failed` on error with `error_code`/`error_message`; resume skips validated artifacts.)

## Config highlights (.env / .env.example)

- `DRAMAWAVE_API_BASE_URL`, `DRAMAWAVE_API_TOKEN`
- `VIDEO_QUALITY=1080p`, `EPISODE_CONCURRENCY=2`
- `SOURCE_LANGUAGE=auto`, `TARGET_LANGUAGE=vi`
- `TRANSLATION_*` (ToolNet primary `alims-intl.llm`, fallback `groq/qwen/qwen3.8-27b`)
- `TTS_VOICE=vi-VN-HoaiMyNeural`, speech-block settings
- Render: `RENDER_PRESET=veryfast`, `RENDER_CRF=21`, subtitle cover + audio mix volumes

## Tests

```bash
docker compose exec api python -m pytest -q
```
