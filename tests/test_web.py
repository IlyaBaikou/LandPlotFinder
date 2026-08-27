from dataclasses import replace

from fastapi.testclient import TestClient

from app.config import load_settings
from app.db import init_db, make_engine, make_session_factory, session_scope
from app.models import ListingModel
from app.web import create_app
from app.web_config import save_web_config


def _settings(tmp_path):
    return replace(
        load_settings(),
        database_url=f"sqlite:///{tmp_path / 'web.db'}",
        google_spreadsheet_id=None,
        google_service_account_json=None,
        telegram_bot_token=None,
        telegram_chat_id=None,
    )


def _seed_listing(settings) -> int:
    engine = make_engine(settings.database_url)
    init_db(engine)
    factory = make_session_factory(engine)
    with session_scope(factory) as session:
        listing = ListingModel(
            source="kufar",
            source_id="42",
            canonical_url="https://re.kufar.by/vi/42",
            title="Участок в Новосёлках",
            description="Электричество 20 кВт, газ по улице",
            locality="Новосёлки",
            district="Минский район",
            price_usd=20_000,
            area_sotok=10,
            distance_mkad_km=22,
            latitude=53.95,
            longitude=27.56,
            electricity_raw="20 кВт на участке",
            electricity_kw=20,
            gas_raw="по улице",
            status="MATCH",
            score=91,
            content_hash="hash",
        )
        session.add(listing)
        session.flush()
        listing_id = listing.id
    engine.dispose()
    return listing_id


def test_web_setup_and_listing_decision(tmp_path) -> None:
    settings = _settings(tmp_path)
    listing_id = _seed_listing(settings)

    with TestClient(create_app(settings)) as client:
        assert client.get("/").status_code == 200
        assert client.get("/health").json()["ok"] is True
        assert client.get("/api/settings").json()["configured"] is False

        response = client.put(
            "/api/settings",
            json={
                "schedule_enabled": False,
                "schedule_interval_hours": 6,
                "activity_check_enabled": True,
                "map_provider": "yandex",
                "sources": ["realt", "kufar"],
                "target_price_usd": 20_000,
                "max_price_usd": 40_000,
                "min_area_sotok": 9,
                "max_area_sotok": 15,
                "max_distance_km": 30,
                "primary_electricity_kw": 20,
                "secondary_electricity_kw": 6,
                "telegram_enabled": False,
                "telegram_bot_token": "",
                "telegram_chat_id": "",
            },
        )
        assert response.status_code == 200
        assert response.json()["configured"] is True

        catalog = client.get("/api/listings").json()
        assert catalog["total"] == 1
        assert catalog["items"][0]["decision"] == "new"

        response = client.put(
            f"/api/listings/{listing_id}/decision",
            json={"state": "liked", "note": "Заехать в субботу"},
        )
        assert response.status_code == 200
        detail = client.get(f"/api/listings/{listing_id}").json()
        assert detail["decision"] == "liked"
        assert detail["note"] == "Заехать в субботу"

        map_data = client.get("/api/map").json()
        assert map_data["map_provider"] == "yandex"
        assert map_data["map_provider_label"] == "Яндекс Карты"
        points = map_data["items"]
        assert points[0]["decision"] == "liked"
        assert points[0]["map_url"].startswith("https://yandex.ru/maps/")


def test_web_rejects_job_before_setup(tmp_path) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        response = client.post("/api/jobs/scan")
        assert response.status_code == 409


