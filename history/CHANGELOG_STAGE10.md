# Stage 10 — notification and sleep configuration

- Added per-bot SQLite notification and sleep configuration.
- Added five sound presets and five vibration presets as stored Mini App preferences (playback not implemented).
- Added /notifications /sound /vibration /quiet /sleep /wake for both Telegram bots.
- Added early Telegram deep-sleep update guard and administrator wake mechanism.
- Added tests for persistence, independent bots, quiet hours across midnight and input validation.

## Known limitations
- Quiet-hour flag is not yet applied automatically to all outgoing Telegram messages.
- Deep sleep does not stop all background tasks, existing jobs, HTTP endpoints or server process.
- Mini App playback and notification settings UI are not yet wired.
- No live Telegram, Railway or Oracle deployment verification.
