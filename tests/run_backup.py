import asyncio, os, sys, json, gzip
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import shutil; shutil.rmtree("/tmp/t2", ignore_errors=True)
os.environ.update(TELEGRAM_TOKEN="1:x", ALLOWED_USERS="111", DB_PATH="/tmp/t2/bot.db", MCP_SECRET="s"*20, GROQ_API_KEY="k",
                  PUBLIC_URL="https://bot.example", DROPBOX_APP_KEY="dk", DROPBOX_APP_SECRET="ds", GDRIVE_CLIENT_ID="gi", GDRIVE_CLIENT_SECRET="gs",
                  ONEDRIVE_CLIENT_ID="oi", ONEDRIVE_CLIENT_SECRET="os", BACKUP_PASSWORD="pw")
import httpx
FILES = {"dropbox": {}, "gdrive": {}, "onedrive": {}}
def handler(req: httpx.Request):
    u = str(req.url)
    if "oauth2/token" in u or "oauth2/v2.0/token" in u or "oauth2.googleapis" in u:
        return httpx.Response(200, json={"access_token": "A", "refresh_token": "R", "expires_in": 3600})
    if "dropboxapi.com/2/files/upload" in u:
        arg = json.loads(req.headers["Dropbox-API-Arg"]); FILES["dropbox"][arg["path"].lower()] = req.content
        return httpx.Response(200, json={"name": arg["path"]})
    if "list_folder" in u:
        return httpx.Response(200, json={"entries": [{".tag": "file", "path_lower": p, "name": p[1:], "size": len(b), "server_modified": p} for p, b in FILES["dropbox"].items()]})
    if "files/download" in u:
        return httpx.Response(200, content=FILES["dropbox"][json.loads(req.headers["Dropbox-API-Arg"])["path"]])
    if "delete_v2" in u:
        FILES["dropbox"].pop(json.loads(req.content)["path"], None); return httpx.Response(200, json={})
    if "googleapis.com/drive/v3/files" in u and req.method == "POST":
        return httpx.Response(200, json={"id": "FOLDER"})
    if "uploadType=resumable" in u:
        return httpx.Response(200, headers={"Location": "https://upload.example/session/" + json.loads(req.content)["name"]})
    if "upload.example/session/" in u:
        FILES["gdrive"][u.rsplit("/",1)[1]] = req.content; return httpx.Response(200, json={"id": "x"})
    if "googleapis.com/drive/v3/files" in u and "alt=media" in u:
        return httpx.Response(200, content=FILES["gdrive"][u.split("/files/")[1].split("?")[0]])
    if "googleapis.com/drive/v3/files" in u:
        return httpx.Response(200, json={"files": [{"id": n, "name": n, "size": str(len(b)), "createdTime": n} for n, b in FILES["gdrive"].items()]})
    if "createUploadSession" in u:
        name = u.split("approot:/")[1].split(":/")[0]; return httpx.Response(200, json={"uploadUrl": "https://up.ms/" + name})
    if "up.ms/" in u:
        FILES["onedrive"][u.rsplit("/",1)[1]] = req.content; return httpx.Response(201, json={})
    if "approot/children" in u:
        return httpx.Response(200, json={"value": [{"id": n, "name": n, "size": len(b), "createdDateTime": n, "file": {}} for n, b in FILES["onedrive"].items()]})
    return httpx.Response(404, text="nope " + u)
_orig = httpx.AsyncClient.__init__
def _init(self, *a, **k):
    k["transport"] = httpx.MockTransport(handler); _orig(self, *a, **k)
httpx.AsyncClient.__init__ = _init
import faketg; tg = faketg.install()
import bot

async def main():
    os.makedirs("/tmp/t2", exist_ok=True)
    bot.init_db(); await tg.start(bot.tg_app)
    U = 111
    await tg.text(U, "/note важлива нотатка")
    await tg.press(U, "m:bk"); print(tg.last_text(U)); print(tg.last_buttons(U))
    for cloud in ("dropbox", "gdrive", "onedrive"):
        await tg.press(U, f"m:bkt:{cloud}")
        url = tg.find(U, "Увійти")
        state = url.split("state=")[1].split("&")[0]
        ok, t = await bot.BK.callback(cloud, "CODE", state); print(cloud, ok, t)
    await tg.press(U, "m:bkgo"); print(tg.last_text(U))
    print({k: list(v) for k, v in FILES.items()})
    sent_doc = [p for c, m, p in tg.sent if m == "sendDocument"]; print("docs:", len(sent_doc))
    await tg.text(U, "/note нотатка після копії")
    await tg.press(U, "m:bkls:dropbox"); print(tg.last_text(U)); 
    await tg.press(U, "m:bkrc:dropbox:0"); print(tg.last_text(U))
    await tg.press(U, tg.find(U, "Так")); print(tg.last_text(U))
    print("notes now:", [n["text"] for n in bot.list_notes_db(chat_id=U)])
    # restore from file upload: encrypted blob
    name, data = bot.BK.make(); print("encrypted:", data[:5])
    tg.handlers["getFile"] = lambda p: {"file_id": p["file_id"], "file_unique_id": "u", "file_path": "files/x"}
    import telegram.request
    orig = telegram.request.HTTPXRequest.do_request
    async def dr(self, url, method, request_data=None, **kw):
        if "/file/bot" in url: return 200, data
        return await orig(self, url, method, request_data, **kw)
    telegram.request.HTTPXRequest.do_request = dr
    await tg.text(U, "", document={"file_id": "D1", "file_unique_id": "u", "file_name": name, "file_size": len(data)})
    print(tg.last_text(U)[:200])
    # channel
    await tg.press(U, "m:bkt:channel")
    await tg.text(U, "пост", forward_origin={"type": "channel", "date": 1, "chat": {"id": -100555, "type": "channel", "title": "Копії"}, "message_id": 5})
    print(tg.last_text(U)[:200])
    await tg.press(U, "m:bkgo:channel"); print(tg.last_text(U))
asyncio.run(main())


async def sched_test():
    U = 111
    await tg.press(U, "m:bkt:dropbox"); print("\n## target:", tg.last_text(U)[:200]); print(tg.last_buttons(U)[:2])
    await tg.press(U, "m:bks:dropbox"); print(tg.last_text(U)); print(tg.last_buttons(U))
    await tg.press(U, "m:bkse:dropbox:86400"); print("daily-int:", bot.BK.sched_text("dropbox"))
    await tg.press(U, "m:bksd:gdrive"); await tg.text(U, "03:30"); print("gdrive:", bot.BK.sched_text("gdrive"))
    await tg.press(U, "m:bksw:onedrive"); await tg.press(U, "m:bksw2:onedrive:6"); await tg.text(U, "22:00"); print("onedrive:", bot.BK.sched_text("onedrive"))
    await tg.press(U, "m:bkstop:dropbox"); print("dropbox:", bot.BK.sched_text("dropbox"))
    await tg.press(U, "m:bkspause:gdrive"); print("gdrive:", bot.BK.sched_text("gdrive"))
    c = bot.BK.cfg(); c["lastt"] = {}; c["last"] = 0; bot.BK.save_cfg(c)
    print("due now:", bot.BK.due_targets())
    await tg.press(U, "m:bkrs"); print("\n## restore menu:", tg.last_text(U)[:250]); print(tg.last_buttons(U))
    await tg.press(U, "m:bk"); print(tg.last_text(U)[-500:])

asyncio.run(sched_test())
