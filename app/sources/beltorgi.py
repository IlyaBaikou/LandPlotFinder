from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Dict, List, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

from app.domain import NormalizedListing
from app.exchange import byn_to_usd
from app.http import PublicPageClient
from app.normalization import (
    extract_electricity_kw,
    normalized_ownership,
    parse_float,
    plain_text,
)
from app.sources.base import ListingSource

POST_ID_RE = re.compile(r"(?P<channel>[A-Za-z0-9_]+)/(?P<id>\d+)$")
CADASTRAL_RE = re.compile(r"(?:кад\.?\s*№|кадастров\w*\s+номер)[:\s]*([\d:]{10,30})", re.I)
AREA_RE = re.compile(
    r"площад\w*\s*[—–-]?\s*(?P<value>\d+(?:[.,]\d+)?)\s*"
    r"(?P<unit>га|сот(?:ок|ки)?)",
    re.I,
)
DISTANCE_RE = re.compile(r"расстояни\w*:\s*(?P<value>\d+(?:[.,]\d+)?)\s*км", re.I)
PRICE_RE = re.compile(
    r"начальн\w*\s+цен\w*:\s*(?P<value>\d[\d\s]*(?:[.,]\d+)?)\s*BYN",
    re.I,
)
AUCTION_DATE_RE = re.compile(
    r"аукцион\s+состоится\s+(?P<day>\d{1,2})\s+(?P<month>[а-яё]+)"
    r"(?:\s+(?P<year>\d{4}))?",
    re.I,
)
DEADLINE_RE = re.compile(
    r"при[её]м\s+документ\w*\s+завершается\s+(?P<day>\d{1,2})\s+"
    r"(?P<month>[а-яё]+)(?:\s+(?P<year>\d{4}))?"
    r"(?:\s+в\s+(?P<time>\d{1,2}:\d{2}))?",
    re.I,
)
MONTHS = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}
OUTSIDE_MINSK_REGION = (
    "брестская область",
    "витебская область",
    "гомельская область",
    "гродненская область",
    "могилевская область",
)


