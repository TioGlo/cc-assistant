# Workspace Reference

This document describes the structure and conventions of your workspace. Read it when you need to understand how things are organized. It's a reference, not an identity — your identity lives in `CLAUDE.md`.

## Directory Layout

```
workspace/
├── CLAUDE.md           # Your identity and operating instructions (yours to maintain)
├── USER.md             # Profile of the user you support
├── HEARTBEAT.md        # Periodic check-in checklist
├── TODO.md             # Living scratchpad for pending items
├── projects/           # Active work with end states
│   └── {name}/
│       ├── summary.md  # Current state, blockers, goals
│       └── items.md    # Timestamped log of facts and decisions
└── areas/              # Ongoing life domains (no end state)
    └── {name}/
        ├── summary.md  # Current state and responsibilities
        └── items.md    # Timestamped log of facts and decisions
```

## Projects & Areas

You use a simplified PARA system to organize your knowledge. This isn't a filing system for the user — it's how *you* manage what you know and what you're responsible for.

### Projects

A project has a goal and an end state. It's something you're actively working on that will eventually be done.

- Each project is a folder in `projects/` with `summary.md` and `items.md`
- `summary.md` is a **live document** — it describes the current state, not the history. Keep it fresh. If you act on a project, update the summary before you finish.
- `items.md` is an **append-only log** — timestamped facts, decisions, and observations. Add a line when something happens.
- When a project is complete, update its status and consider removing the folder.

**When to create a project:** Something you're working on has grown beyond a single conversation. It has multiple steps, multiple sessions, or requires tracking state over time.

### Areas

An area is an ongoing responsibility with no end state. It persists as long as it's relevant.

- Same structure as projects: `summary.md` + `items.md`
- Areas are more passive than projects — check them when something in recent activity relates to one.
- Examples: a user's profession, health, a recurring operational responsibility, a relationship domain.

**When to create an area:** You notice a recurring domain of activity that deserves its own context. Not everything needs to be an area — only create one when having persistent context would genuinely help you serve your user better.

### How to Use Projects & Areas

**Before acting on any task**, check if it relates to an active project or area. If it does:
1. Read the `summary.md` first — it has the current state
2. Use that context when responding
3. After acting, update the `summary.md` if the state changed
4. Add a timestamped line to `items.md` if something factual happened

The summaries are **inputs to your work**, not passive logs. If a summary is out of date, you'll give stale information.

## Key Files

- **CLAUDE.md** — Your identity. You maintain this. It should evolve as you learn and grow.
- **USER.md** — What you know about your user. Update it as you learn more about them.
- **HEARTBEAT.md** — What to check on periodic heartbeats. Work through it in order.
- **TODO.md** — Items that need attention but aren't part of a specific project. Check during heartbeats. If a TODO grows into something bigger, promote it to a project.

## Delegation

For tasks that are complex, long-running, or need full interactive Claude Code capabilities, delegate them:
```
<!--DELEGATE:{"task":"detailed description","timeout":600}-->
```

To delegate with project context auto-prepended:
```
<!--DELEGATE:{"task":"do the thing","timeout":600,"project":"project-name"}-->
```

## Scheduling

To schedule recurring work:
```
<!--SCHEDULE:{"name":"task-name","prompt":"what to do","cron":"0 8 * * *"}-->
```

To schedule a one-shot delayed task:
```
<!--REMIND:{"prompt":"what to do","delay":"2h"}-->
```

### Where scheduled jobs live (and why)

There are two files at the agent root that store scheduling state, and they have **different roles** — confusing them is a common source of bugs.

- **`config.yaml` → `scheduler.jobs`** is a **seed list**. The bot reads it once at startup and merges any jobs it doesn't already know about into the live store. After that it's effectively dormant. Editing `config.yaml` *after* first run won't change a running job — only the very first install picks up its values.
- **`scheduler-jobs.json`** is the **live store**. Every job created via a SCHEDULE block, every change made via the `/schedule` Telegram command, every modification by the agent at runtime — all written here. This is the source of truth for the scheduler.
- **`scheduler-reminders.json`** is the same shape for one-shot REMIND-style reminders.

**Implication:** runtime additions never backfill into `config.yaml`. If you create a job via SCHEDULE, it lives in `scheduler-jobs.json` only — the absence of an entry in `config.yaml` is not a bug.

**Hot-reloading without a restart:** direct edits to `scheduler-jobs.json` or `scheduler-reminders.json` need the bot to re-read the file. Use `/reload` in Telegram to pick up changes immediately — no service restart required.
