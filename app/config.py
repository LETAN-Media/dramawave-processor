from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')

    app_name: str = 'bilibili-processor'
    environment: str = 'development'
    api_key: str | None = None

    database_url: str = 'sqlite:///./bilibili_processor.db'

    worker_id: str = 'worker-1'
    worker_poll_seconds: int = 3
    worker_heartbeat_seconds: int = 20
    job_lease_seconds: int = 180
    stale_job_max_age_hours: int = 24

    work_dir: Path = Path('/tmp/bilibili-processor')
    storage_provider: str = 'local'
    local_storage_dir: Path = Path('/var/lib/bilibili-processor/storage')
    persist_original_video: bool = True

    s3_endpoint_url: str | None = None
    s3_region: str = 'auto'
    s3_bucket: str | None = None
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None
    s3_public_base_url: str | None = None

    bilibili_cookies_file: Path | None = None
    bilibili_user_agent: str = (
        'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) '
        'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 '
        'Mobile/15E148 Safari/604.1'
    )
    # Desktop UA for yt-dlp: iPhone UA forces m.bilibili generic extractor
    # and breaks downloads. API calls keep the iPhone UA above.
    bilibili_ytdlp_user_agent: str = (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/126.0.0.0 Safari/537.36'
    )
    download_format: str = 'bv*[height<=1080]+ba/b[height<=1080]/b'
    download_format_fallback: str = 'bv*[vcodec^=avc1][height<=480]+ba/b[height<=480]/b'
    max_download_height: int = 1080
    download_concurrent_fragments: int = 4
    http_timeout_seconds: int = 20

    subtitle_preferred_languages: list[str] = Field(
        default_factory=lambda: ['zh-CN', 'zh-Hans', 'zh-Hant', 'zh', 'ai-zh']
    )
    # asr | bilibili | auto (default asr: always use video audio)
    subtitle_source_mode: str = 'asr'

    # --- Chinese ASR (faster-whisper, CPU-only default) ---
    whisper_model: str = 'small'
    whisper_device: str = 'cpu'
    whisper_compute_type: str = 'int8'
    whisper_cpu_threads: int = 4
    omp_num_threads: int = 4
    whisper_vad_filter: bool = True
    whisper_word_timestamps: bool = True
    whisper_language: str = 'zh'
    asr_concurrency: int = 1

    # --- ASR provider selection: jianying | whisper | auto (default auto) ---
    asr_provider: str = 'auto'
    allow_remote_asr: bool = True
    jianying_enabled: bool = True
    jianying_max_retries: int = 2
    jianying_upload_timeout: int = 120
    jianying_process_timeout: int = 600
    jianying_poll_interval: int = 2
    jianying_concurrency: int = 1
    jianying_cli: str = 'jianying-subtitle'

    # --- Audio extraction ---
    audio_sample_rate: int = 16000
    audio_channels: int = 1

    # --- Translation (OpenAI-compatible; default ToolNet) ---
    translation_provider: str = 'toolnet'
    translation_base_url: str = 'https://api.toolnet.tech/v1'
    translation_api_key: str | None = None
    translation_model: str = 'alims-intl.llm'
    translation_fallback_model: str = 'groq/qwen/qwen3.8-27b'
    translation_batch_size: int = 50
    translation_concurrency: int = 3
    translation_timeout: int = 120
    translation_max_retries: int = 3
    translation_context_cues: int = 5
    translation_glossary: str = ''

    # --- Vietnamese TTS (Edge default; architecture open) ---
    tts_provider: str = 'edge'
    tts_voice: str = 'vi-VN-HoaiMyNeural'
    tts_rate: str = '+0%'
    tts_volume: str = '+0%'
    tts_concurrency: int = 6
    tts_timeout: int = 120
    tts_max_retries: int = 3
    tts_max_tempo: float = 1.25
    tts_sample_rate: int = 44100
    vi_chars_per_sec: float = 9.0

    # --- Strict SRT + QA rules (single source of truth) ---
    srt_min_duration_ms: int = 300
    srt_max_chars_per_line: int = 24
    srt_max_lines: int = 2
    cps_target: float = 20.0
    qa_max_rounds: int = 2
    qa_severe_overflow_ms: int = 250
    qa_severe_overflow_ratio: float = 0.10

    @field_validator('asr_provider', mode='before')
    @classmethod
    def _normalize_asr_provider(cls, v):
        s = str(v or 'auto').strip().lower()
        if s not in {'jianying', 'whisper', 'auto'}:
            return 'auto'
        return s

    @field_validator('subtitle_source_mode', mode='before')
    @classmethod
    def _normalize_subtitle_mode(cls, v):
        s = str(v or 'asr').strip().lower()
        if s not in {'asr', 'bilibili', 'auto'}:
            return 'asr'
        return s

    @field_validator('bilibili_cookies_file', mode='before')
    @classmethod
    def _normalize_cookies_file(cls, v):
        # Unset / None -> NO COOKIE
        if v is None:
            return None
        # Already a Path (e.g. default): defensive normalize.
        if isinstance(v, Path):
            s = str(v).strip()
            if not s or s == '.':
                return None
            return Path(s)
        # Strings from env / .env: strip whitespace.
        # "", "   " -> None (NO COOKIE). Never Path("") / Path(".").
        s = str(v).strip()
        if not s:
            return None
        return Path(s)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
