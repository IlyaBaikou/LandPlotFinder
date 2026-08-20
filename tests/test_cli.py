from app.cli import _scheduled_job


def test_scheduled_jobs_use_separate_utc_slots() -> None:
    assert _scheduled_job(0) == "activity"
    assert _scheduled_job(12) == "activity"
    assert _scheduled_job(5) == "scan"
    assert _scheduled_job(11) == "scan"
    assert _scheduled_job(17) == "scan"
    assert _scheduled_job(6) == "preferred_scan"
    assert _scheduled_job(18) == "preferred_scan"
    assert _scheduled_job(2) == "locations"
    assert _scheduled_job(8) == "locations"
    assert _scheduled_job(14) == "locations"
    assert _scheduled_job(23) is None
    assert _scheduled_job(10) is None
