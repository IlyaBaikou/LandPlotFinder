from __future__ import annotations

import math
from typing import Iterable, List

from app.domain import NormalizedListing


def flag_possible_duplicates(listings: Iterable[NormalizedListing]) -> int:
    values: List[NormalizedListing] = list(listings)
    flagged = 0
    for left_index, left in enumerate(values):
        for right in values[left_index + 1 :]:
            if left.source == right.source:
                continue
            if _looks_like_duplicate(left, right):
                if not left.possible_duplicate:
                    flagged += 1
                if not right.possible_duplicate:
                    flagged += 1
                left.possible_duplicate = True
                right.possible_duplicate = True
                reason = f"Возможный дубль на {right.source}: {right.canonical_url}"
                reverse = f"Возможный дубль на {left.source}: {left.canonical_url}"
                if reason not in left.reasons:
                    left.reasons.append(reason)
                if reverse not in right.reasons:
                    right.reasons.append(reverse)
    return flagged


def _looks_like_duplicate(left: NormalizedListing, right: NormalizedListing) -> bool:
    left_cadastral = _normalize_cadastral(left.raw_payload.get("cadastral_number"))
    right_cadastral = _normalize_cadastral(right.raw_payload.get("cadastral_number"))
    if left_cadastral and left_cadastral == right_cadastral:
        return True

    area_close = (
        left.area_sotok is not None
        and right.area_sotok is not None
        and abs(left.area_sotok - right.area_sotok) <= 0.35
    )
    price_close = (
        left.price_usd is not None
        and right.price_usd is not None
        and abs(left.price_usd - right.price_usd) / max(left.price_usd, right.price_usd) <= 0.08
    )
    if not (area_close and price_close):
        return False

    if all(
        value is not None
        for value in (left.latitude, left.longitude, right.latitude, right.longitude)
    ):
        return (
            _haversine_m(
                left.latitude or 0,
                left.longitude or 0,
                right.latitude or 0,
                right.longitude or 0,
            )
            <= 500
        )

    left_address = _normalize_address(left.address or left.locality or "")
    right_address = _normalize_address(right.address or right.locality or "")
    return bool(
        left_address
        and right_address
        and (left_address in right_address or right_address in left_address)
    )


def _normalize_address(value: str) -> str:
    return "".join(character for character in value.lower() if character.isalnum())


def _normalize_cadastral(value: object) -> str:
    return "".join(character for character in str(value or "") if character.isdigit())


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6_371_008.8
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
