from app.dedupe import flag_possible_duplicates
from app.domain import NormalizedListing


def test_exact_cadastral_number_flags_cross_source_duplicate() -> None:
    left = NormalizedListing(
        source="rlt_auction",
        source_id="1",
        canonical_url="https://rlt.by/1",
        title="Участок",
        raw_payload={"cadastral_number": "624884500001000217"},
    )
    right = NormalizedListing(
        source="e_auction",
        source_id="2",
        canonical_url="https://e-auction.by/2",
        title="Другой заголовок",
        raw_payload={"cadastral_number": "6248845000:01:000217"},
    )

    assert flag_possible_duplicates([left, right]) == 2
    assert left.possible_duplicate is True
    assert right.possible_duplicate is True
