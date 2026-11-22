"""Переносить повний діалог із експорту Claude (Налаштування → Приватність → Експорт даних, файл conversations.json)
у docs/dialog/<назва>.md — дослівно, усі повідомлення по порядку, з датами й назвами вкладень.
Запуск: python3 tools/import_dialog.py шлях/до/conversations.json [частина назви чату]"""
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
src = pathlib.Path(sys.argv[1])
want = (sys.argv[2] if len(sys.argv) > 2 else "").lower()
convs = json.loads(src.read_text("utf-8"))
out_dir = ROOT / "docs" / "dialog"
out_dir.mkdir(parents=True, exist_ok=True)
SECRET = re.compile(r"(AIza[0-9A-Za-z_-]{30,}|sk-[A-Za-z0-9-]{20,}|apify_api_[A-Za-z0-9]{20,}|\d{8,10}:AA[A-Za-z0-9_-]{30,})")
n, links = 0, []
URL = re.compile(r"https?://[^\s)>\]»\"']+")
for c in convs:
    name = c.get("name") or c.get("uuid", "")
    if want and want not in name.lower():
        continue
    lines = [f"# {name}", f"_{c.get('created_at', '')} — {c.get('updated_at', '')}_", ""]
    for m in c.get("chat_messages", []):
        who = "👤 Власник" if m.get("sender") == "human" else "🤖 Claude"
        text = m.get("text") or "\n".join(p.get("text", "") for p in m.get("content", []) if isinstance(p, dict))
        files = [f.get("file_name") for f in (m.get("files") or []) + (m.get("attachments") or []) if f.get("file_name")]
        if m.get("sender") == "human":
            links += [(m.get("created_at", "")[:16].replace("T", " "), u.rstrip(".,;")) for u in URL.findall(text)]
        lines.append(f"## {who} · {m.get('created_at', '')}")
        lines.append(SECRET.sub("«ключ приховано»", text))
        if files:
            lines.append("📎 " + ", ".join(files))
        lines.append("")
    slug = re.sub(r"[^\w-]+", "-", name.lower()).strip("-")[:60] or c.get("uuid", "chat")
    (out_dir / f"{slug}.md").write_text("\n".join(lines), "utf-8")
    n += 1
lp = ROOT / "docs" / "33-posylannya.md"
body = "\n".join(f"- {d} · {u}" for d, u in dict.fromkeys(links))
lp.write_text(re.sub(r"<!-- AUTO:LINKS -->.*<!-- /AUTO:LINKS -->", "<!-- AUTO:LINKS -->\n" + body + "\n<!-- /AUTO:LINKS -->",
                     lp.read_text("utf-8"), flags=re.S), "utf-8")
print(f"Перенесено діалогів: {n} → {out_dir}; посилань власника: {len(set(links))}")
