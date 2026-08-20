from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import SearchProfile, Settings
from app.domain import LocationProfile, NormalizedListing
from app.http import PublicPageClient, SourceBlockedError
from app.locations import identify_location
from app.models import LocationObservationModel
from app.sources.kufar import KufarSource
from app.sources.realt import RealtSource

LOGGER = logging.getLogger(__name__)

PREMIUM_SIGNAL = "premium_house"
OSM_SIGNAL = "osm_infrastructure"
COLLECTOR_STATE_SIGNAL = "collector_state"


class LocationSignalCollector:
    """Collect slow-changing evidence without involving the main plot scanner."""

    def __init__(self, settings: Settings, factory: sessionmaker) -> None:
        self.settings = settings
        self.factory = factory

    def run(self, profiles: Iterable[LocationProfile]) -> Dict[str, Any]:
        values = list(profiles)
        stats: Dict[str, Any] = {}
        errors: Dict[str, str] = {}
        premium_stats, premium_errors = self._collect_premium_houses(values)
        osm_stats, osm_errors = self._collect_osm(values)
        stats.update(premium_stats)
        stats.update(osm_stats)
        errors.update(premium_errors)
        errors.update(osm_errors)
        if errors:
            stats["enrichment_errors"] = errors
        return stats

    def _collect_premium_houses(
        self,
        profiles: Sequence[LocationProfile],
    ) -> Tuple[Dict[str, int], Dict[str, str]]:
        now = datetime.now(timezone.utc)
        profile_by_key = {profile.key: profile for profile in profiles}
        specs = [
            ("realt", self.settings.premium_realt_search_urls),
            ("kufar", self.settings.premium_kufar_search_urls),
        ]
        stats = {
            "premium_catalogs_scanned": 0,
            "premium_catalogs_cached": 0,
            "premium_houses_seen": 0,
            "premium_houses_matched": 0,
        }
        errors: Dict[str, str] = {}

        with PublicPageClient(
            timeout_seconds=self.settings.http_timeout_seconds,
            retries=self.settings.http_retries,
            delay_seconds=self.settings.request_delay_seconds,
        ) as client:
            for source_name, urls in specs:
                if not urls:
                    continue
                if not self._collector_due(source_name, now):
                    stats["premium_catalogs_cached"] += 1
                    continue
                try:
                    listings = self._premium_source(source_name, urls, client).scan()
                    matched = self._store_premium_listings(
                        listings,
                        profile_by_key,
                        now,
                    )
                    self._mark_collector_success(
                        source_name,
                        now,
                        {"seen": len(listings), "matched": matched},
                    )
                    stats["premium_catalogs_scanned"] += 1
                    stats["premium_houses_seen"] += len(listings)
                    stats["premium_houses_matched"] += matched
                except Exception as exc:
                    LOGGER.warning("Premium %s catalog failed: %s", source_name, exc)
                    errors[f"premium_{source_name}"] = str(exc)
        return stats, errors

    def _premium_source(
        self,
        source_name: str,
        urls: List[str],
        client: PublicPageClient,
    ) -> object:
        broad_profile = SearchProfile(
            discovery_max_price_usd=1_000_000,
            discovery_min_area_sotok=0,
            discovery_max_area_sotok=1_000,
            discovery_max_distance_km=100,
        )
        if source_name == "realt":
            return RealtSource(
                client=client,
                search_urls=urls,
                profile=broad_profile,
                max_details=0,
            )
        return KufarSource(
            client=client,
            search_urls=urls,
            profile=broad_profile,
            max_details=0,
            max_search_pages=min(2, self.settings.kufar_max_search_pages),
            detail_delay_seconds=self.settings.kufar_detail_delay_seconds,
            detail_batch_size=self.settings.kufar_detail_batch_size,
            detail_batch_pause_seconds=self.settings.kufar_detail_batch_pause_seconds,
            rate_limit_pause_seconds=self.settings.kufar_rate_limit_pause_seconds,
        )

    def _store_premium_listings(
        self,
        listings: Iterable[NormalizedListing],
        profiles: Dict[str, LocationProfile],
        now: datetime,
    ) -> int:
        matched = 0
        with self.factory() as session:
            for listing in listings:
                if not listing.raw_payload.get("has_house"):
                    continue
                if (
                    listing.price_usd is None
                    or listing.price_usd < self.settings.premium_min_price_usd
                ):
                    continue
                if (
                    listing.distance_mkad_km is not None
                    and listing.distance_mkad_km
                    > self.settings.profile.discovery_max_distance_km
                ):
                    continue
                location_key = _match_location_key(listing, profiles)
                if location_key is None:
                    continue
                self._upsert_observation(
                    session,
                    location_key=location_key,
                    source=listing.source,
                    source_id=listing.source_id,
                    signal_type=PREMIUM_SIGNAL,
                    canonical_url=listing.canonical_url,
                    latitude=listing.latitude,
                    longitude=listing.longitude,
                    price_usd=listing.price_usd,
                    payload={
                        "title": listing.title,
                        "locality": listing.locality,
                        "district": listing.district,
                        "distance_mkad_km": listing.distance_mkad_km,
                    },
                    now=now,
                    expires_at=now
                    + timedelta(days=self.settings.premium_observation_ttl_days),
                )
                matched += 1
            session.commit()
        return matched

    def _collect_osm(
        self,
        profiles: Sequence[LocationProfile],
    ) -> Tuple[Dict[str, int], Dict[str, str]]:
        stats = {
            "osm_locations_requested": 0,
            "osm_locations_updated": 0,
        }
        errors: Dict[str, str] = {}
        if not self.settings.location_osm_endpoints:
            return stats, errors

        now = datetime.now(timezone.utc)
        existing = self._osm_observations()
        due = [
            profile
            for profile in profiles
            if profile.eligible_count > 0
            and profile.latitude is not None
            and profile.longitude is not None
            and (
                profile.median_distance_mkad_km is None
                or profile.median_distance_mkad_km
                <= self.settings.profile.discovery_max_distance_km
            )
            and (
                profile.key not in existing
                or _as_utc(existing[profile.key].expires_at) <= now
            )
        ]
        due.sort(
            key=lambda profile: (
                0 if profile.key not in existing else 1,
                -profile.eligible_count,
                -profile.score,
                profile.label.lower(),
            )
        )
        batch = due[: max(0, self.settings.location_osm_batch_size)]
        stats["osm_locations_requested"] = len(batch)
        if not batch:
            return stats, errors

        with PublicPageClient(
            timeout_seconds=min(35, self.settings.http_timeout_seconds),
            retries=1,
            delay_seconds=max(1, self.settings.request_delay_seconds),
            user_agent="LandPlotFinder/0.2 (private location research)",
        ) as client:
            for profile in batch:
                try:
                    payload, endpoint = self._fetch_osm(profile, client)
                    payload["endpoint"] = endpoint
                    with self.factory() as session:
                        self._upsert_observation(
                            session,
                            location_key=profile.key,
                            source="openstreetmap",
                            source_id=profile.key,
                            signal_type=OSM_SIGNAL,
                            canonical_url=(
                                "https://www.openstreetmap.org/"
                                f"?mlat={profile.latitude:.6f}"
                                f"&mlon={profile.longitude:.6f}#map=14/"
                                f"{profile.latitude:.6f}/{profile.longitude:.6f}"
                            ),
                            latitude=profile.latitude,
                            longitude=profile.longitude,
                            price_usd=None,
                            payload=payload,
                            now=now,
                            expires_at=now
                            + timedelta(days=self.settings.location_osm_refresh_days),
                        )
                        session.commit()
                    stats["osm_locations_updated"] += 1
                except Exception as exc:
                    LOGGER.warning("OSM enrichment failed for %s: %s", profile.key, exc)
                    errors["osm"] = str(exc)
                    break
        return stats, errors

    def _fetch_osm(
        self,
        profile: LocationProfile,
        client: PublicPageClient,
    ) -> Tuple[Dict[str, Any], str]:
        query = _osm_query(float(profile.latitude), float(profile.longitude))
        failures: List[str] = []
        for endpoint in self.settings.location_osm_endpoints:
            try:
                response = client.post_text_json(endpoint, query)
                return _parse_osm_response(response), endpoint
            except (RuntimeError, SourceBlockedError, ValueError) as exc:
                failures.append(f"{endpoint}: {exc}")
        raise RuntimeError("; ".join(failures))

    def _collector_due(self, source: str, now: datetime) -> bool:
        with self.factory() as session:
            state = session.scalar(
                select(LocationObservationModel).where(
                    LocationObservationModel.source == source,
                    LocationObservationModel.source_id == "premium_catalog",
                    LocationObservationModel.signal_type == COLLECTOR_STATE_SIGNAL,
                )
            )
            return state is None or _as_utc(state.expires_at) <= now

    def _mark_collector_success(
        self,
        source: str,
        now: datetime,
        payload: Dict[str, Any],
    ) -> None:
        with self.factory() as session:
            self._upsert_observation(
                session,
                location_key="*",
                source=source,
                source_id="premium_catalog",
                signal_type=COLLECTOR_STATE_SIGNAL,
                canonical_url=None,
                latitude=None,
                longitude=None,
                price_usd=None,
                payload=payload,
                now=now,
                expires_at=now
                + timedelta(hours=self.settings.premium_catalog_refresh_hours),
            )
            session.commit()

    def _osm_observations(self) -> Dict[str, LocationObservationModel]:
        with self.factory() as session:
            rows = list(
                session.scalars(
                    select(LocationObservationModel).where(
                        LocationObservationModel.signal_type == OSM_SIGNAL
                    )
                )
            )
            session.expunge_all()
            return {row.location_key: row for row in rows}

    @staticmethod
    def _upsert_observation(
        session: Session,
        *,
        location_key: str,
        source: str,
        source_id: str,
        signal_type: str,
        canonical_url: Optional[str],
        latitude: Optional[float],
        longitude: Optional[float],
        price_usd: Optional[float],
        payload: Dict[str, Any],
        now: datetime,
        expires_at: datetime,
    ) -> LocationObservationModel:
        model = session.scalar(
            select(LocationObservationModel).where(
                LocationObservationModel.source == source,
                LocationObservationModel.source_id == source_id,
                LocationObservationModel.signal_type == signal_type,
            )
        )
        if model is None:
            model = LocationObservationModel(
                location_key=location_key,
                source=source,
                source_id=source_id,
                signal_type=signal_type,
                first_seen_at=now,
                expires_at=expires_at,
            )
            session.add(model)
        model.location_key = location_key
        model.canonical_url = canonical_url
        model.latitude = latitude
        model.longitude = longitude
        model.price_usd = price_usd
        model.payload = payload
        model.last_seen_at = now
        model.expires_at = expires_at
        return model


