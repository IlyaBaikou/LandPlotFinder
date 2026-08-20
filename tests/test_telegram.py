from app.domain import MatchStatus, NormalizedListing
from app.integrations.telegram import (
    TelegramNotifier,
    _listing_block,
    _selection_keyboard,
    _telegram_eligible,
)


def test_listing_block_marks_auction_aggregator() -> None:
    listing = NormalizedListing(
        source="beltorgi_auction",
        source_id="42",
        canonical_url="https://t.me/beltorgi_uchastok/42",
        title="Участок",
        raw_payload={"is_auction": True, "is_aggregator": True},
    )

    block = _listing_block(listing)

    assert "🏷 <b>АУКЦИОН</b>" in block
    assert "📡 <b>АГРЕГАТОР</b>" in block


def test_listing_block_marks_house_with_land() -> None:
    listing = NormalizedListing(
        source="kufar",
        source_id="43",
        canonical_url="https://example.test/43",
        title="Старый дом",
        object_kind="Дом с участком",
        raw_payload={"has_house": True},
    )

    assert "🏚 <b>ДОМ С УЧАСТКОМ</b>" in _listing_block(listing)


def test_listing_block_marks_republication() -> None:
    listing = NormalizedListing(
        source="kufar",
        source_id="old",
        canonical_url="https://example.test/new",
        title="Участок",
        raw_payload={"relisted_from_external_id": "kufar:old"},
    )

    assert "♻️ <b>ПЕРЕОПУБЛИКОВАНО</b>" in _listing_block(listing)


def test_listing_block_marks_preferred_location() -> None:
    listing = NormalizedListing(
        source="realt",
        source_id="preferred",
        canonical_url="https://example.test/preferred",
        title="Участок",
        raw_payload={"preferred_location": "Логойское направление"},
    )

    block = _listing_block(listing, event_kind="new")

    assert "💚 <b>ПРИОРИТЕТ: ЛОГОЙСКОЕ НАПРАВЛЕНИЕ</b>" in block


def test_telegram_digest_ignores_new_and_changed_auctions() -> None:
    auction = NormalizedListing(
        source="e_auction",
        source_id="lot-42",
        canonical_url="https://e-auction.by/lot-42",
        title="Земельный участок",
        status=MatchStatus.REVIEW,
        raw_payload={"is_auction": True},
    )
    notifier = TelegramNotifier("unused-token", "unused-chat")
    try:
        sent = notifier.send_digest([auction], [auction], {})
    finally:
        notifier.close()

    assert sent == 0


def test_telegram_ignores_kufar_card_pending_detail_enrichment() -> None:
    listing = NormalizedListing(
        source="kufar",
        source_id="pending",
        canonical_url="https://example.test/pending",
        title="Участок",
        status=MatchStatus.REVIEW,
        raw_payload={"telegram_pending_enrichment": True},
    )

    assert not _telegram_eligible(listing)


def test_listing_block_contains_known_location_score() -> None:
    listing = NormalizedListing(
        source="realt",
        source_id="known-location",
        canonical_url="https://example.test/known-location",
        title="Участок",
        location_score=72,
        location_verdict="Есть сильные положительные сигналы",
        location_confidence="Средняя-высокая",
    )

    block = _listing_block(listing)

    assert "Локация: 72/100" in block
    assert "уверенность средняя-высокая" in block


def test_listing_block_never_uses_long_description_as_link_text() -> None:
    description = "Продаётся прекрасный участок со всеми коммуникациями. " * 12
    listing = NormalizedListing(
        source="realt",
        source_id="long-title",
        canonical_url="https://example.test/long-title",
        title=description[:500],
        description=description,
        locality="Коптевщина",
        object_kind="Участок",
    )

    block = _listing_block(listing, event_kind="new")

    assert "🆕 <b>НОВОЕ</b>" in block
    assert "<b>Участок — Коптевщина</b>" in block
    assert ">Открыть на Realt</a>" in block
    assert description[:120] not in block


def test_listing_block_marks_updated_duplicate_and_lists_changes() -> None:
    listing = NormalizedListing(
        source="kufar",
        source_id="updated",
        canonical_url="https://example.test/updated",
        title="Участок у леса",
        possible_duplicate=True,
        raw_payload={
            "notification_changes": [
                "цена $25,000 → $23,000",
                "ссылка объявления",
            ]
        },
    )

    block = _listing_block(listing, event_kind="updated")

    assert "🔄 <b>ОБНОВЛЕНО</b>" in block
    assert "👯 <b>ВОЗМОЖНЫЙ ДУБЛЬ</b>" in block
    assert "Изменилось: цена $25,000 → $23,000; ссылка объявления" in block


def test_selection_keyboard_identifies_listing_without_putting_url_in_callback() -> None:
    listing = NormalizedListing(
        source="kufar",
        source_id="1067703909",
        canonical_url="https://re.kufar.by/vi/1067703909",
        title="Большое описание участка " * 10,
        locality="Новосёлки",
    )

    keyboard = _selection_keyboard([listing])

    assert keyboard == [[{
        "text": "⭐ Приглянулось · Новосёлки",
        "callback_data": "study|kufar:1067703909",
    }]]
    assert len(keyboard[0][0]["callback_data"].encode("utf-8")) <= 64
