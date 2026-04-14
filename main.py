import argparse
import logging
import sys
from pathlib import Path

from assistant import paths
from assistant.bot import AssistantBot
from assistant.bridge import ClaudeBridge
from assistant.config import load_config
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

    # Tmux dispatch results go to Telegram
    async def on_code_result(task_id: str, result_text: str) -> None:
        await bot.on_job_result(f"Code: {task_id}", result_text)
    bot.tmux.set_callback(on_code_result)

    # Slack triage
    async def on_slack_triage(prompt: str) -> None:
        result_text, _ = await bridge.send_simple(prompt)
        if "nothing notable" not in result_text.lower():
            await bot.on_job_result("Slack digest", result_text)
    slack_monitor.set_triage_callback(on_slack_triage)

    # Build Telegram app
    app = bot.build()

    async def post_init(application) -> None:
        scheduler.start()
        if config.scheduler.jobs:
            scheduler.load_config_jobs(config.scheduler.jobs)
        scheduler.load_dynamic_jobs()
        logger.info("Scheduler started")
        await slack_monitor.start()

    async def post_shutdown(application) -> None:
        await slack_monitor.stop()
        scheduler.stop()
        logger.info("Shutdown complete")

    app.post_init = post_init
    app.post_shutdown = post_shutdown

    logger.info("Starting %s bot...", paths.agent_name())
    app.run_polling(drop_pending_updates=True, bootstrap_retries=-1)


if __name__ == "__main__":
    main()
