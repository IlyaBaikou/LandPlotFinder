from app.config import SearchProfile
from app.domain import MatchStatus, NormalizedListing
from app.filters import evaluate_listing


def listing(**changes) -> NormalizedListing:
    values = {
        "source": "test",
        "source_id": "1",
        "canonical_url": "https://example.test/1",
        "title": "Участок",
        "price_usd": 20_000,
        "area_sotok": 10,
        "distance_mkad_km": 25,
        "electricity_raw": "выделено 20 кВт",
        "electricity_kw": 20,
        "gas_raw": "нет",
        "ownership_raw": "частная собственность",
    }
    values.update(changes)
    return NormalizedListing(**values)


def test_strict_match() -> None:
    result = evaluate_listing(listing(), SearchProfile())

    assert result.status is MatchStatus.MATCH
    assert result.reasons == []


def test_six_kw_and_gas_is_match() -> None:
    result = evaluate_listing(
        listing(
            electricity_raw="электричество 6 кВт",
            electricity_kw=6,
            gas_raw="газ проходит по улице",
        ),
        SearchProfile(),
    )

    assert result.status is MatchStatus.MATCH


def test_missing_power_requires_review() -> None:
    result = evaluate_listing(
        listing(electricity_raw="электричество есть", electricity_kw=None),
        SearchProfile(),
    )

    assert result.status is MatchStatus.REVIEW
    assert any("мощность" in reason for reason in result.reasons)


def test_one_near_miss_is_interesting() -> None:
    result = evaluate_listing(listing(area_sotok=12), SearchProfile())

    assert result.status is MatchStatus.INTERESTING
    assert any("вне диапазона 9–11" in reason for reason in result.reasons)


def test_outside_discovery_envelope_is_rejected() -> None:
    result = evaluate_listing(listing(price_usd=40_001), SearchProfile())

    assert result.status is MatchStatus.REJECT


def test_hard_boundaries_are_inclusive() -> None:
    result = evaluate_listing(
        listing(price_usd=40_000, area_sotok=9, distance_mkad_km=30),
        SearchProfile(),
    )

    assert result.status is MatchStatus.MATCH
    assert not any("выше широкого лимита" in reason for reason in result.reasons)
    assert not any("вне широкого диапазона" in reason for reason in result.reasons)
    assert not any("больше широкого лимита" in reason for reason in result.reasons)


def test_each_new_hard_boundary_rejects() -> None:
    profile = SearchProfile()

    assert evaluate_listing(listing(price_usd=40_001), profile).status is MatchStatus.REJECT
    assert evaluate_listing(listing(area_sotok=8.99), profile).status is MatchStatus.REJECT
    assert evaluate_listing(listing(distance_mkad_km=30.01), profile).status is MatchStatus.REJECT


def test_unknown_hard_values_are_rejected() -> None:
    profile = SearchProfile()

    assert evaluate_listing(listing(price_usd=None), profile).status is MatchStatus.REJECT
    assert evaluate_listing(listing(area_sotok=None), profile).status is MatchStatus.REJECT
    assert evaluate_listing(listing(distance_mkad_km=None), profile).status is MatchStatus.REJECT


def test_source_warning_is_preserved() -> None:
    result = evaluate_listing(
        listing(reasons=["Не удалось загрузить детали источника"]),
        SearchProfile(),
    )

    assert result.status is MatchStatus.REVIEW
    assert result.reasons[0] == "Не удалось загрузить детали источника"


def test_auction_is_never_silent_match() -> None:
    result = evaluate_listing(
        listing(title="Аукцион по продаже земельного участка"),
        SearchProfile(),
    )

    assert result.status is MatchStatus.REVIEW
    assert result.raw_payload["is_auction"] is True
    assert any("стартовой" in reason for reason in result.reasons)


def test_known_non_minsk_region_is_rejected() -> None:
    result = evaluate_listing(
        listing(raw_payload={"outside_target_region": True}),
        SearchProfile(),
    )

    assert result.status is MatchStatus.REJECT
    assert any("вне Минской области" in reason for reason in result.reasons)


def test_internet_is_classified_without_becoming_a_hard_filter() -> None:
    with_internet = evaluate_listing(
        listing(description="На улице доступно оптоволокно GPON"),
        SearchProfile(),
    )
    without_internet = evaluate_listing(
        listing(description="Интернета нет"),
        SearchProfile(),
    )

    assert with_internet.status is MatchStatus.MATCH
    assert with_internet.internet_raw == "Оптика / проводной"
    assert without_internet.status is MatchStatus.REVIEW
    assert without_internet.internet_raw == "Нет"
    assert any("мобильное покрытие" in reason for reason in without_internet.reasons)


def test_missing_utilities_are_enriched_from_description() -> None:
    result = evaluate_listing(
        listing(
            description=(
                "Электричество 15 кВт подведено на участок. "
                "Газ проходит по улице. Есть скважина и септик."
            ),
            electricity_raw=None,
            electricity_kw=None,
            gas_raw=None,
            water_raw=None,
            sewerage_raw=None,
        ),
        SearchProfile(),
    )

    assert result.electricity_kw == 15
    assert result.electricity_raw == "На участке / подключено"
    assert result.gas_raw == "По улице / рядом"
    assert result.water_raw == "Скважина"
    assert result.sewerage_raw == "Септик"
    assert result.status is MatchStatus.MATCH


def test_missing_distance_is_calculated_from_coordinates() -> None:
    result = evaluate_listing(
        listing(
            distance_mkad_km=None,
            latitude=54.002,
            longitude=27.675,
        ),
        SearchProfile(),
    )

    assert result.distance_mkad_km == 5.6
    assert "контура МКАД" in result.evidence["distance"]
