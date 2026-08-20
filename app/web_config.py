from __future__ import annotations

from dataclasses import replace
from typing import Any, Dict, Iterable, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.config import SearchProfile, Settings, load_settings
from app.db import init_db, make_engine, make_session_factory, session_scope
from app.models import ServiceStateModel

CONFIG_KEY = "web_config"
DEFAULT_WEB_CONFIG: Dict[str, Any] = {
    "configured": False,
    "schedule_enabled": True,
    "schedule_interval_hours": 6,
    "activity_check_enabled": True,
    "sources": ["realt", "kufar"],
    "target_price_usd": 20_000,
    "max_price_usd": 40_000,
    "min_area_sotok": 9,
    "max_area_sotok": 15,
    "max_distance_km": 30,
    "primary_electricity_kw": 20,
    "secondary_electricity_kw": 6,
    "telegram_enabled": False,
    "telegram_bot_token": "",
    "telegram_chat_id": "",
}
ALLOWED_SOURCES = {
    "realt",
    "kufar",
    "realt_auction",
    "rlt_auction",
    "e_auction",
}


def load_web_config(database_url: str) -> Dict[str, Any]:
    engine = make_engine(database_url)
    init_db(engine)
    factory = make_session_factory(engine)
    try:
        with session_scope(factory) as session:
            state = session.get(ServiceStateModel, CONFIG_KEY)
            stored = dict(state.payload or {}) if state else {}
        return normalize_web_config({**DEFAULT_WEB_CONFIG, **stored})
    finally:
        engine.dispose()


def save_web_config(database_url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    value = normalize_web_config({**load_web_config(database_url), **payload})
    value["configured"] = True
    engine = make_engine(database_url)
    init_db(engine)
    factory = make_session_factory(engine)
    try:
        with session_scope(factory) as session:
            state = session.get(ServiceStateModel, CONFIG_KEY)
            if state is None:
                session.add(ServiceStateModel(key=CONFIG_KEY, payload=value))
            else:
                state.payload = value
        return value
    finally:
        engine.dispose()


def runtime_settings(
    config: Dict[str, Any],
    base: Optional[Settings] = None,
) -> Settings:
    base = base or load_settings()
    profile: SearchProfile = replace(
        base.profile,
        target_price_usd=float(config["target_price_usd"]),
        base_budget_usd=float(config["max_price_usd"]),
        max_price_usd=float(config["max_price_usd"]),
        discovery_max_price_usd=float(config["max_price_usd"]),
        min_area_sotok=float(config["min_area_sotok"]),
        target_area_sotok=float(config["min_area_sotok"]),
        max_area_sotok=float(config["max_area_sotok"]),
        discovery_min_area_sotok=float(config["min_area_sotok"]),
        discovery_max_area_sotok=float(config["max_area_sotok"]),
        preferred_distance_km=float(config["max_distance_km"]),
        max_distance_km=float(config["max_distance_km"]),
        discovery_max_distance_km=float(config["max_distance_km"]),
        primary_electricity_kw=float(config["primary_electricity_kw"]),
        secondary_electricity_kw=float(config["secondary_electricity_kw"]),
    )
    telegram_enabled = bool(config.get("telegram_enabled"))
    max_price = float(config["max_price_usd"])
    min_area = float(config["min_area_sotok"])
    max_area = float(config["max_area_sotok"])
    max_distance = float(config["max_distance_km"])
    return replace(
        base,
        dry_run=False,
        profile=profile,
        google_spreadsheet_id=None,
        google_service_account_json=None,
        realt_search_urls=_tune_realt_urls(base.realt_search_urls, max_price),
        preferred_realt_search_urls=_tune_realt_urls(
            base.preferred_realt_search_urls, max_price
        ),
        kufar_search_urls=_tune_kufar_urls(
            base.kufar_search_urls,
            max_price,
            min_area,
            max_area,
            max_distance,
        ),
        preferred_kufar_search_urls=_tune_kufar_urls(
            base.preferred_kufar_search_urls,
            max_price,
            min_area,
            max_area,
            max_distance,
        ),
        telegram_bot_token=(
            str(config.get("telegram_bot_token") or "") or None
            if telegram_enabled
            else None
        ),
        telegram_chat_id=(
            str(config.get("telegram_chat_id") or "") or None
            if telegram_enabled
            else None
        ),
    )


def normalize_web_config(payload: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(DEFAULT_WEB_CONFIG)
    result.update(payload)
    result["configured"] = bool(result.get("configured"))
    result["schedule_enabled"] = bool(result.get("schedule_enabled"))
    result["activity_check_enabled"] = bool(result.get("activity_check_enabled"))
    result["telegram_enabled"] = bool(result.get("telegram_enabled"))
    result["schedule_interval_hours"] = _bounded_int(
        result.get("schedule_interval_hours"), 1, 168, 6
    )
    result["target_price_usd"] = _bounded_float(
        result.get("target_price_usd"), 0, 10_000_000, 20_000
    )
    result["max_price_usd"] = _bounded_float(
        result.get("max_price_usd"), 1, 10_000_000, 40_000
    )
    result["min_area_sotok"] = _bounded_float(
        result.get("min_area_sotok"), 0.1, 10_000, 9
    )
    result["max_area_sotok"] = _bounded_float(
        result.get("max_area_sotok"), result["min_area_sotok"], 10_000, 15
    )
    result["max_distance_km"] = _bounded_float(
        result.get("max_distance_km"), 0, 1_000, 30
    )
    result["primary_electricity_kw"] = _bounded_float(
        result.get("primary_electricity_kw"), 0, 1_000, 20
    )
    result["secondary_electricity_kw"] = _bounded_float(
        result.get("secondary_electricity_kw"), 0, 1_000, 6
    )
    result["sources"] = _sources(result.get("sources"))
    result["telegram_bot_token"] = str(result.get("telegram_bot_token") or "").strip()
    result["telegram_chat_id"] = str(result.get("telegram_chat_id") or "").strip()
    return result


def public_web_config(config: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(config)
    token = str(result.get("telegram_bot_token") or "")
    result["telegram_bot_token"] = "••••••••" if token else ""
    result["telegram_configured"] = bool(token and result.get("telegram_chat_id"))
    return result


def _sources(value: Any) -> list[str]:
    values: Iterable[Any] = value if isinstance(value, list) else []
    result = [str(item) for item in values if str(item) in ALLOWED_SOURCES]
    return result or ["realt", "kufar"]


def _bounded_float(value: Any, minimum: float, maximum: float, fallback: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return min(max(number, minimum), maximum)


def _bounded_int(value: Any, minimum: int, maximum: int, fallback: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return min(max(number, minimum), maximum)


def _tune_kufar_urls(
    urls: Iterable[str],
    max_price: float,
    min_area: float,
    max_area: float,
    max_distance: float,
) -> list[str]:
    result = []
    for url in urls:
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query.update(
            {
                "cur": "USD",
                "prc": f"r:0,{max_price:g}",
                "saa": f"r:{min_area:g},{max_area:g}",
                "dr": f"r:0,{max_distance:g}",
            }
        )
        result.append(
            urlunsplit(
                (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
            )
        )
    return result


def _tune_realt_urls(urls: Iterable[str], max_price: float) -> list[str]:
    result = []
    for url in urls:
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        if query or "sale/plots" not in parts.path:
            query["priceTo"] = f"{max_price:g}"
            query["priceType"] = "840"
        result.append(
            urlunsplit(
                (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
            )
        )
    return result
