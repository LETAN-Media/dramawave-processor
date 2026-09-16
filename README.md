# bilibili-processor

Phase 1 service for resilient Bilibili ingestion:

`Bilibili URL -> background job -> resolve -> download -> extract Chinese subtitle -> persist state/files`

This repository is intentionally separate from the Douyin/YouTube application. The web app can call it later through HTTP.

## Architecture

- FastAPI: control plane only; never performs long video work inside an HTTP request.
- PostgreSQL: durable job state, leases and worker heartbeat.
- Worker: claims jobs with a lease, renews the lease while processing, and can recover stale work after a crash/restart.
- yt-dlp + FFmpeg: Bilibili media download/merge.
- Subtitle providers: Bilibili `x/player/v2` first, yt-dlp subtitle metadata fallback.
- Storage abstraction: local for development; S3/R2 for production.

For VPS-loss resilience, production should use **external PostgreSQL + R2/S3**. Local `/tmp` is cache only.

## Production data that survives VPS loss

PostgreSQL stores URL, BV/CID, metadata, stage, progress, error, storage keys, lease and timestamps. When `STORAGE_PROVIDER=r2` and `PERSIST_ORIGINAL_VIDEO=true`, the original merged MP4 and normalized Chinese SRT are also copied to R2.

## Quick start with Docker

```bash
cp .env.example .env
# For local Docker development only, set:
# DATABASE_URL=postgresql+psycopg://bilibili:bilibili-dev-only@postgres:5432/bilibili
# STORAGE_PROVIDER=local

docker compose up -d --build
curl http://127.0.0.1:8100/health
```

Expected health after the worker heartbeat appears:

```json
{
  "ok": true,
  "service": "bilibili-processor",
  "database": true,
  "worker": true,
  "worker_last_seen_at": "..."
}
```

## Create a job

If `API_KEY` is configured, send it as `X-API-Key`.

```bash
curl -sS -X POST http://127.0.0.1:8100/v1/jobs \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: YOUR_API_KEY' \
  -d '{"url":"https://www.bilibili.com/video/BV..."}'
```

Response is immediate:

```json
{"job_id":"...","status":"queued"}
```

Poll:

```bash
curl -sS http://127.0.0.1:8100/v1/jobs/JOB_ID \
  -H 'X-API-Key: YOUR_API_KEY'
```

Download/read normalized Chinese SRT:

```bash
curl -sS http://127.0.0.1:8100/v1/jobs/JOB_ID/subtitle \
  -H 'X-API-Key: YOUR_API_KEY'
```

## Job states

- `queued`
- `resolving`
- `downloading`
- `extracting_subtitles`
- `ready`
- `failed`

Failed jobs can be retried with:

```bash
curl -sS -X POST http://127.0.0.1:8100/v1/jobs/JOB_ID/retry \
  -H 'X-API-Key: YOUR_API_KEY'
```

## VPS install with systemd

Prerequisites:

```bash
apt update
apt install -y python3-venv ffmpeg git
```

Then:

```bash
git clone YOUR_REPOSITORY_URL /root/bilibili-processor
cd /root/bilibili-processor
cp .env.example .env
nano .env
./scripts/install_systemd.sh
```

Useful commands:

```bash
systemctl status bilibili-api bilibili-worker
journalctl -u bilibili-api -f
journalctl -u bilibili-worker -f
curl http://127.0.0.1:8100/health
```

## Nginx example

```nginx
server {
    listen 80;
    server_name bilibili-api.toolnet.tech;

    location / {
        proxy_pass http://127.0.0.1:8100;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 60s;
    }
}
```

The long-running job itself is not tied to Nginx timeout because the API only queues work.

## R2 configuration

Create an R2 bucket and S3-compatible credentials, then configure:

```env
STORAGE_PROVIDER=r2
S3_ENDPOINT_URL=https://<ACCOUNT_ID>.r2.cloudflarestorage.com
S3_REGION=auto
S3_BUCKET=bilibili-processor
S3_ACCESS_KEY_ID=...
S3_SECRET_ACCESS_KEY=...
PERSIST_ORIGINAL_VIDEO=true
```

Keys are stored as:

```text
jobs/<job_id>/original.mp4
jobs/<job_id>/source.zh.srt
```

## Cookies

Public videos should be attempted without login. For content that legitimately requires your authenticated session, export a Netscape `cookies.txt` and set:

```env
BILIBILI_COOKIES_FILE=/root/bilibili-cookies.txt
```

Do not commit cookies or secrets.

## Phase 2 (not included yet)

The next stage should consume `source.zh.srt` and add:

`CN -> VI translation -> review -> Vietnamese TTS -> subtitle cover -> FFmpeg final.vi.mp4`

Keep those as separate modules/jobs instead of adding them inside the HTTP request path.
