import asyncio
import io
import json
import types
import zipfile

import httpx

from plugins import Host
from plugins.newsroom import Plugin as News
from plugins.newsroom.collectors import WebCollector
from plugins.newsroom.lists import parse_file, parse_numbers, parse_text


def run(c):
    return asyncio.run(c)


def news(tmp_path):
    async def ai(prompt, system=None, label=None, image=None):
        return json.dumps({"headline": "Заголовок", "body": "Текст", "region": "Київ", "skip": False}), "Fake"
    sent = []

    async def notify(chat, text, rows=None):
        sent.append((chat, text, rows))
    h = Host(data_dir=tmp_path, admins=lambda: [1], is_admin=lambda u: u == 1, ask_ai=ai)
    h.notify = notify
    h.notify_admins = lambda text, rows=None: notify(1, text, rows)
    p = News(h)
    return p, sent


SAMPLE = """# мої джерела
## Області
@kyiv_oda | КМВА | Київ
https://t.me/lviv_oda | Львівська ОВА | Львівська
t.me/+InviteHash123 | Закритий чат
-1001234567890 | Група
сміття без посилання
## Екологія
@greenpostua | GreenPost | | так
@eco_news_ua | | | ні
"""


def make_xlsx(rows):
    def cell(ref, v):
        return f'<c r="{ref}" t="inlineStr"><is><t>{v}</t></is></c>'
    body = "".join("<row>" + "".join(cell(chr(65 + j) + str(i + 1), v) for j, v in enumerate(r)) + "</row>" for i, r in enumerate(rows))
    sheet = f'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>{body}</sheetData></worksheet>'
    wb = '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheets><sheet name="Канали"/></sheets></workbook>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/workbook.xml", wb)
        z.writestr("xl/worksheets/sheet1.xml", sheet)
    return buf.getvalue()


def test_parse_formats():
    groups, errors = parse_text(SAMPLE)
    assert [g[0] for g in groups] == ["Області", "Екологія"]
    assert [e["ref"] for e in groups[0][1]] == ["kyiv_oda", "lviv_oda", "+InviteHash123", "-1001234567890"]
    assert groups[1][1][1]["enabled"] == 0 and len(errors) == 1
    g, _ = parse_file("a.xlsx", make_xlsx([["Посилання", "Назва", "Область"], ["@odesa_news", "Одеса", "Одеська"], ["t.me/kharkiv_x", "", ""]]))
    assert g[0][0] == "Канали" and g[0][1][0] == {"kind": "public", "ref": "odesa_news", "label": "Одеса", "region": "Одеська", "enabled": 1}
    g, _ = parse_file("a.csv", "посилання;назва\n@one_chan;Один\n@two_chan;Два\n".encode("cp1251"))
    assert [e["label"] for e in g[0][1]] == ["Один", "Два"]
    g, _ = parse_file("a.json", json.dumps({"Мій": ["@abcd_e", {"ref": "t.me/fghi_j", "region": "Сумська"}]}).encode())
    assert g[0][1][1]["region"] == "Сумська"
    assert parse_numbers("3, 5-7 ,10") == [3, 5, 6, 7, 10]


def test_import_edit_sort_bulk(tmp_path):
    p, sent = news(tmp_path)
    L = p.lists
    groups, errors = parse_text(SAMPLE)
    toast, screen = run(p.import_groups(1, groups, errors, 0, "add", "текст"))
    ls = L.lists()
    assert [l["name"] for l in ls] == ["Області", "Екологія"] and ls[0]["n"] == 4
    a = ls[0]["id"]
    # повторний імпорт не дублює, а оновлює
    run(p.import_groups(1, parse_text("@kyiv_oda | Київ МВА")[0], [], a, "add", "текст"))
    assert L.get_list(a)["n"] == 4 and L.entries(a)[0]["label"] == "Київ МВА"
    L.sort(a, "az")
    assert [e["ref"] for e in L.entries(a)][0] == "-1001234567890"
    e = L.entries(a)[-1]
    L.reorder(e["id"], "top")
    assert L.entries(a)[0]["id"] == e["id"] and L.entries(a)[0]["position"] == 1
    L.place(e["id"], 3)
    assert L.entries(a)[2]["id"] == e["id"]
    assert L.set_enabled_positions(a, "1-2", False) == 2 and L.get_list(a)["n_on"] == 2
    assert L.delete_positions(a, "1") == 1 and L.get_list(a)["n"] == 3
    b = ls[1]["id"]
    assert L.move_entry(L.entries(a)[0]["id"], b) and L.get_list(b)["n"] == 3
    L.merge_lists(b, a)
    assert len(L.lists()) == 1 and L.get_list(a)["n"] == 5
    run(p.import_groups(1, parse_text("@only_one_x\n@kyiv_oda")[0], [], a, "replace", "текст"))
    assert [e["ref"] for e in L.entries(a)] == ["only_one_x", "kyiv_oda"]
    assert "only_one_x" in L.export_csv().decode("utf-8-sig") and "## " in L.export_txt().decode()


