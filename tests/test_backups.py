from pathlib import Path

from sqlalchemy import func, select

from app.backups import create_backup, database_stats, restore_backup
from app.db import init_db, make_engine, make_session_factory, session_scope
from app.models import ListingModel


def _listing(source_id: str) -> ListingModel:
    return ListingModel(
        source="test",
        source_id=source_id,
        canonical_url=f"https://example.test/{source_id}",
        title=f"Участок {source_id}",
        status="MATCH",
        score=80,
        content_hash=source_id,
    )


def test_sqlite_backup_round_trip(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'finder.db'}"
    engine = make_engine(database_url)
    init_db(engine)
    factory = make_session_factory(engine)
    with session_scope(factory) as session:
        session.add(_listing("one"))
    engine.dispose()

    backup_path = create_backup(database_url)
    backup = backup_path.read_bytes()
    backup_path.unlink()
    assert backup.startswith(b"SQLite format 3\x00")

    engine = make_engine(database_url)
    factory = make_session_factory(engine)
    with session_scope(factory) as session:
        session.add(_listing("two"))
    engine.dispose()
    assert database_stats(database_url)["listings"] == 2

    restored = restore_backup(database_url, backup)
    assert restored["listings"] == 1
    engine = make_engine(database_url)
    factory = make_session_factory(engine)
    with session_scope(factory) as session:
        assert session.scalar(select(func.count()).select_from(ListingModel)) == 1
    engine.dispose()


def test_restore_rejects_non_sqlite_file(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'finder.db'}"
    try:
        restore_backup(database_url, b"not a database")
    except ValueError as exc:
        assert "не резервная копия" in str(exc)
    else:
        raise AssertionError("Invalid backup should be rejected")


def test_database_path_does_not_create_files_for_stats(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'missing.db'}"
    assert database_stats(database_url) == {
        "size_bytes": 0,
        "listings": 0,
        "snapshots": 0,
    }
    assert not Path(tmp_path / "missing.db").exists()
