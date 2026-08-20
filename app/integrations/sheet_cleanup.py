from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from app.integrations.trips import SHEETS_SCOPE

HARD_MAX_PRICE_USD = 40_000
HARD_MIN_AREA_SOTOK = 9
HARD_MAX_AREA_SOTOK = 15
HARD_MAX_DISTANCE_KM = 30
PROTECTED_STATUSES = {
    "изучаем",
    "к поездке",
    "смотрим",
    "понравилось",
    "избранное",
}


@dataclass(frozen=True)
class CleanupCandidate:
    row_number: int
    external_id: str
    status: str
    price_usd: Optional[float]
    area_sotok: Optional[float]
    distance_mkad_km: Optional[float]
    reasons: List[str]


class GooglePlotsCleaner:
    def __init__(
        self,
        spreadsheet_id: str,
        sheet_name: str,
        credentials_info: dict,
    ) -> None:
        credentials = Credentials.from_service_account_info(
            credentials_info,
            scopes=[SHEETS_SCOPE],
        )
        self.spreadsheet_id = spreadsheet_id
        self.sheet_name = sheet_name
        self.service = build(
            "sheets",
            "v4",
            credentials=credentials,
            cache_discovery=False,
        )
        self.sheet_id = self._resolve_sheet_id()

    def preview(self) -> Dict[str, object]:
        rows = self._rows()
        candidates: List[CleanupCandidate] = []
        protected = 0
        for row_number, row in enumerate(rows, start=5):
            candidate = cleanup_candidate(row, row_number)
            if candidate is not None:
                candidates.append(candidate)
            elif row_is_protected(row) and hard_limit_reasons(row):
                protected += 1
        return {
            "scanned": len(rows),
            "delete_count": len(candidates),
            "protected_count": protected,
            "by_reason": _reason_counts(candidates),
            "candidates": candidates,
        }

    def apply(self) -> Dict[str, object]:
        preview = self.preview()
        candidates = list(preview["candidates"])
        backup_sheet = self._backup_sheet()
        requests = [
            {
                "deleteDimension": {
                    "range": {
                        "sheetId": self.sheet_id,
                        "dimension": "ROWS",
                        "startIndex": candidate.row_number - 1,
                        "endIndex": candidate.row_number,
                    }
                }
            }
            for candidate in sorted(
                candidates,
                key=lambda item: item.row_number,
                reverse=True,
            )
        ]
        for start in range(0, len(requests), 400):
            self.service.spreadsheets().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={"requests": requests[start : start + 400]},
            ).execute()
        self._repair_conditional_format_ranges()
        return {
            key: value
            for key, value in preview.items()
            if key != "candidates"
        } | {"backup_sheet": backup_sheet}

    def _rows(self) -> List[List[object]]:
        response = self.service.spreadsheets().values().get(
            spreadsheetId=self.spreadsheet_id,
            range=f"'{self.sheet_name}'!A5:BA",
            valueRenderOption="UNFORMATTED_VALUE",
        ).execute()
        return response.get("values", [])

    def _resolve_sheet_id(self) -> int:
        response = self.service.spreadsheets().get(
            spreadsheetId=self.spreadsheet_id,
            fields="sheets.properties",
        ).execute()
        for sheet in response.get("sheets", []):
            properties = sheet.get("properties", {})
            if properties.get("title") == self.sheet_name:
                return int(properties["sheetId"])
        raise ValueError(f"Google Sheet tab not found: {self.sheet_name}")

    def _backup_sheet(self) -> str:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H%M UTC")
        title = f"{self.sheet_name} backup {timestamp}"
        response = self.service.spreadsheets().batchUpdate(
            spreadsheetId=self.spreadsheet_id,
            body={
                "requests": [
                    {
                        "duplicateSheet": {
                            "sourceSheetId": self.sheet_id,
                            "newSheetName": title,
                        }
                    }
                ]
            },
        ).execute()
        backup_id = int(
            response["replies"][0]["duplicateSheet"]["properties"]["sheetId"]
        )
        self.service.spreadsheets().batchUpdate(
            spreadsheetId=self.spreadsheet_id,
            body={
                "requests": [
                    {
                        "updateSheetProperties": {
                            "properties": {"sheetId": backup_id, "hidden": True},
                            "fields": "hidden",
                        }
                    }
                ]
            },
        ).execute()
        return title

    def _repair_conditional_format_ranges(self) -> None:
        response = self.service.spreadsheets().get(
            spreadsheetId=self.spreadsheet_id,
            fields="sheets(properties,conditionalFormats)",
        ).execute()
        sheet = next(
            (
                item
                for item in response.get("sheets", [])
                if int(item.get("properties", {}).get("sheetId", -1)) == self.sheet_id
            ),
            None,
        )
        if sheet is None:
            return
        row_count = int(sheet.get("properties", {}).get("gridProperties", {}).get(
            "rowCount", 1000
        ))
        requests = []
        for index, rule in enumerate(sheet.get("conditionalFormats", [])):
            ranges = rule.get("ranges", [])
            if not ranges:
                continue
            first_range = ranges[0]
            repaired_rule = dict(rule)
            repaired_rule["ranges"] = [
                {
                    "sheetId": self.sheet_id,
                    "startRowIndex": 4,
                    "endRowIndex": row_count,
                    "startColumnIndex": first_range.get("startColumnIndex", 0),
                    "endColumnIndex": first_range.get("endColumnIndex", 1),
                }
            ]
            requests.append(
                {
                    "updateConditionalFormatRule": {
                        "sheetId": self.sheet_id,
                        "index": index,
                        "rule": repaired_rule,
                    }
                }
            )
        if requests:
            self.service.spreadsheets().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={"requests": requests},
            ).execute()


