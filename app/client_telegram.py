from __future__ import annotations

import hashlib
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import init_db, make_engine, make_session_factory, session_scope
from app.models import (
    ListingModel,
    WidgetTelegramDeliveryModel,
    WidgetTelegramInviteModel,
    WidgetTelegramSubscriptionModel,
)

LOGGER = logging.getLogger(__name__)
INVITE_TTL = timedelta(hours=48)
CONSENT_VERSION = "daily-listings-v1"


def create_invite(session: Session, lead_id: int, criteria: Dict[str, str], username: str) -> str:
    bot_name = username.strip().lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", bot_name):
        raise ValueError("Telegram bot username is not configured")
    token = secrets.token_urlsafe(24)
    session.add(
        WidgetTelegramInviteModel(
            lead_id=lead_id,
            token_hash=_hash(token),
            criteria=dict(criteria),
            expires_at=datetime.now(timezone.utc) + INVITE_TTL,
        )
    )
    return f"https://t.me/{bot_name}?start={token}"


def process_client_update(session: Session, bot_token: str, update: Dict[str, Any]) -> bool:
    """Handle private-client commands; return False for legacy group updates."""
    callback = update.get("callback_query") or {}
    message = callback.get("message") or update.get("message") or {}
    chat = message.get("chat") or {}
    if chat.get("type") != "private":
        return False
    chat_id = str(chat.get("id") or "")
    if not chat_id:
        return True
    if callback:
        _process_choice(session, bot_token, chat_id, callback)
        return True
    text = str(message.get("text") or "").strip()
    command, _, argument = text.partition(" ")
    command = command.lower().split("@", 1)[0]
    if command == "/start" and argument.strip():
        _start_invite(session, bot_token, chat_id, argument.strip())
    elif command == "/id":
        _send(bot_token, chat_id, f"Ваш chat ID: {chat_id}")
    elif command in {"/stop", "/pause", "/resume", "/status"}:
        _subscription_command(session, bot_token, chat_id, command)
    elif command == "/start":
        _send(
            bot_token,
            chat_id,
            "🏡 ЛидерСтрой · Умный подбор участка\n\n"
            "Откройте приглашение из виджета, чтобы получить подборку "
            "именно по вашим фильтрам.",
        )
    return True


def _start_invite(session: Session, bot_token: str, chat_id: str, token: str) -> None:
    invite = session.scalar(
        select(WidgetTelegramInviteModel).where(
            WidgetTelegramInviteModel.token_hash == _hash(token)
        )
    )
    if (
        invite is None
        or invite.consumed_at
        or _aware(invite.expires_at) < datetime.now(timezone.utc)
    ):
        _send(bot_token, chat_id, "Ссылка устарела. Откройте виджет и запросите новую.")
        return
    if invite.chat_id and invite.chat_id != chat_id:
        _send(
            bot_token, chat_id, "Эта ссылка уже открыта в другом чате. Запросите новую в виджете."
        )
        return
    invite.chat_id = chat_id
    _send(
        bot_token,
        chat_id,
        "🏡 ЛидерСтрой · Умный подбор участка\n\n"
        f"Ваш поиск: {_criteria_text(invite.criteria)}.\n\n"
        "Можем прислать текущие варианты со ссылками на объявления. "
        "Если подпишетесь, раз в день будем присылать только новые совпадения "
        "по этим фильтрам. Без новых вариантов писать не будем. "
        "Подписку можно приостановить командой /pause или отключить командой /stop.\n\n"
        "Выберите, что вам подходит:",
        reply_markup={
            "inline_keyboard": [
                [
                    {
                        "text": "✅ Подписаться и получить варианты",
                        "callback_data": f"client:subscribe:{invite.id}",
                    }
                ],
                [
                    {
                        "text": "🔗 Только текущие варианты",
                        "callback_data": f"client:once:{invite.id}",
                    }
                ],
            ]
        },
    )


