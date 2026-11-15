import asyncio, os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.makedirs("/tmp/ts", exist_ok=True)
import shutil; shutil.rmtree("/tmp/ts", ignore_errors=True); os.makedirs("/tmp/ts", exist_ok=True)
os.environ.update(TELEGRAM_TOKEN="1:x", ALLOWED_USERS="111", DB_PATH="/tmp/ts/bot.db", MCP_SECRET="s"*20, GROQ_API_KEY="k", PUBLIC_URL="https://b.example/ai",
                  GDRIVE_CLIENT_ID="g", GDRIVE_CLIENT_SECRET="gs", ONEDRIVE_CLIENT_ID="o", ONEDRIVE_CLIENT_SECRET="os")
import httpx
TD, NOTION, GT, MS = [], [], [], []
LLM_STEP = {"n": 0}
def handler(req):
    u = str(req.url); body = json.loads(req.content) if req.content and req.headers.get("content-type", "").startswith("application/json") else None
    if "groq.com" in u:
        LLM_STEP["n"] += 1
        if LLM_STEP["n"] == 1 and any(t["function"]["name"] == "external_add" for t in body.get("tools", [])):
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "external_add", "arguments": json.dumps({"kind": "task", "title": "Подзвонити мамі", "due": "tomorrow", "service": "todoist"})}}]}, "finish_reason": "tool_calls"}]})
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "Додав у Todoist."}, "finish_reason": "stop"}]})
    if "todoist.com/api/v1/projects" in u: return httpx.Response(200, json={"results": [{"id": "p1", "name": "Inbox"}]})
    if "todoist.com/api/v1/tasks" in u and req.method == "POST" and u.endswith("/close"): TD[:] = [t for t in TD if t["id"] != u.split("/")[-2]]; return httpx.Response(204)
    if "todoist.com/api/v1/tasks" in u and req.method == "POST": t = {"id": str(len(TD) + 1), "content": body["content"], "due": {"string": body.get("due_string", "")} if body.get("due_string") else None, "url": "https://app.todoist.com/t/1"}; TD.append(t); return httpx.Response(200, json=t)
    if "todoist.com/api/v1/tasks" in u: return httpx.Response(200, json={"results": TD, "next_cursor": None})
    if "api.notion.com/v1/search" in u: return httpx.Response(200, json={"results": [{"id": "page-1", "properties": {"title": {"type": "title", "title": [{"plain_text": "Нотатки з бота"}]}}}]})
    if "api.notion.com/v1/pages" in u: NOTION.append(body); return httpx.Response(200, json={"url": "https://notion.so/x"})
    if "api.notion.com/v1/blocks" in u: return httpx.Response(200, json={"results": [{"id": "b%d" % i, "type": "child_page", "child_page": {"title": p["properties"]["title"]["title"][0]["text"]["content"]}} for i, p in enumerate(NOTION)]})
    if "oauth2.googleapis.com/token" in u or "oauth2/v2.0/token" in u: return httpx.Response(200, json={"access_token": "A", "refresh_token": "R", "expires_in": 3600})
    if "tasks.googleapis.com" in u and req.method == "POST": GT.append(body); return httpx.Response(200, json={"id": "g1"})
    if "tasks.googleapis.com" in u: return httpx.Response(200, json={"items": [{"id": "g%d" % i, "title": t["title"]} for i, t in enumerate(GT)]})
    if "graph.microsoft.com/v1.0/me/todo/lists" in u and u.endswith("/lists"): return httpx.Response(200, json={"value": [{"id": "L1", "wellknownListName": "defaultList"}]})
    if "graph.microsoft.com/v1.0/me/todo/lists/L1/tasks" in u and req.method == "POST": MS.append(body); return httpx.Response(201, json={"id": "m1"})
    if "graph.microsoft.com/v1.0/me/onenote/pages" in u and req.method == "POST": MS.append(req.content.decode()[:80]); return httpx.Response(201, json={"links": {"oneNoteWebUrl": {"href": "https://onenote.com/p"}}})
    return httpx.Response(404, json={"error": {"message": "nope " + u}})
_o = httpx.AsyncClient.__init__
def _i(self, *a, **k):
    k.setdefault("transport", httpx.MockTransport(handler)); _o(self, *a, **k)
