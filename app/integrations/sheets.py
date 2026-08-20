from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Dict, Iterable, List, Optional

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from app.domain import LocationProfile, MatchStatus, NormalizedListing

LOGGER = logging.getLogger(__name__)
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
PLOT_LOCATION_HEADERS = [
    "ID локации",
    "Оценка локации",
    "Вердикт локации",
    "Уверенность",
    "Сигналы локации",
    "Риски локации",
]
LOCATION_HEADERS = [
    "ID локации",
    "Локация",
    "Тип",
    "Район",
    "Объявлений",
    "Подходящих",
    "Домов / дач",
    "Домов от $80 000",
    "Медиана цены домов, $",
    "Медиана до МКАД, км",
    "Электричество",
    "Газ",
    "Интернет",
    "Асфальт",
    "Location Score",
    "Вердикт",
    "Уверенность",
    "Положительные сигналы",
    "Риски и пробелы",
    "Последний расчёт",
    "Ручные заметки",
]


class GoogleSheetsSink:
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
        self.service = build("sheets", "v4", credentials=credentials, cache_discovery=False)
        self.sheet_id = self._resolve_sheet_id()

    def sync(self, listings: Iterable[NormalizedListing]) -> Dict[str, int]:
        self._ensure_plot_location_columns()
        eligible = [
            listing
            for listing in listings
            if listing.status
            in {MatchStatus.MATCH, MatchStatus.REVIEW, MatchStatus.INTERESTING}
        ]
        if not eligible:
            return {"inserted": 0, "updated": 0}

        existing = self._existing_rows()
        inserted = 0
        updated = 0
        row_assignments: List[tuple[int, NormalizedListing]] = []
        existing_assignments: List[tuple[int, NormalizedListing]] = []
        new_listings: List[NormalizedListing] = []

        for listing in eligible:
            row_number = existing.get(listing.external_id)
            if row_number is None:
                new_listings.append(listing)
            else:
                existing_assignments.append((row_number, listing))

        if existing_assignments:
            self._batch_update_rows(existing_assignments)
            row_assignments.extend(existing_assignments)
            updated = len(existing_assignments)

        new_rows: List[int] = []
        if new_listings:
            new_rows = self._append_rows(new_listings)
            row_assignments.extend(zip(new_rows, new_listings))
            inserted = len(new_listings)

        if row_assignments:
            self._write_formulas(row_assignments)
        if new_rows:
            self._copy_template_format(new_rows)
        return {"inserted": inserted, "updated": updated}

    def _resolve_sheet_id(self) -> int:
        response = (
            self.service.spreadsheets()
            .get(spreadsheetId=self.spreadsheet_id, fields="sheets.properties")
            .execute()
        )
        for sheet in response.get("sheets", []):
            properties = sheet.get("properties", {})
            if properties.get("title") == self.sheet_name:
                return int(properties["sheetId"])
        raise ValueError(f"Google Sheet tab not found: {self.sheet_name}")

    def _existing_rows(self) -> Dict[str, int]:
        result = (
            self.service.spreadsheets()
            .values()
            .get(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{self.sheet_name}'!A3:A",
            )
            .execute()
        )
        rows: Dict[str, int] = {}
        for offset, values in enumerate(result.get("values", []), start=3):
            if values and values[0]:
                rows[str(values[0])] = offset
        return rows

    def _append_rows(self, listings: List[NormalizedListing]) -> List[int]:
        result = (
            self.service.spreadsheets()
            .values()
            .append(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{self.sheet_name}'!A:BA",
                valueInputOption="USER_ENTERED",
                insertDataOption="INSERT_ROWS",
                body={
                    "values": [
                        self._row_values(listing, row_number=None)
                        for listing in listings
                    ]
                },
            )
            .execute()
        )
        updated_range = result.get("updates", {}).get("updatedRange", "")
        match = re.search(r"![A-Z]+(\d+):[A-Z]+(\d+)$", updated_range)
        if not match:
            raise RuntimeError(f"Cannot determine appended rows from {updated_range!r}")
        start_row = int(match.group(1))
        end_row = int(match.group(2))
        expected_count = len(listings)
        if end_row - start_row + 1 != expected_count:
            raise RuntimeError(
                f"Expected {expected_count} appended rows, got {updated_range!r}"
            )
        return list(range(start_row, end_row + 1))

    def _batch_update_rows(
        self,
        assignments: List[tuple[int, NormalizedListing]],
    ) -> None:
        data = []
        for row_number, listing in assignments:
            values = self._row_values(listing, row_number=row_number)
            data.extend(
                [
                    {
                        "range": f"'{self.sheet_name}'!A{row_number}",
                        "values": [[values[0]]],
                    },
                    {
                        "range": f"'{self.sheet_name}'!D{row_number}:I{row_number}",
                        "values": [[*values[3:9]]],
                    },
                    {
                        "range": f"'{self.sheet_name}'!K{row_number}:W{row_number}",
                        "values": [[*values[10:23]]],
                    },
                    {
                        "range": f"'{self.sheet_name}'!AN{row_number}:AQ{row_number}",
                        "values": [[*values[39:43]]],
                    },
                    {
                        "range": f"'{self.sheet_name}'!AU{row_number}",
                        "values": [[values[46]]],
                    },
                    {
                        "range": f"'{self.sheet_name}'!AV{row_number}:BA{row_number}",
                        "values": [[*values[47:53]]],
                    },
                ]
            )
        (
            self.service.spreadsheets()
            .values()
            .batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={"valueInputOption": "USER_ENTERED", "data": data},
            )
            .execute()
        )

    def _write_formulas(
        self,
        assignments: Iterable[tuple[int, NormalizedListing]],
    ) -> None:
        data = []
        for row_number, listing in assignments:
            data.extend(
                [
                    {
                        "range": f"'{self.sheet_name}'!J{row_number}",
                        "values": [[f'=IFERROR(H{row_number}/I{row_number},"")']],
                    },
                    {
                        "range": f"'{self.sheet_name}'!AH{row_number}",
                        "values": [
                            [
                                f'=IF(COUNTA(X{row_number}:AG{row_number})=0,'
                                f"{listing.score},"
                                f"ROUND(SUMPRODUCT(X{row_number}:AG{row_number},"
                                "'Criteria'!$B$5:$B$14)*20,1))"
                            ]
                        ],
                    },
                    {
                        "range": f"'{self.sheet_name}'!AT{row_number}",
                        "values": [
                            [
                                f'=IF(H{row_number}="","",'
                                f'H{row_number}+IF(AS{row_number}="",0,AS{row_number}))'
                            ]
                        ],
                    },
                ]
            )
        (
            self.service.spreadsheets()
            .values()
            .batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={"valueInputOption": "USER_ENTERED", "data": data},
            )
            .execute()
        )

    def _copy_template_format(self, row_numbers: List[int]) -> None:
        requests = []
        for row_number in row_numbers:
            destination = {
                "sheetId": self.sheet_id,
                "startRowIndex": row_number - 1,
                "endRowIndex": row_number,
                "startColumnIndex": 0,
                "endColumnIndex": 47,
            }
            source = {
                "sheetId": self.sheet_id,
                "startRowIndex": 4,
                "endRowIndex": 5,
                "startColumnIndex": 0,
                "endColumnIndex": 47,
            }
            for paste_type in ("PASTE_FORMAT", "PASTE_DATA_VALIDATION"):
                requests.append(
                    {
                        "copyPaste": {
                            "source": source,
                            "destination": destination,
                            "pasteType": paste_type,
                            "pasteOrientation": "NORMAL",
                        }
                    }
                )
        try:
            (
                self.service.spreadsheets()
                .batchUpdate(
                    spreadsheetId=self.spreadsheet_id,
                    body={"requests": requests},
                )
                .execute()
            )
        except Exception as exc:
            LOGGER.warning("Rows were written, but template formatting copy failed: %s", exc)

    def _row_values(
        self,
        listing: NormalizedListing,
        row_number: Optional[int],
    ) -> List[object]:
        coordinates = ""
        if listing.latitude is not None and listing.longitude is not None:
            coordinates = f"{listing.latitude:.7f}, {listing.longitude:.7f}"
        status = "Новый"
        date_value = datetime.now().date().isoformat()
        electricity = listing.electricity_raw or ""
        if listing.electricity_kw is not None and "квт" not in electricity.lower():
            electricity = (
                f"{electricity}; {listing.electricity_kw:g} кВт"
                if electricity
                else f"{listing.electricity_kw:g} кВт"
            )
        risks = "; ".join(listing.reasons)
        pluses = _automatic_pluses(listing)

        return [
            listing.external_id,  # A
            status,  # B - never overwritten on update
            date_value,  # C
            _source_label(listing),  # D
            listing.canonical_url,  # E
            listing.district or "",  # F
            listing.locality or "",  # G
            listing.price_usd or "",  # H
            listing.area_sotok or "",  # I
            "",  # J formula is written separately
            listing.distance_mkad_km or "",  # K
            listing.facade_m or "",  # L
            listing.depth_m or "",  # M
            listing.purpose or "",  # N
            electricity,  # O
            listing.gas_raw or "",  # P
            listing.water_raw or "",  # Q
            listing.sewerage_raw or "",  # R
            listing.internet_raw or "Не указано",  # S
            listing.road_raw or "",  # T
            listing.nature_raw or "",  # U
            coordinates,  # V
            listing.seller_type or "",  # W
            "", "", "", "", "", "", "", "", "", "",  # X:AG manual scores
            "",  # AH manual rating formula
            "",  # AI Илья
            "",  # AJ Аня
            pluses,  # AK
            risks,  # AL
            "",  # AM
            listing.object_kind or "Участок",  # AN
            listing.house_area_sqm or "",  # AO
            listing.house_condition or "",  # AP
            listing.sale_format or "",  # AQ
            "",  # AR - manual house decision
            "",  # AS - manual works/demolition estimate
            "",  # AT - formula is written separately
            listing.house_risks or "",  # AU
            listing.location_key or "",  # AV
            listing.location_score if listing.location_score is not None else "",  # AW
            listing.location_verdict or "Локация изучается",  # AX
            listing.location_confidence or "Нет данных",  # AY
            "; ".join(listing.location_signals),  # AZ
            "; ".join(listing.location_risks),  # BA
        ]

    def sync_locations(self, profiles: Iterable[LocationProfile]) -> Dict[str, int]:
        values = list(profiles)
        self._ensure_plot_location_columns()
        location_sheet_id = self._ensure_locations_sheet()
        location_stats = self._upsert_location_rows(values)
        linked = self._write_listing_location_fields(values)
        self._format_locations_sheet(location_sheet_id)
        return {**location_stats, "plots_linked": linked}

    def _ensure_plot_location_columns(self) -> None:
        metadata = (
            self.service.spreadsheets()
            .get(spreadsheetId=self.spreadsheet_id, fields="sheets.properties")
            .execute()
        )
        column_count = 0
        for sheet in metadata.get("sheets", []):
            properties = sheet.get("properties", {})
            if properties.get("sheetId") == self.sheet_id:
                column_count = int(
                    properties.get("gridProperties", {}).get("columnCount", 0)
                )
                break
        if column_count < 53:
            (
                self.service.spreadsheets()
                .batchUpdate(
                    spreadsheetId=self.spreadsheet_id,
                    body={
                        "requests": [
                            {
                                "appendDimension": {
                                    "sheetId": self.sheet_id,
                                    "dimension": "COLUMNS",
                                    "length": 53 - column_count,
                                }
                            }
                        ]
                    },
                )
                .execute()
            )
        (
            self.service.spreadsheets()
            .values()
            .update(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{self.sheet_name}'!AV4:BA4",
                valueInputOption="RAW",
                body={"values": [PLOT_LOCATION_HEADERS]},
            )
            .execute()
        )

    def _ensure_locations_sheet(self) -> int:
        metadata = (
            self.service.spreadsheets()
            .get(spreadsheetId=self.spreadsheet_id, fields="sheets.properties")
            .execute()
        )
        for sheet in metadata.get("sheets", []):
            properties = sheet.get("properties", {})
            if properties.get("title") == "Locations":
                return int(properties["sheetId"])
        response = (
            self.service.spreadsheets()
            .batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={
                    "requests": [
                        {
                            "addSheet": {
                                "properties": {
                                    "title": "Locations",
                                    "gridProperties": {
                                        "rowCount": 2000,
                                        "columnCount": len(LOCATION_HEADERS),
                                        "frozenRowCount": 4,
                                    },
                                }
                            }
                        }
                    ]
                },
            )
            .execute()
        )
        return int(response["replies"][0]["addSheet"]["properties"]["sheetId"])

    def _upsert_location_rows(self, profiles: List[LocationProfile]) -> Dict[str, int]:
        (
            self.service.spreadsheets()
            .values()
            .batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={
                    "valueInputOption": "RAW",
                    "data": [
                        {
                            "range": "'Locations'!A1",
                            "values": [["Профили населённых пунктов и садовых товариществ"]],
                        },
                        {
                            "range": "'Locations'!A4:U4",
                            "values": [LOCATION_HEADERS],
                        },
                    ],
                },
            )
            .execute()
        )
        result = (
            self.service.spreadsheets()
            .values()
            .get(spreadsheetId=self.spreadsheet_id, range="'Locations'!A5:A")
            .execute()
        )
        existing = {
            str(values[0]): row
            for row, values in enumerate(result.get("values", []), start=5)
            if values and values[0]
        }
        updates = []
        new_rows = []
        for profile in profiles:
            row = _location_row(profile)
            row_number = existing.get(profile.key)
            if row_number is None:
                new_rows.append(row)
            else:
                updates.append(
                    {
                        "range": f"'Locations'!A{row_number}:T{row_number}",
                        "values": [row],
                    }
                )
        for chunk in _chunks(updates, 400):
            (
                self.service.spreadsheets()
                .values()
                .batchUpdate(
                    spreadsheetId=self.spreadsheet_id,
                    body={"valueInputOption": "RAW", "data": chunk},
                )
                .execute()
            )
        if new_rows:
            (
                self.service.spreadsheets()
                .values()
                .append(
                    spreadsheetId=self.spreadsheet_id,
                    range="'Locations'!A:T",
                    valueInputOption="RAW",
                    insertDataOption="INSERT_ROWS",
                    body={"values": new_rows},
                )
                .execute()
            )
        return {"locations_inserted": len(new_rows), "locations_updated": len(updates)}

    def _write_listing_location_fields(self, profiles: List[LocationProfile]) -> int:
        existing = self._existing_rows()
        updates = []
        for profile in profiles:
            location_values = [
                profile.key,
                profile.score,
                profile.verdict,
                profile.confidence,
                "; ".join(profile.signals),
                "; ".join(profile.risks),
            ]
            for external_id in profile.member_external_ids:
                row_number = existing.get(external_id)
                if row_number is not None:
                    updates.append(
                        {
                            "range": (
                                f"'{self.sheet_name}'!AV{row_number}:BA{row_number}"
                            ),
                            "values": [location_values],
                        }
                    )
        for chunk in _chunks(updates, 400):
            (
                self.service.spreadsheets()
                .values()
                .batchUpdate(
                    spreadsheetId=self.spreadsheet_id,
                    body={"valueInputOption": "RAW", "data": chunk},
                )
                .execute()
            )
        return len(updates)

    def _format_locations_sheet(self, sheet_id: int) -> None:
        requests = [
            {
                "updateSheetProperties": {
                    "properties": {
                        "sheetId": sheet_id,
                        "gridProperties": {"frozenRowCount": 4},
                    },
                    "fields": "gridProperties.frozenRowCount",
                }
            },
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 3,
                        "endRowIndex": 4,
                        "startColumnIndex": 0,
                        "endColumnIndex": len(LOCATION_HEADERS),
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "textFormat": {"bold": True},
                            "wrapStrategy": "WRAP",
                        }
                    },
                    "fields": (
                        "userEnteredFormat.textFormat,"
                        "userEnteredFormat.wrapStrategy"
                    ),
                }
            },
        ]
        try:
            (
                self.service.spreadsheets()
                .batchUpdate(
                    spreadsheetId=self.spreadsheet_id,
                    body={"requests": requests},
                )
                .execute()
            )
        except Exception as exc:
            LOGGER.warning("Location data was written, but formatting failed: %s", exc)


