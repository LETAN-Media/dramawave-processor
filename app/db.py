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
    # Lightweight additive migration for existing deployments (no data loss).
    if not settings.database_url.startswith('sqlite'):
        from sqlalchemy import text

        stmts = [
            'ALTER TABLE jobs ADD COLUMN IF NOT EXISTS asr_provider VARCHAR(32)',
            'ALTER TABLE jobs ADD COLUMN IF NOT EXISTS asr_started_at TIMESTAMPTZ',
            'ALTER TABLE jobs ADD COLUMN IF NOT EXISTS asr_completed_at TIMESTAMPTZ',
            'ALTER TABLE jobs ADD COLUMN IF NOT EXISTS asr_processing_seconds DOUBLE PRECISION',
            'ALTER TABLE jobs ADD COLUMN IF NOT EXISTS asr_fallback_used BOOLEAN',
        ]
        with engine.begin() as conn:
            for stmt in stmts:
                conn.execute(text(stmt))
