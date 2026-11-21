"""📋 Списки джерел новинарні: канали й чати, з яких бот сам бере новини.

Списки можна завантажити файлом (TXT, CSV, XLSX, JSON) або вставити текстом, а потім редагувати:
перейменовувати, вмикати/вимикати, сортувати (вручну й автоматично), переносити між списками,
видаляти по одному й масово, вивантажувати назад у CSV.
"""
from __future__ import annotations

import csv
import io
import json
import re
import time
import zipfile
import xml.etree.ElementTree as ET
from contextlib import closing

SCHEMA = """
CREATE TABLE IF NOT EXISTS src_lists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    interval INTEGER NOT NULL DEFAULT 600,     -- як часто перевіряти кожне джерело, с
    include TEXT NOT NULL DEFAULT '',          -- брати лише дописи з цими словами (через кому)
    exclude TEXT NOT NULL DEFAULT '',          -- пропускати дописи з цими словами
    region TEXT NOT NULL DEFAULT '',           -- типова область для всіх джерел списку
    min_len INTEGER NOT NULL DEFAULT 0,        -- коротші дописи пропускати (для балакучих груп)
    auto TEXT NOT NULL DEFAULT '',             -- '' — ваш список; account — підписки акаунта; bot — де є бот
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS src_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    list_id INTEGER NOT NULL,
    ref TEXT NOT NULL,                         -- username (малими) | +invite | -100… id | x:user, ig:user… (соцмережі)
    chat_id TEXT NOT NULL DEFAULT '',          -- числовий id, коли вже відомий (для зіставлення)
    chat_type TEXT NOT NULL DEFAULT '',        -- channel | group | chat
    kind TEXT NOT NULL,                        -- public | invite | id | social
    method TEXT NOT NULL DEFAULT 'auto',       -- auto | web | mtproto | bot
    title TEXT NOT NULL DEFAULT '',
    label TEXT NOT NULL DEFAULT '',
    region TEXT NOT NULL DEFAULT '',
    position INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_id INTEGER NOT NULL DEFAULT 0,
    last_check REAL NOT NULL DEFAULT 0,
    last_post REAL NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    via TEXT NOT NULL DEFAULT '',
    taken INTEGER NOT NULL DEFAULT 0,
    filtered INTEGER NOT NULL DEFAULT 0,
    added REAL NOT NULL,
    UNIQUE(list_id, ref)
);
CREATE INDEX IF NOT EXISTS src_entries_list ON src_entries(list_id, position);
"""

USER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
LINK_RE = re.compile(r"(?i)(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/(\S+)")
RESERVED = {"addstickers", "addemoji", "joinchat", "share", "proxy", "socks", "iv", "login", "setlanguage", "addtheme", "c", "s"}

SORTS = {
    "az": ("🔤 За назвою А–Я", lambda e: (e["label"] or e["title"] or e["ref"]).lower()),
    "za": ("🔤 За назвою Я–А", None),
    "region": ("🗺 За областю", lambda e: ((e["region"] or "яяя").lower(), (e["label"] or e["title"] or e["ref"]).lower())),
    "taken": ("📈 Найактивніші вгорі", lambda e: (-e["taken"], (e["label"] or e["ref"]).lower())),
    "fresh": ("🕘 Свіжі дописи вгорі", lambda e: -e["last_post"]),
    "added": ("🆕 Нові в списку вгорі", lambda e: -e["added"]),
    "broken": ("⚠️ Проблемні вгорі", lambda e: (0 if e["last_error"] else 1, (e["label"] or e["ref"]).lower())),
    "off": ("⚪️ Вимкнені вниз", lambda e: (0 if e["enabled"] else 1, e["position"])),
    "rev": ("🔁 Перевернути порядок", None),
}


