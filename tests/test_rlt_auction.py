from app.config import SearchProfile
from app.sources.rlt_auction import RltAuctionSource


class FakeClient:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages

    def get_text(self, url: str) -> str:
        return self.pages[url]

    def get_json(self, url: str) -> dict:
        return {"Cur_OfficialRate": 3.2, "Cur_Scale": 1}


def test_scans_rlt_catalog_and_detail() -> None:
    catalog_url = "https://rlt.by/aukciony/"
    detail_url = "https://rlt.by/aukciony/zemelnyj-uchastok-12345/"
    catalog = """
    <div class="auction-item">
      <h3><a href="/aukciony/zemelnyj-uchastok-12345/">Земельный участок</a></h3>
      <div>Приём заявок до 20.08.2099 17:00</div>
    </div>
    """
    detail = """
    <main>
      Земельный участок площадью 0,10 га.
      Местоположение: Минская область, Смолевичский район, д. Тест.
      Кадастровый номер 624884500001000217.
      Начальная цена: 32 000 BYN.
      Электричество 20 кВт. Частная собственность.
      Приём заявок до 20.08.2099 17:00.
    </main>
    """
    source = RltAuctionSource(
        client=FakeClient({catalog_url: catalog, detail_url: detail}),
        search_urls=[catalog_url],
        profile=SearchProfile(),
        max_details=10,
    )

    listings = source.scan()

    assert len(listings) == 1
    listing = listings[0]
    assert listing.area_sotok == 10
    assert listing.price_usd == 10_000
    assert listing.electricity_kw == 20
    assert listing.raw_payload["cadastral_number"] == "624884500001000217"
    assert listing.raw_payload["is_auction"] is True


def test_scans_javascript_linked_electronic_auction_card() -> None:
    catalog_url = "https://torgi.rlt.by/aukciony/"
    detail_url = "https://torgi.rlt.by/aukciony/lot-777/"
    catalog = """
    <article onclick="location.href='/aukciony/lot-777/'">
      <h3>Земельный участок площадью 0,1 га</h3>
      <div>Приём заявок до 20.08.2099 17:00</div>
    </article>
    """
    detail = """
    <main>
      Земельный участок площадью 0,1 га. Начальная цена: 16 000 BYN.
    </main>
    """
    source = RltAuctionSource(
        client=FakeClient({catalog_url: catalog, detail_url: detail}),
        search_urls=[catalog_url],
        profile=SearchProfile(),
        max_details=10,
    )

    listings = source.scan()

    assert len(listings) == 1
    assert listings[0].area_sotok == 10
