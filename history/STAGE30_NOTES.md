# Stage 30 — Short.io + Telegram UTM

Built incrementally from the complete Stage 29 archive; previous files preserved, apart from the targeted Telegram /utm handler change and addition of this integration.

## Configuration (server environment only)
- SHORTIO_API_KEY: secret private Short.io API key; never enter it in Telegram or Mini App.
- SHORTIO_DOMAIN: default greenpost.s.gy.
- SHORTIO_DOMAIN_ID: numeric Short.io domain ID; required for click counts and importing existing links.
- ORION_SHORTIO_DB: default data/greenpost_shortio.sqlite3.

## Commands
- /utm URL TAG PLATFORM — creates both URLs using Short.io, records them in existing ledger and a new separate registry.
- /utm clicks — real provider click counts for locally registered links and textual bar chart; optional PNG chart if matplotlib installed.
- /utm recent and /utm stats retain previous semantics; stats counts generations, not clicks.

## Limitations (not hidden)
- No API key or no provider access: short creation fails explicitly, not simulated.
- Existing Short.io links aren't yet imported to the local registry; provider `list_links()` is implemented as integration primitive.
- Country/city/device analytics and Mini App UI are not yet connected to Short.io statistics endpoints.
- Click endpoint used here returns lifetime click totals, not time-filtered unique visits or conversions.
- The Short.io key must remain on the server, never in browser JS.
- No deployment or live account access was performed.
- Existing first-party shortener is preserved and not overwritten.
