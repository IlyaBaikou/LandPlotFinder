from app.source_health import health_payloads, record_health_batch


def _observation(total: int, error: str = "") -> dict:
    return {
        "source": "kufar",
        "duration_ms": 1200,
        "http": {"requests": 3, "retries": 1, "rate_limits": 0},
        "quality": {
            "total": total,
            "completeness": {
                "title": 1.0 if total else 0.0,
                "url": 1.0 if total else 0.0,
                "price": 0.9 if total else 0.0,
                "area": 0.8 if total else 0.0,
                "coordinates": 0.5 if total else 0.0,
                "description": 0.7 if total else 0.0,
            },
        },
        "error": error,
    }


def test_source_health_detects_drop_and_schedules_retry(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'health.db'}"
    record_health_batch(database_url, "default", [_observation(20)])
    healthy = health_payloads(database_url, "default")[0]
    assert healthy["status"] == "healthy"
    assert healthy["baseline_item_count"] == 20

    record_health_batch(database_url, "default", [_observation(0)])
    degraded = health_payloads(database_url, "default")[0]
    assert degraded["status"] == "degraded"
    assert degraded["diagnostics"]["warnings"]

    record_health_batch(
        database_url,
        "default",
        [_observation(0, "HTTP 503")],
    )
    failed = health_payloads(database_url, "default")[0]
    assert failed["status"] == "error"
    assert failed["next_retry_at"] is not None
    assert failed["consecutive_failures"] == 1
