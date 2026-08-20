from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple
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
from app.sources.realt_auction import _extract_area_sotok

DATE_RE = re.compile(r"\b(?P<day>\d{1,2})\.(?P<month>\d{1,2})\.(?P<year>\d{4})\b")
PRICE_PATTERNS = (
    re.compile(
        r"начальн\w*\s+цен\w*[^:\d]{0,60}:?\s*"
        r"(?P<value>\d[\d\s]*(?:[.,]\d{1,2})?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?P<value>\d[\d\s]*(?:[.,]\d{1,2})?)\s*(?:byn|руб\.?)"
        r"[^.]{0,50}начальн\w*\s+цен",
        re.IGNORECASE,
    ),
)
CADASTRAL_RE = re.compile(r"\b\d{18,22}\b")
LAND_WORDS = ("земельный участок", "земельного участка", "земельные участки")


class RltAuctionSource(ListingSource):
    name = "rlt_auction"

    def scan(self) -> List[NormalizedListing]:
        candidates: Dict[str, Tuple[str, str, str]] = {}
        errors: List[str] = []
        successful_catalogs = 0
        for search_url in self.search_urls:
            try:
                html = self.client.get_text(search_url)
            except Exception as exc:
                errors.append(f"{search_url}: {exc}")
                continue
            successful_catalogs += 1
            soup = BeautifulSoup(html, "html.parser")
            for link in soup.select("a[href]"):
                candidate = _candidate_from_element(
                    link, str(link.get("href") or ""), search_url
                )
                if candidate is None:
                    continue
                source_id, url, title, card_text = candidate
                if _is_finished(card_text):
                    continue
                candidates[source_id] = (url, title, card_text)
            for element in soup.select("[data-href], [data-url], [onclick]"):
                href = _element_href(element)
                if not href:
                    continue
                candidate = _candidate_from_element(element, href, search_url)
                if candidate is None:
                    continue
                source_id, url, title, card_text = candidate
                if not _is_finished(card_text):
                    candidates[source_id] = (url, title, card_text)

        if not successful_catalogs:
            raise RuntimeError("; ".join(errors))

        listings: List[NormalizedListing] = []
        for source_id, (url, title, card_text) in list(candidates.items())[
            : self.max_details
        ]:
            try:
                detail_html = self.client.get_text(url)
                listings.append(
                    self._normalize_detail(source_id, url, title, card_text, detail_html)
                )
            except Exception as exc:
                listing = self._normalize_text(source_id, url, title, card_text)
                listing.reasons.append(f"Не удалось загрузить детали rlt.by: {exc}")
                listings.append(listing)
        return listings

    def _normalize_detail(
        self, source_id: str, url: str, title: str, card_text: str, html: str
    ) -> NormalizedListing:
        soup = BeautifulSoup(html, "html.parser")
        main = soup.select_one("main, article, .auction, .content") or soup
        detail_text = plain_text(main.get_text(" ", strip=True))
        return self._normalize_text(
            source_id,
            url,
            title,
            f"{card_text} {detail_text}",
        )

    def _normalize_text(
        self, source_id: str, url: str, title: str, text: str
    ) -> NormalizedListing:
        area = _extract_area_sotok(text)
        price_byn = _extract_price_byn(text)
        price_usd = byn_to_usd(self.client, price_byn)
        distance, distance_evidence = extract_distance_km(text)
        electricity_kw, electricity_evidence = extract_electricity_kw(text)
        cadastral = _extract_cadastral(text)
        deadline = _extract_deadline(text)
        address = _extract_address(text)
        evidence: Dict[str, str] = {}
        if distance_evidence:
            evidence["distance"] = distance_evidence
        if electricity_evidence:
            evidence["electricity"] = electricity_evidence
        if cadastral:
            evidence["cadastral_number"] = cadastral
        if deadline:
            evidence["auction_deadline"] = deadline

        reasons = [
            "Аукцион rlt.by: проверить задаток, документы, ограничения и рост цены"
        ]
        if deadline:
            reasons.append(f"Срок подачи заявки: {deadline}")

        event_date = _latest_date(text)
        event_datetime = (
            datetime.combine(event_date, datetime.min.time()) if event_date else None
        )
        return NormalizedListing(
            source=self.name,
            source_id=source_id,
            canonical_url=url,
            title=title,
            description=text,
            district=_district_from_text(address or text),
            locality=_locality_from_address(address),
            address=address,
            price_original=price_byn,
            currency_original="BYN" if price_byn is not None else None,
            price_usd=price_usd,
            area_sotok=area,
            distance_mkad_km=distance,
            purpose="Продажа земельного участка на аукционе",
            electricity_raw=_fragment(text, "электр"),
            electricity_kw=electricity_kw,
            gas_raw=_fragment(text, "газ"),
            water_raw=_fragment(text, "вод"),
            ownership_raw=normalized_ownership(text),
            seller_type="Организатор аукциона",
            source_created_at=event_datetime,
            source_updated_at=event_datetime,
            evidence=evidence,
            reasons=reasons,
            raw_payload={
                "is_auction": True,
                "auction_date": event_date.isoformat() if event_date else None,
                "auction_deadline": deadline,
                "cadastral_number": cadastral,
                "byn_per_usd": _used_rate(price_byn, price_usd),
            },
        )


