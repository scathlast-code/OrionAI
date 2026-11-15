# OrionAI: sticker editor in Telegram and Mini App

Telegram bot: `/stickers <links>` creates a draft and provides an inline keyboard with per-item toggles, pagination, select all/none, submit, approve, reject. `/stickerlist <id>` reopens the keyboard. All editing is owner-scoped in SQLite.

Mini App: run `uvicorn sticker_miniapp:app --host 127.0.0.1 --port 8090` from the project root, expose only behind an HTTPS reverse proxy, then configure a BotFather Mini App URL. Open `/sticker_webapp` **at the service root** (the service serves it at `/`); enter the draft ID. The Mini App authenticates Telegram `initData` via HMAC and checks expiration (one hour) and `ALLOWED_USERS` when configured. It uses the same `ORION_STICKER_STORAGE` as the Telegram bot. Ensure both services share the same persistent volume; otherwise drafts will not appear in both.

Required Python dependencies: `fastapi`, `uvicorn`, `pydantic` (already needed by the main project). Do not expose the service without HTTPS or with a publicly accessible writable shared storage directory.

**Not implemented:** Telegram sticker-set publishing (createNewStickerSet/addStickerToSet) and image previews in Mini App. The Mini App currently lists original filenames and emojis; approving a draft does not create a published Telegram sticker set. Deployment and live Telegram API calls were not tested.
