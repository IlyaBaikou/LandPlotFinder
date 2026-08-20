from __future__ import annotations

import re
import time
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from app.domain import NormalizedListing
from app.exchange import byn_to_usd
from app.normalization import (
    canonical_url,
    extract_distance_km,
    extract_electricity_kw,
    normalized_ownership,
    parse_float,
    plain_text,
)
from app.sources.base import ListingSource

AREA_RE = re.compile(
    r"(?:площад\w*(?:\s+участка)?|участ\w*[^.]{0,50}?площад\w*)"
    r"[^.\d]{0,30}(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>га|сот(?:ок|ки)?|м²|кв\.?\s*м)",
    re.IGNORECASE,
)
CADASTRAL_RE = re.compile(r"\b\d{10,22}\b")
DEADLINE_RE = re.compile(
    r"(?:при[её]м\s+заявок\s+до|окончани\w*\s+при[её]ма)[^.\d]{0,30}"
    r"(?P<value>\d{1,2}\.\d{1,2}\.\d{2,4}(?:\s+\d{1,2}:\d{2})?)",
    re.IGNORECASE,
)
OUTSIDE_MINSK_REGION = (
    "брестская область",
    "витебская область",
    "гомельская область",
    "гродненская область",
    "могилевская область",
    "брестский район",
    "витебский район",
    "гомельский район",
    "гродненский район",
    "могилевский район",
)


class EauctionSource(ListingSource):
    name = "e_auction"

    def scan(self) -> List[NormalizedListing]:
        cards: Dict[str, NormalizedListing] = {}
        for search_url in self.search_urls:
            soup = BeautifulSoup(self.client.get_text(search_url), "html.parser")
            for card in soup.select("a.product-item[href]"):
                listing = self._normalize_card(card, search_url)
                if listing and self.passes_basic_prefilter(listing):
                    cards[listing.source_id] = listing

        listings: List[NormalizedListing] = []
        for listing in list(cards.values())[: self.max_details]:
            try:
                html = self.client.get_text(listing.canonical_url)
                listings.append(self._normalize_detail(listing, html))
            except Exception as exc:
                listing.reasons.append(f"Не удалось загрузить детали e-auction.by: {exc}")
                listings.append(listing)
        return listings

    def _normalize_card(self, card: Tag, search_url: str) -> Optional[NormalizedListing]:
        end_request = parse_float(card.get("data-endrequest"))
        if end_request is not None and end_request < time.time():
            return None

        href = str(card.get("href") or "")
        url = canonical_url(urljoin(search_url, href))
        lot_element = card.select_one(".product_art")
        source_id = plain_text(lot_element.get_text(" ", strip=True) if lot_element else "")
        if not source_id:
            source_id = urlsplit(url).path.rstrip("/").split("/")[-1]
        if not source_id:
            return None

        title_element = card.select_one(".text-header")
        title = plain_text(
            title_element.get_text(" ", strip=True) if title_element else card.get("title")
        )
        card_text = plain_text(card.get_text(" ", strip=True))
        price_element = card.select_one(".popup-exchange-tooltip[data-cur='BYN'][data-value]")
        price_byn = parse_float(price_element.get("data-value")) if price_element else None
        area = _extract_area_sotok(f"{title} {card_text}")
        kind_element = card.select_one(".lot-type")
        auction_kind = plain_text(
            kind_element.get_text(" ", strip=True) if kind_element else ""
        )
        deadline = _extract_deadline(card_text)
        reasons = [_auction_warning(auction_kind)]
        if deadline:
            reasons.append(f"Срок подачи заявки: {deadline}")

        price_usd = byn_to_usd(self.client, price_byn)
        return NormalizedListing(
            source=self.name,
            source_id=source_id,
            canonical_url=url,
            title=title or f"Земельный участок, лот {source_id}",
            description=card_text,
            price_original=price_byn,
            currency_original="BYN" if price_byn is not None else None,
            price_usd=price_usd,
            area_sotok=area,
            purpose="Продажа земельного участка на электронных торгах",
            seller_type="Организатор электронных торгов",
            reasons=reasons,
            raw_payload={
                "is_auction": True,
                "auction_kind": auction_kind or None,
                "application_deadline": deadline,
                "byn_per_usd": _used_rate(price_byn, price_usd),
            },
        )

    def _normalize_detail(
        self, listing: NormalizedListing, html: str
    ) -> NormalizedListing:
        soup = BeautifulSoup(html, "html.parser")
        values = _labeled_values(soup)
        full_text = plain_text(soup.get_text(" ", strip=True))
        description = _value_for(values, "описание имущества") or full_text
        address = _value_for(
            values,
            "местоположение имущества",
            "местонахождение имущества",
            "адрес",
        )
        area_text = _value_for(values, "площадь участка", "площадь земельного участка")
        area = _extract_area_sotok(area_text or "") or listing.area_sotok
        cadastral = _value_for(values, "кадастровый номер") or _extract_cadastral(description)
        encumbrances = _value_for(values, "обременения", "ограничения")
        ownership = normalized_ownership(
            _value_for(values, "право", "форма собственности"),
            description,
        )
        distance, distance_evidence = extract_distance_km(description, address)
        electricity_kw, electricity_evidence = extract_electricity_kw(description)
        deadline = listing.raw_payload.get("application_deadline") or _extract_deadline(
            full_text
        )
        outside_region = _outside_minsk_region(f"{address or ''} {description}")

        if distance_evidence:
            listing.evidence["distance"] = distance_evidence
        if electricity_evidence:
            listing.evidence["electricity"] = electricity_evidence
        if cadastral:
            listing.evidence["cadastral_number"] = cadastral
        if encumbrances:
            listing.reasons.append(f"Обременения: {encumbrances}")

        listing.description = description
        listing.address = address
        listing.district = _district_from_text(address or description)
        listing.locality = _locality_from_address(address)
        listing.area_sotok = area
        listing.distance_mkad_km = distance
        listing.purpose = (
            _value_for(values, "назначение", "целевое назначение")
            or listing.purpose
        )
        listing.electricity_raw = _fragment(description, "электр")
        listing.electricity_kw = electricity_kw
        listing.gas_raw = _fragment(description, "газ")
        listing.water_raw = _fragment(description, "вод")
        listing.ownership_raw = ownership
        listing.raw_payload.update(
            {
                "application_deadline": deadline,
                "cadastral_number": cadastral,
                "encumbrances": encumbrances,
                "outside_target_region": outside_region,
            }
        )
        return listing


