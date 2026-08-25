from dataclasses import replace

from app.config import load_settings
from app.web_config import (
    load_web_config,
    normalize_web_config,
    runtime_settings,
    save_web_config,
)


def test_web_config_is_persisted_in_local_database(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'config.db'}"

    initial = load_web_config(database_url)
    assert initial["configured"] is False
    assert initial["map_provider"] == "google"

    saved = save_web_config(
        database_url,
        {
            "max_price_usd": 55_000,
            "sources": ["realt", "unknown"],
            "schedule_interval_hours": 4,
            "map_provider": "yandex",
        },
    )

    assert saved["configured"] is True
    assert load_web_config(database_url)["max_price_usd"] == 55_000
    assert load_web_config(database_url)["sources"] == ["realt"]
    assert load_web_config(database_url)["map_provider"] == "yandex"


def test_unknown_map_provider_falls_back_to_google() -> None:
    assert normalize_web_config({"map_provider": "unknown"})["map_provider"] == "google"


def test_runtime_settings_apply_ui_filters_to_sources(tmp_path) -> None:
    base = replace(
        load_settings(),
        database_url=f"sqlite:///{tmp_path / 'runtime.db'}",
    )
    config = normalize_web_config(
        {
            "max_price_usd": 52_000,
            "min_area_sotok": 8,
            "max_area_sotok": 18,
            "max_distance_km": 44,
        }
    )

    settings = runtime_settings(config, base)

    assert settings.sheets_enabled is False
    assert settings.profile.max_price_usd == 52_000
    assert any("prc=r%3A0%2C52000" in url for url in settings.kufar_search_urls)
    assert any("saa=r%3A8%2C18" in url for url in settings.kufar_search_urls)
    assert any("dr=r%3A0%2C44" in url for url in settings.kufar_search_urls)
