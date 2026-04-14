import asyncio
import json
import logging
from pathlib import Path

from .config import ClaudeConfig

logger = logging.getLogger(__name__)

AUTH_ERROR_MARKERS = [
    "InvalidToken", "token was rejected", "authentication",
    "unauthorized", "credentials", "login",
]


class BridgeError(Exception):
    pass


class AuthError(BridgeError):
    pass


class ClaudeBridge:
    def __init__(self, config: ClaudeConfig, workspace: Path) -> None:
        self.config = config
        self.workspace = workspace

    def _build_args(self, message: str, session_id: str | None = None) -> list[str]:
        args = [
            "claude", "-p", message,
            "--output-format", "json",
            "--permission-mode", self.config.permission_mode,
            "--model", self.config.model,
            "--max-turns", str(self.config.max_turns),
        ]
        if session_id:
            args.extend(["--resume", session_id])
        system_prompt = self.config.system_prompt or ""
        for fpath in self.config.system_prompt_files:
            p = Path(fpath).expanduser()
            if p.exists():
                try:
                    system_prompt += "\n\n" + p.read_text().strip()
                except OSError:
                    pass
        if system_prompt.strip():
            args.extend(["--append-system-prompt", system_prompt.strip()])
        if self.config.allowed_tools:
            args.extend(["--allowed-tools", *self.config.allowed_tools])
        if self.config.mcp_config:
            args.extend(["--mcp-config", str(Path(self.config.mcp_config).expanduser())])
        return args

    async def send_simple(
        self, message: str, session_id: str | None = None, working_dir: str | None = None,
    ) -> tuple[str, str | None]:
        cwd = Path(working_dir).expanduser() if working_dir else self.workspace
        cwd.mkdir(parents=True, exist_ok=True)

        args = self._build_args(message, session_id)
        logger.debug("Spawning: %s (cwd=%s)", " ".join(args[:6]) + "...", cwd)

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
        )

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.config.timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise BridgeError(f"Claude timed out after {self.config.timeout}s")

        stderr_text = ""
        if stderr:
            stderr_text = stderr.decode(errors="replace").strip()
            if stderr_text:
                logger.warning("Claude stderr: %s", stderr_text[:500])

        if any(m.lower() in stderr_text.lower() for m in AUTH_ERROR_MARKERS):
            raise AuthError("Claude authentication failed. Run `claude auth login` to re-authenticate.")

        stdout_text = stdout.decode(errors="replace").strip()
        if not stdout_text:
            if proc.returncode != 0:
                raise BridgeError(f"Claude exited {proc.returncode}: {stderr_text[:200]}")
            return "(no response)", session_id

        try:
            data = json.loads(stdout_text)
        except json.JSONDecodeError:
            return stdout_text, session_id

        result_text = data.get("result", "(no response)")
        new_session_id = data.get("session_id", session_id)

        if data.get("is_error"):
            logger.error("Claude returned error: %s", result_text)

        return result_text, new_session_id