def test_ui_screens_render(tmp_path):
    p, sent = news(tmp_path)
    run(p.import_groups(1, *parse_text(SAMPLE), 0, "add", "текст"))
    lid = p.lists.lists()[0]["id"]
    eid = p.lists.entries(lid)[0]["id"]
    for args in (["L"], ["Lo", str(lid)], ["Ls", str(lid)], ["Lsort", str(lid)], ["Lbulk", str(lid)], ["Le", str(eid)],
                 ["Lacc"], ["Lhelp"], ["Elst", str(eid)], ["Lmerge", str(lid)], ["Emv", str(eid), "down"], ["Etog", str(eid)],
                 ["Emeth", str(eid)], ["Lsortv", str(lid), "region"], ["Liv", str(lid), "1800"]):
        toast, screen = run(p.route(None, None, 1, args))
        assert screen and screen[0] and all(len(d.encode()) <= 64 for row in screen[1] for _t, d in row if not d.startswith("url:"))
    assert p.lists.get_list(lid)["interval"] == 1800


PAGE1 = """<meta property="og:title" content="Тест канал"><div class="tgme_channel_info"></div>
<div data-post="test_chan/10"><div class="tgme_widget_message_text js-message_text">Старий допис про подію в Києві</div></div>"""
PAGE2 = PAGE1 + """<div data-post="test_chan/11"><div class="tgme_widget_message_text js-message_text">Нова важлива новина про відкриття моста в Житомирі сьогодні</div></div>
<div data-post="test_chan/12"><div class="tgme_widget_message_text js-message_text">Розіграш призів! реклама</div></div>"""


def test_web_collector_baseline_then_new(tmp_path):
    p, sent = news(tmp_path)
    pages = [PAGE1, PAGE2]
    p.web.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=pages[0])))
    lid = p.lists.create_list("Тест")
    p.lists.update_list(lid, exclude="реклама")
    p.lists.add_entries(lid, parse_text("@test_chan | Тест | Житомирська")[0][0][1])
    n, errs = run(p.check_sources(force=True))
    e = p.lists.entries(lid)[0]
    assert n == 0 and errs == 0 and e["last_id"] == 10 and e["via"] == "web" and e["title"] == "Тест канал"
    pages[0] = PAGE2
    n, errs = run(p.check_sources(force=True))
    e = p.lists.entries(lid)[0]
    assert n == 1 and e["taken"] == 1 and e["filtered"] == 1 and e["last_id"] == 12
    item = p.store.item(1)
    assert item["origin"] == "channel" and item["hint"] == "Житомирська" and item["source_url"] == "https://t.me/test_chan/11"
    n, _ = run(p.check_sources(force=True))
    assert n == 0  # повтор не береться


def test_private_without_account_reports_clearly(tmp_path):
    p, _ = news(tmp_path)
    lid = p.lists.create_list("Закриті")
    p.lists.add_entries(lid, parse_text("t.me/+InviteHash123")[0][0][1])
    run(p.check_sources(force=True))
    assert "службовий акаунт" in p.lists.entries(lid)[0]["last_error"]


def test_bot_group_autolist_and_take(tmp_path):
    p, sent = news(tmp_path)

    def upd(text, mid):
        chat = types.SimpleNamespace(id=-100777, type="supergroup", username=None, title="Чат громади")
        msg = types.SimpleNamespace(chat=chat, text=text, caption=None, photo=None, message_id=mid, entities=[], caption_entities=[],
                                    from_user=types.SimpleNamespace(is_bot=False))
        return types.SimpleNamespace(channel_post=None, message=msg)
    run(p.on_channel_post(upd("Перше повідомлення групи, яке ще не моніториться", 1), None))
    l = p.lists.lists()[0]
    assert l["auto"] == "bot" and l["n"] == 1 and l["n_on"] == 0 and "Моніторити" in str(sent[-1][2])
    e = p.lists.entries(l["id"])[0]
    run(p.route(None, None, 1, ["Etog", str(e["id"])]))
    run(p.on_channel_post(upd("Коротко", 2), None))  # коротше за мінімум для груп (60)
    run(p.on_channel_post(upd("У громаді сьогодні відкрили новий центр надання адміністративних послуг для мешканців", 3), None))
    e = p.lists.entries(l["id"])[0]
    assert e["taken"] == 1 and e["filtered"] == 1


def test_account_messages_autolist(tmp_path):
    p, _ = news(tmp_path)
    run(p._on_mt("-100555", "news_chan", "Новини", "channel", 5, "Повідомлення з підписки акаунта про подію в Полтаві", ""))
    l = [x for x in p.lists.lists() if x["auto"] == "account"][0]
    e = p.lists.entries(l["id"])[0]
    assert e["ref"] == "news_chan" and e["chat_id"] == "-100555" and e["taken"] == 1
    p.lists.update_entry(e["id"], enabled=0)
    run(p._on_mt("-100555", "news_chan", "Новини", "channel", 6, "Ще одна новина з вимкненого джерела про Полтаву", ""))
    assert p.lists.entries(l["id"])[0]["taken"] == 1
