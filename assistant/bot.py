import asyncio
import importlib.util
import logging
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
from .bridge import AuthError, ClaudeBridge
from .config import Config
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

logger = logging.getLogger(__name__)


class AssistantBot:
    def __init__(
        self, config: Config, bridge: ClaudeBridge,
        session_manager: SessionManager, scheduler: Scheduler,
    ) -> None:
        self.config = config
        self.bridge = bridge
        self.session_manager = session_manager
        self.scheduler = scheduler
        self.tmux = TmuxDispatch()
        self._start_time = time.time()
        self.app: Application | None = None

    def _is_owner(self, update: Update) -> bool:
        user = update.effective_user
        return user is not None and user.id == self.config.telegram.owner_id

    async def _send_text(self, chat_id: int, text: str) -> None:
        for chunk in split_message(text):
            await self.app.bot.send_message(chat_id=chat_id, text=chunk)

    # -- Scheduler callback --

    async def on_job_result(self, job_name: str, result_text: str) -> None:
        chat_id = self.config.telegram.owner_id
        self._process_commands(result_text)
        await self._process_delegations_from_job(result_text)
        clean_text = strip_commands(result_text)
        if clean_text:
            header = f"**Scheduled: {job_name}**\n\n"
            await self._send_text(chat_id, header + clean_text)

    async def _process_delegations_from_job(self, text: str) -> None:
        for cmd in extract_delegate_commands(text):
            session = cmd.session or self.tmux.session
            logger.info("Cron job delegating task to %s: %s", session, cmd.task[:80])
            try:
                status = await self.tmux.dispatch(cmd.task, timeout=cmd.timeout)
                await self._send_text(self.config.telegram.owner_id, f"Delegated: {status}")
            except Exception as e:
                logger.exception("Cron delegation failed")
                await self._send_text(self.config.telegram.owner_id, f"Delegation failed: {e}")

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
        lines = [
            f"Agent: {paths.agent_name()}",
            f"Uptime: {hours}h {minutes}m {seconds}s",
            f"Model: {self.config.claude.model}",
            f"Session: {session_id or 'none (will start fresh)'}",
            f"Scheduled jobs: {len(jobs)}",
            f"Tmux session: {self.tmux.session}",
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
                f"Tmux session: {self.tmux.session}"
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
        if not await self.tmux.session_exists():
            await update.message.reply_text(
                f"No tmux session '{self.tmux.session}'. It will be created on next /code dispatch."
            )
            return
        output = await self.tmux.capture_recent_output(lines=30)
        msg = f"Session: {self.tmux.session}\n\nRecent output:\n{output}"
        for chunk in split_message(msg):
            await update.message.reply_text(chunk)

    # -- Message handler --

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return
        text = update.message.text
        if not text:
            return
        chat_id = update.effective_chat.id
        typing_task = asyncio.create_task(self._keep_typing(chat_id))
        try:
            session_id = self.session_manager.get_session_id("chat")
            response_text, new_session_id = await self.bridge.send_simple(text, session_id=session_id)
            if new_session_id:
                self.session_manager.set_session_id(new_session_id, "chat")
            self._process_commands(response_text)
            await self._process_delegations(response_text, update)
            clean_text = strip_commands(response_text)
            if clean_text:
                for chunk in split_message(clean_text):
                    await update.message.reply_text(chunk)
        except AuthError as e:
            logger.error("Auth failure: %s", e)
            await update.message.reply_text("Authentication expired. Run `claude auth login` on the server.")
        except Exception as e:
            logger.exception("Error handling message")
            await update.message.reply_text(f"Error: {e}")
        finally:
            typing_task.cancel()

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

    async def _process_delegations(self, text: str, update: Update) -> None:
        for cmd in extract_delegate_commands(text):
            session = cmd.session or self.tmux.session
            logger.info("Delegating task to %s: %s", session, cmd.task[:80])
            try:
                status = await self.tmux.dispatch(cmd.task, timeout=cmd.timeout)
                await update.message.reply_text(status)
            except Exception as e:
                logger.exception("Delegation failed")
                await update.message.reply_text(f"Delegation failed: {e}")

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

        # Catch-all for regular messages
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))

        return self.app
