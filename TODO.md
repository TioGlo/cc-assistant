# cc-assistant TODO

## Token Usage Management (urgent — hitting MAX plan limits)

- [ ] `/pause` command — pause the bot (stop processing Telegram messages and cron jobs)
- [ ] `/pause prana` — pause a specific agent/module without stopping the whole bot
- [ ] `/resume` / `/resume prana` — unpause
- [ ] Token usage tracking — log token counts per `claude -p` invocation (from JSON output)
- [ ] Usage reporting — `/usage` command showing tokens consumed by job name, agent, and time period
- [ ] Usage-aware scheduling — skip or defer low-priority cron jobs when approaching limits
- [ ] Budget alerts — warn via Telegram when approaching daily/weekly usage thresholds

## Task Monitoring & Awareness

### Phase 1 — Lifecycle Hooks (shipped 2026-04-17, in evaluation)

- [x] `task-received.sh` hook — writes `signals/received/{task-id}.json` when agent gets prompt
- [x] `task-stopped.sh` hook — writes `signals/stopped/{task-id}.json` when agent finishes a turn (with summary from transcript)
- [x] `[TASK:id]` marker injected into dispatches so hooks can identify them
- [x] Watcher early-failure detection — alerts at 30s if dispatch never landed, not after full timeout
- [x] Lifecycle-aware timeout diagnostics — three distinct messages based on received/stopped state
- [x] Terser timeout reports — pane excerpt inline, full capture saved to diagnostic file
- [x] Registered in install.sh for workspace agent
- [x] Deployed to Qu's workspace and prana .claude/settings.json

### Phase 1 Evaluation (wait at least 1 week)

Need real-world data before Phase 2:
- [ ] Confirm hooks fire reliably on successful dispatches — check `signals/received/` and `signals/stopped/` accumulate entries
- [ ] Verify Stop hook's transcript parse works across real agent turns (bash jsonl parsing is the fragile piece)
- [ ] Observe a dispatch-never-landed scenario in the wild — does the 30s warning actually help?
- [ ] Check whether 30s deadline is right, too aggressive, or too lenient
- [ ] Confirm timeout diagnostics provide useful info vs. adding noise
- [ ] Watch for unanticipated bugs (task-id collisions, permission issues, etc.)

### Phase 2 — Dispatcher/Bot/Heartbeat Awareness (pending Phase 1 validation)

- [ ] `/tasks` command — show active tmux dispatches (what's running, how long, which agent)
- [ ] Task duration tracking — log start/end time and duration per dispatch
- [ ] Stuck task detection — heartbeat scans `signals/received/` for entries older than N minutes without signal
- [ ] Task history — persist completed task summaries for review (what ran, when, outcome)
- [ ] Agent status dashboard — `/status` shows per-agent state (idle/busy, last task, uptime)
- [ ] Heartbeat awareness of running tasks — don't dispatch new work to a busy agent
- [ ] Auto-compact before dispatch when context is near limit (root-cause fix for the prana failure)
