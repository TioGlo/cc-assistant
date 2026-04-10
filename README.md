# cc-assistant

Personal AI assistant powered by Claude Code. Message your agent from Telegram, and it can do real work — files, shell, web, browser automation, scheduled tasks, and delegation to full interactive Claude Code instances.

Built as an open-source alternative to OpenClaw for Claude Code Max plan users.

## Quick Start

```bash
git clone https://github.com/youruser/cc-assistant.git
cd cc-assistant
./install.sh
# Edit ~/.assistant/config.yaml with your Telegram bot token and owner ID
systemctl --user start assistant
```

## Custom Agent

Deploy a named agent with its own identity and workspace:

```bash
./install.sh --agent-dir ~/.luci
# Edit ~/.luci/config.yaml
systemctl --user start luci
```

## Multiple Agents

Run as many agents as you want on one machine. Each gets its own:
- Config, workspace, and session
- Telegram bot (create one per agent via @BotFather)
- Systemd service (named after the agent directory)
- Tmux coding session (auto-created on first `/code` dispatch)

```bash
./install.sh --agent-dir ~/.qu       # systemctl --user start qu
./install.sh --agent-dir ~/.luci     # systemctl --user start luci
./install.sh --agent-dir ~/.cybin    # systemctl --user start cybin
```

## Architecture

```
Telegram → Bot Gateway → Claude Code CLI (claude -p)
                ↕                    ↕
           Scheduler            Tmux Dispatch
          (cron jobs)        (full Claude Code)
                ↕
         Slack Monitor
        (optional)
```

The bot is a thin gateway. Each message goes to `claude -p` which runs in the agent's workspace where `CLAUDE.md` defines the agent's identity. Heavy tasks get delegated to a tmux Claude Code instance with full interactive capabilities (teams, subagents, worktrees).

## Core Commands

| Command | Description |
|---------|-------------|
| `/start` | Show help |
| `/reset` | Clear conversation session |
| `/status` | Agent info, uptime, session |
| `/jobs` | List scheduled jobs |
| `/schedule <cron> <prompt>` | Add a recurring job |
| `/remind <delay> <prompt>` | One-shot reminder |
| `/cancel <name>` | Remove a scheduled job |
| `/code <task>` | Dispatch to full Claude Code |
| `/codecheck` | Check coding session status |
| `/approve <id>` | Approve a permission request |
| `/deny <id>` | Deny a permission request |

## Modules

Add custom Telegram commands and cron jobs by creating modules in `{AGENT_ROOT}/modules/`:

```
modules/
  my-feature/
    telegram.py     # Register Telegram commands
    cron.py         # Register scheduled jobs
    prompts/        # Prompt files (convention)
    data/           # Module data (convention)
```

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
    scheduler.add_cron_job(
        "my-job", "Do something useful", "0 9 * * *"
    )
```

## Delegation

The agent can self-delegate heavy tasks by emitting structured blocks:

```
<!--DELEGATE:{"task":"build a REST API","timeout":600}-->
```

This dispatches to a full interactive Claude Code in tmux, which can use teams, subagents, and worktrees. Results are delivered back to Telegram.

## Hooks

Template hooks are installed to `{AGENT_ROOT}/hooks/`:

- **precompact-context.sh** — Re-injects TODO and critical context when Claude's session is compressed
- **notify-telegram.sh** — Sends permission prompts and idle notifications to Telegram

Configure in `~/.claude/settings.json` under the `hooks` section.

## Files

```
{AGENT_ROOT}/
├── config.yaml          # Bot token, model, scheduler jobs
├── workspace/
│   ├── CLAUDE.md        # Agent identity and instructions
│   ├── USER.md          # User profile
│   ├── HEARTBEAT.md     # Periodic check-in checklist
│   └── TODO.md          # Living scratchpad
├── modules/             # Custom modules
├── signals/             # Task completion signals
├── coding/              # Tmux Claude working directory
├── hooks/               # Notification and lifecycle hooks
├── pending-approvals/   # Permission approval queue
├── session.json         # Persistent session ID
└── scheduler-jobs.json  # Dynamically-added cron jobs
```

## Requirements

- Python 3.13+
- [uv](https://docs.astral.sh/uv/) for dependency management
- [Claude Code](https://claude.ai/code) CLI installed and authenticated
- Linux with systemd (for service management)
- tmux (for interactive Claude Code sessions)

## License

MIT
