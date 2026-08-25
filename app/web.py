from __future__ import annotations

import csv
import io
import logging
import math
import os
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select
from starlette.background import BackgroundTask

from app.backups import (
    backup_filename,
    create_backup,
    database_stats,
    restore_backup,
)
from app.config import Settings, load_settings
from app.db import init_db, make_engine, make_session_factory, session_scope
from app.integrations.trips import TripPoint, build_trip_routes, google_maps_route_url
from app.jobs import JobCoordinator
from app.models import (
    ListingActivityModel,
    ListingDecisionModel,
    ListingEventModel,
    ListingModel,
    ListingProfileModel,
    ListingSnapshotModel,
    ScanRunModel,
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

LOGGER = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).with_name("static")
DECISION_STATES = {"new", "liked", "studying", "trip", "rejected"}


class DecisionPayload(BaseModel):
    state: str
    note: str = Field(default="", max_length=4000)


class SettingsPayload(BaseModel):
    configured: Optional[bool] = None
    schedule_enabled: bool = True
    schedule_interval_hours: int = Field(default=6, ge=1, le=168)
    activity_check_enabled: bool = True
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
    sources: List[str] = Field(default_factory=lambda: ["realt", "kufar"])
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
        version="1.1.0",
        lifespan=lifespan,
    )
    app.state.settings = base_settings
    app.state.jobs = coordinator
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/health")
    def health() -> Dict[str, Any]:
        return {"ok": True, "service": "land-plot-finder"}

    @app.get("/api/settings")
    def get_settings() -> Dict[str, Any]:
        return public_web_config(load_web_config(base_settings.database_url))

    @app.put("/api/settings")
    def put_settings(payload: SettingsPayload) -> Dict[str, Any]:
        incoming = payload.model_dump(exclude_none=True)
        current = load_web_config(base_settings.database_url)
        if incoming.get("telegram_bot_token") == "••••••••":
            incoming["telegram_bot_token"] = current.get("telegram_bot_token", "")
        saved = save_web_config(base_settings.database_url, incoming)
        coordinator.configure()
        return public_web_config(saved)

    @app.get("/api/profiles")
    def profiles() -> Dict[str, Any]:
        config = load_web_config(base_settings.database_url)
        return {
            "active_profile_id": config["active_profile_id"],
            "items": config["profiles"],
        }

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
    def summary(profile_id: Optional[str] = None) -> Dict[str, Any]:
        config = load_web_config(base_settings.database_url)
        selected_profile_id = _selected_profile_id(config, profile_id)
        with _database(base_settings) as session:
            active = session.scalar(
                select(func.count())
                .select_from(ListingProfileModel)
                .join(ListingModel, ListingModel.id == ListingProfileModel.listing_id)
                .where(
                    ListingProfileModel.profile_id == selected_profile_id,
                    ListingModel.active,
                )
            ) or 0
            archived = session.scalar(
                select(func.count())
                .select_from(ListingProfileModel)
                .join(ListingModel, ListingModel.id == ListingProfileModel.listing_id)
                .where(
                    ListingProfileModel.profile_id == selected_profile_id,
                    ~ListingModel.active,
                )
            ) or 0
            liked = session.scalar(
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
            ) or 0
            high_score = session.scalar(
                select(func.count())
                .select_from(ListingProfileModel)
                .join(ListingModel, ListingModel.id == ListingProfileModel.listing_id)
                .where(
                    ListingProfileModel.profile_id == selected_profile_id,
                    ListingModel.active,
                    ListingProfileModel.score >= 80,
                )
            ) or 0
            latest = session.scalar(
                select(ScanRunModel).order_by(ScanRunModel.started_at.desc()).limit(1)
            )
            return {
                "active": active,
                "archived": archived,
                "selected": liked,
                "high_score": high_score,
                "configured": config["configured"],
                "profile_id": selected_profile_id,
                "latest_run": _run_json(latest) if latest else None,
                "job": coordinator.status(),
            }

    @app.get("/api/listings")
    def listings(
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
            count = session.scalar(
                select(func.count()).select_from(statement.subquery())
            ) or 0
            rows = session.execute(
                statement.order_by(
                    ListingProfileModel.score.desc(),
                    ListingModel.first_seen_at.desc(),
                )
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).all()
            return {
                "items": [_listing_json(*row) for row in rows],
                "total": count,
                "page": page,
                "page_size": page_size,
                "pages": max(1, (count + page_size - 1) // page_size),
            }

    @app.get("/api/listings/{listing_id}")
    def listing_detail(
        listing_id: int,
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
            return _listing_json(*row, detailed=True)

    @app.put("/api/listings/{listing_id}/decision")
    def update_decision(listing_id: int, payload: DecisionPayload) -> Dict[str, Any]:
        if payload.state not in DECISION_STATES:
            raise HTTPException(status_code=422, detail="Неизвестный статус")
        with _database(base_settings) as session:
            listing = session.get(ListingModel, listing_id)
            if listing is None:
                raise HTTPException(status_code=404, detail="Объявление не найдено")
            decision = session.scalar(
                select(ListingDecisionModel).where(
                    ListingDecisionModel.listing_id == listing_id
                )
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
    def listing_history(listing_id: int) -> Dict[str, Any]:
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
            return {
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
    def jobs() -> Dict[str, Any]:
        return coordinator.status()

    @app.get("/api/map")
    def map_points(profile_id: Optional[str] = None) -> Dict[str, Any]:
        config = load_web_config(base_settings.database_url)
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
            return {
                "items": [
                    {
                        "id": listing.id,
                        "title": listing.title,
                        "latitude": listing.latitude,
                        "longitude": listing.longitude,
                        "price_usd": listing.price_usd,
                        "area_sotok": listing.area_sotok,
                        "score": match.score,
                        "url": listing.canonical_url,
                        "decision": decision.state if decision else "new",
                    }
                    for listing, decision, match in rows
                ]
            }

    @app.get("/api/source-health")
    def source_health(profile_id: Optional[str] = None) -> Dict[str, Any]:
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
        return {"profile_id": selected_profile_id, "items": items}

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
            rows = session.execute(
                statement.order_by(ListingProfileModel.score.desc())
            ).all()
            points = [_trip_point(listing, decision) for listing, decision in rows]
        routes = build_trip_routes(
            points,
            max_points=payload.max_points_per_route,
            start=(payload.start_latitude, payload.start_longitude),
        )
        return {
            "points": len(points),
            "routes": [
                {
                    "index": index,
                    "url": google_maps_route_url(route),
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

    @app.get("/")
    @app.get("/{path:path}")
    def index(request: Request, path: str = "") -> FileResponse:
        if path.startswith("api/"):
            raise HTTPException(status_code=404)
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
) -> Dict[str, Any]:
    value: Dict[str, Any] = {
        "id": listing.id,
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
    return value


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


def _trip_point(
    listing: ListingModel,
    decision: ListingDecisionModel,
) -> TripPoint:
    return TripPoint(
        external_id=str(listing.id),
        status=decision.state,
        source=listing.source,
        listing_url=listing.canonical_url,
        district=listing.district or "",
        place=listing.title,
        price=f"${listing.price_usd:,.0f}" if listing.price_usd is not None else "",
        area=f"{listing.area_sotok:g}" if listing.area_sotok is not None else "",
        distance=(
            f"{listing.distance_mkad_km:g}"
            if listing.distance_mkad_km is not None
            else ""
        ),
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
        + math.cos(latitude_a)
        * math.cos(latitude_b)
        * math.sin(delta_longitude / 2) ** 2
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
    uvicorn.run(
        "app.web:create_app",
        factory=True,
        host=os.getenv("WEB_HOST", "127.0.0.1"),
        port=int(os.getenv("WEB_PORT", "8787")),
    )


if __name__ == "__main__":
    main()
