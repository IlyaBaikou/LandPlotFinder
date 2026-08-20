from app.integrations.sheet_cleanup import (
    cleanup_candidate,
    hard_limit_reasons,
    row_is_protected,
)


def row(**changes):
    values = [""] * 53
    values[0] = "realt:42"
    values[1] = "Новый"
    values[7] = 20_000
    values[8] = 10
    values[10] = 25
    for column, value in changes.items():
        values[int(column) - 1] = value
    return values


def test_identifies_each_known_hard_limit_violation() -> None:
    assert hard_limit_reasons(row(**{"8": 40_001})) == ["price"]
    assert hard_limit_reasons(row(**{"9": 8.99})) == ["area"]
    assert hard_limit_reasons(row(**{"11": 30.01})) == ["distance"]


def test_boundaries_are_kept_and_unknown_values_are_removed() -> None:
    assert hard_limit_reasons(row(**{"8": 40_000, "9": 9, "11": 30})) == []
    assert hard_limit_reasons(row(**{"8": "", "9": "", "11": ""})) == [
        "price_unknown",
        "area_unknown",
        "distance_unknown",
    ]


def test_area_above_existing_upper_bound_is_removed() -> None:
    assert hard_limit_reasons(row(**{"9": 15.01})) == ["area"]


def test_selected_or_manually_rated_rows_are_protected() -> None:
    assert row_is_protected(row(**{"2": "Изучаем"}))
    assert row_is_protected(row(**{"24": 5}))
    assert row_is_protected(row(**{"35": 4}))
    assert row_is_protected(row(**{"36": 3}))


def test_cleanup_candidate_contains_auditable_reasons() -> None:
    candidate = cleanup_candidate(
        row(**{"8": 42_000, "9": 8, "11": 35}),
        row_number=17,
    )

    assert candidate is not None
    assert candidate.row_number == 17
    assert candidate.external_id == "realt:42"
    assert candidate.reasons == ["price", "area", "distance"]


def test_protected_row_is_not_a_cleanup_candidate() -> None:
    assert cleanup_candidate(
        row(**{"2": "К поездке", "11": 35}),
        row_number=7,
    ) is None
