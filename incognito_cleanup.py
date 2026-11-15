"""Telegram deletion support for a future fail-closed incognito mode.

No message contents or IDs are persisted by this module. This is not a
complete no-retention guarantee: Telegram and external processors are separate.
"""
from dataclasses import dataclass, field
from typing import Dict, Set, Tuple

@dataclass
class EphemeralDeletionQueue:
    pending: Dict[Tuple[int, int], Set[int]] = field(default_factory=dict)

    def track(self, chat_id: int, user_id: int, message_id: int) -> None:
        self.pending.setdefault((chat_id, user_id), set()).add(message_id)

    def take(self, chat_id: int, user_id: int) -> Set[int]:
        return self.pending.pop((chat_id, user_id), set())

async def delete_messages(bot, chat_id: int, message_ids) -> dict:
    """Best-effort delete; report failures, never claim remote erasure."""
    deleted, failed = [], []
    for message_id in sorted(set(message_ids)):
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
            deleted.append(message_id)
        except Exception as exc:
            failed.append({'message_id': message_id, 'error_type': type(exc).__name__})
    return {'deleted': deleted, 'failed': failed}
