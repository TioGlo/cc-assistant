import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Callable, Awaitable

from . import paths
from .config import CCAgent

logger = logging.getLogger(__name__)

DispatchCallback = Callable[[str, str], Awaitable[None]]


class TmuxDispatchError(Exception):
    pass


class TmuxSession:
    """A single tmux Claude Code session."""

    def __init__(self, agent: CCAgent) -> None:
        self.name = agent.name
        self.tmux_session = agent.tmux_session
        self.working_dir = Path(agent.working_dir).expanduser() if agent.working_dir else paths.coding_dir()
        self.permission_mode = agent.permission_mode
        self._active_task: str | None = None
        self._watcher_task: asyncio.Task | None = None
        self._session_file = paths.signals_dir() / f"tmux-session-{self.name}.json"

    def _load_claude_session_id(self) -> str | None:
        """Load the Claude Code session ID for this tmux agent."""
        if self._session_file.exists():
            try:
                data = json.loads(self._session_file.read_text())
                return data.get("session_id")
            except (json.JSONDecodeError, OSError):
                pass
        return None

    def _save_claude_session_id(self, session_id: str) -> None:
        """Save the Claude Code session ID for this tmux agent."""
        self._session_file.write_text(json.dumps({
            "session_id": session_id,
            "agent": self.name,
            "updated_at": time.time(),
        }, indent=2))

    async def _run(self, *args: str) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return proc.returncode, stdout.decode().strip(), stderr.decode().strip()

    async def exists(self) -> bool:
        rc, _, _ = await self._run("tmux", "has-session", "-t", self.tmux_session)
        return rc == 0

    async def ensure(self) -> None:
        if await self.exists():
            return
        logger.info("Creating tmux session '%s' with Claude Code", self.tmux_session)
        self.working_dir.mkdir(parents=True, exist_ok=True)
        rc, _, stderr = await self._run(
            "tmux", "new-session", "-d", "-s", self.tmux_session, "-c", str(self.working_dir),
        )
        if rc != 0:
            raise TmuxDispatchError(f"Failed to create tmux session: {stderr}")
        await asyncio.sleep(1)
        # Build claude command with --resume if we have a prior session
        claude_cmd = f"claude --{self.permission_mode}"
        session_id = self._load_claude_session_id()
        if session_id:
            claude_cmd += f" --resume {session_id}"
            logger.info("Resuming Claude session '%s' in '%s'", session_id[:12], self.tmux_session)
        await self._run(
            "tmux", "send-keys", "-t", self.tmux_session,
            claude_cmd, "C-m",
        )
        logger.info("Waiting for Claude Code to initialize in '%s'...", self.tmux_session)
        for _ in range(30):
            await asyncio.sleep(2)
            rc, stdout, _ = await self._run(
                "tmux", "capture-pane", "-t", self.tmux_session, "-p", "-S", "-5",
            )
            if rc == 0 and "❯" in stdout:
                logger.info("Claude Code ready in '%s'", self.tmux_session)
                return
        logger.warning("Claude Code may not be fully initialized in '%s'", self.tmux_session)

    async def send_message(self, message: str) -> None:
        # Step 1: Type the text into the TUI (no Enter yet)
        await self._run("tmux", "send-keys", "-t", self.tmux_session, message)
        # Step 2: Wait for the TUI to process the pasted text
        await asyncio.sleep(1.0)
        # Step 3: Press Enter to submit
        await self._run("tmux", "send-keys", "-t", self.tmux_session, "Enter")
        # Step 4: Extra Enter after a pause — Claude Code's TUI sometimes needs
        # a second Enter to actually begin processing (confirmation prompt)
        await asyncio.sleep(0.5)
        await self._run("tmux", "send-keys", "-t", self.tmux_session, "Enter")

    async def capture_recent_output(self, lines: int = 50) -> str:
        rc, stdout, _ = await self._run(
            "tmux", "capture-pane", "-t", self.tmux_session, "-p", "-S", f"-{lines}",
        )
        return stdout if rc == 0 else ""

    @property
    def is_busy(self) -> bool:
        return self._active_task is not None


