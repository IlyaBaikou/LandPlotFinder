from __future__ import annotations

import json
import logging
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List

import httpx

from app.client_telegram import process_client_update
from app.config import Settings, load_settings
from app.db import init_db, make_engine, make_session_factory, session_scope
from app.integrations.trips import GoogleTripsSink

LOGGER = logging.getLogger(__name__)
LISTING_URL = re.compile(
    r"https?://(?:re\.kufar\.by|(?:www\.)?realt\.by)/[^\s<>]+",
    re.IGNORECASE,
)


class TelegramWebhookApplication:
    def __init__(self, settings: Settings) -> None:
        if not settings.telegram_enabled:
            raise RuntimeError("Telegram is not configured")
        if not settings.sheets_enabled:
            raise RuntimeError("Google Sheets is not configured")
        if not settings.telegram_webhook_secret:
            raise RuntimeError("TELEGRAM_WEBHOOK_SECRET is not configured")
        self.settings = settings
        self.client = httpx.Client(timeout=settings.http_timeout_seconds)
        self.endpoint = f"https://api.telegram.org/bot{settings.telegram_bot_token}/"

    def close(self) -> None:
        self.client.close()

    def process(self, update: dict) -> None:
        private_message = (
            (update.get("callback_query") or {}).get("message") or update.get("message") or {}
        )
        if (
            self.settings.widget_client_bot_username
            and (private_message.get("chat") or {}).get("type") == "private"
        ):
            engine = make_engine(self.settings.database_url)
            try:
                init_db(engine)
                with session_scope(make_session_factory(engine)) as session:
                    process_client_update(session, self.settings.telegram_bot_token or "", update)
            finally:
                engine.dispose()
            return
        callback = update.get("callback_query")
        if callback:
            self._process_callback(callback)
            return
        message = update.get("message")
        if message:
            self._process_message(message)

    def _process_callback(self, callback: dict) -> None:
        message = callback.get("message") or {}
        if not self._allowed_chat(message):
            self._api(
                "answerCallbackQuery",
                callback_query_id=callback.get("id"),
                text="Эта кнопка работает только в нашей группе",
                show_alert=True,
            )
            return
        data = str(callback.get("data") or "")
        if data.startswith("selected|"):
            self._api(
                "answerCallbackQuery",
                callback_query_id=callback.get("id"),
                text="Уже в «Изучаем» и на карте",
            )
            return
        if not data.startswith("study|"):
            return
        self._api(
            "answerCallbackQuery",
            callback_query_id=callback.get("id"),
            text="Добавляю в «Изучаем»…",
        )
        reference = data[len("study|") :]
        sink = self._trips_sink()
        selected = sink.select_listing_url(reference)
        if not selected:
            raise ValueError(f"Listing is absent from Plots: {reference}")
        sink.sync()
        keyboard = _selected_keyboard(callback)
        if keyboard:
            self._api(
                "editMessageReplyMarkup",
                chat_id=message["chat"]["id"],
                message_id=message["message_id"],
                reply_markup={"inline_keyboard": keyboard},
            )

    def _process_message(self, message: dict) -> None:
        chat = message.get("chat") or {}
        if chat.get("type") == "private" and _command(str(message.get("text") or "")) == "/id":
            chat_id = chat.get("id")
            if chat_id is not None:
                self._api(
                    "sendMessage",
                    chat_id=chat_id,
                    text=f"Ваш chat ID: {chat_id}",
                )
            return
        if not self._allowed_chat(message):
            return
        text = str(message.get("text") or message.get("caption") or "")
        command = _command(text)
        if command in {"/trip", "/route"}:
            route_urls = self._trips_sink().route_urls()
            answer = (
                "\n".join(
                    f"🚗 Маршрут {index}: {url}" for index, url in enumerate(route_urls, start=1)
                )
                if route_urls
                else "Пока нет участков со статусом «Изучаем»."
            )
            self._reply(message, answer)
            return
        is_add = command in {"/add", "/study"}
        reply_to_bot = bool(
            ((message.get("reply_to_message") or {}).get("from") or {}).get("is_bot")
        )
        if not is_add and not reply_to_bot:
            return
        urls = LISTING_URL.findall(text)
        if not urls:
            return
        sink = self._trips_sink()
        selected = [
            external_id
            for external_id in (sink.select_listing_url(url) for url in urls)
            if external_id
        ]
        if selected:
            sink.sync()
            self._reply(
                message,
                "⭐ Добавлено в «Изучаем»: "
                + ", ".join(selected)
                + ".\nМаршруты обновлены — /trip",
            )
        else:
            self._reply(
                message,
                "Не нашёл это объявление в Plots. Если оно новое, сборщик "
                "подхватит его при следующем поиске.",
            )

    def report_error(self, update: dict) -> None:
        callback = update.get("callback_query") or {}
        message = callback.get("message") or update.get("message") or {}
        if self._allowed_chat(message):
            self._reply(
                message,
                "⚠️ Не смог добавить участок. Попробуйте ещё раз чуть позже.",
            )

    def _allowed_chat(self, message: dict) -> bool:
        chat_id = (message.get("chat") or {}).get("id")
        return str(chat_id or "") == str(self.settings.telegram_chat_id or "")

    def _reply(self, message: dict, text: str) -> None:
        self._api(
            "sendMessage",
            chat_id=self.settings.telegram_chat_id,
            reply_to_message_id=message.get("message_id"),
            text=text,
            disable_web_page_preview=True,
        )

    def _api(self, method: str, **payload: object) -> dict:
        response = self.client.post(self.endpoint + method, json=payload)
        response.raise_for_status()
        result = response.json()
        if not result.get("ok"):
            raise RuntimeError(f"Telegram {method} failed")
        return result.get("result") or {}

    def _trips_sink(self) -> GoogleTripsSink:
        return GoogleTripsSink(
            spreadsheet_id=self.settings.google_spreadsheet_id or "",
            credentials_info=self.settings.google_credentials_info() or {},
        )


