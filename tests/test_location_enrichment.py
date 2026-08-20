from app.domain import LocationProfile, NormalizedListing
from app.location_enrichment import _match_location_key, _parse_osm_response


def profile() -> LocationProfile:
    return LocationProfile(
        key="place:ляховщина:минский",
        label="д. Ляховщина",
        kind="Деревня",
        district="Минский район",
        latitude=53.9500,
        longitude=27.4500,
        listing_count=3,
        eligible_count=2,
        house_count=1,
        premium_house_count=0,
        median_house_price_usd=30_000,
        median_distance_mkad_km=20,
        electricity_share=0.5,
        gas_share=0.5,
        internet_share=0,
        paved_road_share=0.5,
        score=65,
        verdict="Предварительно есть хорошие сигналы",
        confidence="Средняя",
    )


def test_matches_premium_house_by_nearby_coordinates() -> None:
    listing = NormalizedListing(
        source="realt",
        source_id="premium",
        canonical_url="https://example.test/premium",
        title="Коттедж",
        locality="Другое написание",
        latitude=53.951,
        longitude=27.451,
    )

    assert _match_location_key(listing, {profile().key: profile()}) == profile().key


def test_parses_osm_infrastructure_without_double_counting() -> None:
    response = {
        "osm3s": {"timestamp_osm_base": "2026-08-03T00:00:00Z"},
        "elements": [
            {"type": "node", "id": 1, "tags": {"shop": "supermarket", "name": "A"}},
            {
                "type": "node",
                "id": 2,
                "tags": {"highway": "bus_stop", "public_transport": "platform"},
            },
            {"type": "way", "id": 3, "tags": {"amenity": "school", "name": "Школа"}},
            {"type": "node", "id": 4, "tags": {"amenity": "pharmacy"}},
        ],
    }

    value = _parse_osm_response(response)

    assert value["shops"] == 1
    assert value["transport"] == 1
    assert value["education"] == 1
    assert value["healthcare"] == 1
    assert value["examples"]["education"] == ["Школа"]
