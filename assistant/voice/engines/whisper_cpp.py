"""whisper.cpp engine — example subprocess-based engine.

Mirrors openclaw's pattern. Useful if you don't want Python ML deps in the
bot environment, or want to use whisper.cpp's Vulkan/Metal/CUDA acceleration.

Install (Linux):    apt install whisper-cpp  (or build from source)
Install (macOS):    brew install whisper-cpp
Models:             https://huggingface.co/ggerganov/whisper.cpp

Engine options (under `voice.engine_options:` in config.yaml):
    binary:     name of the CLI binary           (default: "whisper-cli")
    model_path: path to a ggml-*.bin model file  (REQUIRED)
    threads:    integer, parallelism             (default: 4)
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from .. import (
    TranscriptionEngine,
    TranscriptionResult,
    VoiceConfig,
    register_engine,
)

logger = logging.getLogger(__name__)


@register_engine("whisper_cpp")
class WhisperCppEngine:
    def __init__(self, binary: str, model_path: Path, threads: int) -> None:
        self.binary = binary
        self.model_path = model_path
        self.threads = threads
        self._lock = asyncio.Lock()

    @classmethod
    def is_available(cls) -> bool:
        # Probe the default binary name. If a config specifies a different
        # name, from_config will re-validate.
        return shutil.which("whisper-cli") is not None or shutil.which("whisper-cpp") is not None

    @classmethod
    def from_config(cls, cfg: VoiceConfig) -> "WhisperCppEngine":
        opts = cfg.engine_options or {}
        binary = opts.get("binary") or shutil.which("whisper-cli") or shutil.which("whisper-cpp")
        if not binary:
            raise RuntimeError("whisper-cli / whisper-cpp not found on PATH")

        model_path = opts.get("model_path")
        if not model_path:
            raise RuntimeError(
                "whisper_cpp engine requires `voice.engine_options.model_path` "
                "pointing at a ggml-*.bin file"
            )
        model_path = Path(model_path).expanduser()
        if not model_path.exists():
            raise RuntimeError(f"whisper_cpp model not found: {model_path}")

        return cls(
            binary=binary,
            model_path=model_path,
            threads=int(opts.get("threads", 4)),
        )

    async def warmup(self) -> None:
        # whisper.cpp is a CLI — nothing to load in-process.
        return None

    async def transcribe(
        self, audio: Path, language: str | None = None
    ) -> TranscriptionResult:
        async with self._lock:
            return await asyncio.to_thread(self._transcribe_sync, audio, language)

    def _transcribe_sync(
        self, audio: Path, language: str | None
    ) -> TranscriptionResult:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_base = Path(tmpdir) / "out"
            args = [
                self.binary,
                "-m", str(self.model_path),
                "-otxt",
                "-of", str(output_base),
                "-np",  # no progress
                "-nt",  # no timestamps
                "-t", str(self.threads),
                str(audio),
            ]
            if language:
                args.extend(["-l", language])

            result = subprocess.run(
                args, capture_output=True, text=True, timeout=300,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"whisper-cli exited {result.returncode}: {result.stderr.strip()}"
                )

            txt_path = Path(f"{output_base}.txt")
            text = txt_path.read_text().strip() if txt_path.exists() else ""

        return TranscriptionResult(text=text, language=language)
