from dataclasses import replace

from fastapi.testclient import TestClient

from app.config import load_settings
from app.db import init_db, make_engine, make_session_factory, session_scope
from app.models import ListingModel, LocationProfileModel
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
            direction="Логойское",
            price_usd=20_000,
            area_sotok=10,
            distance_mkad_km=22,
            latitude=53.954321,
            longitude=27.564321,
            electricity_raw="20 кВт на участке",
            electricity_kw=20,
            gas_raw="по улице",
            water_raw="центральный водопровод",
            sewerage_raw="септик",
            purpose="для ведения личного подсобного хозяйства (ЛПХ)",
            ownership_raw="частная собственность",
            status="MATCH",
            score=91,
            content_hash="hash",
            raw_payload={"location_key": "village:novoselki"},
        )
        session.add(listing)
        session.flush()
        session.add(
            LocationProfileModel(
                key="village:novoselki",
                label="Новосёлки",
                kind="village",
                district="Минский район",
                latitude=53.95,
                longitude=27.56,
                listing_count=8,
                eligible_count=4,
                house_count=5,
                premium_house_count=2,
                score=76,
                verdict="Перспективная жилая локация",
                confidence="medium",
                signals=["Есть современная коттеджная застройка"],
                risks=["Не все коммуникации подтверждены"],
            )
        )
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
        assert (
            client.post(
                "/api/public/widget/statuses",
                json={"references": ["LP-0000000000"]},
            ).status_code
            == 401
        )
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
        assert client.get("/api/public/widget/options").json() == {
            "directions": ["Логойское"]
        }
        assert (
            client.get(
                "/api/public/widget/preview",
                params={"q": "Участок в Новосёлках"},
            ).json()["total"]
            == 0
        )

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
                "water": True,
                "sewerage": True,
            },
        )
        assert catalog.status_code == 200
        assert catalog.json()["total"] == 1
        public_item = catalog.json()["items"][0]
        assert public_item["reference"].startswith("LP-")
        assert public_item["title"] == "Участок 10 сот. в районе Новосёлки"
        assert public_item["electricity"] == "20 кВт"
        assert public_item["water"] == "Центральная"
        assert public_item["sewerage"] == "Септик"
        assert public_item["direction"] == "Логойское"
        assert public_item["purpose"] == "Личное подсобное хозяйство (ЛПХ)"
        assert public_item["ownership"] == "Частная собственность"
        assert public_item["location_score"] == 76
        assert public_item["latitude"] == 53.95
        assert public_item["longitude"] == 27.56
        assert public_item["coordinates_approximate"] is True
        assert 0 < public_item["match_score"] <= 100
        assert {
            "id",
            "source",
            "url",
            "description",
            "original_score",
        }.isdisjoint(public_item)

        statuses = client.post(
            "/api/public/widget/statuses",
            headers=headers,
            json={"references": [public_item["reference"], "LP-0000000000"]},
        )
        assert statuses.status_code == 200
        assert statuses.json()["items"][0]["reference"] == public_item["reference"]
        assert statuses.json()["items"][0]["active"] is True
        assert statuses.json()["items"][0]["last_seen_at"]
        assert statuses.json()["items"][1] == {
            "reference": "LP-0000000000",
            "active": False,
            "last_seen_at": None,
        }

        admin_item = client.get("/api/listings").json()["items"][0]
        assert admin_item["public_ref"] == public_item["reference"]
        assert admin_item["title"] == "Участок в Новосёлках"
        assert admin_item["url"] == "https://re.kufar.by/vi/42"

        broader_budget = client.get(
            "/api/public/widget/listings",
            headers=headers,
            params={
                "q": "Новосёлки",
                "max_price_usd": 40_000,
                "min_area_sotok": 9,
                "max_area_sotok": 11,
                "max_distance_km": 30,
                "electricity": True,
                "gas": True,
                "water": True,
                "sewerage": True,
            },
        ).json()["items"][0]
        assert broader_budget["match_score"] > public_item["match_score"]

        interest = client.post(
            "/api/public/widget/interests",
            headers=headers,
            json={
                "reference": public_item["reference"],
                "search_params": {"max_price_usd": "25000"},
                "source_page": "https://example.tilda.ws/plots",
            },
        )
        assert interest.status_code == 200
        assert interest.json()["message"] == "Запрос передан специалисту"

        missing_interest = client.post(
            "/api/public/widget/interests",
            headers=headers,
            json={"reference": "LP-0000000000"},
        )
        assert missing_interest.status_code == 404

        leads = client.get("/api/widget/leads").json()
        assert leads["total"] == 1
        assert leads["requests_total"] == 1
        assert leads["items"][0]["phone"] == "+375291234567"
        assert leads["items"][0]["interests"][0]["listing_id"] == listing_id
        assert leads["items"][0]["interests"][0]["reference"] == public_item["reference"]

        engine = make_engine(settings.database_url)
        factory = make_session_factory(engine)
        with session_scope(factory) as session:
            session.get(ListingModel, listing_id).active = False
        engine.dispose()

        archived_status = client.post(
            "/api/public/widget/statuses",
            headers=headers,
            json={"references": [public_item["reference"]]},
        )
        assert archived_status.status_code == 200
        assert archived_status.json()["items"][0]["active"] is False
        assert (
            client.post(
                "/api/public/widget/interests",
                headers=headers,
                json={"reference": public_item["reference"]},
            ).status_code
            == 404
        )
        assert (
            leads["items"][0]["interests"][0]["search_params"]["request_type"]
            == "listing_consultation"
        )


def test_admin_password_does_not_block_public_widget(tmp_path) -> None:
    settings = replace(_settings(tmp_path), admin_password="very-secret")

    with TestClient(create_app(settings)) as client:
        blocked = client.get("/", follow_redirects=False)
        assert blocked.status_code == 303
        assert blocked.headers["location"].startswith("/login?next=")
        assert client.get("/api/widget/leads").status_code == 401
        assert client.get("/login").status_code == 200
        failed_login = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "wrong"},
        )
        assert failed_login.status_code == 401
        logged_in = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "very-secret"},
        )
        assert logged_in.status_code == 200
        assert logged_in.cookies.get("lpf_admin_session")
        assert client.get("/").status_code == 200
        assert client.post("/api/auth/logout").status_code == 200
        assert client.get("/api/widget/leads").status_code == 401
        widget_demo = client.get("/widget-demo")
        assert widget_demo.status_code == 200
        assert "ВАША КОМПАНИЯ" in widget_demo.text
        assert "ЛИДЕР СТРОЙ" not in widget_demo.text
        widget_script = client.get("/static/landplotfinder-widget.js")
        assert widget_script.status_code == 200
        assert 'company: script.dataset.companyName || "ВАША КОМПАНИЯ"' in widget_script.text
        service_worker = client.get("/sw.js")
        assert service_worker.status_code == 200
        assert service_worker.headers["cache-control"] == "no-store, max-age=0"
        assert client.get("/health").status_code == 200
        assert client.get("/", auth=("admin", "very-secret")).status_code == 200
