from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain import LocationProfile, NormalizedListing
from app.models import ListingModel, LocationObservationModel, LocationProfileModel

PREMIUM_SIGNAL = "premium_house"
OSM_SIGNAL = "osm_infrastructure"
COLLECTOR_STATE_SIGNAL = "collector_state"

GENERIC_PLACES = {
    "беларусь",
    "минск",
    "минская область",
    "минский район",
    "минская обл",
}
STREET_MARKERS = (" ул", "улица", " пер", "переулок", " проспект", "шоссе")
KIND_PATTERNS: Sequence[Tuple[str, re.Pattern[str]]] = (
    (
        "СТ",
        re.compile(
            r"^(?:с\s*/?\s*т\.?|ст\.?|садоводческое товарищество|"
            r"садовое товарищество)\s+",
            re.I,
        ),
    ),
    ("Деревня", re.compile(r"^(?:д\.?|деревня)\s+", re.I)),
    ("Агрогородок", re.compile(r"^(?:аг\.?|агрогородок)\s+", re.I)),
    ("Посёлок", re.compile(r"^(?:п\.?|пос[её]лок)\s+", re.I)),
    ("Город", re.compile(r"^(?:г\.?|город)\s+", re.I)),
)


@dataclass(frozen=True)
class LocationIdentity:
    key: str
    label: str
    kind: str
    district: Optional[str]
    latitude: Optional[float]
    longitude: Optional[float]


def rebuild_location_profiles(session: Session) -> List[LocationProfile]:
    listings = list(session.scalars(select(ListingModel)))
    observations = list(session.scalars(select(LocationObservationModel)))
    grouped: Dict[str, List[ListingModel]] = defaultdict(list)
    identities: Dict[str, LocationIdentity] = {}
    listing_keys: Dict[int, str] = {}

    for listing in listings:
        if (listing.raw_payload or {}).get("is_auction"):
            continue
        identity = identify_location(listing)
        if identity is None:
            continue
        grouped[identity.key].append(listing)
        current = identities.get(identity.key)
        if current is None or (
            current.kind == "Населённый пункт"
            and identity.kind != "Населённый пункт"
        ):
            identities[identity.key] = identity
        listing_keys[listing.id] = identity.key

    now = datetime.now(timezone.utc)
    observations_by_location: Dict[str, List[LocationObservationModel]] = defaultdict(
        list
    )
    premium_catalog_collected = False
    for observation in observations:
        if observation.signal_type == COLLECTOR_STATE_SIGNAL:
            premium_catalog_collected = True
        else:
            observations_by_location[observation.location_key].append(observation)
    profiles = [
        _build_profile(
            identities[key],
            values,
            now,
            observations_by_location.get(key, []),
            premium_catalog_collected,
        )
        for key, values in grouped.items()
    ]
    saved = {
        profile.key: profile
        for profile in session.scalars(select(LocationProfileModel))
    }
    for profile in profiles:
        model = saved.get(profile.key)
        if model is None:
            model = LocationProfileModel(
                key=profile.key,
                label=profile.label,
                kind=profile.kind,
                verdict=profile.verdict,
                confidence=profile.confidence,
            )
            session.add(model)
        for field in (
            "label",
            "kind",
            "district",
            "latitude",
            "longitude",
            "listing_count",
            "eligible_count",
            "house_count",
            "premium_house_count",
            "median_house_price_usd",
            "median_distance_mkad_km",
            "electricity_share",
            "gas_share",
            "internet_share",
            "paved_road_share",
            "score",
            "verdict",
            "confidence",
            "signals",
            "risks",
            "member_external_ids",
            "calculated_at",
        ):
            setattr(model, field, getattr(profile, field))
        model.next_refresh_at = now + timedelta(days=14)

    for listing in listings:
        key = listing_keys.get(listing.id)
        payload = dict(listing.raw_payload or {})
        if key:
            payload["location_key"] = key
        else:
            payload.pop("location_key", None)
        listing.raw_payload = payload
    session.flush()
    return sorted(profiles, key=lambda item: (-item.score, item.label.lower()))


def load_location_profiles(session: Session) -> Dict[str, LocationProfile]:
    return {
        model.key: _profile_from_model(model)
        for model in session.scalars(select(LocationProfileModel))
    }


