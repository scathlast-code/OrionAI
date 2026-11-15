# Stage 15 — Telegram access administration

- Added `/users grant orion 123456789`, `/users revoke orion 123456789`, `/users list orion` to both Telegram bots for bootstrap owners only.
- Existing persistent SQLite grants and audit trail are used; no Railway/GitHub visit is needed for user grants.
- OrionAI membership checks now respect active grants and explicit revocations.
- Manager bot remains owner-only by design: grants for manager are stored but do not by themselves authorize manager interface use. Do not advertise manager access as functional for nonowners.
- Existing OrionAI invitation workflow remains available; direct ID grants are an additional mechanism.
- Requires a persistent SECURITY_GRANTS_DB volume shared by the relevant services. If bots run on separate hosts, they need a shared authoritative access service; SQLite files on different hosts will not synchronize automatically.
- The bootstrap owner still needs ALLOWED_USERS configured once at deployment.
