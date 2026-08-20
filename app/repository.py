from __future__ import annotations

from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Iterable, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain import NormalizedListing
from app.models import ListingModel, ListingSnapshotModel
from app.normalization import content_hash

MUTABLE_FIELDS = [
    "canonical_url",
    "title",
    "description",
    "district",
    "locality",
    "address",
    "direction",
    "price_original",
    "currency_original",
    "price_usd",
    "area_sotok",
    "distance_mkad_km",
    "latitude",
    "longitude",
    "facade_m",
    "depth_m",
    "purpose",
    "electricity_raw",
    "electricity_kw",
    "gas_raw",
    "water_raw",
    "sewerage_raw",
    "internet_raw",
    "road_raw",
    "nature_raw",
    "ownership_raw",
    "seller_type",
    "score",
    "possible_duplicate",
    "source_created_at",
    "source_updated_at",
]

DETAIL_ENRICHED_FIELDS = [
    "description",
    "distance_mkad_km",
    "latitude",
    "longitude",
    "facade_m",
    "depth_m",
    "purpose",
    "electricity_raw",
    "electricity_kw",
    "gas_raw",
    "water_raw",
    "sewerage_raw",
    "internet_raw",
    "road_raw",
    "nature_raw",
    "ownership_raw",
    "seller_type",
]

NOTIFIABLE_PRICE_CHANGE_RATIO = 0.05


def listing_digest(listing: NormalizedListing) -> str:
    payload = {
        field: getattr(listing, field)
        for field in MUTABLE_FIELDS
        if field not in {"source_created_at", "source_updated_at"}
    }
    payload["status"] = listing.status.value
    payload["reasons"] = listing.reasons
    payload["evidence"] = listing.evidence
    if listing.raw_payload.get("has_house"):
        payload["house"] = {
            "object_kind": listing.object_kind,
            "house_area_sqm": listing.house_area_sqm,
            "house_condition": listing.house_condition,
            "sale_format": listing.sale_format,
            "house_risks": listing.house_risks,
        }
    return content_hash(payload)


def notification_digest(listing: NormalizedListing) -> str:
    """Hash only changes worth interrupting the family about."""
    return content_hash(
        {
            "canonical_url": listing.canonical_url,
            "price_usd": _normalized_number(listing.price_usd),
            "area_sotok": _normalized_number(listing.area_sotok),
            "auction_date": listing.raw_payload.get("auction_date"),
            "auction_deadline": (
                listing.raw_payload.get("auction_deadline")
                or listing.raw_payload.get("application_deadline")
            ),
        }
    )


def hydrate_listings(
    session: Session,
    listings: Iterable[NormalizedListing],
) -> List[NormalizedListing]:
    """Reuse richer saved data and recognize only high-confidence republications."""
    values = list(listings)
    if not values:
        return values

    sources = {listing.source for listing in values}
    saved = list(
        session.scalars(select(ListingModel).where(ListingModel.source.in_(sources)))
    )
    by_external_id = {
        (model.source, model.source_id): model
        for model in saved
    }
    by_alias = {}
    for model in saved:
        for alias in (model.raw_payload or {}).get("source_aliases", []):
            by_alias[(model.source, str(alias))] = model

    incoming_ids = {
        (listing.source, listing.source_id)
        for listing in values
    }
    for listing in values:
        original_source_id = listing.source_id
        model = by_external_id.get((listing.source, original_source_id))
        if model is None:
            alias_model = by_alias.get((listing.source, original_source_id))
            if (
                alias_model is not None
                and (alias_model.source, alias_model.source_id) not in incoming_ids
            ):
                model = alias_model
        if model is None:
            model = _find_republication(saved, listing, incoming_ids)
        if model is None:
            continue

        is_republication = model.source_id != original_source_id
        _merge_saved_detail(model, listing)
        if is_republication:
            aliases = {
                str(value)
                for value in (model.raw_payload or {}).get("source_aliases", [])
            }
            aliases.add(original_source_id)
            listing.raw_payload["source_aliases"] = sorted(aliases)
            listing.raw_payload["current_source_id"] = original_source_id
            listing.raw_payload["relisted_from_external_id"] = (
                f"{model.source}:{model.source_id}"
            )
            listing.source_id = model.source_id
            reason = "Объявление переопубликовано; ссылка обновлена"
            if reason not in listing.reasons:
                listing.reasons.append(reason)
    return values