def attach_location_profiles(
    listings: Iterable[NormalizedListing],
    profiles: Dict[str, LocationProfile],
) -> None:
    for listing in listings:
        identity = identify_location(listing)
        if identity is None:
            continue
        listing.location_key = identity.key
        listing.location_label = identity.label
        listing.raw_payload["location_key"] = identity.key
        profile = profiles.get(identity.key)
        if profile is None:
            listing.location_verdict = "Локация изучается"
            listing.location_confidence = "Нет данных"
            continue
        listing.location_label = profile.label
        listing.location_score = profile.score
        listing.location_verdict = profile.verdict
        listing.location_confidence = profile.confidence
        listing.location_signals = list(profile.signals)
        listing.location_risks = list(profile.risks)


def identify_location(value: object) -> Optional[LocationIdentity]:
    locality = _clean(getattr(value, "locality", None))
    address = _clean(getattr(value, "address", None))
    district = _district(value, address)
    latitude = getattr(value, "latitude", None)
    longitude = getattr(value, "longitude", None)
    payload = getattr(value, "raw_payload", None) or {}

    candidates: List[str] = []
    garden = _clean(payload.get("garden_community"))
    if garden and _normalize(garden) not in {
        "да",
        "есть",
        "true",
        "1",
        "нет",
        "false",
        "0",
    }:
        candidates.append(f"СТ {garden}")
    if address:
        parts = [part.strip() for part in address.split(",") if part.strip()]
        candidates.extend(
            part for part in parts if any(pattern.match(part) for _, pattern in KIND_PATTERNS)
        )
    if locality:
        candidates.append(locality)

    for candidate in candidates:
        parsed = _parse_candidate(candidate, district, latitude, longitude)
        if parsed is not None:
            return parsed

    if latitude is None or longitude is None:
        return None
    grid_lat = round(float(latitude), 2)
    grid_lon = round(float(longitude), 2)
    return LocationIdentity(
        key=f"geo:{grid_lat:.2f}:{grid_lon:.2f}",
        label=f"Район {grid_lat:.2f}, {grid_lon:.2f}",
        kind="Координатный кластер",
        district=district,
        latitude=float(latitude),
        longitude=float(longitude),
    )


def _parse_candidate(
    candidate: str,
    district: Optional[str],
    latitude: Optional[float],
    longitude: Optional[float],
) -> Optional[LocationIdentity]:
    lowered = _normalize(candidate)
    if lowered in GENERIC_PLACES or any(marker in lowered for marker in STREET_MARKERS):
        return None
    kind = "Населённый пункт"
    name = candidate.strip()
    for candidate_kind, pattern in KIND_PATTERNS:
        if pattern.match(name):
            kind = candidate_kind
            name = pattern.sub("", name).strip()
            break
    name = name.strip(" \"'«».,")
    normalized_name = _normalize(name)
    if not normalized_name or normalized_name in GENERIC_PLACES:
        return None
    district_key = _normalize_district(district or "без района")
    prefix = {
        "СТ": "СТ",
        "Деревня": "д.",
        "Агрогородок": "аг.",
        "Посёлок": "п.",
        "Город": "г.",
    }.get(kind)
    label = f"{prefix} {name}" if prefix else name
    return LocationIdentity(
        key=f"place:{normalized_name}:{district_key}",
        label=label,
        kind=kind,
        district=district,
        latitude=float(latitude) if latitude is not None else None,
        longitude=float(longitude) if longitude is not None else None,
    )


