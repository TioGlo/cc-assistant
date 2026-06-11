import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

from assistant import paths
from assistant.bot import AssistantBot
from assistant.bridge import ClaudeBridge
from assistant.config import JobDelivery, load_config
from assistant.discord_bot import DiscordBot
from assistant.scheduler import Scheduler
from assistant.session import SessionManager
from assistant.slack_monitor import SlackMonitor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="Personal AI assistant powered by Claude Code")
    parser.add_argument("--agent-dir", default=None, help="Agent root directory (default: ~/.assistant)")
    args = parser.parse_args()

    # Initialize paths — must happen before anything else
    agent_root = paths.init(args.agent_dir)
    logger.info("Agent root: %s", agent_root)

    # Find config: agent root first, then CWD (for development)
    config_path = paths.config_file()
    if not config_path.exists():
        config_path = Path("config.yaml")
    if not config_path.exists():
        print(
            f"config.yaml not found.\n"
            f"Searched: {paths.config_file()}, ./config.yaml\n"
            f"Run install.sh or copy config.example.yaml to {paths.config_file()}"
        )
        sys.exit(1)

    config = load_config(config_path)
    logger.info("Loaded config: model=%s, owner_id=%d", config.claude.model, config.telegram.owner_id)

    # Ensure directories exist
    paths.workspace().mkdir(parents=True, exist_ok=True)
    (paths.workspace() / "projects").mkdir(parents=True, exist_ok=True)
    (paths.workspace() / "areas").mkdir(parents=True, exist_ok=True)
    paths.signals_dir().mkdir(parents=True, exist_ok=True)
    paths.pending_approvals_dir().mkdir(parents=True, exist_ok=True)
    paths.modules_dir().mkdir(parents=True, exist_ok=True)

    # Initialize components
    bridge = ClaudeBridge(config.claude, workspace=paths.workspace())
    session_manager = SessionManager(paths.session_file())
    scheduler = Scheduler(bridge, session_manager, jobs_file=paths.scheduler_jobs_file())
    slack_monitor = SlackMonitor(config.slack)

    bot = AssistantBot(config, bridge, session_manager, scheduler)
    scheduler.set_callback(bot.on_job_result)

    # Discord transport (optional). Wired before scheduler.start() so outbound
    # cron deliveries find a ready bot.
    discord_bot = DiscordBot(config.discord, assistant_bot=bot)
    bot.set_discord_bot(discord_bot)

    # Tmux dispatch results go to Telegram. fyi by default — the dispatch
    # path literally promises "You'll be notified when it's done" (the model
    # can still tag ACTION/silent_log to override). The signal file is
    # already consumed by the time this fires, so a delivery failure must
    # not propagate into the watcher — preserve the result in the journal.
    # allow_commands=False: a delegated agent's result is a report, not an
    # action surface. The sub-agent runs dangerously-skip-permissions and
    # routinely ingests attacker-controllable text (web pages, issues,
    # READMEs); its result must not be able to mint SCHEDULE/DELEGATE work
    # back in the orchestrator's context. Same hardening as Slack triage.
    async def on_code_result(task_id: str, result_text: str) -> None:
        try:
            await bot.on_job_result(f"Code: {task_id}", result_text,
                                    JobDelivery(priority="fyi"), allow_commands=False,
                                    coalesce_key="code")
        except Exception:
            logger.exception("Delivery of task %s result failed; preserving here:\n%s",
                             task_id, result_text[:2000])
    bot.tmux.set_callback(on_code_result)

    # Slack triage. allow_commands=False: the triage output is derived from
    # messages arbitrary Slack users wrote — an injected SCHEDULE/DELEGATE
    # block must be stripped, never executed.
    async def on_slack_triage(prompt: str) -> None:
        result_text, _ = await bridge.send_simple(prompt)
        if "nothing notable" not in result_text.lower():
            try:
                await bot.on_job_result("Slack digest", result_text,
                                        allow_commands=False)
            except Exception:
                logger.exception("Delivery of Slack digest failed; preserving here:\n%s",
                                 result_text[:2000])
    slack_monitor.set_triage_callback(on_slack_triage)

    # Build Telegram app
    app = bot.build()

    async def post_init(application) -> None:
        # SIGHUP = reload scheduler jobs/reminders, not death. Registered
        # FIRST — before the slow startup awaits (whisper warmup etc.) — so
        # the unprotected window is as small as possible. Without a handler,
        # SIGHUP (e.g. `systemctl kill -s HUP`) kills the bridge, and systemd
        # treats SIGHUP-death as a *clean* exit, so Restart=on-failure never
        # fires (2026-06-04 outage, 6h down).
        def _on_sighup() -> None:
            logger.info(
                "SIGHUP received — reloading scheduler jobs/reminders "
                "(config.yaml/code changes still require a restart)"
            )
            try:
                summary = scheduler.reload()
                logger.info("SIGHUP reload complete: %s", summary)
            except Exception:
                logger.exception("SIGHUP reload failed; service continues running")

        asyncio.get_running_loop().add_signal_handler(signal.SIGHUP, _on_sighup)

        scheduler.start()
        if config.scheduler.jobs:
            scheduler.seed_config_jobs(config.scheduler.jobs)
        scheduler.load_jobs()
        scheduler.load_reminders()
        if config.notifications.digest_enabled:
            # A digest_cron typo must not take the daemon down with it.
            try:
                scheduler.add_internal_cron(
                    bot.run_silent_digest, config.notifications.digest_cron,
                    job_id="_internal_silent_digest", name="silent-log morning digest",
                )
                logger.info("Silent-log digest scheduled (%s)", config.notifications.digest_cron)
            except Exception:
                logger.exception(
                    "Invalid notifications.digest_cron %r; continuing without the digest",
                    config.notifications.digest_cron)
        logger.info("Scheduler started")
        # Optional components must not take the daemon down with them: a
        # Slack/Discord API hiccup at boot would otherwise crash post_init
        # and put systemd into a restart loop while Telegram — the primary
        # surface — was perfectly capable of running.
        try:
            await slack_monitor.start()
        except Exception:
            logger.exception("Slack monitor failed to start; continuing without it")
        if config.discord.enabled and not config.discord.owner_ids:
            logger.warning(
                "Discord is enabled but discord.owner_ids is empty — the Discord "
                "surface will IGNORE ALL MESSAGES (fail-secure). Add your Discord "
                "user ID to discord.owner_ids in config.yaml to use it."
            )
        try:
            await discord_bot.start()
        except Exception:
            logger.exception("Discord bot failed to start; continuing without it")
        try:
            await bot.warmup_voice()
        except Exception:
            logger.exception("Voice warmup failed; lazy warmup may recover on first use")

    async def post_shutdown(application) -> None:
        bot.cancel_background()
        await discord_bot.stop()
        await slack_monitor.stop()
        scheduler.stop()
        logger.info("Shutdown complete")

    app.post_init = post_init
    app.post_shutdown = post_shutdown

    logger.info("Starting %s bot...", paths.agent_name())
    app.run_polling(drop_pending_updates=True, bootstrap_retries=-1)


if __name__ == "__main__":
    main()