def _candidate_from_element(
    element: Tag, href: str, search_url: str
) -> Optional[Tuple[str, str, str, str]]:
    if href.startswith(("#", "mailto:", "tel:", "javascript:")):
        return None
    url = canonical_url(urljoin(search_url, href))
    path = urlsplit(url).path.rstrip("/")
    if not path or path in {"/aukciony", "/"}:
        return None

    container = element
    for selector in (
        ".auction-item",
        ".auction-card",
        ".item-auction",
        ".views-row",
        "article",
        "li",
    ):
        found = (
            element.find_parent(class_=selector[1:])
            if selector.startswith(".")
            else element.find_parent(selector)
        )
        if found is not None:
            container = found
            break
    card_text = plain_text(container.get_text(" ", strip=True))
    link_text = plain_text(element.get_text(" ", strip=True))
    combined = f"{link_text} {card_text}".lower()
    if not any(word in combined for word in LAND_WORDS):
        return None
    heading = container.select_one("h1, h2, h3, h4, .title, .text-header")
    title = plain_text(heading.get_text(" ", strip=True) if heading else link_text)
    if not title:
        return None
    source_id = _source_id(url)
    return source_id, url, title, card_text


def _element_href(element: Tag) -> Optional[str]:
    for attribute in ("data-href", "data-url"):
        value = element.get(attribute)
        if value:
            return str(value)
    onclick = str(element.get("onclick") or "")
    match = re.search(r"""['"](?P<url>/[^'"]+)['"]""", onclick)
    return match.group("url") if match else None


def _source_id(url: str) -> str:
    path = urlsplit(url).path.rstrip("/")
    slug = path.split("/")[-1]
    numeric = re.search(r"\d{3,}", slug)
    if numeric:
        return numeric.group(0)
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


def _is_finished(text: str) -> bool:
    lowered = text.lower()
    if any(word in lowered for word in ("завершен", "завершён", "состоялся", "не состоялся")):
        return True
    dates = _dates(text)
    return bool(dates and max(dates) < date.today())


def _dates(text: str) -> List[date]:
    values: List[date] = []
    for match in DATE_RE.finditer(text):
        try:
            values.append(
                date(
                    int(match.group("year")),
                    int(match.group("month")),
                    int(match.group("day")),
                )
            )
        except ValueError:
            continue
    return values


def _latest_date(text: str) -> Optional[date]:
    dates = _dates(text)
    return max(dates) if dates else None


def _extract_price_byn(text: str) -> Optional[float]:
    for pattern in PRICE_PATTERNS:
        match = pattern.search(text)
        if match:
            return parse_float(match.group("value"))
    return None


def _extract_cadastral(text: str) -> Optional[str]:
    match = CADASTRAL_RE.search(text.replace(":", ""))
    return match.group(0) if match else None


def _extract_deadline(text: str) -> Optional[str]:
    match = re.search(
        r"(?:при[её]м\w*\s+(?:заявок|документ\w*)\s+до|окончани\w*\s+при[её]ма)"
        r"[^.\d]{0,50}(?P<value>\d{1,2}\.\d{1,2}\.\d{4}(?:\s+\d{1,2}:\d{2})?)",
        text,
        re.IGNORECASE,
    )
    return match.group("value") if match else None


def _extract_address(text: str) -> Optional[str]:
    match = re.search(
        r"(?:местоположение|местонахождение|расположен)[^:\n]{0,30}:?\s*"
        r"(?P<value>[^.;]{3,180})",
        text,
        re.IGNORECASE,
    )
    return plain_text(match.group("value")) if match else None


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
