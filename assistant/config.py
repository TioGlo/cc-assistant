from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .voice import VoiceConfig


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
    system_prompt_files: list[str] = field(default_factory=list)  # paths read and appended to system_prompt per invocation
    max_turns: int = 50
    timeout: int = 300
    mcp_config: str | None = None


@dataclass
class CCAgent:
    name: str
    tmux_session: str = ""  # defaults to name if empty
    working_dir: str = ""   # defaults to {AGENT_ROOT}/coding if empty
    permission_mode: str = "dangerously-skip-permissions"
    resume: bool = True     # resume prior Claude session on tmux recreation
    model: str = ""         # passed as `claude --model <model>`; empty = inherit Claude Code's default

    def __post_init__(self):
        if not self.tmux_session:
            self.tmux_session = self.name


@dataclass
class ScheduledJob:
    name: str
    prompt: str
    cron: str
    working_dir: str | None = None
    session: str = "chat"  # session key in session.json; jobs with the same key share context


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
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    cc_agents: list[CCAgent] = field(default_factory=list)

    @property
    def default_agent(self) -> CCAgent | None:
        """First agent in the list is the default dispatch target."""
        return self.cc_agents[0] if self.cc_agents else None


def load_config(path: str | Path) -> Config:
    path = Path(path)
    with path.open() as f:
        raw = yaml.safe_load(f)

    telegram = TelegramConfig(**raw["telegram"])
    claude = ClaudeConfig(**raw.get("claude", {}))

    sched_raw = raw.get("scheduler", {})
    jobs_raw = sched_raw.pop("jobs", [])
    sched_raw.pop("db_path", None)  # legacy field, ignored
    jobs = [ScheduledJob(**j) for j in jobs_raw]
    scheduler = SchedulerConfig(jobs=jobs)

    slack = SlackConfig(**raw.get("slack", {}))

    voice = VoiceConfig(**raw.get("voice", {}))

    agents_raw = raw.get("cc_agents", [])
    cc_agents = [CCAgent(**a) for a in agents_raw]

    return Config(telegram=telegram, claude=claude, scheduler=scheduler,
                  slack=slack, voice=voice, cc_agents=cc_agents)
