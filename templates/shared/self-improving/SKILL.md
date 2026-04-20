---
name: self-improving
description: "Tiered self-improvement system with automatic enforcement via hooks. Captures corrections, errors, and learnings. Auto-promotes recurring patterns. Integrates with heartbeat for periodic review. Use when: (1) user corrects you, (2) a command fails, (3) you discover a better approach, (4) starting non-trivial work (read memory first)."
---

# Self-Improving Skill

Tiered memory system for learning from mistakes, corrections, **and positive signals**. Designed for cc-assistant agents but works with any Claude Code setup.

Key difference from other self-improvement skills: **enforcement via hooks** so the agent can't forget to use it.

**Important:** Hooks only fire in interactive Claude Code TUI sessions. For `claude -p` sessions (e.g., Telegram bot via cc-assistant), enforcement comes from the system prompt and heartbeat. This skill is designed to work in both modes.

## Setup

### 1. Create the directory structure

```bash
WORKSPACE="$AGENT_ROOT/workspace"  # adjust per agent

mkdir -p "$WORKSPACE/self-improving/projects"
mkdir -p "$WORKSPACE/self-improving/domains"
mkdir -p "$WORKSPACE/self-improving/archive"
```

### 2. Create seed files

Copy from `assets/` or create:

```bash
cp assets/memory.md "$WORKSPACE/self-improving/memory.md"
cp assets/corrections.md "$WORKSPACE/self-improving/corrections.md"
```

### 3. Add to CLAUDE.md

Add this section to the agent's `CLAUDE.md` (this is the most important step — CLAUDE.md is loaded every session):

```markdown
## Self-Improving

**Before non-trivial work:** Read `self-improving/memory.md`. Load relevant domain/project files if they exist.

**When corrected or when something fails:**
1. Log to `self-improving/corrections.md` immediately
2. If the lesson is broadly applicable, add to `self-improving/memory.md`
3. If domain-specific, add to `self-improving/domains/` or `self-improving/projects/`

**Promotion rule:** Pattern repeated 3x → promote to memory.md (HOT). Unused 30 days → demote to WARM. Unused 90 days → archive.

**Self-reflection:** After completing significant work, pause: Did it meet expectations? What could be better? Is this a pattern?

**During heartbeat:** Review corrections.md for pending promotions. Check if memory.md exceeds 100 lines and compact if needed.

**Never infer from silence.** Only log explicit corrections, stated preferences, or repeated patterns. Don't learn from what the user *didn't* say.
```

### 4. Add to HEARTBEAT.md

Insert before the TODO section:

```markdown
## N. Self-Improving
- Read `self-improving/corrections.md` — any pending entries that should be promoted?
- Pattern repeated 3x → promote to `self-improving/memory.md`
- Check if `memory.md` exceeds 100 lines — compact if needed
- Unused rules (30+ days) → demote to domains/ or archive/
```

### 5. Add to system prompt (for `claude -p` / bot sessions)

If your agent runs via `claude -p` (e.g., cc-assistant Telegram bot), hooks won't fire. Add self-improving to the system prompt in `config.yaml`:

```yaml
claude:
  system_prompt: |
    ...existing prompt...

    SELF-IMPROVING: When corrected or when something fails, log to
    workspace/self-improving/corrections.md immediately. Before non-trivial
    work, read workspace/self-improving/memory.md. Log positive signals too —
    when an approach works well, note it.
```

This is the `claude -p` equivalent of the hook-based enforcement.

### 6. Install hooks (the enforcement layer for TUI sessions)

Copy the hook scripts:

```bash
cp hooks/self-improving-activator.sh "$AGENT_ROOT/hooks/"
cp hooks/error-detector.sh "$AGENT_ROOT/hooks/"
chmod +x "$AGENT_ROOT/hooks/self-improving-activator.sh"
chmod +x "$AGENT_ROOT/hooks/error-detector.sh"
```

