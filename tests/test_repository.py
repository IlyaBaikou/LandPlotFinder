from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.domain import MatchStatus, NormalizedListing
from app.models import Base, ListingModel, ListingSnapshotModel
from app.repository import hydrate_listings, upsert_listing


def listing(price: float = 20_000) -> NormalizedListing:
    return NormalizedListing(
        source="test",
        source_id="1",
        canonical_url="https://example.test/1",
        title="Участок",
        price_usd=price,
        status=MatchStatus.MATCH,
    )


def test_upsert_detects_new_unchanged_and_changed() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        _, is_new, is_changed, pending_released = upsert_listing(session, listing())
        session.commit()
        assert (is_new, is_changed, pending_released) == (True, True, False)

        _, is_new, is_changed, pending_released = upsert_listing(session, listing())
        session.commit()
        assert (is_new, is_changed, pending_released) == (False, False, False)

        changed = listing(18_900)
        _, is_new, is_changed, pending_released = upsert_listing(session, changed)
        session.commit()
        assert (is_new, is_changed, pending_released) == (False, True, False)
        assert changed.raw_payload["notification_changes"] == [
            "цена $20,000 → $18,900"
        ]

        assert session.scalar(select(ListingModel)).price_usd == 18_900
        assert len(session.scalars(select(ListingSnapshotModel)).all()) == 2


def test_small_price_fluctuations_accumulate_against_last_notification() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        upsert_listing(session, listing(20_000))
        session.commit()

        small_change = listing(19_500)
        _, _, is_changed, _ = upsert_listing(session, small_change)
        session.commit()
        assert is_changed is False
        assert "notification_changes" not in small_change.raw_payload

        exact_five_percent = listing(19_000)
        _, _, is_changed, _ = upsert_listing(session, exact_five_percent)
        session.commit()
        assert is_changed is False

        accumulated_change = listing(18_900)
        _, _, is_changed, _ = upsert_listing(session, accumulated_change)
        session.commit()
        assert is_changed is True
        assert accumulated_change.raw_payload["notification_changes"] == [
            "цена $20,000 → $18,900"
        ]


def test_description_enrichment_is_not_a_notifiable_change() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        model, _, _, _ = upsert_listing(session, listing())
        model.last_notified_hash = None
        session.commit()
        enriched = listing()
        enriched.description = "Подробное описание участка и коммуникаций"

        _, is_new, is_changed, pending_released = upsert_listing(session, enriched)
        session.commit()

        assert (is_new, is_changed, pending_released) == (False, False, False)
        assert len(session.scalars(select(ListingSnapshotModel)).all()) == 2


def test_releases_pending_listing_as_a_delayed_new_notification() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    pending = listing()
    pending.raw_payload["telegram_pending_enrichment"] = True

    with Session(engine) as session:
        upsert_listing(session, pending)
        session.commit()
        enriched = listing()
        enriched.raw_payload["detail_loaded"] = True
        enriched.distance_mkad_km = 12.3

        _, is_new, is_changed, pending_released = upsert_listing(session, enriched)
        session.commit()

        assert (is_new, is_changed, pending_released) == (False, False, True)


def test_hydration_preserves_saved_detail_when_scan_has_summary_only() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    detailed = listing()
    detailed.description = "Полное описание"
    detailed.electricity_raw = "20 кВт"
    detailed.raw_payload["detail_loaded"] = True

    with Session(engine) as session:
        upsert_listing(session, detailed)
        session.commit()
        summary = listing()
        summary.description = "Короткая карточка"
        summary.raw_payload["detail_loaded"] = False

        hydrate_listings(session, [summary])

        assert summary.description == "Полное описание"
        assert summary.electricity_raw == "20 кВт"
        assert summary.raw_payload["detail_loaded"] is True


def test_hydration_keeps_preferred_location_on_regular_scan() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    preferred = listing()
    preferred.raw_payload.update(
        {
            "preferred_location": "Логойское направление",
            "search_queue": "preferred",
        }
    )

    with Session(engine) as session:
        upsert_listing(session, preferred)
        session.commit()
        regular_scan = listing()

        hydrate_listings(session, [regular_scan])

        assert regular_scan.raw_payload["preferred_location"] == "Логойское направление"
        assert regular_scan.raw_payload["search_queue"] == "preferred"


def test_hydration_reuses_stable_id_for_high_confidence_republication() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    old = listing()
    old.source_id = "old"
    old.description = "Редкое точное описание участка " * 8
    old.locality = "Палетак"
    old.area_sotok = 10

    with Session(engine) as session:
        upsert_listing(session, old)
        session.commit()
        republished = listing()
        republished.source_id = "new"
        republished.canonical_url = "https://example.test/new"
        republished.description = old.description
        republished.locality = old.locality
        republished.area_sotok = old.area_sotok

        hydrate_listings(session, [republished])

        assert republished.source_id == "old"
        assert republished.canonical_url == "https://example.test/new"
        assert republished.raw_payload["source_aliases"] == ["new"]
        assert republished.raw_payload["relisted_from_external_id"] == "test:old"
