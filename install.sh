#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Parse arguments
AGENT_ROOT="$HOME/.assistant"
TEMPLATE="assistant"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --agent-dir) AGENT_ROOT="$2"; shift 2 ;;
        --template)  TEMPLATE="$2"; shift 2 ;;
        -h|--help)
            echo "Usage: install.sh [--agent-dir <path>] [--template <name>]"
            echo ""
            echo "Install a personal AI assistant powered by Claude Code."
            echo ""
            echo "Options:"
            echo "  --agent-dir <path>   Agent root directory (default: ~/.assistant)"
            echo "  --template <name>    Starting template (default: assistant)"
            echo ""
            echo "Templates:"
            echo "  assistant   Ready-to-serve personal assistant. Practical, structured,"
            echo "              workspace pre-explained. Good for users who want a capable"
            echo "              agent immediately."
            echo ""
            echo "  curious     Discovery-driven agent. Boots with questions, not answers."
            echo "              Builds its identity through interaction. Good for autonomous"
            echo "              agents or users who want a deeper working relationship."
            echo ""
            echo "Examples:"
            echo "  ./install.sh                                    # Default assistant"
            echo "  ./install.sh --agent-dir ~/.luci --template curious"
            exit 0 ;;
        *) echo "Unknown argument: $1. Use --help for usage."; exit 1 ;;
    esac
done

# Validate template
TEMPLATE_DIR="$SCRIPT_DIR/templates/$TEMPLATE"
if [ ! -d "$TEMPLATE_DIR" ]; then
    echo "Unknown template: $TEMPLATE"
    echo "Available templates: $(ls "$SCRIPT_DIR/templates/" | grep -v shared | tr '\n' ' ')"
    exit 1
fi

# Derive names
AGENT_NAME="$(basename "$AGENT_ROOT" | sed 's/^\.//')"
SERVICE_NAME="$AGENT_NAME"

echo "=== ${AGENT_NAME} Install ==="
echo "Agent root: $AGENT_ROOT"
echo "Template: $TEMPLATE"
echo "Service: $SERVICE_NAME"
echo ""

# 1. Create directory structure
echo "[1/6] Creating directory structure..."
mkdir -p "$AGENT_ROOT/workspace"
mkdir -p "$AGENT_ROOT/workspace/projects"
mkdir -p "$AGENT_ROOT/workspace/areas"
mkdir -p "$AGENT_ROOT/workspace/self-improving/projects"
mkdir -p "$AGENT_ROOT/workspace/self-improving/domains"
mkdir -p "$AGENT_ROOT/workspace/self-improving/archive"
mkdir -p "$AGENT_ROOT/signals"
mkdir -p "$AGENT_ROOT/modules"
mkdir -p "$AGENT_ROOT/hooks"
mkdir -p "$AGENT_ROOT/pending-approvals"

# 2. Config file
echo "[2/6] Setting up config..."
if [ ! -f "$AGENT_ROOT/config.yaml" ]; then
    sed \
        -e "s|{{AGENT_ROOT}}|${AGENT_ROOT}|g" \
        -e "s|{{AGENT_NAME}}|${AGENT_NAME}|g" \
        "$SCRIPT_DIR/config.example.yaml" > "$AGENT_ROOT/config.yaml"
    echo "  Created $AGENT_ROOT/config.yaml — edit with your settings"
else
    echo "  Config already exists, skipping"
fi

# 3. Seed workspace templates (don't overwrite existing)
echo "[3/6] Seeding workspace templates..."

# Copy CLAUDE.md from selected template
if [ ! -f "$AGENT_ROOT/workspace/CLAUDE.md" ]; then
    cp "$TEMPLATE_DIR/CLAUDE.md" "$AGENT_ROOT/workspace/CLAUDE.md"
    echo "  Created CLAUDE.md (from $TEMPLATE template)"
fi

# Copy shared workspace reference
if [ ! -f "$AGENT_ROOT/workspace/WORKSPACE_REFERENCE.md" ]; then
    cp "$SCRIPT_DIR/templates/shared/WORKSPACE_REFERENCE.md" "$AGENT_ROOT/workspace/WORKSPACE_REFERENCE.md"
    echo "  Created WORKSPACE_REFERENCE.md"
fi

# Seed self-improving files
for f in memory.md corrections.md; do
    target="$AGENT_ROOT/workspace/self-improving/$f"
    if [ ! -f "$target" ]; then
        cp "$SCRIPT_DIR/templates/shared/self-improving/$f" "$target"
        echo "  Created self-improving/$f"
    fi
done