def _automatic_pluses(listing: NormalizedListing) -> str:
    values: List[str] = []
    if listing.price_usd is not None and listing.price_usd <= 20_000:
        values.append("Цена в целевом бюджете")
    if listing.area_sotok is not None and 9 <= listing.area_sotok <= 11:
        values.append("Подходящая площадь")
    if listing.distance_mkad_km is not None and listing.distance_mkad_km <= 30:
        values.append("До 30 км от МКАД")
    if listing.electricity_kw is not None and listing.electricity_kw >= 20:
        values.append("Электричество 20+ кВт")
    if listing.ownership_raw and "частн" in listing.ownership_raw.lower():
        values.append("Частная собственность")
    if listing.internet_raw == "Оптика / проводной":
        values.append("Оптика или проводной интернет")
    elif listing.internet_raw == "Мобильный 4G/LTE":
        values.append("Есть мобильный интернет")
    elif listing.internet_raw == "Есть, тип не указан":
        values.append("Интернет упомянут")
    return "; ".join(values)


def _source_label(listing: NormalizedListing) -> str:
    source = listing.source
    label = {
        "realt": "Realt",
        "realt_auction": "Realt аукцион",
        "rlt_auction": "RLT аукцион",
        "e_auction": "e-auction.by",
        "beltorgi_auction": "Белторги",
        "kufar": "Kufar",
    }.get(source, source.replace("_", " ").capitalize())
    if listing.object_kind and listing.object_kind != "Участок":
        label = f"{label} · Дом"
    if listing.raw_payload.get("is_auction") and "аукцион" not in label.lower():
        label = f"{label} · Аукцион"
    if listing.raw_payload.get("is_aggregator"):
        label = f"{label} · Агрегатор"
    if listing.raw_payload.get("preferred_location"):
        label = f"{label} · Приоритет"
    return label


def _location_row(profile: LocationProfile) -> List[object]:
    return [
        profile.key,
        profile.label,
        profile.kind,
        profile.district or "",
        profile.listing_count,
        profile.eligible_count,
        profile.house_count,
        profile.premium_house_count,
        profile.median_house_price_usd or "",
        profile.median_distance_mkad_km
        if profile.median_distance_mkad_km is not None
        else "",
        profile.electricity_share,
        profile.gas_share,
        profile.internet_share,
        profile.paved_road_share,
        profile.score,
        profile.verdict,
        profile.confidence,
        "; ".join(profile.signals),
        "; ".join(profile.risks),
        profile.calculated_at.isoformat() if profile.calculated_at else "",
    ]


def _chunks(values: List[dict], size: int) -> Iterable[List[dict]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]
