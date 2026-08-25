from app.web_config import (
    delete_search_profile,
    get_profile,
    load_web_config,
    save_search_profile,
    save_web_config,
)


def test_multiple_profiles_keep_independent_filters(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'profiles.db'}"
    save_web_config(database_url, {"max_price_usd": 40_000})
    second = save_search_profile(
        database_url,
        {
            "name": "Дом под снос",
            "max_price_usd": 55_000,
            "min_area_sotok": 12,
            "sources": ["realt"],
            "schedule_interval_hours": 12,
        },
    )

    config = load_web_config(database_url)
    assert len(config["profiles"]) == 2
    assert config["active_profile_id"] == second["id"]
    assert get_profile(config, second["id"])["max_price_usd"] == 55_000
    assert get_profile(config, "default")["max_price_usd"] == 40_000

    saved = delete_search_profile(database_url, second["id"])
    assert saved["active_profile_id"] == "default"
    assert len(saved["profiles"]) == 1
