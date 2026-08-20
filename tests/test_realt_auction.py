from app.sources.realt_auction import (
    _extract_area_sotok,
    _extract_date,
    _extract_price_byn,
)


def test_extracts_auction_values() -> None:
    text = (
        "Дата 21.08.2099. Земельный участок пл. 0,10 га. "
        "Начальная цена предмета аукциона: 35 450,25 руб."
    )

    assert _extract_date(text).isoformat() == "2099-08-21"
    assert _extract_area_sotok(text) == 10
    assert _extract_price_byn(text) == 35_450.25


def test_prefers_area_inside_discovery_range_when_event_has_multiple_numbers() -> None:
    text = (
        "Участок площадью 0,25 га. "
        "Второй земельный участок площадью 0,099 га."
    )

    assert _extract_area_sotok(text) == 9.9
