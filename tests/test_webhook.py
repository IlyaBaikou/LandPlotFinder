from app.webhook import TelegramWebhookApplication, _command, _selected_keyboard


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

    assert keyboard[0] == [{
        "text": "✅ В изучаем",
        "callback_data": "selected|kufar:42",
    }]
    assert keyboard[1] == [
        {"text": "⭐ Приглянулось", "callback_data": "study|realt:7"}
    ]
