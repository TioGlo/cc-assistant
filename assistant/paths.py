"""Centralized path resolution for the assistant.

All runtime paths derive from a single AGENT_ROOT.
Resolution order: CLI --agent-dir > AGENT_ROOT env var > ~/.assistant/
"""

import os
from pathlib import Path

_DEFAULT_ROOT = "~/.assistant"
_agent_root: Path | None = None


def init(agent_dir: str | None = None) -> Path:
    """Initialize and return the agent root. Call once at startup."""
    global _agent_root
    raw = agent_dir or os.environ.get("AGENT_ROOT") or _DEFAULT_ROOT
    _agent_root = Path(raw).expanduser().resolve()
    return _agent_root


def root() -> Path:
    if _agent_root is None:
        raise RuntimeError("paths.init() must be called before accessing paths")
    return _agent_root


def config_file() -> Path:
    return root() / "config.yaml"


def workspace() -> Path:
    return root() / "workspace"


def session_file() -> Path:
    return root() / "session.json"


def scheduler_jobs_file() -> Path:
    return root() / "scheduler-jobs.json"


def signals_dir() -> Path:
    return root() / "signals"


def coding_dir() -> Path:
    return root() / "coding"


def pending_approvals_dir() -> Path:
    return root() / "pending-approvals"


def modules_dir() -> Path:
    return root() / "modules"


def agent_name() -> str:
    """Derive agent name from directory (e.g. '.assistant' -> 'assistant', '.luci' -> 'luci')."""
    return root().name.lstrip(".")
