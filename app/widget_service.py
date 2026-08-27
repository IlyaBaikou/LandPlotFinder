from __future__ import annotations

import hashlib
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import ListingModel, WidgetInterestModel, WidgetLeadModel

LOGGER = logging.getLogger(__name__)
OTP_TTL_MINUTES = 10
OTP_COOLDOWN_SECONDS = 60
OTP_MAX_REQUESTS_PER_HOUR = 5
OTP_MAX_ATTEMPTS = 5
SESSION_TTL_HOURS = 24


class WidgetError(Exception):
    def __init__(self, detail: str, status_code: int = 400) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def request_code(
    session: Session,
    settings: Settings,
    phone_value: str,
    consent: bool,
    source_page: str = "",
    utm: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if not consent:
        raise WidgetError("Нужно согласие на обработку контактных данных", 422)
    phone = normalize_phone(phone_value)
    now = datetime.now(timezone.utc)
    lead = session.scalar(select(WidgetLeadModel).where(WidgetLeadModel.phone == phone))
    if lead is None:
        lead = WidgetLeadModel(phone=phone, consent_at=now, created_at=now, last_seen_at=now)
        session.add(lead)
    elif lead.last_code_sent_at:
        elapsed = (now - _aware(lead.last_code_sent_at)).total_seconds()
        if elapsed < OTP_COOLDOWN_SECONDS:
            wait = max(1, round(OTP_COOLDOWN_SECONDS - elapsed))
            raise WidgetError(f"Повторный код можно запросить через {wait} сек.", 429)

    window_started = _aware(lead.otp_window_started_at)
    if window_started is None or now - window_started >= timedelta(hours=1):
        lead.otp_window_started_at = now
        lead.otp_request_count = 0
    if lead.otp_request_count >= OTP_MAX_REQUESTS_PER_HOUR:
        raise WidgetError("Слишком много кодов. Попробуйте через час.", 429)

    code = f"{secrets.randbelow(1_000_000):06d}"
    lead.consent_at = now
    lead.last_seen_at = now
    lead.source_page = _short(source_page, 2_000) or None
    lead.utm = clean_mapping(utm or {}, 20)
    lead.otp_hash = _secret_hash(settings.widget_auth_secret, phone, code)
    lead.otp_expires_at = now + timedelta(minutes=OTP_TTL_MINUTES)
    lead.otp_attempts = 0
    lead.otp_request_count += 1
    lead.last_code_sent_at = now

    _deliver_code(settings, phone, code)
    result: Dict[str, Any] = {
        "ok": True,
        "phone": mask_phone(phone),
        "expires_in_seconds": OTP_TTL_MINUTES * 60,
        "mode": settings.widget_phone_auth_mode,
    }
    if settings.widget_phone_auth_mode == "demo":
        result["demo_code"] = code
    return result


def verify_code(
    session: Session,
    settings: Settings,
    phone_value: str,
    code_value: str,
) -> Dict[str, Any]:
    phone = normalize_phone(phone_value)
    code = re.sub(r"\D", "", code_value or "")
    if len(code) != 6:
        raise WidgetError("Введите шестизначный код", 422)
    lead = session.scalar(select(WidgetLeadModel).where(WidgetLeadModel.phone == phone))
    if lead is None or not lead.otp_hash or not lead.otp_expires_at:
        raise WidgetError("Сначала запросите код", 409)
    now = datetime.now(timezone.utc)
    if now > _aware(lead.otp_expires_at):
        raise WidgetError("Срок действия кода истёк. Запросите новый.", 410)
    if lead.otp_attempts >= OTP_MAX_ATTEMPTS:
        raise WidgetError("Слишком много попыток. Запросите новый код.", 429)
    lead.otp_attempts += 1
    expected = _secret_hash(settings.widget_auth_secret, phone, code)
    if not secrets.compare_digest(expected, lead.otp_hash):
        raise WidgetError("Неверный код", 401)

    token = secrets.token_urlsafe(32)
    lead.verified_at = now
    lead.last_seen_at = now
    lead.otp_hash = None
    lead.otp_expires_at = None
    lead.session_token_hash = _token_hash(token)
    lead.session_expires_at = now + timedelta(hours=SESSION_TTL_HOURS)
    return {
        "ok": True,
        "token": token,
        "phone": mask_phone(phone),
        "expires_in_seconds": SESSION_TTL_HOURS * 60 * 60,
    }


def verified_lead(session: Session, token: str) -> WidgetLeadModel:
    if not token:
        raise WidgetError("Подтвердите номер телефона", 401)
    lead = session.scalar(
        select(WidgetLeadModel).where(
            WidgetLeadModel.session_token_hash == _token_hash(token)
        )
    )
    now = datetime.now(timezone.utc)
    if lead is None or not lead.session_expires_at or now > _aware(lead.session_expires_at):
        raise WidgetError("Сессия истекла. Подтвердите телефон ещё раз.", 401)
    lead.last_seen_at = now
    return lead


def record_interest(
    session: Session,
    lead: WidgetLeadModel,
    listing_id: int,
    search_params: Optional[Dict[str, Any]] = None,
    source_page: str = "",
) -> Dict[str, Any]:
    listing = session.get(ListingModel, listing_id)
    if listing is None or not listing.active:
        raise WidgetError("Объявление больше недоступно", 404)
    interest = session.scalar(
        select(WidgetInterestModel).where(
            WidgetInterestModel.lead_id == lead.id,
            WidgetInterestModel.listing_id == listing.id,
        )
    )
    if interest is None:
        interest = WidgetInterestModel(
            lead_id=lead.id,
            listing_id=listing.id,
            listing_title=listing.title,
            listing_url=listing.canonical_url,
        )
        session.add(interest)
    interest.search_params = clean_mapping(search_params or {}, 30)
    interest.source_page = _short(source_page, 2_000) or lead.source_page
    return {
        "lead_id": lead.id,
        "phone": lead.phone,
        "listing_id": listing.id,
        "listing_title": listing.title,
        "listing_url": listing.canonical_url,
        "search_params": interest.search_params,
        "source_page": interest.source_page,
    }


def notify_lead(settings: Settings, payload: Dict[str, Any]) -> None:
    if not settings.widget_lead_webhook_url:
        return
    headers = {"Content-Type": "application/json"}
    if settings.widget_lead_webhook_token:
        headers["Authorization"] = f"Bearer {settings.widget_lead_webhook_token}"
    try:
        response = httpx.post(
            settings.widget_lead_webhook_url,
            json=payload,
            headers=headers,
            timeout=8,
        )
        response.raise_for_status()
    except Exception:
        LOGGER.exception("Widget lead webhook failed")


def normalize_phone(value: object) -> str:
    text = str(value or "").strip()
    digits = re.sub(r"\D", "", text)
    if digits.startswith("80") and len(digits) == 11:
        digits = "375" + digits[2:]
    elif len(digits) == 9:
        digits = "375" + digits
    if not 8 <= len(digits) <= 15 or digits.startswith("0"):
        raise WidgetError("Введите телефон в международном формате", 422)
    return f"+{digits}"


def mask_phone(phone: str) -> str:
    return f"{phone[:4]}•••{phone[-4:]}"


def clean_mapping(value: Dict[str, Any], limit: int) -> Dict[str, str]:
    result = {}
    for key, item in list(value.items())[:limit]:
        safe_key = re.sub(r"[^a-zA-Z0-9_-]", "", str(key))[:64]
        if safe_key:
            result[safe_key] = _short(item, 500)
    return result


def _deliver_code(settings: Settings, phone: str, code: str) -> None:
    mode = settings.widget_phone_auth_mode
    if mode == "demo":
        return
    if mode != "webhook" or not settings.widget_sms_webhook_url:
        raise WidgetError("Отправка SMS ещё не настроена", 503)
    headers = {"Content-Type": "application/json"}
    if settings.widget_sms_webhook_token:
        headers["Authorization"] = f"Bearer {settings.widget_sms_webhook_token}"
    try:
        response = httpx.post(
            settings.widget_sms_webhook_url,
            json={
                "phone": phone,
                "code": code,
                "message": f"Код входа LandPlotFinder: {code}",
            },
            headers=headers,
            timeout=8,
        )
        response.raise_for_status()
    except Exception as exc:
        raise WidgetError("Не удалось отправить код. Попробуйте позже.", 502) from exc


def _secret_hash(secret: str, phone: str, code: str) -> str:
    return hashlib.sha256(f"{secret}|{phone}|{code}".encode("utf-8")).hexdigest()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _short(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]
