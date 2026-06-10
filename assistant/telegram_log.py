"""Rolling log of outbound Telegram messages, shared across sessions.

Each cron job runs in its own claude session (chat-haiku, chat-sonnet, ...),
so messages those jobs post to Telegram are invisible to the main `chat`
session that handles user replies. This log gives the main session a
just-in-time view of what other sessions said, prepended only when there
has been non-chat activity since the user's last message.
"""

import json
from datetime import datetime
from pathlib import Path
from threading import Lock

from . import paths
from .fileio import atomic_write_text

_MAX_ENTRIES = 20
_MAX_TEXT_LEN = 500
_lock = Lock()


def _log_path() -> Path:
    return paths.root() / "telegram-log.jsonl"


def append(source: str, text: str) -> None:
    """Append an outbound message to the rolling log."""
    truncated = text[:_MAX_TEXT_LEN] + ("..." if len(text) > _MAX_TEXT_LEN else "")
    entry = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "source": source,
        "text": truncated,
    }
    path = _log_path()
    with _lock:
        entries = read_all()
        entries.append(entry)
        entries = entries[-_MAX_ENTRIES:]
        atomic_write_text(path, "".join(json.dumps(e) + "\n" for e in entries))


def read_all() -> list[dict]:
    path = _log_path()
    if not path.exists():
        return []
    entries = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def entries_since(
    timestamp: datetime, exclude_sources: set[str] | None = None,
) -> list[dict]:
    cutoff = timestamp.isoformat(timespec="seconds")
    out = []
    for e in read_all():
        # Tolerate wrong-shape entries (hand edit, torn write that still
        # parses): a KeyError here would break ALL inbound chat, since this
        # runs on the user-message hot path.
        if not isinstance(e, dict) or not isinstance(e.get("timestamp"), str):
            continue
        if e["timestamp"] <= cutoff:
            continue
        if exclude_sources and e.get("source") in exclude_sources:
            continue
        out.append(e)
    return out


def render_for_prompt(entries: list[dict]) -> str:
    if not entries:
        return ""
    lines = ["Recent Telegram activity from other sessions:"]
    for e in entries:
        ts = str(e.get("timestamp", "")).replace("T", " ")
        lines.append(f"[{ts} {e.get('source', '?')}] {e.get('text', '')}")
    return "\n".join(lines)
