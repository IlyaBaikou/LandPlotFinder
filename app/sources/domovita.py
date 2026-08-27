from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

from app.domain import NormalizedListing
from app.geo import distance_to_mkad_km
from app.house import detect_house_risks, join_house_condition
from app.normalization import (
    canonical_url,
    classify_electricity,
    classify_gas,
    classify_internet,
    classify_sewerage,
    classify_water,
    extract_dimensions,
    extract_distance_km,
    extract_electricity_kw,
    normalized_ownership,
    parse_datetime,
    parse_float,
    plain_text,
)
from app.sources.base import ListingSource
from app.sources.rotation import rotating_detail_ids

LOGGER = logging.getLogger(__name__)


class DomovitaSource(ListingSource):
    """Public Domovita catalog pages for plots, houses and cottages."""

    name = "domovita"

    def __init__(self, *args: Any, max_search_pages: int = 2, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.max_search_pages = max(1, max_search_pages)

    def scan(self) -> List[NormalizedListing]:
        summaries: Dict[str, Dict[str, Any]] = {}
        successful_pages = 0
        errors: List[str] = []
        for search_url in self.search_urls:
            for page in range(1, self.max_search_pages + 1):
                page_url = _page_url(search_url, page)
                try:
                    html = self.client.get_text(page_url)
                    items = _parse_catalog(html)
                    successful_pages += 1
                except Exception as exc:
                    LOGGER.warning("Unable to load Domovita catalog %s: %s", page_url, exc)
                    errors.append(f"{page_url}: {exc}")
                    break
                for item in items:
                    summaries[item["source_id"]] = item
                if not items:
                    break

        if not successful_pages:
            raise RuntimeError(
                "Domovita catalog unavailable: " + (errors[0] if errors else "no pages")
            )

        normalized = {source_id: self._normalize(item) for source_id, item in summaries.items()}
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
                    html = self.client.get_text(summary.canonical_url)
                    enriched = _parse_detail(html)
                    merged = _merge_item(summaries[source_id], enriched)
                    summary = self._normalize(merged)
                    summary.raw_payload["detail_loaded"] = True
                except Exception as exc:
                    LOGGER.warning("Unable to load Domovita detail %s: %s", source_id, exc)
                    summary.reasons.append(f"Не удалось загрузить детали Domovita: {exc}")
            listings.append(summary)
        return listings

    def _normalize(self, item: Dict[str, Any]) -> NormalizedListing:
        title = plain_text(item.get("title")) or f"Объект Domovita {item['source_id']}"
        description = plain_text(item.get("description"))
        combined = " ".join(value for value in [title, description] if value)
        object_type = str(item.get("object_type") or "")
        url = canonical_url(str(item["url"]))
        is_house = bool(
            "house" in object_type.lower()
            or "cottage" in object_type.lower()
            or "/houses/" in url
            or "/cottages/" in url
        )
        object_kind = "Дом с участком" if is_house else "Участок"

        params = item.get("params") if isinstance(item.get("params"), dict) else {}
        latitude = parse_float(item.get("latitude"))
        longitude = parse_float(item.get("longitude"))
        distance = parse_float(params.get("Удаленность от МКАД"))
        distance_evidence = (
            f"Структурированное поле Domovita: {params.get('Удаленность от МКАД')}"
            if distance is not None
            else None
        )
        if distance is None:
            distance, distance_evidence = extract_distance_km(combined)
        if distance is None and latitude is not None and longitude is not None:
            distance = distance_to_mkad_km(latitude, longitude)
            distance_evidence = (
                f"По прямой до контура МКАД по координатам {latitude:.5f}, {longitude:.5f}"
            )

        area = parse_float(params.get("Участок")) or _extract_land_area(combined)
        electricity, electricity_evidence = classify_electricity([description])
        electricity_kw, kw_evidence = extract_electricity_kw(description)
        gas, gas_evidence = classify_gas([description])
        water, water_evidence = classify_water([description])
        sewerage, sewerage_evidence = classify_sewerage([description])
        internet, internet_evidence = classify_internet([description])
        facade, depth, dimensions_evidence = extract_dimensions(description)

        locality = _optional(params.get("Город")) or _optional(item.get("locality"))
        address = _optional(params.get("Адрес")) or _optional(item.get("address"))
        direction = _optional(params.get("Направление")) or _extract_direction(combined)
        district = _extract_district(title)
        ownership = normalized_ownership(description)
        house_area = (
            parse_float(params.get("Общая площадь")) or _extract_house_area(description)
            if is_house
            else None
        )
        house_condition = (
            join_house_condition(
                item.get("house_condition"),
                _find_evidence(description, ["год постройки", "реконструирован", "состояние"]),
            )
            if is_house
            else None
        )

        evidence: Dict[str, str] = {}
        for key, value in {
            "distance": distance_evidence,
            "electricity": kw_evidence or electricity_evidence,
            "gas": gas_evidence,
            "water": water_evidence,
            "sewerage": sewerage_evidence,
            "internet": internet_evidence,
            "dimensions": dimensions_evidence,
        }.items():
            if value:
                evidence[key] = value

        seller = item.get("seller") if isinstance(item.get("seller"), dict) else {}
        seller_name = _optional(seller.get("name"))
        seller_type = seller_name
        image_urls = item.get("image_urls") if isinstance(item.get("image_urls"), list) else []

        return NormalizedListing(
            source=self.name,
            source_id=str(item["source_id"]),
            canonical_url=url,
            title=title,
            description=description,
            district=district,
            locality=locality,
            address=address,
            direction=direction,
            price_original=parse_float(item.get("price_original")),
            currency_original=_optional(item.get("currency_original")),
            price_usd=parse_float(item.get("price_usd")),
            area_sotok=area,
            distance_mkad_km=distance,
            latitude=latitude,
            longitude=longitude,
            facade_m=facade,
            depth_m=depth,
            purpose=_find_evidence(description, ["назначение", "лпх", "ижс"]),
            electricity_raw=(
                _optional(item.get("electricity_raw"))
                or electricity_evidence
                or electricity
                or _find_evidence(description, ["электричество", "электро", "свет"])
            ),
            electricity_kw=electricity_kw,
            gas_raw=(
                _optional(item.get("gas_raw"))
                or gas_evidence
                or gas
                or _find_evidence(description, ["газ"])
            ),
            water_raw=(
                _optional(item.get("water_raw"))
                or water_evidence
                or water
                or _find_evidence(description, ["вода"])
            ),
            sewerage_raw=(
                _optional(item.get("sewerage_raw"))
                or sewerage_evidence
                or sewerage
                or _find_evidence(description, ["канализац", "септик"])
            ),
            internet_raw=(
                internet_evidence
                or internet
                or _find_evidence(description, ["интернет", "оптоволокно"])
            ),
            road_raw=_find_evidence(description, ["асфальт", "подъезд", "дорог"]),
            nature_raw=_find_evidence(description, ["лес", "озеро", "водоем", "река"]),
            ownership_raw=ownership,
            seller_type=seller_type,
            object_kind=object_kind,
            house_area_sqm=house_area,
            house_condition=house_condition,
            sale_format="Частная продажа",
            house_risks=(
                detect_house_risks([title, description, house_condition]) if is_house else None
            ),
            source_created_at=parse_datetime(item.get("created_at")),
            source_updated_at=parse_datetime(item.get("updated_at")),
            evidence=evidence,
            raw_payload={
                "has_house": is_house,
                "object_kind": object_kind,
                "object_type": object_type,
                "seller_id": seller_name,
                "images": image_urls,
                "params": params,
            },
        )


def _parse_catalog(html: str) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select(".found_item[data-key]")
    if not cards:
        if soup.select_one("#__NEXT_DATA__"):
            raise ValueError("Domovita catalog changed: listing cards missing")
        raise ValueError("Domovita catalog response is not a listing page")
    return [_parse_card(card) for card in cards]


def _parse_card(card: Tag) -> Dict[str, Any]:
    place = _schema(card, "Place") or {}
    link = card.select_one("a.title--listing") or card.select_one("a.link-object")
    url = str((link or {}).get("href") or place.get("url") or place.get("@id") or "")
    source_id = str(card.get("data-key") or place.get("branchCode") or "")
    if not url or not source_id:
        raise ValueError("Domovita card is missing URL or source ID")
    geo = place.get("geo") if isinstance(place.get("geo"), dict) else {}
    address = place.get("address") if isinstance(place.get("address"), dict) else {}
    price_usd_node = card.select_one(".price-usd")
    date_node = card.select_one(".date")
    title = plain_text(link.get_text(" ") if link else str(place.get("name") or ""))
    description_node = card.select_one(".text-block")
    description = plain_text(
        str(place.get("description") or "")
        or (description_node.get_text(" ") if description_node else "")
    )
    return {
        "source_id": source_id,
        "url": url,
        "title": title,
        "description": description,
        "object_type": str(card.get("data-object-type") or ""),
        "price_usd": _money(price_usd_node.get_text(" ") if price_usd_node else None),
        "latitude": geo.get("latitude"),
        "longitude": geo.get("longitude"),
        "locality": address.get("addressLocality"),
        "address": address.get("streetAddress"),
        "updated_at": _domovita_date(date_node.get_text(" ") if date_node else None),
        "image_urls": _image_urls(card, place),
        "params": {},
    }


def _parse_detail(html: str) -> Dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    product = _schema(soup, "Product") or {}
    place = _schema(soup, "Place") or {}
    if not product and not place:
        raise ValueError("Domovita detail changed: structured object missing")
    geo = place.get("geo") if isinstance(place.get("geo"), dict) else {}
    address = place.get("address") if isinstance(place.get("address"), dict) else {}
    offer = product.get("offers") if isinstance(product.get("offers"), dict) else {}
    seller = offer.get("seller") if isinstance(offer.get("seller"), dict) else {}
    description_node = soup.select_one("#object-description")
    params = _detail_params(soup)
    price_usd_node = soup.select_one(".price-usd")
    currency = str(offer.get("priceCurrency") or "") or None
    model_id = soup.select_one("#model_id")
    object_type = soup.select_one("#className")
    source_id = str(
        product.get("productID")
        or place.get("branchCode")
        or (model_id.get("value") if model_id else "")
    )
    result = {
        "source_id": source_id,
        "url": product.get("url") or place.get("url") or place.get("@id"),
        "title": product.get("name") or place.get("name"),
        "description": (
            product.get("description")
            or place.get("description")
            or (description_node.get_text(" ") if description_node else None)
        ),
        "object_type": object_type.get("value") if object_type else None,
        "price_usd": (
            offer.get("price")
            if currency == "USD"
            else _money(price_usd_node.get_text(" ") if price_usd_node else None)
        ),
        "price_original": offer.get("price"),
        "currency_original": currency,
        "latitude": geo.get("latitude"),
        "longitude": geo.get("longitude"),
        "locality": address.get("addressLocality"),
        "address": address.get("streetAddress"),
        "created_at": product.get("productionDate"),
        "updated_at": product.get("releaseDate"),
        "seller": seller,
        "params": params,
        "image_urls": _image_urls(soup, product or place),
    }
    object_data = _next_object(soup)
    if object_data:
        object_address = (
            object_data.get("address") if isinstance(object_data.get("address"), dict) else {}
        )
        coords = (
            object_address.get("coords") if isinstance(object_address.get("coords"), dict) else {}
        )
        town = object_address.get("town") if isinstance(object_address.get("town"), dict) else {}
        areas = object_data.get("area") if isinstance(object_data.get("area"), dict) else {}
        prices = object_data.get("price") if isinstance(object_data.get("price"), dict) else {}
        object_params = {
            "Город": town.get("name"),
            "Адрес": " ".join(
                str(value)
                for value in [
                    object_address.get("street_name"),
                    object_address.get("house_number"),
                ]
                if value
            ),
            "Направление": object_data.get("direction_name"),
            "Удаленность от МКАД": object_data.get("town_distance"),
            "Участок": object_data.get("area_ground"),
            "Общая площадь": areas.get("total"),
        }
        photos = object_data.get("photos") if isinstance(object_data.get("photos"), list) else []
        photo_urls = [
            str(photo.get("url"))
            for photo in photos
            if isinstance(photo, dict) and photo.get("url")
        ]
        result = _merge_item(
            result,
            {
                "source_id": object_data.get("id"),
                "url": object_data.get("url"),
                "title": object_data.get("title"),
                "description": object_data.get("description"),
                "object_type": object_data.get("type"),
                "price_usd": prices.get("USD"),
                "price_original": prices.get("BYN"),
                "currency_original": "BYN" if prices.get("BYN") is not None else None,
                "latitude": coords.get("position_x"),
                "longitude": coords.get("position_y"),
                "locality": town.get("name"),
                "address": object_params["Адрес"],
                "created_at": object_data.get("date_reception"),
                "updated_at": object_data.get("date_revision"),
                "seller": {"name": object_data.get("owner_type")},
                "params": {
                    key: value for key, value in object_params.items() if value not in (None, "")
                },
                "image_urls": photo_urls,
                "electricity_raw": ("Есть" if object_data.get("electro") else None),
                "gas_raw": object_data.get("gas"),
                "water_raw": object_data.get("water"),
                "sewerage_raw": object_data.get("sewer"),
                "house_condition": join_house_condition(
                    object_data.get("repair_state"),
                    (
                        f"Год постройки: {object_data.get('building_year')}"
                        if object_data.get("building_year")
                        else None
                    ),
                    object_data.get("walls_material"),
                ),
            },
        )
    return result


def _schema(root: Tag | BeautifulSoup, schema_type: str) -> Optional[Dict[str, Any]]:
    for script in root.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(script.string or script.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, dict) and item.get("@type") == schema_type:
                return item
    return None


def _next_object(soup: BeautifulSoup) -> Optional[Dict[str, Any]]:
    script = soup.select_one("#__NEXT_DATA__")
    if not script:
        return None
    try:
        data = json.loads(script.string or script.get_text())
    except (TypeError, json.JSONDecodeError):
        return None
    value = data.get("props", {}).get("pageProps", {}).get("objectData")
    return value if isinstance(value, dict) else None


def _detail_params(soup: BeautifulSoup) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for row in soup.select(".object-info__parametr"):
        spans = row.find_all("span", recursive=False)
        if len(spans) < 2:
            continue
        key = plain_text(spans[0].get_text(" "))
        value = plain_text(spans[-1].get_text(" "))
        if key and value:
            result[key] = value
    return result


def _merge_item(summary: Dict[str, Any], detail: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(summary)
    for key, value in detail.items():
        if value not in (None, "", [], {}):
            result[key] = value
    result["params"] = {**summary.get("params", {}), **detail.get("params", {})}
    result["image_urls"] = list(
        dict.fromkeys([*summary.get("image_urls", []), *detail.get("image_urls", [])])
    )
    return result


def _page_url(url: str, page: int) -> str:
    if page <= 1:
        return url
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["page"] = str(page)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _money(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    match = re.search(r"(\d[\d\s\u00a0]*(?:[.,]\d+)?)", value)
    return parse_float(match.group(1).replace(" ", "").replace("\u00a0", "")) if match else None


def _domovita_date(value: Optional[str]) -> Optional[str]:
    match = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", value or "")
    if not match:
        return None
    day, month, year = match.groups()
    return datetime(int(year), int(month), int(day)).isoformat()


def _extract_land_area(text: str) -> Optional[float]:
    patterns = [
        r"(?:участок|земельный\s+участок)[^\d]{0,35}(\d+(?:[.,]\d+)?)\s*(?:сот|соток)",
        r"(\d+(?:[.,]\d+)?)\s*[- ]?(?:сот|соток)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return parse_float(match.group(1))
    return None


def _extract_house_area(text: str) -> Optional[float]:
    patterns = [
        r"(?:общая\s+площадь|площадь\s+дома)[^\d]{0,25}(\d+(?:[.,]\d+)?)\s*(?:м2|м²|кв\.?\s*м)",
        r"дом[^.]{0,60}?(\d+(?:[.,]\d+)?)\s*(?:м2|м²|кв\.?\s*м)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return parse_float(match.group(1))
    return None


def _extract_district(text: str) -> Optional[str]:
    match = re.search(r"([А-ЯЁA-Z][а-яёa-z-]+)\s+р-н", text)
    return f"{match.group(1)} район" if match else None


def _extract_direction(text: str) -> Optional[str]:
    match = re.search(r"([А-ЯЁA-Z][а-яёa-z-]+)\s+направлен", text)
    return match.group(1) if match else None


def _find_evidence(text: str, needles: List[str]) -> Optional[str]:
    lowered = text.lower().replace("ё", "е")
    for needle in needles:
        position = lowered.find(needle.lower().replace("ё", "е"))
        if position >= 0:
            return text[max(0, position - 70) : min(len(text), position + len(needle) + 120)]
    return None


def _image_urls(root: Tag | BeautifulSoup, schema: Dict[str, Any]) -> List[str]:
    result: List[str] = []
    image = schema.get("image")
    if isinstance(image, str):
        result.append(image)
    elif isinstance(image, dict):
        result.extend(str(image[key]) for key in ("url", "thumbnail") if image.get(key))
    for node in root.select("img[src]")[:10]:
        src = str(node.get("src") or "")
        if src.startswith("http"):
            result.append(src)
    return list(dict.fromkeys(result))


def _optional(value: Any) -> Optional[str]:
    text = plain_text(str(value)) if value not in (None, "") else ""
    return text or None
