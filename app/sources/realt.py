from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from app.domain import NormalizedListing
from app.geo import distance_to_mkad_km
from app.house import detect_house_risks, join_house_condition
from app.normalization import (
    canonical_url,
    extract_dimensions,
    extract_distance_km,
    extract_electricity_kw,
    normalized_ownership,
    parse_datetime,
    parse_float,
    plain_text,
)
from app.sources.base import ListingSource
from app.sources.next_data import extract_next_data
from app.sources.rotation import rotating_detail_ids

LOGGER = logging.getLogger(__name__)


class RealtSource(ListingSource):
    name = "realt"

    def scan(self) -> List[NormalizedListing]:
        summaries: Dict[str, Dict[str, Any]] = {}
        for search_url in self.search_urls:
            html = self.client.get_text(search_url)
            data = extract_next_data(html)
            page_props = data.get("props", {}).get("pageProps", {})
            objects = page_props.get("objects")
            if not isinstance(objects, list):
                raise ValueError(f"Realt listing page changed: objects missing at {search_url}")
            for item in objects:
                if not isinstance(item, dict) or item.get("code") is None:
                    continue
                summaries[str(item["code"])] = item

        normalized = {
            source_id: self._normalize(item) for source_id, item in summaries.items()
        }
        for summary in normalized.values():
            summary.raw_payload["detail_loaded"] = False
        candidates = [
            source_id
            for source_id, summary in normalized.items()
            if self.passes_basic_prefilter(summary)
        ]
        detail_ids = set(rotating_detail_ids(candidates, self.max_details))

        listings: List[NormalizedListing] = []
        for source_id, summary in normalized.items():
            if source_id in detail_ids:
                try:
                    detail_url = summary.canonical_url
                    detail_html = self.client.get_text(detail_url)
                    detail_data = extract_next_data(detail_html)
                    detail = detail_data.get("props", {}).get("pageProps", {}).get("object")
                    if isinstance(detail, dict):
                        summary = self._normalize(detail)
                        summary.raw_payload["detail_loaded"] = True
                except Exception as exc:
                    LOGGER.warning("Unable to load Realt detail %s: %s", source_id, exc)
                    summary.reasons.append(f"Не удалось загрузить детали Realt: {exc}")
            listings.append(summary)
        return listings

    def _normalize(self, item: Dict[str, Any]) -> NormalizedListing:
        source_id = str(item.get("code") or item.get("id"))
        category = str(item.get("category") or "")
        object_type = str(item.get("objectType") or "")
        if category == "11" or object_type == "1":
            path = "sale-cottages"
            object_kind = "Дом с участком"
        elif category == "13" or object_type == "3":
            path = "sale-dachi"
            object_kind = "Дача с участком"
        else:
            path = "sale-plots"
            object_kind = "Участок"
        is_house = object_kind != "Участок"
        url = canonical_url(f"https://realt.by/{path}/object/{source_id}/")
        description = plain_text(
            item.get("description") or item.get("headline") or item.get("title")
        )
        title = plain_text(item.get("title") or item.get("headline")) or f"Участок {source_id}"

        price_rates = item.get("priceRates") or {}
        price_usd = parse_float(price_rates.get("840") or price_rates.get(840))
        price_original = parse_float(item.get("price"))
        currency_original = "BYN" if item.get("priceCurrency") == 933 else str(
            item.get("priceCurrency") or ""
        )

        reported_town_distance = parse_float(item.get("townDistance"))
        distance, distance_evidence = extract_distance_km(description)

        electricity_raw = _text(item.get("electricity"))
        electricity_power_raw = _text(item.get("electricityPowerV2"))
        electricity_kw, electricity_evidence = extract_electricity_kw(
            electricity_power_raw,
            electricity_raw,
            description,
        )
        facade, depth, dimensions_evidence = extract_dimensions(description)

        location = item.get("location") or []
        longitude: Optional[float] = None
        latitude: Optional[float] = None
        if isinstance(location, list) and len(location) >= 2:
            longitude = parse_float(location[0])
            latitude = parse_float(location[1])
        if distance is None and latitude is not None and longitude is not None:
            distance = distance_to_mkad_km(latitude, longitude)
            distance_evidence = (
                "По прямой до контура МКАД по координатам "
                f"{latitude:.5f}, {longitude:.5f}"
            )
        elif distance is None and reported_town_distance is not None:
            distance = reported_town_distance
            distance_evidence = "Расстояние из структурированного поля Realt"

        gas_raw = _text(item.get("gas"))
        ownership = normalized_ownership(
            _text(item.get("privatization")),
            _text(item.get("legalStatus")),
            description,
        )
        infrastructure = item.get("infrastructure")
        if isinstance(infrastructure, list):
            nature_raw = ", ".join(str(value) for value in infrastructure)
        else:
            nature_raw = _text(infrastructure)

        evidence: Dict[str, str] = {}
        if electricity_evidence:
            evidence["electricity"] = electricity_evidence
        if distance_evidence:
            evidence["distance"] = distance_evidence
        if dimensions_evidence:
            evidence["dimensions"] = dimensions_evidence

        seller_type = "Агентство" if item.get("agency") or item.get("agencyName") else None
        if not seller_type and item.get("companyName"):
            seller_type = "Компания"
        if not seller_type and item.get("contactName"):
            seller_type = "Контактное лицо"
        house_area = parse_float(item.get("areaTotal")) if is_house else None
        house_condition = (
            join_house_condition(
                _text(item.get("repairState")),
                (
                    f"Год постройки: {item.get('buildingYear')}"
                    if item.get("buildingYear")
                    else None
                ),
                _text(item.get("wallMaterial")),
            )
            if is_house
            else None
        )
        house_risks = (
            detect_house_risks([title, description, house_condition])
            if is_house
            else None
        )

        return NormalizedListing(
            source=self.name,
            source_id=source_id,
            canonical_url=url,
            title=title,
            description=description,
            district=_text(item.get("stateDistrictName")),
            locality=_text(item.get("townName")),
            address=_text(item.get("address")),
            direction=_text(item.get("directionName")),
            price_original=price_original,
            currency_original=currency_original or None,
            price_usd=price_usd,
            area_sotok=parse_float(item.get("areaLand")),
            distance_mkad_km=distance,
            latitude=latitude,
            longitude=longitude,
            facade_m=facade,
            depth_m=depth,
            purpose=_text(item.get("legalStatus")),
            electricity_raw="; ".join(
                value for value in [electricity_raw, electricity_power_raw] if value
            )
            or None,
            electricity_kw=electricity_kw,
            gas_raw=gas_raw,
            water_raw=_text(item.get("water")),
            sewerage_raw=_text(item.get("sewerage")),
            internet_raw=_find_evidence(description, ["интернет", "оптоволокно", "оптика"]),
            road_raw=_find_evidence(description, ["асфальт", "подъезд", "дорог"]),
            nature_raw=nature_raw or _find_evidence(
                description, ["лес", "озеро", "водоем", "река"]
            ),
            ownership_raw=ownership,
            seller_type=seller_type,
            object_kind=object_kind,
            house_area_sqm=house_area,
            house_condition=house_condition,
            sale_format=(
                _text(item.get("termsOfSale")) or "Частная продажа"
                if is_house
                else None
            ),
            house_risks=house_risks,
            source_created_at=parse_datetime(item.get("createdAt")),
            source_updated_at=parse_datetime(item.get("updatedAt")),
            evidence=evidence,
            raw_payload={
                "price_change_direction": item.get("priceChangeDirection"),
                "terms_of_sale": item.get("termsOfSale"),
                "infrastructure": infrastructure,
                "has_house": is_house,
                "object_kind": object_kind,
                "seller_id": (
                    item.get("userUuid")
                    or item.get("companyUuid")
                    or item.get("agencyUuid")
                ),
            },
        )


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return plain_text(str(value))
    text = plain_text(str(value))
    return text or None


def _find_evidence(text: str, needles: List[str]) -> Optional[str]:
    lowered = text.lower()
    for needle in needles:
        position = lowered.find(needle)
        if position >= 0:
            start = max(0, position - 70)
            end = min(len(text), position + len(needle) + 100)
            return text[start:end]
    return None
