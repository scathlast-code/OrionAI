# Stage 20 — Persistent policy alarms

- Policy page changes and fetch failures now create persistent, non-self-clearing alarm records.
- `/policies alerts` lists alarms with the original official source URL; `/policies ack ID` explicitly acknowledges them.
- Existing `/policies scan` reports unresolved alarms even when current check succeeds.
- The monitor does **not yet** perform independent web research, legal interpretation, proactive scheduled alerts or Mini App badge integration. Do not claim those features work.
- `ack` means the owner reviewed the incident; it does not mean the issue is fixed.
