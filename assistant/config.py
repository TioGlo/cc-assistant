from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class TelegramConfig:
    bot_token: str
    owner_id: int


@dataclass
class ClaudeConfig:
    model: str = "opus"
    permission_mode: str = "bypassPermissions"
    allowed_tools: list[str] = field(default_factory=list)
    system_prompt: str = ""
    max_turns: int = 50
    timeout: int = 300
    mcp_config: str | None = None


@dataclass
class ScheduledJob:
    name: str
    prompt: str
    cron: str
    working_dir: str | None = None


@dataclass
class SlackConfig:
    bot_token: str = ""
    app_token: str = ""
    channels: dict[str, dict] = field(default_factory=dict)
    history_limit: int = 50
    triage_interval: int = 900
    enabled: bool = False


@dataclass
class SchedulerConfig:
    jobs: list[ScheduledJob] = field(default_factory=list)


@dataclass
class Config:
    telegram: TelegramConfig
    claude: ClaudeConfig
    scheduler: SchedulerConfig
    slack: SlackConfig


def load_config(path: str | Path) -> Config:
    path = Path(path)
    with path.open() as f:
        raw = yaml.safe_load(f)

    telegram = TelegramConfig(**raw["telegram"])
    claude = ClaudeConfig(**raw.get("claude", {}))

    sched_raw = raw.get("scheduler", {})
    jobs_raw = sched_raw.pop("jobs", [])
    jobs = [ScheduledJob(**j) for j in jobs_raw]
    scheduler = SchedulerConfig(jobs=jobs)

    slack = SlackConfig(**raw.get("slack", {}))

    return Config(telegram=telegram, claude=claude, scheduler=scheduler, slack=slack)
