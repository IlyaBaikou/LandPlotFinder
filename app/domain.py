from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


class MatchStatus(str, Enum):
    MATCH = "MATCH"
    REVIEW = "REVIEW"
    INTERESTING = "INTERESTING"
    REJECT = "REJECT"


@dataclass
class NormalizedListing:
    source: str
    source_id: str
    canonical_url: str
    title: str
    description: str = ""
    district: Optional[str] = None
    locality: Optional[str] = None
    address: Optional[str] = None
    direction: Optional[str] = None
    price_original: Optional[float] = None
    currency_original: Optional[str] = None
    price_usd: Optional[float] = None
    area_sotok: Optional[float] = None
    distance_mkad_km: Optional[float] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    facade_m: Optional[float] = None
    depth_m: Optional[float] = None
    purpose: Optional[str] = None
    electricity_raw: Optional[str] = None
    electricity_kw: Optional[float] = None
    gas_raw: Optional[str] = None
    water_raw: Optional[str] = None
    sewerage_raw: Optional[str] = None
    internet_raw: Optional[str] = None
    road_raw: Optional[str] = None
    nature_raw: Optional[str] = None
    ownership_raw: Optional[str] = None
    seller_type: Optional[str] = None
    object_kind: Optional[str] = None
    house_area_sqm: Optional[float] = None
    house_condition: Optional[str] = None
    sale_format: Optional[str] = None
    house_risks: Optional[str] = None
    location_key: Optional[str] = None
    location_label: Optional[str] = None
    location_score: Optional[int] = None
    location_verdict: Optional[str] = None
    location_confidence: Optional[str] = None
    location_signals: List[str] = field(default_factory=list)
    location_risks: List[str] = field(default_factory=list)
    source_created_at: Optional[datetime] = None
    source_updated_at: Optional[datetime] = None
    evidence: Dict[str, str] = field(default_factory=dict)
    raw_payload: Dict[str, Any] = field(default_factory=dict)
    status: MatchStatus = MatchStatus.REVIEW
    score: int = 0
    reasons: List[str] = field(default_factory=list)
    possible_duplicate: bool = False

    @property
    def external_id(self) -> str:
        return f"{self.source}:{self.source_id}"

    def serializable(self) -> Dict[str, Any]:
        value = asdict(self)
        value["status"] = self.status.value
        for key in ("source_created_at", "source_updated_at"):
            if value[key]:
                value[key] = value[key].isoformat()
        return value


@dataclass
class ScanResult:
    started_at: datetime
    completed_at: Optional[datetime] = None
    listings: List[NormalizedListing] = field(default_factory=list)
    new_listings: List[NormalizedListing] = field(default_factory=list)
    changed_listings: List[NormalizedListing] = field(default_factory=list)
    errors: Dict[str, str] = field(default_factory=dict)
    source_stats: Dict[str, Dict[str, int]] = field(default_factory=dict)


@dataclass
class LocationProfile:
    key: str
    label: str
    kind: str
    district: Optional[str]
    latitude: Optional[float]
    longitude: Optional[float]
    listing_count: int
    eligible_count: int
    house_count: int
    premium_house_count: int
    median_house_price_usd: Optional[float]
    median_distance_mkad_km: Optional[float]
    electricity_share: float
    gas_share: float
    internet_share: float
    paved_road_share: float
    score: int
    verdict: str
    confidence: str
    signals: List[str] = field(default_factory=list)
    risks: List[str] = field(default_factory=list)
    member_external_ids: List[str] = field(default_factory=list)
    calculated_at: Optional[datetime] = None
