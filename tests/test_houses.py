from app.config import SearchProfile
from app.domain import MatchStatus
from app.filters import evaluate_listing
from app.sources.kufar import KufarSource
from app.sources.realt import RealtSource


def test_kufar_house_parameters_are_normalized() -> None:
    source = object.__new__(KufarSource)
    listing = source._normalize(
        {
            "ad_id": 123,
            "category": "1020",
            "subject": "Старый дом в деревне",
            "body": "Дом требует ремонта, участок ровный.",
            "price_usd": "2000000",
            "ad_parameters": [
                {"p": "size_area", "vl": "10"},
                {"p": "size", "vl": "48"},
                {"p": "house_type_for_sell", "vl": "Дом"},
                {"p": "house_readiness", "vl": "Требует ремонта"},
                {"p": "electricity", "vl": "Есть"},
                {"p": "house_gaz", "vl": "По улице"},
            ],
        }
    )

    assert listing.object_kind == "Дом с участком"
    assert listing.house_area_sqm == 48
    assert listing.area_sotok == 10
    assert listing.electricity_raw == "Есть"
    assert listing.gas_raw == "По улице"
    assert "ремонт" in (listing.house_risks or "").lower()
    assert listing.canonical_url == "https://re.kufar.by/vi/123"


def test_realt_house_uses_cottage_url_and_house_fields() -> None:
    source = object.__new__(RealtSource)
    listing = source._normalize(
        {
            "code": 456,
            "category": 11,
            "title": "Дом под реконструкцию",
            "description": "Старый дом на участке.",
            "priceRates": {"840": 25000},
            "areaLand": 9.8,
            "areaTotal": 62,
            "repairState": "Требует ремонта",
            "buildingYear": 1965,
        }
    )

    assert listing.object_kind == "Дом с участком"
    assert listing.house_area_sqm == 62
    assert "1965" in (listing.house_condition or "")
    assert listing.canonical_url == "https://realt.by/sale-cottages/object/456/"


def test_house_uses_same_hard_price_limit_as_plot() -> None:
    source = object.__new__(KufarSource)
    listing = source._normalize(
        {
            "ad_id": 789,
            "category": "1020",
            "subject": "Дом с участком",
            "price_usd": "4000100",
            "ad_parameters": [
                {"p": "size_area", "vl": "10"},
                {"p": "house_type_for_sell", "vl": "Дом"},
            ],
        }
    )

    result = evaluate_listing(listing, SearchProfile())

    assert result.status is MatchStatus.REJECT
    assert any("широкого лимита" in reason for reason in result.reasons)
