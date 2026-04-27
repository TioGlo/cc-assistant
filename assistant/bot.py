import asyncio
import importlib.util
import logging
import tempfile
import time
from datetime import datetime
from pathlib import Path

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import paths
from .bridge import AuthError, BridgeError, ClaudeBridge
from .config import Config, JobDelivery
from .formatter import (
    extract_delegate_commands,
    extract_remind_commands,
    extract_schedule_commands,
    split_message,
    strip_commands,
)
from .scheduler import Scheduler
from .session import SessionManager
from .tmux_dispatch import TmuxDispatch
from .voice import TranscriptionEngine, get_engine

logger = logging.getLogger(__name__)

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

    async def _send_text(self, chat_id: int, text: str) -> None:
        for chunk in split_message(text):
            await self.app.bot.send_message(chat_id=chat_id, text=chunk)

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
        await self._send_text(chat_id, header + clean_text)

    async def _process_delegations_from_job(self, text: str) -> None:
        for cmd in extract_delegate_commands(text):
            session = cmd.session or None
            task = self._enrich_with_project(cmd.task, cmd.project)
            logger.info("Cron job delegating task: %s", cmd.task[:80])
            try:
                status = await self.tmux.dispatch(task, timeout=cmd.timeout, session=session)
                await self._send_text(self.config.telegram.owner_id, f"Delegated: {status}")
            except Exception as e:
                logger.exception("Cron delegation failed")
                await self._send_text(self.config.telegram.owner_id, f"Delegation failed: {e}")

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
        lines = [f"- {j['name']} (next: {j['next_run']})\n  {j['prompt']}" for j in jobs]
        await update.message.reply_text("\n".join(lines))

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

        async def send_text(chunk: str) -> None:
            await update.message.reply_text(chunk)

        async def send_typing() -> None:
            await self._keep_typing(chat_id)

        await self.process_text_input(
            text=text, session_key="chat",
            send_text=send_text, send_typing=send_typing,
        )

    async def process_text_input(
        self, text: str, session_key: str,
        send_text, send_typing=None,
    ) -> None:
        """Transport-agnostic core: run text through the LLM, send the reply,
        process embedded commands/delegations.

        Used by both Telegram message handlers and the Discord bot.
        - text: user prompt (already cleaned of channel-specific markup)
        - session_key: claude session ID key — "chat" for Telegram, "discord:<channel_id>" for Discord
        - send_text(chunk): async callable that sends a chunk to the user
        - send_typing(): optional async callable that pulses a typing indicator
          until cancelled
        """
        typing_task = asyncio.create_task(send_typing()) if send_typing else None
        try:
            session_id = self.session_manager.get_session_id(session_key)
            try:
                response_text, new_session_id = await self.bridge.send_simple(
                    text, session_id=session_id,
                )
            except BridgeError as e:
                if session_id and "No conversation found" in str(e):
                    logger.info("Stale session %s, falling back to fresh", session_key)
                    self.session_manager.clear_session(session_key)
                    response_text, new_session_id = await self.bridge.send_simple(text)
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
        except AuthError as e:
            logger.error("Auth failure: %s", e)
            await send_text("Authentication expired. Run `claude auth login` on the server.")
        except Exception as e:
            logger.exception("Error handling message")
            await send_text(f"Error: {e}")
        finally:
            if typing_task is not None:
                typing_task.cancel()

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
            self.scheduler.add_cron_job(cmd.name, cmd.prompt, cmd.cron, cmd.working_dir)
            logger.info("Claude scheduled cron job: %s", cmd.name)
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