def test_web_profiles_history_routes_health_and_backup(tmp_path) -> None:
    settings = _settings(tmp_path)
    listing_id = _seed_listing(settings)
    save_web_config(settings.database_url, {"map_provider": "yandex"})

    with TestClient(create_app(settings)) as client:
        profiles = client.get("/api/profiles").json()
        assert profiles["active_profile_id"] == "default"

        health = client.get("/api/source-health").json()
        assert {item["status"] for item in health["items"]} == {"never"}

        history = client.get(f"/api/listings/{listing_id}/history")
        assert history.status_code == 200
        assert history.json()["listing_id"] == listing_id

        client.put(
            f"/api/listings/{listing_id}/decision",
            json={"state": "trip", "note": "Посмотреть в субботу"},
        )
        route = client.post(
            "/api/trips/plan",
            json={"listing_ids": [], "max_points_per_route": 4},
        ).json()
        assert route["points"] == 1
        assert route["map_provider"] == "yandex"
        assert route["routes"][0]["url"].startswith("https://yandex.ru/maps/")
        assert route["routes"][0]["items"][0]["id"] == listing_id

        backup = client.get("/api/backups/download")
        assert backup.status_code == 200
        assert backup.content.startswith(b"SQLite format 3\x00")

        created = client.post(
            "/api/profiles",
            json={
                "name": "Дачи",
                "enabled": True,
                "schedule_enabled": False,
                "schedule_interval_hours": 12,
                "sources": ["realt"],
                "target_price_usd": 15_000,
                "max_price_usd": 25_000,
                "min_area_sotok": 6,
                "max_area_sotok": 12,
                "max_distance_km": 40,
                "primary_electricity_kw": 10,
                "secondary_electricity_kw": 5,
            },
        )
        assert created.status_code == 201
        second_id = created.json()["id"]
        assert client.get(f"/api/listings?profile_id={second_id}").json()["total"] == 0
        assert client.delete(f"/api/profiles/{second_id}").status_code == 200


def test_public_widget_phone_search_and_interest(tmp_path) -> None:
    settings = _settings(tmp_path)
    listing_id = _seed_listing(settings)

    with TestClient(create_app(settings)) as client:
        assert client.get("/api/public/widget/listings").status_code == 401
        preview = client.get(
            "/api/public/widget/preview",
            params={
                "q": "Новосёлки",
                "max_price_usd": 25_000,
                "min_area_sotok": 9,
                "max_area_sotok": 11,
                "max_distance_km": 30,
            },
        )
        assert preview.status_code == 200
        assert preview.json() == {"total": 1, "preview_cards": 1}
        assert "items" not in preview.json()

        requested = client.post(
            "/api/public/widget/auth/request-code",
            headers={"Origin": "https://example.tilda.ws"},
            json={
                "phone": "+375 29 123-45-67",
                "consent": True,
                "source_page": "https://example.tilda.ws/plots?utm_source=test",
                "utm": {"utm_source": "test"},
            },
        )
        assert requested.status_code == 200
        assert requested.json()["mode"] == "demo"
        assert len(requested.json()["demo_code"]) == 6
        assert requested.headers["access-control-allow-origin"] == "*"

        verified = client.post(
            "/api/public/widget/auth/verify-code",
            json={
                "phone": "+375291234567",
                "code": requested.json()["demo_code"],
            },
        )
        assert verified.status_code == 200
        assert verified.json()["expires_in_seconds"] == 30 * 24 * 60 * 60
        token = verified.json()["token"]
        headers = {"Authorization": f"Bearer {token}"}

        catalog = client.get(
            "/api/public/widget/listings",
            headers=headers,
            params={
                "q": "Новосёлки",
                "max_price_usd": 25_000,
                "min_area_sotok": 9,
                "max_area_sotok": 11,
                "max_distance_km": 30,
                "electricity": True,
                "gas": True,
            },
        )
        assert catalog.status_code == 200
        assert catalog.json()["total"] == 1
        assert catalog.json()["items"][0]["id"] == listing_id
        assert catalog.json()["items"][0]["electricity_kw"] == 20

        interest = client.post(
            "/api/public/widget/interests",
            headers=headers,
            json={
                "listing_id": listing_id,
                "name": "Илья",
                "search_params": {"max_price_usd": "25000"},
                "source_page": "https://example.tilda.ws/plots",
            },
        )
        assert interest.status_code == 200

        leads = client.get("/api/widget/leads").json()
        assert leads["total"] == 1
        assert leads["items"][0]["phone"] == "+375291234567"
        assert leads["items"][0]["interests"][0]["listing_id"] == listing_id
        assert (
            leads["items"][0]["interests"][0]["search_params"]["contact_name"]
            == "Илья"
        )


def test_admin_password_does_not_block_public_widget(tmp_path) -> None:
    settings = replace(_settings(tmp_path), admin_password="very-secret")

    with TestClient(create_app(settings)) as client:
        assert client.get("/").status_code == 401
        assert client.get("/api/widget/leads").status_code == 401
        assert client.get("/widget-demo").status_code == 200
        assert client.get("/static/landplotfinder-widget.js").status_code == 200
        assert client.get("/health").status_code == 200
        assert client.get("/", auth=("admin", "very-secret")).status_code == 200
