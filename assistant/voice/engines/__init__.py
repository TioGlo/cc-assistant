"""Built-in transcription engines.

Importing this package is what triggers each engine's @register_engine
decorator. Add new engines by:
    1. Creating `<name>.py` in this directory with a @register_engine class
    2. Adding `from . import <name>` below

Engines that fail to import (missing deps) are skipped silently — they'll
fail loudly via `is_available() == False` if the user tries to select them.
"""
import logging

logger = logging.getLogger(__name__)


def _try_import(module: str) -> None:
    try:
        __import__(__name__ + "." + module)
    except Exception as e:
        logger.debug("Voice engine %s did not register: %s", module, e)


# Register built-in engines. Failure to import is non-fatal — the engine
# just won't appear in list_engines().
for _mod in ("faster_whisper", "whisper_cpp", "openai_api"):
    _try_import(_mod)
