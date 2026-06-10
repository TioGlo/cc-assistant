import asyncio
import importlib.util
import json
import logging
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import paths, telegram_log
from .bridge import AuthError, BridgeError, ClaudeBridge
from .config import Config, JobDelivery
from .formatter import (
    extract_delegate_commands,
    extract_remind_commands,
    extract_schedule_commands,
    split_message,
    strip_commands,
    strip_markdown,
    to_telegram_markdown,
)
from .scheduler import Scheduler
from .session import SessionManager
from .tmux_dispatch import TmuxDispatch
from .voice import TranscriptionEngine, get_engine

logger = logging.getLogger(__name__)

# Telegram-priority routing. See projects/telegram-streamlining/ for the design.
_PRIORITY_TAG_RE = re.compile(
    r"<!--\s*PRIORITY\s*:\s*(action|fyi|silent_log)\s*-->", re.IGNORECASE
)
_SILENT_LOG_PATH = Path("~/.assistant/workspace/data/telegram-silent-log.jsonl").expanduser()
_QUIET_HOURS_START = 21  # 21:00 local
_QUIET_HOURS_END = 8     # 08:00 local


def _extract_priority(text: str) -> tuple[str | None, str]:
    """Find a <!--PRIORITY:LEVEL--> tag in `text`. Returns (level_or_None, text_with_tag_stripped)."""
    m = _PRIORITY_TAG_RE.search(text)
    if not m:
        return None, text
    level = m.group(1).lower()
    cleaned = _PRIORITY_TAG_RE.sub("", text).strip()
    return level, cleaned


def _in_quiet_hours(now: datetime | None = None) -> bool:
    """Return True if local time is within quiet hours [21:00, 08:00)."""
    h = (now or datetime.now()).hour
    return h >= _QUIET_HOURS_START or h < _QUIET_HOURS_END


# Type alias avoiding circular import with discord_bot.py
SendTextFn = "Callable[[str], Awaitable[None]]"
SendTypingFn = "Callable[[], Awaitable[None]] | None"


