import asyncio
from types import SimpleNamespace as N

from plugins.stickers import builder as B


def st(uid, kind="regular", anim=False):
    return N(file_unique_id=uid, file_id="f" + uid, type=kind, is_video=False, is_animated=anim, emoji="😀")


class Bot:
    username = "ai_orion_7bot"

    def __init__(self):
        self.sets = {}

    async def get_sticker_set(self, name):
        if name in self.sets:
            ss = self.sets[name]
            return N(name=name, title=ss["title"], stickers=ss["st"], sticker_type=ss["kind"])
        title = "Ватні стікери" if name == "bad" else "Котики"
        return N(name=name, title=title, stickers=[st(name + str(i)) for i in range(3)] + [st("shared")], sticker_type="regular")

    async def create_new_sticker_set(self, uid, name, title, stickers, sticker_type=None):
        self.sets[name] = {"title": title, "kind": sticker_type, "st": [st(s.sticker[1:]) for s in stickers]}
        return True

    async def add_sticker_to_set(self, uid, name, s):
        self.sets[name]["st"].append(st(s.sticker[1:]))
        return True

    async def delete_sticker_from_set(self, fid):
        for v in self.sets.values():
            v["st"] = [x for x in v["st"] if x.file_id != fid] + [x for x in v["st"] if x.file_id == fid][1:]
        return True


def test_merge_dedup_block_and_add():
    bot = Bot()
    got, skipped = asyncio.run(B.pack_stickers(bot, ["cats", "dogs", "bad"]))
    assert len(got) == 8 and skipped and "не беремо" in skipped[0]
    name, kind, added, sk = asyncio.run(B.create(bot, 1, "Мої котики", got + [st("e1", "custom_emoji")], bot.username))
    assert name.endswith("_by_ai_orion_7bot") and name.startswith("moi_kotyky") and kind == "regular" and added == 7 and sk == 1
    a, s = asyncio.run(B.add(bot, 1, name, [st("new1"), st("shared")]))
    assert a == 1 and s == 1
    assert B.blocked("Стікери ZOV") and not B.blocked("Українські котики")
