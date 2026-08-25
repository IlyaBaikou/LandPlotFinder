from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from sqlalchemy import case, func, select

from app.config import Settings
from app.db import init_db, make_engine, make_session_factory, session_scope
from app.http import PageUnavailableError, PublicPageClient, SourceBlockedError
from app.integrations.trips import SHEETS_SCOPE
from app.models import ListingActivityModel, ListingEventModel, ListingModel
from app.sources.next_data import extract_next_data

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ActivityTarget:
    listing_id: int
    external_id: str
    source: str
    canonical_url: str
    last_seen_at: datetime
    previous_checks: int


class ListingActivityChecker:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def run(self) -> Dict[str, int]:
        targets = self._targets()
        results: List[Tuple[ActivityTarget, str, Optional[str]]] = []
        with PublicPageClient(
            timeout_seconds=self.settings.http_timeout_seconds,
            retries=self.settings.http_retries,
            delay_seconds=max(
                self.settings.request_delay_seconds,
                self.settings.activity_request_delay_seconds,
            ),
        ) as client:
            for target in targets:
                try:
                    html = client.get_text(target.canonical_url)
                    status = listing_page_status(target.source, html)
                    error = None if status != "unknown" else "Новая структура страницы"
                except PageUnavailableError as exc:
                    status, error = "unavailable", str(exc)
                except SourceBlockedError as exc:
                    LOGGER.warning("Activity check rate-limited for %s: %s", target.source, exc)
                    status, error = "error", str(exc)
                except Exception as exc:
                    LOGGER.warning("Activity check failed for %s: %s", target.external_id, exc)
                    status, error = "error", str(exc)
                results.append((target, status, error))

        archived = self._save_results(results)
        if archived and self.settings.sheets_enabled:
            GoogleSheetActivitySink(
                spreadsheet_id=self.settings.google_spreadsheet_id or "",
                sheet_name=self.settings.google_plots_sheet,
                credentials_info=self.settings.google_credentials_info() or {},
            ).archive(archived)
        return {
            "checked": len(results),
            "available": sum(status == "available" for _, status, _ in results),
            "unavailable": sum(status == "unavailable" for _, status, _ in results),
            "errors": sum(status in {"error", "unknown"} for _, status, _ in results),
            "archived": len(archived),
        }

    def _targets(self) -> List[ActivityTarget]:
        engine = make_engine(self.settings.database_url)
        init_db(engine)
        factory = make_session_factory(engine)
        try:
            with session_scope(factory) as session:
                previous = func.coalesce(
                    ListingActivityModel.consecutive_unavailable,
                    0,
                )
                rows = session.execute(
                    select(ListingModel, ListingActivityModel)
                    .outerjoin(
                        ListingActivityModel,
                        ListingActivityModel.listing_id == ListingModel.id,
                    )
                    .where(
                        ListingModel.active.is_(True),
                        ListingModel.source.in_({"realt", "kufar"}),
                        ListingModel.price_usd.is_not(None),
                        ListingModel.price_usd <= self.settings.profile.max_price_usd,
                        ListingModel.area_sotok.is_not(None),
                        ListingModel.area_sotok.between(
                            self.settings.profile.discovery_min_area_sotok,
                            self.settings.profile.discovery_max_area_sotok,
                        ),
                        ListingModel.distance_mkad_km.is_not(None),
                        ListingModel.distance_mkad_km
                        <= self.settings.profile.max_distance_km,
                    )
                    .order_by(
                        case((previous > 0, 0), else_=1),
                        ListingActivityModel.last_checked_at.asc().nulls_first(),
                        ListingModel.last_seen_at.asc(),
                    )
                    .limit(self.settings.activity_check_batch_size)
                )
                return [
                    ActivityTarget(
                        listing_id=listing.id,
                        external_id=f"{listing.source}:{listing.source_id}",
                        source=listing.source,
                        canonical_url=listing.canonical_url,
                        last_seen_at=listing.last_seen_at,
                        previous_checks=(activity.consecutive_unavailable if activity else 0),
                    )
                    for listing, activity in rows
                ]
        finally:
            engine.dispose()

    def _save_results(
        self,
        results: List[Tuple[ActivityTarget, str, Optional[str]]],
    ) -> List[str]:
        engine = make_engine(self.settings.database_url)
        init_db(engine)
        factory = make_session_factory(engine)
        archived: List[str] = []
        now = datetime.now(timezone.utc)
        try:
            with session_scope(factory) as session:
                for target, status, error in results:
                    listing = session.get(ListingModel, target.listing_id)
                    if listing is None:
                        continue
                    activity = session.scalar(
                        select(ListingActivityModel).where(
                            ListingActivityModel.listing_id == target.listing_id
                        )
                    )
                    if activity is None:
                        activity = ListingActivityModel(listing_id=target.listing_id)
                        session.add(activity)
                    if activity.last_checked_at is not None and _timestamp(
                        listing.last_seen_at
                    ) > _timestamp(activity.last_checked_at):
                        activity.consecutive_unavailable = 0
                    activity.last_checked_at = now
                    activity.last_status = status
                    activity.last_error = error
                    if status == "available":
                        activity.consecutive_unavailable = 0
                        activity.last_available_at = now
                        listing.active = True
                    elif status == "unavailable":
                        activity.consecutive_unavailable = (
                            activity.consecutive_unavailable or 0
                        ) + 1
                        if (
                            activity.consecutive_unavailable
                            >= self.settings.activity_confirmation_count
                        ):
                            if listing.active:
                                listing.active = False
                                session.add(
                                    ListingEventModel(
                                        listing_id=listing.id,
                                        event_type="archived",
                                        payload={
                                            "reason": error or "Страница объявления недоступна",
                                            "checks": activity.consecutive_unavailable,
                                        },
                                    )
                                )
                                archived.append(target.external_id)
            return archived
        finally:
            engine.dispose()


class GoogleSheetActivitySink:
    def __init__(
        self,
        spreadsheet_id: str,
        sheet_name: str,
        credentials_info: dict,
    ) -> None:
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build

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

    def archive(self, external_ids: List[str]) -> None:
        response = self.service.spreadsheets().values().get(
            spreadsheetId=self.spreadsheet_id,
            range=f"'{self.sheet_name}'!A5:B",
        ).execute()
        wanted = set(external_ids)
        updates = []
        for row_number, row in enumerate(response.get("values", []), start=5):
            if not row or str(row[0]) not in wanted:
                continue
            updates.append(
                {
                    "range": f"'{self.sheet_name}'!B{row_number}",
                    "values": [["Архив"]],
                }
            )
        if updates:
            self.service.spreadsheets().values().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={"valueInputOption": "RAW", "data": updates},
            ).execute()


def listing_page_status(source: str, html: str) -> str:
    try:
        data = extract_next_data(html)
    except ValueError:
        return "unknown"
    if source == "realt":
        page_props = data.get("props", {}).get("pageProps", {})
        if isinstance(page_props.get("object"), dict):
            return "available"
        if page_props.get("notFound") is True or "object" in page_props:
            return "unavailable"
        return "unknown"
    if source == "kufar":
        ad_view = data.get("props", {}).get("initialState", {}).get("adView", {})
        if isinstance(ad_view.get("data"), dict):
            return "available"
        if "data" in ad_view:
            return "unavailable"
        return "unknown"
    return "unknown"


def _timestamp(value: datetime) -> float:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()
