import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SCRIPT = r'''
import json, bot
chat = 1
bot.init_db()
# користувач зі старим збереженим меню (до плагінів)
old_groups = {k: v for k, v in bot.DEFAULT_GROUPS.items() if k != "plugins"}
old_layout = [x for x in bot.DEFAULT_LAYOUT if x[0] not in ("g:plugins", "news", "stk", "plg")]
bot.set_pref(chat, "groups", old_groups)
bot.set_pref(chat, "layout", old_layout)
bot.set_pref(chat, "compact_menu_v2", True)
bot.set_pref(chat, "accordion_menu_v4", True)
lay = bot.layout(chat)
ids = [i for i, _ in lay]
print(json.dumps({
  "order": ids[:3],
  "places": {i: p for i, p in lay if i in ("news", "stk", "plg", "g:plugins")},
  "view": [bot.is_view("m:pl"), bot.is_view("m:pl:news:d:4"), bot.is_view("m:pl:news:ok:4")],
  "group": bot.L.groups(chat).get("plugins"),
}))
'''


def test_menu_migration_adds_plugins_category(tmp_path):
    env = dict(os.environ, TELEGRAM_TOKEN="", MCP_SECRET="x" * 20, ALLOWED_USERS="1", DB_PATH=str(tmp_path / "bot.db"))
    out = subprocess.run([sys.executable, "-c", SCRIPT], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    res = json.loads(out.stdout.strip().splitlines()[-1])
    assert res["order"][:2] == ["g:work", "g:plugins"]
    assert res["places"] == {"g:plugins": "home", "news": "g:plugins", "stk": "g:plugins", "plg": "g:settings"}
    assert res["view"] == [True, True, False]
    assert res["group"] == "📰 Медіа"
