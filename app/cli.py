from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from typing import List, Optional

from app.activity import ListingActivityChecker
from app.catalog import GoogleCatalogReader
from app.config import load_settings
from app.domain import MatchStatus, NormalizedListing
from app.integrations.sheet_cleanup import GooglePlotsCleaner
from app.integrations.telegram import TelegramNotifier
from app.integrations.trips import GoogleTripsSink
from app.location_service import LocationEnricher
from app.orchestrator import Scanner


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="land-plot-finder")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser("scan", help="Scan configured listing sources")
    mode = scan.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Never persist or notify")
    mode.add_argument("--persist", action="store_true", help="Persist and run integrations")
    scan.add_argument(
        "--source",
        action="append",
        choices=[
            "realt",
            "realt_auction",
            "rlt_auction",
            "e_auction",
            "kufar",
        ],
        help="Limit scan to one source; repeat for both",
    )
    scan.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable result",
    )
    scan.add_argument(
        "--preferred",
        action="store_true",
        help="Run only the configured preferred-location queue",
    )
    scan.add_argument(
        "--show",
        type=int,
        default=10,
        help="Maximum non-rejected listings to print",
    )

    subparsers.add_parser("check-config", help="Show enabled components without secrets")
    subparsers.add_parser(
        "enrich-locations",
        help="Rebuild location profiles from accumulated listings",
    )
    subparsers.add_parser(
        "update-trips",
        help="Build Google Maps trip links from selected spreadsheet rows",
    )
    subparsers.add_parser(
        "check-activity",
        help="Verify that collected Realt and Kufar listings are still available",
    )
    cleanup = subparsers.add_parser(
        "clean-sheet",
        help="Preview or remove auto rows outside hard numeric limits",
    )
    cleanup.add_argument(
        "--apply",
        action="store_true",
        help="Delete unprotected rows; without this flag only preview",
    )
    catalog = subparsers.add_parser(
        "send-catalog",
        help="Send top unselected spreadsheet candidates to Telegram",
    )
    catalog.add_argument("--min-score", type=float, default=80)
    catalog.add_argument("--limit", type=int, default=48)
    subparsers.add_parser(
        "scheduled",
        help="Run the scan or location enrichment for the current cron slot",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.command == "check-config":
        print(
            json.dumps(
                {
                    "database_url": _redact_database_url(settings.database_url),
                    "dry_run": settings.dry_run,
                    "realt_search_urls": settings.realt_search_urls,
                    "realt_auction_urls": settings.realt_auction_urls,
                    "rlt_auction_urls": settings.rlt_auction_urls,
                    "e_auction_urls": settings.e_auction_urls,
                    "beltorgi_auction_urls": settings.beltorgi_auction_urls,
                    "kufar_search_urls": settings.kufar_search_urls,
                    "preferred_location_name": settings.preferred_location_name,
                    "preferred_realt_search_urls": settings.preferred_realt_search_urls,
                    "preferred_kufar_search_urls": settings.preferred_kufar_search_urls,
                    "premium_realt_search_urls": settings.premium_realt_search_urls,
                    "premium_kufar_search_urls": settings.premium_kufar_search_urls,
                    "location_osm_batch_size": settings.location_osm_batch_size,
                    "sheets_enabled": settings.sheets_enabled,
                    "telegram_enabled": settings.telegram_enabled,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.command == "enrich-locations":
        stats = LocationEnricher(settings).run()
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        return 0

    if args.command == "update-trips":
        if not settings.sheets_enabled:
            raise RuntimeError("Google Sheets is not configured")
        stats = GoogleTripsSink(
            spreadsheet_id=settings.google_spreadsheet_id or "",
            credentials_info=settings.google_credentials_info() or {},
        ).sync()
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        return 0

    if args.command == "check-activity":
        stats = ListingActivityChecker(settings).run()
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        return 0

    if args.command == "clean-sheet":
        if not settings.sheets_enabled:
            raise RuntimeError("Google Sheets is not configured")
        cleaner = GooglePlotsCleaner(
            spreadsheet_id=settings.google_spreadsheet_id or "",
            sheet_name=settings.google_plots_sheet,
            credentials_info=settings.google_credentials_info() or {},
        )
        stats = cleaner.apply() if args.apply else cleaner.preview()
        if "candidates" in stats:
            stats = {
                **stats,
                "candidates": [
                    {
                        "row": item.row_number,
                        "id": item.external_id,
                        "status": item.status,
                        "price_usd": item.price_usd,
                        "area_sotok": item.area_sotok,
                        "distance_mkad_km": item.distance_mkad_km,
                        "reasons": item.reasons,
                    }
                    for item in stats["candidates"][:20]
                ],
            }
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        return 0

    if args.command == "send-catalog":
        if not settings.sheets_enabled or not settings.telegram_enabled:
            raise RuntimeError("Google Sheets and Telegram must be configured")
        listings = GoogleCatalogReader(
            spreadsheet_id=settings.google_spreadsheet_id or "",
            credentials_info=settings.google_credentials_info() or {},
        ).top_candidates(min_score=args.min_score, limit=args.limit)
        notifier = TelegramNotifier(
            bot_token=settings.telegram_bot_token or "",
            chat_id=settings.telegram_chat_id or "",
        )
        try:
            sent = notifier.send_catalog(listings)
        finally:
            notifier.close()
        print(json.dumps({"selected": len(listings), "sent": sent}, ensure_ascii=False))
        return 0

    if args.command == "scheduled":
        job = _scheduled_job(datetime.now(timezone.utc).hour)
        if job == "activity":
            stats = ListingActivityChecker(settings).run()
            print(json.dumps(stats, ensure_ascii=False, indent=2))
            return 0
        if job == "locations":
            stats = LocationEnricher(settings).run()
            print(json.dumps(stats, ensure_ascii=False, indent=2))
            return 0
        if job is None:
            print("No job is assigned to this UTC hour")
            return 0
        result = Scanner(settings).run(
            dry_run=settings.dry_run,
            queue="preferred" if job == "preferred_scan" else "standard",
        )
        _print_summary(result.source_stats, result.errors, result.listings, 10)
        return 1 if result.errors and not result.listings else 0

    dry_run = True if args.dry_run else False if args.persist else settings.dry_run
    result = Scanner(settings).run(
        dry_run=dry_run,
        enabled_sources=args.source,
        queue="preferred" if args.preferred else "standard",
    )

    if args.json:
        print(
            json.dumps(
                {
                    "started_at": result.started_at.isoformat(),
                    "completed_at": (
                        result.completed_at.isoformat() if result.completed_at else None
                    ),
                    "source_stats": result.source_stats,
                    "errors": result.errors,
                    "listings": [listing.serializable() for listing in result.listings],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        _print_summary(result.source_stats, result.errors, result.listings, args.show)
    return 1 if result.errors and not result.listings else 0


def _print_summary(
    source_stats: dict,
    errors: dict,
    listings: List[NormalizedListing],
    show: int,
) -> None:
    print("\nLandPlotFinder scan")
    for source, stats in source_stats.items():
        print(f"- {source}: {stats}")
    if errors:
        print(f"- errors: {errors}")

    selected = sorted(
        (item for item in listings if item.status is not MatchStatus.REJECT),
        key=lambda item: item.score,
        reverse=True,
    )[:show]
    print(f"\nCandidates shown: {len(selected)}")
    for item in selected:
        price = f"${item.price_usd:,.0f}" if item.price_usd is not None else "price ?"
        area = f"{item.area_sotok:g} sotok" if item.area_sotok is not None else "area ?"
        distance = (
            f"{item.distance_mkad_km:g} km"
            if item.distance_mkad_km is not None
            else "distance ?"
        )
        print(
            f"[{item.status.value:11}] {item.score:3}/100 "
            f"{price:>10} | {area:>10} | {distance:>10} | {item.title}"
        )
        if item.reasons:
            print(f"  reason: {item.reasons[0]}")
        print(f"  {item.canonical_url}")


def _redact_database_url(value: str) -> str:
    if "@" not in value:
        return value
    prefix, suffix = value.rsplit("@", 1)
    scheme = prefix.split(":", 1)[0]
    return f"{scheme}://***@{suffix}"


def _scheduled_job(utc_hour: int) -> Optional[str]:
    if utc_hour in {0, 12}:
        return "activity"
    if utc_hour in {2, 8, 14}:
        return "locations"
    if utc_hour in {5, 11, 17}:
        return "scan"
    if utc_hour in {6, 18}:
        return "preferred_scan"
    return None


if __name__ == "__main__":
    sys.exit(main())