# Seed USER.md, HEARTBEAT.md, TODO.md (shared across all templates)
for f in USER.md HEARTBEAT.md TODO.md; do
    target="$AGENT_ROOT/workspace/$f"
    if [ ! -f "$target" ]; then
        case "$f" in
            USER.md)
                cat > "$target" <<'TMPL'
# About the User

*(Your agent doesn't know you yet. Tell them about yourself here, or let them learn through conversation.)*
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

## 4. Self-Improving
- Read `self-improving/corrections.md` — any pending entries that should be promoted?
- Pattern repeated 3x → promote to `self-improving/memory.md`
- Check if `memory.md` exceeds 100 lines — compact if needed
- Unused rules (30+ days) → demote to domains/ or archive/

## 5. TODO List
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

# 3b. Seed the onboarding project (template-specific)
if [ "$TEMPLATE" = "curious" ]; then
    ONBOARDING_DIR="$AGENT_ROOT/workspace/projects/becoming"
    ONBOARDING_NAME="becoming"
else
    ONBOARDING_DIR="$AGENT_ROOT/workspace/projects/onboarding"
    ONBOARDING_NAME="onboarding"
fi

if [ ! -d "$ONBOARDING_DIR" ]; then
    mkdir -p "$ONBOARDING_DIR"

    # Use template-specific summary if it exists, otherwise use default
    if [ -f "$TEMPLATE_DIR/onboarding-summary.md" ]; then
        cp "$TEMPLATE_DIR/onboarding-summary.md" "$ONBOARDING_DIR/summary.md"
    else
        cat > "$ONBOARDING_DIR/summary.md" <<'TMPL'
# Project: Onboarding

**Goal:** Establish a productive working relationship with the user. Learn who they are, what they want support with, and how they want you to show up.

**Status:** Active — this is your first project. Work it honestly.

**End state:** You know the user well enough to support them effectively. At least one real project AND at least one area have been created with the user's input. A reliable check-in rhythm is established.

## What onboarding actually involves

1. **Read all workspace files thoroughly** — CLAUDE.md, USER.md, HEARTBEAT.md, TODO.md. These are your starting context.

2. **Introduce yourself on first interaction** — briefly. Tell the user what you understand about your role, and ask them what's missing.

3. **Ask to learn, not to interrogate** — Discover what the user is working on, what's hard for them, what they want help with. Don't demand a full questionnaire. Learn across conversations.

4. **Document learnings** — As you learn things about the user, write them to `items.md` in this project, and eventually promote durable facts into USER.md itself.

5. **Help identify first real projects and areas** — When something recurring or substantial comes up, ask if it should become a project or area. Create the folders together.

   **Projects are easier to spot** — they have a goal and end state ("build X", "complete Y course", "launch Z"). Don't let the agent's bias toward concrete projects cause areas to be neglected.

   **Areas need proactive discovery.** An area is an ongoing life domain with no end state. Every user has several. During onboarding, actively look for patterns that suggest areas exist and should be created:

   - **Work/profession** — how the user earns a living, even if it's complicated or in transition
   - **Health & self-care** — physical, mental, emotional well-being
   - **A creative or learning pursuit** — something they're developing over time
   - **Important relationships** — family, partners, close friends, chosen community
   - **Home & environment** — living space, possessions, systems that need maintenance
   - **Financial life** — income streams, bills, savings, debts

   These aren't universal — different users have different areas. The point is to recognize the patterns when they surface. If the user mentions something in passing that clearly fits one of these domains, propose creating the area and write its first summary together.

   **Create at least one area during onboarding.** The first area is the hardest to create — once one exists, the pattern becomes obvious. Aim to establish one before onboarding completes.

6. **Establish a check-in rhythm** — Figure out how often the user wants you to check in. Daily? On-demand? Heartbeat-driven? Respect their answer.

7. **Mark onboarding complete when you reach steady state** — You have context, you have projects, you have rhythm. At that point, update this summary to "Complete" and the onboarding project can be removed or archived.

## Working principles during onboarding

- **Curiosity over efficiency** — This is the one time where asking questions is the primary work.
- **Don't assume you know the user** — Even if USER.md is detailed, ask to confirm what matters most right now.
- **Write things down immediately** — If the user mentions a goal, a constraint, a preference — capture it in items.md before the conversation ends.
- **Small commitments, kept** — Don't offer to do everything. Offer one thing, do it well.
TMPL
    fi

    cat > "$ONBOARDING_DIR/items.md" <<TMPL
# ${ONBOARDING_NAME^} — Items

Learnings about the user, captured as you discover them. Once this list gets
substantial, promote the durable facts into USER.md.

- (Nothing yet. Start here.)
TMPL
    echo "  Created $ONBOARDING_DIR/"
fi

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

# 5. Install browser-mcp (ships with cc-assistant)
echo "[5/8] Installing browser-mcp..."
BROWSER_MCP_DIR="$SCRIPT_DIR/browser-mcp"
if [ -d "$BROWSER_MCP_DIR" ] && [ -f "$BROWSER_MCP_DIR/package.json" ]; then
    (cd "$BROWSER_MCP_DIR" && npm install --silent && npm run build --silent) 2>&1 | tail -1
    # Wire up workspace agent's .mcp.json
    WORKSPACE_MCP="$AGENT_ROOT/workspace/.mcp.json"
    if [ ! -f "$WORKSPACE_MCP" ]; then
        cat > "$WORKSPACE_MCP" <<MCPEOF
{
  "mcpServers": {
    "browser-mcp": {
      "command": "node",
      "args": ["$BROWSER_MCP_DIR/dist/index.js"],
      "env": {
        "CDP_URL": "http://localhost:9222"
      }
    }
  }
}
MCPEOF
        echo "  browser-mcp configured in $WORKSPACE_MCP"
    else
        echo "  $WORKSPACE_MCP already exists, skipping"
    fi
    echo "  Note: Chrome must be running with --remote-debugging-port=9222"
else
    echo "  browser-mcp submodule not found — run: git submodule update --init"
fi

# 6. Optional add-ons (interactive)
echo ""
echo "=== Optional Add-ons ==="
echo ""
echo "  [1] Google Workspace  — Gmail, Calendar, Drive access via MCP"
echo "                          (requires Google Cloud OAuth setup after install)"
echo ""
read -rp "Install add-ons? (comma-separated numbers, or Enter to skip): " ADDONS
echo ""

ADDON_GOOGLE=false
for addon in $(echo "$ADDONS" | tr ',' ' '); do
    case "$addon" in
        1) ADDON_GOOGLE=true ;;
        *) echo "  Unknown add-on: $addon, skipping" ;;
    esac
