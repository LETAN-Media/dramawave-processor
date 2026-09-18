import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, UniqueConstraint
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
    episode_metadata: Mapped[str | None] = mapped_column(Text)
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


class SpeechBlock(Base):
    """Audio-only speech block checkpoints. SRT cues/timestamps are never modified."""

    __tablename__ = 'speech_blocks'

    job_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    block_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    start_ms: Mapped[int | None] = mapped_column(Integer)
    end_ms: Mapped[int | None] = mapped_column(Integer)
    cue_ids: Mapped[str | None] = mapped_column(Text)  # JSON list[int]
    subtitle_texts: Mapped[str | None] = mapped_column(Text)  # JSON list[str], SRT originals
    tts_text: Mapped[str | None] = mapped_column(Text)  # voice text (may be compressed)
    voice: Mapped[str | None] = mapped_column(String(128))
    rate: Mapped[str | None] = mapped_column(String(16))
    mp3_path: Mapped[str | None] = mapped_column(Text)
    wav_path: Mapped[str | None] = mapped_column(Text)
    tts_duration_ms: Mapped[int | None] = mapped_column(Integer)
    tempo: Mapped[float | None] = mapped_column(Float)
    overflow_ms: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str | None] = mapped_column(String(16))  # pending/done/failed
    qa_class: Mapped[str | None] = mapped_column(String(16))
    manual_review_required: Mapped[bool | None] = mapped_column(Boolean)
    error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Series(Base):
    """A drama series from any source provider (dramawave, ...)."""

    __tablename__ = 'series'
    __table_args__ = (UniqueConstraint('provider', 'provider_series_id', name='uq_series_provider_sid'),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    provider: Mapped[str] = mapped_column(String(32), index=True)
    provider_series_id: Mapped[str] = mapped_column(String(128), index=True)
    title: Mapped[str | None] = mapped_column(Text)
    episode_metadata: Mapped[str | None] = mapped_column(Text)
    cover_url: Mapped[str | None] = mapped_column(Text)
    episode_count: Mapped[int | None] = mapped_column(Integer)
    source_url: Mapped[str | None] = mapped_column(Text)
    translation_style: Mapped[str | None] = mapped_column(Text)
    story_summary: Mapped[str | None] = mapped_column(Text)
    character_glossary: Mapped[str | None] = mapped_column(Text)  # JSON
    relationship_glossary: Mapped[str | None] = mapped_column(Text)  # JSON
    series_metadata: Mapped[str | None] = mapped_column(Text)  # JSON extras
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Episode(Base):
    """One episode of a series. Unique per (series_id, provider_episode_id)."""

    __tablename__ = 'episodes'
    __table_args__ = (UniqueConstraint('series_id', 'provider_episode_id', name='uq_episode_series_ep'),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    series_id: Mapped[str] = mapped_column(String(36), index=True)
    provider_episode_id: Mapped[str] = mapped_column(String(128), index=True)
    episode_number: Mapped[int | None] = mapped_column(Integer, index=True)
    title: Mapped[str | None] = mapped_column(Text)
    episode_metadata: Mapped[str | None] = mapped_column(Text)
    duration: Mapped[float | None] = mapped_column(Float)
    locked: Mapped[bool | None] = mapped_column(Boolean)
    status: Mapped[str] = mapped_column(String(40), default='discovered', index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class EpisodeJob(Base):
    """Phase-1 processing job for one episode (download + ASR). Later phases reuse it."""

    __tablename__ = 'episode_jobs'
    __table_args__ = (UniqueConstraint('episode_id', name='uq_episode_job_episode'),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    episode_id: Mapped[str] = mapped_column(String(36), index=True)
    status: Mapped[str] = mapped_column(String(40), default='queued', index=True)
    current_stage: Mapped[str] = mapped_column(String(40), default='queued', index=True)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    original_path: Mapped[str | None] = mapped_column(Text)
    source_srt_path: Mapped[str | None] = mapped_column(Text)
    vi_srt_path: Mapped[str | None] = mapped_column(Text)
    voice_path: Mapped[str | None] = mapped_column(Text)
    final_path: Mapped[str | None] = mapped_column(Text)

    source_provider: Mapped[str | None] = mapped_column(String(40))
    source_provider_series_id: Mapped[str | None] = mapped_column(String(120))
    source_provider_episode_id: Mapped[str | None] = mapped_column(String(120))
    source_type: Mapped[str | None] = mapped_column(String(20))
    source_quality: Mapped[str | None] = mapped_column(String(20))
    source_fallback_count: Mapped[int] = mapped_column(Integer, default=0)

    source_language: Mapped[str | None] = mapped_column(String(16))
    subtitle_cue_count: Mapped[int | None] = mapped_column(Integer)
    playback_type: Mapped[str | None] = mapped_column(String(16))
    quality: Mapped[str | None] = mapped_column(String(16))
    requested_quality: Mapped[str | None] = mapped_column(String(16))
    target_language: Mapped[str | None] = mapped_column(String(16))
    voice: Mapped[str | None] = mapped_column(String(128))
    resolve_seconds: Mapped[float | None] = mapped_column(Float)
    download_seconds: Mapped[float | None] = mapped_column(Float)
    audio_extract_seconds: Mapped[float | None] = mapped_column(Float)
    asr_seconds: Mapped[float | None] = mapped_column(Float)
    asr_provider: Mapped[str | None] = mapped_column(String(32))
    asr_fallback_used: Mapped[bool | None] = mapped_column(Boolean)
    translation_seconds: Mapped[float | None] = mapped_column(Float)
    tts_seconds: Mapped[float | None] = mapped_column(Float)
    tts_timing_warnings: Mapped[int | None] = mapped_column(Integer)
    tts_blocks_total: Mapped[int | None] = mapped_column(Integer)
    tts_blocks_completed: Mapped[int | None] = mapped_column(Integer)
    voice_qa_round: Mapped[int | None] = mapped_column(Integer)
    overflow_blocks_remaining: Mapped[int | None] = mapped_column(Integer)
    render_seconds: Mapped[float | None] = mapped_column(Float)
    voice_duration: Mapped[float | None] = mapped_column(Float)
    youtube_enabled: Mapped[bool | None] = mapped_column(Boolean, default=False)
    youtube_destination_id: Mapped[str | None] = mapped_column(String(36))
    youtube_privacy: Mapped[str | None] = mapped_column(String(16))
    youtube_metadata_mode: Mapped[str | None] = mapped_column(String(16))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(128), index=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class YouTubeDestination(Base):
    """A connected YouTube channel. Multiple channels supported (no singleton)."""

    __tablename__ = 'youtube_destinations'

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    youtube_channel_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    youtube_channel_title: Mapped[str | None] = mapped_column(Text)
    refresh_token_encrypted: Mapped[str | None] = mapped_column(Text)  # Fernet, never plain text
    scope: Mapped[str | None] = mapped_column(Text)  # space-joined granted scopes
    is_active: Mapped[bool | None] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    last_upload_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class YouTubePublication(Base):
    """One upload of an EpisodeJob's final video to a destination channel."""

    __tablename__ = 'youtube_publications'
    __table_args__ = (UniqueConstraint('job_id', 'destination_id', name='uq_pub_job_dest'),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    job_id: Mapped[str] = mapped_column(String(36), index=True)
    destination_id: Mapped[str] = mapped_column(String(36), index=True)
    youtube_video_id: Mapped[str | None] = mapped_column(String(32), index=True)
    youtube_url: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    episode_metadata: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[str | None] = mapped_column(Text)  # JSON list[str]
    privacy: Mapped[str | None] = mapped_column(String(16), default='public')
    upload_status: Mapped[str] = mapped_column(String(32), default='queued', index=True)
    upload_progress: Mapped[int] = mapped_column(Integer, default=0)
    upload_attempts: Mapped[int] = mapped_column(Integer, default=0)
    youtube_processing_status: Mapped[str | None] = mapped_column(String(32))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class WorkerHeartbeat(Base):
    __tablename__ = 'worker_heartbeats'

    worker_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    hostname: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), default='idle')
    current_job_id: Mapped[str | None] = mapped_column(String(36), index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
