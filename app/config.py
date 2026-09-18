from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')

    app_name: str = 'dramawave-processor'
    environment: str = 'development'
    api_key: str | None = None

    database_url: str = 'sqlite:///./dramawave_processor.db'

    worker_id: str = 'worker-1'
    worker_poll_seconds: int = 3
    worker_heartbeat_seconds: int = 20
    job_lease_seconds: int = 180
    stale_job_max_age_hours: int = 24

    work_dir: Path = Path('/tmp/dramawave-processor')
    storage_provider: str = 'local'
    local_storage_dir: Path = Path('/var/lib/dramawave-processor/storage')

    s3_endpoint_url: str | None = None
    s3_region: str = 'auto'
    s3_bucket: str | None = None
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None
    s3_public_base_url: str | None = None

    # --- Languages (source auto-detected per episode, target always VI) ---
    source_language: str = 'auto'
    target_language: str = 'vi'

    # --- ASR (JianYing primary for zh, faster-whisper fallback/others) ---
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
    translation_model: str = 'groq/qwen/qwen3.8-27b'
    translation_fallback_model: str = 'alims-intl.llm'
    translation_batch_size: int = 50
    translation_concurrency: int = 3
    translation_timeout: int = 120
    translation_max_retries: int = 3
    translation_context_cues: int = 5
    translation_glossary: str = ''

    # --- Primary-model circuit breaker (translation) ---
    # After N transport timeouts the primary is skipped for the cooldown window
    # and the fallback is used immediately (no more 120s waits per request).
    translation_primary_failure_threshold: int = 1
    translation_primary_cooldown_seconds: int = 600

    # --- Voice QA fast AI path (small compression requests must stay fast) ---
    voice_qa_ai_timeout: int = 20
    voice_qa_primary_retries: int = 0
    voice_qa_fallback_retries: int = 2
    voice_qa_max_rounds: int = 2

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

    # --- Speech-block voice (SRT stays immutable; blocks are audio-only) ---
    voice_block_max_gap_ms: int = 250
    voice_block_target_ms: int = 4500
    voice_block_max_ms: int = 8000
    voice_block_pref_tempo: float = 1.15

    # --- DramaWave resolver API (Render; the ONLY DramaWave source) ---
    dramawave_enabled: bool = True
    drama_source_api_base_url: str = 'https://dramawave-api.onrender.com'
    drama_source_api_token: str | None = None
    drama_source_api_timeout: int = 90
    drama_source_api_max_retries: int = 3

    # --- Episode processing ---
    video_quality: str = '1080p'
    episode_concurrency: int = 2

    # --- Google OAuth / YouTube publishing (all optional until connected) ---
    google_client_id: str | None = None
    google_client_secret: str | None = None
    youtube_redirect_uri: str | None = None
    youtube_default_privacy: str = 'public'
    youtube_auto_upload: bool = False
    youtube_upload_concurrency: int = 1
    youtube_upload_interval_seconds: int = 0
    app_encryption_key: str | None = None

    # --- DramaWave Studio web UI (dashboard auth; empty = open, set both to require login) ---
    dashboard_username: str | None = None
    dashboard_password: str | None = None
    dashboard_secret: str | None = None
    dashboard_session_hours: int = 24

    # --- Phase 3 render ---
    subtitle_cover_enabled: bool = True
    subtitle_cover_bottom_ratio: float = 0.22
    subtitle_cover_opacity: float = 0.78
    original_audio_volume: float = 0.20
    vi_voice_volume: float = 1.0
    render_preset: str = 'veryfast'
    render_crf: int = 21

    @field_validator('asr_provider', mode='before')
    @classmethod
    def _normalize_asr_provider(cls, v):
        s = str(v or 'auto').strip().lower()
        if s not in {'jianying', 'whisper', 'auto'}:
            return 'auto'
        return s

    @field_validator('youtube_default_privacy', mode='before')
    @classmethod
    def _normalize_privacy(cls, v):
        s = str(v or 'public').strip().lower()
        if s not in {'public', 'unlisted', 'private'}:
            return 'public'
        return s


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