def cleanup_candidate(
    row: Sequence[object],
    row_number: int,
) -> Optional[CleanupCandidate]:
    reasons = hard_limit_reasons(row)
    if not reasons or row_is_protected(row):
        return None
    return CleanupCandidate(
        row_number=row_number,
        external_id=_cell(row, 1),
        status=_cell(row, 2),
        price_usd=_number(_value(row, 8)),
        area_sotok=_number(_value(row, 9)),
        distance_mkad_km=_number(_value(row, 11)),
        reasons=reasons,
    )


def hard_limit_reasons(row: Sequence[object]) -> List[str]:
    reasons = []
    price = _number(_value(row, 8))
    area = _number(_value(row, 9))
    distance = _number(_value(row, 11))
    if price is None:
        reasons.append("price_unknown")
    elif price > HARD_MAX_PRICE_USD:
        reasons.append("price")
    if area is None:
        reasons.append("area_unknown")
    elif not HARD_MIN_AREA_SOTOK <= area <= HARD_MAX_AREA_SOTOK:
        reasons.append("area")
    if distance is None:
        reasons.append("distance_unknown")
    elif distance > HARD_MAX_DISTANCE_KM:
        reasons.append("distance")
    return reasons


def row_is_protected(row: Sequence[object]) -> bool:
    if _normalize(_cell(row, 2)) in PROTECTED_STATUSES:
        return True
    manual_columns = [
        *range(24, 34),  # X:AG manual criteria
        35,  # AI Ilya
        36,  # AJ Anya
        39,  # AM notes
        44,  # AR house decision
        45,  # AS works/demolition estimate
    ]
    return any(_cell(row, column) for column in manual_columns)


def _reason_counts(candidates: Sequence[CleanupCandidate]) -> Dict[str, int]:
    result = {
        "price": 0,
        "price_unknown": 0,
        "area": 0,
        "area_unknown": 0,
        "distance": 0,
        "distance_unknown": 0,
    }
    for candidate in candidates:
        for reason in candidate.reasons:
            result[reason] += 1
    return result


def _value(row: Sequence[object], column: int) -> object:
    return row[column - 1] if len(row) >= column else ""


def _cell(row: Sequence[object], column: int) -> str:
    return str(_value(row, column)).strip()


def _number(value: object) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return None


def _normalize(value: str) -> str:
    return " ".join(value.lower().replace("ё", "е").split())
