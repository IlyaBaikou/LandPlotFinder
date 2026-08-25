from __future__ import annotations

import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict

from sqlalchemy.engine import make_url

MAX_BACKUP_BYTES = 250 * 1024 * 1024
REQUIRED_TABLES = {"listings", "service_state", "scan_runs"}


def database_path(database_url: str) -> Path:
    url = make_url(database_url)
    if not url.drivername.startswith("sqlite") or not url.database or url.database == ":memory:":
        raise ValueError("Резервные копии через интерфейс поддерживаются только для SQLite")
    return Path(url.database).expanduser().resolve()


def create_backup(database_url: str) -> Path:
    source_path = database_path(database_url)
    if not source_path.exists():
        raise ValueError("Локальная база ещё не создана")
    handle, temp_name = tempfile.mkstemp(prefix="landplotfinder-", suffix=".sqlite3")
    os.close(handle)
    Path(temp_name).unlink(missing_ok=True)
    try:
        with sqlite3.connect(source_path) as source, sqlite3.connect(temp_name) as target:
            source.backup(target)
        _validate_database(Path(temp_name))
        return Path(temp_name)
    except Exception:
        Path(temp_name).unlink(missing_ok=True)
        raise


def restore_backup(database_url: str, content: bytes) -> Dict[str, int]:
    if len(content) > MAX_BACKUP_BYTES:
        raise ValueError("Файл резервной копии больше 250 МБ")
    if not content.startswith(b"SQLite format 3\x00"):
        raise ValueError("Это не резервная копия LandPlotFinder")
    target_path = database_path(database_url)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix="landplotfinder-restore-", suffix=".sqlite3")
    try:
        with os.fdopen(handle, "wb") as temp_file:
            temp_file.write(content)
        source_path = Path(temp_name)
        _validate_database(source_path)
        with sqlite3.connect(source_path) as source, sqlite3.connect(target_path) as target:
            source.backup(target)
        return database_stats(database_url)
    finally:
        Path(temp_name).unlink(missing_ok=True)


def database_stats(database_url: str) -> Dict[str, int]:
    path = database_path(database_url)
    size = path.stat().st_size if path.exists() else 0
    if not path.exists():
        return {"size_bytes": 0, "listings": 0, "snapshots": 0}
    with sqlite3.connect(path) as connection:
        listings = _count(connection, "listings")
        snapshots = _count(connection, "listing_snapshots")
    return {"size_bytes": size, "listings": listings, "snapshots": snapshots}


def backup_filename() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"landplotfinder-backup-{stamp}.sqlite3"


def _validate_database(path: Path) -> None:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        check = connection.execute("PRAGMA quick_check").fetchone()
        if not check or check[0] != "ok":
            raise ValueError("Резервная копия повреждена")
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if not REQUIRED_TABLES.issubset(tables):
            raise ValueError("В файле нет обязательных таблиц LandPlotFinder")


def _count(connection: sqlite3.Connection, table: str) -> int:
    tables = {
        row[0]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    if table not in tables:
        return 0
    return int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
