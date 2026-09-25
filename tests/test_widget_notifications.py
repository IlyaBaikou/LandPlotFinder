from dataclasses import replace

from app.config import load_settings
from app.widget_service import notify_lead


def test_widget_telegram_only_sends_explicit_interest(monkeypatch) -> None:
    settings = replace(
        load_settings(),
        widget_telegram_bot_token="test-token",
        widget_telegram_chat_id="12345",
        widget_lead_webhook_url=None,
    )
    calls = []

    class SuccessfulResponse:
        def raise_for_status(self) -> None:
            pass

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return SuccessfulResponse()

    monkeypatch.setattr("app.widget_service.httpx.post", fake_post)
    notify_lead(settings, {"event": "lead_verified", "phone": "+375291234567"})
    assert calls == []

    notify_lead(
        settings,
        {
            "event": "listing_interest_marked",
            "phone": "+375291234567",
            "reference": "LP-1234567890",
            "listing_title": "Участок\nв Новосёлках",
            "search_params": {
                "q": "Логойское",
                "max_price_usd": "40000",
                "min_area_sotok": "9",
                "max_area_sotok": "15",
                "max_distance_km": "30",
            },
        },
    )
    assert len(calls) == 1
    url, kwargs = calls[0]
    assert url.endswith("/bottest-token/sendMessage")
    assert kwargs["json"]["chat_id"] == "12345"
    assert "Участок в Новосёлках" in kwargs["json"]["text"]
    assert "Участок\nв Новосёлках" not in kwargs["json"]["text"]
    assert "LP-1234567890" in kwargs["json"]["text"]
    assert "Логойское, до $40000, 9–15 сот., до 30 км" in kwargs["json"]["text"]


def test_widget_telegram_error_does_not_log_token(monkeypatch, caplog) -> None:
    settings = replace(
        load_settings(),
        widget_telegram_bot_token="test-secret-token",
        widget_telegram_chat_id="12345",
        widget_lead_webhook_url=None,
    )

    def failing_post(url, **kwargs):
        raise RuntimeError(f"failed at {url}")

    monkeypatch.setattr("app.widget_service.httpx.post", failing_post)
    notify_lead(settings, {"event": "listing_interest_marked", "phone": "+375291234567"})
    assert "Widget Telegram notification failed" in caplog.text
    assert "test-secret-token" not in caplog.text
