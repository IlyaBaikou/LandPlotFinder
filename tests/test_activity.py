import json
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import select

from app.activity import ActivityTarget, ListingActivityChecker, listing_page_status
from app.db import init_db, make_engine, make_session_factory, session_scope
from app.models import ListingActivityModel, ListingModel


def _page(data: dict) -> str:
    return (
        '<html><script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(data)
        + "</script></html>"
    )


def test_realt_activity_status() -> None:
    assert listing_page_status(
        "realt", _page({"props": {"pageProps": {"object": {"code": 42}}}})
    ) == "available"
    assert listing_page_status(
        "realt", _page({"props": {"pageProps": {"object": None}}})
    ) == "unavailable"


def test_kufar_activity_status() -> None:
    assert listing_page_status(
        "kufar",
        _page(
            {
                "props": {
                    "initialState": {"adView": {"data": {"ad_id": 42}}}
                }
            }
        ),
    ) == "available"
    assert listing_page_status(
        "kufar",
        _page({"props": {"initialState": {"adView": {"data": None}}}}),
    ) == "unavailable"


def test_unknown_page_is_not_treated_as_removed() -> None:
    assert listing_page_status("realt", "<html>blocked</html>") == "unknown"
    assert listing_page_status("unexpected", _page({"props": {}})) == "unknown"


def test_listing_is_archived_only_after_two_unavailable_checks(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'activity.db'}"
    engine = make_engine(database_url)
    init_db(engine)
    factory = make_session_factory(engine)
    with session_scope(factory) as session:
        listing = ListingModel(
            source="realt",
            source_id="42",
            canonical_url="https://realt.by/sale-plots/object/42/",
            title="Участок",
            status="INTERESTING",
            content_hash="hash",
        )
        session.add(listing)
        session.flush()
        listing_id = listing.id
    engine.dispose()

    checker = ListingActivityChecker(
        SimpleNamespace(database_url=database_url, activity_confirmation_count=2)
    )
    target = ActivityTarget(
        listing_id=listing_id,
        external_id="realt:42",
        source="realt",
        canonical_url="https://realt.by/sale-plots/object/42/",
        last_seen_at=datetime.now(timezone.utc),
        previous_checks=0,
    )

    assert checker._save_results([(target, "unavailable", "404")]) == []
    assert checker._save_results([(target, "unavailable", "404")]) == ["realt:42"]

    engine = make_engine(database_url)
    factory = make_session_factory(engine)
    with session_scope(factory) as session:
        listing = session.get(ListingModel, listing_id)
        activity = session.scalar(
            select(ListingActivityModel).where(
                ListingActivityModel.listing_id == listing_id
            )
        )
        assert listing is not None and listing.active is False
        assert activity is not None and activity.consecutive_unavailable == 2
    engine.dispose()
