"""OpenAI Whisper API engine — example HTTP-based engine.

Pay-per-use, $0.006/min as of 2026-04. Audio leaves the box. Included as a
reference implementation; not a privacy-first choice.

Engine options (under `voice.engine_options:` in config.yaml):
    api_key: sk-...                 (REQUIRED — supports ${OPENAI_API_KEY})
    base_url: https://api.openai.com/v1   (override for OpenAI-compatible APIs)
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from .. import (
    TranscriptionEngine,
    TranscriptionResult,
    VoiceConfig,
    register_engine,
)

logger = logging.getLogger(__name__)


@register_engine("openai_api")
class OpenAIApiEngine:
    def __init__(self, api_key: str, model: str, base_url: str | None) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url

    @classmethod
    def is_available(cls) -> bool:
        try:
            import openai  # noqa: F401
            return True
        except ImportError:
            return False

    @classmethod
    def from_config(cls, cfg: VoiceConfig) -> "OpenAIApiEngine":
        opts = cfg.engine_options or {}
        api_key = opts.get("api_key") or os.environ.get("OPENAI_API_KEY")
        # Allow `${VAR}` style expansion for the api_key value.
        if api_key and api_key.startswith("${") and api_key.endswith("}"):
            api_key = os.environ.get(api_key[2:-1], "")
        if not api_key:
            raise RuntimeError(
                "openai_api engine requires `voice.engine_options.api_key` "
                "or the OPENAI_API_KEY environment variable"
            )
        return cls(api_key=api_key, model=cfg.model, base_url=opts.get("base_url"))

    async def warmup(self) -> None:
        return None

    async def transcribe(
        self, audio: Path, language: str | None = None
    ) -> TranscriptionResult:
        return await asyncio.to_thread(self._transcribe_sync, audio, language)

    def _transcribe_sync(
        self, audio: Path, language: str | None
    ) -> TranscriptionResult:
        from openai import OpenAI

        kwargs = {"api_key": self.api_key}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        client = OpenAI(**kwargs)

        with audio.open("rb") as f:
            resp = client.audio.transcriptions.create(
                model=self.model,
                file=f,
                language=language,
            )
        return TranscriptionResult(text=resp.text, language=language)
