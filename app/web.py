from __future__ import annotations

import copy
import csv
import hashlib
import hmac
import io
import logging
import math
import os
import secrets
import time
from base64 import b64decode, urlsafe_b64decode, urlsafe_b64encode
from binascii import Error as BinasciiError
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional
from urllib.parse import quote

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app import __version__
from app.backups import (
    backup_filename,
    create_backup,
    database_stats,
    restore_backup,
)
from app.client_telegram import create_invite
from app.config import Settings, load_settings
from app.db import init_db, make_engine, make_session_factory, session_scope
from app.integrations.trips import (
    TripPoint,
    build_trip_routes,
    map_provider_label,
    maps_pin_url,
    maps_route_url,
)
from app.jobs import JobCoordinator
from app.models import (
    ListingActivityModel,
    ListingDecisionModel,
    ListingEventModel,
    ListingModel,
    ListingProfileModel,
    ListingSnapshotModel,
    LocationProfileModel,
    ScanRunModel,
    WidgetInterestModel,
    WidgetLeadModel,
    WidgetSearchContextModel,
    WidgetTelegramSubscriptionModel,
)
from app.source_health import health_payloads
from app.web_config import (
    delete_search_profile,
    get_profile,
    load_web_config,
    public_web_config,
    save_search_profile,
    save_web_config,
)
from app.widget_limits import consume, prune_expired, visitor_ip
from app.widget_service import (
    WidgetError,
    normalize_phone,
    notify_lead,
    record_interest,
    request_code,
    save_search_context,
    verified_lead,
    verify_code,
)

LOGGER = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).with_name("static")
DECISION_STATES = {"new", "liked", "studying", "trip", "rejected"}
ADMIN_SESSION_COOKIE = "lpf_admin_session"
ADMIN_SESSION_TTL_SECONDS = 7 * 24 * 60 * 60
AUTH_ROLES = {"admin", "viewer"}


class DecisionPayload(BaseModel):
    state: str
    note: str = Field(default="", max_length=4000)


class SettingsPayload(BaseModel):
    configured: Optional[bool] = None
    schedule_enabled: bool = True
    schedule_interval_hours: int = Field(default=6, ge=1, le=168)
    activity_check_enabled: bool = True
    map_provider: Literal["google", "yandex"] = "google"
    sources: List[str]
    target_price_usd: float = Field(default=20_000, ge=0)
    max_price_usd: float = Field(default=40_000, gt=0)
    min_area_sotok: float = Field(default=9, gt=0)
    max_area_sotok: float = Field(default=15, gt=0)
    max_distance_km: float = Field(default=30, ge=0)
    primary_electricity_kw: float = Field(default=20, ge=0)
    secondary_electricity_kw: float = Field(default=6, ge=0)
    telegram_enabled: bool = False
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""


class SearchProfilePayload(BaseModel):
    name: str = Field(default="Новый поиск", min_length=1, max_length=80)
    enabled: bool = True
    schedule_enabled: bool = True
    schedule_interval_hours: int = Field(default=6, ge=1, le=168)
    sources: List[str] = Field(default_factory=lambda: ["realt", "kufar", "domovita"])
    target_price_usd: float = Field(default=20_000, ge=0)
    max_price_usd: float = Field(default=40_000, gt=0)
    min_area_sotok: float = Field(default=9, gt=0)
    max_area_sotok: float = Field(default=15, gt=0)
    max_distance_km: float = Field(default=30, ge=0)
    primary_electricity_kw: float = Field(default=20, ge=0)
    secondary_electricity_kw: float = Field(default=6, ge=0)


class TripPlanPayload(BaseModel):
    listing_ids: List[int] = Field(default_factory=list, max_length=100)
    start_latitude: float = Field(default=53.9006, ge=-90, le=90)
    start_longitude: float = Field(default=27.5590, ge=-180, le=180)
    max_points_per_route: int = Field(default=4, ge=2, le=9)


class WidgetCodeRequest(BaseModel):
    phone: str = Field(min_length=8, max_length=32)
    consent: bool = False
    source_page: str = Field(default="", max_length=2_000)
    utm: Dict[str, Any] = Field(default_factory=dict)


class WidgetCodeVerify(BaseModel):
    phone: str = Field(min_length=8, max_length=32)
    code: str = Field(min_length=4, max_length=12)


class WidgetInterestPayload(BaseModel):
    reference: str = Field(min_length=13, max_length=13)
    search_params: Dict[str, Any] = Field(default_factory=dict)
    source_page: str = Field(default="", max_length=2_000)


class WidgetStatusPayload(BaseModel):
    references: List[str] = Field(min_length=1, max_length=50)