def upsert_listing(
    session: Session,
    listing: NormalizedListing,
) -> Tuple[ListingModel, bool, bool, bool]:
    now = datetime.now(timezone.utc)
    digest = listing_digest(listing)
    notify_digest = notification_digest(listing)
    model = session.scalar(
        select(ListingModel).where(
            ListingModel.source == listing.source,
            ListingModel.source_id == listing.source_id,
        )
    )
    is_new = model is None
    is_changed = False
    pending_released = bool(
        model is not None
        and (model.raw_payload or {}).get("telegram_pending_enrichment")
        and not listing.raw_payload.get("telegram_pending_enrichment")
    )

    if model is None:
        listing.raw_payload.pop("notification_changes", None)
        listing.raw_payload["notification_baseline"] = _notification_state(listing)
        model = ListingModel(
            source=listing.source,
            source_id=listing.source_id,
            canonical_url=listing.canonical_url,
            title=listing.title,
            description=listing.description,
            status=listing.status.value,
            reasons=listing.reasons,
            evidence=listing.evidence,
            raw_payload=listing.raw_payload,
            content_hash=digest,
            last_notified_hash=notify_digest,
            first_seen_at=now,
            last_seen_at=now,
        )
        session.add(model)
        session.flush()
        is_changed = True
    else:
        content_changed = model.content_hash != digest
        notification_baseline = _notification_baseline(model)
        notification_changes = _notification_changes(
            notification_baseline,
            listing,
        )
        is_changed = bool(notification_changes)
        if is_changed:
            listing.raw_payload["notification_changes"] = notification_changes
            listing.raw_payload["notification_baseline"] = _notification_state(listing)
        else:
            listing.raw_payload.pop("notification_changes", None)
            listing.raw_payload["notification_baseline"] = notification_baseline
        model.last_seen_at = now
        if content_changed:
            session.add(
                ListingSnapshotModel(
                    listing=model,
                    price_usd=listing.price_usd,
                    status=listing.status.value,
                    content_hash=digest,
                    payload=listing.serializable(),
                )
            )

    for field in MUTABLE_FIELDS:
        setattr(model, field, getattr(listing, field))
    model.status = listing.status.value
    model.reasons = listing.reasons
    model.evidence = listing.evidence
    model.raw_payload = listing.raw_payload
    model.content_hash = digest
    model.last_notified_hash = notify_digest
    model.active = True

    if is_new:
        session.add(
            ListingSnapshotModel(
                listing=model,
                price_usd=listing.price_usd,
                status=listing.status.value,
                content_hash=digest,
                payload=listing.serializable(),
            )
        )
    return model, is_new, is_changed, pending_released


def _find_republication(
    saved: Iterable[ListingModel],
    listing: NormalizedListing,
    incoming_ids: set,
) -> Optional[ListingModel]:
    matches = [
        model
        for model in saved
        if model.source == listing.source
        and (model.source, model.source_id) not in incoming_ids
        and _same_physical_listing(model, listing)
    ]
    return matches[0] if len(matches) == 1 else None


def _same_physical_listing(
    model: ListingModel,
    listing: NormalizedListing,
) -> bool:
    left_cadastral = _digits((model.raw_payload or {}).get("cadastral_number"))
    right_cadastral = _digits(listing.raw_payload.get("cadastral_number"))
    if left_cadastral and left_cadastral == right_cadastral:
        return True

    if (
        model.area_sotok is None
        or listing.area_sotok is None
        or abs(model.area_sotok - listing.area_sotok) > 0.2
    ):
        return False
    left_description = _identity_text(model.description)
    right_description = _identity_text(listing.description)
    if len(left_description) < 80 or len(right_description) < 80:
        return False
    if SequenceMatcher(None, left_description, right_description).ratio() < 0.97:
        return False

    left_seller = str((model.raw_payload or {}).get("seller_id") or "")
    right_seller = str(listing.raw_payload.get("seller_id") or "")
    if left_seller and right_seller and left_seller != right_seller:
        return False

    left_place = _identity_text(model.address or model.locality)
    right_place = _identity_text(listing.address or listing.locality)
    return bool(left_place and left_place == right_place)


