import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Callable, Awaitable

from . import paths

logger = logging.getLogger(__name__)

DispatchCallback = Callable[[str, str], Awaitable[None]]


class TmuxDispatchError(Exception):
    pass


class TmuxDispatch:
    def __init__(self, session_name: str | None = None) -> None:
        self.session = session_name or f"{paths.agent_name()}-code"
        self.coding_dir = paths.coding_dir()
        self.signal_dir = paths.signals_dir()
        self.signal_dir.mkdir(parents=True, exist_ok=True)
        self._callback: DispatchCallback | None = None
        self._active_task: str | None = None
        self._watcher_task: asyncio.Task | None = None

    def set_callback(self, callback: DispatchCallback) -> None:
        self._callback = callback

    async def _run(self, *args: str) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return proc.returncode, stdout.decode().strip(), stderr.decode().strip()

    async def session_exists(self) -> bool:
        rc, _, _ = await self._run("tmux", "has-session", "-t", self.session)
        return rc == 0

    async def ensure_session(self) -> None:
        if await self.session_exists():
            return
        logger.info("Creating tmux session '%s' with Claude Code", self.session)
        self.coding_dir.mkdir(parents=True, exist_ok=True)
        rc, _, stderr = await self._run(
            "tmux", "new-session", "-d", "-s", self.session, "-c", str(self.coding_dir),
        )
        if rc != 0:
            raise TmuxDispatchError(f"Failed to create tmux session: {stderr}")
        await asyncio.sleep(1)
        await self._run(
            "tmux", "send-keys", "-t", self.session,
            "claude --dangerously-skip-permissions", "C-m",
        )
        logger.info("Waiting for Claude Code to initialize...")
        for _ in range(30):
            await asyncio.sleep(2)
            rc, stdout, _ = await self._run(
                "tmux", "capture-pane", "-t", self.session, "-p", "-S", "-5",
            )
            if rc == 0 and "❯" in stdout:
                logger.info("Claude Code ready in session '%s'", self.session)
                return
        logger.warning("Claude Code may not be fully initialized")

    async def send_message(self, message: str) -> None:
        await self._run("tmux", "send-keys", "-t", self.session, message, "C-m")
        await asyncio.sleep(0.5)
        await self._run("tmux", "send-keys", "-t", self.session, "Enter", "C-m")
        await asyncio.sleep(0.3)
        await self._run("tmux", "send-keys", "-t", self.session, "Enter")

    async def capture_recent_output(self, lines: int = 50) -> str:
        rc, stdout, _ = await self._run(
            "tmux", "capture-pane", "-t", self.session, "-p", "-S", f"-{lines}",
        )
        return stdout if rc == 0 else ""

    @property
    def is_busy(self) -> bool:
        return self._active_task is not None

    async def dispatch(self, task_description: str, task_name: str | None = None, timeout: int = 600) -> str:
        await self.ensure_session()
        if self.is_busy:
            return f"Session '{self.session}' is working on '{self._active_task}'. Use /codecheck to see progress."

        task_id = task_name or f"task-{int(time.time())}"
        signal_file = self.signal_dir / f"{task_id}.json"
        result_file = self.signal_dir / f"{task_id}-result.md"
        signal_file.unlink(missing_ok=True)
        result_file.unlink(missing_ok=True)

        tasks_dir = paths.workspace() / "tasks"
        tasks_dir.mkdir(parents=True, exist_ok=True)
        task_file = tasks_dir / f"{task_id}.md"

        task_spec = (
            f"# Task: {task_id}\n\n"
            f"{task_description}\n\n---\n\n"
            f"## IMPORTANT: When you are completely finished with this task:\n\n"
            f"1. Write a brief summary of what you did to: `{result_file}`\n"
            f"2. Then write this exact JSON to signal completion:\n"
            f"```bash\n"
            f'echo \'{{"status":"done","task":"{task_id}","timestamp":"\'$(date -Iseconds)\'"}}\' > {signal_file}\n'
            f"```\n"
            f"Both steps are required. The signal file tells the dispatcher you're finished.\n"
        )
        task_file.write_text(task_spec)
        logger.info("Dispatching task %s to tmux '%s'", task_id, self.session)

        await self.send_message(f"Read the file {task_file} and follow its instructions exactly.")

        self._active_task = task_id
        self._watcher_task = asyncio.create_task(
            self._watch_for_completion(task_id, signal_file, result_file, timeout)
        )
        return f"Task '{task_id}' dispatched to {self.session}. You'll be notified when it's done."

    async def _watch_for_completion(self, task_id: str, signal_file: Path,
                                     result_file: Path, timeout: int) -> None:
        try:
            elapsed = 0
            while elapsed < timeout:
                await asyncio.sleep(5)
                elapsed += 5
                if signal_file.exists():
                    try:
                        json.loads(signal_file.read_text())
                    except (json.JSONDecodeError, OSError):
                        continue
                    result_text = result_file.read_text() if result_file.exists() else "Task completed."
                    logger.info("Task %s completed after %ds", task_id, elapsed)
                    signal_file.unlink(missing_ok=True)
                    if self._callback:
                        await self._callback(task_id, result_text)
                    return

            output = await self.capture_recent_output(lines=30)
            msg = f"Task '{task_id}' timed out after {timeout}s.\n\nRecent output:\n{output}"
            if self._callback:
                await self._callback(task_id, msg)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.exception("Watcher error for task %s", task_id)
            if self._callback:
                await self._callback(task_id, f"Watcher error: {e}")
        finally:
            self._active_task = None
            self._watcher_task = None
