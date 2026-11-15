# Stage 28 — non-destructive sleep and feedback integration

Source: complete Stage 27 archive. Existing files are retained, except for the additive changes to `bot.py` and `runtime_control.py`. Previous stage notes, tests, plugins, web UI, Mini App, and documentation remain included.

## Implemented
- Telegram owner-only `/sleepmode <module> <active|light|deep|emergency>` and read-only `/sleepmode <module>`.
- Telegram `/sleepmode <module> schedule <delay_minutes> <duration_minutes> [light|deep]` stored in SQLite and applied by the existing worker tick.
- Telegram `/feedback <bug|suggestion|rating|result> <module> <text>` persisted in SQLite.
- Existing background task worker checks `scheduler` and `ai` admission; existing backup task checks `backups`; `run_task` checks `ai` admission. All modules default to active.
- Schedule expiration restores active only if the module is still in the scheduled sleep mode, so emergency intervention is not overwritten.
- Existing `notification_modes.py` and its deep-sleep/quiet-hours semantics are preserved, not migrated or removed.

## Limitations
- There is no full UI in Mini App for these new controls. Other plugins and worker paths have not all been connected to the admission controller. `greenpost` can be configured but its independent workers are not yet gated by this Stage 28 integration.
- Sleep scheduling uses the bot's background tick; if the bot process is down, it cannot wake itself until restarted. The schedule is absolute and one-shot, not recurring.
- Parent bot command transport, heartbeat/feedback forwarding, and delegated capability handling remain outside this stage. Only the local owner can modify sleep policy.
- No actual cloud servers or Telegram accounts were deployed or tested live.