def _build_profile(
    identity: LocationIdentity,
    listings: List[ListingModel],
    now: datetime,
    observations: Optional[List[LocationObservationModel]] = None,
    premium_catalog_collected: bool = False,
) -> LocationProfile:
    active_observations = [
        item
        for item in (observations or [])
        if _as_utc(item.expires_at) > now
    ]
    houses = [item for item in listings if (item.raw_payload or {}).get("has_house")]
    eligible = [item for item in listings if item.status != "REJECT"]
    house_price_by_id = {
        f"{item.source}:{item.source_id}": item.price_usd
        for item in houses
        if item.price_usd is not None
    }
    distances = [
        item.distance_mkad_km for item in listings if item.distance_mkad_km is not None
    ]
    coordinates = [
        (item.latitude, item.longitude)
        for item in listings
        if item.latitude is not None and item.longitude is not None
    ]
    count = len(listings)
    electricity_share = _share(listings, "electricity_raw")
    gas_share = _share(listings, "gas_raw")
    internet_share = _share(listings, "internet_raw")
    paved_share = sum(_mentions_paved(item) for item in listings) / count
    premium_by_id = {
        external_id: price
        for external_id, price in house_price_by_id.items()
        if price >= 80_000
    }
    for observation in active_observations:
        if observation.signal_type == PREMIUM_SIGNAL and observation.price_usd is not None:
            premium_by_id[f"{observation.source}:{observation.source_id}"] = (
                observation.price_usd
            )
            house_price_by_id.setdefault(
                f"{observation.source}:{observation.source_id}",
                observation.price_usd,
            )
    premium_count = len(premium_by_id)
    house_prices = list(house_price_by_id.values())
    osm = next(
        (
            item.payload or {}
            for item in sorted(
                active_observations,
                key=lambda value: _as_utc(value.last_seen_at),
                reverse=True,
            )
            if item.signal_type == OSM_SIGNAL
        ),
        None,
    )

    score = 50
    score += min(8, round(math.log2(count + 1) * 2))
    score += min(8, len(houses) * 2)
    score += min(6, premium_count * 2)
    score += round((electricity_share + gas_share + internet_share + paved_share) * 2)
    if osm:
        score += 2 if int(osm.get("shops") or 0) > 0 else 0
        score += 2 if int(osm.get("transport") or 0) > 0 else 0
        score += 2 if int(osm.get("education") or 0) > 0 else 0
        score += 1 if int(osm.get("healthcare") or 0) > 0 else 0
    median_distance = median(distances) if distances else None
    if median_distance is not None and median_distance <= 30:
        score += 4
    elif median_distance is not None and median_distance <= 35:
        score += 1
    elif median_distance is not None and median_distance > 45:
        score -= 15
    score = max(30, min(85, score))

    completeness = (
        sum(bool(item.electricity_raw or item.gas_raw or item.internet_raw) for item in listings)
        / count
    )
    if count >= 5 and len(coordinates) >= 3 and completeness >= 0.5 and osm:
        confidence = "Средняя-высокая"
    elif count >= 5 and len(coordinates) >= 3 and completeness >= 0.5:
        confidence = "Средняя"
    elif count >= 3 and len(coordinates) >= 2:
        confidence = "Низкая-средняя"
    else:
        confidence = "Низкая"
    if confidence == "Низкая":
        verdict = (
            "Предварительно есть хорошие сигналы"
            if score >= 60
            else "Мало данных, нужна проверка"
        )
    elif score >= 70:
        verdict = "Есть сильные положительные сигналы"
    elif score >= 60:
        verdict = "Предварительно есть хорошие сигналы"
    else:
        verdict = "Нейтрально, нужна проверка"

    signals = [f"{count} {_plural(count, 'объявление', 'объявления', 'объявлений')} в базе"]
    if houses:
        house_count = len(houses)
        signals.append(
            f"{house_count} {_plural(house_count, 'объявление', 'объявления', 'объявлений')} "
            "домов или дач"
        )
    if premium_count:
        signals.append(f"{premium_count} домов от $80 000")
    if osm:
        osm_parts = []
        for key, label in (
            ("shops", "магазины"),
            ("transport", "транспорт"),
            ("education", "образование"),
            ("healthcare", "медицина"),
        ):
            value = int(osm.get(key) or 0)
            if value:
                osm_parts.append(f"{label}: {value}")
        if osm_parts:
            signals.append("В OSM рядом — " + ", ".join(osm_parts))
    if electricity_share >= 0.5:
        signals.append(f"Электричество упомянуто в {electricity_share:.0%} объявлений")
    if gas_share >= 0.35:
        signals.append(f"Газ упомянут в {gas_share:.0%} объявлений")
    if internet_share >= 0.25:
        signals.append(f"Интернет упомянут в {internet_share:.0%} объявлений")
    if paved_share >= 0.35:
        signals.append(f"Асфальт упомянут в {paved_share:.0%} объявлений")

    risks: List[str] = []
    if count < 3:
        risks.append("Мало объявлений для уверенной оценки")
    if len(coordinates) < max(1, math.ceil(count / 2)):
        risks.append("Недостаточно координат для проверки окружения")
    if premium_count == 0:
        risks.append(
            "В текущей широкой выдаче домов от $80 000 не найдено"
            if premium_catalog_collected
            else "Дорогие коттеджи пока специально не собирались"
        )
    if osm is None:
        risks.append("OSM-инфраструктура ещё не проверена")
    elif not any(int(osm.get(key) or 0) for key in ("shops", "transport", "education")):
        risks.append("В OpenStreetMap рядом мало отмеченной инфраструктуры")
    if median_distance is not None and median_distance > 45:
        risks.append(f"Медианное расстояние до МКАД — {median_distance:.1f} км")
    risks.append("Генплан, кадастр и экологические ограничения ещё не проверены")

    return LocationProfile(
        key=identity.key,
        label=identity.label,
        kind=identity.kind,
        district=identity.district,
        latitude=round(sum(item[0] for item in coordinates) / len(coordinates), 6)
        if coordinates
        else identity.latitude,
        longitude=round(sum(item[1] for item in coordinates) / len(coordinates), 6)
        if coordinates
        else identity.longitude,
        listing_count=count,
        eligible_count=len(eligible),
        house_count=len(houses),
        premium_house_count=premium_count,
        median_house_price_usd=round(median(house_prices), 2) if house_prices else None,
        median_distance_mkad_km=round(median(distances), 1) if distances else None,
        electricity_share=round(electricity_share, 3),
        gas_share=round(gas_share, 3),
        internet_share=round(internet_share, 3),
        paved_road_share=round(paved_share, 3),
        score=score,
        verdict=verdict,
        confidence=confidence,
        signals=signals,
        risks=risks,
        member_external_ids=sorted(f"{item.source}:{item.source_id}" for item in listings),
        calculated_at=now,
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _profile_from_model(model: LocationProfileModel) -> LocationProfile:
    return LocationProfile(
        key=model.key,
        label=model.label,
        kind=model.kind,
        district=model.district,
        latitude=model.latitude,
        longitude=model.longitude,
        listing_count=model.listing_count,
        eligible_count=model.eligible_count,
        house_count=model.house_count,
        premium_house_count=model.premium_house_count,
        median_house_price_usd=model.median_house_price_usd,
        median_distance_mkad_km=model.median_distance_mkad_km,
        electricity_share=model.electricity_share,
        gas_share=model.gas_share,
        internet_share=model.internet_share,
        paved_road_share=model.paved_road_share,
        score=model.score,
        verdict=model.verdict,
        confidence=model.confidence,
        signals=list(model.signals or []),
        risks=list(model.risks or []),
        member_external_ids=list(model.member_external_ids or []),
        calculated_at=model.calculated_at,
    )


def _share(listings: List[ListingModel], field: str) -> float:
    return sum(_has_positive_utility(getattr(item, field)) for item in listings) / len(
        listings
    )


def _has_positive_utility(value: object) -> bool:
    if not value:
        return False
    normalized = _normalize(str(value))
    return normalized not in {
        "нет",
        "не указано",
        "отсутствует",
        "без газа",
        "без электричества",
        "без интернета",
    }


def _mentions_paved(listing: ListingModel) -> bool:
    text = " ".join(
        value
        for value in (listing.road_raw, listing.description)
        if isinstance(value, str)
    ).lower()
    return "асфальт" in text or "асфальтирован" in text


def _district(value: object, address: Optional[str]) -> Optional[str]:
    district = _clean(getattr(value, "district", None))
    if address:
        for part in address.split(","):
            if "район" in part.lower():
                return part.strip()
    if district and _normalize(district) not in {
        "минская область",
        "минская обл",
        "другие города",
    }:
        return district
    return district


def _clean(value: object) -> Optional[str]:
    if value is None:
        return None
    result = " ".join(str(value).split()).strip()
    return result or None


def _normalize(value: str) -> str:
    return " ".join(
        re.sub(r"[^0-9a-zа-яё]+", " ", value.lower().replace("ё", "е")).split()
    )


def _normalize_district(value: str) -> str:
    normalized = _normalize(value)
    normalized = re.sub(r"\b(?:район|р н)\b", "", normalized)
    return " ".join(normalized.split()) or "без района"


def _plural(value: int, one: str, few: str, many: str) -> str:
    if value % 10 == 1 and value % 100 != 11:
        return one
    if value % 10 in {2, 3, 4} and value % 100 not in {12, 13, 14}:
        return few
    return many