class AdminLoginPayload(BaseModel):
    username: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=1_000)


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    base_settings = settings or load_settings()
    _ensure_sqlite_parent(base_settings.database_url)
    engine = make_engine(base_settings.database_url)
    init_db(engine)
    engine.dispose()
    coordinator = JobCoordinator(base_settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        coordinator.start()
        yield
        coordinator.shutdown()

    app = FastAPI(
        title="LandPlotFinder",
        description="Локальная панель поиска и отбора земельных участков",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.settings = base_settings
    app.state.jobs = coordinator
    app.add_middleware(
        CORSMiddleware,
        allow_origins=base_settings.widget_allowed_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

    @app.middleware("http")
    async def protect_admin(request: Request, call_next):
        auth_enabled = bool(base_settings.admin_password or base_settings.viewer_password)
        if not auth_enabled or _is_public_widget_path(request.url.path):
            request.state.auth_role = "admin"
            return await call_next(request)
        credentials = _basic_credentials(request.headers.get("Authorization"))
        role = _credentials_role(credentials, base_settings) or _session_role(
            request.cookies.get(ADMIN_SESSION_COOKIE),
            base_settings,
        )
        if role:
            request.state.auth_role = role
            if role == "viewer" and not _viewer_request_allowed(request):
                return JSONResponse(
                    {"detail": "Демо-доступ работает только в режиме просмотра"},
                    status_code=403,
                )
            return await call_next(request)
        if request.url.path.startswith("/api/"):
            return JSONResponse(
                {"detail": "Требуется вход администратора"},
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="LandPlotFinder"'},
            )
        next_path = request.url.path
        if request.url.query:
            next_path = f"{next_path}?{request.url.query}"
        return RedirectResponse(url=f"/login?next={quote(next_path, safe='')}", status_code=303)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/health")
    def health() -> Dict[str, Any]:
        return {"ok": True, "service": "land-plot-finder"}

    @app.post("/api/auth/login")
    def admin_login(payload: AdminLoginPayload, request: Request) -> JSONResponse:
        role = _credentials_role((payload.username, payload.password), base_settings)
        if not role:
            raise HTTPException(status_code=401, detail="Неверный логин или пароль")
        response = JSONResponse({"ok": True, "role": role, "read_only": role == "viewer"})
        response.set_cookie(
            ADMIN_SESSION_COOKIE,
            _admin_session_token(base_settings, role, payload.username),
            max_age=ADMIN_SESSION_TTL_SECONDS,
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="lax",
            path="/",
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/auth/me")
    def auth_me(request: Request) -> Dict[str, Any]:
        role = _request_role(request)
        return {"role": role, "read_only": role == "viewer"}

    @app.post("/api/auth/logout")
    def admin_logout() -> JSONResponse:
        response = JSONResponse({"ok": True})
        response.delete_cookie(ADMIN_SESSION_COOKIE, path="/")
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/settings")
    def get_settings(request: Request) -> Dict[str, Any]:
        value = public_web_config(load_web_config(base_settings.database_url))
        return _viewer_web_config(value) if _is_viewer(request) else value

    @app.put("/api/settings")
    def put_settings(payload: SettingsPayload) -> Dict[str, Any]:
        incoming = payload.model_dump(exclude_none=True)
        current = load_web_config(base_settings.database_url)
        if incoming.get("telegram_bot_token") == "••••••••":
            incoming["telegram_bot_token"] = current.get("telegram_bot_token", "")
        saved = save_web_config(base_settings.database_url, incoming)
        coordinator.configure()
        return public_web_config(saved)

    @app.post("/api/public/widget/auth/request-code")
    def widget_request_code(request: Request, payload: WidgetCodeRequest) -> Dict[str, Any]:
        try:
            if not payload.consent:
                raise WidgetError("Нужно согласие на обработку контактных данных", 422)
            normalize_phone(payload.phone)
            with _database(base_settings) as session:
                prune_expired(session)
                ip = visitor_ip(request)
                consume(session, base_settings, "otp-ip-hour", ip, 10, 3600)
                consume(session, base_settings, "otp-ip-day", ip, 30, 86400)
                consume(
                    session,
                    base_settings,
                    "otp-global-day",
                    "all",
                    base_settings.widget_codes_per_day,
                    86400,
                    message="Лимит отправки кодов на сегодня исчерпан. Попробуйте завтра.",
                )
                return request_code(
                    session,
                    base_settings,
                    payload.phone,
                    payload.consent,
                    payload.source_page,
                    payload.utm,
                )
        except WidgetError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    @app.post("/api/public/widget/auth/verify-code")
    def widget_verify_code(request: Request, payload: WidgetCodeVerify) -> Dict[str, Any]:
        try:
            with _database(base_settings) as session:
                consume(
                    session,
                    base_settings,
                    "verify-ip-hour",
                    visitor_ip(request),
                    30,
                    3600,
                )
                try:
                    result = verify_code(
                        session,
                        base_settings,
                        payload.phone,
                        payload.code,
                    )
                except WidgetError as exc:
                    if exc.status_code == 401:
                        # Failed guesses must count even though the response is an error.
                        session.commit()
                    raise
                first_verified = result.pop("first_verified")
                lead = verified_lead(session, result["token"])
                notification = {
                    "event": "lead_verified",
                    "lead_id": lead.id,
                    "phone": lead.phone,
                    "source_page": lead.source_page,
                    "utm": lead.utm,
                }
        except WidgetError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        if first_verified:
            notify_lead(base_settings, notification)
        return result

    @app.get("/api/public/widget/preview")
    def public_widget_preview(
        request: Request,
        q: str = Query(default="", max_length=120),
        max_price_usd: Optional[float] = Query(default=None, gt=0, le=10_000_000),
        min_area_sotok: Optional[float] = Query(default=None, gt=0, le=10_000),
        max_area_sotok: Optional[float] = Query(default=None, gt=0, le=10_000),
        max_distance_km: Optional[float] = Query(default=None, ge=0, le=1_000),
        electricity: bool = False,
        gas: bool = False,
        water: bool = False,
        sewerage: bool = False,
        profile_id: Optional[str] = Query(default=None, max_length=64),
    ) -> JSONResponse:
        _validate_widget_area(min_area_sotok, max_area_sotok)
        config = load_web_config(base_settings.database_url)
        selected_profile_id = _selected_profile_id(
            config,
            profile_id or base_settings.widget_profile_id,
        )
        statement = _widget_listing_statement(
            selected_profile_id,
            q,
            max_price_usd,
            min_area_sotok,
            max_area_sotok,
            max_distance_km,
            electricity,
            gas,
            water,
            sewerage,
        )
        with _database(base_settings) as session:
            try:
                consume(
                    session,
                    base_settings,
                    "preview-ip-hour",
                    visitor_ip(request),
                    120,
                    3600,
                )
            except WidgetError as exc:
                raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
            total = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
        return JSONResponse(
            {"total": total, "preview_cards": min(total, 3)},
            headers={"Cache-Control": "public, max-age=60"},
        )

    @app.get("/api/public/widget/listings")
    def public_widget_listings(
        request: Request,
        q: str = Query(default="", max_length=120),
        max_price_usd: Optional[float] = Query(default=None, gt=0, le=10_000_000),
        min_area_sotok: Optional[float] = Query(default=None, gt=0, le=10_000),
        max_area_sotok: Optional[float] = Query(default=None, gt=0, le=10_000),
        max_distance_km: Optional[float] = Query(default=None, ge=0, le=1_000),
        electricity: bool = False,
        gas: bool = False,
        water: bool = False,
        sewerage: bool = False,
        limit: int = Query(default=9, ge=1, le=24),
        offset: int = Query(default=0, ge=0, le=10_000),
        sort: Literal["match", "price", "distance", "newest"] = "match",
        profile_id: Optional[str] = Query(default=None, max_length=64),
    ) -> JSONResponse:
        _validate_widget_area(min_area_sotok, max_area_sotok)
        config = load_web_config(base_settings.database_url)
        selected_profile_id = _selected_profile_id(
            config,
            profile_id or base_settings.widget_profile_id,
        )
        profile = get_profile(config, selected_profile_id)
        try:
            with _database(base_settings) as session:
                lead = verified_lead(session, _bearer_token(request))
                save_search_context(
                    session,
                    lead,
                    {
                        key: value
                        for key, value in {
                            "q": q,
                            "max_price_usd": max_price_usd,
                            "min_area_sotok": min_area_sotok,
                            "max_area_sotok": max_area_sotok,
                            "max_distance_km": max_distance_km,
                            "electricity": "true" if electricity else None,
                            "gas": "true" if gas else None,
                            "water": "true" if water else None,
                            "sewerage": "true" if sewerage else None,
                            "profile_id": selected_profile_id,
                        }.items()
                        if value is not None and value != ""
                    },
                )
                statement = _widget_listing_statement(
                    selected_profile_id,
                    q,
                    max_price_usd,
                    min_area_sotok,
                    max_area_sotok,
                    max_distance_km,
                    electricity,
                    gas,
                    water,
                    sewerage,
                )
                rows = session.execute(statement).all()
                page_count = min(limit, max(0, len(rows) - offset))
                consume(
                    session,
                    base_settings,
                    "catalog-lead-day",
                    str(lead.id),
                    base_settings.widget_catalog_cards_per_day,
                    86400,
                    cost=page_count,
                    message="Лимит просмотра участков на сегодня достигнут. Продолжите завтра.",
                )
                consume(
                    session,
                    base_settings,
                    "catalog-ip-day",
                    visitor_ip(request),
                    360,
                    86400,
                    cost=page_count,
                    message="С этого подключения сегодня просмотрено слишком много вариантов.",
                )
                location_keys = {
                    str((listing.raw_payload or {}).get("location_key"))
                    for listing, _ in rows
                    if (listing.raw_payload or {}).get("location_key")
                }
                locations = (
                    {
                        item.key: item
                        for item in session.scalars(
                            select(LocationProfileModel).where(
                                LocationProfileModel.key.in_(location_keys)
                            )
                        )
                    }
                    if location_keys
                    else {}
                )
                interested_ids = session.scalars(
                    select(WidgetInterestModel.listing_id).where(
                        WidgetInterestModel.lead_id == lead.id
                    )
                ).all()
        except WidgetError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

        items = [
            _public_widget_listing_json(
                listing,
                locations.get(str((listing.raw_payload or {}).get("location_key"))),
                base_settings.widget_auth_secret,
                max_price_usd=max_price_usd,
                min_area_sotok=min_area_sotok,
                max_area_sotok=max_area_sotok,
                max_distance_km=max_distance_km,
                electricity_required=electricity,
                gas_required=gas,
                water_required=water,
                sewerage_required=sewerage,
            )
            for listing, _ in rows
        ]
        items.sort(
            key=lambda item: _widget_sort_key(item, sort),
            reverse=sort == "newest",
        )
        total = len(items)
        return JSONResponse(
            {
                "profile": {"id": selected_profile_id, "name": profile["name"]},
                "total": total,
                "offset": offset,
                "limit": limit,
                "has_more": offset + limit < total,
                "items": items[offset : offset + limit],
                "interested_references": [
                    _public_listing_ref(base_settings.widget_auth_secret, listing_id)
                    for listing_id in interested_ids
                ],
            },
            headers={"Cache-Control": "private, no-store"},
        )

    @app.get("/api/public/widget/options")
    def public_widget_options(
        profile_id: Optional[str] = Query(default=None, max_length=64),
    ) -> JSONResponse:
        config = load_web_config(base_settings.database_url)
        selected_profile_id = _selected_profile_id(
            config,
            profile_id or base_settings.widget_profile_id,
        )
        with _database(base_settings) as session:
            directions = session.scalars(
                select(ListingModel.direction)
                .join(
                    ListingProfileModel,
                    and_(
                        ListingProfileModel.listing_id == ListingModel.id,
                        ListingProfileModel.profile_id == selected_profile_id,
                    ),
                )
                .where(
                    ListingModel.active.is_(True),
                    ListingModel.direction.is_not(None),
                    ListingModel.direction != "",
                )
                .distinct()
                .order_by(ListingModel.direction)
            ).all()
        cleaned = sorted(
            {" ".join(str(value).split()) for value in directions if str(value).strip()},
            key=str.casefold,
        )
        return JSONResponse(
            {
                "directions": cleaned,
                "telegram_subscriptions_available": bool(base_settings.widget_client_bot_username),
            },
            headers={"Cache-Control": "public, max-age=300"},
        )

    @app.post("/api/public/widget/telegram/invite")
    def widget_telegram_invite(request: Request) -> Dict[str, str]:
        if not base_settings.widget_client_bot_username:
            raise HTTPException(status_code=503, detail="Подписка в Telegram пока недоступна")
        try:
            with _database(base_settings) as session:
                lead = verified_lead(session, _bearer_token(request))
                context = session.scalar(
                    select(WidgetSearchContextModel).where(
                        WidgetSearchContextModel.lead_id == lead.id
                    )
                )
                if context is None or not context.criteria:
                    raise WidgetError("Сначала выполните поиск участков", 409)
                consume(
                    session,
                    base_settings,
                    "invite-lead-day",
                    str(lead.id),
                    3,
                    86400,
                    message="Сегодня уже создано три ссылки на бота. Попробуйте завтра.",
                )
                consume(
                    session,
                    base_settings,
                    "invite-ip-day",
                    visitor_ip(request),
                    20,
                    86400,
                )
                url = create_invite(
                    session,
                    lead.id,
                    context.criteria,
                    base_settings.widget_client_bot_username,
                )
        except WidgetError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        return {"url": url}

    @app.post("/api/public/widget/statuses")
    def widget_statuses(
        request: Request,
        payload: WidgetStatusPayload,
    ) -> Dict[str, Any]:
        references = list(
            dict.fromkeys(str(reference or "").strip().upper() for reference in payload.references)
        )
        try:
            with _database(base_settings) as session:
                verified_lead(session, _bearer_token(request))
                listings = _listings_from_public_refs(
                    session,
                    base_settings.widget_auth_secret,
                    references,
                )
        except WidgetError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        return {
            "items": [
                {
                    "reference": reference,
                    "active": bool(listings.get(reference) and listings[reference].active),
                    "last_seen_at": (
                        _iso(listings[reference].last_seen_at) if listings.get(reference) else None
                    ),
                }
                for reference in references
            ]
        }

    @app.post("/api/public/widget/interests")
    def widget_interest(
        request: Request,
        payload: WidgetInterestPayload,
    ) -> Dict[str, Any]:
        try:
            with _database(base_settings) as session:
                lead = verified_lead(session, _bearer_token(request))
                listing = _listing_from_public_ref(
                    session,
                    base_settings.widget_auth_secret,
                    payload.reference,
                )
                notification = record_interest(
                    session,
                    lead,
                    listing.id,
                    {
                        **payload.search_params,
                        "public_reference": _public_listing_ref(
                            base_settings.widget_auth_secret,
                            listing.id,
                        ),
                        "request_type": "listing_interest",
                    },
                    payload.source_page,
                )
        except WidgetError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        if notification["created"]:
            notify_lead(base_settings, notification)
        return {"ok": True, "message": "Интерес к варианту отмечен"}

    @app.get("/api/widget/leads")
    def widget_leads(request: Request) -> Dict[str, Any]:
        with _database(base_settings) as session:
            leads = session.scalars(
                select(WidgetLeadModel)
                .where(WidgetLeadModel.verified_at.is_not(None))
                .order_by(WidgetLeadModel.created_at.desc())
            ).all()
            interests = session.scalars(
                select(WidgetInterestModel).order_by(WidgetInterestModel.created_at.desc())
            ).all()
            contexts = session.scalars(select(WidgetSearchContextModel)).all()
            subscriptions = session.scalars(select(WidgetTelegramSubscriptionModel)).all()
        contexts_by_lead = {item.lead_id: item for item in contexts}
        subscriptions_by_lead = {item.lead_id: item for item in subscriptions}
        grouped: Dict[int, List[WidgetInterestModel]] = {}
        for interest in interests:
            grouped.setdefault(interest.lead_id, []).append(interest)
        value = {
            "total": len(leads),
            "requests_total": len(interests),
            "interests_total": len(interests),
            "items": [
                {
                    "id": lead.id,
                    "phone": lead.phone,
                    "telegram_status": (
                        subscriptions_by_lead[lead.id].status
                        if lead.id in subscriptions_by_lead
                        else None
                    ),
                    "verified_at": _iso(lead.verified_at),
                    "created_at": _iso(lead.created_at),
                    "source_page": lead.source_page,
                    "utm": lead.utm,
                    "search": (
                        contexts_by_lead[lead.id].criteria if lead.id in contexts_by_lead else {}
                    ),
                    "search_updated_at": (
                        _iso(contexts_by_lead[lead.id].updated_at)
                        if lead.id in contexts_by_lead
                        else None
                    ),
                    "interests": [
                        {
                            "listing_id": item.listing_id,
                            "reference": _public_listing_ref(
                                base_settings.widget_auth_secret,
                                item.listing_id,
                            ),
                            "title": item.listing_title,
                            "url": item.listing_url,
                            "search_params": item.search_params,
                            "created_at": _iso(item.created_at),
                        }
                        for item in grouped.get(lead.id, [])
                    ],
                }
                for lead in leads
            ],
        }
        return _viewer_leads(value) if _is_viewer(request) else value

    @app.get("/api/profiles")
    def profiles(request: Request) -> Dict[str, Any]:
        config = load_web_config(base_settings.database_url)
        value = {
            "active_profile_id": config["active_profile_id"],
            "items": config["profiles"],
        }
        if _is_viewer(request):
            value["items"] = [
                {**profile, "name": f"Демо-профиль {index}"}
                for index, profile in enumerate(value["items"], start=1)
            ]
        return value

    @app.post("/api/profiles", status_code=status.HTTP_201_CREATED)
    def create_profile(payload: SearchProfilePayload) -> Dict[str, Any]:
        try:
            profile = save_search_profile(
                base_settings.database_url,
                payload.model_dump(),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        coordinator.configure()
        return profile

    @app.put("/api/profiles/{profile_id}")
    def update_profile(profile_id: str, payload: SearchProfilePayload) -> Dict[str, Any]:
        try:
            profile = save_search_profile(
                base_settings.database_url,
                payload.model_dump(),
                profile_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        coordinator.configure()
        return profile

    @app.post("/api/profiles/{profile_id}/activate")
    def activate_profile(profile_id: str) -> Dict[str, Any]:
        config = load_web_config(base_settings.database_url)
        try:
            get_profile(config, profile_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Профиль не найден") from exc
        saved = save_web_config(
            base_settings.database_url,
            {"active_profile_id": profile_id},
        )
        return {"active_profile_id": saved["active_profile_id"]}

    @app.delete("/api/profiles/{profile_id}")
    def delete_profile(profile_id: str) -> Dict[str, Any]:
        try:
            saved = delete_search_profile(base_settings.database_url, profile_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Профиль не найден") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        coordinator.configure()
        return {
            "ok": True,
            "active_profile_id": saved["active_profile_id"],
        }

    @app.get("/api/summary")
    def summary(request: Request, profile_id: Optional[str] = None) -> Dict[str, Any]:
        config = load_web_config(base_settings.database_url)
        selected_profile_id = _selected_profile_id(config, profile_id)
        with _database(base_settings) as session:
            active = (
                session.scalar(
                    select(func.count())
                    .select_from(ListingProfileModel)
                    .join(ListingModel, ListingModel.id == ListingProfileModel.listing_id)
                    .where(
                        ListingProfileModel.profile_id == selected_profile_id,
                        ListingModel.active,
                    )
                )
                or 0
            )
            archived = (
                session.scalar(
                    select(func.count())
                    .select_from(ListingProfileModel)
                    .join(ListingModel, ListingModel.id == ListingProfileModel.listing_id)
                    .where(
                        ListingProfileModel.profile_id == selected_profile_id,
                        ~ListingModel.active,
                    )
                )
                or 0
            )
            liked = (
                session.scalar(
                    select(func.count())
                    .select_from(ListingDecisionModel)
                    .join(
                        ListingProfileModel,
                        ListingProfileModel.listing_id == ListingDecisionModel.listing_id,
                    )
                    .where(
                        ListingProfileModel.profile_id == selected_profile_id,
                        ListingDecisionModel.state.in_({"liked", "studying", "trip"}),
                    )
                )
                or 0
            )
            high_score = (
                session.scalar(
                    select(func.count())
                    .select_from(ListingProfileModel)
                    .join(ListingModel, ListingModel.id == ListingProfileModel.listing_id)
                    .where(
                        ListingProfileModel.profile_id == selected_profile_id,
                        ListingModel.active,
                        ListingProfileModel.score >= 80,
                    )
                )
                or 0
            )
            latest = session.scalar(
                select(ScanRunModel).order_by(ScanRunModel.started_at.desc()).limit(1)
            )
            value = {
                "active": active,
                "archived": archived,
                "selected": liked,
                "high_score": high_score,
                "configured": config["configured"],
                "profile_id": selected_profile_id,
                "latest_run": _run_json(latest) if latest else None,
                "job": coordinator.status(),
            }
            return _viewer_summary(value) if _is_viewer(request) else value

    @app.get("/api/listings")
    def listings(
        request: Request,
        q: str = "",
        decision: str = "all",
        source: str = "all",
        active: str = "active",
        min_score: int = Query(default=0, ge=0, le=100),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=24, ge=1, le=100),
        profile_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        config = load_web_config(base_settings.database_url)
        selected_profile_id = _selected_profile_id(config, profile_id)
        with _database(base_settings) as session:
            statement = (
                select(
                    ListingModel,
                    ListingDecisionModel,
                    ListingActivityModel,
                    ListingProfileModel,
                )
                .join(
                    ListingProfileModel,
                    and_(
                        ListingProfileModel.listing_id == ListingModel.id,
                        ListingProfileModel.profile_id == selected_profile_id,
                    ),
                )
                .outerjoin(
                    ListingDecisionModel,
                    ListingDecisionModel.listing_id == ListingModel.id,
                )
                .outerjoin(
                    ListingActivityModel,
                    ListingActivityModel.listing_id == ListingModel.id,
                )
            )
            conditions = [ListingProfileModel.score >= min_score]
            if active == "active":
                conditions.append(ListingModel.active.is_(True))
            elif active == "archived":
                conditions.append(ListingModel.active.is_(False))
            if source != "all":
                conditions.append(ListingModel.source == source)
            if decision != "all":
                if decision == "new":
                    conditions.append(
                        or_(
                            ListingDecisionModel.state.is_(None),
                            ListingDecisionModel.state == "new",
                        )
                    )
                else:
                    conditions.append(ListingDecisionModel.state == decision)
            if q.strip():
                pattern = f"%{q.strip()}%"
                conditions.append(
                    or_(
                        ListingModel.title.ilike(pattern),
                        ListingModel.locality.ilike(pattern),
                        ListingModel.district.ilike(pattern),
                        ListingModel.description.ilike(pattern),
                    )
                )
            statement = statement.where(*conditions)
            count = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
            rows = session.execute(
                statement.order_by(
                    ListingProfileModel.score.desc(),
                    ListingModel.first_seen_at.desc(),
                )
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).all()
            return {
                "items": [
                    _listing_json(
                        *row,
                        public_secret=base_settings.widget_auth_secret,
                        viewer=_is_viewer(request),
                    )
                    for row in rows
                ],
                "total": count,
                "page": page,
                "page_size": page_size,
                "pages": max(1, (count + page_size - 1) // page_size),
            }

    @app.get("/api/listings/{listing_id}")
    def listing_detail(
        listing_id: int,
        request: Request,
        profile_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        config = load_web_config(base_settings.database_url)
        selected_profile_id = _selected_profile_id(config, profile_id)
        with _database(base_settings) as session:
            row = session.execute(
                select(
                    ListingModel,
                    ListingDecisionModel,
                    ListingActivityModel,
                    ListingProfileModel,
                )
                .outerjoin(
                    ListingProfileModel,
                    and_(
                        ListingProfileModel.listing_id == ListingModel.id,
                        ListingProfileModel.profile_id == selected_profile_id,
                    ),
                )
                .outerjoin(
                    ListingDecisionModel,
                    ListingDecisionModel.listing_id == ListingModel.id,
                )
                .outerjoin(
                    ListingActivityModel,
                    ListingActivityModel.listing_id == ListingModel.id,
                )
                .where(ListingModel.id == listing_id)
            ).one_or_none()
            if row is None:
                raise HTTPException(status_code=404, detail="Объявление не найдено")
            return _listing_json(
                *row,
                detailed=True,
                public_secret=base_settings.widget_auth_secret,
                viewer=_is_viewer(request),
            )

    @app.put("/api/listings/{listing_id}/decision")
    def update_decision(listing_id: int, payload: DecisionPayload) -> Dict[str, Any]:
        if payload.state not in DECISION_STATES:
            raise HTTPException(status_code=422, detail="Неизвестный статус")
        with _database(base_settings) as session:
            listing = session.get(ListingModel, listing_id)
            if listing is None:
                raise HTTPException(status_code=404, detail="Объявление не найдено")
            decision = session.scalar(
                select(ListingDecisionModel).where(ListingDecisionModel.listing_id == listing_id)
            )
            if decision is None:
                decision = ListingDecisionModel(listing_id=listing_id)
                session.add(decision)
            previous_state = decision.state
            previous_note = decision.note
            decision.state = payload.state
            decision.note = payload.note.strip()
            if previous_state != decision.state or previous_note != decision.note:
                session.add(
                    ListingEventModel(
                        listing_id=listing_id,
                        event_type="decision",
                        payload={
                            "from": previous_state,
                            "to": decision.state,
                            "note_changed": previous_note != decision.note,
                        },
                    )
                )
            session.flush()
            return {
                "listing_id": listing_id,
                "state": decision.state,
                "note": decision.note,
            }

    @app.get("/api/listings/{listing_id}/history")
    def listing_history(listing_id: int, request: Request) -> Dict[str, Any]:
        with _database(base_settings) as session:
            listing = session.get(ListingModel, listing_id)
            if listing is None:
                raise HTTPException(status_code=404, detail="Объявление не найдено")
            snapshots = list(
                session.scalars(
                    select(ListingSnapshotModel)
                    .where(ListingSnapshotModel.listing_id == listing_id)
                    .order_by(ListingSnapshotModel.observed_at)
                )
            )
            events = list(
                session.scalars(
                    select(ListingEventModel)
                    .where(ListingEventModel.listing_id == listing_id)
                    .order_by(ListingEventModel.occurred_at)
                )
            )
            value = {
                "listing_id": listing_id,
                "first_seen_at": _iso(listing.first_seen_at),
                "last_seen_at": _iso(listing.last_seen_at),
                "active": listing.active,
                "snapshots": [
                    {
                        "observed_at": _iso(snapshot.observed_at),
                        "price_usd": snapshot.price_usd,
                        "status": snapshot.status,
                    }
                    for snapshot in snapshots
                ],
                "events": [
                    {
                        "occurred_at": _iso(event.occurred_at),
                        "type": event.event_type,
                        "payload": event.payload or {},
                    }
                    for event in events
                ],
            }
            return _viewer_history(value) if _is_viewer(request) else value

    @app.post("/api/jobs/{kind}", status_code=status.HTTP_202_ACCEPTED)
    def start_job(kind: str, profile_id: Optional[str] = None) -> JSONResponse:
        if kind not in {"scan", "activity"}:
            raise HTTPException(status_code=404, detail="Неизвестная задача")
        config = load_web_config(base_settings.database_url)
        if not config["configured"]:
            raise HTTPException(status_code=409, detail="Сначала завершите настройку")
        started = (
            coordinator.request_scan(profile_id=profile_id)
            if kind == "scan"
            else coordinator.request_activity()
        )
        if not started:
            return JSONResponse(
                status_code=409,
                content={"ok": False, "detail": "Другая задача уже выполняется"},
            )
        return JSONResponse(status_code=202, content={"ok": True, "kind": kind})

    @app.get("/api/jobs")
    def jobs(request: Request) -> Dict[str, Any]:
        value = coordinator.status()
        return _viewer_job(value) if _is_viewer(request) else value

    @app.get("/api/map")
    def map_points(request: Request, profile_id: Optional[str] = None) -> Dict[str, Any]:
        config = load_web_config(base_settings.database_url)
        map_provider = config["map_provider"]
        selected_profile_id = _selected_profile_id(config, profile_id)
        with _database(base_settings) as session:
            rows = session.execute(
                select(ListingModel, ListingDecisionModel, ListingProfileModel)
                .join(
                    ListingProfileModel,
                    and_(
                        ListingProfileModel.listing_id == ListingModel.id,
                        ListingProfileModel.profile_id == selected_profile_id,
                    ),
                )
                .outerjoin(
                    ListingDecisionModel,
                    ListingDecisionModel.listing_id == ListingModel.id,
                )
                .where(
                    ListingModel.active.is_(True),
                    ListingModel.latitude.is_not(None),
                    ListingModel.longitude.is_not(None),
                )
                .order_by(ListingProfileModel.score.desc())
                .limit(1000)
            ).all()
            items = []
            for listing, decision, match in rows:
                point = _trip_point(listing, decision)
                viewer = _is_viewer(request)
                latitude = round(listing.latitude, 2) if viewer else listing.latitude
                longitude = round(listing.longitude, 2) if viewer else listing.longitude
                if viewer:
                    point = TripPoint(
                        **{
                            **point.__dict__,
                            "place": _public_listing_ref(
                                base_settings.widget_auth_secret,
                                listing.id,
                            ),
                            "listing_url": "",
                            "latitude": latitude,
                            "longitude": longitude,
                        }
                    )
                items.append(
                    {
                        "id": listing.id,
                        "title": (
                            "Объект "
                            + _public_listing_ref(
                                base_settings.widget_auth_secret,
                                listing.id,
                            )
                            if viewer
                            else listing.title
                        ),
                        "latitude": latitude,
                        "longitude": longitude,
                        "price_usd": listing.price_usd,
                        "area_sotok": listing.area_sotok,
                        "score": match.score,
                        "url": None if viewer else listing.canonical_url,
                        "decision": decision.state if decision else "new",
                        "map_url": maps_pin_url(point, map_provider),
                    }
                )
            return {
                "map_provider": map_provider,
                "map_provider_label": map_provider_label(map_provider),
                "items": items,
            }

    @app.get("/api/source-health")
    def source_health(request: Request, profile_id: Optional[str] = None) -> Dict[str, Any]:
        config = load_web_config(base_settings.database_url)
        selected_profile_id = _selected_profile_id(config, profile_id)
        profile = get_profile(config, selected_profile_id)
        saved = {
            item["source"]: item
            for item in health_payloads(base_settings.database_url, selected_profile_id)
        }
        items = []
        for source in profile["sources"]:
            items.append(
                saved.get(
                    source,
                    {
                        "source": source,
                        "profile_id": selected_profile_id,
                        "status": "never",
                        "last_attempt_at": None,
                        "last_success_at": None,
                        "last_error": None,
                        "last_item_count": 0,
                        "consecutive_failures": 0,
                        "diagnostics": {},
                    },
                )
            )
        value = {"profile_id": selected_profile_id, "items": items}
        return _viewer_health(value) if _is_viewer(request) else value

    @app.post("/api/trips/plan")
    def trip_plan(
        payload: TripPlanPayload,
        profile_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        config = load_web_config(base_settings.database_url)
        selected_profile_id = _selected_profile_id(config, profile_id)
        with _database(base_settings) as session:
            statement = (
                select(ListingModel, ListingDecisionModel)
                .join(
                    ListingProfileModel,
                    and_(
                        ListingProfileModel.listing_id == ListingModel.id,
                        ListingProfileModel.profile_id == selected_profile_id,
                    ),
                )
                .join(
                    ListingDecisionModel,
                    ListingDecisionModel.listing_id == ListingModel.id,
                )
                .where(
                    ListingModel.active.is_(True),
                    ListingModel.latitude.is_not(None),
                    ListingModel.longitude.is_not(None),
                )
            )
            if payload.listing_ids:
                statement = statement.where(ListingModel.id.in_(payload.listing_ids))
            else:
                statement = statement.where(ListingDecisionModel.state == "trip")
            rows = session.execute(statement.order_by(ListingProfileModel.score.desc())).all()
            points = [_trip_point(listing, decision) for listing, decision in rows]
        routes = build_trip_routes(
            points,
            max_points=payload.max_points_per_route,
            start=(payload.start_latitude, payload.start_longitude),
        )
        return {
            "points": len(points),
            "map_provider": config["map_provider"],
            "map_provider_label": map_provider_label(config["map_provider"]),
            "routes": [
                {
                    "index": index,
                    "url": maps_route_url(route, config["map_provider"]),
                    "distance_km": round(_route_distance(route, payload), 1),
                    "items": [
                        {
                            "id": int(point.external_id),
                            "title": point.place,
                            "latitude": point.latitude,
                            "longitude": point.longitude,
                            "price": point.price,
                            "url": point.listing_url,
                        }
                        for point in route
                    ],
                }
                for index, route in enumerate(routes, start=1)
            ],
        }

    @app.get("/api/export.csv")
    def export_csv() -> StreamingResponse:
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(
            [
                "source",
                "source_id",
                "title",
                "price_usd",
                "area_sotok",
                "distance_mkad_km",
                "score",
                "decision",
                "active",
                "url",
            ]
        )
        with _database(base_settings) as session:
            rows = session.execute(
                select(ListingModel, ListingDecisionModel)
                .outerjoin(
                    ListingDecisionModel,
                    ListingDecisionModel.listing_id == ListingModel.id,
                )
                .order_by(ListingModel.score.desc())
            )
            for listing, decision in rows:
                writer.writerow(
                    [
                        listing.source,
                        listing.source_id,
                        listing.title,
                        listing.price_usd,
                        listing.area_sotok,
                        listing.distance_mkad_km,
                        listing.score,
                        decision.state if decision else "new",
                        listing.active,
                        listing.canonical_url,
                    ]
                )
        content = "\ufeff" + output.getvalue()
        return StreamingResponse(
            iter([content]),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=land-plots.csv"},
        )

    @app.get("/api/backups/info")
    def backup_info() -> Dict[str, int]:
        try:
            return database_stats(base_settings.database_url)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/backups/download")
    def download_backup() -> FileResponse:
        if coordinator.status()["running"]:
            raise HTTPException(
                status_code=409,
                detail="Дождитесь завершения текущей задачи",
            )
        try:
            path = create_backup(base_settings.database_url)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return FileResponse(
            path,
            filename=backup_filename(),
            media_type="application/vnd.sqlite3",
            background=BackgroundTask(path.unlink, missing_ok=True),
        )

    @app.post("/api/backups/restore")
    async def upload_backup(request: Request) -> Dict[str, Any]:
        if coordinator.status()["running"]:
            raise HTTPException(
                status_code=409,
                detail="Дождитесь завершения текущей задачи",
            )
        content = await request.body()
        try:
            restored = restore_backup(base_settings.database_url, content)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        engine = make_engine(base_settings.database_url)
        init_db(engine)
        engine.dispose()
        coordinator.configure()
        return {"ok": True, **restored}

    @app.get("/widget-demo")
    def widget_demo() -> FileResponse:
        return FileResponse(STATIC_DIR / "widget-demo.html")

    @app.get("/login")
    def login_page() -> Response:
        if base_settings.admin_brand == "liderstroy":
            html = (STATIC_DIR / "login.html").read_text(encoding="utf-8")
            html = html.replace("Вход · LandPlotFinder", "Вход · ЛидерСтрой", 1)
            html = html.replace(
                "</style>",
                '</style>\n  <link rel="stylesheet" '
                'href="/static/admin-login-brand.css?v=20260925-lider">',
                1,
            )
            html = html.replace("Вход в LandPlotFinder", "Вход в панель ЛидерСтрой", 1)
            return HTMLResponse(html, headers={"Cache-Control": "no-store, max-age=0"})
        return FileResponse(
            STATIC_DIR / "login.html",
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    @app.get("/sw.js")
    def retire_service_worker() -> FileResponse:
        return FileResponse(
            STATIC_DIR / "sw.js",
            media_type="application/javascript",
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    @app.get("/")
    @app.get("/{path:path}")
    def index(request: Request, path: str = "") -> Response:
        if path.startswith("api/"):
            raise HTTPException(status_code=404)
        if base_settings.admin_brand == "liderstroy":
            html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
            html = html.replace(
                "LandPlotFinder — клиенты и каталог",
                "ЛидерСтрой — клиенты и подбор участков",
                1,
            )
            html = html.replace('content="#31473a"', 'content="#201f1e"', 1)
            html = html.replace(
                '<link rel="stylesheet" href="/static/v11.css?v=20260925-clients">',
                '<link rel="stylesheet" href="/static/v11.css?v=20260925-clients">\n'
                '  <link rel="stylesheet" href="/static/admin-brand.css?v=20260925-lider">',
                1,
            )
            html = html.replace(
                '<a class="brand" href="#widget" aria-label="LandPlotFinder — клиенты">',
                '<a class="brand" href="#widget" aria-label="ЛидерСтрой — клиенты">',
                1,
            )
            html = html.replace(
                "<span><strong>LandPlot</strong><small>Finder</small></span>",
                "<span><strong>ЛИДЕР СТРОЙ</strong><small>Подбор участков</small></span>",
                1,
            )
            html = html.replace(
                'id="serviceText">Локальный сервис',
                'id="serviceText">Сервис подбора',
                1,
            )
            return HTMLResponse(html, headers={"Cache-Control": "no-store"})
        return FileResponse(STATIC_DIR / "index.html")

    return app


@contextmanager
def _database(settings: Settings):
    engine = make_engine(settings.database_url)
    init_db(engine)
    factory = make_session_factory(engine)
    try:
        with session_scope(factory) as session:
            yield session
    finally:
        engine.dispose()


def _listing_json(
    listing: ListingModel,
    decision: Optional[ListingDecisionModel],
    activity: Optional[ListingActivityModel],
    profile: Optional[ListingProfileModel],
    detailed: bool = False,
    public_secret: Optional[str] = None,
    viewer: bool = False,
) -> Dict[str, Any]:
    public_ref = _public_listing_ref(public_secret, listing.id) if public_secret else None
    value: Dict[str, Any] = {
        "id": listing.id,
        "public_ref": public_ref,
        "external_id": f"{listing.source}:{listing.source_id}",
        "source": listing.source,
        "title": listing.title,
        "district": listing.district,
        "locality": listing.locality,
        "address": listing.address,
        "price_usd": listing.price_usd,
        "area_sotok": listing.area_sotok,
        "distance_mkad_km": listing.distance_mkad_km,
        "latitude": listing.latitude,
        "longitude": listing.longitude,
        "score": profile.score if profile else listing.score,
        "match_status": profile.status if profile else listing.status,
        "profile_id": profile.profile_id if profile else None,
        "decision": decision.state if decision else "new",
        "note": decision.note if decision else "",
        "active": listing.active,
        "possible_duplicate": listing.possible_duplicate,
        "object_kind": (listing.raw_payload or {}).get("object_kind", "Участок"),
        "url": listing.canonical_url,
        "first_seen_at": _iso(listing.first_seen_at),
        "last_seen_at": _iso(listing.last_seen_at),
        "activity_status": activity.last_status if activity else None,
        "activity_checked_at": _iso(activity.last_checked_at) if activity else None,
    }
    if detailed:
        value.update(
            {
                "description": listing.description,
                "direction": listing.direction,
                "facade_m": listing.facade_m,
                "depth_m": listing.depth_m,
                "purpose": listing.purpose,
                "electricity": listing.electricity_raw,
                "electricity_kw": listing.electricity_kw,
                "gas": listing.gas_raw,
                "water": listing.water_raw,
                "sewerage": listing.sewerage_raw,
                "internet": listing.internet_raw,
                "road": listing.road_raw,
                "nature": listing.nature_raw,
                "ownership": listing.ownership_raw,
                "reasons": profile.reasons if profile else listing.reasons or [],
                "evidence": listing.evidence or {},
            }
        )
    if viewer:
        value.update(
            {
                "external_id": public_ref,
                "title": f"Объект {public_ref or listing.id}",
                "district": "Пригород",
                "locality": "Выбранное направление",
                "address": None,
                "latitude": round(listing.latitude, 2) if listing.latitude is not None else None,
                "longitude": round(listing.longitude, 2) if listing.longitude is not None else None,
                "note": "",
                "url": None,
            }
        )
        if detailed:
            value.update(
                {
                    "description": "Описание и контакты скрыты в демонстрационном режиме.",
                    "evidence": {},
                }
            )
    return value


def _public_widget_listing_json(
    listing: ListingModel,
    location_profile: Optional[LocationProfileModel],
    public_secret: str,
    *,
    max_price_usd: Optional[float],
    min_area_sotok: Optional[float],
    max_area_sotok: Optional[float],
    max_distance_km: Optional[float],
    electricity_required: bool,
    gas_required: bool,
    water_required: bool,
    sewerage_required: bool,
) -> Dict[str, Any]:
    location = _public_location(listing)
    score, match_reasons, warnings = _personal_match_score(
        listing,
        location_profile,
        max_price_usd=max_price_usd,
        min_area_sotok=min_area_sotok,
        max_area_sotok=max_area_sotok,
        max_distance_km=max_distance_km,
        electricity_required=electricity_required,
        gas_required=gas_required,
        water_required=water_required,
        sewerage_required=sewerage_required,
    )
    latitude = round(listing.latitude, 2) if listing.latitude is not None else None
    longitude = round(listing.longitude, 2) if listing.longitude is not None else None
    return {
        "reference": _public_listing_ref(public_secret, listing.id),
        "url": listing.canonical_url,
        "title": _public_listing_title(listing),
        "location": location,
        "direction": listing.direction,
        "price_usd": listing.price_usd,
        "area_sotok": listing.area_sotok,
        "distance_mkad_km": listing.distance_mkad_km,
        "facade_m": listing.facade_m,
        "depth_m": listing.depth_m,
        "purpose": _purpose_label(listing.purpose),
        "ownership": _ownership_label(listing.ownership_raw),
        "electricity": _electricity_label(listing),
        "gas": _utility_label(listing.gas_raw, "gas"),
        "water": _utility_label(listing.water_raw, "water"),
        "sewerage": _utility_label(listing.sewerage_raw, "sewerage"),
        "internet": _utility_label(listing.internet_raw, "internet"),
        "road": _utility_label(listing.road_raw, "road"),
        "match_score": score,
        "match_reasons": match_reasons,
        "warnings": warnings,
        "location_score": location_profile.score if location_profile else None,
        "location_verdict": location_profile.verdict if location_profile else None,
        "location_confidence": location_profile.confidence if location_profile else None,
        "location_signals": list(location_profile.signals[:4]) if location_profile else [],
        "location_risks": list(location_profile.risks[:3]) if location_profile else [],
        "nearby_premium_houses": (location_profile.premium_house_count if location_profile else 0),
        "latitude": latitude,
        "longitude": longitude,
        "coordinates_approximate": latitude is not None and longitude is not None,
        "last_seen_at": _iso(listing.last_seen_at),
    }


def _public_listing_ref(secret: str, listing_id: int) -> str:
    digest = hmac.new(
        secret.encode("utf-8"),
        f"public-listing:{listing_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:10]
    return f"LP-{digest.upper()}"


def _listing_from_public_ref(
    session: Session,
    secret: str,
    reference: str,
) -> ListingModel:
    normalized = str(reference or "").strip().upper()
    if not normalized.startswith("LP-"):
        raise WidgetError("Вариант не найден", 404)
    listings = session.scalars(select(ListingModel).where(ListingModel.active.is_(True))).all()
    for listing in listings:
        expected = _public_listing_ref(secret, listing.id)
        if secrets.compare_digest(expected, normalized):
            return listing
    raise WidgetError("Объявление больше недоступно", 404)


def _listings_from_public_refs(
    session: Session,
    secret: str,
    references: List[str],
) -> Dict[str, ListingModel]:
    wanted = {
        reference
        for reference in references
        if len(reference) == 13 and reference.startswith("LP-")
    }
    if not wanted:
        return {}
    result: Dict[str, ListingModel] = {}
    for listing in session.scalars(select(ListingModel)).all():
        reference = _public_listing_ref(secret, listing.id)
        if reference in wanted:
            result[reference] = listing
            if len(result) == len(wanted):
                break
    return result


def _public_listing_title(listing: ListingModel) -> str:
    area = (
        f"{listing.area_sotok:g} сот." if listing.area_sotok is not None else "площадь уточняется"
    )
    if listing.locality:
        return f"Участок {area} в районе {listing.locality}"
    if listing.district:
        return f"Участок {area}, {listing.district}"
    return f"Участок {area}"


def _public_location(listing: ListingModel) -> str:
    if listing.locality and listing.district:
        return f"Район {listing.locality}, {listing.district}"
    if listing.locality:
        return f"Район {listing.locality}"
    return listing.district or "Расположение уточняется"


def _personal_match_score(
    listing: ListingModel,
    location: Optional[LocationProfileModel],
    *,
    max_price_usd: Optional[float],
    min_area_sotok: Optional[float],
    max_area_sotok: Optional[float],
    max_distance_km: Optional[float],
    electricity_required: bool,
    gas_required: bool,
    water_required: bool,
    sewerage_required: bool,
) -> tuple[int, List[str], List[str]]:
    reasons: List[str] = []
    warnings: List[str] = []

    price_points = 15.0
    if max_price_usd and listing.price_usd is not None:
        ratio = min(1.0, max(0.0, listing.price_usd / max_price_usd))
        price_points = 30.0 * (1.0 - 0.4 * ratio)
        saving = max_price_usd - listing.price_usd
        reasons.append(f"Ниже бюджета на ${saving:,.0f}".replace(",", " "))
    elif listing.price_usd is None:
        warnings.append("Цена не подтверждена")

    area_points = 17.0
    if listing.area_sotok is not None and min_area_sotok and max_area_sotok:
        center = (min_area_sotok + max_area_sotok) / 2
        half_range = max((max_area_sotok - min_area_sotok) / 2, 0.5)
        closeness = max(0.0, 1.0 - abs(listing.area_sotok - center) / half_range)
        area_points = 17.0 + 8.0 * closeness
        reasons.append("Площадь входит в выбранный диапазон")
    elif listing.area_sotok is None:
        warnings.append("Площадь не подтверждена")

    distance_points = 10.0
    if max_distance_km is not None and listing.distance_mkad_km is not None:
        if max_distance_km == 0:
            ratio = 0.0
        else:
            ratio = min(1.0, max(0.0, listing.distance_mkad_km / max_distance_km))
        distance_points = 20.0 * (1.0 - 0.4 * ratio)
        reasons.append(f"Около {listing.distance_mkad_km:g} км до МКАД")
    elif listing.distance_mkad_km is None:
        warnings.append("Расстояние до МКАД не подтверждено")

    utility_points = 0.0
    if listing.electricity_kw:
        utility_points += 7.0
        reasons.append(f"Электричество {listing.electricity_kw:g} кВт")
    elif listing.electricity_raw:
        utility_points += 5.0
        reasons.append("Электричество упоминается")
    elif electricity_required:
        warnings.append("Электричество не подтверждено")
    if _utility_present(listing.gas_raw, "gas"):
        utility_points += 4.0
        reasons.append("Есть сведения о газе")
    elif gas_required:
        warnings.append("Газ не подтверждён")
    if _utility_present(listing.water_raw, "water"):
        utility_points += 2.0 if water_required else 1.0
        if water_required:
            reasons.append("Есть сведения о водоснабжении")
    elif water_required:
        warnings.append("Водоснабжение не подтверждено")
    if _utility_present(listing.sewerage_raw, "sewerage"):
        utility_points += 2.0 if sewerage_required else 1.0
        if sewerage_required:
            reasons.append("Есть сведения о канализации")
    elif sewerage_required:
        warnings.append("Канализация не подтверждена")
    utility_points += float(_utility_present(listing.internet_raw, "internet"))
    utility_points += float(_utility_present(listing.road_raw, "road"))
    utility_points = min(15.0, utility_points)

    location_points = (location.score / 100 * 10.0) if location else 5.0
    if location:
        reasons.append(f"Оценка локации {location.score}/100")
    else:
        warnings.append("Локация ещё изучается")

    return (
        round(
            min(
                100.0,
                price_points + area_points + distance_points + utility_points + location_points,
            )
        ),
        reasons[:5],
        warnings[:4],
    )


def _electricity_label(listing: ListingModel) -> Optional[str]:
    if listing.electricity_kw:
        return f"{listing.electricity_kw:g} кВт"
    if listing.electricity_raw and not _utility_present(listing.electricity_raw, "electricity"):
        return "Нет"
    return "Упоминается" if listing.electricity_raw else None


def _utility_label(value: Optional[str], kind: str) -> Optional[str]:
    if not value:
        return None
    text = value.lower()
    if not _utility_present(value, kind):
        return "Нет"
    patterns = {
        "gas": (("по улице", "По улице"), ("на участке", "На участке")),
        "water": (
            ("централ", "Центральная"),
            ("скваж", "Скважина"),
            ("колод", "Колодец"),
        ),
        "sewerage": (
            ("централ", "Центральная"),
            ("септик", "Септик"),
            ("местн", "Местная"),
        ),
        "internet": (
            ("оптовол", "Оптоволокно"),
            ("оптик", "Оптоволокно"),
            ("4g", "4G"),
        ),
        "road": (
            ("асфальт", "Асфальт"),
            ("грав", "Гравийная"),
            ("грунт", "Грунтовая"),
        ),
    }
    for marker, label in patterns.get(kind, ()):
        if marker in text:
            return label
    return "Есть сведения"


def _utility_present(value: Optional[str], kind: str) -> bool:
    text = " ".join((value or "").lower().split())
    if not text:
        return False
    absent_patterns = {
        "electricity": (
            "электричества нет",
            "без электричества",
            "электричество отсутств",
            "электроэнергии нет",
        ),
        "gas": ("газа нет", "без газа", "газ отсутств"),
        "water": ("воды нет", "без воды", "водоснабжение отсутств"),
        "sewerage": ("канализации нет", "без канализации", "канализация отсутств"),
        "internet": ("интернета нет", "без интернета", "интернет отсутств"),
        "road": ("дороги нет", "без дороги", "дорога отсутств"),
    }
    if text == "нет":
        return False
    return not any(marker in text for marker in absent_patterns.get(kind, ()))


def _ownership_label(value: Optional[str]) -> Optional[str]:
    text = (value or "").lower()
    if "частн" in text and "собствен" in text:
        return "Частная собственность"
    if "пожизн" in text:
        return "Пожизненное наследуемое владение"
    if "аренд" in text:
        return "Аренда"
    cleaned = " ".join((value or "").split())
    return cleaned[:120] if cleaned else None


def _purpose_label(value: Optional[str]) -> Optional[str]:
    text = (value or "").lower()
    if "лпх" in text or "личн" in text and "подсоб" in text:
        return "Личное подсобное хозяйство (ЛПХ)"
    if any(marker in text for marker in ("жилого дома", "строительств")):
        return "Для строительства жилого дома"
    if any(marker in text for marker in ("садовод", "дач")):
        return "Садоводство"
    cleaned = " ".join((value or "").split())
    return cleaned[:120] if cleaned else None


def _widget_sort_key(item: Dict[str, Any], sort: str) -> tuple:
    if sort == "price":
        return (item["price_usd"] is None, item["price_usd"] or 0)
    if sort == "distance":
        return (
            item["distance_mkad_km"] is None,
            item["distance_mkad_km"] or 0,
        )
    if sort == "newest":
        return (item["last_seen_at"] or "",)
    return (-item["match_score"], -(item["location_score"] or 0))


def _run_json(run: ScanRunModel) -> Dict[str, Any]:
    return {
        "started_at": _iso(run.started_at),
        "completed_at": _iso(run.completed_at),
        "status": run.status,
        "source_stats": run.source_stats,
        "errors": run.errors,
    }


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _selected_profile_id(config: Dict[str, Any], profile_id: Optional[str]) -> str:
    selected = str(profile_id or config["active_profile_id"])
    try:
        get_profile(config, selected)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Профиль поиска не найден") from exc
    return selected


def _validate_widget_area(
    min_area_sotok: Optional[float],
    max_area_sotok: Optional[float],
) -> None:
    if (
        min_area_sotok is not None
        and max_area_sotok is not None
        and min_area_sotok > max_area_sotok
    ):
        raise HTTPException(
            status_code=422,
            detail="Минимальная площадь не может быть больше максимальной",
        )


def _widget_listing_statement(
    profile_id: str,
    q: str,
    max_price_usd: Optional[float],
    min_area_sotok: Optional[float],
    max_area_sotok: Optional[float],
    max_distance_km: Optional[float],
    electricity: bool,
    gas: bool,
    water: bool,
    sewerage: bool,
):
    statement = (
        select(ListingModel, ListingProfileModel)
        .join(
            ListingProfileModel,
            and_(
                ListingProfileModel.listing_id == ListingModel.id,
                ListingProfileModel.profile_id == profile_id,
            ),
        )
        .where(ListingModel.active.is_(True))
    )
    conditions = []
    if q.strip():
        pattern = f"%{q.strip()}%"
        conditions.append(
            or_(
                ListingModel.locality.ilike(pattern),
                ListingModel.district.ilike(pattern),
                ListingModel.direction.ilike(pattern),
            )
        )
    if max_price_usd is not None:
        conditions.extend(
            [
                ListingModel.price_usd.is_not(None),
                ListingModel.price_usd <= max_price_usd,
            ]
        )
    if min_area_sotok is not None:
        conditions.extend(
            [
                ListingModel.area_sotok.is_not(None),
                ListingModel.area_sotok >= min_area_sotok,
            ]
        )
    if max_area_sotok is not None:
        conditions.extend(
            [
                ListingModel.area_sotok.is_not(None),
                ListingModel.area_sotok <= max_area_sotok,
            ]
        )
    if max_distance_km is not None:
        conditions.extend(
            [
                ListingModel.distance_mkad_km.is_not(None),
                ListingModel.distance_mkad_km <= max_distance_km,
            ]
        )
    if electricity:
        conditions.append(
            or_(
                ListingModel.electricity_kw > 0,
                and_(
                    ListingModel.electricity_raw.is_not(None),
                    ListingModel.electricity_raw != "",
                ),
            )
        )
    if gas:
        conditions.append(
            and_(
                ListingModel.gas_raw.is_not(None),
                ListingModel.gas_raw != "",
                ~ListingModel.gas_raw.ilike("%газа нет%"),
                ~ListingModel.gas_raw.ilike("%без газа%"),
                func.lower(ListingModel.gas_raw) != "нет",
            )
        )
    if water:
        conditions.append(
            and_(
                ListingModel.water_raw.is_not(None),
                ListingModel.water_raw != "",
                ~ListingModel.water_raw.ilike("%воды нет%"),
                ~ListingModel.water_raw.ilike("%без воды%"),
                func.lower(ListingModel.water_raw) != "нет",
            )
        )
    if sewerage:
        conditions.append(
            and_(
                ListingModel.sewerage_raw.is_not(None),
                ListingModel.sewerage_raw != "",
                ~ListingModel.sewerage_raw.ilike("%канализации нет%"),
                ~ListingModel.sewerage_raw.ilike("%без канализации%"),
                func.lower(ListingModel.sewerage_raw) != "нет",
            )
        )
    return statement.where(*conditions)


def _is_public_widget_path(path: str) -> bool:
    return path in {
        "/health",
        "/login",
        "/sw.js",
        "/widget-demo",
        "/api/auth/login",
        "/api/auth/logout",
    } or path.startswith(("/api/public/widget/", "/static/"))


def _viewer_web_config(value: Dict[str, Any]) -> Dict[str, Any]:
    result = copy.deepcopy(value)
    result.update(
        {
            "telegram_enabled": False,
            "telegram_bot_token": "",
            "telegram_chat_id": "",
            "telegram_configured": False,
        }
    )
    result["profiles"] = [
        {**profile, "name": f"Демо-профиль {index}"}
        for index, profile in enumerate(result.get("profiles", []), start=1)
    ]
    return result


def _viewer_leads(value: Dict[str, Any]) -> Dict[str, Any]:
    result = copy.deepcopy(value)
    for lead in result.get("items", []):
        lead.update({"phone": "+375 •• •••-••-••", "source_page": "", "utm": {}})
        lead["search"] = {
            key: item
            for key, item in (lead.get("search") or {}).items()
            if key
            in {
                "max_price_usd",
                "min_area_sotok",
                "max_area_sotok",
                "max_distance_km",
                "electricity",
                "gas",
                "water",
                "sewerage",
            }
        }
        for interest in lead.get("interests", []):
            reference = interest.get("reference") or "LP-DEMO"
            interest.update(
                {
                    "title": f"Объект {reference}",
                    "url": None,
                    "search_params": {
                        key: item
                        for key, item in (interest.get("search_params") or {}).items()
                        if key
                        in {
                            "max_price_usd",
                            "min_area_sotok",
                            "max_area_sotok",
                            "max_distance_km",
                            "electricity",
                            "gas",
                            "water",
                            "sewerage",
                        }
                    },
                }
            )
    return result


def _viewer_job(value: Dict[str, Any]) -> Dict[str, Any]:
    result = copy.deepcopy(value)
    if result.get("last_error"):
        result["last_error"] = "Последний запуск завершился с ошибкой"
    last_result = result.get("last_result")
    if isinstance(last_result, dict) and last_result.get("errors"):
        last_result["errors"] = {"sources": "Некоторые источники требуют проверки"}
    for job in result.get("scheduled", []):
        job["name"] = "Автоматический поиск"
    return result


def _viewer_summary(value: Dict[str, Any]) -> Dict[str, Any]:
    result = copy.deepcopy(value)
    result["job"] = _viewer_job(result.get("job") or {})
    latest = result.get("latest_run")
    if isinstance(latest, dict) and latest.get("errors"):
        latest["errors"] = {"sources": "Некоторые источники требуют проверки"}
    return result


def _viewer_health(value: Dict[str, Any]) -> Dict[str, Any]:
    result = copy.deepcopy(value)
    for item in result.get("items", []):
        if item.get("last_error"):
            item["last_error"] = "Последняя проверка завершилась ошибкой"
        diagnostics = item.get("diagnostics")
        if isinstance(diagnostics, dict) and diagnostics.get("warnings"):
            diagnostics["warnings"] = ["Источник требует внимания"]
    return result


def _viewer_history(value: Dict[str, Any]) -> Dict[str, Any]:
    result = copy.deepcopy(value)
    for event in result.get("events", []):
        payload = event.get("payload") or {}
        if event.get("type") == "decision":
            event["payload"] = {
                key: payload[key] for key in ("from", "to", "note_changed") if key in payload
            }
        elif event.get("type") == "updated":
            event["payload"] = {
                "changes": {
                    key: {"label": change.get("label", key)}
                    for key, change in (payload.get("changes") or {}).items()
                    if isinstance(change, dict)
                }
            }
        else:
            event["payload"] = {}
    return result


def _viewer_request_allowed(request: Request) -> bool:
    if request.url.path == "/api/auth/logout":
        return True
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        return False
    return request.url.path not in {
        "/api/export.csv",
        "/api/backups/info",
        "/api/backups/download",
    }


def _request_role(request: Request) -> str:
    role = str(getattr(request.state, "auth_role", "admin"))
    return role if role in AUTH_ROLES else "admin"


def _is_viewer(request: Request) -> bool:
    return _request_role(request) == "viewer"


def _admin_session_secret(settings: Settings) -> bytes:
    return (
        f"{settings.admin_password or ''}\0{settings.viewer_password or ''}"
        f"\0{settings.widget_auth_secret}"
    ).encode("utf-8")


def _admin_session_token(settings: Settings, role: str, username: str) -> str:
    expires_at = int(time.time()) + ADMIN_SESSION_TTL_SECONDS
    payload = f"{role}:{username}:{expires_at}".encode("utf-8")
    encoded = urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    signature = hmac.new(
        _admin_session_secret(settings),
        encoded.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    return f"{encoded}.{signature}"


def _session_role(value: Optional[str], settings: Settings) -> Optional[str]:
    if not value or "." not in value:
        return None
    encoded, signature = value.rsplit(".", 1)
    expected = hmac.new(
        _admin_session_secret(settings),
        encoded.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    if not secrets.compare_digest(signature, expected):
        return None
    try:
        padding = "=" * (-len(encoded) % 4)
        role, username, expires_at = (
            urlsafe_b64decode(encoded + padding).decode("utf-8").rsplit(":", 2)
        )
        if role not in AUTH_ROLES or int(expires_at) <= int(time.time()):
            return None
        expected_username = settings.admin_username if role == "admin" else settings.viewer_username
        password = settings.admin_password if role == "admin" else settings.viewer_password
        return role if password and secrets.compare_digest(username, expected_username) else None
    except (ValueError, UnicodeDecodeError, BinasciiError):
        return None


def _credentials_role(
    credentials: Optional[tuple[str, str]],
    settings: Settings,
) -> Optional[str]:
    if not credentials:
        return None
    username, password = credentials
    candidates = (
        ("admin", settings.admin_username, settings.admin_password),
        ("viewer", settings.viewer_username, settings.viewer_password),
    )
    for role, expected_username, expected_password in candidates:
        if (
            expected_password
            and secrets.compare_digest(username, expected_username)
            and secrets.compare_digest(password, expected_password)
        ):
            return role
    return None


def _basic_credentials(value: Optional[str]) -> Optional[tuple[str, str]]:
    if not value or not value.startswith("Basic "):
        return None
    try:
        decoded = b64decode(value[6:].strip(), validate=True).decode("utf-8")
    except (BinasciiError, UnicodeDecodeError):
        return None
    if ":" not in decoded:
        return None
    username, password = decoded.split(":", 1)
    return username, password


def _bearer_token(request: Request) -> str:
    value = request.headers.get("Authorization", "")
    if not value.startswith("Bearer "):
        return ""
    return value[7:].strip()


def _trip_point(
    listing: ListingModel,
    decision: Optional[ListingDecisionModel],
) -> TripPoint:
    return TripPoint(
        external_id=str(listing.id),
        status=decision.state if decision else "new",
        source=listing.source,
        listing_url=listing.canonical_url,
        district=listing.district or "",
        place=listing.title,
        price=f"${listing.price_usd:,.0f}" if listing.price_usd is not None else "",
        area=f"{listing.area_sotok:g}" if listing.area_sotok is not None else "",
        distance=(f"{listing.distance_mkad_km:g}" if listing.distance_mkad_km is not None else ""),
        latitude=float(listing.latitude),
        longitude=float(listing.longitude),
        rating=str(listing.score),
        location_score="",
    )


def _route_distance(route: List[TripPoint], payload: TripPlanPayload) -> float:
    current = (payload.start_latitude, payload.start_longitude)
    total = 0.0
    for point in route:
        total += _haversine(current[0], current[1], point.latitude, point.longitude)
        current = (point.latitude, point.longitude)
    return total


def _haversine(lat_a: float, lon_a: float, lat_b: float, lon_b: float) -> float:
    latitude_a = math.radians(lat_a)
    latitude_b = math.radians(lat_b)
    delta_latitude = latitude_b - latitude_a
    delta_longitude = math.radians(lon_b - lon_a)
    value = (
        math.sin(delta_latitude / 2) ** 2
        + math.cos(latitude_a) * math.cos(latitude_b) * math.sin(delta_longitude / 2) ** 2
    )
    return 6371.0088 * 2 * math.asin(math.sqrt(value))


def _ensure_sqlite_parent(database_url: str) -> None:
    if not database_url.startswith("sqlite:///"):
        return
    path = database_url[len("sqlite:///") :]
    if path and path != ":memory:":
        Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Provider and Telegram credentials can appear in request URLs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    uvicorn.run(
        "app.web:create_app",
        factory=True,
        host=os.getenv("WEB_HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", os.getenv("WEB_PORT", "8787"))),
    )


if __name__ == "__main__":
    main()
