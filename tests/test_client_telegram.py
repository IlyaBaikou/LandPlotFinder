from dataclasses import replace
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from sqlalchemy import select

from app.client_telegram import create_invite, process_client_update, send_daily_digests
from app.config import load_settings
from app.db import init_db, make_engine, make_session_factory, session_scope
from app.models import (
    ListingModel,
    ListingProfileModel,
    WidgetLeadModel,
    WidgetTelegramDeliveryModel,
    WidgetTelegramInviteModel,
    WidgetTelegramSubscriptionModel,
)


def _database(tmp_path):
    settings = replace(
        load_settings(),
        database_url=f"sqlite:///{tmp_path / 'client_bot.db'}",
        widget_client_bot_username="wormiefinder_bot",
        widget_client_bot_token="test-token",
    )
    engine = make_engine(settings.database_url)
    init_db(engine)
    return settings, engine, make_session_factory(engine)


def _listing(session, source_id, price=20_000, first_seen_at=None):
    listing = ListingModel(
        source="kufar",
        source_id=source_id,
        canonical_url=f"https://re.kufar.by/vi/{source_id}",
        title=f"Участок {source_id} в Новосёлках",
        locality="Новосёлки",
        price_usd=price,
        area_sotok=10,
        distance_mkad_km=20,
        status="MATCH",
        score=90,
        content_hash=source_id,
        active=True,
        first_seen_at=first_seen_at or datetime.now(timezone.utc),
        last_seen_at=datetime.now(timezone.utc),
    )
    session.add(listing)
    session.flush()
    session.add(
        ListingProfileModel(listing_id=listing.id, profile_id="default", status="MATCH", score=90)
    )
    return listing


def test_opt_in_daily_digest_and_stop(tmp_path, monkeypatch):
    settings, engine, factory = _database(tmp_path)
    sent = []
    monkeypatch.setattr(
        "app.client_telegram._send",
        lambda _token, chat_id, text, **extras: sent.append((chat_id, text, extras)),
    )
    monkeypatch.setattr("app.client_telegram._answer", lambda *_args: None)
    criteria = {
        "q": "Новосёлки",
        "max_price_usd": "25000",
        "min_area_sotok": "9",
        "max_area_sotok": "11",
        "max_distance_km": "30",
        "profile_id": "default",
    }
    with session_scope(factory) as session:
        lead = WidgetLeadModel(phone="+375291234567")
        session.add(lead)
        session.flush()
        old = _listing(
            session,
            "old",
            first_seen_at=datetime.now(timezone.utc) - timedelta(days=2),
        )
        url = create_invite(session, lead.id, criteria, "wormiefinder_bot")
        assert url.startswith("https://t.me/wormiefinder_bot?start=")
        assert "375" not in url
    token = parse_qs(urlparse(url).query)["start"][0]

    with session_scope(factory) as session:
        assert process_client_update(
            session,
            "test-token",
            {"message": {"chat": {"id": 99, "type": "private"}, "text": f"/start {token}"}},
        )
        invite_id = session.scalar(select(WidgetTelegramInviteModel.id))
    assert "Подписаться" in sent[-1][2]["reply_markup"]["inline_keyboard"][0][0]["text"]

    with session_scope(factory) as session:
        process_client_update(
            session,
            "test-token",
            {
                "callback_query": {
                    "id": "callback-1",
                    "data": f"client:subscribe:{invite_id}",
                    "message": {"chat": {"id": 99, "type": "private"}},
                }
            },
        )
    assert old.canonical_url in sent[-1][1]
    assert "ЛидерСтрой" in sent[-1][1]
    with session_scope(factory) as session:
        subscription = session.scalar(select(WidgetTelegramSubscriptionModel))
        assert subscription.status == "active"
        assert subscription.criteria == criteria
        assert session.scalar(select(WidgetTelegramDeliveryModel.listing_id)) == old.id
        _listing(session, "new")
        _listing(session, "too-expensive", price=40_000)

    assert send_daily_digests(settings) == 1
    assert "https://re.kufar.by/vi/new" in sent[-1][1]
    assert "too-expensive" not in sent[-1][1]
    assert send_daily_digests(settings) == 0
    with session_scope(factory) as session:
        process_client_update(
            session,
            "test-token",
            {"message": {"chat": {"id": 99, "type": "private"}, "text": "/stop"}},
        )
        assert session.scalar(select(WidgetTelegramSubscriptionModel)).status == "stopped"
    assert send_daily_digests(settings) == 0
    engine.dispose()


def test_private_invite_cannot_be_used_by_second_chat(tmp_path, monkeypatch):
    _settings, engine, factory = _database(tmp_path)
    sent = []
    monkeypatch.setattr(
        "app.client_telegram._send",
        lambda _token, chat_id, text, **_extras: sent.append((chat_id, text)),
    )
    with session_scope(factory) as session:
        lead = WidgetLeadModel(phone="+375291234567")
        session.add(lead)
        session.flush()
        url = create_invite(session, lead.id, {"q": "Логойское"}, "wormiefinder_bot")
    token = parse_qs(urlparse(url).query)["start"][0]
    for chat_id in (11, 22):
        with session_scope(factory) as session:
            process_client_update(
                session,
                "test-token",
                {
                    "message": {
                        "chat": {"id": chat_id, "type": "private"},
                        "text": f"/start {token}",
                    }
                },
            )
    assert "другом чате" in sent[-1][1]
    engine.dispose()


def test_one_time_options_do_not_create_subscription(tmp_path, monkeypatch):
    _settings, engine, factory = _database(tmp_path)
    sent = []
    monkeypatch.setattr(
        "app.client_telegram._send",
        lambda _token, chat_id, text, **_extras: sent.append((chat_id, text)),
    )
    monkeypatch.setattr("app.client_telegram._answer", lambda *_args: None)
    with session_scope(factory) as session:
        lead = WidgetLeadModel(phone="+375291234567")
        session.add(lead)
        session.flush()
        _listing(session, "42")
        url = create_invite(session, lead.id, {"profile_id": "default"}, "wormiefinder_bot")
    token = parse_qs(urlparse(url).query)["start"][0]
    with session_scope(factory) as session:
        process_client_update(
            session,
            "test-token",
            {"message": {"chat": {"id": 99, "type": "private"}, "text": f"/start {token}"}},
        )
        invite_id = session.scalar(select(WidgetTelegramInviteModel.id))
    with session_scope(factory) as session:
        process_client_update(
            session,
            "test-token",
            {
                "callback_query": {
                    "id": "callback-2",
                    "data": f"client:once:{invite_id}",
                    "message": {"chat": {"id": 99, "type": "private"}},
                }
            },
        )
        assert session.scalar(select(WidgetTelegramSubscriptionModel.id)) is None
    assert "https://re.kufar.by/vi/42" in sent[-1][1]
    assert "Новые варианты придут" not in sent[-1][1]
    engine.dispose()
