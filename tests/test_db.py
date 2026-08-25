from sqlalchemy import select, text

from app.db import (
    init_db,
    make_engine,
    make_session_factory,
    normalize_database_url,
    session_scope,
)
from app.models import ListingModel, ListingProfileModel


def test_railway_postgres_url_uses_installed_psycopg_driver() -> None:
    assert (
        normalize_database_url("postgresql://user:pass@host:5432/db")
        == "postgresql+psycopg://user:pass@host:5432/db"
    )
    assert (
        normalize_database_url("postgres://user:pass@host:5432/db")
        == "postgresql+psycopg://user:pass@host:5432/db"
    )


def test_sqlite_url_is_unchanged() -> None:
    assert normalize_database_url("sqlite:///landplots.db") == "sqlite:///landplots.db"


def test_init_db_backfills_existing_listings_into_default_profile(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'legacy.db'}"
    engine = make_engine(database_url)
    init_db(engine)
    factory = make_session_factory(engine)
    with session_scope(factory) as session:
        session.add(
            ListingModel(
                source="legacy",
                source_id="one",
                canonical_url="https://example.test/one",
                title="Старое объявление",
                status="MATCH",
                score=75,
                reasons=["Подходит"],
                content_hash="legacy-one",
            )
        )
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE listing_profiles"))

    init_db(engine)
    factory = make_session_factory(engine)
    with session_scope(factory) as session:
        profile = session.scalar(select(ListingProfileModel))
        assert profile is not None
        assert profile.profile_id == "default"
        assert profile.score == 75
        assert profile.reasons == ["Подходит"]
    engine.dispose()