def _labeled_values(soup: BeautifulSoup) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for row in soup.select("tr"):
        cells = row.select("th, td")
        if len(cells) >= 2:
            key = plain_text(cells[0].get_text(" ", strip=True)).rstrip(":").lower()
            value = plain_text(cells[-1].get_text(" ", strip=True))
            if key and value:
                values[key] = value
    return values


def _value_for(values: Dict[str, str], *needles: str) -> Optional[str]:
    for needle in needles:
        for key, value in values.items():
            if needle in key:
                return value
    return None


def _extract_area_sotok(text: str) -> Optional[float]:
    match = AREA_RE.search(text)
    if not match:
        return None
    value = parse_float(match.group("value"))
    if value is None:
        return None
    unit = match.group("unit").lower()
    if unit == "га":
        value *= 100
    elif "м" in unit:
        value /= 100
    return round(value, 4)


def _extract_cadastral(text: str) -> Optional[str]:
    match = CADASTRAL_RE.search(text.replace(":", ""))
    return match.group(0) if match else None


def _extract_deadline(text: str) -> Optional[str]:
    match = DEADLINE_RE.search(text)
    return match.group("value") if match else None


def _auction_warning(kind: str) -> str:
    if "арест" in kind.lower():
        return (
            "Арестованное имущество: проверить исполнительное производство, "
            "обременения и возможность регистрации"
        )
    return "Электронный аукцион: проверить задаток, документы и условия участия"


def _outside_minsk_region(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in OUTSIDE_MINSK_REGION)


def _district_from_text(text: str) -> Optional[str]:
    match = re.search(r"[^,.]{1,60}\s+район", text, re.IGNORECASE)
    return plain_text(match.group(0)) if match else None


def _locality_from_address(address: Optional[str]) -> Optional[str]:
    if not address:
        return None
    parts = [part.strip() for part in address.split(",") if part.strip()]
    return parts[-1] if parts else None


def _fragment(text: str, needle: str) -> Optional[str]:
    position = text.lower().find(needle)
    if position < 0:
        return None
    return text[max(0, position - 70) : min(len(text), position + 150)]


def _used_rate(price_byn: Optional[float], price_usd: Optional[float]) -> Optional[float]:
    if price_byn is None or not price_usd:
        return None
    return round(price_byn / price_usd, 6)
