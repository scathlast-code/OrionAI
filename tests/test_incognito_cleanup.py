import asyncio
from incognito_cleanup import EphemeralDeletionQueue, delete_messages

def test_queue_is_per_chat_and_user():
    q = EphemeralDeletionQueue()
    q.track(1, 10, 20)
    q.track(1, 11, 21)
    assert q.take(1, 10) == {20}
    assert q.take(1, 10) == set()
    assert q.take(1, 11) == {21}

def test_delete_reports_partial_failure():
    class Bot:
        async def delete_message(self, chat_id, message_id):
            if message_id == 2:
                raise PermissionError('no rights')
    result = asyncio.run(delete_messages(Bot(), 1, [1, 2, 1]))
    assert result['deleted'] == [1]
    assert result['failed'] == [{'message_id': 2, 'error_type': 'PermissionError'}]
