# Stage 18 — provider policy monitoring (initial integration)

- Real SQLite-based source registry, snapshots and change events. No automatic acceptance of changed terms.
- Owner-only `/policies` command in both Telegram bots. Add a verified HTTPS terms URL, scan on demand, view changes and mark reviewed.
- Failures are visible; first fetch establishes baseline and does not falsely claim a change.
- Important limitations: this detects changed page bytes, **not** legal meaning, effective dates, pricing or official notices. No scheduled scanning, push alerts, Mini App integration, or automatic remediation yet. Sources must be added by the owner; do not feed arbitrary untrusted URLs (SSRF risk). Never add internal URLs or credentials.
- On production, use an allowlist of known provider domains, block private-network resolution and redirects, and schedule scans with incident notifications.
