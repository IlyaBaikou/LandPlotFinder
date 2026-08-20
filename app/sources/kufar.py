from __future__ import annotations

import logging
import time
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.domain import NormalizedListing
from app.geo import distance_to_mkad_km
from app.house import detect_house_risks, join_house_condition
from app.http import SourceBlockedError
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
from app.sources.rotation import rotating_detail_ids as _rotating_detail_ids

LOGGER = logging.getLogger(__name__)


class KufarSource(ListingSource):
    name = "kufar"

    def __init__(
        self,
        *args: Any,
        max_search_pages: int = 3,
        detail_delay_seconds: float = 6,
        detail_batch_size: int = 8,
        detail_batch_pause_seconds: float = 45,
        rate_limit_pause_seconds: float = 60,
        known_listing_ids: Optional[Iterable[str]] = None,
        known_distance_ids: Optional[Iterable[str]] = None,
        pending_notification_ids: Optional[Iterable[str]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.max_search_pages = max(1, max_search_pages)
        self.detail_delay_seconds = max(0, detail_delay_seconds)
        self.detail_batch_size = max(1, detail_batch_size)
        self.detail_batch_pause_seconds = max(0, detail_batch_pause_seconds)
        self.rate_limit_pause_seconds = max(1, rate_limit_pause_seconds)
        self.known_listing_ids = set(known_listing_ids or [])
        self.known_distance_ids = set(known_distance_ids or [])
        self.pending_notification_ids = set(pending_notification_ids or [])

    def scan(self) -> List[NormalizedListing]:
        summaries: Dict[str, Dict[str, Any]] = {}
        source_blocked = False
        for search_url in self.search_urls:
            page_url: Optional[str] = search_url
            for _ in range(self.max_search_pages):
                if not page_url:
                    break
                try:
                    html = self.client.get_text(page_url)
                except SourceBlockedError as exc:
                    source_blocked = True
                    LOGGER.warning("Kufar stopped search requests after rate limit: %s", exc)
                    break
                except Exception as exc:
                    LOGGER.warning("Unable to load Kufar search URL %s: %s", page_url, exc)
                    break
                data = extract_next_data(html)
                listing = data.get("props", {}).get("initialState", {}).get("listing", {})
                ads = listing.get("ads")
                if not isinstance(ads, list):
                    raise ValueError(f"Kufar listing page changed: ads missing at {page_url}")
                for item in ads:
                    if not isinstance(item, dict) or item.get("ad_id") is None:
                        continue
                    summaries[str(item["ad_id"])] = item
                page_url = _next_page_url(search_url, listing.get("pagination"))
            if source_blocked:
                break

        normalized = {
            source_id: self._normalize(item) for source_id, item in summaries.items()
        }
        for summary in normalized.values():
            summary.raw_payload["detail_loaded"] = False
        detail_candidates = [
            source_id
            for source_id, summary in normalized.items()
            if self.passes_basic_prefilter(summary)
        ]
        detail_ids = _prioritized_detail_ids(
            detail_candidates,
            self.known_listing_ids,
            self.known_distance_ids,
            self.pending_notification_ids,
            self.max_details,
        )
        detail_candidate_set = set(detail_candidates)

        listings: List[NormalizedListing] = []
        detail_count = 0
        details_blocked = source_blocked
        for source_id, summary in normalized.items():
            if not details_blocked and source_id in detail_ids:
                if (
                    detail_count
                    and detail_count % self.detail_batch_size == 0
                    and self.detail_batch_pause_seconds
                ):
                    LOGGER.info(
                        "Kufar detail batch complete; pausing %.0f seconds",
                        self.detail_batch_pause_seconds,
                    )
                    time.sleep(self.detail_batch_pause_seconds)
                detail_count += 1
                try:
                    detail_html = self.client.get_text(
                        summary.canonical_url,
                        min_delay_seconds=self.detail_delay_seconds,
                        retry_rate_limit=True,
                        rate_limit_pause_seconds=self.rate_limit_pause_seconds,
                    )
                    detail_data = extract_next_data(detail_html)
                    detail = (
                        detail_data.get("props", {})
                        .get("initialState", {})
                        .get("adView", {})
                        .get("data")
                    )
                    if isinstance(detail, dict):
                        summary = self._normalize(detail)
                        summary.raw_payload["detail_loaded"] = True
                except SourceBlockedError as exc:
                    details_blocked = True
                    LOGGER.warning("Kufar stopped detail requests after rate limit: %s", exc)
                    summary.reasons.append(
                        "Kufar ограничил частоту запросов; данные взяты из карточки списка"
                    )
                except Exception as exc:
                    LOGGER.warning("Unable to load Kufar detail %s: %s", source_id, exc)
                    summary.reasons.append(f"Не удалось загрузить детали Kufar: {exc}")
            needs_complete_notification = (
                source_id in detail_candidate_set
                and (
                    source_id not in self.known_listing_ids
                    or source_id in self.pending_notification_ids
                )
                and not summary.raw_payload.get("detail_loaded")
            )
            if needs_complete_notification:
                summary.raw_payload["telegram_pending_enrichment"] = True
            else:
                summary.raw_payload.pop("telegram_pending_enrichment", None)
            listings.append(summary)
        return listings

    def _normalize(self, item: Dict[str, Any]) -> NormalizedListing:
        source_id = str(item.get("ad_id") or item.get("adId") or item.get("id"))
        url = canonical_url(f"https://re.kufar.by/vi/{source_id}")
        description = plain_text(item.get("body") or item.get("description"))
        title = plain_text(item.get("subject") or item.get("title")) or f"Участок {source_id}"

        initial = item.get("initial") if isinstance(item.get("initial"), dict) else item
        params = _parameter_map(
            initial.get("ad_parameters")
            or item.get("adParams")
            or item.get("parameters")
            or []
        )
        account_params = _parameter_map(
            initial.get("account_parameters") or item.get("accountParams") or []
        )

        price_usd = _money_from_cents(
            initial.get("price_usd")
            or item.get("priceUsd")
            or _calculator_price(initial.get("calculator") or item.get("calculator"), "USD")
        )
        price_byn = _money_from_cents(
            initial.get("price_byn")
            or _calculator_price(initial.get("calculator") or item.get("calculator"), "BYN")
        )

        area = parse_float(_param_value(params, "Площадь участка, сот.", "size_area"))
        electricity_raw = _param_value(
            params,
            "Электричество",
            "re_electricity",
            "house_electricity",
            "electricity",
        )
        gas_raw = _param_value(params, "Газ", "re_gaz", "house_gaz", "gaz")
        water_raw = _param_value(params, "Вода", "re_water", "house_water", "water")
        sewerage_raw = _param_value(
            params,
            "Канализация",
            "re_sewage",
            "house_sewage",
            "sewage",
        )
        purpose = _param_value(
            params,
            "Целевое назначение участка",
            "re_land_purpose",
            "land_purpose",
        )
        ownership_raw = normalized_ownership(
            _param_value(params, "Право собственности", "re_ownership", "ownership"),
            description,
        )

        coordinates = item.get("coordinates") or []
        latitude: Optional[float] = None
        longitude: Optional[float] = None
        if isinstance(coordinates, list) and len(coordinates) >= 2:
            latitude = parse_float(coordinates[0])
            longitude = parse_float(coordinates[1])

        distance, distance_evidence = extract_distance_km(description)
        if distance is None and latitude is not None and longitude is not None:
            distance = distance_to_mkad_km(latitude, longitude)
            distance_evidence = (
                "По прямой до контура МКАД по координатам "
                f"{latitude:.5f}, {longitude:.5f}"
            )

        electricity_kw, electricity_evidence = extract_electricity_kw(
            electricity_raw,
            description,
        )
        facade, depth, dimensions_evidence = extract_dimensions(description)

        address = (
            plain_text(item.get("addressWithDistrict"))
            or plain_text(item.get("address"))
            or _param_value(account_params, "Адрес", "address")
        )
        district = (
            _param_value(params, "Город / Район", "area")
            or plain_text(item.get("region"))
        )

        evidence: Dict[str, str] = {}
        if electricity_evidence:
            evidence["electricity"] = electricity_evidence
        if distance_evidence:
            evidence["distance"] = distance_evidence
        if dimensions_evidence:
            evidence["dimensions"] = dimensions_evidence

        company_ad = bool(initial.get("company_ad") or item.get("isCompanyAd"))
        seller_type = "Компания" if company_ad else "Частное лицо"

        list_time = initial.get("list_time") or item.get("date")
        house_type = _param_value(
            params,
            "Вид объекта",
            "house_type_for_sell",
            "house_type",
        )
        is_house = bool(house_type) or str(item.get("category") or "") == "1020"
        object_kind = "Дом с участком" if is_house else "Участок"
        house_area = (
            parse_float(_param_value(params, "Площадь дома, м²", "size"))
            if is_house
            else None
        )
        house_condition = (
            join_house_condition(
                _param_value(params, "Состояние дома", "house_readiness"),
                _param_value(params, "Состояние", "condition"),
                _param_value(params, "Год постройки", "year_built", "build_year"),
                _param_value(params, "Материал стен", "wall_material"),
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
            district=district,
            locality=_locality_from_address(address),
            address=address,
            price_original=price_byn,
            currency_original="BYN" if price_byn is not None else None,
            price_usd=price_usd,
            area_sotok=area,
            distance_mkad_km=distance,
            latitude=latitude,
            longitude=longitude,
            facade_m=facade,
            depth_m=depth,
            purpose=purpose,
            electricity_raw=electricity_raw,
            electricity_kw=electricity_kw,
            gas_raw=gas_raw,
            water_raw=water_raw,
            sewerage_raw=sewerage_raw,
            internet_raw=_find_text(description, ["интернет", "оптоволокно", "оптика"]),
            road_raw=_find_text(description, ["асфальт", "подъезд", "дорог"]),
            nature_raw=_find_text(description, ["лес", "озеро", "водоем", "река"]),
            ownership_raw=ownership_raw,
            seller_type=seller_type,
            object_kind=object_kind,
            house_area_sqm=house_area,
            house_condition=house_condition,
            sale_format="Частная продажа" if is_house else None,
            house_risks=house_risks,
            source_created_at=parse_datetime(list_time),
            source_updated_at=parse_datetime(list_time),
            evidence=evidence,
            raw_payload={
                "garden_community": _param_value(
                    params,
                    "Участок расположен в садовом товариществе",
                    "re_garden_community",
                ),
                "has_house": is_house,
                "object_kind": object_kind,
                "seller_id": initial.get("account_id") or item.get("account_id"),
            },
        )


def _parameter_map(values: Any) -> Dict[str, Tuple[Any, Any]]:
    result: Dict[str, Tuple[Any, Any]] = {}
    if not isinstance(values, list):
        return result
    for item in values:
        if not isinstance(item, dict):
            continue
        label = item.get("pl") or item.get("label")
        code = item.get("p") or item.get("name")
        display = item.get("vl")
        raw = item.get("v")
        value = display if display not in (None, "") else raw
        if label:
            result[str(label).lower()] = (value, raw)
        if code:
            result[str(code).lower()] = (value, raw)
    return result


def _param_value(params: Dict[str, Tuple[Any, Any]], *keys: str) -> Optional[str]:
    for key in keys:
        found = params.get(key.lower())
        if found:
            value = found[0]
            return plain_text(str(value)) if value is not None else None
    return None


def _calculator_price(calculator: Any, currency: str) -> Optional[Any]:
    if not isinstance(calculator, list):
        return None
    for item in calculator:
        if isinstance(item, dict) and item.get("currency") == currency:
            return item.get("price")
    return None


def _prioritized_detail_ids(
    source_ids: List[str],
    known_listing_ids: Set[str],
    known_distance_ids: Set[str],
    pending_notification_ids: Set[str],
    limit: int,
    *,
    slot: Optional[int] = None,
) -> Set[str]:
    priorities = (
        [source_id for source_id in source_ids if source_id not in known_listing_ids],
        [source_id for source_id in source_ids if source_id in pending_notification_ids],
        [source_id for source_id in source_ids if source_id not in known_distance_ids],
        source_ids,
    )
    selected: List[str] = []
    for values in priorities:
        if len(selected) >= limit:
            break
        selected_set = set(selected)
        candidates = [value for value in values if value not in selected_set]
        remaining = limit - len(selected)
        selected.extend(_rotating_detail_ids(candidates, remaining, slot=slot))
    return set(selected)


def _money_from_cents(value: Any) -> Optional[float]:
    parsed = parse_float(value)
    return round(parsed / 100, 2) if parsed is not None and parsed > 0 else None


def _next_page_url(base_url: str, pagination: Any) -> Optional[str]:
    if not isinstance(pagination, list):
        return None
    token = None
    for item in pagination:
        if isinstance(item, dict) and item.get("label") == "next":
            token = item.get("token")
            break
    if not token:
        return None
    parsed = urlsplit(base_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["cur"] = query.get("cur", "USD")
    query["size"] = query.get("size", "30")
    query["cursor"] = str(token)
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
    )


def _locality_from_address(address: Optional[str]) -> Optional[str]:
    if not address:
        return None
    parts = [part.strip() for part in address.split(",") if part.strip()]
    for part in parts:
        lowered = part.lower()
        if any(
            marker in lowered
            for marker in (
                "дерев",
                "агрогород",
                "посел",
                "садов",
                "ст ",
                "минск",
                "борисов",
                "логойск",
                "смолевич",
                "дзержинск",
            )
        ):
            return part
    return parts[-1] if parts else None


def _find_text(text: str, needles: List[str]) -> Optional[str]:
    lowered = text.lower()
    for needle in needles:
        position = lowered.find(needle)
        if position >= 0:
            return text[max(0, position - 70) : min(len(text), position + 120)]
    return None
