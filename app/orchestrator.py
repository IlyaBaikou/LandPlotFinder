from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Set

from sqlalchemy import select

from app.config import Settings
from app.db import init_db, make_engine, make_session_factory, session_scope
from app.dedupe import flag_possible_duplicates
from app.domain import MatchStatus, NormalizedListing, ScanResult
from app.filters import evaluate_listing
from app.http import PublicPageClient
from app.integrations.telegram import TelegramNotifier
from app.locations import attach_location_profiles, load_location_profiles
from app.models import ListingModel, ScanRunModel
from app.repository import hydrate_listings, upsert_listing, upsert_listing_profile
from app.source_health import quality_metrics, record_health_batch
from app.sources.base import ListingSource
from app.sources.beltorgi import BeltorgiAuctionSource
from app.sources.e_auction import EauctionSource
from app.sources.kufar import KufarSource
from app.sources.realt import RealtSource
from app.sources.realt_auction import RealtAuctionSource
from app.sources.rlt_auction import RltAuctionSource

LOGGER = logging.getLogger(__name__)


class Scanner:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def run(
        self,
        dry_run: Optional[bool] = None,
        enabled_sources: Optional[Sequence[str]] = None,
        queue: str = "standard",
        profile_id: str = "default",
    ) -> ScanResult:
        if queue not in {"standard", "preferred"}:
            raise ValueError(f"Unknown scan queue: {queue}")
        effective_dry_run = self.settings.dry_run if dry_run is None else dry_run
        result = ScanResult(started_at=datetime.now(timezone.utc))
        health_observations = []

        with PublicPageClient(
            timeout_seconds=self.settings.http_timeout_seconds,
            retries=self.settings.http_retries,
            delay_seconds=self.settings.request_delay_seconds,
        ) as client:
            listing_state = self._listing_state() if not effective_dry_run else {}
            sources = self._sources(client, enabled_sources, listing_state, queue=queue)
            for source in sources:
                stat_key = f"preferred:{source.name}" if queue == "preferred" else source.name
                started = time.monotonic()
                http_before = client.metrics()
                try:
                    raw_listings = source.scan()
                    if not effective_dry_run:
                        raw_listings = self._hydrate(raw_listings)
                    if queue == "preferred":
                        _mark_preferred(
                            raw_listings,
                            self.settings.preferred_location_name,
                        )
                    evaluated = [
                        evaluate_listing(listing, self.settings.profile)
                        for listing in raw_listings
                    ]
                    result.listings.extend(evaluated)
                    stats = _status_counts(evaluated)
                    quality = quality_metrics(evaluated)
                    http_metrics = _metric_delta(http_before, client.metrics())
                    stats.update(
                        {
                            "duration_ms": round((time.monotonic() - started) * 1000),
                            "http": http_metrics,
                            "quality": quality,
                        }
                    )
                    result.source_stats[stat_key] = stats
                    health_observations.append(
                        {
                            "source": stat_key,
                            "duration_ms": stats["duration_ms"],
                            "http": http_metrics,
                            "quality": quality,
                        }
                    )
                except Exception as exc:
                    LOGGER.exception("Source %s failed in %s queue", source.name, queue)
                    result.errors[stat_key] = str(exc)
                    http_metrics = _metric_delta(http_before, client.metrics())
                    duration_ms = round((time.monotonic() - started) * 1000)
                    result.source_stats[stat_key] = {
                        "error": 1,
                        "duration_ms": duration_ms,
                        "http": http_metrics,
                    }
                    health_observations.append(
                        {
                            "source": stat_key,
                            "duration_ms": duration_ms,
                            "http": http_metrics,
                            "quality": {"total": 0, "completeness": {}},
                            "error": str(exc),
                        }
                    )

        flag_possible_duplicates(result.listings)

        if not effective_dry_run:
            record_health_batch(
                self.settings.database_url,
                profile_id,
                health_observations,
            )
            self._attach_location_profiles(result.listings)
            self._persist(result, profile_id)
            self._sync_external(result)
        else:
            result.new_listings = list(result.listings)

        result.completed_at = datetime.now(timezone.utc)
        return result

    def _sources(
        self,
        client: PublicPageClient,
        enabled_sources: Optional[Sequence[str]],
        listing_state: Optional[Dict[str, Dict[str, Set[str]]]] = None,
        queue: str = "standard",
    ) -> List[ListingSource]:
        default_sources = (
            ("realt", "kufar")
            if queue == "preferred"
            else (
                "realt",
                "realt_auction",
                "rlt_auction",
                "e_auction",
                "kufar",
            )
        )
        enabled = set(enabled_sources or default_sources)
        if queue == "preferred":
            enabled &= {"realt", "kufar"}
        realt_search_urls = (
            self.settings.preferred_realt_search_urls
            if queue == "preferred"
            else self.settings.realt_search_urls
        )
        kufar_search_urls = (
            self.settings.preferred_kufar_search_urls
            if queue == "preferred"
            else self.settings.kufar_search_urls
        )
        max_details = (
            self.settings.preferred_max_details_per_source
            if queue == "preferred"
            else self.settings.max_details_per_source
        )
        sources: List[ListingSource] = []
        if "realt" in enabled:
            sources.append(
                RealtSource(
                    client=client,
                    search_urls=realt_search_urls,
                    profile=self.settings.profile,
                    max_details=max_details,
                )
            )
        if "realt_auction" in enabled:
            sources.append(
                RealtAuctionSource(
                    client=client,
                    search_urls=self.settings.realt_auction_urls,
                    profile=self.settings.profile,
                    max_details=self.settings.max_details_per_source,
                )
            )
        if "rlt_auction" in enabled:
            sources.append(
                RltAuctionSource(
                    client=client,
                    search_urls=self.settings.rlt_auction_urls,
                    profile=self.settings.profile,
                    max_details=self.settings.max_details_per_source,
                )
            )
        if "e_auction" in enabled:
            sources.append(
                EauctionSource(
                    client=client,
                    search_urls=self.settings.e_auction_urls,
                    profile=self.settings.profile,
                    max_details=self.settings.max_details_per_source,
                )
            )
        if "beltorgi_auction" in enabled and self.settings.beltorgi_auction_urls:
            sources.append(
                BeltorgiAuctionSource(
                    client=client,
                    search_urls=self.settings.beltorgi_auction_urls,
                    profile=self.settings.profile,
                    max_details=self.settings.max_details_per_source,
                    max_pages=self.settings.beltorgi_max_pages,
                )
            )
        if "kufar" in enabled:
            kufar_state = (listing_state or {}).get("kufar", {})
            sources.append(
                KufarSource(
                    client=client,
                    search_urls=kufar_search_urls,
                    profile=self.settings.profile,
                    max_details=max_details,
                    max_search_pages=self.settings.kufar_max_search_pages,
                    detail_delay_seconds=self.settings.kufar_detail_delay_seconds,
                    detail_batch_size=self.settings.kufar_detail_batch_size,
                    detail_batch_pause_seconds=(
                        self.settings.kufar_detail_batch_pause_seconds
                    ),
                    rate_limit_pause_seconds=(
                        self.settings.kufar_rate_limit_pause_seconds
                    ),
                    known_listing_ids=kufar_state.get("known", set()),
                    known_distance_ids=kufar_state.get("distance", set()),
                    pending_notification_ids=kufar_state.get("pending", set()),
                )
            )
        return sources

    def _listing_state(self) -> Dict[str, Dict[str, Set[str]]]:
        engine = make_engine(self.settings.database_url)
        init_db(engine)
        factory = make_session_factory(engine)
        result: Dict[str, Dict[str, Set[str]]] = {}
        try:
            with session_scope(factory) as session:
                rows = session.execute(
                    select(
                        ListingModel.source,
                        ListingModel.source_id,
                        ListingModel.distance_mkad_km,
                        ListingModel.raw_payload,
                    )
                )
                for source, source_id, distance, raw_payload in rows:
                    state = result.setdefault(
                        source,
                        {"known": set(), "distance": set(), "pending": set()},
                    )
                    state["known"].add(source_id)
                    if distance is not None:
                        state["distance"].add(source_id)
                    if (raw_payload or {}).get("telegram_pending_enrichment"):
                        state["pending"].add(source_id)
            return result
        finally:
            engine.dispose()

    def _hydrate(
        self,
        listings: Iterable[NormalizedListing],
    ) -> List[NormalizedListing]:
        engine = make_engine(self.settings.database_url)
        init_db(engine)
        factory = make_session_factory(engine)
        try:
            with session_scope(factory) as session:
                return hydrate_listings(session, listings)
        finally:
            engine.dispose()

    def _persist(self, result: ScanResult, profile_id: str = "default") -> None:
        engine = make_engine(self.settings.database_url)
        init_db(engine)
        factory = make_session_factory(engine)
        with session_scope(factory) as session:
            scan_run = ScanRunModel(
                started_at=result.started_at,
                status="RUNNING",
                source_stats=result.source_stats,
                errors=result.errors,
            )
            session.add(scan_run)
            session.flush()

            for listing in result.listings:
                model, is_new, is_changed, pending_released = upsert_listing(
                    session, listing
                )
                upsert_listing_profile(session, model, listing, profile_id)
                is_pending = bool(
                    listing.raw_payload.get("telegram_pending_enrichment")
                )
                if (is_new and not is_pending) or pending_released:
                    result.new_listings.append(listing)
                elif is_changed and not is_pending:
                    result.changed_listings.append(listing)

            scan_run.completed_at = datetime.now(timezone.utc)
            scan_run.status = "PARTIAL" if result.errors else "SUCCESS"
            result.completed_at = scan_run.completed_at
        engine.dispose()

    def _attach_location_profiles(
        self,
        listings: Iterable[NormalizedListing],
    ) -> None:
        engine = make_engine(self.settings.database_url)
        init_db(engine)
        factory = make_session_factory(engine)
        try:
            with session_scope(factory) as session:
                profiles = load_location_profiles(session)
            attach_location_profiles(listings, profiles)
        finally:
            engine.dispose()

    def _sync_external(self, result: ScanResult) -> None:
        sheet_listings = _unique(result.listings)

        if self.settings.sheets_enabled:
            try:
                from app.integrations.sheets import GoogleSheetsSink

                sink = GoogleSheetsSink(
                    spreadsheet_id=self.settings.google_spreadsheet_id or "",
                    sheet_name=self.settings.google_plots_sheet,
                    credentials_info=self.settings.google_credentials_info() or {},
                )
                stats = sink.sync(sheet_listings)
                LOGGER.info("Google Sheets sync: %s", stats)
            except Exception as exc:
                LOGGER.exception("Google Sheets sync failed")
                result.errors["google_sheets"] = str(exc)

        if self.settings.telegram_enabled:
            notifier = TelegramNotifier(
                bot_token=self.settings.telegram_bot_token or "",
                chat_id=self.settings.telegram_chat_id or "",
                selection_buttons=self.settings.sheets_enabled,
            )
            try:
                count = notifier.send_digest(
                    new_listings=result.new_listings,
                    changed_listings=result.changed_listings,
                    errors=result.errors,
                )
                LOGGER.info("Telegram messages sent: %s", count)
            except Exception as exc:
                LOGGER.exception("Telegram notification failed")
                result.errors["telegram"] = str(exc)
            finally:
                notifier.close()


def _status_counts(listings: Iterable[NormalizedListing]) -> dict:
    result = {
        MatchStatus.MATCH.value: 0,
        MatchStatus.REVIEW.value: 0,
        MatchStatus.INTERESTING.value: 0,
        MatchStatus.REJECT.value: 0,
        "total": 0,
    }
    for listing in listings:
        result[listing.status.value] += 1
        result["total"] += 1
    return result


def _unique(values: Iterable[NormalizedListing]) -> List[NormalizedListing]:
    result = {}
    for value in values:
        result[value.external_id] = value
    return list(result.values())


def _metric_delta(before: Dict[str, int], after: Dict[str, int]) -> Dict[str, int]:
    return {
        key: max(0, int(after.get(key, 0)) - int(before.get(key, 0)))
        for key in {**before, **after}
    }


def _mark_preferred(
    listings: Iterable[NormalizedListing],
    location_name: str,
) -> None:
    """Attach queue provenance without changing filtering or notification state."""
    for listing in listings:
        listing.raw_payload["preferred_location"] = location_name
        listing.raw_payload["search_queue"] = "preferred"