def _match_location_key(
    listing: NormalizedListing,
    profiles: Dict[str, LocationProfile],
) -> Optional[str]:
    identity = identify_location(listing)
    if identity is not None and identity.key in profiles:
        return identity.key
    if listing.latitude is None or listing.longitude is None:
        return None
    candidates = [
        (
            _haversine_km(
                float(listing.latitude),
                float(listing.longitude),
                float(profile.latitude),
                float(profile.longitude),
            ),
            profile.key,
        )
        for profile in profiles.values()
        if profile.latitude is not None and profile.longitude is not None
    ]
    if not candidates:
        return None
    distance, key = min(candidates)
    return key if distance <= 1.5 else None


def _osm_query(latitude: float, longitude: float) -> str:
    lat = f"{latitude:.6f}"
    lon = f"{longitude:.6f}"
    return f'''[out:json][timeout:20];
(
  nwr["shop"](around:2500,{lat},{lon});
  nwr["amenity"~"^(marketplace|school|kindergarten|clinic|doctors|pharmacy|hospital)$"](around:3000,{lat},{lon});
  node["highway"="bus_stop"](around:2500,{lat},{lon});
  node["public_transport"="platform"](around:2500,{lat},{lon});
  node["railway"~"^(station|halt)$"](around:4000,{lat},{lon});
);
out tags center qt;'''


