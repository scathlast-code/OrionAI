# Stage 4 — OrionAI sticker selection and approval

- Sticker importer integrated into OrionAI Telegram bot (`sticker_archive.py`).
- Persistent draft store (`sticker_drafts.py`), selective inclusion/exclusion, explicit submit/approve/reject.
- Selection ZIP preserves original files and produces a separate approved selection; original import is not deleted.
- Access limited to the originating Telegram user ID.
- Automated local test: `tests/test_sticker_drafts.py`.
- Remaining: actual publication of Telegram sticker/custom emoji sets; authenticated Mini App frontend/API; live Oracle/Telegram deployment tests.