def _command(text: str) -> str:
    first = text.strip().split(maxsplit=1)[0].lower() if text.strip() else ""
    return first.split("@", 1)[0]


def _selected_keyboard(callback: dict) -> List[List[Dict[str, str]]]:
    message = callback.get("message") or {}
    keyboard = ((message.get("reply_markup") or {}).get("inline_keyboard")) or []
    result = []
    for row in keyboard:
        updated_row = []
        for button in row:
            value = dict(button)
            if value.get("callback_data") == callback.get("data"):
                text = str(value.get("text") or "Приглянулось")
                value["text"] = re.sub(
                    r"^⭐ Приглянулось",
                    "✅ В изучаем",
                    text,
                )
                value["callback_data"] = (
                    "selected|" + str(callback.get("data") or "")[len("study|") :]
                )
            updated_row.append(value)
        result.append(updated_row)
    return result


class TelegramWebhookHandler(BaseHTTPRequestHandler):
    application: TelegramWebhookApplication

    def do_GET(self) -> None:
        if self.path == "/health":
            self._respond(200, {"ok": True, "service": "telegram-actions"})
        else:
            self._respond(404, {"ok": False})

    def do_POST(self) -> None:
        if self.path != "/telegram":
            self._respond(404, {"ok": False})
            return
        secret = self.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if secret != self.application.settings.telegram_webhook_secret:
            self._respond(401, {"ok": False})
            return
        length = int(self.headers.get("Content-Length") or 0)
        update = {}
        try:
            update = json.loads(self.rfile.read(length) or b"{}")
            self.application.process(update)
        except Exception:
            LOGGER.exception("Telegram update failed")
            message = (
                (update.get("callback_query") or {}).get("message") or update.get("message") or {}
            )
            if (
                self.application.settings.widget_client_bot_username
                and (message.get("chat") or {}).get("type") == "private"
            ):
                self._respond(503, {"ok": False})
                return
            try:
                self.application.report_error(update)
            except Exception:
                LOGGER.exception("Telegram error report failed")
        self._respond(200, {"ok": True})

    def log_message(self, message_format: str, *args: object) -> None:
        LOGGER.info(message_format, *args)

    def _respond(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> None:
    settings = load_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    application = TelegramWebhookApplication(settings)
    TelegramWebhookHandler.application = application
    port = int(os.getenv("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), TelegramWebhookHandler)
    LOGGER.info("Telegram webhook listening on port %s", port)
    try:
        server.serve_forever()
    finally:
        application.close()
        server.server_close()


if __name__ == "__main__":
    main()