def _parse_osm_response(response: Dict[str, Any]) -> Dict[str, Any]:
    groups: Dict[str, set] = {
        "shops": set(),
        "education": set(),
        "healthcare": set(),
        "transport": set(),
    }
    examples: Dict[str, List[str]] = {key: [] for key in groups}
    for element in response.get("elements", []):
        if not isinstance(element, dict):
            continue
        tags = element.get("tags") or {}
        if not isinstance(tags, dict):
            continue
        element_id = f"{element.get('type')}:{element.get('id')}"
        categories: List[str] = []
        amenity = tags.get("amenity")
        if tags.get("shop") or amenity == "marketplace":
            categories.append("shops")
        if amenity in {"school", "kindergarten"}:
            categories.append("education")
        if amenity in {"clinic", "doctors", "pharmacy", "hospital"}:
            categories.append("healthcare")
        if (
            tags.get("highway") == "bus_stop"
            or tags.get("public_transport") == "platform"
            or tags.get("railway") in {"station", "halt"}
        ):
            categories.append("transport")
        name = str(tags.get("name:ru") or tags.get("name") or "").strip()
        for category in categories:
            groups[category].add(element_id)
            if name and name not in examples[category] and len(examples[category]) < 3:
                examples[category].append(name)
    return {
        "shops": len(groups["shops"]),
        "education": len(groups["education"]),
        "healthcare": len(groups["healthcare"]),
        "transport": len(groups["transport"]),
        "examples": examples,
        "osm_timestamp": (response.get("osm3s") or {}).get("timestamp_osm_base"),
    }


def _haversine_km(
    latitude_a: float,
    longitude_a: float,
    latitude_b: float,
    longitude_b: float,
) -> float:
    lat_a = math.radians(latitude_a)
    lat_b = math.radians(latitude_b)
    delta_lat = lat_b - lat_a
    delta_lon = math.radians(longitude_b - longitude_a)
    value = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat_a) * math.cos(lat_b) * math.sin(delta_lon / 2) ** 2
    )
    return 6371.0088 * 2 * math.asin(math.sqrt(value))


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
