from __future__ import annotations

import logging
from typing import Any, Dict

from app.config import Settings
from app.db import init_db, make_engine, make_session_factory, session_scope
from app.integrations.sheets import GoogleSheetsSink
from app.integrations.trips import GoogleTripsSink
from app.location_enrichment import LocationSignalCollector
from app.locations import rebuild_location_profiles

LOGGER = logging.getLogger(__name__)


class LocationEnricher:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def run(self) -> Dict[str, Any]:
        engine = make_engine(self.settings.database_url)
        init_db(engine)
        factory = make_session_factory(engine)
        enrichment_stats: Dict[str, Any] = {}
        try:
            with session_scope(factory) as session:
                profiles = rebuild_location_profiles(session)
            enrichment_stats = LocationSignalCollector(
                self.settings,
                factory,
            ).run(profiles)
            with session_scope(factory) as session:
                profiles = rebuild_location_profiles(session)
        finally:
            engine.dispose()

        stats = {
            "locations": len(profiles),
            "database_listings_grouped": sum(
                len(item.member_external_ids) for item in profiles
            ),
            **enrichment_stats,
        }
        if self.settings.sheets_enabled:
            visible_profiles = [item for item in profiles if item.eligible_count > 0]
            sink = GoogleSheetsSink(
                spreadsheet_id=self.settings.google_spreadsheet_id or "",
                sheet_name=self.settings.google_plots_sheet,
                credentials_info=self.settings.google_credentials_info() or {},
            )
            sheet_stats = sink.sync_locations(visible_profiles)
            stats.update(sheet_stats)
            LOGGER.info("Location Sheets sync: %s", sheet_stats)
            try:
                trip_stats = GoogleTripsSink(
                    spreadsheet_id=self.settings.google_spreadsheet_id or "",
                    credentials_info=self.settings.google_credentials_info() or {},
                ).sync()
                stats.update(trip_stats)
                LOGGER.info("Trip map sync: %s", trip_stats)
            except Exception as exc:
                LOGGER.exception("Trip map sync failed")
                stats["trip_map_error"] = str(exc)
        return stats
