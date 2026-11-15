# Stage 22 — GreenPost navigation, UTM and connectivity diagnostics

## Changed
- Removed the legacy `astro` topic from the live topic database on initialization.
- Added independent Astronomy, Astrology, Weather (tomorrow/week/month/season), Health, Agriculture and Energy subtopics. Existing topic and tag editing remains available in Newsroom settings.
- Topic parent buttons now open nested subtopic choices, followed by short/news/big drafting choices.
- Fixed Mini App network errors: unauthorized, HTTP errors, malformed JSON and server unavailable are differentiated.
- Offline sandbox does not falsely claim its feedback was sent; it offers copy instead. Sandbox navigation includes the new categories, but is not live.
- Added `/utm URL TAG PLATFORM` command, `/utm recent`, `/utm stats` with persistent SQLite ledger, 3-day duplicate warnings and preserved original URL query parameters. Reserve formats exist in library but are not enabled via command.
- Included original GreenPost visual brief and UTM templates under `reference/`.

## Important limitations
- No actual Telegram, API or external news source was connected or live-tested here. A running deployment needs valid environment and dependencies.
- The original sandbox snapshot remains synthetic. It does not prove real backend actions or news verification.
- UTM checks validate URL syntax and query parameters, NOT that the destination site is online (no false '3x verified' claim).
- Weather forecast categories are search topics, NOT a connected weather forecast API. Monthly and seasonal forecasts must show uncertainty when implemented.
- Historical editorial analytics in the sandbox are illustrative and not verified live data.
- Brand prompt is a specification, not an implementation of image creation.
