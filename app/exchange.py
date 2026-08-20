from __future__ import annotations

from typing import Optional

from app.http import PublicPageClient
from app.normalization import parse_float

NBRB_USD_RATE_URL = "https://api.nbrb.by/exrates/rates/USD?parammode=2"
FALLBACK_USD_RATE_URL = "https://open.er-api.com/v6/latest/USD"


def byn_to_usd(client: PublicPageClient, amount_byn: Optional[float]) -> Optional[float]:
    if amount_byn is None:
        return None
    rate = official_byn_per_usd(client)
    return round(amount_byn / rate, 2) if rate else None


def official_byn_per_usd(client: PublicPageClient) -> Optional[float]:
    cached = getattr(client, "_landplotfinder_byn_per_usd", None)
    if cached is not None:
        return cached or None
    try:
        payload = client.get_json(NBRB_USD_RATE_URL)
        official_rate = parse_float(payload.get("Cur_OfficialRate"))
        scale = parse_float(payload.get("Cur_Scale")) or 1
        rate = official_rate / scale if official_rate else None
    except Exception:
        rate = None
    if rate is None:
        try:
            payload = client.get_json(FALLBACK_USD_RATE_URL)
            rate = parse_float(payload.get("rates", {}).get("BYN"))
        except Exception:
            rate = None
    setattr(client, "_landplotfinder_byn_per_usd", rate or 0)
    return rate
