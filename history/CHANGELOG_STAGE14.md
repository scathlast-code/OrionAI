# Stage 14 — Privacy and connectivity mode gates (partial implementation)

Added `orion_modes.py`, SQLite user-specific mode preferences, `/mode` command and an entry-point gate in the Telegram conversational handler.

**Fail-closed:** `offline` and `orion_only` refuse cloud model calls because the local inference backend is NOT yet integrated. `online_required` refuses answers because a mandatory source-check pipeline is NOT yet integrated. `incognito` cannot be activated until persistence, Telegram transport, provider retention, and logging are audited. `normal` remains unchanged.

**Not yet implemented:** local small language model, OS-level network isolation, web verification, Mini App mode controls, zero-retention guarantees, guards for every alternative handler/background task. These modes are NOT production-complete. Never claim otherwise.
