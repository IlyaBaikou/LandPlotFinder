from app.config import SearchProfile
from app.sources.e_auction import EauctionSource, _extract_area_sotok


class FakeClient:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages

    def get_text(self, url: str) -> str:
        return self.pages[url]

    def get_json(self, url: str) -> dict:
        return {"Cur_OfficialRate": 3.2, "Cur_Scale": 1}


def test_scans_e_auction_catalog_and_detail() -> None:
    catalog_url = "https://e-auction.by/nedvizhimost/zemelnye_uchastki/"
    detail_url = (
        "https://e-auction.by/nedvizhimost/zemelnye_uchastki/4-2099-08-00020/"
    )
    catalog = """
    <a class="product-item type-auction status-st"
       data-endrequest="4102444800"
       href="/nedvizhimost/zemelnye_uchastki/4-2099-08-00020/">
      <span class="lot-type">Арестованное</span>
      <div class="product_art">4.2099.08.00020</div>
      <div class="text-header">Земельный участок площадью 0,1021 га</div>
      <div class="text-status">Приём заявок до 10.08.99 16:00</div>
      <span class="popup-exchange-tooltip" data-cur="BYN" data-value="32000"></span>
    </a>
    """
    detail = """
    <h1>Земельный участок</h1>
    <table>
      <tr><td>Описание имущества</td><td>Частная собственность. Электричество 20 кВт.</td></tr>
      <tr><td>Местоположение имущества</td>
          <td>Минская область, Смолевичский район, д. Тест</td></tr>
      <tr><td>Площадь участка</td><td>0,1021 га</td></tr>
      <tr><td>Кадастровый номер</td><td>624884500001000217</td></tr>
      <tr><td>Обременения</td><td>Запрет регистрационных действий</td></tr>
    </table>
    """
    source = EauctionSource(
        client=FakeClient({catalog_url: catalog, detail_url: detail}),
        search_urls=[catalog_url],
        profile=SearchProfile(),
        max_details=10,
    )

    listings = source.scan()

    assert len(listings) == 1
    listing = listings[0]
    assert listing.source_id == "4.2099.08.00020"
    assert listing.area_sotok == 10.21
    assert listing.price_usd == 10_000
    assert listing.electricity_kw == 20
    assert listing.raw_payload["cadastral_number"] == "624884500001000217"
    assert listing.raw_payload["is_auction"] is True
    assert any("Обременения" in reason for reason in listing.reasons)


def test_extracts_square_metres_as_sotok() -> None:
    assert _extract_area_sotok("Площадь участка 1000 м²") == 10
