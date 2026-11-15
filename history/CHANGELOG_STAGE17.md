# Stage 17 — lifetime statistics reporting

- Added `/stats` for each user (numeric totals and per-mode requests), `/stats all` and `/stats users` for configured owners only.
- Added numeric-only per-mode and per-user aggregation and consistent SQLite snapshot backup helper.
- Incognito content is not written to this ledger. User ID, mode, timestamps, and counters **are** retained; this is not fully anonymous.
- Duration/tokens/cost/failures will be zero unless the corresponding execution paths instrument those values. Do not mistake zero for verified zero consumption.
- Backup helper does not automatically schedule offsite backup; protect and persist the database.
- No network deployment or Telegram live testing performed.
