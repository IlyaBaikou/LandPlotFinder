from __future__ import annotations

from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base


def make_engine(database_url: str) -> Engine:
    database_url = normalize_database_url(database_url)
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    return create_engine(database_url, pool_pre_ping=True, connect_args=connect_args)


def normalize_database_url(database_url: str) -> str:
    if database_url.startswith("postgres://"):
        return database_url.replace("postgres://", "postgresql+psycopg://", 1)
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    return database_url


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)
    _backfill_default_profile(engine)


def _backfill_default_profile(engine: Engine) -> None:
    """Attach databases created before search profiles to the default profile."""
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO listing_profiles (
                    listing_id, profile_id, status, score, reasons,
                    first_seen_at, last_seen_at
                )
                SELECT
                    listings.id, 'default', listings.status, listings.score,
                    listings.reasons, listings.first_seen_at, listings.last_seen_at
                FROM listings
                WHERE NOT EXISTS (
                    SELECT 1 FROM listing_profiles
                    WHERE listing_profiles.listing_id = listings.id
                      AND listing_profiles.profile_id = 'default'
                )
                """
            )
        )


def make_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


@contextmanager
def session_scope(factory: sessionmaker) -> Generator[Session, None, None]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
