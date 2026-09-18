from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class SeriesOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    provider: str
    provider_series_id: str
    title: str | None = None
    cover_url: str | None = None
    episode_count: int | None = None
    source_url: str | None = None
    created_at: datetime
    updated_at: datetime


class EpisodeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    series_id: str
    provider_episode_id: str
    episode_number: int | None = None
    title: str | None = None
    duration: float | None = None
    locked: bool | None = None
    status: str


class JobAccepted(BaseModel):
    job_id: str
    status: str


class HealthOut(BaseModel):
    ok: bool
    service: str
    source: str = 'dramawave'
    database: bool
    worker: bool
    worker_last_seen_at: datetime | None = None
    dramawave: dict | None = None
    dramawave_api: dict | None = None
    asr: dict | None = None
    translation: dict | None = None
    tts: dict | None = None


class EpisodeJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    episode_id: str
    status: str
    current_stage: str
    progress: int
    original_path: str | None = None
    source_srt_path: str | None = None
    vi_srt_path: str | None = None
    voice_path: str | None = None
    final_path: str | None = None
    source_language: str | None = None
    subtitle_cue_count: int | None = None
    playback_type: str | None = None
    quality: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    attempts: int = 0
    created_at: datetime
    updated_at: datetime


class DramaWaveResolveIn(BaseModel):
    url: str = Field(min_length=8, max_length=2048)


class DramaWaveSearchIn(BaseModel):
    keyword: str = Field(min_length=1, max_length=200)
    limit: int = Field(default=20, ge=1, le=50)


class DramaWaveEpisodeOut(BaseModel):
    episode_number: int
    episode_id: str
    title: str | None = None
    duration: float | None = None
    locked: bool = False
    status: str = 'discovered'


class DramaWaveSeriesOut(BaseModel):
    provider: str = 'dramawave'
    series_id: str
    title: str | None = None
    episode_count: int | None = None
    episodes: list[DramaWaveEpisodeOut] = Field(default_factory=list)


class YouTubeProcessIn(BaseModel):
    enabled: bool = False
    destination_id: str | None = None
    privacy: str | None = None
    metadata_mode: str = 'auto'


class DramaWaveProcessIn(BaseModel):
    from_episode: int = 1
    to_episode: int | None = None
    force: bool = False
    quality: str | None = None
    target_language: str | None = None
    voice: str | None = None
    translation_style: str | None = None
    youtube: YouTubeProcessIn | None = None


class EpisodeProcessIn(BaseModel):
    quality: str | None = None
    target_language: str | None = None
    voice: str | None = None
    force: bool = False
    translation_style: str | None = None
    youtube: YouTubeProcessIn | None = None


class YouTubeDestinationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    youtube_channel_id: str
    youtube_channel_title: str | None = None
    is_active: bool | None = None
    last_upload_at: datetime | None = None
    last_error: str | None = None
    reauth_required: bool = False


class YouTubeUploadIn(BaseModel):
    destination_id: str = Field(min_length=8, max_length=64)
    privacy: str | None = None


class YouTubePublicationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    job_id: str
    destination_id: str
    youtube_video_id: str | None = None
    youtube_url: str | None = None
    title: str | None = None
    privacy: str | None = None
    upload_status: str
    upload_progress: int = 0
    upload_attempts: int = 0
    youtube_processing_status: str | None = None
    published_at: datetime | None = None
    error_code: str | None = None
    error_message: str | None = None


class SeriesProcessOut(BaseModel):
    series_id: str
    jobs: list[str] = Field(default_factory=list)
    skipped: list[int] = Field(default_factory=list)
    status: str = 'queued'
