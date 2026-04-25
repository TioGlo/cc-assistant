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

# Standard cron uses 0=Sun, 1=Mon, ..., 6=Sat (and 7=Sun for legacy).
# APScheduler's CronTrigger uses 0=Mon, ..., 6=Sun — incompatible. We translate
# digits in the day-of-week field to unambiguous day-name abbreviations so that
# the meaning is preserved no matter which convention APScheduler interprets.
_CRON_DOW_NAMES = {
    "0": "sun", "1": "mon", "2": "tue", "3": "wed",
    "4": "thu", "5": "fri", "6": "sat", "7": "sun",
}


def _translate_dow(field: str) -> str:
    """Convert standard-cron DoW digits to day-name abbreviations.

    Preserves *, ranges, lists, steps. Already-named tokens (mon, tue) pass
    through unchanged. Examples:
        '6'         -> 'sat'
        '1,2,4,5'   -> 'mon,tue,thu,fri'
        '1-5'       -> 'mon-fri'
        '*'         -> '*'
        'mon-fri'   -> 'mon-fri'
    """
    return re.sub(r"\b[0-7]\b", lambda m: _CRON_DOW_NAMES[m.group(0)], field)

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
        self._reminders_file = jobs_file.parent / "scheduler-reminders.json"
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

    def _load_reminders(self) -> list[dict]:
        if not self._reminders_file.exists():
            return []
        try:
            return json.loads(self._reminders_file.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load reminders: %s", e)
            return []

    def _save_reminders(self, reminders: list[dict]) -> None:
        self._reminders_file.parent.mkdir(parents=True, exist_ok=True)
        self._reminders_file.write_text(json.dumps(reminders, indent=2))

    def _append_reminder(self, reminder_id: str, prompt: str, run_at: datetime,
                         session: str = "chat") -> None:
        reminders = self._load_reminders()
        reminders.append({
            "id": reminder_id,
            "prompt": prompt,
            "run_at": run_at.isoformat(),
            "session": session,
        })
        self._save_reminders(reminders)

    def _remove_reminder(self, reminder_id: str) -> None:
        reminders = self._load_reminders()
        filtered = [r for r in reminders if r["id"] != reminder_id]
        self._save_reminders(filtered)

    def _remove_dynamic_job(self, name: str) -> bool:
        jobs = self._load_dynamic_jobs()
        filtered = [j for j in jobs if j["name"] != name]
        if len(filtered) < len(jobs):
            self._save_dynamic_jobs(filtered)
            return True
        return False

    # -- Job loading --

    def seed_config_jobs(self, jobs: list[ScheduledJob]) -> None:
        """Seed config.yaml jobs into the dynamic jobs file (one-time merge).

        Config jobs are initial seeds — once they exist in scheduler-jobs.json,
        they're managed through the dynamic system like any other job.
        """
        dynamic = self._load_dynamic_jobs()
        dynamic_names = {j["name"] for j in dynamic}
        seeded = 0
        for job in jobs:
            if job.name not in dynamic_names:
                self._append_dynamic_job(job.name, job.prompt, job.cron,
                                         job.working_dir, session=job.session)
                seeded += 1
                logger.info("Seeded job from config: %s (%s)", job.name, job.cron)
        if seeded:
            logger.info("Seeded %d new jobs from config.yaml", seeded)

    def load_jobs(self) -> None:
        """Load all jobs from the dynamic jobs file into the scheduler."""
        for job in self._load_dynamic_jobs():
            job_id = f"job_{job['name']}"
            if job_id not in {j.id for j in self._scheduler.get_jobs()}:
                self._add_to_scheduler(job["name"], job["prompt"], job["cron"],
                                       job.get("working_dir"), session=job.get("session", "chat"),
                                       job_id_prefix="job_")
                logger.info("Loaded job: %s (%s, session=%s)", job["name"], job["cron"],
                            job.get("session", "chat"))

    def load_reminders(self) -> None:
        """Reload persisted reminders that haven't fired yet."""
        now = datetime.now()
        reminders = self._load_reminders()
        surviving = []
        existing = {j.id for j in self._scheduler.get_jobs()}
        for r in reminders:
            run_at = datetime.fromisoformat(r["run_at"])
            if run_at <= now:
                logger.info("Discarding expired reminder: %s", r["id"])
                continue
            if r["id"] in existing:
                surviving.append(r)
                continue
            self._scheduler.add_job(
                self._run_reminder, trigger="date", run_date=run_at, id=r["id"],
                name=f"reminder @ {run_at.strftime('%H:%M')}",
                kwargs={"reminder_id": r["id"], "prompt": r["prompt"],
                        "session": r.get("session", "chat")},
            )
            surviving.append(r)
            logger.info("Restored reminder: %s (fires at %s)", r["id"], run_at.strftime('%H:%M'))
        # Clean up expired entries
        if len(surviving) != len(reminders):
            self._save_reminders(surviving)

    # -- Public API --

    def add_cron_job(self, name: str, prompt: str, cron_expr: str,
                     working_dir: str | None = None, session: str = "chat") -> str:
        job_id = self._add_to_scheduler(name, prompt, cron_expr, working_dir,
                                        session=session, job_id_prefix="job_")
        self._append_dynamic_job(name, prompt, cron_expr, working_dir, session=session)
        return job_id

    def add_one_shot(self, prompt: str, delay: str, working_dir: str | None = None,
                     session: str = "chat") -> str:
        delta = parse_delay(delay)
        run_at = datetime.now() + delta
        job_id = f"remind_{int(run_at.timestamp())}"
        self._scheduler.add_job(
            self._run_reminder, trigger="date", run_date=run_at, id=job_id,
            name=f"reminder @ {run_at.strftime('%H:%M')}",
            kwargs={"reminder_id": job_id, "prompt": prompt, "working_dir": working_dir,
                    "session": session},
        )
        self._append_reminder(job_id, prompt, run_at, session)
        logger.info("Added one-shot reminder: %s (runs at %s)", prompt[:50], run_at)
        return job_id

    def remove_job(self, name: str) -> bool:
        removed = False
        for prefix in ("job_", "user_", "config_", "remind_"):
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
                                  month=parts[3],
                                  day_of_week=_translate_dow(parts[4]))
        else:
            # 6+ field forms aren't standard cron — pass through as-is.
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

    async def _run_reminder(self, reminder_id: str, prompt: str, working_dir: str | None = None,
                            session: str = "chat") -> None:
        logger.info("Executing reminder: %s (session=%s)", reminder_id, session)
        await self._run_job(job_name="reminder", prompt=prompt, working_dir=working_dir, session=session)
        self._remove_reminder(reminder_id)
        logger.info("Reminder %s fired and removed from persistence", reminder_id)