class BeltorgiAuctionSource(ListingSource):
    name = "beltorgi_auction"

    def __init__(self, *args: object, max_pages: int = 2, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.max_pages = max(1, max_pages)

    def scan(self) -> List[NormalizedListing]:
        posts: Dict[str, Tag] = {}
        for search_url in self.search_urls:
            page_url: Optional[str] = search_url
            for _ in range(self.max_pages):
                if not page_url:
                    break
                soup = BeautifulSoup(self.client.get_text(page_url), "html.parser")
                page_posts = soup.select(".tgme_widget_message[data-post]")
                if not page_posts:
                    break
                oldest_id: Optional[int] = None
                for post in page_posts:
                    post_key = str(post.get("data-post") or "")
                    match = POST_ID_RE.search(post_key)
                    if not match:
                        continue
                    posts[post_key] = post
                    post_id = int(match.group("id"))
                    oldest_id = post_id if oldest_id is None else min(oldest_id, post_id)
                page_url = _before_url(search_url, oldest_id)

        listings: List[NormalizedListing] = []
        today = date.today()
        for post_key, post in posts.items():
            post_listings = _normalize_post(self.client, post_key, post)
            for listing in post_listings:
                auction_date = _date_from_iso(
                    listing.raw_payload.get("auction_date")
                )
                if auction_date is not None and auction_date < today:
                    continue
                listings.append(listing)
        return listings


def _normalize_post(
    client: PublicPageClient,
    post_key: str,
    post: Tag,
) -> List[NormalizedListing]:
    message = post.select_one(".tgme_widget_message_text")
    if message is None:
        return []
    lines = _message_lines(message)
    full_text = " ".join(lines)
    posted_at = _post_datetime(post)
    auction_date = _extract_named_date(
        AUCTION_DATE_RE,
        full_text,
        posted_at.date() if posted_at else date.today(),
    )
    deadline = _extract_deadline(
        full_text,
        posted_at.date() if posted_at else date.today(),
    )
    auction_kind = _auction_kind(full_text)
    permalink = _post_permalink(post, post_key)
    lots = _split_lots(lines)
    results: List[NormalizedListing] = []

    for index, lot in enumerate(lots, start=1):
        text = " ".join(lot["lines"])
        address = _field_from_lines(lot["lines"], "Адрес")
        if not address:
            continue
        cadastral = _extract_cadastral(text)
        area = _extract_area_sotok(text)
        distance = _extract_distance_km(text)
        price_byn = _extract_price_byn(text)
        price_usd = byn_to_usd(client, price_byn)
        infrastructure = _field_from_lines(lot["lines"], "Инфраструктура")
        purpose = _field_from_lines(lot["lines"], "Назначение")
        electricity_kw, electricity_evidence = extract_electricity_kw(
            infrastructure,
            text,
        )
        source_id = cadastral or f"{post_key.rsplit('/', 1)[-1]}:{index}"
        district = lot.get("district")
        context_text = f"{district or ''} {lot.get('council') or ''} {address}"
        evidence: Dict[str, str] = {}
        if cadastral:
            evidence["cadastral_number"] = cadastral
        if electricity_evidence:
            evidence["electricity"] = electricity_evidence
        if deadline:
            evidence["auction_deadline"] = deadline.isoformat()

        results.append(
            NormalizedListing(
                source="beltorgi_auction",
                source_id=source_id,
                canonical_url=permalink,
                title=f"Аукционный участок: {address}",
                description=text,
                district=district,
                locality=_locality_from_address(address),
                address=address,
                price_original=price_byn,
                currency_original="BYN" if price_byn is not None else None,
                price_usd=price_usd,
                area_sotok=area,
                distance_mkad_km=distance,
                purpose=purpose or "Продажа земельного участка на аукционе",
                electricity_raw=_infrastructure_fragment(infrastructure, "электр"),
                electricity_kw=electricity_kw,
                gas_raw=_infrastructure_fragment(infrastructure, "газ"),
                water_raw=_infrastructure_fragment(infrastructure, "вод"),
                sewerage_raw=_infrastructure_fragment(
                    infrastructure,
                    "водоотвед",
                    "канализ",
                ),
                ownership_raw=normalized_ownership(text),
                seller_type="Агрегатор аукционов",
                source_created_at=posted_at,
                source_updated_at=posted_at,
                evidence=evidence,
                raw_payload={
                    "is_auction": True,
                    "is_aggregator": True,
                    "telegram_post": post_key,
                    "lot_marker": lot.get("marker"),
                    "cadastral_number": cadastral,
                    "auction_date": (
                        auction_date.isoformat() if auction_date else None
                    ),
                    "auction_deadline": deadline.isoformat() if deadline else None,
                    "auction_kind": auction_kind,
                    "outside_target_region": any(
                        marker in context_text.lower()
                        for marker in OUTSIDE_MINSK_REGION
                    ),
                    "byn_per_usd": (
                        round(price_byn / price_usd, 6)
                        if price_byn is not None and price_usd
                        else None
                    ),
                },
                reasons=[
                    "Агрегатор «Белторги»: проверить лот, сроки и условия "
                    "у официального организатора"
                ],
            )
        )
    return results


def _message_lines(message: Tag) -> List[str]:
    values = []
    for raw_line in message.get_text("\n").splitlines():
        line = plain_text(raw_line)
        if line and set(line) != {"*"}:
            values.append(line)
    return values


def _split_lots(lines: List[str]) -> List[dict]:
    lots: List[dict] = []
    current: Optional[dict] = None
    district: Optional[str] = None
    council: Optional[str] = None
    marker: Optional[str] = None

    for line in lines:
        lowered = line.lower()
        if _is_auction_footer(lowered):
            if current:
                lots.append(current)
                current = None
            continue
        if "район" in lowered and len(line) <= 80 and not lowered.startswith("адрес:"):
            if current:
                lots.append(current)
                current = None
            district = line
            continue
        if (
            lowered.endswith("с/с")
            or "сельсовет" in lowered
            or "сельский совет" in lowered
        ):
            if current:
                lots.append(current)
                current = None
            council = line
            continue
        if re.match(r"^\[?лоты?\b", lowered):
            if current:
                lots.append(current)
                current = None
            marker = line
            continue
        if lowered.startswith("адрес:"):
            if current:
                lots.append(current)
            current = {
                "district": district,
                "council": council,
                "marker": marker,
                "lines": [line],
            }
            marker = None
            continue
        if current:
            current["lines"].append(line)
    if current:
        lots.append(current)
    return lots


def _is_auction_footer(lowered: str) -> bool:
    return any(
        lowered.startswith(prefix)
        for prefix in (
            "аукцион состоится",
            "приём документов",
            "прием документов",
            "очные торги",
            "электронные торги",
            "если вам нужна консультация",
            "или звоните",
        )
    )


def _field(text: str, label: str) -> Optional[str]:
    match = re.search(
        rf"{re.escape(label)}:\s*(?P<value>.+?)(?=\s+[А-ЯЁ⚡][^:]{1,35}:|$)",
        text,
    )
    return plain_text(match.group("value")).strip(" .") if match else None


def _field_from_lines(lines: List[str], label: str) -> Optional[str]:
    prefix = f"{label.lower()}:"
    for line in lines:
        lowered = line.lower()
        position = lowered.find(prefix)
        if position < 0:
            continue
        return plain_text(line[position + len(prefix) :]).strip(" .") or None
    return _field(" ".join(lines), label)


def _extract_cadastral(text: str) -> Optional[str]:
    match = CADASTRAL_RE.search(text)
    if not match:
        return None
    value = "".join(character for character in match.group(1) if character.isdigit())
    return value or None


def _extract_area_sotok(text: str) -> Optional[float]:
    match = AREA_RE.search(text)
    if not match:
        return None
    value = parse_float(match.group("value"))
    if value is None:
        return None
    if match.group("unit").lower() == "га":
        value *= 100
    return round(value, 4)


def _extract_distance_km(text: str) -> Optional[float]:
    match = DISTANCE_RE.search(text)
    return parse_float(match.group("value")) if match else None


def _extract_price_byn(text: str) -> Optional[float]:
    match = PRICE_RE.search(text)
    return parse_float(match.group("value")) if match else None


def _extract_named_date(
    pattern: re.Pattern[str],
    text: str,
    reference: date,
) -> Optional[date]:
    match = pattern.search(text)
    if not match:
        return None
    month = MONTHS.get(match.group("month").lower())
    if month is None:
        return None
    year_value = match.groupdict().get("year")
    year = int(year_value) if year_value else reference.year
    try:
        value = date(year, month, int(match.group("day")))
    except ValueError:
        return None
    if not year_value and value < reference.replace(day=1):
        try:
            value = value.replace(year=year + 1)
        except ValueError:
            return None
    return value


def _extract_deadline(text: str, reference: date) -> Optional[datetime]:
    match = DEADLINE_RE.search(text)
    if not match:
        return None
    deadline_date = _extract_named_date(DEADLINE_RE, text, reference)
    if deadline_date is None:
        return None
    time_value = match.group("time") or "23:59"
    hour, minute = (int(value) for value in time_value.split(":"))
    return datetime(
        deadline_date.year,
        deadline_date.month,
        deadline_date.day,
        hour,
        minute,
        tzinfo=timezone.utc,
    )


def _auction_kind(text: str) -> Optional[str]:
    lowered = text.lower()
    if "электронные торги" in lowered:
        return "Электронные торги"
    if "очные торги" in lowered:
        return "Очные торги"
    return None


def _post_datetime(post: Tag) -> Optional[datetime]:
    time_element = post.select_one("time[datetime]")
    if time_element is None:
        return None
    try:
        return datetime.fromisoformat(str(time_element.get("datetime")))
    except (TypeError, ValueError):
        return None


def _post_permalink(post: Tag, post_key: str) -> str:
    link = post.select_one("a.tgme_widget_message_date[href]")
    if link is not None and link.get("href"):
        return str(link.get("href"))
    return f"https://t.me/{post_key}"


def _locality_from_address(address: str) -> Optional[str]:
    parts = [part.strip() for part in address.split(",") if part.strip()]
    return parts[0] if parts else None


def _infrastructure_fragment(
    infrastructure: Optional[str],
    *needles: str,
) -> Optional[str]:
    if not infrastructure:
        return None
    lowered = infrastructure.lower()
    return infrastructure if any(needle in lowered for needle in needles) else None


def _before_url(base_url: str, before: Optional[int]) -> Optional[str]:
    if before is None:
        return None
    parsed = urlsplit(base_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["before"] = str(before)
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
    )


def _date_from_iso(value: object) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None