def _process_choice(
    session: Session, bot_token: str, chat_id: str, callback: Dict[str, Any]
) -> None:
    data = str(callback.get("data") or "")
    parts = data.split(":")
    if (
        len(parts) != 3
        or parts[0] != "client"
        or parts[1] not in {"subscribe", "once"}
        or not parts[2].isdigit()
    ):
        return
    invite = session.get(WidgetTelegramInviteModel, int(parts[2]))
    now = datetime.now(timezone.utc)
    if (
        invite is None
        or invite.chat_id != chat_id
        or invite.consumed_at
        or _aware(invite.expires_at) < now
    ):
        _answer(bot_token, callback, "Ссылка устарела. Запросите новую в виджете.")
        return
    subscription: Optional[WidgetTelegramSubscriptionModel] = None
    if parts[1] == "subscribe":
        other = session.scalar(
            select(WidgetTelegramSubscriptionModel).where(
                WidgetTelegramSubscriptionModel.chat_id == chat_id,
                WidgetTelegramSubscriptionModel.lead_id != invite.lead_id,
            )
        )
        if other:
            _answer(bot_token, callback, "Этот чат уже привязан к другому поиску.")
            return
        subscription = session.scalar(
            select(WidgetTelegramSubscriptionModel).where(
                WidgetTelegramSubscriptionModel.lead_id == invite.lead_id
            )
        )
        if subscription is None:
            subscription = WidgetTelegramSubscriptionModel(
                lead_id=invite.lead_id,
                chat_id=chat_id,
                criteria=dict(invite.criteria),
                status="active",
                consent_at=now,
                consent_version=CONSENT_VERSION,
            )
            session.add(subscription)
        else:
            subscription.chat_id = chat_id
            subscription.criteria = dict(invite.criteria)
            subscription.status = "active"
            subscription.consent_at = now
            subscription.consent_version = CONSENT_VERSION
        session.flush()
    listings = _matches(session, invite.criteria, limit=3)
    _send(bot_token, chat_id, _welcome_text(invite.criteria, listings, bool(subscription)))
    if subscription:
        for listing in listings:
            _remember_delivery(session, subscription.id, listing.id, now)
    invite.consumed_at = now
    _answer(bot_token, callback, "Готово")


def _subscription_command(session: Session, bot_token: str, chat_id: str, command: str) -> None:
    subscription = session.scalar(
        select(WidgetTelegramSubscriptionModel).where(
            WidgetTelegramSubscriptionModel.chat_id == chat_id
        )
    )
    if subscription is None:
        _send(bot_token, chat_id, "Активной подписки нет. Для подключения откройте виджет.")
        return
    if command == "/stop":
        subscription.status = "stopped"
        answer = (
            "Рассылка отключена. Ваши контакты и история остались "
            "в закрытой панели. Новые подборки и напоминания не придут."
        )
    elif command == "/pause":
        subscription.status = "paused"
        answer = "Подборка на паузе. Чтобы возобновить, отправьте /resume."
    elif command == "/resume":
        if subscription.status == "stopped":
            answer = "Вы отписались. Для новой подписки откройте виджет и подтвердите её заново."
        else:
            subscription.status = "active"
            answer = "Подборка снова активна. Новые совпадения придут раз в день."
    else:
        answer = f"Статус: {subscription.status}. Поиск: {_criteria_text(subscription.criteria)}."
    _send(bot_token, chat_id, answer)


def send_daily_digests(settings: Settings) -> int:
    token = settings.widget_client_bot_token
    if not token:
        return 0
    engine = make_engine(settings.database_url)
    try:
        init_db(engine)
        factory = make_session_factory(engine)
        with session_scope(factory) as session:
            ids = session.scalars(
                select(WidgetTelegramSubscriptionModel.id).where(
                    WidgetTelegramSubscriptionModel.status == "active"
                )
            ).all()
        sent = 0
        for subscription_id in ids:
            try:
                with session_scope(factory) as session:
                    subscription = session.get(WidgetTelegramSubscriptionModel, subscription_id)
                    if subscription is None or subscription.status != "active":
                        continue
                    now = datetime.now(timezone.utc)
                    if (
                        subscription.last_digest_at
                        and _aware(subscription.last_digest_at).date() == now.date()
                    ):
                        continue
                    delivered_ids = set(
                        session.scalars(
                            select(WidgetTelegramDeliveryModel.listing_id).where(
                                WidgetTelegramDeliveryModel.subscription_id == subscription.id
                            )
                        ).all()
                    )
                    listings = [
                        listing
                        for listing in _matches(session, subscription.criteria, limit=100)
                        if listing.id not in delivered_ids
                        and _aware(listing.first_seen_at) >= _aware(subscription.consent_at)
                    ][:5]
                    if listings:
                        _send(
                            token,
                            subscription.chat_id,
                            _digest_text(subscription.criteria, listings),
                        )
                        for listing in listings:
                            _remember_delivery(session, subscription.id, listing.id, now)
                        sent += 1
                    subscription.last_digest_at = now
            except Exception as exc:
                LOGGER.error("Client Telegram digest failed (%s)", type(exc).__name__)
        return sent
    finally:
        engine.dispose()


