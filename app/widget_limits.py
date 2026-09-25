"""Shared, database-backed quotas for the public widget.

The counters are updated atomically so a second web replica cannot bypass a
quota. Subjects are keyed with an HMAC; raw phone numbers and IPs are not kept
in the counter table.
"""

from __future__ import annotations

import hmac
import ipaddress
from datetime import datetime, timezone
from hashlib import sha256
from typing import Optional

from fastapi import Request
from sqlalchemy import delete, text
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import WidgetRateLimitModel
from app.widget_service import WidgetError


def visitor_ip(request: Request) -> str:
    """Use Railway's documented client-IP header only on Railway requests."""
    if request.headers.get("x-railway-request-id"):
        supplied = request.headers.get("x-real-ip", "")
        try:
            return str(ipaddress.ip_address(supplied))
        except ValueError:
            pass
    return str(request.client.host if request.client else "unknown")


def consume(
    session: Session,
    settings: Settings,
    action: str,
    subject: str,
    limit: int,
    period_seconds: int,
    *,
    cost: int = 1,
    message: str = "Слишком много запросов. Попробуйте позже.",
    now: Optional[datetime] = None,
) -> None:
    if cost <= 0:
        return
    if limit < cost:
        raise WidgetError(message, 429)
    moment = now or datetime.now(timezone.utc)
    start_epoch = (int(moment.timestamp()) // period_seconds) * period_seconds
    expiry = datetime.fromtimestamp(start_epoch + period_seconds, timezone.utc)
    subject_hash = hmac.new(
        settings.widget_auth_secret.encode("utf-8"), subject.encode("utf-8"), sha256
    ).hexdigest()
    bucket_key = f"{action}:{start_epoch}:{subject_hash}"
    used = session.execute(
        text(
            """
            INSERT INTO widget_rate_limits (bucket_key, used, expires_at)
            VALUES (:bucket_key, :cost, :expiry)
            ON CONFLICT (bucket_key) DO UPDATE
            SET used = widget_rate_limits.used + excluded.used
            WHERE widget_rate_limits.used + excluded.used <= :limit
            RETURNING used
            """
        ),
        {"bucket_key": bucket_key, "cost": cost, "expiry": expiry, "limit": limit},
    ).scalar_one_or_none()
    if used is None:
        raise WidgetError(message, 429)


def prune_expired(session: Session, now: Optional[datetime] = None) -> None:
    cutoff = now or datetime.now(timezone.utc)
    session.execute(delete(WidgetRateLimitModel).where(WidgetRateLimitModel.expires_at <= cutoff))