# ==================================================================== розбір посилань
def normalize_ref(raw: str):
    """-> (kind, ref) або None. kind: public | invite | id."""
    s = (raw or "").strip().strip("<>()[]{}\"'«»,;")
    if not s:
        return None
    from . import social
    if not LINK_RE.search(s):
        so = social.parse(s)
        if so:
            return so
    m = LINK_RE.search(s)
    if m:
        path = m.group(1).split("?")[0].strip("/")
        parts = path.split("/")
        if parts[0].startswith("+"):
            return ("invite", "+" + parts[0][1:]) if len(parts[0]) > 5 else None
        if parts[0].lower() == "joinchat" and len(parts) > 1:
            return "invite", "+" + parts[1]
        if parts[0].lower() == "c" and len(parts) > 1 and parts[1].isdigit():
            return "id", "-100" + parts[1]
        if parts[0].lower() == "s" and len(parts) > 1:
            parts = parts[1:]
        name = parts[0]
        if USER_RE.match(name) and name.lower() not in RESERVED:
            return "public", name.lower()
        return None
    if s.startswith("@") and USER_RE.match(s[1:]):
        return "public", s[1:].lower()
    if re.fullmatch(r"-?\d{5,20}", s):
        n = s if s.startswith("-") else ("-100" + s if len(s) >= 9 else None)
        return ("id", n) if n else None
    if USER_RE.match(s) and s.lower() not in RESERVED and not s.isdigit():
        return "public", s.lower()
    return None


def display_ref(kind, ref):
    if kind == "social":
        from . import social
        return social.display(ref)
    return f"@{ref}" if kind == "public" else (f"t.me/{ref}" if kind == "invite" else ref)


def link_of(kind, ref):
    if kind == "social":
        from . import social
        return social.link(ref)
    if kind == "public":
        return f"https://t.me/{ref}"
    if kind == "invite":
        return f"https://t.me/{ref}"
    return ""


YES = {"1", "так", "yes", "y", "true", "on", "увімк", "+", "✅", "да"}
NO = {"0", "ні", "no", "n", "false", "off", "вимк", "-", "❌", "нет"}

HEADER_KEYS = {
    "ref": ("посилання", "канал", "чат", "ref", "link", "url", "username", "джерело", "адреса", "id"),
    "label": ("назва", "label", "name", "title", "підпис", "ім'я"),
    "region": ("регіон", "область", "region", "oblast", "місто"),
    "enabled": ("увімк", "enabled", "активний", "on", "стан", "статус"),
}


def _header_map(row):
    low = [str(c or "").strip().lower() for c in row]
    found = {}
    for key, names in HEADER_KEYS.items():
        for i, c in enumerate(low):
            if c and any(c.startswith(n) for n in names) and i not in found.values():
                found[key] = i
                break
    return found if "ref" in found else None


def _row_entry(cells, hmap=None):
    cells = [str(c if c is not None else "").strip() for c in cells]
    cells = [c for c in cells] if hmap else [c for c in cells if c]
    if not any(cells):
        return None
    if hmap:
        get = lambda k: cells[hmap[k]] if k in hmap and hmap[k] < len(cells) else ""
        norm = normalize_ref(get("ref"))
        if not norm:
            return {"error": get("ref") or " ".join(cells)[:60]}
        en = get("enabled").lower()
        return {"kind": norm[0], "ref": norm[1], "label": get("label")[:80], "region": get("region")[:60],
                "enabled": 0 if en in NO else 1}
    # без заголовка: шукаємо посилання в будь-якій клітинці, решта — назва / область / стан
    idx, norm = None, None
    rest = ""
    for i, c in enumerate(cells):
        for token in c.split() + [c]:
            norm = normalize_ref(token)
            if norm:
                idx = i
                rest = c.replace(token, "", 1).strip(" -—–:|;,")
                break
        if norm:
            break
    if not norm:
        return {"error": " ".join(cells)[:60]}
    others = [x for j, x in enumerate(cells) if j != idx]
    if rest:
        others.insert(0, rest)
    enabled = 1
    if others and others[-1].lower() in YES | NO:
        enabled = 0 if others.pop().lower() in NO else 1
    return {"kind": norm[0], "ref": norm[1], "label": (others[0] if others else "")[:80],
            "region": (others[1] if len(others) > 1 else "")[:60], "enabled": enabled}


