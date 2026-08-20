from __future__ import annotations

import html
import logging
import re
import time
from typing import Iterable, List, Optional

import httpx

from app.domain import MatchStatus, NormalizedListing

HTTPX_LOGGER = logging.getLogger("httpx")


class TelegramNotifier:
    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        timeout_seconds: float = 20,
        message_delay_seconds: float = 3.2,
        selection_buttons: bool = True,
    ) -> None:
        self.chat_id = chat_id
        self.endpoint = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self.client = httpx.Client(timeout=timeout_seconds)
        self.message_delay_seconds = message_delay_seconds
        self.selection_buttons = selection_buttons

    def close(self) -> None:
        self.client.close()

    def send_digest(
        self,
        new_listings: Iterable[NormalizedListing],
        changed_listings: Iterable[NormalizedListing],
        errors: dict,
    ) -> int:
        new_values = [
            item for item in new_listings if _telegram_eligible(item)
        ]
        changed_values = [
            item for item in changed_listings if _telegram_eligible(item)
        ]
        if not new_values and not changed_values and not errors:
            return 0

        cards = [
            (listing, "new")
            for status in (
                MatchStatus.MATCH,
                MatchStatus.REVIEW,
                MatchStatus.INTERESTING,
            )
            for listing in new_values
            if listing.status is status
        ]
        cards.extend((listing, "updated") for listing in changed_values[:10])
        sent = self._send_cards(cards)
        if errors:
            error_lines = ["<b>⚠️ Ошибки источников</b>"]
            error_lines.extend(
                f"• {html.escape(source)}: {html.escape(str(message))}"
                for source, message in errors.items()
            )
            self._send({
                "chat_id": self.chat_id,
                "text": "\n".join(error_lines),
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            })
            sent += 1
        return sent

    def send_catalog(self, listings: Iterable[NormalizedListing]) -> int:
        cards = [
            (listing, "catalog")
            for listing in listings
            if _telegram_eligible(listing)
        ]
        return self._send_cards(cards)

    def _send_cards(self, cards: List[tuple]) -> int:
        sent = 0
        for index, (listing, event_kind) in enumerate(cards):
            message = f"<b>🏡 LandPlotFinder</b>\n{_listing_block(listing, event_kind)}"
            payload = {
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            keyboard = (
                _selection_keyboard([listing], include_place=False)
                if self.selection_buttons
                else []
            )
            if keyboard:
                payload["reply_markup"] = {"inline_keyboard": keyboard}
            self._send(payload)
            sent += 1
            if index < len(cards) - 1 and self.message_delay_seconds > 0:
                time.sleep(self.message_delay_seconds)
        return sent

    def _send(self, payload: dict) -> None:
        previous_level = HTTPX_LOGGER.level
        try:
            HTTPX_LOGGER.setLevel(logging.WARNING)
            response = self.client.post(self.endpoint, json=payload)
        finally:
            HTTPX_LOGGER.setLevel(previous_level)
        if response.status_code == 429:
            retry_after = float(response.json().get("parameters", {}).get("retry_after", 3))
            time.sleep(retry_after + 0.5)
            response = self.client.post(self.endpoint, json=payload)
        if not response.is_success:
            raise RuntimeError(f"Telegram API returned HTTP {response.status_code}")


def _telegram_eligible(listing: NormalizedListing) -> bool:
    return (
        listing.status is not MatchStatus.REJECT
        and not listing.raw_payload.get("is_auction")
        and not listing.raw_payload.get("telegram_pending_enrichment")
    )


def _selection_keyboard(
    listings: Iterable[NormalizedListing],
    include_place: bool = True,
) -> List[List[dict]]:
    keyboard = []
    for listing in listings:
        callback_data = f"study|{listing.external_id}"
        if len(callback_data.encode("utf-8")) > 64:
            continue
        place = listing.locality or listing.address or listing.district or listing.title
        label = _shorten(place, 28)
        keyboard.append(
            [{
                "text": (
                    f"⭐ Приглянулось · {label}"
                    if include_place
                    else "⭐ Приглянулось"
                ),
                "callback_data": callback_data,
            }]
        )
    return keyboard


def _listing_block(
    listing: NormalizedListing,
    event_kind: Optional[str] = None,
) -> str:
    price = f"${listing.price_usd:,.0f}" if listing.price_usd is not None else "цена ?"
    area = f"{listing.area_sotok:g} сот." if listing.area_sotok is not None else "площадь ?"
    distance = (
        f"{listing.distance_mkad_km:g} км"
        if listing.distance_mkad_km is not None
        else "расстояние ?"
    )
    place = listing.locality or listing.address or listing.district or "место не указано"
    reason = next(
        (
            value
            for value in listing.reasons
            if not value.lower().startswith("возможный дубль")
        ),
        "",
    )
    reason_line = f"\n⚠️ {html.escape(reason)}" if reason else ""
    internet_line = (
        f"\n🌐 {html.escape(listing.internet_raw)}" if listing.internet_raw else ""
    )
    location_line = ""
    if listing.location_score is not None:
        location_line = (
            f"\n📍 Локация: {listing.location_score}/100 · "
            f"{html.escape(listing.location_verdict or '')} · "
            f"уверенность {html.escape((listing.location_confidence or '').lower())}"
        )
    elif listing.location_verdict:
        location_line = f"\n📍 {html.escape(listing.location_verdict)}"
    tags = []
    if event_kind == "new":
        tags.append("🆕 <b>НОВОЕ</b>")
    elif event_kind == "updated":
        tags.append("🔄 <b>ОБНОВЛЕНО</b>")
    elif event_kind == "catalog":
        tags.append("📚 <b>СТАРТОВАЯ ПОДБОРКА</b>")
    status_tag = {
        MatchStatus.MATCH: "✅ <b>ПОДХОДИТ</b>",
        MatchStatus.REVIEW: "🔎 <b>ПРОВЕРИТЬ</b>",
        MatchStatus.INTERESTING: "✨ <b>ИНТЕРЕСНО</b>",
    }.get(listing.status)
    if status_tag:
        tags.append(status_tag)
    if listing.raw_payload.get("is_auction"):
        tags.append("🏷 <b>АУКЦИОН</b>")
    if listing.raw_payload.get("is_aggregator"):
        tags.append("📡 <b>АГРЕГАТОР</b>")
    preferred_location = listing.raw_payload.get("preferred_location")
    if preferred_location:
        tags.append(
            "💚 <b>ПРИОРИТЕТ: "
            f"{html.escape(str(preferred_location).upper())}</b>"
        )
    if listing.raw_payload.get("relisted_from_external_id"):
        tags.append("♻️ <b>ПЕРЕОПУБЛИКОВАНО</b>")
    if listing.possible_duplicate:
        tags.append("👯 <b>ВОЗМОЖНЫЙ ДУБЛЬ</b>")
    if listing.object_kind and listing.object_kind != "Участок":
        tags.append("🏚 <b>ДОМ С УЧАСТКОМ</b>")
    tag_line = f"\n{' · '.join(tags)}" if tags else ""
    changes = listing.raw_payload.get("notification_changes") or []
    changes_line = (
        f"\n✏️ Изменилось: {html.escape('; '.join(str(value) for value in changes))}"
        if event_kind == "updated" and changes
        else ""
    )
    title = _display_title(listing, place)
    source = _source_name(listing.source)
    return (
        f"{tag_line}\n<b>{html.escape(title)}</b>\n"
        f'🔗 <a href="{html.escape(listing.canonical_url)}">'
        f"Открыть на {html.escape(source)}</a>\n"
        f"💰 {price} · 📐 {area} · 🛣 {distance}\n"
        f"📌 {html.escape(_shorten(place, 120))}\n"
        f"⭐ Объявление: {listing.score}/100"
        f"{changes_line}{location_line}{internet_line}{reason_line}"
    )


def _display_title(listing: NormalizedListing, place: str) -> str:
    title = " ".join(str(listing.title or "").split())
    description = " ".join(str(listing.description or "").split())
    looks_like_description = (
        len(title) > 110
        or (description and len(title) > 70 and description.startswith(title[:70]))
        or title.count(".") >= 3
        or "http://" in title.lower()
        or "https://" in title.lower()
    )
    if title and not looks_like_description:
        return _shorten(title, 100)
    object_kind = listing.object_kind or (
        "Дом с участком" if listing.raw_payload.get("has_house") else "Участок"
    )
    short_place = _shorten(place, 70)
    return f"{object_kind} — {short_place}" if short_place else object_kind


def _shorten(value: object, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip(" ,.;:-") + "…"


def _source_name(source: str) -> str:
    return {
        "realt": "Realt",
        "kufar": "Kufar",
        "realt_auction": "Realt",
        "rlt_auction": "RLT",
        "e_auction": "e-auction.by",
        "beltorgi_auction": "Белторги",
    }.get(source, source.replace("_", " ").title())


def _chunk(text: str, limit: int) -> List[str]:
    if len(text) <= limit:
        return [text]
    chunks: List[str] = []
    current = ""
    for block in text.split("\n\n"):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) > limit and current:
            chunks.append(current)
            current = block
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks
