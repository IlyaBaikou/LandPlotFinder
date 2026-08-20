from app.config import SearchProfile
from app.sources.beltorgi import BeltorgiAuctionSource


class FakeClient:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages

    def get_text(self, url: str) -> str:
        return self.pages[url]

    def get_json(self, url: str) -> dict:
        return {"Cur_OfficialRate": 3.4, "Cur_Scale": 1}


def test_scans_multiple_beltorgi_lots_from_one_post() -> None:
    channel_url = "https://t.me/s/beltorgi_uchastok"
    page = """
    <div class="tgme_widget_message" data-post="beltorgi_uchastok/1215">
      <div class="tgme_widget_message_text">
        Минский район<br>
        Петришковский с/с<br>
        [Лот 21]<br>
        Адрес: д. Новашино, ул. Тестовая, 1<br>
        Расстояние: 22 км от МКАД<br>
        Кадастровый номер: 623600000001000021<br>
        Площадь — 9,88 соток<br>
        Назначение: строительство жилого дома<br>
        Инфраструктура: электричество, газ<br>
        Начальная цена: 34 185 BYN<br>
        [Лот 22]<br>
        Адрес: д. Шубники, ул. Тестовая, 2<br>
        Расстояние: 20 км от МКАД<br>
        Кадастровый номер: 623600000001000022<br>
        Площадь — 10,07 соток<br>
        Назначение: строительство жилого дома<br>
        Инфраструктура: электричество 20 кВт<br>
        Начальная цена: 44 066 BYN<br>
        Аукцион состоится 21 августа 2099<br>
        Приём документов завершается 17 августа 2099 в 17:00<br>
        Очные торги
      </div>
      <a class="tgme_widget_message_date"
         href="https://t.me/beltorgi_uchastok/1215">
        <time datetime="2099-07-29T09:00:00+00:00"></time>
      </a>
    </div>
    """
    source = BeltorgiAuctionSource(
        client=FakeClient({channel_url: page}),
        search_urls=[channel_url],
        profile=SearchProfile(),
        max_details=10,
        max_pages=1,
    )

    listings = source.scan()

    assert len(listings) == 2
    first, second = listings
    assert first.source_id == "623600000001000021"
    assert first.area_sotok == 9.88
    assert first.distance_mkad_km == 22
    assert first.price_original == 34_185
    assert first.price_usd == 10_054.41
    assert first.raw_payload["auction_date"] == "2099-08-21"
    assert first.raw_payload["auction_deadline"] == "2099-08-17T17:00:00+00:00"
    assert first.raw_payload["is_auction"] is True
    assert first.raw_payload["is_aggregator"] is True
    assert second.source_id == "623600000001000022"
    assert second.area_sotok == 10.07
    assert second.electricity_kw == 20
