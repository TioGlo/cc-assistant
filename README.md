# cc-assistant

Personal AI assistant powered by Claude Code. Message your agent from Telegram, and it can do real work — files, shell, web, browser automation, scheduled tasks, and delegation to full interactive Claude Code instances with teams and subagents.

Built as an open-source alternative to OpenClaw for Claude Code Max plan users.

## Prerequisites

- **Linux** with systemd (Ubuntu 22.04+, Fedora 39+, Arch, etc.) **or macOS** (12+, with launchd)
- **Python 3.13+** with [uv](https://docs.astral.sh/uv/) for dependency management
- **Node.js 18+** with npm (for browser-mcp)
- **[Claude Code CLI](https://claude.ai/code)** installed and authenticated (`claude auth login`)
- **tmux** for interactive Claude Code sessions
- **Chrome or Chromium** with remote debugging (for browser automation)
- **A Telegram bot** — create one via [@BotFather](https://t.me/BotFather)
- **Your Telegram user ID** — get it from [@userinfobot](https://t.me/userinfobot)

On **macOS** the easiest way to get the toolchain is Homebrew:

```bash
brew install uv tmux jq node git
# Claude Code CLI: see https://docs.claude.com/en/docs/claude-code/setup
```

## Quick Start

```bash
git clone --recursive https://github.com/TioGlo/cc-assistant.git
cd cc-assistant
./install.sh
# Edit ~/.assistant/config.yaml with your Telegram bot token and owner ID
```

Then start the service:

- **Linux:** `systemctl --user start assistant`
- **macOS:** the agent is loaded automatically by `launchctl` during install. To start manually: `launchctl kickstart -k gui/$UID/com.assistant`

Your bot should respond to `/start` on Telegram.

### Service management

| Action | Linux (systemd) | macOS (launchd) |
|--------|-----------------|-----------------|
| Status | `systemctl --user status assistant` | `launchctl print gui/$UID/com.assistant` |
| Restart | `systemctl --user restart assistant` | `launchctl kickstart -k gui/$UID/com.assistant` |
| Stop | `systemctl --user stop assistant` | `launchctl bootout gui/$UID/com.assistant` |
| Logs | `journalctl --user -u assistant -f` | `tail -f ~/Library/Logs/assistant.log` |

## Templates

Install ships with two starting templates that shape how the agent behaves from its first interaction:

```bash
./install.sh                          # Default: assistant template
./install.sh --template curious       # Discovery-driven agent
```

| Template | First interaction | Identity | Best for |
|----------|------------------|----------|----------|
| `assistant` | "How can I help you?" | Pre-defined: practical, structured, ready to serve | Users who want a capable agent immediately |
| `curious` | Self-directed discovery | Blank — agent builds it through interaction | Autonomous agents, deeper working relationships |

Both templates share `WORKSPACE_REFERENCE.md` (PARA docs, directory layout, delegation syntax). The `assistant` template pre-explains the workspace in its `CLAUDE.md`; the `curious` template points to the reference file and focuses on identity discovery.

The `curious` template seeds a "Becoming" project instead of "Onboarding" — the agent's first project is figuring out who it is and who its user is, not running through a setup checklist.

## Custom Agent Directory

Deploy a named agent with its own identity and workspace:

```bash
./install.sh --agent-dir ~/.luci --template curious
# Edit ~/.luci/config.yaml
systemctl --user start luci
```

Each agent gets its own config, workspace, sessions, and systemd service.

## Multiple Agents on One Machine

```bash
./install.sh                                          # ~/.assistant → service: assistant
./install.sh --agent-dir ~/.luci --template curious   # ~/.luci     → service: luci
./install.sh --agent-dir ~/.cybin                     # ~/.cybin    → service: cybin
```

Each agent needs its own Telegram bot (create via @BotFather).

## What's Included

**Ships automatically:**
- **browser-mcp** — Browser automation via persistent Chrome (CDP). Configured in the coding agent's `.mcp.json` at install time. Requires Chrome running with `--remote-debugging-port=9222`.

**Optional add-ons (interactive prompt during install):**
- **Google Workspace** — Gmail, Calendar, Drive access via MCP. Requires a Google Cloud project with OAuth credentials. The installer adds it to the coding agent's `.mcp.json` and prints post-install steps.
- **Voice input** — Speak to the bot via Telegram voice messages. Local transcription via Whisper-class models. Pluggable engine architecture (faster-whisper / whisper.cpp / OpenAI API). See [Voice Input](#voice-input) below.

### Voice Input

Send a Telegram voice note → the bot transcribes it locally → routes the text through the normal message flow. Audio never leaves the box (with local engines).

**Enable:** set `voice.enabled: true` in `config.yaml`, then `uv sync --extra voice` (the installer does this automatically if voice is enabled when you run it).

**Engines.** All three ship as working examples; pick one in `config.yaml` and the others stay dormant.

| Engine | Type | Best for |
|--------|------|----------|
| `faster_whisper` | Python lib (default) | First-time install, CPU, no extra binary needed |
| `whisper_cpp` | Subprocess to `whisper-cli` | Vulkan/Metal/CUDA acceleration, no Python ML stack |
| `openai_api` | HTTP to OpenAI | Trivial setup; audio leaves the box; pay-per-use |

**Adding a new engine** is one file at `assistant/voice/engines/<name>.py` with a `@register_engine("<name>")` class implementing the `TranscriptionEngine` protocol. No central edit, no config schema change — engine-specific options live in `voice.engine_options`.

**Latency.** ~5–8s end-to-end for a 30s clip with `faster_whisper` `base.en` int8 on CPU. The bot shows "typing…" immediately so the wait is visible. The model eager-loads at startup; first message isn't slower than subsequent ones.

**Privacy.** With `faster_whisper` or `whisper_cpp`, audio is processed locally and never sent to a third party. With `openai_api`, audio goes to OpenAI per their terms.

### Chrome Setup

browser-mcp needs a Chrome instance with remote debugging. Run manually or as a systemd service:

```bash
google-chrome --remote-debugging-port=9222 \
  --user-data-dir="$HOME/.myagent/chrome-profile" \
  --no-first-run --no-default-browser-check
```

For headless servers, add `--headless=new`. Sessions persist in the profile — log into sites once manually, and the agent automates from there.

## Architecture

```
Telegram Bot
      │
      ▼
Bot Gateway (Python) ──── Claude Code CLI (claude -p)
      │                         │
      ├── Scheduler        Workspace CLAUDE.md
      │   (cron jobs)      (agent identity)
      │
      ├── Tmux Dispatch ──── Claude Code (interactive)
      │   (fire & forget)     ├── Teams
      │                       ├── Subagents
      │                       └── Browser MCP
      │
      └── Slack Monitor
          (optional)
```

The bot is a thin gateway. Each Telegram message goes to `claude -p` which runs in the agent's workspace where `CLAUDE.md` defines its identity. Heavy tasks get delegated to tmux Claude Code instances with full interactive capabilities.

## Configuration

### config.yaml

```yaml
telegram:
  bot_token: "YOUR_BOT_TOKEN"
  owner_id: 123456789

claude:
  model: "opus"                     # or sonnet, haiku
  permission_mode: "bypassPermissions"
  system_prompt: |
    You are a personal AI assistant accessible via Telegram.
    # ... see config.example.yaml for full template
  system_prompt_files:              # files appended to system prompt per invocation
    - "~/.myagent/workspace/self-improving/memory.md"
  max_turns: 50
  timeout: 300
  mcp_config: "/path/to/mcp.json"  # optional MCP server config

# Claude Code tmux agents for task delegation
cc_agents:
  - name: "my-code"
    tmux_session: "my-code"
    # working_dir: ""              # defaults to {AGENT_ROOT}/coding
    # permission_mode: "dangerously-skip-permissions"

# Scheduled jobs
scheduler:
  jobs:
    - name: "heartbeat"
      prompt: "Read HEARTBEAT.md and follow its instructions."
      cron: "25 */2 * * *"

# Optional Slack monitoring
# slack:
#   enabled: true
#   bot_token: "xoxb-..."
#   app_token: "xapp-..."
#   channels:
#     "#general": { enabled: true, requireMention: false }
```

### system_prompt_files

Append file contents to the system prompt on every `claude -p` invocation. Files are read fresh each call — updates take effect immediately without restarting the service.

```yaml
claude:
  system_prompt_files:
    - "~/.myagent/workspace/self-improving/memory.md"
    - "~/.myagent/workspace/some-other-context.md"
```

Use this to inject dynamic context like self-improving rules, learned preferences, or any file that changes over time and should be part of every conversation. If a file doesn't exist or can't be read, it's silently skipped.

### cc_agents

Define one or more tmux Claude Code sessions. The first is the default delegation target.

The default agent runs from the workspace — it shares the same CLAUDE.md, projects, areas, and self-improving system as the bot. Use it for deep work, planning, browser automation, anything that needs more than the bot's timeout.

```yaml
cc_agents:
  - name: "my-workspace"              # default: shares workspace context with bot
    tmux_session: "my-workspace"
    working_dir: "~/.myagent/workspace"
  - name: "my-code"                    # optional: dedicated coding agent
    tmux_session: "my-code"
    working_dir: "~/.myagent/coding"
    resume: false                      # start fresh every time
```

By default, agents resume their prior Claude Code session when their tmux session is recreated (e.g. after a crash or service restart). Set `resume: false` for agents that should start with a clean slate on every dispatch.

Agents are auto-created on first `/code` dispatch. Or start manually:

```bash
tmux new-session -s my-workspace -c ~/.myagent/workspace
claude --dangerously-skip-permissions
```

## Core Commands

| Command | Description |
|---------|-------------|
| `/start` | Show help |
| `/reset` | Clear conversation session |
| `/status` | Agent info, uptime, tmux session |
| `/jobs` | List scheduled jobs |
| `/schedule <cron> <prompt>` | Add a recurring job |
| `/remind <delay> <prompt>` | One-shot reminder (e.g. `/remind 2h check deploy`) |
| `/cancel <name>` | Remove a scheduled job |
| `/code <task>` | Dispatch to full Claude Code |
| `/codecheck` | Check coding session output |
| `/approve <id>` | Approve a tmux permission request |
| `/approve_always <id>` | Approve with "don't ask again" |
| `/deny <id>` | Deny a permission request |

## Self-Delegation

The agent can self-delegate heavy tasks by emitting structured blocks in its responses:

```
<!--DELEGATE:{"task":"detailed description","timeout":600}-->
<!--DELEGATE:{"task":"research AI trends","timeout":900,"session":"research"}-->
<!--SCHEDULE:{"name":"daily-check","prompt":"check the deploy","cron":"0 9 * * *"}-->
<!--REMIND:{"prompt":"follow up on email","delay":"2h"}-->
```

The bot parses these and acts on them — dispatching to tmux, registering cron jobs, or setting reminders.

## Modules

Add custom Telegram commands and cron jobs by creating modules in `{AGENT_ROOT}/modules/`:

```
modules/
  my-feature/
    telegram.py     # present? → registers Telegram commands
    cron.py         # present? → registers scheduled jobs
    prompts/        # convention (not auto-loaded)
    data/           # convention (not auto-loaded)
```

No manifest needed. File presence is the declaration.

### telegram.py

```python
from telegram.ext import CommandHandler

def register(bot):
    async def cmd_hello(update, context):
        await update.message.reply_text("Hello from my module!")
    bot.app.add_handler(CommandHandler("hello", cmd_hello))
```

### cron.py

```python
def register(scheduler):
    scheduler.add_cron_job("my-task", "Do something useful", "0 9 * * *")
```

## Recommended Setup

### Workspace Files

After installation, customize these files in `{AGENT_ROOT}/workspace/`:

| File | Purpose |
|------|---------|
| `CLAUDE.md` | Agent identity, operating principles, capabilities |
| `USER.md` | Who you are, your preferences, what you want from the agent |
| `HEARTBEAT.md` | Periodic check-in checklist (system health, TODO items) |
| `TODO.md` | Living scratchpad for pending items |

### MCP Servers

We recommend these MCP servers. Configure globally in `~/.claude.json` or per-project in `.mcp.json`:

**Global (all sessions):**

| Server | Purpose | Install |
|--------|---------|---------|
| [qmd](https://github.com/tobi/qmd) | Session history search, workspace search | `bun install -g @tobilu/qmd` |
| [context7](https://github.com/upstash/context7) | Library/framework documentation lookup | Auto via npx |

**Agent-specific (in `{AGENT_ROOT}/coding/.mcp.json`):**

| Server | Purpose | When needed |
|--------|---------|-------------|
| [browser-mcp](https://github.com/anthropics/browser-mcp) | Browser automation via persistent Chrome | Social media, web scraping, form filling |
| [google-workspace](https://github.com/taylorwilsdon/google_workspace_mcp) | Gmail, Calendar, Drive | Email triage, calendar management |
| comfyui-mcp | AI image generation | Content creation pipelines |

**Browser automation** requires Chrome running with remote debugging:

```bash
google-chrome --remote-debugging-port=9222 \
  --user-data-dir="$HOME/.assistant/chrome-profile" \
  --no-first-run --no-default-browser-check
```

Sessions persist in the Chrome profile. Log into sites once manually, and the agent can automate from there.

### Claude Code Subagents

Define specialized subagents in `~/.claude/agents/` for cheaper, focused task execution:

```markdown
---
name: email-triager
description: Triage Gmail inbox
model: sonnet
tools: [Read, Write, Bash, Grep, Glob]
mcpServers: [google-workspace]
maxTurns: 30
---
Read the email triage prompt and follow its instructions.
```

Cron jobs can invoke subagents instead of using full Opus sessions:

```yaml
- name: "email-triage"
  prompt: "Use the @email-triager subagent to triage the inbox."
  cron: "0 9,14,19 * * *"
```

### Hooks

Template hooks are installed to `{AGENT_ROOT}/hooks/`:

| Hook | Event | Purpose |
|------|-------|---------|
| `precompact-context.sh` | PreCompact | Re-injects TODO items and critical context when session is compressed |
| `notify-telegram.sh` | Notification | Sends permission prompts and idle notifications to Telegram |

Register in `~/.claude/settings.json`:

```json
{
  "hooks": {
    "PreCompact": [{"hooks": [{"type": "command", "command": "/path/to/precompact-context.sh"}]}],
    "Notification": [{"hooks": [{"type": "command", "command": "/path/to/notify-telegram.sh"}]}]
  }
}
```

The notification hook enables **remote approval** — when a tmux agent hits a permission wall, you get a Telegram message with `/approve` and `/deny` commands.

### qmd Setup

[qmd](https://github.com/tobi/qmd) indexes your workspace and session transcripts for fast search:

```bash
# Install
bun install -g @tobilu/qmd

# Index your workspace
qmd collection add ~/.assistant/workspace --name assistant-workspace --mask "**/*.md"
qmd embed

# Add to Claude Code as MCP server
claude mcp add -s user qmd -- /path/to/qmd-mcp
```

Add a qmd-sessions hook to auto-index session transcripts on PreCompact and SessionEnd. See [qmd-sessions skill](https://github.com/tobi/qmd) for details.

## File Structure

```
{AGENT_ROOT}/
├── config.yaml            # Bot token, model, agents, scheduler jobs
├── workspace/
│   ├── CLAUDE.md          # Agent identity and instructions
│   ├── USER.md            # User profile
│   ├── HEARTBEAT.md       # Periodic check-in checklist
│   ├── TODO.md            # Living scratchpad
│   ├── skills/            # Claude Code skills (SKILL.md convention)
│   ├── prompts/           # Task-specific prompt files
│   └── tasks/             # Dispatched task specs (auto-generated)
├── modules/               # Custom Telegram commands and cron jobs
├── coding/                # Tmux Claude working directory
├── signals/               # Task completion signals
├── hooks/                 # Notification and lifecycle hooks
├── pending-approvals/     # Permission approval queue
├── chrome-profile/        # Persistent Chrome browser data (optional)
├── session.json           # Persistent conversation session ID
└── scheduler-jobs.json    # Dynamically-added cron jobs
```

## How It Works

1. **Telegram message** → Bot receives it, sends to `claude -p` with session resumption
2. **Claude responds** → Bot parses SCHEDULE/REMIND/DELEGATE blocks, strips them, sends clean text to Telegram
3. **DELEGATE block** → Bot dispatches task to a tmux Claude Code session, notifies on completion
4. **Cron job fires** → Bot runs `claude -p` with the prompt, processes response same as above
5. **Permission needed** → Notification hook sends Telegram message, user approves/denies remotely
6. **Session compressed** → PreCompact hook re-injects critical context (TODO, pending tasks)
7. **Heartbeat** → Periodic check-in runs subagent, reports status, handles pending items

## License

MIT
