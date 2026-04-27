import json
import re
from dataclasses import dataclass

TELEGRAM_MAX_LEN = 4096

SCHEDULE_PATTERN = re.compile(r"<!--SCHEDULE:(.*?)-->", re.DOTALL)
REMIND_PATTERN = re.compile(r"<!--REMIND:(.*?)-->", re.DOTALL)
DELEGATE_PATTERN = re.compile(r"<!--DELEGATE:(.*?)-->", re.DOTALL)

# Match fenced code blocks (```...```) and inline code (`...`) so we leave
# their contents alone when transforming markdown for Telegram.
_CODE_REGION_PATTERN = re.compile(r"(```.*?```|`[^`\n]*`)", re.DOTALL)


def to_telegram_markdown(text: str) -> str:
    """Convert standard markdown to Telegram's legacy `Markdown` parse mode.

    Telegram's legacy mode uses single asterisks for bold: `*bold*` (not the
    `**bold**` of CommonMark / Claude's default output). It also doesn't
    support headers. This converter rewrites the most common Claude→Telegram
    incompatibilities while leaving code regions untouched.

    Conversions outside code regions:
      - `**bold**` → `*bold*`
      - leading `# `, `## `, `### ` headers → `*…*` (bold the line)

    Code blocks (```...```) and inline `code` are passed through verbatim.
    Telegram preserves the language tag on fenced blocks.
    """
    parts = _CODE_REGION_PATTERN.split(text)
    out: list[str] = []
    for part in parts:
        if part.startswith("`"):
            out.append(part)
            continue
        # **bold** → *bold*
        part = re.sub(r"\*\*([^*\n]+)\*\*", r"*\1*", part)
        # Headers (#, ##, ###...) → bold line
        part = re.sub(r"(?m)^#{1,6}\s+(.*)$", r"*\1*", part)
        out.append(part)
    return "".join(out)


@dataclass
class ScheduleCommand:
    name: str
    prompt: str
    cron: str
    working_dir: str | None = None


@dataclass
class RemindCommand:
    prompt: str
    delay: str


@dataclass
class DelegateCommand:
    task: str
    timeout: int = 600
    session: str = ""  # empty = use default tmux session
    project: str = ""  # optional project name to inject summary context


def split_message(text: str, max_len: int = TELEGRAM_MAX_LEN) -> list[str]:
    if len(text) <= max_len:
        return [text]
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n\n", 0, max_len)
        if split_at > max_len // 2:
            chunks.append(remaining[:split_at])
            remaining = remaining[split_at + 2:]
            continue
        split_at = remaining.rfind("\n", 0, max_len)
        if split_at > max_len // 2:
            chunks.append(remaining[:split_at])
            remaining = remaining[split_at + 1:]
            continue
        chunks.append(remaining[:max_len])
        remaining = remaining[max_len:]
    return chunks


def extract_schedule_commands(text: str) -> list[ScheduleCommand]:
    commands: list[ScheduleCommand] = []
    for match in SCHEDULE_PATTERN.finditer(text):
        try:
            data = json.loads(match.group(1))
            commands.append(ScheduleCommand(
                name=data["name"], prompt=data["prompt"], cron=data["cron"],
                working_dir=data.get("working_dir"),
            ))
        except (json.JSONDecodeError, KeyError):
            pass
    return commands


def extract_remind_commands(text: str) -> list[RemindCommand]:
    commands: list[RemindCommand] = []
    for match in REMIND_PATTERN.finditer(text):
        try:
            data = json.loads(match.group(1))
            commands.append(RemindCommand(prompt=data["prompt"], delay=data["delay"]))
        except (json.JSONDecodeError, KeyError):
            pass
    return commands


def extract_delegate_commands(text: str) -> list[DelegateCommand]:
    commands: list[DelegateCommand] = []
    for match in DELEGATE_PATTERN.finditer(text):
        try:
            data = json.loads(match.group(1))
            commands.append(DelegateCommand(
                task=data["task"], timeout=data.get("timeout", 600),
                session=data.get("session", ""),
                project=data.get("project", ""),
            ))
        except (json.JSONDecodeError, KeyError):
            pass
    return commands


def strip_commands(text: str) -> str:
    text = SCHEDULE_PATTERN.sub("", text)
    text = REMIND_PATTERN.sub("", text)
    text = DELEGATE_PATTERN.sub("", text)
    # Collapse runs of 3+ newlines (left behind when commands are stripped)
    # down to a single blank line.
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