def _merge_saved_detail(
    model: ListingModel,
    listing: NormalizedListing,
) -> None:
    saved_payload = model.raw_payload or {}
    for key in ("preferred_location", "search_queue"):
        if saved_payload.get(key):
            listing.raw_payload.setdefault(key, saved_payload[key])
    if saved_payload.get("source_aliases"):
        listing.raw_payload.setdefault(
            "source_aliases",
            list(saved_payload["source_aliases"]),
        )
    saved_has_detail = bool(saved_payload.get("detail_loaded"))
    incoming_has_detail = bool(listing.raw_payload.get("detail_loaded"))
    if saved_has_detail and not incoming_has_detail:
        for field in DETAIL_ENRICHED_FIELDS:
            saved_value = getattr(model, field)
            if saved_value not in (None, ""):
                setattr(listing, field, saved_value)
        listing.raw_payload = {
            **saved_payload,
            **listing.raw_payload,
            "detail_loaded": True,
        }
        return

    for field in DETAIL_ENRICHED_FIELDS:
        if getattr(listing, field) in (None, ""):
            saved_value = getattr(model, field)
            if saved_value not in (None, ""):
                setattr(listing, field, saved_value)


def _identity_text(value: object) -> str:
    return "".join(
        character
        for character in str(value or "").lower()
        if character.isalnum()
    )


def _digits(value: object) -> str:
    return "".join(character for character in str(value or "") if character.isdigit())


def _stored_notification_digest(model: ListingModel) -> str:
    payload = model.raw_payload or {}
    return content_hash(
        {
            "canonical_url": model.canonical_url,
            "price_usd": _normalized_number(model.price_usd),
            "area_sotok": _normalized_number(model.area_sotok),
            "auction_date": payload.get("auction_date"),
            "auction_deadline": (
                payload.get("auction_deadline")
                or payload.get("application_deadline")
            ),
        }
    )


def _notification_changes(
    baseline: dict,
    listing: NormalizedListing,
) -> List[str]:
    changes: List[str] = []
    baseline_price = _optional_float(baseline.get("price_usd"))
    if _significant_price_change(baseline_price, listing.price_usd):
        changes.append(
            "цена "
            f"{_money_label(baseline_price)} → {_money_label(listing.price_usd)}"
        )
    baseline_area = _optional_float(baseline.get("area_sotok"))
    if _normalized_number(baseline_area) != _normalized_number(listing.area_sotok):
        changes.append(
            "площадь "
            f"{_area_label(baseline_area)} → {_area_label(listing.area_sotok)}"
        )
    if baseline.get("canonical_url") != listing.canonical_url:
        changes.append("ссылка объявления")

    incoming_deadline = (
        listing.raw_payload.get("auction_deadline")
        or listing.raw_payload.get("application_deadline")
    )
    if baseline.get("auction_date") != listing.raw_payload.get("auction_date"):
        changes.append("дата аукциона")
    if baseline.get("auction_deadline") != incoming_deadline:
        changes.append("срок подачи заявки")
    return changes


def _notification_baseline(model: ListingModel) -> dict:
    saved = (model.raw_payload or {}).get("notification_baseline")
    if isinstance(saved, dict):
        return saved
    payload = model.raw_payload or {}
    return {
        "canonical_url": model.canonical_url,
        "price_usd": model.price_usd,
        "area_sotok": model.area_sotok,
        "auction_date": payload.get("auction_date"),
        "auction_deadline": (
            payload.get("auction_deadline")
            or payload.get("application_deadline")
        ),
    }


def _notification_state(listing: NormalizedListing) -> dict:
    return {
        "canonical_url": listing.canonical_url,
        "price_usd": listing.price_usd,
        "area_sotok": listing.area_sotok,
        "auction_date": listing.raw_payload.get("auction_date"),
        "auction_deadline": (
            listing.raw_payload.get("auction_deadline")
            or listing.raw_payload.get("application_deadline")
        ),
    }


def _significant_price_change(
    baseline: Optional[float],
    current: Optional[float],
) -> bool:
    if baseline is None or current is None:
        return baseline != current
    if baseline == 0:
        return current != 0
    return abs(current - baseline) / abs(baseline) > NOTIFIABLE_PRICE_CHANGE_RATIO


def _optional_float(value: object) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _money_label(value: Optional[float]) -> str:
    return f"${value:,.0f}" if value is not None else "не указана"


def _area_label(value: Optional[float]) -> str:
    return f"{value:g} сот." if value is not None else "не указана"


def _normalized_number(value: Optional[float]) -> Optional[float]:
    return round(float(value), 4) if value is not None else None


def mark_notified(session: Session, listing: NormalizedListing) -> None:
    model = session.scalar(
        select(ListingModel).where(
            ListingModel.source == listing.source,
            ListingModel.source_id == listing.source_id,
        )
    )
    if model:
        model.last_notified_hash = model.content_hash
