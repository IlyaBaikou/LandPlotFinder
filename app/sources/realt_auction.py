from __future__ import annotations

import re
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

from bs4 import BeautifulSoup

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

AUCTION_LINK_RE = re.compile(r"/auctions/(?P<id>\d+)-")
DATE_RE = re.compile(r"(?P<day>\d{2})\.(?P<month>\d{2})\.(?P<year>\d{4})")
AREA_RE = re.compile(
    r"(?:земельн\w*\s+участ\w*|участ\w*)[^.]{0,120}?"
    r"(?:пл(?:ощадью|ощадь)?\.?\s*)"
    r"(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>га|сот(?:ок|ки)?)",
    re.IGNORECASE,
)
PRICE_RE = re.compile(
    r"начальн\w*\s+цен\w*[^:\d]{0,80}:?\s*"
    r"(?P<value>\d[\d\s]*(?:[.,]\d{1,2})?)\s*(?:руб|byn)",
    re.IGNORECASE,
)
DEADLINE_RE = re.compile(
    r"(?:окончани\w*\s+прием\w*|прием\w*\s+документ\w*)"
    r"[^.]{0,180}?(?P<date>\d{1,2}\s+[а-яё]+\s+\d{4})",
    re.IGNORECASE,
)


class RealtAuctionSource(ListingSource):
    name = "realt_auction"

    def scan(self) -> List[NormalizedListing]:
        cards: Dict[str, Tuple[str, str, date]] = {}
        today = date.today()
        for search_url in self.search_urls:
            soup = BeautifulSoup(self.client.get_text(search_url), "html.parser")
            for card in soup.select("div.auction-item"):
                link = card.select_one("div.title a[href]")
                if link is None:
                    continue
                href = str(link.get("href") or "")
                match = AUCTION_LINK_RE.search(href)
                if not match:
                    continue
                card_text = plain_text(card.get_text(" ", strip=True))
                event_date = _extract_date(card_text)
                if event_date is None or event_date < today or "завершен" in card_text.lower():
                    continue
                cards[match.group("id")] = (
                    canonical_url(urljoin(search_url, href)),
                    plain_text(link.get_text(" ", strip=True)),
                    event_date,
                )

        listings: List[NormalizedListing] = []
        for source_id, (url, title, event_date) in cards.items():
            try:
                detail_html = self.client.get_text(url)
                listings.append(
                    self._normalize_detail(source_id, url, title, event_date, detail_html)
                )
            except Exception as exc:
                listings.append(
                    NormalizedListing(
                        source=self.name,
                        source_id=source_id,
                        canonical_url=url,
                        title=title,
                        purpose="Аукцион участка для индивидуального строительства",
                        raw_payload={
                            "auction_date": event_date.isoformat(),
                            "is_auction": True,
                        },
                        reasons=[f"Не удалось загрузить детали аукциона Realt: {exc}"],
                    )
                )
        return listings

    def _normalize_detail(
        self,
        source_id: str,
        url: str,
        title: str,
        event_date: date,
        html: str,
    ) -> NormalizedListing:
        soup = BeautifulSoup(html, "html.parser")
        root = soup.select_one("div.tx-uedbauction-pi1") or soup
        meta = root.select_one("div.agentstvo-block")
        body = root.select_one("div.text-news")
        meta_text = plain_text(meta.get_text(" ", strip=True) if meta else "")
        description = plain_text(body.get_text(" ", strip=True) if body else "")

        address = _extract_labeled(meta_text, "Местоположение:")
        area = _extract_area_sotok(description)
        price_byn = _extract_price_byn(description)
        price_usd = byn_to_usd(self.client, price_byn)
        distance, distance_evidence = extract_distance_km(description)
        electricity_kw, electricity_evidence = extract_electricity_kw(description)
        deadline_match = DEADLINE_RE.search(description)
        deadline = deadline_match.group("date") if deadline_match else None

        evidence: Dict[str, str] = {}
        if distance_evidence:
            evidence["distance"] = distance_evidence
        if electricity_evidence:
            evidence["electricity"] = electricity_evidence
        if deadline:
            evidence["auction_deadline"] = deadline

        reasons = [
            "Аукцион: начальная цена может вырасти; проверить задаток и условия участия"
        ]
        if deadline:
            reasons.append(f"Срок подачи документов: {deadline}")

        event_datetime = datetime.combine(event_date, datetime.min.time())
        return NormalizedListing(
            source=self.name,
            source_id=source_id,
            canonical_url=url,
            title=title,
            description=description,
            district=_district_from_address(address),
            locality=_locality_from_address(address),
            address=address,
            price_original=price_byn,
            currency_original="BYN" if price_byn is not None else None,
            price_usd=price_usd,
            area_sotok=area,
            distance_mkad_km=distance,
            purpose="Аукцион участка для индивидуального строительства",
            electricity_raw=_find_fragment(description, "электр"),
            electricity_kw=electricity_kw,
            gas_raw=_find_fragment(description, "газ"),
            ownership_raw=normalized_ownership(description),
            seller_type="Организатор аукциона",
            source_created_at=event_datetime,
            source_updated_at=event_datetime,
            evidence=evidence,
            raw_payload={
                "auction_date": event_date.isoformat(),
                "auction_deadline": deadline,
                "is_auction": True,
                "byn_per_usd": (
                    round(price_byn / price_usd, 6)
                    if price_byn is not None and price_usd
                    else None
                ),
            },
            reasons=reasons,
        )


def _extract_date(text: str) -> Optional[date]:
    match = DATE_RE.search(text)
    if not match:
        return None
    try:
        return date(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
        )
    except ValueError:
        return None


def _extract_area_sotok(text: str) -> Optional[float]:
    values: List[float] = []
    for match in AREA_RE.finditer(text):
        value = parse_float(match.group("value"))
        if value is None:
            continue
        if match.group("unit").lower() == "га":
            value *= 100
        if 0.1 <= value <= 1_000:
            values.append(round(value, 4))
    preferred = [value for value in values if 7 <= value <= 15]
    return preferred[0] if preferred else values[0] if values else None


def _extract_price_byn(text: str) -> Optional[float]:
    match = PRICE_RE.search(text)
    return parse_float(match.group("value")) if match else None


def _extract_labeled(text: str, label: str) -> Optional[str]:
    position = text.lower().find(label.lower())
    if position < 0:
        return None
    value = text[position + len(label) :]
    for boundary in (" Организатор:", " Просмотров", " Категория:"):
        value = value.split(boundary, 1)[0]
    return value.strip(" ,") or None


def _district_from_address(address: Optional[str]) -> Optional[str]:
    if not address:
        return None
    for part in (part.strip() for part in address.split(",")):
        if "район" in part.lower():
            return part
    return None


def _locality_from_address(address: Optional[str]) -> Optional[str]:
    if not address:
        return None
    parts = [part.strip() for part in address.split(",") if part.strip()]
    return parts[-1] if parts else None


def _find_fragment(text: str, needle: str) -> Optional[str]:
    position = text.lower().find(needle)
    if position < 0:
        return None
    return text[max(0, position - 60) : min(len(text), position + 140)]
