import os
import socket
from pathlib import Path

from app.desktop import (
    app_data_dir,
    configure_desktop_environment,
    find_available_port,
    sqlite_database_url,
)


def test_app_data_dir_uses_native_user_folder() -> None:
    home = Path("/Users/friend")
    assert app_data_dir("Darwin", {}, home) == (
        home / "Library" / "Application Support" / "LandPlotFinder"
    )
    assert app_data_dir(
        "Windows",
        {"LOCALAPPDATA": "C:/Users/friend/AppData/Local"},
        home,
    ) == Path("C:/Users/friend/AppData/Local/LandPlotFinder")
    assert app_data_dir(
        "Linux",
        {"XDG_DATA_HOME": "/tmp/friend-data"},
        home,
    ) == Path("/tmp/friend-data/LandPlotFinder")


def test_sqlite_database_url_is_absolute(tmp_path) -> None:
    assert sqlite_database_url(tmp_path / "finder.db").startswith("sqlite:////")


def test_configure_desktop_environment_creates_private_database_folder(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("GOOGLE_SPREADSHEET_ID", "legacy")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "legacy")
    configure_desktop_environment(tmp_path / "LandPlotFinder")
    assert (tmp_path / "LandPlotFinder").is_dir()
    assert os.environ["DATABASE_URL"].endswith("/LandPlotFinder/landplots.db")
    assert os.environ["WEB_HOST"] == "127.0.0.1"
    assert os.environ["LANDPLOTFINDER_DESKTOP"] == "1"
    assert "GOOGLE_SPREADSHEET_ID" not in os.environ
    assert "GOOGLE_SERVICE_ACCOUNT_JSON" not in os.environ


def test_find_available_port_skips_occupied_port() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
        occupied.bind(("127.0.0.1", 0))
        port = occupied.getsockname()[1]
        assert find_available_port(port, attempts=2) == port + 1
