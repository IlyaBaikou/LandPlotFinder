from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import load_settings
from app.web import create_app
from app.widget_service import SMSBY_SEND_URL, WidgetError, _deliver_code


def _settings():
    return replace(
        load_settings(),
        widget_phone_auth_mode="smsby",
        widget_smsby_api_key="test-api-key",
        widget_smsby_alphaname_id="123",
    )


def test_smsby_sends_branded_code_to_belarus(monkeypatch) -> None:
    sent = []

    def fake_post(url, **kwargs):
        sent.append((url, kwargs))
        return httpx.Response(
            200,
            json={"sms_id": 2197871, "status": "NEW", "parts": 1},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr("app.widget_service.httpx.post", fake_post)
    _deliver_code(_settings(), "+375291234567", "123456")

    assert len(sent) == 1
    assert sent[0][0] == SMSBY_SEND_URL
    assert sent[0][1]["params"] == {
        "token": "test-api-key",
        "phone": "+375291234567",
        "message": "ЛидерСтрой: код 123456. Никому не сообщайте его.",
        "alphaname_id": "123",
    }


@pytest.mark.parametrize("phone", ["+12025550123", "+37529123456", "+3752912345678"])
def test_smsby_rejects_non_belarusian_numbers_before_send(monkeypatch, phone) -> None:
    def unexpected_post(*args, **kwargs):
        raise AssertionError("SMS must not be sent")

    monkeypatch.setattr("app.widget_service.httpx.post", unexpected_post)
    with pytest.raises(WidgetError) as error:
        _deliver_code(_settings(), phone, "123456")
    assert error.value.status_code == 422


def test_smsby_requires_credentials(monkeypatch) -> None:
    settings = replace(_settings(), widget_smsby_api_key=None)
    with pytest.raises(WidgetError) as error:
        _deliver_code(settings, "+375291234567", "123456")
    assert error.value.status_code == 503


@pytest.mark.parametrize(
    "status_code,body",
    [
        (429, {"error": "rate limit"}),
        (200, {"status": "ERROR", "message": "no funds"}),
        (200, {"sms_id": 0, "status": "NEW"}),
        (200, {"sms_id": 1, "status": "ERROR"}),
    ],
)
def test_smsby_failure_is_not_reported_as_sent(monkeypatch, status_code, body) -> None:
    def fake_post(url, **kwargs):
        return httpx.Response(
            status_code,
            json=body,
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr("app.widget_service.httpx.post", fake_post)
    with pytest.raises(WidgetError) as error:
        _deliver_code(_settings(), "+375291234567", "123456")
    assert error.value.status_code == 502
    assert "test-api-key" not in error.value.detail


def test_smsby_timeout_is_not_reported_as_sent(monkeypatch) -> None:
    def fake_post(url, **kwargs):
        raise httpx.ReadTimeout("provider timeout")

    monkeypatch.setattr("app.widget_service.httpx.post", fake_post)
    with pytest.raises(WidgetError) as error:
        _deliver_code(_settings(), "+375291234567", "123456")
    assert error.value.status_code == 502


def test_smsby_request_endpoint_never_returns_the_code(monkeypatch, tmp_path) -> None:
    def fake_post(url, **kwargs):
        return httpx.Response(
            200,
            json={"sms_id": 5, "status": "NEW"},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr("app.widget_service.httpx.post", fake_post)
    settings = replace(_settings(), database_url=f"sqlite:///{tmp_path / 'smsby.db'}")
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/public/widget/auth/request-code",
            json={"phone": "+375291234567", "consent": True},
        )
    assert response.status_code == 200
    assert response.json()["mode"] == "smsby"
    assert "demo_code" not in response.json()
