"""Atomic file-write helper for the JSON state files.

Several state files (scheduler-jobs.json, scheduler-reminders.json) refuse
to start on corruption by design, so a torn write — crash or OOM-kill
mid-write_text — would turn into a boot-blocking outage. Writing to a temp
file in the same directory and os.replace()-ing it in makes the swap atomic
on POSIX: readers (including other agents editing these files) see either
the old or the new contents, never a partial file.
"""

import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
