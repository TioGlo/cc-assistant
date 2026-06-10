import json
import logging
from pathlib import Path

from .fileio import atomic_write_text

logger = logging.getLogger(__name__)


class SessionManager:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                self._cache = json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Failed to load session file: %s", e)
                self._cache = {}

    def _save(self) -> None:
        try:
            atomic_write_text(self.path, json.dumps(self._cache, indent=2))
        except OSError as e:
            logger.error("Failed to save session file: %s", e)

    def get_session_id(self, key: str = "chat") -> str | None:
        return self._cache.get(key)

    def set_session_id(self, session_id: str, key: str = "chat") -> None:
        # Reject None / empty keys loudly. Python's json.dumps silently
        # serializes a None dict key as the literal string "null", which
        # produces a phantom "null" session entry that masks the upstream
        # bug (a job dict with explicit "session": null defeating the
        # call-site default). Surface it as a real error instead.
        if not key:
            raise ValueError(
                f"set_session_id refused: key={key!r} is falsy. "
                f"Caller must pass a non-empty string session key."
            )
        self._cache[key] = session_id
        self._save()

    def clear_session(self, key: str = "chat") -> None:
        self._cache.pop(key, None)
        self._save()

    def clear_all(self) -> None:
        self._cache.clear()
        self._save()
