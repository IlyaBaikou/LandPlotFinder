from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from apscheduler.schedulers.background import BackgroundScheduler

from app.activity import ListingActivityChecker
from app.config import Settings
from app.orchestrator import Scanner
from app.web_config import load_web_config, runtime_settings

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
        if not config["configured"] or not config["schedule_enabled"]:
            return
        interval = int(config["schedule_interval_hours"])
        self.scheduler.add_job(
            self.request_scan,
            "interval",
            hours=interval,
            id="listing-scan",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            next_run_time=datetime.now(timezone.utc) + timedelta(hours=interval),
        )
        if config["activity_check_enabled"]:
            self.scheduler.add_job(
                self.request_activity,
                "interval",
                hours=max(12, interval),
                id="activity-check",
                replace_existing=True,
                coalesce=True,
                max_instances=1,
                next_run_time=datetime.now(timezone.utc) + timedelta(minutes=30),
            )

    def request_scan(self) -> bool:
        return self._submit("scan")

    def request_activity(self) -> bool:
        return self._submit("activity")

    def status(self) -> Dict[str, Any]:
        with self.lock:
            result = dict(self.state)
        result["scheduled"] = [
            {
                "id": job.id,
                "next_run_at": (
                    job.next_run_time.isoformat() if job.next_run_time else None
                ),
            }
            for job in self.scheduler.get_jobs()
        ]
        return result

    def _submit(self, kind: str) -> bool:
        with self.lock:
            if self.state["running"]:
                return False
            self.state.update(
                {
                    "running": True,
                    "kind": kind,
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "completed_at": None,
                    "last_result": None,
                    "last_error": None,
                }
            )
        self.executor.submit(self._execute, kind)
        return True

    def _execute(self, kind: str) -> None:
        try:
            config = load_web_config(self.base_settings.database_url)
            settings = runtime_settings(config, self.base_settings)
            if kind == "scan":
                result = Scanner(settings).run(
                    dry_run=False,
                    enabled_sources=config["sources"],
                )
                payload: Dict[str, Any] = {
                    "found": len(result.listings),
                    "new": len(result.new_listings),
                    "updated": len(result.changed_listings),
                    "errors": result.errors,
                    "sources": result.source_stats,
                }
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
