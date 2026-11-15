# Incognito deletion requirement — partial implementation

User options: (1) delete incoming Telegram message after it has been safely parsed and processed, or (2) delete all incoming and bot-generated session messages when the incognito session closes. Deletion should be attempted on both session close and timeout, with clear failure feedback. Do not write message bodies to databases, caches, application logs, analytics, backups, third-party AI, or tracing systems.

`incognito_cleanup.py` provides a RAM-only queue and a Telegram Bot API deletion executor with per-message failure reporting. It is not wired to live bot processing yet. The `/mode incognito` gate remains disabled deliberately because the existing bot has other storage and logging paths that need auditing. Do not claim no-retention or automatic deletion is active.

Telegram API deletion is subject to permissions, time limits and platform retention; it cannot guarantee erasure from Telegram infrastructure, recipient devices or backups. In group chats permissions differ. Telegram transport itself remains network-based.
