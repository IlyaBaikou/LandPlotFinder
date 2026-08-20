from __future__ import annotations

from typing import List, Optional, Sequence

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from app.domain import MatchStatus, NormalizedListing
from app.integrations.trips import SHEETS_SCOPE, TRIP_STATUSES, parse_coordinates

EXCLUDED_STATUSES = TRIP_STATUSES | {
    "отказ",
    "отклонено",
    "не подходит",
    "архив",
}


class GoogleCatalogReader:
    def __init__(self, spreadsheet_id: str, credentials_info: dict) -> None:
        credentials = Credentials.from_service_account_info(
            credentials_info,
            scopes=[SHEETS_SCOPE],
        )
        self.spreadsheet_id = spreadsheet_id
        self.service = build(
            "sheets",
            "v4",
            credentials=credentials,
            cache_discovery=False,
        )

    def top_candidates(
        self,
        min_score: float = 80,
        limit: int = 48,
    ) -> List[NormalizedListing]:
        response = (
            self.service.spreadsheets()
            .values()
            .get(
                spreadsheetId=self.spreadsheet_id,
                range="'Plots'!A5:BA",
            )
            .execute()
        )
        candidates = []
        for row in response.get("values", []):
            listing = _listing_from_row(row)
            if listing is None or listing.score < min_score:
                continue
            candidates.append(listing)
        return sorted(candidates, key=lambda item: item.score, reverse=True)[:limit]


def _listing_from_row(row: Sequence[object]) -> Optional[NormalizedListing]:
    external_id = _cell(row, 1)
    source, separator, source_id = external_id.partition(":")
    if separator != ":" or source not in {"realt", "kufar"} or not source_id:
        return None
    if _normalize_status(_cell(row, 2)) in EXCLUDED_STATUSES:
        return None
    canonical_url = _cell(row, 5)
    if not canonical_url:
        return None
    score = _number(_cell(row, 34))
    if score is None:
        return None
    coordinates = parse_coordinates(_cell(row, 22))
    object_kind = _cell(row, 40) or "Участок"
    place = _cell(row, 7) or _cell(row, 6) or "место не указано"
    price_usd = _number(_cell(row, 8))
    area_sotok = _number(_cell(row, 9))
    distance_mkad_km = _number(_cell(row, 11))
    if (
        price_usd is None
        or price_usd > 40_000
        or area_sotok is None
        or not 9 <= area_sotok <= 15
        or distance_mkad_km is None
        or distance_mkad_km > 30
    ):
        return None
    return NormalizedListing(
        source=source,
        source_id=source_id,
        canonical_url=canonical_url,
        title=f"{object_kind} — {place}",
        district=_cell(row, 6) or None,
        locality=_cell(row, 7) or None,
        price_usd=price_usd,
        area_sotok=area_sotok,
        distance_mkad_km=distance_mkad_km,
        latitude=coordinates[0] if coordinates else None,
        longitude=coordinates[1] if coordinates else None,
        facade_m=_number(_cell(row, 12)),
        depth_m=_number(_cell(row, 13)),
        purpose=_cell(row, 14) or None,
        electricity_raw=_cell(row, 15) or None,
        gas_raw=_cell(row, 16) or None,
        water_raw=_cell(row, 17) or None,
        sewerage_raw=_cell(row, 18) or None,
        internet_raw=_cell(row, 19) or None,
        road_raw=_cell(row, 20) or None,
        nature_raw=_cell(row, 21) or None,
        seller_type=_cell(row, 23) or None,
        object_kind=object_kind,
        house_area_sqm=_number(_cell(row, 41)),
        house_condition=_cell(row, 42) or None,
        sale_format=_cell(row, 43) or None,
        house_risks=_cell(row, 47) or None,
        location_key=_cell(row, 48) or None,
        location_score=_integer(_cell(row, 49)),
        location_verdict=_cell(row, 50) or None,
        location_confidence=_cell(row, 51) or None,
        location_signals=_split(_cell(row, 52)),
        location_risks=_split(_cell(row, 53)),
        status=MatchStatus.INTERESTING,
        score=round(score),
        reasons=_split(_cell(row, 38)),
        raw_payload={"has_house": object_kind != "Участок"},
    )


def _cell(row: Sequence[object], column: int) -> str:
    return str(row[column - 1]).strip() if len(row) >= column else ""


def _number(value: object) -> Optional[float]:
    try:
        return float(str(value).replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return None


def _integer(value: object) -> Optional[int]:
    number = _number(value)
    return round(number) if number is not None else None


def _normalize_status(value: str) -> str:
    return " ".join(value.lower().replace("ё", "е").split())


def _split(value: str) -> List[str]:
    return [part.strip() for part in value.split(";") if part.strip()]
