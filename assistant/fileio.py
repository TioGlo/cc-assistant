"""Atomic file-write helper for the JSON state files.

Several state files (scheduler-jobs.json, scheduler-reminders.json) refuse
to start on corruption by design, so a torn write — crash or OOM-kill
mid-write_text — would turn into a boot-blocking outage. Writing to a temp
file in the same directory and os.replace()-ing it in makes the swap atomic
on POSIX: readers (including other agents editing these files) see either
the old or the new contents, never a partial file.

Deliberately no fsync: several callers run on the asyncio event loop's hot
path (session save and telegram-log rewrite happen on every chat message),
and fsync can stall for tens of ms under disk load. os.replace alone fully
protects against process-level crashes — the common case — and ext4's
auto_da_alloc heuristic flushes data on rename-over-existing, so even on
power loss the realistic outcome is the previous version, not a torn file.
"""

import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        try:
            # Preserve the target's existing mode — mkstemp creates 0600,
            # which would silently tighten files other tools read.
            os.fchmod(fd, os.stat(path).st_mode & 0o777)
        except FileNotFoundError:
            pass  # new file: keep mkstemp's conservative 0600
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
