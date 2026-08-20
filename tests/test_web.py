from dataclasses import replace

from fastapi.testclient import TestClient

from app.config import load_settings
from app.db import init_db, make_engine, make_session_factory, session_scope
from app.models import ListingModel
from app.web import create_app


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

        points = client.get("/api/map").json()["items"]
        assert points[0]["decision"] == "liked"


def test_web_rejects_job_before_setup(tmp_path) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        response = client.post("/api/jobs/scan")
        assert response.status_code == 409
