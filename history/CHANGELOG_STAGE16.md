# Stage 16 — Lifetime numeric statistics (partial integration)

- Added `lifetime_stats.py` with SQLite append-only event ledger, application-level DELETE/UPDATE triggers, per-bot/user/mode aggregation.
- Hooked **ordinary incoming message handlers** in OrionAI and mother bot to count admitted requests, without message content.
- Incognito mode has a numeric-only schema but **incognito remains disabled** in `orion_modes.py` pending privacy audit.
- Duration, token and cost metrics can be recorded using `lifetime_stats.record`, but are **not yet automatically wired to all providers, jobs, Mini App calls, commands or sessions**. Do not interpret zero as measured zero.
- No automatic reset or truncate path is provided. DB administrator, storage loss or disk corruption can still destroy data; use immutable off-site backups, durable volume, DB ACLs and integrity checks.
- Set `LIFETIME_STATS_DB` to a shared persistent database path on a durable volume for both bots if co-hosted. SQLite files on separate servers are not automatically synchronized. No guarantee of exactly-once counts under retries; next phase needs request deduplication and durable ingestion.
- Do not log raw incognito messages; complete privacy audit and secure deletion are separate work.
