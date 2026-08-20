from app.catalog import _listing_from_row


def test_catalog_row_becomes_listing() -> None:
    row = [""] * 53
    row[0] = "kufar:42"
    row[1] = "Новый"
    row[4] = "https://re.kufar.by/vi/42"
    row[5] = "Минский район"
    row[6] = "Новосёлки"
    row[7] = "19000"
    row[8] = "10"
    row[10] = "24.5"
    row[21] = "53.9, 27.5"
    row[33] = "84"
    row[39] = "Участок"
    row[48] = "72"

    listing = _listing_from_row(row)

    assert listing is not None
    assert listing.external_id == "kufar:42"
    assert listing.score == 84
    assert listing.location_score == 72


def test_catalog_skips_already_selected_row() -> None:
    row = [""] * 53
    row[0] = "realt:7"
    row[1] = "Изучаем"
    row[4] = "https://realt.by/sale/plots/object/7/"
    row[33] = "90"

    assert _listing_from_row(row) is None


def test_catalog_skips_row_without_confirmed_distance() -> None:
    row = [""] * 53
    row[0] = "kufar:42"
    row[4] = "https://re.kufar.by/vi/42"
    row[7] = "19000"
    row[8] = "10"
    row[33] = "90"

    assert _listing_from_row(row) is None


def test_catalog_skips_row_outside_hard_limits() -> None:
    row = [""] * 53
    row[0] = "realt:42"
    row[4] = "https://realt.by/sale/plots/object/42/"
    row[7] = "40001"
    row[8] = "10"
    row[10] = "20"
    row[33] = "90"

    assert _listing_from_row(row) is None
