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
class JobDelivery:
    """Optional delivery routing for a scheduled job.

    Default behavior (when delivery is None) sends to the Telegram owner.
    When transport is "discord", the result is sent to the named Discord
    channel via the Discord bot.
    """
    transport: str = "telegram"  # "telegram" | "discord"
    channel_id: str | int | None = None  # Discord channel ID (required if transport=="discord")


@dataclass
class ScheduledJob:
    name: str
    prompt: str
    cron: str | None = None        # 5-field cron expression (e.g. "0 9 * * *")
    interval: str | None = None    # interval expression (e.g. "55m", "2h", "30s") — mutually exclusive with cron
    working_dir: str | None = None
    session: str = "chat"  # session key in session.json; jobs with the same key share context
    delivery: JobDelivery | None = None  # default: Telegram owner
    model: str = ""        # passed to claude -p as --model; empty = inherit claude.model

    def __post_init__(self):
        if not self.cron and not self.interval:
            raise ValueError(f"job '{self.name}' must set either `cron` or `interval`")
        if self.cron and self.interval:
            raise ValueError(f"job '{self.name}' sets both `cron` and `interval`; pick one")
        if isinstance(self.delivery, dict):
            self.delivery = JobDelivery(**self.delivery)
        if self.delivery and self.delivery.transport == "discord" and not self.delivery.channel_id:
            raise ValueError(f"job '{self.name}' has discord delivery but no channel_id")
        # Auto-isolate session per model so --resume doesn't try to rehydrate
        # a conversation built under a different model. Only kicks in when the
        # job leaves session at the default "chat" — explicit session= still wins.
        if self.model and self.session == "chat":
            self.session = f"chat-{self.model}"


@dataclass
class SlackConfig:
    bot_token: str = ""
    app_token: str = ""
    channels: dict[str, dict] = field(default_factory=dict)
    history_limit: int = 50
    triage_interval: int = 900
    enabled: bool = False


@dataclass
class DiscordChannelConfig:
    """Per-channel routing rules. requireMention=True means the bot only
    responds when @-mentioned in that channel; False means it responds to
    every message (use for private/dedicated channels)."""
    requireMention: bool = True


@dataclass
class DiscordGuildConfig:
    channels: dict[str, DiscordChannelConfig] = field(default_factory=dict)

    def __post_init__(self):
        # Allow YAML to pass plain dicts; coerce them
        coerced = {}
        for cid, cfg in self.channels.items():
            if isinstance(cfg, dict):
                coerced[str(cid)] = DiscordChannelConfig(**cfg)
            else:
                coerced[str(cid)] = cfg
        self.channels = coerced


@dataclass
class DiscordConfig:
    bot_token: str = ""
    enabled: bool = False
    guilds: dict[str, DiscordGuildConfig] = field(default_factory=dict)
    # Default mention requirement for any channel not explicitly listed under a guild.
    # Most safe: require mention when channel is unknown.
    default_require_mention: bool = True

    def __post_init__(self):
        coerced = {}
        for gid, cfg in self.guilds.items():
            if isinstance(cfg, dict):
                coerced[str(gid)] = DiscordGuildConfig(**cfg)
            else:
                coerced[str(gid)] = cfg
        self.guilds = coerced

    def channel_requires_mention(self, guild_id: int | str | None, channel_id: int | str) -> bool | None:
        """Return True/False if the channel is allowlisted, None if it isn't.
        None signals 'do not respond' — channel is outside the allowlist entirely."""
        if guild_id is None:
            return None
        guild = self.guilds.get(str(guild_id))
        if guild is None:
            return None
        ch = guild.channels.get(str(channel_id))
        if ch is None:
            return None
        return ch.requireMention


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
    discord: DiscordConfig = field(default_factory=DiscordConfig)

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

    discord = DiscordConfig(**raw.get("discord", {}))

    return Config(telegram=telegram, claude=claude, scheduler=scheduler,
                  slack=slack, voice=voice, cc_agents=cc_agents, discord=discord)
