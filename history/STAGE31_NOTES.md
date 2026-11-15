# Stage 31 — Short.io Mini App analytics (incremental)

Based on full Stage 30 archive. Existing files retained; changes confined to Short.io integration, sticker_miniapp router, and new UTM Mini App page.

## Implemented
- Signed Telegram initData authorization on UTM endpoints; API key remains server-side.
- POST /api/utm/create: full UTM and Short.io URL pair, no GPT prefixes.
- POST /api/utm/dashboard: locally recorded links, Short.io lifetime click counts and attempted provider breakdowns; unavailable data marked as errors.
- POST /api/utm/import: idempotent first batch of up to 150 existing Short.io links.
- /utm.html: interactive creation and real-data chart, raw breakdown sections, import control.

## Unfinished / caveats
- The Short.io detailed statistics endpoints and response formats have NOT been validated against a real account; they may require adjustments, permissions or a paid plan. No live connection or deployment was performed.
- Import currently handles one batch only; pagination and full-history sync are NOT implemented.
- /utm.html is a separate page; it has not yet been linked from the primary Mini App menu or Telegram bot commands. Telegram signed initData is required; direct browser use will be rejected.
- No daily trend chart normalization or city/device pie charts yet. The first chart is clicks by platform, if the API supplies click totals.
- The existing Stage 30 Telegram command integration was not confirmed by code inspection; do not assume it works. This stage does not claim to repair all Telegram handlers.
- Clicks are not conversions. Location is approximate when supplied by provider.
- The Short.io API key must be configured in the environment. Never paste it in chat.