def parse_text(text: str):
    """Текст -> [(назва списку або None, [записи])], помилки. «## Назва» або «[Назва]» починає новий список."""
    groups, cur, errors = [], (None, []), []
    lines = (text or "").replace("\r", "").split("\n")
    sample = "\n".join(l for l in lines[:20] if l.strip())
    delim = None
    if sample.count(";") >= 2 or sample.count("\t") >= 2:
        delim = ";" if sample.count(";") >= sample.count("\t") else "\t"
    elif sample.count(",") >= 2 and not sample.count("|"):
        delim = ","
    hmap = None
    for n, line in enumerate(lines, 1):
        s = line.strip()
        if not s or (s.startswith("#") and not s.startswith("##")) or s.startswith("//"):
            continue
        head = re.match(r"^(?:##\s*(.+)|\[(.+)\]|Список\s*:\s*(.+))$", s, re.I)
        if head:
            if cur[1] or cur[0]:
                groups.append(cur)
            cur = ((head.group(1) or head.group(2) or head.group(3)).strip()[:60], [])
            hmap = None
            continue
        if "|" in s:
            cells = s.split("|")
        elif delim:
            cells = next(csv.reader([s], delimiter=delim))
        else:
            cells = [s]
        if hmap is None and n <= 3 and _header_map(cells) and not any(normalize_ref(c) for c in cells if c.strip().startswith(("@", "http", "t.me"))):
            hmap = _header_map(cells)
            continue
        e = _row_entry(cells, hmap)
        if not e:
            continue
        if "error" in e:
            errors.append(f"рядок {n}: «{e['error']}» — не схоже на канал чи чат")
        else:
            cur[1].append(e)
    if cur[1] or cur[0]:
        groups.append(cur)
    return groups, errors


def xlsx_rows(data: bytes):
    """Мінімальне читання XLSX без сторонніх бібліотек: усі аркуші, значення клітинок -> [(аркуш, рядки)]."""
    out = []
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        if sum(z.getinfo(n).file_size for n in names) > 30 * 1024 * 1024:
            raise ValueError("таблиця завелика")
        shared = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.findall("m:si", ns):
                shared.append("".join(t.text or "" for t in si.iter("{%s}t" % ns["m"])))
        titles = {}
        if "xl/workbook.xml" in names:
            wb = ET.fromstring(z.read("xl/workbook.xml"))
            for i, sh in enumerate(wb.iter("{%s}sheet" % ns["m"]), 1):
                titles[f"xl/worksheets/sheet{i}.xml"] = sh.attrib.get("name", f"Аркуш {i}")
        sheets = sorted((n for n in names if re.match(r"xl/worksheets/sheet\d+\.xml$", n)),
                        key=lambda x: int(re.findall(r"\d+", x)[-1]))
        for sh in sheets:
            root = ET.fromstring(z.read(sh))
            rows = []
            for row in root.iter("{%s}row" % ns["m"]):
                cells = {}
                for c in row.findall("m:c", ns):
                    ref = c.attrib.get("r", "")
                    col = 0
                    for ch in re.match(r"[A-Z]*", ref).group(0):
                        col = col * 26 + ord(ch) - 64
                    t = c.attrib.get("t")
                    v = c.find("m:v", ns)
                    if t == "s" and v is not None:
                        val = shared[int(v.text)]
                    elif t == "inlineStr":
                        val = "".join(x.text or "" for x in c.iter("{%s}t" % ns["m"]))
                    else:
                        val = v.text if v is not None else ""
                    cells[max(col - 1, 0)] = val
                if cells:
                    rows.append([cells.get(i, "") for i in range(max(cells) + 1)])
            out.append((titles.get(sh, sh), rows))
    return out


