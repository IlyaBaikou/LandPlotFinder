from dataclasses import replace

from app.config import load_settings
from app.jobs import JobCoordinator
from app.web_config import save_search_profile, save_web_config


def test_scheduler_creates_independent_profile_jobs(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'jobs.db'}"
    base = replace(load_settings(), database_url=database_url)
    save_web_config(
        database_url,
        {"schedule_enabled": True, "schedule_interval_hours": 6},
    )
    second = save_search_profile(
        database_url,
        {
            "name": "Дачи",
            "schedule_enabled": True,
            "schedule_interval_hours": 12,
            "sources": ["realt"],
        },
    )
    coordinator = JobCoordinator(base)
    try:
        coordinator.start()
        jobs = {job["id"] for job in coordinator.status()["scheduled"]}
        assert "listing-scan:default" in jobs
        assert f"listing-scan:{second['id']}" in jobs
        assert "activity-check" in jobs
    finally:
        coordinator.shutdown()
