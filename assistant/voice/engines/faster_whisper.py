"""faster-whisper engine — the default. Pure-Python (CTranslate2 backed),
runs on CPU or GPU, no external binary needed.

Install: `uv sync --extra voice`  (pulls faster-whisper into the env)

Engine options (under `voice.engine_options:` in config.yaml):
    compute_type: int8 | int8_float16 | float16 | float32   (default: int8)
    device:       cpu | cuda | auto                          (default: cpu)
    download_root: optional path for model cache             (default: HF cache)
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from .. import (
    TranscriptionEngine,
    TranscriptionResult,
    VoiceConfig,
    register_engine,
)

logger = logging.getLogger(__name__)


@register_engine("faster_whisper")
class FasterWhisperEngine:
    def __init__(self, model_name: str, options: dict) -> None:
        self.model_name = model_name
        self.options = options
        self._model = None
        self._lock = asyncio.Lock()  # faster-whisper isn't thread-safe

    @classmethod
    def is_available(cls) -> bool:
        try:
            import faster_whisper  # noqa: F401
            return True
        except ImportError:
            return False

    @classmethod
    def from_config(cls, cfg: VoiceConfig) -> "FasterWhisperEngine":
        return cls(model_name=cfg.model, options=dict(cfg.engine_options or {}))

    async def warmup(self) -> None:
        """Load the model in a thread so we don't block the event loop."""
        if self._model is not None:
            return
        logger.info("Loading faster-whisper model: %s", self.model_name)
        await asyncio.to_thread(self._load_model)
        logger.info("faster-whisper model loaded")

    def _load_model(self) -> None:
        from faster_whisper import WhisperModel

        kwargs = {
            "compute_type": self.options.get("compute_type", "int8"),
            "device": self.options.get("device", "cpu"),
        }
        if "download_root" in self.options:
            kwargs["download_root"] = self.options["download_root"]
        self._model = WhisperModel(self.model_name, **kwargs)

    async def transcribe(
        self, audio: Path, language: str | None = None
    ) -> TranscriptionResult:
        if self._model is None:
            await self.warmup()

        async with self._lock:
            return await asyncio.to_thread(self._transcribe_sync, audio, language)

    def _transcribe_sync(
        self, audio: Path, language: str | None
    ) -> TranscriptionResult:
        # The model returns a (segments_iter, info) tuple. Iterating segments
        # is what actually does the transcription — we materialize it here.
        segments, info = self._model.transcribe(
            str(audio),
            language=language,
            beam_size=int(self.options.get("beam_size", 5)),
            vad_filter=bool(self.options.get("vad_filter", True)),
        )
        text = " ".join(segment.text.strip() for segment in segments).strip()
        return TranscriptionResult(
            text=text,
            language=info.language,
            duration_seconds=info.duration,
        )