def parse_file(filename: str, data: bytes):
    """Файл -> ([(назва списку, записи)], помилки). Підтримка: .txt .csv .tsv .xlsx .json"""
    name = (filename or "").lower()
    if len(data) > 5 * 1024 * 1024:
        raise ValueError("файл більший за 5 МБ")
    if name.endswith(".xlsx"):
        groups, errors = [], []
        for title, rows in xlsx_rows(data):
            hmap = _header_map(rows[0]) if rows else None
            items = []
            for n, r in enumerate(rows[1:] if hmap else rows, 2 if hmap else 1):
                e = _row_entry(r, hmap)
                if not e:
                    continue
                if "error" in e:
                    errors.append(f"{title}, рядок {n}: «{e['error']}»")
                else:
                    items.append(e)
            if items:
                groups.append((title, items))
        return groups, errors
    if name.endswith(".json"):
        obj = json.loads(data.decode("utf-8-sig"))
        groups, errors = [], []
        blocks = obj.items() if isinstance(obj, dict) else [(None, obj)]
        for title, items in blocks:
            out = []
            for it in items if isinstance(items, list) else []:
                if isinstance(it, str):
                    it = {"ref": it}
                norm = normalize_ref(str(it.get("ref") or it.get("link") or it.get("username") or ""))
                if not norm:
                    errors.append(f"«{str(it)[:50]}»")
                    continue
                out.append({"kind": norm[0], "ref": norm[1], "label": str(it.get("label") or it.get("name") or "")[:80],
                            "region": str(it.get("region") or "")[:60], "enabled": 0 if str(it.get("enabled", 1)).lower() in NO else 1})
            groups.append((title, out))
        return groups, errors
    for enc in ("utf-8-sig", "cp1251"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("не вдалося прочитати кодування файлу")
    if name.endswith(".tsv"):
        text = text.replace("\t", ";")
    return parse_text(text)


# ==================================================================== сховище
class Lists:
    def __init__(self, store):
        self.store = store
        with closing(store.db()) as con, con:
            con.executescript(SCHEMA)

    def db(self):
        return self.store.db()

    # ----- списки
    def lists(self):
        with closing(self.db()) as con:
            rows = con.execute(
                "SELECT l.*, COUNT(e.id) AS n, COALESCE(SUM(e.enabled),0) AS n_on, COALESCE(SUM(e.taken),0) AS taken,"
                " COALESCE(SUM(CASE WHEN e.last_error!='' THEN 1 ELSE 0 END),0) AS n_err"
                " FROM src_lists l LEFT JOIN src_entries e ON e.list_id=l.id GROUP BY l.id ORDER BY l.position, l.id")
            return [dict(r) for r in rows]

    def get_list(self, lid):
        return next((x for x in self.lists() if x["id"] == int(lid)), None)

    def find_list(self, name):
        return next((x for x in self.lists() if x["name"].lower() == (name or "").lower()), None)

    def create_list(self, name, region="", auto="", min_len=0, enabled=1):
        with closing(self.db()) as con, con:
            pos = con.execute("SELECT COALESCE(MAX(position),0)+1 FROM src_lists").fetchone()[0]
            cur = con.execute("INSERT INTO src_lists(name,position,region,auto,min_len,enabled,created) VALUES(?,?,?,?,?,?,?)",
                              ((name or "Список").strip()[:60], pos, region[:60], auto, int(min_len), int(enabled), time.time()))
            return cur.lastrowid

    def auto_list(self, auto):
        """Службовий список (account / bot), створюється за потреби."""
        found = next((x for x in self.lists() if x["auto"] == auto), None)
        if found:
            return found["id"]
        name = {"account": "📲 Підписки акаунта", "bot": "🤖 Де є бот", "ours": "🏠 Наші канали й чати"}.get(auto, auto)
        return self.create_list(name, auto=auto, min_len=60 if auto == "bot" else 0)

    def upsert_chat(self, lid, kind, ref, chat_id, title, chat_type, enabled):
        """Додає/оновлює джерело службового списку за chat_id. -> (id, чи нове)."""
        with closing(self.db()) as con, con:
            r = con.execute("SELECT id FROM src_entries WHERE list_id=? AND (chat_id=? OR ref=?)", (int(lid), str(chat_id), ref)).fetchone()
            if r:
                con.execute("UPDATE src_entries SET title=?, chat_id=?, chat_type=?, last_error=CASE WHEN last_error LIKE 'акаунт%' THEN '' ELSE last_error END WHERE id=?",
                            (title[:120], str(chat_id), chat_type, r["id"]))
                return r["id"], False
            pos = con.execute("SELECT COALESCE(MAX(position),0)+1 FROM src_entries WHERE list_id=?", (int(lid),)).fetchone()[0]
            cur = con.execute("INSERT INTO src_entries(list_id,ref,kind,chat_id,chat_type,title,position,enabled,added,method) VALUES(?,?,?,?,?,?,?,?,?,?)",
                              (int(lid), ref, kind, str(chat_id), chat_type, title[:120], pos, int(enabled), time.time(),
                               "mtproto" if kind != "bot" else "bot"))
            return cur.lastrowid, True

    def update_list(self, lid, **kw):
        cols = ", ".join(f"{k}=?" for k in kw)
        with closing(self.db()) as con, con:
            con.execute(f"UPDATE src_lists SET {cols} WHERE id=?", (*kw.values(), int(lid)))

    def delete_list(self, lid):
        with closing(self.db()) as con, con:
            con.execute("DELETE FROM src_entries WHERE list_id=?", (int(lid),))
            con.execute("DELETE FROM src_lists WHERE id=?", (int(lid),))

    def move_list(self, lid, how):
        ids = [x["id"] for x in self.lists()]
        _reorder(ids, int(lid), how)
        with closing(self.db()) as con, con:
            for pos, i in enumerate(ids, 1):
                con.execute("UPDATE src_lists SET position=? WHERE id=?", (pos, i))

    def merge_lists(self, src, dst):
        """Переносить усі джерела зі списку src у dst (повтори пропускає) і видаляє src."""
        moved = 0
        for e in self.entries(src):
            moved += 1 if self.move_entry(e["id"], dst) else 0
        self.delete_list(src)
        return moved

    # ----- джерела
    def entries(self, lid, offset=0, limit=None, only_on=False):
        q = "SELECT * FROM src_entries WHERE list_id=?" + (" AND enabled=1" if only_on else "") + " ORDER BY position, id"
        args = [int(lid)]
        if limit:
            q += " LIMIT ? OFFSET ?"
            args += [limit, offset]
        with closing(self.db()) as con:
            return [dict(r) for r in con.execute(q, args)]

    def entry(self, eid):
        with closing(self.db()) as con:
            r = con.execute("SELECT * FROM src_entries WHERE id=?", (int(eid),)).fetchone()
        return dict(r) if r else None

    def entries_by_ref(self, refs):
        refs = [str(r).lower() for r in refs if r]
        if not refs:
            return []
        with closing(self.db()) as con:
            q = ("SELECT e.*, l.name AS list_name, l.include, l.exclude, l.region AS list_region, l.min_len,"
                 " l.enabled AS list_on, l.auto FROM src_entries e"
                 " JOIN src_lists l ON l.id=e.list_id WHERE (lower(e.ref) IN ({0}) OR e.chat_id IN ({0}))").format(",".join("?" * len(refs)))
            return [dict(r) for r in con.execute(q, refs + refs)]

    def add_entries(self, lid, items, mode="add"):
        """mode: add — додати нові (наявні оновити назву/область, якщо задано); replace — список стає рівно таким."""
        added = updated = 0
        with closing(self.db()) as con, con:
            if mode == "replace":
                keep = {i["ref"] for i in items}
                for r in con.execute("SELECT id, ref FROM src_entries WHERE list_id=?", (int(lid),)).fetchall():
                    if r["ref"] not in keep:
                        con.execute("DELETE FROM src_entries WHERE id=?", (r["id"],))
            pos = con.execute("SELECT COALESCE(MAX(position),0) FROM src_entries WHERE list_id=?", (int(lid),)).fetchone()[0]
            for i in items:
                old = con.execute("SELECT id FROM src_entries WHERE list_id=? AND ref=?", (int(lid), i["ref"])).fetchone()
                if old:
                    sets = {k: i[k] for k in ("label", "region") if i.get(k)}
                    if mode == "replace":
                        sets["enabled"] = i.get("enabled", 1)
                    if sets:
                        con.execute("UPDATE src_entries SET " + ", ".join(f"{k}=?" for k in sets) + " WHERE id=?", (*sets.values(), old["id"]))
                        updated += 1
                    continue
                pos += 1
                con.execute("INSERT INTO src_entries(list_id,ref,kind,label,region,position,enabled,added) VALUES(?,?,?,?,?,?,?,?)",
                            (int(lid), i["ref"], i["kind"], i.get("label", ""), i.get("region", ""), pos, i.get("enabled", 1), time.time()))
                added += 1
            if mode == "replace":
                order = {i["ref"]: n for n, i in enumerate(items, 1)}
                for r in con.execute("SELECT id, ref FROM src_entries WHERE list_id=?", (int(lid),)).fetchall():
                    con.execute("UPDATE src_entries SET position=? WHERE id=?", (order.get(r["ref"], 10 ** 6), r["id"]))
        self.renumber(lid)
        return added, updated

    def update_entry(self, eid, **kw):
        cols = ", ".join(f"{k}=?" for k in kw)
        with closing(self.db()) as con, con:
            con.execute(f"UPDATE src_entries SET {cols} WHERE id=?", (*kw.values(), int(eid)))

    def delete_entries(self, ids):
        ids = [int(i) for i in ids]
        if not ids:
            return 0
        with closing(self.db()) as con, con:
            n = con.execute("DELETE FROM src_entries WHERE id IN (%s)" % ",".join("?" * len(ids)), ids).rowcount
        return n

    def delete_positions(self, lid, spec):
        """«3, 5-8, 12» -> видаляє джерела з цими номерами в поточному порядку списку."""
        nums = parse_numbers(spec)
        es = self.entries(lid)
        ids = [es[n - 1]["id"] for n in nums if 1 <= n <= len(es)]
        n = self.delete_entries(ids)
        self.renumber(lid)
        return n

    def set_enabled_positions(self, lid, spec, on):
        nums = parse_numbers(spec)
        es = self.entries(lid)
        for n in nums:
            if 1 <= n <= len(es):
                self.update_entry(es[n - 1]["id"], enabled=1 if on else 0)
        return len([n for n in nums if 1 <= n <= len(es)])

    def move_entry(self, eid, to_list):
        e = self.entry(eid)
        if not e or e["list_id"] == int(to_list):
            return False
        with closing(self.db()) as con, con:
            if con.execute("SELECT 1 FROM src_entries WHERE list_id=? AND ref=?", (int(to_list), e["ref"])).fetchone():
                con.execute("DELETE FROM src_entries WHERE id=?", (e["id"],))
                return False
            pos = con.execute("SELECT COALESCE(MAX(position),0)+1 FROM src_entries WHERE list_id=?", (int(to_list),)).fetchone()[0]
            con.execute("UPDATE src_entries SET list_id=?, position=? WHERE id=?", (int(to_list), pos, e["id"]))
        self.renumber(e["list_id"])
        return True

    def reorder(self, eid, how):
        e = self.entry(eid)
        ids = [x["id"] for x in self.entries(e["list_id"])]
        _reorder(ids, e["id"], how)
        self._save_order(ids)

    def place(self, eid, position):
        e = self.entry(eid)
        ids = [x["id"] for x in self.entries(e["list_id"])]
        ids.remove(e["id"])
        ids.insert(max(0, min(len(ids), int(position) - 1)), e["id"])
        self._save_order(ids)

    def sort(self, lid, how):
        es = self.entries(lid)
        if how == "rev":
            es.reverse()
        elif how == "za":
            es.sort(key=SORTS["az"][1], reverse=True)
        else:
            es.sort(key=SORTS[how][1])
        self._save_order([e["id"] for e in es])

    def renumber(self, lid):
        self._save_order([e["id"] for e in self.entries(lid)])

    def _save_order(self, ids):
        with closing(self.db()) as con, con:
            for pos, i in enumerate(ids, 1):
                con.execute("UPDATE src_entries SET position=? WHERE id=?", (pos, i))

    def cleanup(self, lid, what):
        """what: dups — однакові джерела в різних списках (лишається в першому); broken — з помилкою; off — вимкнені."""
        es = self.entries(lid)
        if what == "broken":
            return self.delete_entries([e["id"] for e in es if e["last_error"]])
        if what == "off":
            return self.delete_entries([e["id"] for e in es if not e["enabled"]])
        if what == "dups":
            mine = {e["ref"]: e["id"] for e in es}
            other = set()
            for l in self.lists():
                if l["id"] != int(lid) and l["position"] < (self.get_list(lid) or {}).get("position", 0):
                    other |= {e["ref"] for e in self.entries(l["id"])}
            return self.delete_entries([i for r, i in mine.items() if r in other])
        return 0

    def due(self, now=None):
        """Джерела, які пора перевірити (з увімкнених списків, за інтервалом списку)."""
        now = now or time.time()
        with closing(self.db()) as con:
            rows = con.execute(
                "SELECT e.*, l.name AS list_name, l.interval, l.include, l.exclude, l.region AS list_region, l.min_len, l.auto"
                " FROM src_entries e JOIN src_lists l ON l.id=e.list_id"
                " WHERE e.enabled=1 AND l.enabled=1 AND e.last_check + l.interval <= ? ORDER BY e.last_check", (now,))
            return [dict(r) for r in rows]

    def export_csv(self, lid=None) -> bytes:
        out = io.StringIO()
        w = csv.writer(out, delimiter=";")
        w.writerow(["список", "№", "посилання", "назва", "область", "увімк", "спосіб", "узято", "остання перевірка", "помилка"])
        for l in self.lists():
            if lid and l["id"] != int(lid):
                continue
            for e in self.entries(l["id"]):
                w.writerow([l["name"], e["position"], display_ref(e["kind"], e["ref"]), e["label"] or e["title"], e["region"],
                            "так" if e["enabled"] else "ні", e["via"] or e["method"], e["taken"],
                            time.strftime("%d.%m.%Y %H:%M", time.localtime(e["last_check"])) if e["last_check"] else "",
                            e["last_error"]])
        return ("﻿" + out.getvalue()).encode("utf-8")  # BOM — щоб Excel одразу показав кирилицю

    def export_txt(self) -> bytes:
        lines = ["# Списки джерел новинарні. Формат: посилання | назва | область | так/ні", ""]
        for l in self.lists():
            lines.append(f"## {l['name']}")
            for e in self.entries(l["id"]):
                lines.append(" | ".join([display_ref(e["kind"], e["ref"]), e["label"] or e["title"], e["region"], "так" if e["enabled"] else "ні"]).rstrip(" |"))
            lines.append("")
        return "\n".join(lines).encode("utf-8")


def parse_numbers(spec: str):
    out = []
    for part in re.split(r"[,\s;]+", spec or ""):
        m = re.fullmatch(r"(\d+)\s*[-–—]\s*(\d+)", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            out += list(range(min(a, b), max(a, b) + 1))[:1000]
        elif part.isdigit():
            out.append(int(part))
    return sorted(set(out))


def _reorder(ids, item, how):
    i = ids.index(item)
    ids.pop(i)
    j = {"up": max(0, i - 1), "down": min(len(ids), i + 1), "top": 0, "bottom": len(ids)}.get(how, i)
    ids.insert(j, item)


def passes(text: str, include: str, exclude: str) -> bool:
    low = (text or "").lower()
    inc = [w.strip().lower() for w in (include or "").split(",") if w.strip()]
    exc = [w.strip().lower() for w in (exclude or "").split(",") if w.strip()]
    if exc and any(w in low for w in exc):
        return False
    if inc and not any(w in low for w in inc):
        return False
    return True