Register in `~/.claude/settings.json` (merge with existing hooks):

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "/path/to/agent/hooks/self-improving-activator.sh"
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "/path/to/agent/hooks/error-detector.sh"
          }
        ]
      }
    ]
  }
}
```

### 7. (Optional) Add to PreCompact hook

If you have a PreCompact hook, add this to re-inject HOT memory when context is compressed:

```bash
echo "## Self-Improving HOT Rules"
head -40 "$WORKSPACE/self-improving/memory.md" 2>/dev/null || echo "None"
```

This ensures the most critical rules survive long sessions where context gets truncated.

## How It Works

### Four enforcement layers

| Layer | Mechanism | What it does |
|-------|-----------|-------------|
| **CLAUDE.md** | Loaded every session | Instructions to read memory before work, log corrections immediately |
| **UserPromptSubmit hook** | Fires after every user message | Injects reminder: "if this involves a correction, log it" |
| **PostToolUse hook** | Fires after failed Bash commands | Flags errors for potential logging |
| **Heartbeat** | Periodic cron (e.g. every 2h) | Reviews corrections for promotions, compacts memory |
| **PreCompact hook** | Fires before context compression | Re-injects HOT rules so they survive long sessions |

The agent can't forget to learn because the system reminds it involuntarily.

**Mode coverage:**

| Agent mode | Enforcement mechanism |
|------------|----------------------|
| Interactive TUI (tmux agents) | Hooks + CLAUDE.md + heartbeat + PreCompact |
| `claude -p` (Telegram bot) | system_prompt + CLAUDE.md + heartbeat |

### Memory boundaries with auto-memory

Claude Code has a built-in auto-memory system at `~/.claude/projects/*/memory/`. To avoid fragmentation:

| What to store | Where |
|---------------|-------|
| User facts, preferences, profile | Auto-memory (`user` type) |
| Project context, decisions, deadlines | Auto-memory (`project` type) |
| External resource pointers | Auto-memory (`reference` type) |
| **Behavioral rules — how to act** | **self-improving/memory.md** |
| **Corrections — what went wrong** | **self-improving/corrections.md** |
| **Domain knowledge — tool gotchas** | **self-improving/domains/** |

Rule of thumb: "Who is the user?" → auto-memory. "How should I behave?" → self-improving.

### Tiered storage

| Tier | Location | Size Limit | Behavior |
|------|----------|------------|----------|
| **HOT** | `memory.md` | ≤100 lines | Always loaded. Confirmed rules only. |
| **WARM** | `projects/`, `domains/` | ≤200 lines each | Load on context match. |
| **COLD** | `archive/` | Unlimited | Load on explicit query. |

### Promotion / demotion rules

- Pattern repeated **3x** → promote to HOT (`memory.md`)
- Before promoting, agent confirms with user if ambiguous
- Unused **30 days** → demote to WARM
- Unused **90 days** → archive to COLD
- **Never delete** without asking

### What to log

| Signal | Type | Action |
|--------|------|--------|
| User says "no", "actually", "that's wrong" | correction | Log → `corrections.md` |
| User says "I prefer X", "always do Y" | preference | Log → `memory.md` |
| Command fails with non-zero exit | error | Evaluate → `corrections.md` |
| Same instruction given 3x | pattern | Promote to rule → `memory.md` |
| User says "yes exactly", "perfect", accepts non-obvious choice | positive | Log → `corrections.md` with type `positive` |
| An approach works well in practice | positive | Log → `corrections.md` with type `positive` |

**Positive signals are easy to miss.** Corrections are loud — the user says "no." Confirmations are quiet — the user just moves on. Watch for both.

### What NOT to log

- One-time instructions ("do X now")
- Context-specific directions ("in this file...")
- Hypotheticals ("what if...")
- Inferences from silence (the user didn't complain ≠ the user approves)

### Conflict resolution

1. Most specific wins (project > domain > global)
2. Most recent wins (same level)
3. If ambiguous → ask user

### Compaction (when memory.md exceeds 100 lines)

1. Merge similar corrections into single rules
2. Archive unused patterns
3. Summarize verbose entries
4. Never lose confirmed preferences

## Logging Format

### corrections.md

Simple table format. Quick to write, easy to scan:

```markdown
| Date | What I Got Wrong | Correct Behavior | Status |
|------|-----------------|------------------|--------|
| 2026-04-13 | Did X | Should have done Y | pending |
```

Status values: `pending`, `promoted: memory.md`, `promoted: domains/X.md`, `noted`, `wont_fix`

### memory.md

Structured rules with provenance:

```markdown
### RULE NAME IN CAPS
**Promoted:** date | **Source:** where this came from
One to three lines explaining the rule and when it applies.
```

### Self-reflection

After significant work:

```
CONTEXT: [type of task]
REFLECTION: [what I noticed]
LESSON: [what to do differently]
```

## Common Traps

| Trap | Why It Fails | Better Move |
|------|--------------|-------------|
| Learning from silence | Creates false rules | Wait for explicit correction |
| Promoting too fast | Pollutes HOT memory | Keep tentative until 3x |
| Reading all memory files | Wastes context | Load only HOT + smallest relevant files |
| Compacting by deletion | Loses history | Merge, summarize, or demote instead |
| Verbose log entries | Memory bloat | One-liners in corrections, short rules in memory |
