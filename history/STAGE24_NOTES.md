# OrionAI Stage 24 — optional editorial profiles per topic

Added SQLite `topic_profiles` with optional `tone`, `voice`, `sources`, `direction`, `outcome` for each topic and subtopic. Subtopics inherit parent values unless overridden; clearing a field restores inheritance. Profiles are accessed through `Desk.topic_profile`, `Desk.save_topic_profile`, and `Desk.editorial_instructions`. Existing topics/tags and prior database records are retained.

## Current implementation boundary

The database and profile API are implemented and tested. **Telegram buttons, Mini App settings screens, and automatic injection into every news generation/search pathway are NOT wired in this stage.** No live bot or deployment test was performed. A future stage must expose secure editor-only settings UI and apply these instructions to search, scoring, drafting and Big News generation. A profile never overrides factual verification or editorial approval.
