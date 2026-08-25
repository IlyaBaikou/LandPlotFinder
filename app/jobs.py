from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Sequence

from apscheduler.schedulers.background import BackgroundScheduler

from app.activity import ListingActivityChecker
from app.config import Settings
from app.orchestrator import Scanner
from app.web_config import get_profile, load_web_config, runtime_settings

LOGGER = logging.getLogger(__name__)


class JobCoordinator:
    def __init__(self, base_settings: Settings) -> None:
        self.base_settings = base_settings
        self.scheduler = BackgroundScheduler(timezone="UTC")
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="landplot-job")
        self.lock = threading.Lock()
        self.state: Dict[str, Any] = {
            "running": False,
            "kind": None,
            "profile_id": None,
            "started_at": None,
            "completed_at": None,
            "last_result": None,
            "last_error": None,
        }

    def start(self) -> None:
        if not self.scheduler.running:
            self.scheduler.start()
        self.configure()

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        self.executor.shutdown(wait=False, cancel_futures=True)

    def configure(self) -> None:
        self.scheduler.remove_all_jobs()
        config = load_web_config(self.base_settings.database_url)
        if not config["configured"]:
            return
        enabled_intervals = []
        for profile in config["profiles"]:
            if not profile["enabled"] or not profile["schedule_enabled"]:
                continue
            interval = int(profile["schedule_interval_hours"])
            enabled_intervals.append(interval)
            self.scheduler.add_job(
                self.request_scan,
                "interval",
                kwargs={"profile_id": profile["id"]},
                hours=interval,
                id=f"listing-scan:{profile['id']}",
                name=f"Поиск: {profile['name']}",
                replace_existing=True,
                coalesce=True,
                max_instances=1,
                next_run_time=datetime.now(timezone.utc) + timedelta(hours=interval),
            )
        if config["activity_check_enabled"] and enabled_intervals:
            self.scheduler.add_job(
                self.request_activity,
                "interval",
                hours=max(12, min(enabled_intervals)),
                id="activity-check",
                name="Проверка актуальности",
                replace_existing=True,
                coalesce=True,
                max_instances=1,
                next_run_time=datetime.now(timezone.utc) + timedelta(minutes=30),
            )

    def request_scan(
        self,
        profile_id: Optional[str] = None,
        enabled_sources: Optional[Sequence[str]] = None,
        retry_attempt: int = 0,
    ) -> bool:
        return self._submit("scan", profile_id, enabled_sources, retry_attempt)

    def request_activity(self) -> bool:
        return self._submit("activity", None, None, 0)

    def status(self) -> Dict[str, Any]:
        with self.lock:
            result = dict(self.state)
        result["scheduled"] = [
            {
                "id": job.id,
                "name": job.name,
                "next_run_at": (
                    job.next_run_time.isoformat() if job.next_run_time else None
                ),
            }
            for job in self.scheduler.get_jobs()
        ]
        return result

    def _submit(
        self,
        kind: str,
        profile_id: Optional[str],
        enabled_sources: Optional[Sequence[str]],
        retry_attempt: int,
    ) -> bool:
        config = load_web_config(self.base_settings.database_url)
        effective_profile_id = profile_id or config["active_profile_id"]
        with self.lock:
            if self.state["running"]:
                return False
            self.state.update(
                {
                    "running": True,
                    "kind": kind,
                    "profile_id": effective_profile_id,
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "completed_at": None,
                    "last_result": None,
                    "last_error": None,
                }
            )
        self.executor.submit(
            self._execute,
            kind,
            effective_profile_id,
            list(enabled_sources) if enabled_sources else None,
            retry_attempt,
        )
        return True

    def _execute(
        self,
        kind: str,
        profile_id: str,
        enabled_sources: Optional[Sequence[str]],
        retry_attempt: int,
    ) -> None:
        try:
            config = load_web_config(self.base_settings.database_url)
            profile = get_profile(config, profile_id)
            settings = runtime_settings({**config, **profile}, self.base_settings)
            if kind == "scan":
                result = Scanner(settings).run(
                    dry_run=False,
                    enabled_sources=enabled_sources or profile["sources"],
                    profile_id=profile_id,
                )
                payload: Dict[str, Any] = {
                    "found": len(result.listings),
                    "new": len(result.new_listings),
                    "updated": len(result.changed_listings),
                    "errors": result.errors,
                    "sources": result.source_stats,
                    "profile_id": profile_id,
                }
                self._schedule_failed_source_retry(
                    profile_id,
                    profile["sources"],
                    result.errors,
                    retry_attempt,
                )
            else:
                payload = ListingActivityChecker(settings).run()
            with self.lock:
                self.state["last_result"] = payload
        except Exception as exc:
            LOGGER.exception("Background %s job failed", kind)
            with self.lock:
                self.state["last_error"] = str(exc)
        finally:
            with self.lock:
                self.state["running"] = False
                self.state["completed_at"] = datetime.now(timezone.utc).isoformat()

    def _schedule_failed_source_retry(
        self,
        profile_id: str,
        profile_sources: Sequence[str],
        errors: Dict[str, Any],
        retry_attempt: int,
    ) -> None:
        if retry_attempt >= 3:
            return
        allowed = set(profile_sources)
        failed = sorted(
            {
                key.split(":", 1)[-1]
                for key in errors
                if key.split(":", 1)[-1] in allowed
            }
        )
        if not failed:
            return
        delay_minutes = min(5 * (2**retry_attempt), 60)
        self.scheduler.add_job(
            self.request_scan,
            "date",
            kwargs={
                "profile_id": profile_id,
                "enabled_sources": failed,
                "retry_attempt": retry_attempt + 1,
            },
            run_date=datetime.now(timezone.utc) + timedelta(minutes=delay_minutes),
            id=f"source-retry:{profile_id}",
            name=f"Повтор источников: {', '.join(failed)}",
            replace_existing=True,
            misfire_grace_time=900,
        )
