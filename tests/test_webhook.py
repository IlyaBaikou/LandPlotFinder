from dataclasses import replace

from app.config import load_settings
from app.webhook import TelegramWebhookApplication, _command, _selected_keyboard


def test_client_bot_routes_private_updates_without_changing_group_flow(
    tmp_path, monkeypatch
) -> None:
    app = TelegramWebhookApplication.__new__(TelegramWebhookApplication)
    app.settings = replace(
        load_settings(),
        database_url=f"sqlite:///{tmp_path / 'webhook.db'}",
        telegram_bot_token="test-token",
        widget_client_bot_username="wormiefinder_bot",
    )
    private_updates = []
    group_updates = []
    monkeypatch.setattr(
        "app.webhook.process_client_update",
        lambda _session, _token, update: private_updates.append(update),
    )
    app._process_message = lambda message: group_updates.append(message)
    private = {"message": {"chat": {"id": 123, "type": "private"}, "text": "/id"}}
    group = {"message": {"chat": {"id": -123, "type": "supergroup"}, "text": "/trip"}}

    app.process(private)
    app.process(group)

    assert private_updates == [private]
    assert group_updates == [group["message"]]


def test_private_id_command_replies_without_group_access() -> None:
    app = TelegramWebhookApplication.__new__(TelegramWebhookApplication)
    calls = []
    app._api = lambda method, **kwargs: calls.append((method, kwargs))

    app._process_message({"chat": {"id": 12345, "type": "private"}, "text": "/id"})

    assert calls == [("sendMessage", {"chat_id": 12345, "text": "Ваш chat ID: 12345"})]


def test_command_removes_bot_username() -> None:
    assert _command("/add@landplotfinder_bot https://example.test") == "/add"


def test_selected_keyboard_only_changes_clicked_button() -> None:
    callback = {
        "data": "study|kufar:42",
        "message": {
            "reply_markup": {
                "inline_keyboard": [
                    [{"text": "⭐ Приглянулось", "callback_data": "study|kufar:42"}],
                    [{"text": "⭐ Приглянулось", "callback_data": "study|realt:7"}],
                ]
            }
        },
    }

    keyboard = _selected_keyboard(callback)

    assert keyboard[0] == [
        {
            "text": "✅ В изучаем",
            "callback_data": "selected|kufar:42",
        }
    ]
    assert keyboard[1] == [{"text": "⭐ Приглянулось", "callback_data": "study|realt:7"}]
