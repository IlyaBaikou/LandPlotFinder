from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.domain import NormalizedListing
from app.locations import (
    attach_location_profiles,
    identify_location,
    rebuild_location_profiles,
)
from app.models import Base, ListingModel, LocationObservationModel


def stored_listing(
    source_id: str,
    *,
    locality: str,
    address: str,
    has_house: bool = False,
    gas: str = "Нет",
) -> ListingModel:
    return ListingModel(
        source="test",
        source_id=source_id,
        canonical_url=f"https://example.test/{source_id}",
        title="Дом" if has_house else "Участок",
        description="Асфальтированный подъезд",
        district="Минский район",
        locality=locality,
        address=address,
        price_usd=30_000 if has_house else 15_000,
        distance_mkad_km=20,
        latitude=53.95,
        longitude=27.45,
        electricity_raw="Есть",
        gas_raw=gas,
        status="REVIEW",
        score=70,
        reasons=[],
        evidence={},
        raw_payload={"has_house": has_house},
        content_hash=f"hash-{source_id}",
        first_seen_at=datetime.now(timezone.utc),
        last_seen_at=datetime.now(timezone.utc),
    )


def test_location_identity_merges_prefixed_and_plain_village_names() -> None:
    plain = NormalizedListing(
        source="realt",
        source_id="1",
        canonical_url="https://example.test/1",
        title="Участок",
        district="Минский район",
        locality="Ляховщина",
    )
    prefixed = NormalizedListing(
        source="kufar",
        source_id="2",
        canonical_url="https://example.test/2",
        title="Участок",
        district="Минский район",
        locality="Центральная ул",
        address="Центральная ул, д. Ляховщина, Минский район, Минская область",
    )

    assert identify_location(plain).key == identify_location(prefixed).key
    assert identify_location(prefixed).label == "д. Ляховщина"


def test_rebuilds_profiles_from_existing_database_and_attaches_them() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                stored_listing(
                    "1",
                    locality="Ляховщина",
                    address="Ляховщина",
                ),
                stored_listing(
                    "2",
                    locality="Центральная ул",
                    address="Центральная ул, д. Ляховщина, Минский район",
                    has_house=True,
                    gas="По улице",
                ),
            ]
        )
        session.commit()

        profiles = rebuild_location_profiles(session)
        session.commit()

    assert len(profiles) == 1
    profile = profiles[0]
    assert profile.listing_count == 2
    assert profile.house_count == 1
    assert profile.gas_share == 0.5
    listing = NormalizedListing(
        source="realt",
        source_id="3",
        canonical_url="https://example.test/3",
        title="Участок",
        district="Минский район",
        locality="Ляховщина",
    )
    attach_location_profiles([listing], {profile.key: profile})

    assert listing.location_score == profile.score
    assert listing.location_label == profile.label


def test_external_observations_improve_location_profile() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        session.add(
            stored_listing(
                "1",
                locality="Ляховщина",
                address="д. Ляховщина, Минский район",
            )
        )
        session.add_all(
            [
                LocationObservationModel(
                    location_key="place:ляховщина:минский",
                    source="realt",
                    source_id="premium-1",
                    signal_type="premium_house",
                    canonical_url="https://example.test/premium-1",
                    price_usd=120_000,
                    payload={"title": "Коттедж"},
                    expires_at=now + timedelta(days=30),
                ),
                LocationObservationModel(
                    location_key="place:ляховщина:минский",
                    source="openstreetmap",
                    source_id="place:ляховщина:минский",
                    signal_type="osm_infrastructure",
                    payload={
                        "shops": 2,
                        "transport": 3,
                        "education": 1,
                        "healthcare": 0,
                    },
                    expires_at=now + timedelta(days=10),
                ),
            ]
        )
        session.commit()

        profile = rebuild_location_profiles(session)[0]

    assert profile.premium_house_count == 1
    assert profile.median_house_price_usd == 120_000
    assert any("1 домов от $80 000" in value for value in profile.signals)
    assert any("В OSM рядом" in value for value in profile.signals)
    assert "OSM-инфраструктура ещё не проверена" not in profile.risks
