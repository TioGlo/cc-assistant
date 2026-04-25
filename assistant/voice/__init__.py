"""Voice input pipeline — pluggable transcription engines.

Public API:
    VoiceConfig            — dataclass parsed from config.yaml
    TranscriptionResult    — dataclass returned by every engine
    TranscriptionEngine    — Protocol all engines implement
    register_engine(name)  — class decorator for new engines
    get_engine(cfg)        — factory; returns a configured engine instance
    list_engines()         — names registered

Adding a new engine: drop a file at `assistant/voice/engines/<name>.py`,
define a class with `@register_engine("<name>")` implementing the Protocol,
and add `from . import <name>` to `engines/__init__.py`. No central edit.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@dataclass
class VoiceConfig:
    """Voice input configuration parsed from config.yaml's `voice:` block."""
    enabled: bool = False
    engine: str = "faster_whisper"
    model: str = "base.en"
    language: str | None = "en"
    # Engine-specific options. Each engine documents its own keys.
    engine_options: dict = field(default_factory=dict)


@dataclass
class TranscriptionResult:
    text: str
    language: str | None = None
    duration_seconds: float | None = None


@runtime_checkable
class TranscriptionEngine(Protocol):
    """Engines must implement these four classmethods/methods.

    Engines should lazy-import their heavy deps (faster-whisper, openai, etc.)
    inside is_available() / from_config() so that selecting a different engine
    doesn't pull in unused dependencies.
    """
    name: ClassVar[str]

    @classmethod
    def is_available(cls) -> bool:
        """Cheap probe: True if deps + binaries + models are present."""
        ...

    @classmethod
    def from_config(cls, cfg: VoiceConfig) -> "TranscriptionEngine":
        """Construct from voice config. Validate options here."""
        ...

    async def warmup(self) -> None:
        """Load the model into memory. Called once at bot start."""
        ...

    async def transcribe(
        self, audio: Path, language: str | None = None
    ) -> TranscriptionResult:
        """Transcribe an OGG/Opus or WAV file. Other formats may be supported."""
        ...


# --- Registry ---

_ENGINES: dict[str, type[TranscriptionEngine]] = {}


def register_engine(name: str):
    """Class decorator. Adds the engine class to the registry under `name`."""
    def decorator(cls):
        cls.name = name
        _ENGINES[name] = cls
        return cls
    return decorator


def list_engines() -> list[str]:
    _ensure_engines_loaded()
    return sorted(_ENGINES)


def get_engine(cfg: VoiceConfig) -> TranscriptionEngine:
    """Construct a configured engine. Raises ValueError on unknown / unavailable."""
    _ensure_engines_loaded()
    engine_cls = _ENGINES.get(cfg.engine)
    if not engine_cls:
        raise ValueError(
            f"Unknown voice engine: {cfg.engine!r}. "
            f"Available: {list(_ENGINES)}"
        )
    if not engine_cls.is_available():
        raise RuntimeError(
            f"Voice engine {cfg.engine!r} is not available. "
            f"Check that its dependencies / binaries / models are installed."
        )
    return engine_cls.from_config(cfg)


_engines_loaded = False


def _ensure_engines_loaded() -> None:
    """Import the engines package once so each engine's @register_engine fires."""
    global _engines_loaded
    if _engines_loaded:
        return
    _engines_loaded = True
    from . import engines  # noqa: F401  — import for side effects (registration)