done

if [ "$ADDON_GOOGLE" = true ]; then
    echo "[6a] Adding Google Workspace MCP..."
    # Add google-workspace to the workspace agent's .mcp.json
    WORKSPACE_MCP="$AGENT_ROOT/workspace/.mcp.json"
    if [ -f "$WORKSPACE_MCP" ]; then
        # Merge into existing .mcp.json
        python3 -c "
import json, sys
with open('$WORKSPACE_MCP') as f:
    config = json.load(f)
config['mcpServers']['google-workspace'] = {
    'command': 'uvx',
    'args': ['workspace-mcp', '--tool-tier', 'extended'],
    'env': {
        'GOOGLE_OAUTH_CLIENT_ID': 'YOUR_GOOGLE_CLIENT_ID',
        'GOOGLE_OAUTH_CLIENT_SECRET': 'YOUR_GOOGLE_CLIENT_SECRET'
    }
}
with open('$WORKSPACE_MCP', 'w') as f:
    json.dump(config, f, indent=2)
"
    else
        cat > "$WORKSPACE_MCP" <<'MCPEOF'
{
  "mcpServers": {
    "google-workspace": {
      "command": "uvx",
      "args": ["workspace-mcp", "--tool-tier", "extended"],
      "env": {
        "GOOGLE_OAUTH_CLIENT_ID": "YOUR_GOOGLE_CLIENT_ID",
        "GOOGLE_OAUTH_CLIENT_SECRET": "YOUR_GOOGLE_CLIENT_SECRET"
      }
    }
  }
}
MCPEOF
    fi
    echo "  Google Workspace MCP added to $WORKSPACE_MCP"
    echo ""
    echo "  !! Post-install steps for Google Workspace:"
    echo "     1. Create a Google Cloud project and enable the Gmail, Calendar, and Drive APIs"
    echo "     2. Create OAuth 2.0 credentials (Desktop app type)"
    echo "     3. Edit $WORKSPACE_MCP — replace YOUR_GOOGLE_CLIENT_ID and YOUR_GOOGLE_CLIENT_SECRET"
    echo "     4. Run: uvx workspace-mcp --authenticate"
    echo "        to complete the OAuth flow and store refresh tokens"
    echo ""
fi

# 7. Install Python dependencies
echo "[7/8] Installing Python dependencies..."
cd "$SCRIPT_DIR"
uv sync 2>&1 | tail -1

# 8. Install systemd user service
echo "[8/8] Installing systemd service..."
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
echo "  │   ├── self-improving/     # Tiered learning system (memory, corrections, domains)"
echo "  │   ├── projects/          # Active work with end states"
echo "  │   └── areas/             # Ongoing life domains"
echo "  ├── modules/               # Custom modules (telegram.py, cron.py per module)"
echo "  ├── signals/               # Task completion signals"
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
