from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import select

from app.db import init_db, make_engine, make_session_factory, session_scope
from app.domain import NormalizedListing
from app.models import SourceHealthModel


def quality_metrics(listings: Iterable[NormalizedListing]) -> Dict[str, Any]:
    values = list(listings)
    total = len(values)
    fields = {
        "title": lambda item: bool(item.title.strip()),
        "url": lambda item: bool(item.canonical_url.strip()),
        "price": lambda item: item.price_usd is not None,
        "area": lambda item: item.area_sotok is not None,
        "coordinates": lambda item: item.latitude is not None and item.longitude is not None,
        "description": lambda item: len(item.description.strip()) >= 20,
    }
    completeness = {
        field: round(sum(check(item) for item in values) / total, 3) if total else 0.0
        for field, check in fields.items()
    }
    return {"total": total, "completeness": completeness}


def record_health_batch(
    database_url: str,
    profile_id: str,
    observations: Iterable[Dict[str, Any]],
) -> None:
    engine = make_engine(database_url)
    init_db(engine)
    factory = make_session_factory(engine)
    now = datetime.now(timezone.utc)
    try:
        with session_scope(factory) as session:
            for observation in observations:
                source = str(observation["source"])
                health = session.scalar(
                    select(SourceHealthModel).where(
                        SourceHealthModel.source == source,
                        SourceHealthModel.profile_id == profile_id,
                    )
                )
                if health is None:
                    health = SourceHealthModel(source=source, profile_id=profile_id)
                    session.add(health)
                    session.flush()
                _apply_observation(health, observation, now)
    finally:
        engine.dispose()


def health_payloads(database_url: str, profile_id: str) -> List[Dict[str, Any]]:
    engine = make_engine(database_url)
    init_db(engine)
    factory = make_session_factory(engine)
    try:
        with session_scope(factory) as session:
            rows = list(
                session.scalars(
                    select(SourceHealthModel)
                    .where(SourceHealthModel.profile_id == profile_id)
                    .order_by(SourceHealthModel.source)
                )
            )
            return [health_payload(row) for row in rows]
    finally:
        engine.dispose()


def health_payload(value: SourceHealthModel) -> Dict[str, Any]:
    return {
        "source": value.source,
        "profile_id": value.profile_id,
        "status": value.status,
        "last_attempt_at": _iso(value.last_attempt_at),
        "last_success_at": _iso(value.last_success_at),
        "last_failure_at": _iso(value.last_failure_at),
        "next_retry_at": _iso(value.next_retry_at),
        "last_duration_ms": value.last_duration_ms,
        "last_item_count": value.last_item_count,
        "baseline_item_count": round(value.baseline_item_count, 1),
        "consecutive_failures": value.consecutive_failures,
        "consecutive_degraded": value.consecutive_degraded,
        "last_error": value.last_error,
        "diagnostics": value.diagnostics or {},
    }


def _apply_observation(
    health: SourceHealthModel,
    observation: Dict[str, Any],
    now: datetime,
) -> None:
    error = str(observation.get("error") or "").strip()
    metrics = dict(observation.get("quality") or {})
    total = int(metrics.get("total") or 0)
    previous = dict(health.diagnostics or {})
    warnings = _diagnostic_warnings(total, health.baseline_item_count, metrics, previous)

    health.last_attempt_at = now
    health.last_duration_ms = int(observation.get("duration_ms") or 0)
    health.last_item_count = total
    metrics["warnings"] = warnings
    metrics["http"] = dict(observation.get("http") or {})
    health.diagnostics = metrics

    if error:
        health.status = "error"
        health.consecutive_failures += 1
        health.last_failure_at = now
        health.last_error = error[:4000]
        health.next_retry_at = now + timedelta(
            minutes=min(5 * (2 ** max(0, health.consecutive_failures - 1)), 120)
        )
        return

    health.last_success_at = now
    health.last_error = None
    health.next_retry_at = None
    health.consecutive_failures = 0
    if warnings:
        health.status = "degraded"
        health.consecutive_degraded += 1
    elif total == 0:
        health.status = "empty"
        health.consecutive_degraded = 0
    else:
        health.status = "healthy"
        health.consecutive_degraded = 0
    if total > 0:
        health.baseline_item_count = (
            float(total)
            if health.baseline_item_count <= 0
            else health.baseline_item_count * 0.8 + float(total) * 0.2
        )


def _diagnostic_warnings(
    total: int,
    baseline: float,
    metrics: Dict[str, Any],
    previous: Dict[str, Any],
) -> List[str]:
    warnings: List[str] = []
    if baseline >= 5 and total < max(1, baseline * 0.2):
        warnings.append(
            f"Количество результатов резко снизилось: {total} при обычном уровне {baseline:.0f}"
        )
    completeness = dict(metrics.get("completeness") or {})
    previous_completeness = dict(previous.get("completeness") or {})
    for field, label in {
        "title": "заголовки",
        "url": "ссылки",
        "price": "цены",
        "area": "площади",
        "coordinates": "координаты",
        "description": "описания",
    }.items():
        current = float(completeness.get(field) or 0)
        old = float(previous_completeness.get(field) or 0)
        if total >= 3 and old >= 0.4 and current < old * 0.3:
            warnings.append(
                f"Похоже, изменилась структура страницы: пропали {label} "
                f"({current:.0%} вместо {old:.0%})"
            )
    if total >= 3 and completeness.get("title", 0) < 0.8:
        warnings.append("У большинства карточек не распознан заголовок")
    if total >= 3 and completeness.get("url", 0) < 0.8:
        warnings.append("У большинства карточек не распознана ссылка")
    return warnings


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None
