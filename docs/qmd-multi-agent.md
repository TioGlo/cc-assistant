# qmd Multi-Agent Setup

How to configure [qmd](https://github.com/tobi/qmd) (local markdown search engine) for multiple cc-assistant agents on the same machine or across machines.

## Overview

Each agent gets its own qmd index with collections scoped to that agent's data. This prevents agents from seeing each other's sessions or workspace files, and allows independent embedding and search.

qmd's storage is controlled by two XDG environment variables:
- `XDG_CACHE_HOME` — where the SQLite database and model cache live
- `XDG_CONFIG_HOME` — where the collection config (`index.yml`) lives

By setting these per-agent, each agent gets a fully isolated qmd instance.

## Prerequisites

```bash
# Install qmd globally (requires Node.js 24+)
npm install -g @tobilu/qmd

# Verify
qmd --help
```

**Node version note:** qmd's `better-sqlite3` dependency is compiled against the Node version used at install time. If your system has multiple Node versions (e.g. via nvm), qmd must run under the same version it was installed with. The wrapper scripts below handle this.

## Per-Agent Setup

### 1. Create the qmd directory structure

```bash
AGENT_ROOT=~/.luci  # adjust per agent

mkdir -p "$AGENT_ROOT/qmd/cache"
mkdir -p "$AGENT_ROOT/qmd/config"
```

### 2. Create the agent's qmd wrapper script

This wrapper sets the XDG env vars so qmd uses the agent's own database, then forwards all arguments.

```bash
cat > "$AGENT_ROOT/qmd-mcp" << 'WRAPPER'
#!/bin/bash
# qmd MCP server scoped to this agent's data
AGENT_ROOT="$(cd "$(dirname "$0")" && pwd)"
export XDG_CACHE_HOME="$AGENT_ROOT/qmd/cache"
export XDG_CONFIG_HOME="$AGENT_ROOT/qmd/config"

# If using nvm, ensure the correct Node version:
# export PATH="$HOME/.nvm/versions/node/v24.4.0/bin:$PATH"

exec qmd mcp "$@"
WRAPPER
chmod +x "$AGENT_ROOT/qmd-mcp"
```

Also create a CLI helper (for manual `qmd` commands scoped to this agent):

```bash
cat > "$AGENT_ROOT/qmd-cli" << 'CLI'
#!/bin/bash
# Run qmd CLI commands scoped to this agent's data
AGENT_ROOT="$(cd "$(dirname "$0")" && pwd)"
export XDG_CACHE_HOME="$AGENT_ROOT/qmd/cache"
export XDG_CONFIG_HOME="$AGENT_ROOT/qmd/config"

# If using nvm:
# export PATH="$HOME/.nvm/versions/node/v24.4.0/bin:$PATH"

exec qmd "$@"
CLI
chmod +x "$AGENT_ROOT/qmd-cli"
```

### 3. Add collections

Each agent should have at minimum two collections:

**Agent workspace** — the agent's markdown files (CLAUDE.md, projects, areas, etc.):

```bash
$AGENT_ROOT/qmd-cli collection add "$AGENT_ROOT/workspace" \
  --name "${AGENT_NAME}-workspace" \
  --mask "**/*.md"
```

**Claude Code sessions** — session transcripts for this agent's conversations:

```bash
# Claude Code stores session transcripts at:
#   ~/.claude/projects/{project-path-hash}/
# The exact path depends on which directories the agent runs from.
# Check ~/.claude/projects/ and identify the directory for your agent.

# For sessions from the agent's workspace:
SESSION_DIR=$(ls -d ~/.claude/projects/*${AGENT_NAME}* 2>/dev/null | head -1)

$AGENT_ROOT/qmd-cli collection add "$SESSION_DIR" \
  --name "${AGENT_NAME}-sessions" \
  --mask "**/*.md"
```

**Optional — agent-specific data directories:**

```bash
# If the agent has a dedicated content directory (like Prana's media pipeline)
$AGENT_ROOT/qmd-cli collection add "$AGENT_ROOT/prana" \
  --name "prana-content" \
  --mask "**/*.md"
```

### 4. Add collection context

Context helps qmd understand what's in each collection, improving search quality:

```bash
$AGENT_ROOT/qmd-cli context add "$AGENT_ROOT/workspace" \
  "Agent workspace — identity docs, projects, areas, skills, prompts"

$AGENT_ROOT/qmd-cli context add "$SESSION_DIR" \
  "Claude Code session transcripts — conversations, decisions, research"
```

### 5. Build the initial index

```bash
# Index all collections
$AGENT_ROOT/qmd-cli update

# Create vector embeddings (slower, enables semantic search)
$AGENT_ROOT/qmd-cli embed
```

### 6. Register as MCP server

Add qmd to the agent's MCP config. For the main assistant (runs via `claude -p` from workspace):

```json
// {AGENT_ROOT}/workspace/.mcp.json (or global ~/.claude.json)
{
  "mcpServers": {
    "qmd": {
      "command": "/home/user/.luci/qmd-mcp",
      "args": []
    }
  }
}
```

For the coding agent (runs from `{AGENT_ROOT}/coding/`):

```json
// {AGENT_ROOT}/coding/.mcp.json
{
  "mcpServers": {
    "qmd": {
      "command": "/home/user/.luci/qmd-mcp",
      "args": []
    }
  }
}
```

Or register globally for all Claude Code sessions on this machine:

```bash
claude mcp add -s user qmd -- /home/user/.luci/qmd-mcp
```

### 7. Keep the index fresh

#### Option A: Cron job

```bash
# Add to crontab: re-index every 30 minutes
(crontab -l 2>/dev/null; echo "*/30 * * * * $AGENT_ROOT/qmd-cli update --pull 2>/dev/null") | crontab -
```

#### Option B: Claude Code hook

Create a hook that re-indexes on session end or compaction:

```bash
# {AGENT_ROOT}/hooks/qmd-reindex.sh
#!/bin/bash
AGENT_ROOT="$(dirname "$(dirname "$0")")"
export XDG_CACHE_HOME="$AGENT_ROOT/qmd/cache"
export XDG_CONFIG_HOME="$AGENT_ROOT/qmd/config"
qmd update 2>/dev/null &
```

Register in `~/.claude/settings.json`:

```json
{
  "hooks": {
    "PostToolUse": [{
      "matcher": "Write|Edit",
      "hooks": [{"type": "command", "command": "/path/to/qmd-reindex.sh"}]
    }]
  }
}
```

#### Option C: Scheduled job in cc-assistant

Add to the agent's `config.yaml`:

```yaml
scheduler:
  jobs:
    - name: "qmd-reindex"
      prompt: "Run: $AGENT_ROOT/qmd-cli update && $AGENT_ROOT/qmd-cli embed"
      cron: "0 */6 * * *"
```

## Multi-Agent on Same Machine

Example: Qu and Prana on `ra`, each with their own qmd index.

```
~/.assistant/          # Qu
├── qmd/
│   ├── cache/         # Qu's SQLite database
│   └── config/        # Qu's collection config
├── qmd-mcp            # Qu's MCP wrapper
├── qmd-cli            # Qu's CLI wrapper
├── workspace/         # indexed as "qu-workspace"
└── coding/

~/.assistant/prana/    # Prana (sub-agent of Qu)
├── qmd/
│   ├── cache/
│   └── config/
├── qmd-mcp
├── qmd-cli
└── ...                # indexed as "prana-workspace"
```

Each agent's `.mcp.json` points to its own `qmd-mcp` wrapper. Collections never overlap unless you intentionally add the same directory to multiple agents' indexes.

## Cross-Agent Search (optional)

If you want one agent to search another's data (e.g., Qu searching Prana's content), add the other agent's directory as a collection:

