import asyncio
import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Awaitable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .bridge import ClaudeBridge
from .config import JobDelivery, ScheduledJob
from .session import SessionManager

logger = logging.getLogger(__name__)

DELAY_PATTERN = re.compile(r"^(\d+)\s*([smhd])$", re.IGNORECASE)
DELAY_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}

# Interval expressions like "55m", "2h", "30s", "1d". Reuses DELAY_PATTERN.
INTERVAL_PATTERN = DELAY_PATTERN


def _parse_interval(expr: str) -> dict:
    """Parse '55m' / '2h' / '30s' / '1d' into kwargs for IntervalTrigger."""
    m = INTERVAL_PATTERN.match(expr.strip())
    if not m:
        raise ValueError(
            f"Invalid interval expression: {expr!r}. Use formats like '55m', '2h', '30s'."
        )
    return {DELAY_UNITS[m.group(2).lower()]: int(m.group(1))}

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

JobCallback = Callable[[str, str, "JobDelivery | None"], Awaitable[None]]


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
        # mtime fingerprints of the persistence files — kept in sync with disk
        # so the file-watcher can tell external edits (Kshana editing
        # scheduler-jobs.json by hand) apart from in-process saves.
        self._jobs_file_mtime: float = 0.0
        self._reminders_file_mtime: float = 0.0
        # Raw on-disk definition of each loaded job, by name. reload() uses
        # this to leave unchanged jobs alone: re-adding an interval job with
        # replace_existing=True resets its phase to now+interval, and the
        # 15s file watcher reloads on ANY external edit — so without this
        # check, frequent edits to scheduler-jobs.json starve every
        # interval job (e.g. the 55m heartbeat) indefinitely.
        self._loaded_job_defs: dict[str, dict] = {}

    def set_callback(self, callback: JobCallback) -> None:
        self._callback = callback

    def start(self) -> None:
        self._scheduler.start()
        # Seed mtimes so the first watcher tick doesn't trigger a spurious reload.
        self._jobs_file_mtime = self._mtime(self._jobs_file)
        self._reminders_file_mtime = self._mtime(self._reminders_file)
        # Auto-reload on external edits to scheduler-jobs.json /
        # scheduler-reminders.json. Kshana and other agents edit these files
        # directly, so without this watcher the live APScheduler triggers
        # diverge from the on-disk state until /reload is invoked manually.
        self._scheduler.add_job(
            self._watch_files, trigger=IntervalTrigger(seconds=15),
            id="_internal_file_watch", name="scheduler file watcher",
            replace_existing=True,
        )
        logger.info("Scheduler started with %d jobs", len(self._scheduler.get_jobs()))

    @staticmethod
    def _mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    async def _watch_files(self) -> None:
        """Reload if scheduler-jobs.json or scheduler-reminders.json changed on disk."""
        jobs_mt = self._mtime(self._jobs_file)
        rem_mt = self._mtime(self._reminders_file)
        if jobs_mt == self._jobs_file_mtime and rem_mt == self._reminders_file_mtime:
            return
        logger.info("Scheduler files changed on disk (jobs %s→%s, reminders %s→%s); reloading",
                    self._jobs_file_mtime, jobs_mt,
                    self._reminders_file_mtime, rem_mt)
        try:
            self.reload()
        except Exception:
            logger.exception("Auto-reload failed")
        # Always refresh the fingerprints — even on a failed reload — so we
        # don't tight-loop reloading a corrupt file every 15 seconds.
        self._jobs_file_mtime = self._mtime(self._jobs_file)
        self._reminders_file_mtime = self._mtime(self._reminders_file)

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
            # Refuse to proceed on a corrupt/unreadable jobs file. The previous
            # behavior was to log a warning and return []; downstream
            # seed_config_jobs() then treated the file as empty and overwrote
            # it with config.yaml seeds, silently destroying any dynamic-only
            # jobs (jobs created via /schedule or SCHEDULE blocks that don't
            # have a config.yaml counterpart). The fix: surface the error
            # loudly so the operator can repair the file before the service
            # comes back up. Any data loss on this code path is a regression
            # we cannot fix later — recovery requires the file's contents.
            raise RuntimeError(
                f"scheduler-jobs.json is unreadable or malformed: {e}\n"
                f"  Path: {self._jobs_file}\n"
                f"  The service refuses to start to prevent overwriting the\n"
                f"  file with empty config seeds (which would destroy any\n"
                f"  dynamic-only jobs). Inspect the file, fix the JSON, and\n"
                f"  restart. If you can't recover it, copy a backup over the\n"
                f"  corrupt file before restarting."
            ) from e

    def _save_dynamic_jobs(self, jobs: list[dict]) -> None:
        self._jobs_file.parent.mkdir(parents=True, exist_ok=True)
        self._jobs_file.write_text(json.dumps(jobs, indent=2))
        # Update fingerprint so the watcher doesn't treat our own write as an
        # external edit and trigger a redundant reload.
        self._jobs_file_mtime = self._mtime(self._jobs_file)

    def _append_dynamic_job(self, name: str, prompt: str, cron_expr: str | None,
                            working_dir: str | None = None, session: str = "chat",
                            interval_expr: str | None = None,
                            delivery: JobDelivery | None = None,
                            model: str = "",
                            command: str = "",
                            timeout_seconds: int = 60,
                            silent_on_empty: bool = True) -> None:
        jobs = self._load_dynamic_jobs()
        jobs = [j for j in jobs if j["name"] != name]
        entry = {"name": name,
                 "working_dir": working_dir, "created_at": datetime.now().isoformat()}
        if command:
            entry["command"] = command
            entry["timeout_seconds"] = timeout_seconds
            if not silent_on_empty:
                entry["silent_on_empty"] = False
        else:
            entry["prompt"] = prompt
        if cron_expr:
            entry["cron"] = cron_expr
        if interval_expr:
            entry["interval"] = interval_expr
        if session != "chat":
            entry["session"] = session
        if delivery is not None:
            entry["delivery"] = {"transport": delivery.transport,
                                 "channel_id": delivery.channel_id}
        if model:
            entry["model"] = model
        jobs.append(entry)
        self._save_dynamic_jobs(jobs)
        # Keep the reload snapshot in sync so the next watcher-triggered
        # reload sees this entry as unchanged rather than re-adding it.
        self._loaded_job_defs[name] = entry

    def _load_reminders(self) -> list[dict]:
        if not self._reminders_file.exists():
            return []
        try:
            return json.loads(self._reminders_file.read_text())
        except (json.JSONDecodeError, OSError) as e:
            # Same data-loss class as _load_dynamic_jobs above: silently
            # treating a corrupt file as empty and rewriting it would erase
            # every pending reminder. Refuse instead.
            raise RuntimeError(
                f"scheduler-reminders.json is unreadable or malformed: {e}\n"
                f"  Path: {self._reminders_file}\n"
                f"  The service refuses to start. Inspect the file, fix the\n"
                f"  JSON (or move it aside if losing pending reminders is\n"
                f"  acceptable), and restart."
            ) from e

    def _save_reminders(self, reminders: list[dict]) -> None:
        self._reminders_file.parent.mkdir(parents=True, exist_ok=True)
        self._reminders_file.write_text(json.dumps(reminders, indent=2))
        self._reminders_file_mtime = self._mtime(self._reminders_file)

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
        self._loaded_job_defs.pop(name, None)
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
                                         job.working_dir, session=job.session,
                                         interval_expr=job.interval,
                                         delivery=job.delivery,
                                         model=job.model,
                                         command=job.command,
                                         timeout_seconds=job.timeout_seconds,
                                         silent_on_empty=job.silent_on_empty)
                seeded += 1
                schedule = job.cron or f"interval={job.interval}"
                logger.info("Seeded job from config: %s (%s)", job.name, schedule)
        if seeded:
            logger.info("Seeded %d new jobs from config.yaml", seeded)

    def load_jobs(self) -> None:
        """Load all jobs from the dynamic jobs file into the scheduler.

        Jobs with `enabled: false` are skipped — entry stays on disk but
        no scheduling happens, so flipping the flag back is reversible.
        """
        for job in self._load_dynamic_jobs():
            if not job.get("enabled", True):
                logger.info("Skipping disabled job: %s", job.get("name"))
                continue
            job_id = f"job_{job['name']}"
            if job_id not in {j.id for j in self._scheduler.get_jobs()}:
                delivery_raw = job.get("delivery")
                delivery = JobDelivery(**delivery_raw) if delivery_raw else None
                self._add_to_scheduler(job["name"], job.get("prompt", ""), job.get("cron"),
                                       job.get("working_dir"), session=(job.get("session") or "chat"),
                                       job_id_prefix="job_",
                                       interval_expr=job.get("interval"),
                                       delivery=delivery,
                                       model=job.get("model", ""),
                                       command=job.get("command", ""),
                                       timeout_seconds=int(job.get("timeout_seconds", 60)),
                                       silent_on_empty=bool(job.get("silent_on_empty", True)),
                                       resume=bool(job.get("resume", False)))
                self._loaded_job_defs[job["name"]] = job
                schedule = job.get("cron") or f"interval={job.get('interval')}"
                target = (delivery.transport if delivery else "telegram")
                model_label = f" model={job['model']}" if job.get("model") else ""
                logger.info("Loaded job: %s (%s, session=%s, target=%s%s)",
                            job["name"], schedule, (job.get("session") or "chat"), target,
                            model_label)

    def reload(self) -> dict:
        """Re-read scheduler-jobs.json and scheduler-reminders.json and sync the
        running scheduler. Adds new entries, replaces modified ones, removes
        orphans. Returns a small summary suitable for displaying back to the user.
        """
        # --- Recurring jobs ---
        before_jobs = {j.id for j in self._scheduler.get_jobs() if j.id.startswith("job_")}
        target_jobs = set()
        added = 0
        replaced = 0
        unchanged = 0
        skipped = 0
        for job in self._load_dynamic_jobs():
            if not job.get("enabled", True):
                # Disabled jobs are intentionally absent from target_jobs so
                # they fall into removed_jobs below if previously running.
                self._loaded_job_defs.pop(job.get("name", ""), None)
                skipped += 1
                continue
            job_id = f"job_{job['name']}"
            target_jobs.add(job_id)
            existing = job_id in before_jobs
            if existing and self._loaded_job_defs.get(job["name"]) == job:
                # Definition unchanged — don't touch the live job, so an
                # interval trigger keeps its phase (next_run_time) instead
                # of being pushed out to now+interval by every reload.
                unchanged += 1
                continue
            delivery_raw = job.get("delivery")
            delivery = JobDelivery(**delivery_raw) if delivery_raw else None
            self._add_to_scheduler(job["name"], job.get("prompt", ""), job.get("cron"),
                                   job.get("working_dir"), session=(job.get("session") or "chat"),
                                   job_id_prefix="job_",
                                   interval_expr=job.get("interval"),
                                   delivery=delivery,
                                   model=job.get("model", ""),
                                   command=job.get("command", ""),
                                   timeout_seconds=int(job.get("timeout_seconds", 60)),
                                   silent_on_empty=bool(job.get("silent_on_empty", True)),
                                   resume=bool(job.get("resume", False)))
            self._loaded_job_defs[job["name"]] = job
            if existing:
                replaced += 1
            else:
                added += 1
        removed_jobs = before_jobs - target_jobs
        for job_id in removed_jobs:
            self._loaded_job_defs.pop(job_id.removeprefix("job_"), None)
            try:
                self._scheduler.remove_job(job_id)
            except Exception:
                pass

        # --- Reminders ---
        # Drop all currently-scheduled reminder jobs (those whose IDs match the
        # ids in scheduler-reminders.json), then re-add from disk.
        now = datetime.now()
        existing_reminder_ids = {j.id for j in self._scheduler.get_jobs()
                                 if j.id not in target_jobs and not j.id.startswith("job_")}
        # We can't perfectly tell which non-job_ ids are reminders without state,
        # so be conservative: only remove ones present in the reminders file.
        on_disk = self._load_reminders()
        on_disk_ids = {r["id"] for r in on_disk}
        for rid in (existing_reminder_ids & on_disk_ids):
            try:
                self._scheduler.remove_job(rid)
            except Exception:
                pass

        reminder_added = 0
        reminder_dropped = 0
        surviving = []
        for r in on_disk:
            run_at = datetime.fromisoformat(r["run_at"])
            if run_at <= now:
                reminder_dropped += 1
                continue
            self._scheduler.add_job(
                self._run_reminder, trigger="date", run_date=run_at, id=r["id"],
                name=f"reminder @ {run_at.strftime('%H:%M')}", replace_existing=True,
                kwargs={"reminder_id": r["id"], "prompt": r["prompt"],
                        "session": r.get("session", "chat")},
            )
            reminder_added += 1
            surviving.append(r)
        if len(surviving) != len(on_disk):
            self._save_reminders(surviving)

        summary = {
            "jobs_added": added,
            "jobs_replaced": replaced,
            "jobs_unchanged": unchanged,
            "jobs_removed": len(removed_jobs),
            "jobs_skipped": skipped,
            "reminders_loaded": reminder_added,
            "reminders_expired": reminder_dropped,
        }
        logger.info("Reload: %s", summary)
        return summary

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

    def add_cron_job(self, name: str, prompt: str, cron_expr: str | None,
                     working_dir: str | None = None, session: str = "chat",
                     delivery: JobDelivery | None = None,
                     interval_expr: str | None = None,
                     command: str = "",
                     timeout_seconds: int = 60,
                     silent_on_empty: bool = True,
                     model: str = "") -> str:
        if command and prompt:
            raise ValueError(f"job '{name}' sets both prompt and command")
        if not command and not prompt:
            raise ValueError(f"job '{name}' must set either prompt or command")
        # Auto-isolate session per model (mirrors ScheduledJob.__post_init__).
        if model and session == "chat" and not command:
            session = f"chat-{model}"
        job_id = self._add_to_scheduler(name, prompt, cron_expr, working_dir,
                                        session=session, job_id_prefix="job_",
                                        interval_expr=interval_expr,
                                        delivery=delivery,
                                        model=model,
                                        command=command,
                                        timeout_seconds=timeout_seconds,
                                        silent_on_empty=silent_on_empty)
        self._append_dynamic_job(name, prompt, cron_expr, working_dir, session=session,
                                 interval_expr=interval_expr,
                                 delivery=delivery,
                                 model=model,
                                 command=command,
                                 timeout_seconds=timeout_seconds,
                                 silent_on_empty=silent_on_empty)
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
        out = []
        for j in self._scheduler.get_jobs():
            cmd = j.kwargs.get("command", "")
            prompt = j.kwargs.get("prompt", "")
            out.append({
                "id": j.id, "name": j.name,
                "next_run": j.next_run_time.strftime("%Y-%m-%d %H:%M:%S") if j.next_run_time else "paused",
                "kind": "command" if cmd else "prompt",
                "prompt": (cmd if cmd else prompt)[:100],
            })
        return out

    # -- Internal --

    def _add_to_scheduler(self, name: str, prompt: str, cron_expr: str | None,
                          working_dir: str | None = None, session: str = "chat",
                          job_id_prefix: str = "user_",
                          interval_expr: str | None = None,
                          delivery: JobDelivery | None = None,
                          model: str = "",
                          command: str = "",
                          timeout_seconds: int = 60,
                          silent_on_empty: bool = True,
                          resume: bool = False) -> str:
        job_id = f"{job_id_prefix}{name}"

        if interval_expr:
            trigger = IntervalTrigger(**_parse_interval(interval_expr))
            schedule_label = f"every {interval_expr}"
        elif cron_expr:
            parts = cron_expr.strip().split()
            if len(parts) == 5:
                trigger = CronTrigger(minute=parts[0], hour=parts[1], day=parts[2],
                                      month=parts[3],
                                      day_of_week=_translate_dow(parts[4]))
            else:
                # 6+ field forms aren't standard cron — pass through as-is.
                trigger = CronTrigger.from_crontab(cron_expr)
            schedule_label = cron_expr
        else:
            raise ValueError(f"job '{name}' has neither cron nor interval set")

        # Stash delivery as a plain dict in kwargs so APScheduler can persist it
        # (apscheduler pickles kwargs; a dict is friendlier than a dataclass).
        delivery_dict = (
            {"transport": delivery.transport, "channel_id": delivery.channel_id}
            if delivery else None
        )
        kwargs = {"job_name": name, "working_dir": working_dir,
                  "delivery": delivery_dict}
        if command:
            kwargs["command"] = command
            kwargs["timeout_seconds"] = timeout_seconds
            kwargs["silent_on_empty"] = silent_on_empty
            target = self._run_command_job
            kind_label = "command"
        else:
            # If the prompt job targets the main "chat" surface, default
            # resume=true. Mirrors ScheduledJob.__post_init__: the main chat
            # session is bounded by nightly session-cleanup, so cron jobs
            # sharing it benefit from continuity with the user's interactive
            # context without the unbounded-growth failure mode. Per-model
            # sessions (chat-sonnet, chat-haiku) are NOT bounded by nightly
            # reset, so the rule only fires on literal "chat".
            if session == "chat" and not resume:
                resume = True
            kwargs["prompt"] = prompt
            kwargs["session"] = session
            kwargs["model"] = model
            kwargs["resume"] = resume
            target = self._run_job
            kind_label = "prompt"
        self._scheduler.add_job(
            target, trigger=trigger, id=job_id, name=name, replace_existing=True,
            kwargs=kwargs,
        )
        if command:
            extra = " command"
        else:
            extra = f" session={session}"
            if model:
                extra += f" model={model}"
        logger.info("Added %s job: %s (%s,%s)", kind_label, name, schedule_label, extra)
        return job_id

    async def _run_command_job(self, job_name: str, command: str,
                               working_dir: str | None = None,
                               delivery: dict | None = None,
                               timeout_seconds: int = 60,
                               silent_on_empty: bool = True) -> None:
        """Run a bash command directly. No LLM in the path. Output (or error)
        flows through the same delivery callback as prompt-type jobs."""
        logger.info("Executing command job: %s -> %s", job_name, command[:200])
        delivery_obj = JobDelivery(**delivery) if delivery else None
        cwd = str(Path(working_dir).expanduser()) if working_dir else None
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout_seconds,
                )
            except asyncio.TimeoutError:
                proc.kill()
                msg = f"⏱ `{job_name}` timed out after {timeout_seconds}s"
                if self._callback:
                    await self._callback(job_name, msg, delivery_obj)
                return
        except Exception as e:
            logger.exception("Command job %s failed to spawn", job_name)
            if self._callback:
                await self._callback(job_name, f"❌ `{job_name}` spawn failed: {e}", delivery_obj)
            return

        out = stdout.decode(errors="replace").strip()
        err = stderr.decode(errors="replace").strip()
        rc = proc.returncode

        if rc != 0:
            tail = (err or out or "(no output)")[-1500:]
            msg = f"❌ `{job_name}` exit {rc}\n```\n{tail}\n```"
        elif not out:
            if silent_on_empty:
                logger.info("Command job %s produced no output; skipping delivery", job_name)
                return
            msg = f"✅ `{job_name}` (no output)"
        elif "\n" in out or len(out) > 200:
            msg = f"```\n{out[-3500:]}\n```"
        else:
            msg = out

        if self._callback:
            await self._callback(job_name, msg, delivery_obj)

    async def _run_job(self, job_name: str, prompt: str, working_dir: str | None = None,
                       session: str = "chat", delivery: dict | None = None,
                       model: str = "", resume: bool = False) -> None:
        logger.info("Executing scheduled job: %s (session=%s, model=%s, resume=%s)",
                    job_name, session, model or "default", resume)
        delivery_obj = JobDelivery(**delivery) if delivery else None
        # Stateless by default — passing session_id forces --resume which can
        # overflow Sonnet's window once the transcript grows past ~200k tokens
        # (claude -p does NOT auto-compact). Jobs that explicitly want
        # cross-fire continuity must opt in via resume=true AND keep their
        # session bounded.
        session_id = self.session_manager.get_session_id(session) if resume else None
        max_retries = 3
        for attempt in range(max_retries):
            try:
                result_text, new_session_id = await self.bridge.send_simple(
                    prompt, session_id=session_id, working_dir=working_dir,
                    model=model or None,
                )
                # Only persist the new session ID when this job is opted into
                # resume — stateless jobs spawn a fresh session each fire and
                # we don't want those one-shot IDs polluting session.json.
                if resume and new_session_id:
                    self.session_manager.set_session_id(new_session_id, session)
                if self._callback:
                    await self._callback(job_name, result_text, delivery_obj)
                return
            except Exception as e:
                logger.error("Job %s attempt %d failed: %s", job_name, attempt + 1, e)
                if session_id and "No conversation found" in str(e):
                    logger.info("Stale session for '%s', falling back to fresh", session)
                    session_id = None
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt * 5)
        if self._callback:
            await self._callback(
                job_name, f"Job failed after {max_retries} attempts. Check logs.",
                delivery_obj,
            )

    async def _run_reminder(self, reminder_id: str, prompt: str, working_dir: str | None = None,
                            session: str = "chat") -> None:
        logger.info("Executing reminder: %s (session=%s)", reminder_id, session)
        await self._run_job(job_name="reminder", prompt=prompt, working_dir=working_dir, session=session)
        self._remove_reminder(reminder_id)
        logger.info("Reminder %s fired and removed from persistence", reminder_id)
