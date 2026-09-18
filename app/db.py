from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


connect_args = {'check_same_thread': False} if settings.database_url.startswith('sqlite') else {}
engine = create_engine(settings.database_url, pool_pre_ping=True, future=True, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    # Additive migrations only (never drop: resume must survive restarts).
    # New EpisodeJob progress columns (sqlite create_all does NOT add columns
    # to existing tables, so backfill explicitly on both backends).
    _sqlite_backfill_episode_job_columns()
    if not settings.database_url.startswith('sqlite'):
        from sqlalchemy import text

        stmts = [
            'ALTER TABLE jobs ADD COLUMN IF NOT EXISTS asr_provider VARCHAR(32)',
            'ALTER TABLE jobs ADD COLUMN IF NOT EXISTS asr_started_at TIMESTAMPTZ',
            'ALTER TABLE jobs ADD COLUMN IF NOT EXISTS asr_completed_at TIMESTAMPTZ',
            'ALTER TABLE jobs ADD COLUMN IF NOT EXISTS asr_processing_seconds DOUBLE PRECISION',
            'ALTER TABLE jobs ADD COLUMN IF NOT EXISTS asr_fallback_used BOOLEAN',
            'ALTER TABLE jobs ADD COLUMN IF NOT EXISTS vi_local_path TEXT',
            'ALTER TABLE jobs ADD COLUMN IF NOT EXISTS vi_storage_key TEXT',
            'ALTER TABLE jobs ADD COLUMN IF NOT EXISTS vi_cue_count INTEGER',
            'ALTER TABLE jobs ADD COLUMN IF NOT EXISTS translation_provider VARCHAR(64)',
            'ALTER TABLE jobs ADD COLUMN IF NOT EXISTS translation_model VARCHAR(128)',
            'ALTER TABLE jobs ADD COLUMN IF NOT EXISTS translation_batches INTEGER',
            'ALTER TABLE jobs ADD COLUMN IF NOT EXISTS translation_seconds DOUBLE PRECISION',
            'ALTER TABLE jobs ADD COLUMN IF NOT EXISTS translation_primary_model VARCHAR(128)',
            'ALTER TABLE jobs ADD COLUMN IF NOT EXISTS translation_fallback_model VARCHAR(128)',
            'ALTER TABLE jobs ADD COLUMN IF NOT EXISTS translation_primary_batches INTEGER',
            'ALTER TABLE jobs ADD COLUMN IF NOT EXISTS translation_fallback_batches INTEGER',
            'ALTER TABLE jobs ADD COLUMN IF NOT EXISTS translation_failed_batches INTEGER',
            'ALTER TABLE jobs ADD COLUMN IF NOT EXISTS translation_retries INTEGER',
            'ALTER TABLE jobs ADD COLUMN IF NOT EXISTS translation_started_at TIMESTAMPTZ',
            'ALTER TABLE jobs ADD COLUMN IF NOT EXISTS translation_completed_at TIMESTAMPTZ',
            'ALTER TABLE jobs ADD COLUMN IF NOT EXISTS tts_provider VARCHAR(32)',
            'ALTER TABLE jobs ADD COLUMN IF NOT EXISTS tts_voice VARCHAR(128)',
            'ALTER TABLE jobs ADD COLUMN IF NOT EXISTS tts_clip_count INTEGER',
            'ALTER TABLE jobs ADD COLUMN IF NOT EXISTS tts_seconds DOUBLE PRECISION',
            'ALTER TABLE jobs ADD COLUMN IF NOT EXISTS tts_timing_warnings INTEGER',
            'ALTER TABLE jobs ADD COLUMN IF NOT EXISTS voice_local_path TEXT',
            'ALTER TABLE jobs ADD COLUMN IF NOT EXISTS voice_storage_key TEXT',
            'ALTER TABLE jobs ADD COLUMN IF NOT EXISTS voice_duration_seconds DOUBLE PRECISION',
            'ALTER TABLE jobs ADD COLUMN IF NOT EXISTS phase2_started_at TIMESTAMPTZ',
            'ALTER TABLE jobs ADD COLUMN IF NOT EXISTS phase2_completed_at TIMESTAMPTZ',
            'ALTER TABLE jobs ADD COLUMN IF NOT EXISTS phase2_seconds DOUBLE PRECISION',
            'ALTER TABLE series ADD COLUMN IF NOT EXISTS cover_url TEXT',
            'ALTER TABLE series ADD COLUMN IF NOT EXISTS episode_count INTEGER',
            'ALTER TABLE series ADD COLUMN IF NOT EXISTS translation_style VARCHAR(32)',
            'ALTER TABLE series ADD COLUMN IF NOT EXISTS story_summary TEXT',
            'ALTER TABLE series ADD COLUMN IF NOT EXISTS character_glossary TEXT',
            'ALTER TABLE series ADD COLUMN IF NOT EXISTS relationship_glossary TEXT',
            'ALTER TABLE episode_jobs ADD COLUMN IF NOT EXISTS original_path TEXT',
            'ALTER TABLE episode_jobs ADD COLUMN IF NOT EXISTS source_srt_path TEXT',
            'ALTER TABLE episode_jobs ADD COLUMN IF NOT EXISTS vi_srt_path TEXT',
            'ALTER TABLE episode_jobs ADD COLUMN IF NOT EXISTS voice_path TEXT',
            'ALTER TABLE episode_jobs ADD COLUMN IF NOT EXISTS final_path TEXT',
            'ALTER TABLE episode_jobs ADD COLUMN IF NOT EXISTS source_language VARCHAR(16)',
            'ALTER TABLE episode_jobs ADD COLUMN IF NOT EXISTS asr_fallback_used BOOLEAN',
            'ALTER TABLE episode_jobs ADD COLUMN IF NOT EXISTS translation_seconds DOUBLE PRECISION',
            'ALTER TABLE episode_jobs ADD COLUMN IF NOT EXISTS tts_seconds DOUBLE PRECISION',
            'ALTER TABLE episode_jobs ADD COLUMN IF NOT EXISTS tts_timing_warnings INTEGER',
            'ALTER TABLE episode_jobs ADD COLUMN IF NOT EXISTS requested_quality VARCHAR(16)',
            'ALTER TABLE episode_jobs ADD COLUMN IF NOT EXISTS target_language VARCHAR(16)',
            'ALTER TABLE episode_jobs ADD COLUMN IF NOT EXISTS voice VARCHAR(128)',
            'ALTER TABLE episode_jobs ADD COLUMN IF NOT EXISTS render_seconds DOUBLE PRECISION',
            'ALTER TABLE episode_jobs ADD COLUMN IF NOT EXISTS voice_duration DOUBLE PRECISION',
            'ALTER TABLE episode_jobs ADD COLUMN IF NOT EXISTS tts_blocks_total INTEGER',
            'ALTER TABLE episode_jobs ADD COLUMN IF NOT EXISTS tts_blocks_completed INTEGER',
            'ALTER TABLE episode_jobs ADD COLUMN IF NOT EXISTS voice_qa_round INTEGER',
            'ALTER TABLE episode_jobs ADD COLUMN IF NOT EXISTS overflow_blocks_remaining INTEGER',
            'ALTER TABLE episode_jobs ADD COLUMN IF NOT EXISTS youtube_enabled BOOLEAN',
            'ALTER TABLE episode_jobs ADD COLUMN IF NOT EXISTS youtube_destination_id VARCHAR(36)',
            'ALTER TABLE episode_jobs ADD COLUMN IF NOT EXISTS youtube_privacy VARCHAR(16)',
            'ALTER TABLE episode_jobs ADD COLUMN IF NOT EXISTS youtube_metadata_mode VARCHAR(16)',
        ]
        with engine.begin() as conn:
            for stmt in stmts:
                conn.execute(text(stmt))
            conn.execute(text(
                'ALTER TABLE cue_states ADD COLUMN IF NOT EXISTS cps DOUBLE PRECISION'))
            conn.execute(text(
                "ALTER TABLE cue_states ADD COLUMN IF NOT EXISTS tts_status VARCHAR(16)"))
            # Widen tts_status for values like 'needs_regeneration'.
            conn.execute(text(
                "ALTER TABLE cue_states ALTER COLUMN tts_status TYPE VARCHAR(32)"))
            conn.execute(text(
                'ALTER TABLE cue_states ADD COLUMN IF NOT EXISTS qa_class VARCHAR(16)'))
            conn.execute(text(
                'ALTER TABLE cue_states ADD COLUMN IF NOT EXISTS final_tts_ms INTEGER'))
            conn.execute(text(
                'ALTER TABLE cue_states ADD COLUMN IF NOT EXISTS manual_review_required BOOLEAN'))


def _sqlite_backfill_episode_job_columns() -> None:
    """ADD COLUMN backfill for sqlite (create_all skips existing tables)."""
    if not settings.database_url.startswith('sqlite'):
        return
    from sqlalchemy import inspect, text

    wanted = {
        'tts_blocks_total': 'INTEGER',
        'tts_blocks_completed': 'INTEGER',
        'voice_qa_round': 'INTEGER',
        'overflow_blocks_remaining': 'INTEGER',
        'youtube_enabled': 'BOOLEAN',
        'youtube_destination_id': 'VARCHAR(36)',
        'youtube_privacy': 'VARCHAR(16)',
        'youtube_metadata_mode': 'VARCHAR(16)',
    }
    try:
        existing = {c['name'] for c in inspect(engine).get_columns('episode_jobs')}
    except Exception:
        return  # table genuinely missing; create_all above already handled it
    missing = {k: v for k, v in wanted.items() if k not in existing}
    if not missing:
        return
    with engine.begin() as conn:
        for name, ddl in missing.items():
            conn.execute(text(f'ALTER TABLE episode_jobs ADD COLUMN {name} {ddl}'))
