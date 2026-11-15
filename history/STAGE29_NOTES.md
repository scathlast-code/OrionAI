# Stage 29 — UTM short links and first-party analytics (additive)

Built on full Stage 28 archive. No existing files deleted. New `plugins/newsroom/utm_tracking.py`, `tests/test_utm_tracking_stage29.py`; additions to `utm.py` and newsroom HTTP routes.

## Available
- Existing `build_url` and `Ledger.create` remain unchanged. New `create_both(ledger,url,tag,platform,short_base)` creates BOTH full and short URLs and preserves duplicate warnings.
- Set `ORION_SHORT_BASE=https://go.your-domain.example` and point this HTTPS domain at the existing OrionAI web service. Public `GET /r/{code}` redirects to the full UTM URL, recording clicks in `utm_tracking.sqlite`. This route is registered only if ORION_SHORT_BASE is set.
- `ShortLinks.report(code=...,days=30)` returns real counts grouped by social platform (UTM source), device class, OS, browser, country, city and referrer host; bot-like user agents excluded from the human-click total. Reports are a Python API, not yet a Telegram/Mini App screen.
- Clicks are recorded without saving raw IP addresses or full user agents. Country/city are `unknown` until a consent-appropriate trusted server-side GeoIP enrichment is configured. Client-supplied geo headers are intentionally not trusted. No claim of unique visitors or confirmed site visits.
- No GPT/OpenAI/Orion prefixes are added to UTM parameters or short codes. Short URLs use random opaque codes.
- Disabling a short link is supported. No third-party shortener or monthly service is required, but a public HTTPS domain and server are necessary.

## Not yet connected
- The existing newsroom Telegram/Mini App does not yet expose a full UTM generator UI; `create_both` is callable from Python. Stats dashboards, Excel/Word exports for clicks, city GeoIP database, authenticated admin endpoints, and click deduplication are follow-on work.
- An HTTP route exists only if a Starlette host actually registers newsroom plugin routes and ORION_SHORT_BASE is configured. No live deployment was performed.
- Referrer headers are often omitted by social apps; `platform` records which channel a link was created for, not independent proof of the app from which a click originated. Use separate short links per platform.
- Consider privacy notice, retention settings and applicable data-protection rules before production use.
