import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Job(Base):
    __tablename__ = 'jobs'

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    resolved_url: Mapped[str | None] = mapped_column(Text)
    bvid: Mapped[str | None] = mapped_column(String(32), index=True)
    cid: Mapped[str | None] = mapped_column(String(32), index=True)
    title: Mapped[str | None] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(Text)
    cover_url: Mapped[str | None] = mapped_column(Text)
    duration_seconds: Mapped[float | None] = mapped_column(Float)

    status: Mapped[str] = mapped_column(String(40), default='queued', index=True)
    current_stage: Mapped[str] = mapped_column(String(40), default='queued', index=True)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)

    local_video_path: Mapped[str | None] = mapped_column(Text)
    local_subtitle_path: Mapped[str | None] = mapped_column(Text)
    video_storage_key: Mapped[str | None] = mapped_column(Text)
    subtitle_storage_key: Mapped[str | None] = mapped_column(Text)
    subtitle_language: Mapped[str | None] = mapped_column(String(32))
    subtitle_cue_count: Mapped[int | None] = mapped_column(Integer)
    subtitle_source: Mapped[str | None] = mapped_column(String(40))

    asr_provider: Mapped[str | None] = mapped_column(String(32))
    asr_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    asr_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    asr_processing_seconds: Mapped[float | None] = mapped_column(Float)
    asr_fallback_used: Mapped[bool | None] = mapped_column(Boolean)

    # --- Phase 2: VI translation + synchronized TTS ---
    vi_local_path: Mapped[str | None] = mapped_column(Text)
    vi_storage_key: Mapped[str | None] = mapped_column(Text)
    vi_cue_count: Mapped[int | None] = mapped_column(Integer)
    translation_provider: Mapped[str | None] = mapped_column(String(64))
    translation_model: Mapped[str | None] = mapped_column(String(128))
    translation_batches: Mapped[int | None] = mapped_column(Integer)
    translation_seconds: Mapped[float | None] = mapped_column(Float)
    translation_primary_model: Mapped[str | None] = mapped_column(String(128))
    translation_fallback_model: Mapped[str | None] = mapped_column(String(128))
    translation_primary_batches: Mapped[int | None] = mapped_column(Integer)
    translation_fallback_batches: Mapped[int | None] = mapped_column(Integer)
    translation_failed_batches: Mapped[int | None] = mapped_column(Integer)
    translation_retries: Mapped[int | None] = mapped_column(Integer)
    translation_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    translation_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tts_provider: Mapped[str | None] = mapped_column(String(32))
    tts_voice: Mapped[str | None] = mapped_column(String(128))
    tts_clip_count: Mapped[int | None] = mapped_column(Integer)
    tts_seconds: Mapped[float | None] = mapped_column(Float)
    tts_timing_warnings: Mapped[int | None] = mapped_column(Integer)
    voice_local_path: Mapped[str | None] = mapped_column(Text)
    voice_storage_key: Mapped[str | None] = mapped_column(Text)
    voice_duration_seconds: Mapped[float | None] = mapped_column(Float)
    phase2_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    phase2_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    phase2_seconds: Mapped[float | None] = mapped_column(Float)

    lease_owner: Mapped[str | None] = mapped_column(String(128), index=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CueState(Base):
    """Per-cue Phase 2 checkpoint: translation + TTS state for resume."""

    __tablename__ = 'cue_states'

    job_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    cue_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    start_ms: Mapped[int | None] = mapped_column(Integer)
    end_ms: Mapped[int | None] = mapped_column(Integer)
    zh_text: Mapped[str | None] = mapped_column(Text)
    vi_text: Mapped[str | None] = mapped_column(Text)
    translated: Mapped[bool | None] = mapped_column(Boolean)
    estimated_speech_ms: Mapped[int | None] = mapped_column(Integer)
    cps: Mapped[float | None] = mapped_column(Float)
    tts_status: Mapped[str | None] = mapped_column(String(32))
    qa_class: Mapped[str | None] = mapped_column(String(16))
    final_tts_ms: Mapped[int | None] = mapped_column(Integer)
    manual_review_required: Mapped[bool | None] = mapped_column(Boolean)
    tts_path: Mapped[str | None] = mapped_column(Text)
    tts_duration_ms: Mapped[int | None] = mapped_column(Integer)
    tempo: Mapped[float | None] = mapped_column(Float)
    timing_warning: Mapped[bool | None] = mapped_column(Boolean)
    error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class WorkerHeartbeat(Base):
    __tablename__ = 'worker_heartbeats'

    worker_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    hostname: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), default='idle')
    current_job_id: Mapped[str | None] = mapped_column(String(36), index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
