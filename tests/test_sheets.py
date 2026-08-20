from app.domain import NormalizedListing
from app.integrations.sheets import GoogleSheetsSink, _source_label


class FakeSheetsService:
    def __init__(self) -> None:
        self.bodies = []

    def spreadsheets(self):
        return self

    def values(self):
        return self

    def batchUpdate(self, **kwargs):
        self.bodies.append(kwargs["body"])
        return self

    def execute(self):
        return {}


def sample_listing() -> NormalizedListing:
    return NormalizedListing(
        source="realt",
        source_id="42",
        canonical_url="https://example.test/42",
        title="Участок",
        price_usd=20_000,
        area_sotok=10,
    )


def test_sheet_row_has_all_columns_a_to_ba() -> None:
    sink = object.__new__(GoogleSheetsSink)
    values = sink._row_values(
        sample_listing(),
        row_number=None,
    )

    assert len(values) == 53
    assert values[0] == "realt:42"
    assert values[1] == "Новый"


def test_update_preserves_status_discovery_date_and_manual_columns() -> None:
    sink = object.__new__(GoogleSheetsSink)
    sink.spreadsheet_id = "sheet-id"
    sink.sheet_name = "Plots"
    sink.service = FakeSheetsService()

    sink._batch_update_rows([(7, sample_listing())])

    update_ranges = [item["range"] for item in sink.service.bodies[0]["data"]]
    assert update_ranges == [
        "'Plots'!A7",
        "'Plots'!D7:I7",
        "'Plots'!K7:W7",
        "'Plots'!AN7:AQ7",
        "'Plots'!AU7",
        "'Plots'!AV7:BA7",
    ]


def test_rating_formula_falls_back_to_automatic_score() -> None:
    listing = sample_listing()
    listing.score = 73
    sink = object.__new__(GoogleSheetsSink)
    sink.spreadsheet_id = "sheet-id"
    sink.sheet_name = "Plots"
    sink.service = FakeSheetsService()

    sink._write_formulas([(7, listing)])

    rating = next(
        item["values"][0][0]
        for item in sink.service.bodies[0]["data"]
        if item["range"] == "'Plots'!AH7"
    )
    assert "COUNTA(X7:AG7)=0,73" in rating
    assert "'Criteria'!$B$5:$B$14" in rating


def test_source_label_marks_auction_and_aggregator() -> None:
    listing = sample_listing()
    listing.source = "beltorgi_auction"
    listing.raw_payload = {"is_auction": True, "is_aggregator": True}

    assert _source_label(listing) == "Белторги · Аукцион · Агрегатор"


def test_source_label_marks_preferred_location() -> None:
    listing = sample_listing()
    listing.raw_payload = {"preferred_location": "Логойское направление"}

    assert _source_label(listing) == "Realt · Приоритет"


def test_house_fields_are_written_without_touching_manual_house_decision() -> None:
    listing = sample_listing()
    listing.object_kind = "Дом с участком"
    listing.house_area_sqm = 48
    listing.house_condition = "Требует ремонта"
    listing.sale_format = "Частная продажа"
    listing.house_risks = "Проверить документы"

    sink = object.__new__(GoogleSheetsSink)
    values = sink._row_values(listing, row_number=None)

    assert values[39:47] == [
        "Дом с участком",
        48,
        "Требует ремонта",
        "Частная продажа",
        "",
        "",
        "",
        "Проверить документы",
    ]
    assert _source_label(listing) == "Realt · Дом"
