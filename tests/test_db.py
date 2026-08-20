from app.db import normalize_database_url


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
