from app.config import load_settings
from app.domain import NormalizedListing
from app.orchestrator import Scanner, _mark_preferred
from app.sources.e_auction import EauctionSource
from app.sources.kufar import KufarSource
from app.sources.realt import RealtSource
from app.sources.realt_auction import RealtAuctionSource
from app.sources.rlt_auction import RltAuctionSource


def test_builds_both_sources_with_kufar_page_limit(monkeypatch) -> None:
    monkeypatch.setenv("KUFAR_MAX_SEARCH_PAGES", "2")
    scanner = Scanner(load_settings())

    sources = scanner._sources(client=object(), enabled_sources=None)

    assert isinstance(sources[0], RealtSource)
    assert isinstance(sources[1], RealtAuctionSource)
    assert isinstance(sources[2], RltAuctionSource)
    assert isinstance(sources[3], EauctionSource)
    assert isinstance(sources[4], KufarSource)
    assert sources[4].max_search_pages == 2


def test_beltorgi_is_not_scanned_by_default(monkeypatch) -> None:
    monkeypatch.setenv(
        "BELTORGI_AUCTION_URLS",
        "https://t.me/s/beltorgi_uchastok",
    )
    scanner = Scanner(load_settings())

    sources = scanner._sources(client=object(), enabled_sources=None)

    assert all(source.name != "beltorgi_auction" for source in sources)


def test_preferred_queue_uses_only_dedicated_realt_and_kufar_searches() -> None:
    settings = load_settings()
    scanner = Scanner(settings)

    sources = scanner._sources(
        client=object(),
        enabled_sources=None,
        queue="preferred",
    )

    assert [source.name for source in sources] == ["realt", "kufar"]
    assert sources[0].search_urls == settings.preferred_realt_search_urls
    assert sources[1].search_urls == settings.preferred_kufar_search_urls
    assert all(
        source.max_details == settings.preferred_max_details_per_source
        for source in sources
    )
    assert scanner.settings.profile.discovery_min_area_sotok == 9
    assert scanner.settings.profile.discovery_max_area_sotok == 15
    assert scanner.settings.profile.discovery_max_distance_km == 30


def test_preferred_queue_marks_provenance_without_changing_listing() -> None:
    listing = NormalizedListing(
        source="realt",
        source_id="42",
        canonical_url="https://example.test/42",
        title="Участок",
        area_sotok=10,
        distance_mkad_km=30,
    )

    _mark_preferred([listing], "Логойское направление")

    assert listing.raw_payload == {
        "preferred_location": "Логойское направление",
        "search_queue": "preferred",
    }
    assert listing.area_sotok == 10
    assert listing.distance_mkad_km == 30
