from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import List, Optional

from dotenv import load_dotenv


def _as_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _as_urls(value: str) -> List[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass(frozen=True)
class SearchProfile:
    target_price_usd: float = 20_000
    base_budget_usd: float = 30_000
    max_price_usd: float = 40_000
    discovery_max_price_usd: float = 40_000
    min_area_sotok: float = 9
    target_area_sotok: float = 10
    max_area_sotok: float = 11
    discovery_min_area_sotok: float = 9
    discovery_max_area_sotok: float = 15
    preferred_distance_km: float = 30
    max_distance_km: float = 30
    discovery_max_distance_km: float = 30
    primary_electricity_kw: float = 20
    secondary_electricity_kw: float = 6


@dataclass(frozen=True)
class Settings:
    database_url: str
    dry_run: bool
    log_level: str
    http_timeout_seconds: float
    http_retries: int
    request_delay_seconds: float
    max_details_per_source: int
    domovita_max_search_pages: int
    kufar_max_search_pages: int
    kufar_detail_delay_seconds: float
    kufar_detail_batch_size: int
    kufar_detail_batch_pause_seconds: float
    kufar_rate_limit_pause_seconds: float
    realt_search_urls: List[str] = field(default_factory=list)
    realt_auction_urls: List[str] = field(default_factory=list)
    rlt_auction_urls: List[str] = field(default_factory=list)
    e_auction_urls: List[str] = field(default_factory=list)
    beltorgi_auction_urls: List[str] = field(default_factory=list)
    domovita_search_urls: List[str] = field(default_factory=list)
    kufar_search_urls: List[str] = field(default_factory=list)
    preferred_location_name: str = "Логойское направление"
    preferred_realt_search_urls: List[str] = field(default_factory=list)
    preferred_kufar_search_urls: List[str] = field(default_factory=list)
    preferred_max_details_per_source: int = 8
    activity_check_batch_size: int = 40
    activity_confirmation_count: int = 2
    activity_request_delay_seconds: float = 4
    premium_realt_search_urls: List[str] = field(default_factory=list)
    premium_kufar_search_urls: List[str] = field(default_factory=list)
    premium_min_price_usd: float = 80_000
    premium_catalog_refresh_hours: int = 12
    premium_observation_ttl_days: int = 45
    location_osm_endpoints: List[str] = field(default_factory=list)
    location_osm_batch_size: int = 12
    location_osm_refresh_days: int = 14
    beltorgi_max_pages: int = 2
    profile: SearchProfile = field(default_factory=SearchProfile)
    google_spreadsheet_id: Optional[str] = None
    google_service_account_json: Optional[str] = None
    google_plots_sheet: str = "Plots"
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    telegram_webhook_secret: Optional[str] = None
    widget_profile_id: Optional[str] = None
    widget_allowed_origins: List[str] = field(default_factory=lambda: ["*"])
    widget_phone_auth_mode: str = "demo"
    widget_auth_secret: str = "local-demo-secret"
    widget_sms_webhook_url: Optional[str] = None
    widget_sms_webhook_token: Optional[str] = None
    widget_lead_webhook_url: Optional[str] = None
    widget_lead_webhook_token: Optional[str] = None
    web_scheduler_enabled: bool = True
    admin_username: str = "admin"
    admin_password: Optional[str] = None
    admin_brand: str = "landplotfinder"
    viewer_username: str = "viewer"
    viewer_password: Optional[str] = None

    @property
    def sheets_enabled(self) -> bool:
        return bool(self.google_spreadsheet_id and self.google_service_account_json)

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    def google_credentials_info(self) -> Optional[dict]:
        if not self.google_service_account_json:
            return None
        return json.loads(self.google_service_account_json)


def load_settings(env_file: Optional[str] = None) -> Settings:
    load_dotenv(env_file)

    profile = SearchProfile(
        target_price_usd=float(os.getenv("TARGET_PRICE_USD", "20000")),
        base_budget_usd=float(os.getenv("BASE_BUDGET_USD", "30000")),
        max_price_usd=float(os.getenv("MAX_PRICE_USD", "40000")),
        discovery_max_price_usd=float(os.getenv("DISCOVERY_MAX_PRICE_USD", "40000")),
        min_area_sotok=float(os.getenv("MIN_AREA_SOTOK", "9")),
        target_area_sotok=float(os.getenv("TARGET_AREA_SOTOK", "10")),
        max_area_sotok=float(os.getenv("MAX_AREA_SOTOK", "11")),
        discovery_min_area_sotok=float(os.getenv("DISCOVERY_MIN_AREA_SOTOK", "9")),
        discovery_max_area_sotok=float(os.getenv("DISCOVERY_MAX_AREA_SOTOK", "15")),
        preferred_distance_km=float(os.getenv("PREFERRED_DISTANCE_KM", "30")),
        max_distance_km=float(os.getenv("MAX_DISTANCE_KM", "30")),
        discovery_max_distance_km=float(
            os.getenv("DISCOVERY_MAX_DISTANCE_KM", "30")
        ),
        primary_electricity_kw=float(os.getenv("PRIMARY_ELECTRICITY_KW", "20")),
        secondary_electricity_kw=float(os.getenv("SECONDARY_ELECTRICITY_KW", "6")),
    )

    return Settings(
        database_url=os.getenv("DATABASE_URL", "sqlite:///./landplots.db"),
        dry_run=_as_bool(os.getenv("DRY_RUN", "true")),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        http_timeout_seconds=float(os.getenv("HTTP_TIMEOUT_SECONDS", "30")),
        http_retries=int(os.getenv("HTTP_RETRIES", "3")),
        request_delay_seconds=float(os.getenv("REQUEST_DELAY_SECONDS", "3")),
        max_details_per_source=int(os.getenv("MAX_DETAILS_PER_SOURCE", "15")),
        domovita_max_search_pages=int(
            os.getenv("DOMOVITA_MAX_SEARCH_PAGES", "2")
        ),
        kufar_max_search_pages=int(os.getenv("KUFAR_MAX_SEARCH_PAGES", "3")),
        kufar_detail_delay_seconds=float(
            os.getenv("KUFAR_DETAIL_DELAY_SECONDS", "6")
        ),
        kufar_detail_batch_size=int(os.getenv("KUFAR_DETAIL_BATCH_SIZE", "8")),
        kufar_detail_batch_pause_seconds=float(
            os.getenv("KUFAR_DETAIL_BATCH_PAUSE_SECONDS", "45")
        ),
        kufar_rate_limit_pause_seconds=float(
            os.getenv("KUFAR_RATE_LIMIT_PAUSE_SECONDS", "60")
        ),
        realt_search_urls=_as_urls(
            os.getenv(
                "REALT_SEARCH_URLS",
                ",".join(
                    [
                        "https://realt.by/sale/plots/",
                        "https://realt.by/sale/cottages/"
                        "?addressV2=%5B%7B%22stateRegionUuid%22%3A"
                        "%22499f06b8-7b00-11eb-8943-0cc47adabd66%22%7D%5D"
                        "&priceTo=40000&priceType=840",
                        "https://realt.by/sale/dachi/"
                        "?addressV2=%5B%7B%22stateRegionUuid%22%3A"
                        "%22499f06b8-7b00-11eb-8943-0cc47adabd66%22%7D%5D"
                        "&priceTo=40000&priceType=840",
                    ]
                ),
            )
        ),
        realt_auction_urls=_as_urls(
            os.getenv(
                "REALT_AUCTION_URLS",
                "https://realt.by/auctions/"
                "zemelnie-ychastki-dlya-individyalnogo-stroitelstva/",
            )
        ),
        rlt_auction_urls=_as_urls(
            os.getenv(
                "RLT_AUCTION_URLS",
                "https://rlt.by/aukciony/,https://torgi.rlt.by/aukciony/",
            )
        ),
        e_auction_urls=_as_urls(
            os.getenv(
                "E_AUCTION_URLS",
                "https://e-auction.by/nedvizhimost/zemelnye_uchastki/",
            )
        ),
        beltorgi_auction_urls=_as_urls(
            os.getenv(
                "BELTORGI_AUCTION_URLS",
                "",
            )
        ),
        domovita_search_urls=_as_urls(
            os.getenv(
                "DOMOVITA_SEARCH_URLS",
                ",".join(
                    [
                        "https://domovita.by/minskiyi-rayion/area/sale",
                        "https://domovita.by/minskiyi-rayion/houses/sale",
                        "https://domovita.by/logoyiskiyi-rayion/area/sale",
                        "https://domovita.by/logoyiskiyi-rayion/houses/sale",
                    ]
                ),
            )
        ),
        kufar_search_urls=_as_urls(
            os.getenv(
                "KUFAR_SEARCH_URLS",
                ",".join(
                    [
                        "https://re.kufar.by/l/minsk/kupit/uchastok"
                        "?cur=USD&dr=r:0%2C30&prc=r:0%2C40000&saa=r:9%2C15",
                        "https://re.kufar.by/l/minskij-rajon/kupit/uchastok"
                        "?cur=USD&dr=r:0%2C30&prc=r:0%2C40000&saa=r:9%2C15",
                        "https://re.kufar.by/l/minskaya-oblast/kupit/uchastok"
                        "?cur=USD&dr=r:0%2C30&prc=r:0%2C40000&saa=r:9%2C15",
                        "https://re.kufar.by/l/minskaya-oblast/kupit/dom"
                        "?cur=USD&dr=r:0%2C30&prc=r:0%2C40000&saa=r:9%2C15",
                    ]
                ),
            )
        ),
        preferred_location_name=os.getenv(
            "PREFERRED_LOCATION_NAME",
            "Логойское направление",
        ).strip(),
        preferred_realt_search_urls=_as_urls(
            os.getenv(
                "PREFERRED_REALT_SEARCH_URLS",
                ",".join(
                    [
                        "https://realt.by/sale/plots/logojskij-rajon/",
                        "https://realt.by/sale/plots/borovljany/",
                        "https://realt.by/sale/plots/logojskij-ss/",
                        "https://realt.by/sale/plots/ostroshickij-ss/",
                        "https://realt.by/sale/plots/ostroshicko-gorodokskij-ss/",
                    ]
                ),
            )
        ),
        preferred_kufar_search_urls=_as_urls(
            os.getenv(
                "PREFERRED_KUFAR_SEARCH_URLS",
                ",".join(
                    [
                        "https://re.kufar.by/l/minskaya-oblast/kupit/uchastok"
                        "?cd=v:2&cur=USD&dr=r:0%2C30"
                        "&prc=r:0%2C40000&saa=r:9%2C15",
                        "https://re.kufar.by/l/minskaya-oblast/kupit/dom"
                        "?cd=v:2&cur=USD&dr=r:0%2C30"
                        "&prc=r:0%2C40000&saa=r:9%2C15",
                    ]
                ),
            )
        ),
        preferred_max_details_per_source=int(
            os.getenv("PREFERRED_MAX_DETAILS_PER_SOURCE", "8")
        ),
        activity_check_batch_size=int(
            os.getenv("ACTIVITY_CHECK_BATCH_SIZE", "40")
        ),
        activity_confirmation_count=int(
            os.getenv("ACTIVITY_CONFIRMATION_COUNT", "2")
        ),
        activity_request_delay_seconds=float(
            os.getenv("ACTIVITY_REQUEST_DELAY_SECONDS", "4")
        ),
        premium_realt_search_urls=_as_urls(
            os.getenv(
                "PREMIUM_REALT_SEARCH_URLS",
                "https://realt.by/sale/cottages/"
                "?addressV2=%5B%7B%22stateRegionUuid%22%3A"
                "%22499f06b8-7b00-11eb-8943-0cc47adabd66%22%7D%5D"
                "&priceFrom=80000&priceTo=500000&priceType=840",
            )
        ),
        premium_kufar_search_urls=_as_urls(
            os.getenv(
                "PREMIUM_KUFAR_SEARCH_URLS",
                "https://re.kufar.by/l/minskaya-oblast/kupit/dom"
                "?cur=USD&prc=r:80000%2C500000",
            )
        ),
        premium_min_price_usd=float(
            os.getenv("PREMIUM_MIN_PRICE_USD", "80000")
        ),
        premium_catalog_refresh_hours=int(
            os.getenv("PREMIUM_CATALOG_REFRESH_HOURS", "12")
        ),
        premium_observation_ttl_days=int(
            os.getenv("PREMIUM_OBSERVATION_TTL_DAYS", "45")
        ),
        location_osm_endpoints=_as_urls(
            os.getenv(
                "LOCATION_OSM_ENDPOINTS",
                "https://maps.mail.ru/osm/tools/overpass/api/interpreter,"
                "https://overpass-api.de/api/interpreter,"
                "https://overpass.private.coffee/api/interpreter",
            )
        ),
        location_osm_batch_size=int(
            os.getenv("LOCATION_OSM_BATCH_SIZE", "12")
        ),
        location_osm_refresh_days=int(
            os.getenv("LOCATION_OSM_REFRESH_DAYS", "14")
        ),
        beltorgi_max_pages=int(os.getenv("BELTORGI_MAX_PAGES", "2")),
        profile=profile,
        google_spreadsheet_id=os.getenv("GOOGLE_SPREADSHEET_ID") or None,
        google_service_account_json=os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") or None,
        google_plots_sheet=os.getenv("GOOGLE_PLOTS_SHEET", "Plots"),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN") or None,
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID") or None,
        telegram_webhook_secret=os.getenv("TELEGRAM_WEBHOOK_SECRET") or None,
        widget_profile_id=os.getenv("WIDGET_PROFILE_ID") or None,
        widget_allowed_origins=_as_urls(os.getenv("WIDGET_ALLOWED_ORIGINS", "*"))
        or ["*"],
        widget_phone_auth_mode=os.getenv("WIDGET_PHONE_AUTH_MODE", "demo").strip().lower(),
        widget_auth_secret=os.getenv("WIDGET_AUTH_SECRET", "local-demo-secret"),
        widget_sms_webhook_url=os.getenv("WIDGET_SMS_WEBHOOK_URL") or None,
        widget_sms_webhook_token=os.getenv("WIDGET_SMS_WEBHOOK_TOKEN") or None,
        widget_lead_webhook_url=os.getenv("WIDGET_LEAD_WEBHOOK_URL") or None,
        widget_lead_webhook_token=os.getenv("WIDGET_LEAD_WEBHOOK_TOKEN") or None,
        web_scheduler_enabled=_as_bool(
            os.getenv("WEB_SCHEDULER_ENABLED", "true")
        ),
        admin_username=os.getenv("ADMIN_USERNAME", "admin").strip() or "admin",
        admin_password=os.getenv("ADMIN_PASSWORD") or None,
        admin_brand=os.getenv("ADMIN_BRAND", "landplotfinder").strip().lower(),
        viewer_username=os.getenv("VIEWER_USERNAME", "viewer").strip() or "viewer",
        viewer_password=os.getenv("VIEWER_PASSWORD") or None,
    )
