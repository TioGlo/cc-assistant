import asyncio
import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Awaitable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .bridge import ClaudeBridge
from .config import ScheduledJob
from .session import SessionManager

logger = logging.getLogger(__name__)

DELAY_PATTERN = re.compile(r"^(\d+)\s*([smhd])$", re.IGNORECASE)
DELAY_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}

JobCallback = Callable[[str, str], Awaitable[None]]


def parse_delay(delay_str: str) -> timedelta:
    match = DELAY_PATTERN.match(delay_str.strip())
    if not match:
        raise ValueError(f"Invalid delay format: {delay_str!r} (expected e.g. '30m', '2h', '1d')")
    value = int(match.group(1))
    unit = DELAY_UNITS[match.group(2).lower()]
    return timedelta(**{unit: value})


class Scheduler:
    def __init__(self, bridge: ClaudeBridge, session_manager: SessionManager, jobs_file: Path) -> None:
        self.bridge = bridge
        self.session_manager = session_manager
        self._jobs_file = jobs_file
        self._callback: JobCallback | None = None
        self._scheduler = AsyncIOScheduler()

    def set_callback(self, callback: JobCallback) -> None:
        self._callback = callback

    def start(self) -> None:
        self._scheduler.start()
        logger.info("Scheduler started with %d jobs", len(self._scheduler.get_jobs()))

    def stop(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    # -- Persistence --

    def _load_dynamic_jobs(self) -> list[dict]:
        if not self._jobs_file.exists():
            return []
        try:
            return json.loads(self._jobs_file.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load dynamic jobs: %s", e)
            return []

    def _save_dynamic_jobs(self, jobs: list[dict]) -> None:
        self._jobs_file.parent.mkdir(parents=True, exist_ok=True)
        self._jobs_file.write_text(json.dumps(jobs, indent=2))

    def _append_dynamic_job(self, name: str, prompt: str, cron_expr: str,
                            working_dir: str | None = None, session: str = "chat") -> None:
        jobs = self._load_dynamic_jobs()
        jobs = [j for j in jobs if j["name"] != name]
        entry = {"name": name, "prompt": prompt, "cron": cron_expr,
                 "working_dir": working_dir, "created_at": datetime.now().isoformat()}
        if session != "chat":
            entry["session"] = session
        jobs.append(entry)
        self._save_dynamic_jobs(jobs)

    def _remove_dynamic_job(self, name: str) -> bool:
        jobs = self._load_dynamic_jobs()
        filtered = [j for j in jobs if j["name"] != name]
        if len(filtered) < len(jobs):
            self._save_dynamic_jobs(filtered)
            return True
        return False

    # -- Job loading --

    def load_config_jobs(self, jobs: list[ScheduledJob]) -> None:
        existing = {j.id for j in self._scheduler.get_jobs()}
        for job in jobs:
            job_id = f"config_{job.name}"
            if job_id not in existing:
                self._add_to_scheduler(job.name, job.prompt, job.cron, job.working_dir,
                                       session=job.session, job_id_prefix="config_")
                logger.info("Loaded config job: %s (%s)", job.name, job.cron)

    def load_dynamic_jobs(self) -> None:
        existing = {j.id for j in self._scheduler.get_jobs()}
        for job in self._load_dynamic_jobs():
            job_id = f"user_{job['name']}"
            if job_id not in existing:
                self._add_to_scheduler(job["name"], job["prompt"], job["cron"],
                                       job.get("working_dir"), session=job.get("session", "chat"),
                                       job_id_prefix="user_")
                logger.info("Loaded dynamic job: %s (%s)", job["name"], job["cron"])

    # -- Public API --

    def add_cron_job(self, name: str, prompt: str, cron_expr: str,
                     working_dir: str | None = None, session: str = "chat",
                     job_id_prefix: str = "user_") -> str:
        job_id = self._add_to_scheduler(name, prompt, cron_expr, working_dir,
                                        session=session, job_id_prefix=job_id_prefix)
        if job_id_prefix == "user_":
            self._append_dynamic_job(name, prompt, cron_expr, working_dir, session=session)
        return job_id

    def add_one_shot(self, prompt: str, delay: str, working_dir: str | None = None,
                     session: str = "chat") -> str:
        delta = parse_delay(delay)
        run_at = datetime.now() + delta
        job_id = f"remind_{int(run_at.timestamp())}"
        self._scheduler.add_job(
            self._run_job, trigger="date", run_date=run_at, id=job_id,
            name=f"reminder @ {run_at.strftime('%H:%M')}",
            kwargs={"job_name": "reminder", "prompt": prompt, "working_dir": working_dir,
                    "session": session},
        )
        logger.info("Added one-shot job: %s (runs at %s)", prompt[:50], run_at)
        return job_id

    def remove_job(self, name: str) -> bool:
        removed = False
        for prefix in ("user_", "config_", "remind_"):
            try:
                self._scheduler.remove_job(f"{prefix}{name}")
                removed = True
                break
            except Exception:
                continue
        if not removed:
            try:
                self._scheduler.remove_job(name)
                removed = True
            except Exception:
                pass
        self._remove_dynamic_job(name)
        return removed

    def list_jobs(self) -> list[dict]:
        return [{
            "id": j.id, "name": j.name,
            "next_run": j.next_run_time.strftime("%Y-%m-%d %H:%M:%S") if j.next_run_time else "paused",
            "prompt": j.kwargs.get("prompt", "")[:100],
        } for j in self._scheduler.get_jobs()]

    # -- Internal --

    def _add_to_scheduler(self, name: str, prompt: str, cron_expr: str,
                          working_dir: str | None = None, session: str = "chat",
                          job_id_prefix: str = "user_") -> str:
        job_id = f"{job_id_prefix}{name}"
        parts = cron_expr.strip().split()
        if len(parts) == 5:
            trigger = CronTrigger(minute=parts[0], hour=parts[1], day=parts[2],
                                  month=parts[3], day_of_week=parts[4])
        else:
            trigger = CronTrigger.from_crontab(cron_expr)
        self._scheduler.add_job(
            self._run_job, trigger=trigger, id=job_id, name=name, replace_existing=True,
            kwargs={"job_name": name, "prompt": prompt, "working_dir": working_dir,
                    "session": session},
        )
        logger.info("Added cron job: %s (%s, session=%s)", name, cron_expr, session)
        return job_id

    async def _run_job(self, job_name: str, prompt: str, working_dir: str | None = None,
                       session: str = "chat") -> None:
        logger.info("Executing scheduled job: %s (session=%s)", job_name, session)
        session_id = self.session_manager.get_session_id(session)
        max_retries = 3
        for attempt in range(max_retries):
            try:
                result_text, new_session_id = await self.bridge.send_simple(
                    prompt, session_id=session_id, working_dir=working_dir,
                )
                if new_session_id:
                    self.session_manager.set_session_id(new_session_id, session)
                if self._callback:
                    await self._callback(job_name, result_text)
                return
            except Exception as e:
                logger.error("Job %s attempt %d failed: %s", job_name, attempt + 1, e)
                if session_id and "No conversation found" in str(e):
                    logger.info("Stale session for '%s', falling back to fresh", session)
                    session_id = None
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt * 5)
        if self._callback:
            await self._callback(job_name, f"Job failed after {max_retries} attempts. Check logs.")
