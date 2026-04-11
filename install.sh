#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Parse arguments
AGENT_ROOT="$HOME/.assistant"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --agent-dir) AGENT_ROOT="$2"; shift 2 ;;
        -h|--help)
            echo "Usage: install.sh [--agent-dir <path>]"
            echo ""
            echo "Install a personal AI assistant powered by Claude Code."
            echo ""
            echo "Options:"
            echo "  --agent-dir <path>  Agent root directory (default: ~/.assistant)"
            echo ""
            echo "Examples:"
            echo "  ./install.sh                        # Default agent at ~/.assistant"
            echo "  ./install.sh --agent-dir ~/.luci     # Custom agent named 'luci'"
            exit 0 ;;
        *) echo "Unknown argument: $1. Use --help for usage."; exit 1 ;;
    esac
done

# Derive names
AGENT_NAME="$(basename "$AGENT_ROOT" | sed 's/^\.//')"
SERVICE_NAME="$AGENT_NAME"

echo "=== ${AGENT_NAME} Install ==="
echo "Agent root: $AGENT_ROOT"
echo "Service: $SERVICE_NAME"
echo ""

# 1. Create directory structure
echo "[1/6] Creating directory structure..."
mkdir -p "$AGENT_ROOT/workspace"
mkdir -p "$AGENT_ROOT/workspace/projects"
mkdir -p "$AGENT_ROOT/workspace/areas"
mkdir -p "$AGENT_ROOT/signals"
mkdir -p "$AGENT_ROOT/coding"
mkdir -p "$AGENT_ROOT/modules"
mkdir -p "$AGENT_ROOT/hooks"
mkdir -p "$AGENT_ROOT/pending-approvals"

# 2. Config file
echo "[2/6] Setting up config..."
if [ ! -f "$AGENT_ROOT/config.yaml" ]; then
    cp "$SCRIPT_DIR/config.example.yaml" "$AGENT_ROOT/config.yaml"
    echo "  Created $AGENT_ROOT/config.yaml — edit with your settings"
else
    echo "  Config already exists, skipping"
fi

# 3. Seed workspace templates (don't overwrite existing)
echo "[3/6] Seeding workspace templates..."
for f in CLAUDE.md USER.md HEARTBEAT.md TODO.md; do
    target="$AGENT_ROOT/workspace/$f"
    if [ ! -f "$target" ]; then
        case "$f" in
            CLAUDE.md)
                cat > "$target" <<'TMPL'
# Agent Identity

Define your agent's identity, operating principles, and capabilities here.
This file is loaded automatically for every claude -p session from this workspace.

## Workspace Structure

- `CLAUDE.md` — This file. Your identity and operating instructions.
- `USER.md` — Profile of the user you support.
- `HEARTBEAT.md` — Periodic check-in checklist.
- `TODO.md` — Living scratchpad for pending items.
- `projects/` — Active work with end states. Each project has `summary.md` + `items.md`.
- `areas/` — Ongoing life domains. Each area has `summary.md` + `items.md`.

## Projects & Areas

You use a simplified PARA system. Before acting on a task, check if it relates
to an active project or area. If it does, read the `summary.md` first —
the summaries are inputs to your work, not passive logs. After acting, update
the summary if state changed, and add a timestamped line to items.md.
TMPL
                ;;
            USER.md)
                cat > "$target" <<'TMPL'
# About the User

Define who you are, your preferences, and what you want from this agent.
TMPL
                ;;
            HEARTBEAT.md)
                cat > "$target" <<'TMPL'
# Heartbeat

Define what to check on each periodic heartbeat. Work through sections in order. If something needs action, handle it or delegate it. Keep responses brief.

## 1. Projects & Areas (strategic check — first, not last)

- `ls workspace/projects/` — list active projects
- For each project, glance at `summary.md` — is the status still accurate?
- If a project shows a blocker you can act on, do it or delegate it
- Areas are more passive — check when something in recent activity relates to one
- Before finishing: if you took action on a project, update its `summary.md` and add a line to `items.md`

## 2. Pending Tasks
- Check for any pending signals or incomplete work in signals/
- Check tmux sessions are alive

## 3. System Health
- Verify services are running

## 4. TODO List
- Check TODO.md for pending items
- If a TODO has grown into something bigger, promote it to a project folder
TMPL
                ;;
            TODO.md)
                cat > "$target" <<'TMPL'
# TODO

Items that need attention. Check during heartbeat.

## Pending

## Done
TMPL
                ;;
        esac
        echo "  Created $target"
    fi
done

# 4. Copy hook templates
echo "[4/6] Setting up hooks..."
for template in "$SCRIPT_DIR"/hooks/*.template; do
    [ -f "$template" ] || continue
    name="$(basename "$template" .template)"
    target="$AGENT_ROOT/hooks/$name"
    if [ ! -f "$target" ]; then
        sed \
            -e "s|{{AGENT_ROOT}}|${AGENT_ROOT}|g" \
            -e "s|{{AGENT_NAME}}|${AGENT_NAME}|g" \
            "$template" > "$target"
        chmod +x "$target"
        echo "  Created $target"
    fi
done

# 5. Install Python dependencies
echo "[5/6] Installing Python dependencies..."
cd "$SCRIPT_DIR"
uv sync 2>&1 | tail -1

# 6. Install systemd user service
echo "[6/6] Installing systemd service..."
mkdir -p "$HOME/.config/systemd/user"
sed \
    -e "s|{{AGENT_ROOT}}|${AGENT_ROOT}|g" \
    -e "s|{{PROJECT_DIR}}|${SCRIPT_DIR}|g" \
    -e "s|{{AGENT_NAME}}|${AGENT_NAME}|g" \
    "$SCRIPT_DIR/systemd/assistant.service.template" \
    > "$HOME/.config/systemd/user/${SERVICE_NAME}.service"
systemctl --user daemon-reload
systemctl --user enable "$SERVICE_NAME"

echo ""
echo "=== Install complete ==="
echo ""
echo "Directory structure:"
echo "  $AGENT_ROOT/"
echo "  ├── config.yaml            # Edit with your Telegram bot token + owner ID"
echo "  ├── workspace/             # Agent workspace"
echo "  │   ├── CLAUDE.md          # Agent identity"
echo "  │   ├── USER.md            # User profile"
echo "  │   ├── HEARTBEAT.md       # Periodic check-in checklist"
echo "  │   ├── TODO.md            # Living scratchpad"
echo "  │   ├── projects/          # Active work with end states"
echo "  │   └── areas/             # Ongoing life domains"
echo "  ├── modules/               # Custom modules (telegram.py, cron.py per module)"
echo "  ├── signals/               # Task completion signals"
echo "  ├── coding/                # Tmux Claude working directory"
echo "  ├── hooks/                 # Notification and lifecycle hooks"
echo "  └── pending-approvals/     # Tmux permission approval queue"
echo ""
echo "Commands:"
echo "  systemctl --user start $SERVICE_NAME"
echo "  systemctl --user stop $SERVICE_NAME"
echo "  systemctl --user restart $SERVICE_NAME"
echo "  systemctl --user status $SERVICE_NAME"
echo "  journalctl --user -u $SERVICE_NAME -f"
echo ""

if grep -q "YOUR_BOT_TOKEN" "$AGENT_ROOT/config.yaml" 2>/dev/null; then
    echo "!! Before starting, edit $AGENT_ROOT/config.yaml with your:"
    echo "   - Telegram bot token (from @BotFather)"
    echo "   - Telegram owner ID (from @userinfobot)"
fi
