from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlencode

SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
TRIP_STATUSES = {
    "изучаем",
    "к поездке",
    "смотрим",
    "понравилось",
    "избранное",
}
MINSK_CENTER = (53.9006, 27.5590)


@dataclass(frozen=True)
class TripPoint:
    external_id: str
    status: str
    source: str
    listing_url: str
    district: str
    place: str
    price: str
    area: str
    distance: str
    latitude: float
    longitude: float
    rating: str
    location_score: str

    @property
    def coordinates(self) -> str:
        return f"{self.latitude:.7f},{self.longitude:.7f}"


class GoogleTripsSink:
    def __init__(self, spreadsheet_id: str, credentials_info: dict) -> None:
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build

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

    def sync(self) -> Dict[str, int]:
        points = self._selected_points()
        routes = build_trip_routes(points)
        sheet_id = self._ensure_sheet()
        self._write(points, routes)
        self._format(sheet_id)
        return {
            "trip_points": len(points),
            "trip_routes": len(routes),
        }

    def select_listing_url(self, listing_url: str) -> Optional[str]:
        target = listing_identity(listing_url)
        response = (
            self.service.spreadsheets()
            .values()
            .get(
                spreadsheetId=self.spreadsheet_id,
                range="'Plots'!A5:E",
            )
            .execute()
        )
        for row_number, row in enumerate(response.get("values", []), start=5):
            external_id = _cell(row, 1)
            saved_url = _cell(row, 5)
            if not (
                target
                and target in {listing_identity(external_id), listing_identity(saved_url)}
            ) and _canonical_url(saved_url) != _canonical_url(listing_url):
                continue
            (
                self.service.spreadsheets()
                .values()
                .update(
                    spreadsheetId=self.spreadsheet_id,
                    range=f"'Plots'!B{row_number}",
                    valueInputOption="RAW",
                    body={"values": [["Изучаем"]]},
                )
                .execute()
            )
            return external_id
        return None

    def route_urls(self) -> List[str]:
        return [
            google_maps_route_url(route)
            for route in build_trip_routes(self._selected_points())
        ]

    def _selected_points(self) -> List[TripPoint]:
        response = (
            self.service.spreadsheets()
            .values()
            .get(
                spreadsheetId=self.spreadsheet_id,
                range="'Plots'!A5:BA",
            )
            .execute()
        )
        points = []
        for row in response.get("values", []):
            status = _cell(row, 2)
            if _normalize_status(status) not in TRIP_STATUSES:
                continue
            coordinates = parse_coordinates(_cell(row, 22))
            if coordinates is None:
                continue
            points.append(
                TripPoint(
                    external_id=_cell(row, 1),
                    status=status,
                    source=_cell(row, 4),
                    listing_url=_cell(row, 5),
                    district=_cell(row, 6),
                    place=_cell(row, 7),
                    price=_cell(row, 8),
                    area=_cell(row, 9),
                    distance=_cell(row, 11),
                    latitude=coordinates[0],
                    longitude=coordinates[1],
                    rating=_cell(row, 34),
                    location_score=_cell(row, 49),
                )
            )
        return points

    def _ensure_sheet(self) -> int:
        metadata = (
            self.service.spreadsheets()
            .get(spreadsheetId=self.spreadsheet_id, fields="sheets.properties")
            .execute()
        )
        for sheet in metadata.get("sheets", []):
            properties = sheet.get("properties", {})
            if properties.get("title") == "Trips":
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
                                    "title": "Trips",
                                    "gridProperties": {
                                        "rowCount": 1000,
                                        "columnCount": 14,
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

    def _write(
        self,
        points: Sequence[TripPoint],
        routes: Sequence[Sequence[TripPoint]],
    ) -> None:
        self.service.spreadsheets().values().clear(
            spreadsheetId=self.spreadsheet_id,
            range="'Trips'!A:N",
            body={},
        ).execute()
        route_rows: List[List[object]] = []
        route_by_id: Dict[str, int] = {}
        for index, route in enumerate(routes, start=1):
            for point in route:
                route_by_id[point.external_id] = index
            route_rows.append(
                [
                    f"Маршрут {index}",
                    len(route),
                    " → ".join(point.place or point.district for point in route),
                    google_maps_route_url(route),
                ]
            )
        point_rows = [
            [
                route_by_id.get(point.external_id, ""),
                point.status,
                point.place,
                point.district,
                point.price,
                point.area,
                point.distance,
                point.rating,
                point.location_score,
                point.coordinates,
                point.source,
                point.listing_url,
                google_maps_pin_url(point),
                point.external_id,
            ]
            for point in points
        ]
        data: List[dict] = [
            {
                "range": "'Trips'!A1",
                "values": [["🗺 Выбранные участки и маршруты поездки"]],
            },
            {
                "range": "'Trips'!A2",
                "values": [[
                    "Источник — строки Plots со статусом «Изучаем» или «К поездке». "
                    "Маршрут открывается в Google Maps и стартует от текущей геопозиции."
                ]],
            },
            {
                "range": "'Trips'!A4:D4",
                "values": [["Маршрут", "Точек", "Порядок", "Открыть в Google Maps"]],
            },
        ]
        if route_rows:
            data.append({"range": "'Trips'!A5:D", "values": route_rows})
        point_header_row = 6 + len(route_rows)
        data.append(
            {
                "range": f"'Trips'!A{point_header_row}:N{point_header_row}",
                "values": [[
                    "№ маршрута",
                    "Статус",
                    "Локация",
                    "Район",
                    "Цена, $",
                    "Соток",
                    "До МКАД, км",
                    "Рейтинг",
                    "Оценка локации",
                    "Координаты",
                    "Источник",
                    "Объявление",
                    "Точка на карте",
                    "ID",
                ]],
            }
        )
        if point_rows:
            data.append(
                {
                    "range": f"'Trips'!A{point_header_row + 1}:N",
                    "values": point_rows,
                }
            )
        (
            self.service.spreadsheets()
            .values()
            .batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={"valueInputOption": "RAW", "data": data},
            )
            .execute()
        )

    def _format(self, sheet_id: int) -> None:
        self.service.spreadsheets().batchUpdate(
            spreadsheetId=self.spreadsheet_id,
            body={
                "requests": [
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
                                "startRowIndex": 0,
                                "endRowIndex": 1,
                                "startColumnIndex": 0,
                                "endColumnIndex": 14,
                            },
                            "cell": {
                                "userEnteredFormat": {
                                    "textFormat": {"bold": True, "fontSize": 14}
                                }
                            },
                            "fields": "userEnteredFormat.textFormat",
                        }
                    },
                    {
                        "autoResizeDimensions": {
                            "dimensions": {
                                "sheetId": sheet_id,
                                "dimension": "COLUMNS",
                                "startIndex": 0,
                                "endIndex": 14,
                            }
                        }
                    },
                ]
            },
        ).execute()