httpx.AsyncClient.__init__ = _i
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import faketg; tg = faketg.install()
import bot
async def main():
    bot.init_db(); await tg.start(bot.tg_app); U = 111
    await tg.text(U, "/start")
    await tg.press(U, "m:svc"); print(tg.last_text(U)[:400]); print(tg.last_buttons(U)[:4])
    await tg.press(U, "m:sv:todoist"); await tg.press(U, "m:svcon:todoist"); await tg.text(U, "tok123")
    print("\n## todoist:", tg.out(U)[-2]["text"][:100]); print(tg.last_text(U)[:200])
    await tg.press(U, "m:svadd:todoist"); await tg.text(U, "Купити хліб @tomorrow\nбілий"); print("add:", tg.last_text(U), TD)
    await tg.press(U, "m:svls:todoist"); print(tg.last_text(U)); await tg.press(U, "m:svdone:todoist:0"); print("after done:", TD)
    await tg.press(U, "m:sv:notion"); await tg.press(U, "m:svcon:notion"); await tg.text(U, "ntn_abc"); print("\n## notion:", tg.last_text(U)[:200])
    await tg.press(U, "m:svauto:note"); await tg.text(U, "/note Ідея поста про зубрів"); await asyncio.sleep(0.2); print("auto-copied to notion:", len(NOTION))
    # send note via button
    await tg.press(U, "m:note:1"); print(tg.last_buttons(U)); await tg.press(U, "m:svp:n:1"); print(tg.last_buttons(U)); await tg.press(U, "m:svsend:notion:n:1"); print(tg.last_text(U))
    # oauth google tasks
    await tg.press(U, "m:sv:gtasks"); url = tg.find(U, "Увійти"); state = url.split("state=")[1].split("&")[0]
    print("\n## google:", await bot.SVC.callback("gtasks", "CODE", state)); await bot.SVC.add("gtasks", "task", "Тест Google", "", "2026-11-10"); print(GT)
    await tg.press(U, "m:sv:mstodo"); url = tg.find(U, "Увійти"); state = url.split("state=")[1].split("&")[0]
    print("## ms:", await bot.SVC.callback("mstodo", "C", state)); await bot.SVC.add("mstodo", "task", "Тест To Do", "", None)
    await tg.press(U, "m:sv:onenote"); url = tg.find(U, "Увійти"); state = url.split("state=")[1].split("&")[0]
    print("## onenote:", await bot.SVC.callback("onenote", "C", state)); print(await bot.SVC.add("onenote", "note", "Нотатка", "рядок1\nрядок2")); print(MS)
    # LLM tool
    await tg.text(U, "додай у Todoist подзвонити мамі завтра"); print("\n## llm:", tg.last_text(U)[:120], TD)
    await tg.press(U, "m:svc"); print(tg.last_text(U)[:600])
asyncio.run(main())


async def apple_mail():
    U = 111
    await tg.press(U, "m:sv:iphone"); print("\n## iphone:", tg.last_text(U)[:200])
    await tg.press(U, "m:svcon:iphone"); print(tg.last_text(U)[:300]); print(tg.last_buttons(U)[:3])
    await tg.press(U, "m:svadd:iphone"); await tg.text(U, "Купити ліки @2026-11-06"); print(tg.last_text(U))
    await tg.press(U, "m:note:1"); await tg.press(U, "m:svp:n:1"); await tg.press(U, "m:svsend:iphone:n:1"); print(tg.last_text(U))
    app = bot.mcp.streamable_http_app(streamable_http_path=f"/{bot.MCP_SECRET}/mcp", stateless_http=True, host="0.0.0.0")
    key = bot.SVC.cfg()["acc"]["iphone"]["key"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get(f"/{bot.MCP_SECRET}/iphone", params={"key": key}); print("GET:", r.status_code, json.dumps(r.json(), ensure_ascii=False)[:300])
        r = await c.get(f"/{bot.MCP_SECRET}/iphone", params={"key": key}); print("GET again:", r.json()["count"])
        r = await c.get(f"/{bot.MCP_SECRET}/iphone", params={"key": "bad"}); print("bad key:", r.status_code)
        r = await c.post(f"/{bot.MCP_SECRET}/iphone?key={key}", json={"text": "Нотатка з iPhone"}); print("POST:", r.json(), [n["text"] for n in bot.list_notes_db(chat_id=U)][:2])
    await tg.press(U, "m:sv:evernote"); print("\n## evernote:", tg.last_text(U)[-200:])
    import services
    sent = []
    async def fake_mail(to, subj, body): sent.append((to, subj, body))
    services.send_mail = fake_mail
    os.environ.update(SMTP_HOST="smtp.gmail.com", SMTP_USER="me@gmail.com", SMTP_PASSWORD="x")
    await tg.press(U, "m:sv:evernote"); await tg.press(U, "m:svcon:evernote"); await tg.text(U, "me.123@m.evernote.com"); print(tg.out(U)[-2]["text"])
    await tg.press(U, "m:svsend:evernote:n:1"); print(tg.last_text(U), sent)
    await tg.press(U, "m:svc"); print(tg.last_text(U)[-400:])

asyncio.run(apple_mail())