class TmuxDispatch:
    """Manages multiple tmux Claude Code sessions and dispatches tasks to them."""

    def __init__(self, agents: list[CCAgent] | None = None) -> None:
        self.signal_dir = paths.signals_dir()
        self.signal_dir.mkdir(parents=True, exist_ok=True)
        self._callback: DispatchCallback | None = None
        self._sessions: dict[str, TmuxSession] = {}

        if agents:
            for agent in agents:
                self._sessions[agent.name] = TmuxSession(agent)

    @property
    def default_session_name(self) -> str:
        """Name of the first (default) session, or fallback."""
        if self._sessions:
            return next(iter(self._sessions)).strip()
        return f"{paths.agent_name()}-code"

    def set_callback(self, callback: DispatchCallback) -> None:
        self._callback = callback

    def _get_session(self, name: str | None = None) -> TmuxSession:
        """Get a session by name, or the default."""
        if name and name in self._sessions:
            return self._sessions[name]
        if not name and self._sessions:
            return next(iter(self._sessions.values()))
        # Fallback: create an ad-hoc session
        fallback_name = name or self.default_session_name
        if fallback_name not in self._sessions:
            agent = CCAgent(name=fallback_name, tmux_session=fallback_name)
            self._sessions[fallback_name] = TmuxSession(agent)
        return self._sessions[fallback_name]

    async def session_exists(self, name: str | None = None) -> bool:
        return await self._get_session(name).exists()

    async def capture_recent_output(self, name: str | None = None, lines: int = 50) -> str:
        return await self._get_session(name).capture_recent_output(lines)

    async def dispatch(self, task_description: str, task_name: str | None = None,
                       timeout: int = 600, session: str | None = None) -> str:
        sess = self._get_session(session)
        await sess.ensure()

        if sess.is_busy:
            return f"Session '{sess.tmux_session}' is working on '{sess._active_task}'. Use /codecheck to see progress."

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
        logger.info("Dispatching task %s to tmux '%s'", task_id, sess.tmux_session)

        await sess.send_message(f"Read the file {task_file} and follow its instructions exactly.")

        sess._active_task = task_id
        sess._watcher_task = asyncio.create_task(
            self._watch_for_completion(sess, task_id, signal_file, result_file, timeout)
        )
        return f"Task '{task_id}' dispatched to {sess.tmux_session}. You'll be notified when it's done."

    async def _watch_for_completion(self, sess: TmuxSession, task_id: str,
                                     signal_file: Path, result_file: Path, timeout: int) -> None:
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
                    # Capture Claude session ID for future --resume
                    self._capture_session_id(sess)
                    if self._callback:
                        await self._callback(task_id, result_text)
                    return

            output = await sess.capture_recent_output(lines=30)
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
            sess._active_task = None
            sess._watcher_task = None

    @staticmethod
    def _capture_session_id(sess: TmuxSession) -> None:
        """Find the most recent Claude session ID for this agent's working directory."""
        try:
            # Claude stores sessions under ~/.claude/projects/{path-hash}/
            # The path hash is the working dir with / replaced by -
            claude_dir = Path.home() / ".claude" / "projects"
            if not claude_dir.exists():
                return
            # Find the project dir matching the agent's working directory
            dir_key = str(sess.working_dir).replace("/", "-").lstrip("-")
            matching = [d for d in claude_dir.iterdir() if d.is_dir() and dir_key in d.name]
            if not matching:
                return
            project_dir = matching[0]
            # Find the most recent .jsonl session file
            sessions = sorted(project_dir.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
            if not sessions:
                return
            # Session ID is the filename without extension
            session_id = sessions[0].stem
            sess._save_claude_session_id(session_id)
            logger.info("Captured session ID '%s' for agent '%s'", session_id[:12], sess.name)
        except Exception as e:
            logger.debug("Could not capture session ID for '%s': %s", sess.name, e)
