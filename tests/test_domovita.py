from app.config import SearchProfile
from app.sources.domovita import DomovitaSource

CATALOG = """
<html><body>
  <div class="found_item OAreaSale" data-key="569920" data-object-type="OAreaSale">
    <a class="title--listing" href="https://domovita.by/test/area/sale/plot-569920">
      Участок, 10-соток, Минский р-н, д. Тестово
    </a>
    <div class="price-usd">≈ 25 000 $</div>
    <div class="text-block">Электричество 20 кВт, газ по улице, 18 км от МКАД.</div>
    <div class="date">Обновлено: 27.08.2026</div>
    <script type="application/ld+json">
      {
        "@type": "Place",
        "branchCode": "569920",
        "url": "https://domovita.by/test/area/sale/plot-569920",
        "name": "Участок, 10-соток, Минский р-н, д. Тестово",
        "description": "Электричество 20 кВт, газ по улице, 18 км от МКАД.",
        "geo": {"latitude": "54.00", "longitude": "27.50"},
        "address": {"addressLocality": "Тестово", "streetAddress": "Садовая"}
      }
    </script>
  </div>
</body></html>
"""

DETAIL = """
<html><body>
  <input id="model_id" value="569920">
  <input id="className" value="OAreaSale">
  <div class="object-info__parametr"><span>Город</span><span>Тестово</span></div>
  <div class="object-info__parametr"><span>Направление</span><span>Логойское</span></div>
  <div class="object-info__parametr"><span>Удаленность от МКАД</span><span>19.2 км</span></div>
  <div class="object-info__parametr"><span>Участок</span><span>10 соток</span></div>
  <script type="application/ld+json">
    {
      "@type": "Place",
      "branchCode": "569920",
      "url": "https://domovita.by/test/area/sale/plot-569920",
      "name": "Участок, 10-соток, Минский р-н, д. Тестово",
      "description": "20 кВт, газ по улице, скважина, частная собственность.",
      "geo": {"latitude": "54.00", "longitude": "27.50"},
      "address": {"addressLocality": "Тестово", "streetAddress": "Садовая"}
    }
  </script>
  <script type="application/ld+json">
    {
      "@type": "Product",
      "productID": "569920",
      "url": "https://domovita.by/test/area/sale/plot-569920",
      "name": "Участок, 10-соток, Минский р-н, д. Тестово",
      "description": "20 кВт, газ по улице, скважина, частная собственность.",
      "productionDate": "2026-08-20",
      "releaseDate": "2026-08-27",
      "offers": {
        "priceCurrency": "USD",
        "price": "25000",
        "seller": {"@type": "Person", "name": "Собственник"}
      }
    }
  </script>
</body></html>
"""


class FakeClient:
    def get_text(self, url: str) -> str:
        return CATALOG if url.endswith("/sale") else DETAIL


def test_scans_domovita_catalog_and_enriches_detail() -> None:
    source = DomovitaSource(
        client=FakeClient(),
        search_urls=["https://domovita.by/minskiyi-rayion/area/sale"],
        profile=SearchProfile(),
        max_details=1,
        max_search_pages=1,
    )

    listings = source.scan()

    assert len(listings) == 1
    listing = listings[0]
    assert listing.source == "domovita"
    assert listing.source_id == "569920"
    assert listing.price_usd == 25_000
    assert listing.area_sotok == 10
    assert listing.distance_mkad_km == 19.2
    assert listing.direction == "Логойское"
    assert listing.electricity_kw == 20
    assert listing.gas_raw is not None
    assert listing.water_raw is not None
    assert listing.ownership_raw == "частная собственность"
    assert listing.raw_payload["detail_loaded"] is True