```bash
# In Qu's qmd, add read access to Prana's workspace
~/.assistant/qmd-cli collection add ~/.assistant/prana \
  --name "prana-content" \
  --mask "**/*.md"
```

This is one-directional — Prana doesn't automatically get access to Qu's data.

## Multi-Machine Setup

For agents on different machines (e.g., Luci on `ram-dass`), each machine runs its own qmd installation. No shared state is needed — qmd is purely local.

```bash
# On ram-dass as user luci:
npm install -g @tobilu/qmd
# Then follow the per-agent setup above with AGENT_ROOT=~/.luci
```

## Verifying the Setup

```bash
# Check index status
$AGENT_ROOT/qmd-cli status

# List collections
$AGENT_ROOT/qmd-cli collection list

# Test a search
$AGENT_ROOT/qmd-cli search "test query" -c "${AGENT_NAME}-workspace"

# Test semantic search (requires embeddings)
$AGENT_ROOT/qmd-cli query "what are the agent's responsibilities"
```

## Migrating from OpenClaw qmd

If you have an existing qmd index from OpenClaw (stored at `~/.openclaw/agents/main/qmd/`), you can either:

1. **Start fresh** (recommended) — create new collections pointing to the cc-assistant paths. Old session transcripts are still on disk and can be re-indexed if needed.

2. **Keep the old index** for historical search and create a new index for the agent. Add the old session dir as a read-only collection in the new agent's qmd.

The old OpenClaw qmd used custom XDG paths set by OpenClaw's runtime. The cc-assistant wrapper scripts replace that mechanism with explicit per-agent env vars.