def _matches(session: Session, criteria: Dict[str, Any], limit: int) -> List[ListingModel]:
    # Reuse the widget's exact query rather than silently broadening client filters.
    from app.models import ServiceStateModel
    from app.web import _widget_listing_statement
    from app.web_config import get_profile, normalize_web_config

    def number(name: str) -> Optional[float]:
        try:
            return float(criteria[name]) if criteria.get(name) not in (None, "") else None
        except (TypeError, ValueError):
            return None

    state = session.get(ServiceStateModel, "web_config")
    config = normalize_web_config(dict(state.payload or {}) if state else {})
    profile_id = str(criteria.get("profile_id") or "default")
    try:
        profile = get_profile(config, profile_id)
    except KeyError:
        return []
    statement = _widget_listing_statement(
        profile_id,
        profile["sources"],
        str(criteria.get("q") or ""),
        number("max_price_usd"),
        number("min_area_sotok"),
        number("max_area_sotok"),
        number("max_distance_km"),
        criteria.get("electricity") == "true",
        criteria.get("gas") == "true",
        criteria.get("water") == "true",
        criteria.get("sewerage") == "true",
    )
    return [
        row[0]
        for row in session.execute(
            statement.order_by(ListingModel.first_seen_at.desc(), ListingModel.id.desc()).limit(
                limit
            )
        ).all()
    ]


def _welcome_text(criteria: Dict[str, Any], listings: List[ListingModel], subscribed: bool) -> str:
    header = "🏡 ЛидерСтрой · Умный подбор участка\n\n"
    intro = f"Ваш поиск: {_criteria_text(criteria)}.\n\n"
    choices = (
        "Вот подходящие варианты:\n" + "\n".join(_listing_line(item) for item in listings)
        if listings
        else "Пока нет актуальных вариантов по этим параметрам."
    )
    tail = (
        "\n\nПланируете строительство? Поможем оценить участок, "
        "подготовить проект и построить дом под ключ.\n"
        "ЛидерСтрой: https://lider-stroy.by/\n"
        "Консультация: +375 33 377 75 76"
    )
    if subscribed:
        tail += "\n\nНовые варианты придут одной подборкой в день. Пауза — /pause, отписка — /stop."
    return header + intro + choices + tail


def _digest_text(criteria: Dict[str, Any], listings: List[ListingModel]) -> str:
    return (
        "🏡 ЛидерСтрой · Новые участки по вашему поиску\n"
        f"{_criteria_text(criteria)}\n\n"
        + "\n".join(_listing_line(item) for item in listings)
        + "\n\nПланируете строить дом? Обсудим участок, проект и строительство: "
        "https://lider-stroy.by/ · +375 33 377 75 76" + "\nПауза — /pause · Отписаться — /stop"
    )


def _listing_line(listing: ListingModel) -> str:
    title = " ".join((listing.title or "Участок").split())[:100]
    facts = []
    if listing.price_usd is not None:
        facts.append(f"${listing.price_usd:,.0f}".replace(",", " "))
    if listing.area_sotok is not None:
        facts.append(f"{listing.area_sotok:g} сот.")
    if listing.distance_mkad_km is not None:
        facts.append(f"{listing.distance_mkad_km:g} км до МКАД")
    url = listing.canonical_url if listing.canonical_url.startswith(("https://", "http://")) else ""
    return f"• {title} — {', '.join(facts)}\n{url}".strip()


def _criteria_text(criteria: Dict[str, Any]) -> str:
    parts = []
    if criteria.get("q"):
        parts.append(str(criteria["q"])[:60])
    if criteria.get("max_price_usd"):
        parts.append(f"до ${criteria['max_price_usd']}")
    if criteria.get("min_area_sotok") or criteria.get("max_area_sotok"):
        parts.append(
            f"{criteria.get('min_area_sotok') or '?'}–{criteria.get('max_area_sotok') or '?'} сот."
        )
    if criteria.get("max_distance_km"):
        parts.append(f"до {criteria['max_distance_km']} км от МКАД")
    return ", ".join(parts) if parts else "выбранные параметры"


def _remember_delivery(
    session: Session, subscription_id: int, listing_id: int, now: datetime
) -> None:
    exists = session.scalar(
        select(WidgetTelegramDeliveryModel.id).where(
            WidgetTelegramDeliveryModel.subscription_id == subscription_id,
            WidgetTelegramDeliveryModel.listing_id == listing_id,
        )
    )
    if not exists:
        session.add(
            WidgetTelegramDeliveryModel(
                subscription_id=subscription_id, listing_id=listing_id, sent_at=now
            )
        )


def _send(bot_token: str, chat_id: str, text: str, **extras: Any) -> None:
    try:
        response = httpx.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text[:4000],
                "disable_web_page_preview": True,
                **extras,
            },
            timeout=10,
        )
        response.raise_for_status()
    except Exception as exc:
        raise RuntimeError(f"Telegram send failed ({type(exc).__name__})") from None


def _answer(bot_token: str, callback: Dict[str, Any], text: str) -> None:
    if not callback.get("id"):
        return
    try:
        httpx.post(
            f"https://api.telegram.org/bot{bot_token}/answerCallbackQuery",
            json={"callback_query_id": callback["id"], "text": text},
            timeout=5,
        ).raise_for_status()
    except Exception as exc:
        LOGGER.warning("Telegram callback answer failed (%s)", type(exc).__name__)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