class AssistantBot:
    def __init__(
        self, config: Config, bridge: ClaudeBridge,
        session_manager: SessionManager, scheduler: Scheduler,
    ) -> None:
        self.config = config
        self.bridge = bridge
        self.session_manager = session_manager
        self.scheduler = scheduler
        self.tmux = TmuxDispatch(config.cc_agents or None)
        self._start_time = time.time()
        self._last_user_msg_time = datetime.now()
        self.app: Application | None = None
        self.voice_engine: TranscriptionEngine | None = None
        # Set later by main.py via set_discord_bot(); used to route
        # cron jobs whose delivery.transport == "discord".
        self.discord_bot = None
        if config.voice.enabled:
            try:
                self.voice_engine = get_engine(config.voice)
                logger.info(
                    "Voice engine: %s (model=%s)",
                    config.voice.engine, config.voice.model,
                )
            except Exception:
                logger.exception("Voice engine init failed; voice disabled")

    def _is_owner(self, update: Update) -> bool:
        user = update.effective_user
        return user is not None and user.id == self.config.telegram.owner_id

    async def _send_text(
        self,
        chat_id: int,
        text: str,
        priority: str = "silent_log",
        source: str = "unknown",
    ) -> None:
        """Push to Telegram (priority=action|fyi) or write to the silent log
        (priority=silent_log). Default is silent_log so unprompted callers
        opt-in to push rather than opt-out. Conversational replies use
        `_reply`, not this path; that flow is unaffected.

        Quiet hours: between 21:00 and 08:00 local, ACTION degrades to
        a silent push — the ACTION prefix is preserved so Tॐ sees the
        priority when he wakes, but the notification doesn't disturb sleep.
        """
        if priority not in ("action", "fyi", "silent_log"):
            logger.warning("Unknown priority %r; defaulting to silent_log", priority)
            priority = "silent_log"

        if priority == "silent_log":
            await self._append_silent_log(source=source, text=text)
            return

        prefix = {"action": "🔴 ACTION: ", "fyi": "🟡 FYI: "}[priority]
        disable_notif = priority == "fyi" or (priority == "action" and _in_quiet_hours())

        for chunk in split_message(prefix + text):
            await self._send_chunk(chat_id, chunk, disable_notification=disable_notif)

    async def _append_silent_log(self, source: str, text: str) -> None:
        """Write an entry to the silent-log JSONL file without blocking the event loop."""
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "source": source,
            "text": text,
        }

        def _write() -> None:
            _SILENT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with _SILENT_LOG_PATH.open("a") as f:
                f.write(json.dumps(entry) + "\n")

        try:
            await asyncio.get_event_loop().run_in_executor(None, _write)
        except Exception:
            logger.exception("Failed to append to silent log at %s", _SILENT_LOG_PATH)

    async def _send_chunk(
        self, chat_id: int, chunk: str, disable_notification: bool = False
    ) -> None:
        """Send one chunk with Telegram Markdown rendering. Falls back to
        plain text on any parse error so a stray '*' or '_' in the model's
        output never blocks delivery.
        """
        try:
            await self.app.bot.send_message(
                chat_id=chat_id,
                text=to_telegram_markdown(chunk),
                parse_mode="Markdown",
                disable_notification=disable_notification,
            )
        except Exception as e:
            logger.debug("Markdown send failed (%s); falling back to plain", e)
            await self.app.bot.send_message(
                chat_id=chat_id,
                text=strip_markdown(chunk),
                disable_notification=disable_notification,
            )

    async def _reply(self, update: Update, text: str) -> None:
        """update.message.reply_text with Markdown + plain fallback."""
        try:
            await update.message.reply_text(
                to_telegram_markdown(text), parse_mode="Markdown",
            )
        except Exception as e:
            logger.debug("Markdown reply failed (%s); falling back to plain", e)
            await update.message.reply_text(strip_markdown(text))

    # -- Discord wiring --

    def set_discord_bot(self, discord_bot) -> None:
        """Wired by main.py after both bots are constructed.

        Allows on_job_result to deliver to Discord channels and lets
        DiscordBot reach back into AssistantBot for the shared message-
        processing pipeline.
        """
        self.discord_bot = discord_bot

    # -- Scheduler callback --

    async def on_job_result(
        self, job_name: str, result_text: str,
        delivery: JobDelivery | None = None,
    ) -> None:
        self._process_commands(result_text)
        await self._process_delegations_from_job(result_text)
        clean_text = strip_commands(result_text)
        if not clean_text:
            return
        if clean_text.strip().upper() in ("HEARTBEAT_OK", "NO_REPLY"):
            return

        # Telegram priority routing. The model output may carry a
        # <!--PRIORITY:action|fyi|silent_log--> tag; otherwise the job's
        # configured default is used, falling back to silent_log.
        tag_priority, clean_text = _extract_priority(clean_text)
        job_default = (delivery and delivery.priority) or "silent_log"
        priority = tag_priority or job_default

        # Route based on delivery
        if delivery and delivery.transport == "discord" and delivery.channel_id:
            if self.discord_bot is None:
                logger.warning(
                    "Job %s wants Discord delivery but Discord bot is not configured;"
                    " falling back to Telegram",
                    job_name,
                )
            else:
                header = f"**Scheduled: {job_name}**\n\n"
                await self.discord_bot.send_to_channel(delivery.channel_id, header + clean_text)
                return

        # Default: Telegram owner
        chat_id = self.config.telegram.owner_id
        header = f"**Scheduled: {job_name}**\n\n"
        await self._send_text(
            chat_id, header + clean_text,
            priority=priority, source=f"cron:{job_name}",
        )
        telegram_log.append(job_name, clean_text)

    async def _process_delegations_from_job(self, text: str) -> None:
        for cmd in extract_delegate_commands(text):
            session = cmd.session or None
            task = self._enrich_with_project(cmd.task, cmd.project)
            logger.info("Cron job delegating task: %s", cmd.task[:80])
            try:
                status = await self.tmux.dispatch(task, timeout=cmd.timeout, session=session)
                msg = f"Delegated: {status}"
                await self._send_text(
                    self.config.telegram.owner_id, msg,
                    priority="silent_log", source="delegation",
                )
                telegram_log.append("delegation", msg)
            except Exception as e:
                logger.exception("Cron delegation failed")
                msg = f"Delegation failed: {e}"
                # Failures escalate — Tॐ needs to know if a dispatch broke.
                await self._send_text(
                    self.config.telegram.owner_id, msg,
                    priority="action", source="delegation_failure",
                )
                telegram_log.append("delegation", msg)

    def _enrich_with_project(self, task: str, project: str) -> str:
        """If a project name is given, prepend its summary to the task description."""
        if not project:
            return task
        summary_path = paths.workspace() / "projects" / project / "summary.md"
        if not summary_path.exists():
            logger.warning("Project summary not found: %s", project)
            return task
        try:
            summary = summary_path.read_text()
            return (
                f"# Project Context: {project}\n\n"
                f"{summary}\n\n"
                f"---\n\n"
                f"# Task\n\n"
                f"{task}\n\n"
                f"---\n\n"
                f"After completing the task, update {summary_path} if the project state changed."
            )
        except OSError:
            return task

    # -- Core commands --

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return
        name = paths.agent_name()
        cmds = [
            f"/{name} assistant is running.\n",
            "Core commands:",
            "/reset - Start a fresh session",
            "/status - Show bot status",
            "/jobs - List scheduled jobs",
            "/cancel <name> - Cancel a scheduled job",
            "/schedule <cron> <prompt> - Schedule a recurring task",
            "/remind <delay> <prompt> - Set a one-shot reminder",
            "/code <task> - Dispatch coding task",
            "/codecheck - Check coding session status",
            "/approve <id> - Approve a permission request",
            "/deny <id> - Deny a permission request",
        ]
        await update.message.reply_text("\n".join(cmds))

    async def cmd_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return
        self.session_manager.clear_session("chat")
        await update.message.reply_text("Session cleared. Next message starts fresh.")

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return
        uptime = int(time.time() - self._start_time)
        hours, remainder = divmod(uptime, 3600)
        minutes, seconds = divmod(remainder, 60)
        session_id = self.session_manager.get_session_id("chat")
        jobs = self.scheduler.list_jobs()

        # Count active projects and areas
        projects_dir = paths.workspace() / "projects"
        areas_dir = paths.workspace() / "areas"
        project_count = sum(1 for p in projects_dir.iterdir() if p.is_dir()) if projects_dir.exists() else 0
        area_count = sum(1 for a in areas_dir.iterdir() if a.is_dir()) if areas_dir.exists() else 0

        lines = [
            f"Agent: {paths.agent_name()}",
            f"Uptime: {hours}h {minutes}m {seconds}s",
            f"Model: {self.config.claude.model}",
            f"Session: {session_id or 'none (will start fresh)'}",
            f"Scheduled jobs: {len(jobs)}",
            f"Active projects: {project_count}",
            f"Areas: {area_count}",
            f"Tmux session: {self.tmux.default_session_name}",
        ]
        await update.message.reply_text("\n".join(lines))

    async def cmd_jobs(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return
        jobs = self.scheduler.list_jobs()
        if not jobs:
            await update.message.reply_text("No scheduled jobs.")
            return
        lines = []
        for j in jobs:
            icon = "⚙️" if j.get("kind") == "command" else "🤖"
            lines.append(f"{icon} {j['name']} (next: {j['next_run']})\n  {j['prompt']}")
        await update.message.reply_text("\n".join(lines))

    async def cmd_reload(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Re-read scheduler-jobs.json and scheduler-reminders.json without restarting."""
        if not self._is_owner(update):
            return
        try:
            summary = self.scheduler.reload()
        except Exception as e:
            logger.exception("Reload failed")
            await update.message.reply_text(f"Reload failed: {e}")
            return
        text = (
            f"Reloaded scheduler.\n"
            f"Jobs: +{summary['jobs_added']} added, "
            f"~{summary['jobs_replaced']} replaced, "
            f"={summary['jobs_unchanged']} unchanged, "
            f"−{summary['jobs_removed']} removed.\n"
            f"Reminders: {summary['reminders_loaded']} active, "
            f"{summary['reminders_expired']} expired."
        )
        if summary.get("malformed"):
            text += (
                f"\n⚠️ {summary['malformed']} malformed entr"
                f"{'y' if summary['malformed'] == 1 else 'ies'} skipped "
                f"(left on disk — check logs)."
            )
        await update.message.reply_text(text)

    async def cmd_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return
        if not context.args:
            await update.message.reply_text("Usage: /cancel <job-name>")
            return
        name = context.args[0]
        if self.scheduler.remove_job(name):
            await update.message.reply_text(f"Cancelled job: {name}")
        else:
            await update.message.reply_text(f"Job not found: {name}")

    async def cmd_schedule(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return
        if not context.args or len(context.args) < 6:
            await update.message.reply_text(
                "Usage: /schedule <min> <hour> <day> <month> <dow> <prompt>\n"
                "Example: /schedule 0 8 * * * Morning briefing"
            )
            return
        cron_expr = " ".join(context.args[:5])
        prompt = " ".join(context.args[5:])
        name = f"manual_{int(datetime.now().timestamp())}"
        self.scheduler.add_cron_job(name, prompt, cron_expr)
        await update.message.reply_text(f"Scheduled: {name}\nCron: {cron_expr}\nPrompt: {prompt}")

    async def cmd_remind(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return
        if not context.args or len(context.args) < 2:
            await update.message.reply_text("Usage: /remind <delay> <prompt>\nExample: /remind 2h Check the deploy")
            return
        delay = context.args[0]
        prompt = " ".join(context.args[1:])
        try:
            self.scheduler.add_one_shot(prompt, delay)
            await update.message.reply_text(f"Reminder set ({delay}): {prompt}")
        except ValueError as e:
            await update.message.reply_text(str(e))

    # -- Tmux approval commands --

    async def cmd_approve(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return
        await self._handle_approval(update, context, persistent=False)

    async def cmd_approve_always(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return
        await self._handle_approval(update, context, persistent=True)

    async def cmd_deny(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return
        if not context.args:
            await update.message.reply_text("Usage: /deny <approval-id>")
            return
        approval_id = context.args[0]
        approval = self._load_approval(approval_id)
        if not approval:
            await update.message.reply_text(f"Approval not found: {approval_id}")
            return
        session = approval["session"]
        await asyncio.get_event_loop().run_in_executor(
            None, self._send_tmux_keys, session, ["Escape"]
        )
        self._remove_approval(approval_id)
        await update.message.reply_text(f"Denied and sent Escape to {session}")

    async def _handle_approval(self, update: Update, context: ContextTypes.DEFAULT_TYPE, persistent: bool) -> None:
        if not context.args:
            cmd = "/approve_always" if persistent else "/approve"
            await update.message.reply_text(f"Usage: {cmd} <approval-id>")
            return
        approval_id = context.args[0]
        approval = self._load_approval(approval_id)
        if not approval:
            await update.message.reply_text(f"Approval not found: {approval_id}")
            return
        session = approval["session"]
        if persistent and approval.get("has_persistent", 0) > 0:
            await asyncio.get_event_loop().run_in_executor(
                None, self._send_tmux_keys, session, ["Tab", 0.1, "Enter"]
            )
            self._remove_approval(approval_id)
            await update.message.reply_text(f"Approved (persistent) for {session}")
        else:
            await asyncio.get_event_loop().run_in_executor(
                None, self._send_tmux_keys, session, ["Enter"]
            )
            self._remove_approval(approval_id)
            await update.message.reply_text(f"Approved for {session}")

    def _send_tmux_keys(self, session: str, keys: list) -> None:
        import subprocess, time as t
        for key in keys:
            if isinstance(key, (int, float)):
                t.sleep(key)
            else:
                subprocess.run(["tmux", "send-keys", "-t", session, key], check=True)

    def _load_approval(self, approval_id: str) -> dict | None:
        import json
        path = paths.pending_approvals_dir() / f"{approval_id}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None

    def _remove_approval(self, approval_id: str) -> None:
        (paths.pending_approvals_dir() / f"{approval_id}.json").unlink(missing_ok=True)

    # -- Tmux coding commands --

    async def cmd_code(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return
        if not context.args:
            await update.message.reply_text(
                "Usage: /code <task description>\n\n"
                "Dispatches to a full interactive Claude Code session.\n"
                f"Tmux session: {self.tmux.default_session_name}"
            )
            return
        task = " ".join(context.args)
        try:
            status = await self.tmux.dispatch(task, timeout=600)
            await update.message.reply_text(status)
        except Exception as e:
            logger.exception("Code dispatch failed")
            await update.message.reply_text(f"Code dispatch error: {e}")

    async def cmd_codecheck(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return
        if not await self.tmux.default_session_name_exists():
            await update.message.reply_text(
                f"No tmux session '{self.tmux.default_session_name}'. It will be created on next /code dispatch."
            )
            return
        output = await self.tmux.capture_recent_output(lines=30)
        msg = f"Session: {self.tmux.default_session_name}\n\nRecent output:\n{output}"
        for chunk in split_message(msg):
            await update.message.reply_text(chunk)

    # -- Message handler --

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return
        text = update.message.text
        if not text:
            return
        await self._process_user_text(text, update)

    async def _process_user_text(self, text: str, update: Update) -> None:
        """Telegram-specific entrypoint — wraps the transport-agnostic core."""
        chat_id = update.effective_chat.id

        # Prepend recent non-chat Telegram activity so main Qu sees what
        # cron jobs posted between user turns. Only fires when something
        # has happened since the last user message.
        recent = telegram_log.entries_since(
            self._last_user_msg_time, exclude_sources={"chat"},
        )
        prefix = telegram_log.render_for_prompt(recent)
        if prefix:
            text = f"{prefix}\n\n---\n\n{text}"
        self._last_user_msg_time = datetime.now()

        async def send_text(chunk: str) -> None:
            await self._reply(update, chunk)

        async def send_typing() -> None:
            await self._keep_typing(chat_id)

        await self.process_text_input(
            text=text, session_key="chat",
            send_text=send_text, send_typing=send_typing,
            telegram_log_source="chat",
        )

    async def process_text_input(
        self, text: str, session_key: str,
        send_text, send_typing=None,
        telegram_log_source: str | None = None,
    ) -> None:
        """Transport-agnostic core: run text through the LLM, send the reply,
        process embedded commands/delegations.

        Used by both Telegram message handlers and the Discord bot.
        - text: user prompt (already cleaned of channel-specific markup)
        - session_key: claude session ID key — "chat" for Telegram, "discord:<channel_id>" for Discord
        - send_text(chunk): async callable that sends a chunk to the user
        - send_typing(): optional async callable that pulses a typing indicator
          until cancelled
        - telegram_log_source: if set, append the response to the shared
          Telegram log under this source label (Telegram only — Discord
          replies skip it since the log is Telegram-specific).
        """
        typing_task = asyncio.create_task(send_typing()) if send_typing else None
        try:
            session_id = self.session_manager.get_session_id(session_key)
            chat_effort = self.config.claude.chat_effort or None
            try:
                response_text, new_session_id = await self.bridge.send_simple(
                    text, session_id=session_id, effort=chat_effort,
                )
            except BridgeError as e:
                if session_id and "No conversation found" in str(e):
                    logger.info("Stale session %s, falling back to fresh", session_key)
                    self.session_manager.clear_session(session_key)
                    response_text, new_session_id = await self.bridge.send_simple(
                        text, effort=chat_effort,
                    )
                else:
                    raise
            if new_session_id:
                self.session_manager.set_session_id(new_session_id, session_key)
            self._process_commands(response_text)
            await self._process_delegations(response_text, send_text)
            clean_text = strip_commands(response_text)
            if clean_text:
                for chunk in split_message(clean_text):
                    await send_text(chunk)
                if telegram_log_source:
                    telegram_log.append(telegram_log_source, clean_text)
        except AuthError as e:
            logger.error("Auth failure: %s", e)
            await send_text("Authentication expired. Run `claude auth login` on the server.")
        except Exception as e:
            logger.exception("Error handling message")
            await send_text(f"Error: {e}")
        finally:
            if typing_task is not None:
                typing_task.cancel()

    async def handle_photo_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Save a Telegram photo (or image document) to the workspace inbox and route a synthesized text prompt through the message pipeline so the agent can read the file."""
        if not self._is_owner(update):
            return

        message = update.message
        file_id = None
        suffix = ".jpg"
        if message.photo:
            # Largest available size is last in the list
            file_id = message.photo[-1].file_id
        elif message.document and (message.document.mime_type or "").startswith("image/"):
            file_id = message.document.file_id
            name = message.document.file_name or ""
            if "." in name:
                suffix = "." + name.rsplit(".", 1)[-1].lower()
        if not file_id:
            return

        chat_id = update.effective_chat.id
        await self.app.bot.send_chat_action(chat_id=chat_id, action="typing")

        inbox = paths.workspace() / "inbox" / "photos"
        inbox.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        photo_path = inbox / f"photo-{ts}{suffix}"
        try:
            file = await context.bot.get_file(file_id)
            await file.download_to_drive(photo_path)
        except Exception as e:
            logger.exception("Photo download failed")
            await update.message.reply_text(f"Couldn't save that image — {e}")
            return

        caption = (message.caption or "").strip()
        prompt = f"[photo: {photo_path}]"
        if caption:
            prompt += f" {caption}"
        logger.info("Photo saved (%s): %s", photo_path.name, caption[:80] or "(no caption)")
        await self._process_user_text(prompt, update)

    async def handle_voice_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Transcribe a Telegram voice note and route the text through handle_message."""
        if not self._is_owner(update):
            return
        voice = update.message.voice
        if voice is None:
            return
        if self.voice_engine is None:
            await update.message.reply_text(
                "Voice messages aren't enabled. Set `voice.enabled: true` in config.yaml."
            )
            return

        chat_id = update.effective_chat.id
        await self.app.bot.send_chat_action(chat_id=chat_id, action="typing")

        # Telegram voice notes are OGG/Opus
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            audio_path = Path(tmp.name)
        try:
            file = await context.bot.get_file(voice.file_id)
            await file.download_to_drive(audio_path)
            result = await self.voice_engine.transcribe(
                audio_path, language=self.config.voice.language,
            )
        except Exception as e:
            logger.exception("Voice transcription failed")
            await update.message.reply_text(f"Couldn't transcribe that — {e}")
            return
        finally:
            audio_path.unlink(missing_ok=True)

        transcript = (result.text or "").strip()
        if not transcript:
            await update.message.reply_text("(empty transcription)")
            return

        # Prefix the text with [voice] so the LLM knows the input channel.
        # Phase 2 will use this signal to decide when to reply with voice.
        # Note: Telegram Message.text is read-only, so we pass the text
        # directly into _process_user_text rather than mutating the message.
        prompt = f"[voice] {transcript}"
        logger.info("Voice transcribed (%.1fs): %s", result.duration_seconds or 0, transcript[:80])
        await self._process_user_text(prompt, update)

    def _process_commands(self, text: str) -> None:
        for cmd in extract_schedule_commands(text):
            try:
                self.scheduler.add_cron_job(
                    cmd.name, cmd.prompt, cmd.cron or None, cmd.working_dir,
                    interval_expr=(cmd.interval or None),
                    command=cmd.command,
                    timeout_seconds=cmd.timeout_seconds,
                    silent_on_empty=cmd.silent_on_empty,
                )
                kind = "command" if cmd.command else "prompt"
                logger.info("Claude scheduled %s job: %s", kind, cmd.name)
            except Exception as e:
                logger.warning("Invalid SCHEDULE block (%s): %s", cmd.name, e)
        for cmd in extract_remind_commands(text):
            try:
                self.scheduler.add_one_shot(cmd.prompt, cmd.delay)
                logger.info("Claude scheduled reminder: %s (%s)", cmd.prompt[:50], cmd.delay)
            except ValueError as e:
                logger.warning("Invalid remind command: %s", e)

    async def _process_delegations(self, text: str, send_text) -> None:
        for cmd in extract_delegate_commands(text):
            session = cmd.session or None
            task = self._enrich_with_project(cmd.task, cmd.project)
            logger.info("Delegating task: %s", cmd.task[:80])
            try:
                status = await self.tmux.dispatch(task, timeout=cmd.timeout, session=session)
                await send_text(status)
            except Exception as e:
                logger.exception("Delegation failed")
                await send_text(f"Delegation failed: {e}")

    async def _keep_typing(self, chat_id: int) -> None:
        try:
            while True:
                await self.app.bot.send_chat_action(chat_id=chat_id, action="typing")
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            pass

    # -- Module loading --

    def _load_modules(self) -> None:
        modules_path = paths.modules_dir()
        if not modules_path.exists():
            return
        for module_dir in sorted(modules_path.iterdir()):
            if not module_dir.is_dir():
                continue
            # Load telegram commands
            tg_file = module_dir / "telegram.py"
            if tg_file.exists():
                try:
                    spec = importlib.util.spec_from_file_location(
                        f"module_{module_dir.name}_telegram", tg_file,
                    )
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    if hasattr(mod, "register"):
                        mod.register(self)
                        logger.info("Loaded module telegram: %s", module_dir.name)
                except Exception:
                    logger.exception("Failed to load module %s/telegram.py", module_dir.name)

            # Load cron jobs
            cron_file = module_dir / "cron.py"
            if cron_file.exists():
                try:
                    spec = importlib.util.spec_from_file_location(
                        f"module_{module_dir.name}_cron", cron_file,
                    )
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    if hasattr(mod, "register"):
                        mod.register(self.scheduler)
                        logger.info("Loaded module cron: %s", module_dir.name)
                except Exception:
                    logger.exception("Failed to load module %s/cron.py", module_dir.name)

    # -- Lifecycle --

    def build(self) -> Application:
        self.app = (
            Application.builder()
            .token(self.config.telegram.bot_token)
            .build()
        )
        # Core commands
        self.app.add_handler(CommandHandler("start", self.cmd_start))
        self.app.add_handler(CommandHandler("reset", self.cmd_reset))
        self.app.add_handler(CommandHandler("status", self.cmd_status))
        self.app.add_handler(CommandHandler("jobs", self.cmd_jobs))
        self.app.add_handler(CommandHandler("reload", self.cmd_reload))
        self.app.add_handler(CommandHandler("cancel", self.cmd_cancel))
        self.app.add_handler(CommandHandler("schedule", self.cmd_schedule))
        self.app.add_handler(CommandHandler("remind", self.cmd_remind))
        self.app.add_handler(CommandHandler("approve", self.cmd_approve))
        self.app.add_handler(CommandHandler("approve_always", self.cmd_approve_always))
        self.app.add_handler(CommandHandler("deny", self.cmd_deny))
        self.app.add_handler(CommandHandler("code", self.cmd_code))
        self.app.add_handler(CommandHandler("codecheck", self.cmd_codecheck))

        # Load user modules (before catch-all message handler)
        self._load_modules()

        # Voice messages — transcribed and routed through handle_message
        self.app.add_handler(MessageHandler(filters.VOICE, self.handle_voice_message))

        # Photo / image-document messages — saved to inbox, path passed through handle_message
        self.app.add_handler(
            MessageHandler(filters.PHOTO | filters.Document.IMAGE, self.handle_photo_message)
        )

        # Catch-all for regular messages
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))

        return self.app

    async def warmup_voice(self) -> None:
        """Eager-load the voice model so the first message isn't laggy.

        Called by main.py after the application starts.
        """
        if self.voice_engine is None:
            return
        try:
            await self.voice_engine.warmup()
        except Exception:
            logger.exception("Voice warmup failed; first transcription may be slow")
