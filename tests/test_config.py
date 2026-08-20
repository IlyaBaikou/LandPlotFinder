from app.config import _as_urls, load_settings


def test_encoded_filter_commas_do_not_split_urls(monkeypatch) -> None:
    monkeypatch.delenv("KUFAR_SEARCH_URLS", raising=False)
    monkeypatch.delenv("BELTORGI_AUCTION_URLS", raising=False)

    settings = load_settings()

    assert len(settings.kufar_search_urls) == 4
    assert all("prc=r:0%2C40000" in url for url in settings.kufar_search_urls)
    assert all("saa=r:9%2C15" in url for url in settings.kufar_search_urls)
    assert all("dr=r:0%2C30" in url for url in settings.kufar_search_urls)
    assert len(settings.realt_search_urls) == 3
    assert "addressV2=" in settings.realt_search_urls[1]
    assert "priceTo=40000&priceType=840" in settings.realt_search_urls[1]
    assert settings.rlt_auction_urls == [
        "https://rlt.by/aukciony/",
        "https://torgi.rlt.by/aukciony/",
    ]
    assert settings.e_auction_urls == [
        "https://e-auction.by/nedvizhimost/zemelnye_uchastki/"
    ]
    assert settings.beltorgi_auction_urls == []
    assert settings.kufar_detail_delay_seconds == 6
    assert settings.kufar_detail_batch_size == 8
    assert settings.kufar_detail_batch_pause_seconds == 45
    assert settings.kufar_rate_limit_pause_seconds == 60
    assert settings.premium_min_price_usd == 80_000
    assert settings.premium_catalog_refresh_hours == 12
    assert settings.location_osm_batch_size == 12
    assert len(settings.location_osm_endpoints) == 3
    assert "priceFrom=80000" in settings.premium_realt_search_urls[0]
    assert "prc=r:80000%2C500000" in settings.premium_kufar_search_urls[0]
    assert settings.preferred_location_name == "Логойское направление"
    assert len(settings.preferred_realt_search_urls) == 5
    assert len(settings.preferred_kufar_search_urls) == 2
    assert settings.preferred_max_details_per_source == 8
    assert all("cd=v:2" in url for url in settings.preferred_kufar_search_urls)
    assert all("dr=r:0%2C30" in url for url in settings.preferred_kufar_search_urls)
    assert all("saa=r:9%2C15" in url for url in settings.preferred_kufar_search_urls)
    assert all("prc=r:0%2C40000" in url for url in settings.preferred_kufar_search_urls)
    assert settings.profile.max_price_usd == 40_000
    assert settings.profile.discovery_max_price_usd == 40_000
    assert settings.profile.discovery_min_area_sotok == 9
    assert settings.profile.max_distance_km == 30
    assert settings.profile.discovery_max_distance_km == 30


def test_url_list_is_comma_separated() -> None:
    assert _as_urls("https://a.test, https://b.test") == [
        "https://a.test",
        "https://b.test",
    ]
