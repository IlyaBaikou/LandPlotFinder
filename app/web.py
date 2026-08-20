from __future__ import annotations

import csv
import io
import logging
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
from sqlalchemy import func, or_, select

from app.config import Settings, load_settings
from app.db import init_db, make_engine, make_session_factory, session_scope
from app.jobs import JobCoordinator
from app.models import (
    ListingActivityModel,
    ListingDecisionModel,
    ListingModel,
    ScanRunModel,
)
from app.web_config import (
    load_web_config,
    public_web_config,
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
        version="1.0.0",
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

    @app.get("/api/summary")
    def summary() -> Dict[str, Any]:
        with _database(base_settings) as session:
            active = session.scalar(
                select(func.count()).select_from(ListingModel).where(ListingModel.active)
            ) or 0
            archived = session.scalar(
                select(func.count()).select_from(ListingModel).where(~ListingModel.active)
            ) or 0
            liked = session.scalar(
                select(func.count())
                .select_from(ListingDecisionModel)
                .where(ListingDecisionModel.state.in_({"liked", "studying", "trip"}))
            ) or 0
            high_score = session.scalar(
                select(func.count())
                .select_from(ListingModel)
                .where(ListingModel.active, ListingModel.score >= 80)
            ) or 0
            latest = session.scalar(
                select(ScanRunModel).order_by(ScanRunModel.started_at.desc()).limit(1)
            )
            return {
                "active": active,
                "archived": archived,
                "selected": liked,
                "high_score": high_score,
                "configured": load_web_config(base_settings.database_url)["configured"],
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
    ) -> Dict[str, Any]:
        with _database(base_settings) as session:
            statement = (
                select(ListingModel, ListingDecisionModel, ListingActivityModel)
                .outerjoin(
                    ListingDecisionModel,
                    ListingDecisionModel.listing_id == ListingModel.id,
                )
                .outerjoin(
                    ListingActivityModel,
                    ListingActivityModel.listing_id == ListingModel.id,
                )
            )
            conditions = [ListingModel.score >= min_score]
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
                    ListingModel.score.desc(),
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
    def listing_detail(listing_id: int) -> Dict[str, Any]:
        with _database(base_settings) as session:
            row = session.execute(
                select(ListingModel, ListingDecisionModel, ListingActivityModel)
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
            decision.state = payload.state
            decision.note = payload.note.strip()
            session.flush()
            return {
                "listing_id": listing_id,
                "state": decision.state,
                "note": decision.note,
            }

    @app.post("/api/jobs/{kind}", status_code=status.HTTP_202_ACCEPTED)
    def start_job(kind: str) -> JSONResponse:
        if kind not in {"scan", "activity"}:
            raise HTTPException(status_code=404, detail="Неизвестная задача")
        config = load_web_config(base_settings.database_url)
        if not config["configured"]:
            raise HTTPException(status_code=409, detail="Сначала завершите настройку")
        started = (
            coordinator.request_scan()
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
    def map_points() -> Dict[str, Any]:
        with _database(base_settings) as session:
            rows = session.execute(
                select(ListingModel, ListingDecisionModel)
                .outerjoin(
                    ListingDecisionModel,
                    ListingDecisionModel.listing_id == ListingModel.id,
                )
                .where(
                    ListingModel.active.is_(True),
                    ListingModel.latitude.is_not(None),
                    ListingModel.longitude.is_not(None),
                )
                .order_by(ListingModel.score.desc())
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
                        "score": listing.score,
                        "url": listing.canonical_url,
                        "decision": decision.state if decision else "new",
                    }
                    for listing, decision in rows
                ]
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
        "score": listing.score,
        "match_status": listing.status,
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
                "reasons": listing.reasons or [],
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
