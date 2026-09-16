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
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None


class JobAccepted(BaseModel):
    job_id: str
    status: str


class HealthOut(BaseModel):
    ok: bool
    service: str
    database: bool
    worker: bool
    worker_last_seen_at: datetime | None = None
