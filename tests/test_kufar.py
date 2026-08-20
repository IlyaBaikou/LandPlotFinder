from app.sources.kufar import (
    _money_from_cents,
    _next_page_url,
    _prioritized_detail_ids,
    _rotating_detail_ids,
)


def test_money_from_cents() -> None:
    assert _money_from_cents("4224962") == 42_249.62
    assert _money_from_cents("0") is None


def test_builds_next_page_url_and_preserves_broad_filters() -> None:
    url = _next_page_url(
        "https://re.kufar.by/l/minskij-rajon/kupit/uchastok"
        "?cur=USD&dr=r:0,30&prc=r:0,40000&saa=r:9,15",
        [
            {"label": "self", "num": 1, "token": None},
            {"label": "next", "num": 2, "token": "opaque-token=="},
        ],
    )

    assert url
    assert "dr=r%3A0%2C30" in url
    assert "prc=r%3A0%2C40000" in url
    assert "saa=r%3A9%2C15" in url
    assert "cursor=opaque-token%3D%3D" in url


def test_rotates_kufar_detail_batch_between_schedule_slots() -> None:
    source_ids = [str(value) for value in range(10)]

    first = _rotating_detail_ids(source_ids, 3, slot=0)
    second = _rotating_detail_ids(source_ids, 3, slot=1)

    assert first == ["0", "1", "2"]
    assert second == ["3", "4", "5"]
    assert set(first).isdisjoint(second)


def test_prioritizes_kufar_cards_without_saved_distance() -> None:
    selected = _prioritized_detail_ids(
        ["known-1", "missing-1", "known-2", "missing-2"],
        {"known-1", "missing-1", "known-2", "missing-2"},
        {"known-1", "known-2"},
        set(),
        2,
        slot=0,
    )

    assert selected == {"missing-1", "missing-2"}


def test_prioritizes_new_then_pending_kufar_cards() -> None:
    selected = _prioritized_detail_ids(
        ["old-missing", "pending", "new", "known"],
        {"old-missing", "pending", "known"},
        {"known"},
        {"pending"},
        2,
        slot=0,
    )

    assert selected == {"new", "pending"}
