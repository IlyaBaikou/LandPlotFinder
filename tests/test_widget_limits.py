from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from starlette.requests import Request

from app.config import load_settings
from app.db import init_db, make_engine, make_session_factory, session_scope
from app.models import WidgetRateLimitModel
from app.widget_limits import consume, prune_expired, visitor_ip
from app.widget_service import WidgetError


def test_quota_is_shared_across_sessions_and_expires(tmp_path) -> None:
    settings = replace(load_settings(), database_url=f"sqlite:///{tmp_path / 'limits.db'}")
    engine = make_engine(settings.database_url)
    init_db(engine)
    factory = make_session_factory(engine)
    first_hour = datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc)
    for _ in range(2):
        with session_scope(factory) as session:
            consume(session, settings, "test-hour", "203.0.113.5", 2, 3600, now=first_hour)
    with pytest.raises(WidgetError) as error:
        with session_scope(factory) as session:
            consume(session, settings, "test-hour", "203.0.113.5", 2, 3600, now=first_hour)
    assert error.value.status_code == 429
    with session_scope(factory) as session:
        keys = session.scalars(select(WidgetRateLimitModel.bucket_key)).all()
        assert len(keys) == 1
        assert "203.0.113.5" not in keys[0]
        consume(
            session,
            settings,
            "test-hour",
            "203.0.113.5",
            2,
            3600,
            now=first_hour + timedelta(hours=1),
        )
        prune_expired(session, first_hour + timedelta(hours=2))
    with session_scope(factory) as session:
        assert session.scalars(select(WidgetRateLimitModel.bucket_key)).all() == []
    engine.dispose()


def test_visitor_ip_ignores_untrusted_forwarded_header() -> None:
    plain = Request(
        {
            "type": "http",
            "headers": [(b"x-real-ip", b"198.51.100.8")],
            "client": ("203.0.113.5", 1234),
        }
    )
    railway = Request(
        {
            "type": "http",
            "headers": [
                (b"x-real-ip", b"198.51.100.8"),
                (b"x-railway-request-id", b"request-1"),
            ],
            "client": ("203.0.113.5", 1234),
        }
    )
    assert visitor_ip(plain) == "203.0.113.5"
    assert visitor_ip(railway) == "198.51.100.8"
