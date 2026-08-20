from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List

from app.config import SearchProfile
from app.domain import NormalizedListing
from app.http import PublicPageClient


class ListingSource(ABC):
    name: str

    def __init__(
        self,
        client: PublicPageClient,
        search_urls: List[str],
        profile: SearchProfile,
        max_details: int,
    ) -> None:
        self.client = client
        self.search_urls = search_urls
        self.profile = profile
        self.max_details = max_details

    @abstractmethod
    def scan(self) -> List[NormalizedListing]:
        raise NotImplementedError

    def passes_basic_prefilter(self, listing: NormalizedListing) -> bool:
        if (
            listing.price_usd is not None
            and listing.price_usd > self.profile.discovery_max_price_usd
        ):
            return False
        if listing.area_sotok is not None and not (
            self.profile.discovery_min_area_sotok
            <= listing.area_sotok
            <= self.profile.discovery_max_area_sotok
        ):
            return False
        if (
            listing.distance_mkad_km is not None
            and listing.distance_mkad_km > self.profile.discovery_max_distance_km
        ):
            return False
        return True
