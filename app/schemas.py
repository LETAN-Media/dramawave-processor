from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class JobCreate(BaseModel):
    url: str = Field(min_length=8, max_length=2048)


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    source_url: str
    resolved_url: str | None = None
    bvid: str | None = None
    cid: str | None = None
    title: str | None = None
    author: str | None = None
    cover_url: str | None = None
    duration_seconds: float | None = None
    status: str
    current_stage: str
    progress: int
    error: str | None = None
    attempts: int
    video_storage_key: str | None = None
    subtitle_storage_key: str | None = None
    subtitle_language: str | None = None
    subtitle_cue_count: int | None = None
    subtitle_source: str | None = None
    asr_provider: str | None = None
    asr_started_at: datetime | None = None
    asr_completed_at: datetime | None = None
    asr_processing_seconds: float | None = None
    asr_fallback_used: bool | None = None
    vi_storage_key: str | None = None
    vi_cue_count: int | None = None
    translation_provider: str | None = None
    translation_model: str | None = None
    translation_batches: int | None = None
    translation_seconds: float | None = None
    translation_primary_model: str | None = None
    translation_fallback_model: str | None = None
    translation_primary_batches: int | None = None
    translation_fallback_batches: int | None = None
    translation_failed_batches: int | None = None
    translation_retries: int | None = None
    tts_provider: str | None = None
    tts_voice: str | None = None
    tts_clip_count: int | None = None
    tts_seconds: float | None = None
    tts_timing_warnings: int | None = None
    voice_storage_key: str | None = None
    voice_duration_seconds: float | None = None
    phase2_seconds: float | None = None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None


class JobAccepted(BaseModel):
    job_id: str
    status: str


class ViCueEdit(BaseModel):
    id: int
    text: str = Field(min_length=1, max_length=2000)


class ViPatch(BaseModel):
    cues: list[ViCueEdit] = Field(min_length=1, max_length=500)


class HealthOut(BaseModel):
    ok: bool
    service: str
    database: bool
    worker: bool
    worker_last_seen_at: datetime | None = None
    asr: dict | None = None