def build_trip_routes(
    points: Iterable[TripPoint],
    max_points: int = 4,
    start: Tuple[float, float] = MINSK_CENTER,
) -> List[List[TripPoint]]:
    remaining = list(points)
    ordered: List[TripPoint] = []
    current = start
    while remaining:
        next_point = min(
            remaining,
            key=lambda point: _haversine_km(
                current[0],
                current[1],
                point.latitude,
                point.longitude,
            ),
        )
        ordered.append(next_point)
        remaining.remove(next_point)
        current = (next_point.latitude, next_point.longitude)
    return [
        ordered[index : index + max_points]
        for index in range(0, len(ordered), max_points)
    ]


def google_maps_route_url(points: Sequence[TripPoint]) -> str:
    if not points:
        return ""
    params = {
        "api": "1",
        "destination": points[-1].coordinates,
        "travelmode": "driving",
        "dir_action": "navigate",
    }
    if len(points) > 1:
        params["waypoints"] = "|".join(point.coordinates for point in points[:-1])
    return "https://www.google.com/maps/dir/?" + urlencode(params)


def google_maps_pin_url(point: TripPoint) -> str:
    return "https://www.google.com/maps/search/?" + urlencode(
        {"api": "1", "query": point.coordinates}
    )


def parse_coordinates(value: object) -> Optional[Tuple[float, float]]:
    parts = [part.strip() for part in str(value or "").split(",")]
    if len(parts) != 2:
        return None
    try:
        latitude, longitude = float(parts[0]), float(parts[1])
    except ValueError:
        return None
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    return latitude, longitude


def listing_identity(value: object) -> Optional[str]:
    text = str(value or "").lower()
    if text.startswith("kufar:") or text.startswith("realt:"):
        source, _, source_id = text.partition(":")
        return f"{source}:{source_id}" if source_id else None
    kufar = re.search(r"re\.kufar\.by/(?:vi/)?(?P<id>\d+)", text)
    if kufar:
        return f"kufar:{kufar.group('id')}"
    realt = re.search(r"realt\.by/[^?#]*/object/(?P<id>\d+)", text)
    if realt:
        return f"realt:{realt.group('id')}"
    return None


def _cell(row: Sequence[object], column: int) -> str:
    return str(row[column - 1]).strip() if len(row) >= column else ""


def _normalize_status(value: str) -> str:
    return " ".join(value.lower().replace("ё", "е").split())


def _canonical_url(value: object) -> str:
    return str(value or "").split("?", 1)[0].split("#", 1)[0].rstrip("/").lower()


def _haversine_km(
    latitude_a: float,
    longitude_a: float,
    latitude_b: float,
    longitude_b: float,
) -> float:
    lat_a = math.radians(latitude_a)
    lat_b = math.radians(latitude_b)
    delta_lat = lat_b - lat_a
    delta_lon = math.radians(longitude_b - longitude_a)
    value = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat_a) * math.cos(lat_b) * math.sin(delta_lon / 2) ** 2
    )
    return 6371.0088 * 2 * math.asin(math.sqrt(value))
