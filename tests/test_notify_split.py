import asyncio
import re

from plugins import Host


class Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat, text, **kw):
        opened = len(re.findall(r"<blockquote", text))
        if kw.get("parse_mode") and opened != text.count("</blockquote>"):
            raise ValueError("Can't parse entities: unclosed start tag")
        self.sent.append((text, kw))
        return len(self.sent)


def test_long_morning_digest_arrives_in_parts(tmp_path):
    bot = Bot()
    h = Host(data_dir=tmp_path, admins=lambda: [1], is_admin=lambda u: True, is_allowed=lambda u: True, bot=lambda: bot)
    text = "🌅 <b>Теми дня</b>\n\n<blockquote expandable>" + "\n".join(f"день {i} <a href='https://x.ua/{i}'>джерело</a>" for i in range(500)) + "</blockquote>"
    asyncio.run(h.notify(1, text, [[("Оновити", "m:x")]]))
    assert len(bot.sent) >= 2 and all(kw.get("parse_mode") == "HTML" for _t, kw in bot.sent)
    assert bot.sent[-1][1]["reply_markup"] is not None and bot.sent[0][1]["reply_markup"] is None
    assert "день 499" in bot.sent[-1][0]
