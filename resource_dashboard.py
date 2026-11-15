"""Telegram HTML resource table. Never invent provider balances or reset timestamps."""
from html import escape

SEARCH = [
    ('Tavily','TAVILY_API_KEY','https://docs.tavily.com/','https://app.tavily.com/'),
    ('Brave Search','BRAVE_SEARCH_API_KEY','https://api-dashboard.search.brave.com/documentation','https://api-dashboard.search.brave.com/'),
    ('Serper','SERPER_API_KEY','https://serper.dev/','https://serper.dev/'),
    ('SerpAPI','SERPAPI_API_KEY','https://serpapi.com/search-api','https://serpapi.com/manage-api-key'),
    ('Exa','EXA_API_KEY','https://docs.exa.ai/','https://dashboard.exa.ai/api-keys'),
    ('You.com','YOU_API_KEY','https://docs.you.com/','https://you.com/platform'),
    ('SearchAPI','SEARCHAPI_API_KEY','https://www.searchapi.io/docs','https://www.searchapi.io/'),
    ('DuckDuckGo',None,'https://duckduckgo.com/duckduckgo-help-pages/',None),
    ('Wikipedia',None,'https://www.mediawiki.org/wiki/API:Main_page',None),
    ('GDELT',None,'https://www.gdeltproject.org/',None),
]
MODEL_LINKS = {
    'gemini':('https://ai.google.dev/gemini-api/docs','https://aistudio.google.com/api-keys'),
    'groq':('https://console.groq.com/docs/overview','https://console.groq.com/keys'),
    'mistral':('https://docs.mistral.ai/','https://console.mistral.ai/'),
    'nvidia':('https://docs.api.nvidia.com/','https://build.nvidia.com/'),
    'ollama_cloud':('https://docs.ollama.com/','https://ollama.com/'),
    'openrouter':('https://openrouter.ai/docs/quickstart','https://openrouter.ai/settings/keys'),
    'cloudflare':('https://developers.cloudflare.com/workers-ai/','https://dash.cloudflare.com/'),
    'cohere':('https://docs.cohere.com/','https://dashboard.cohere.com/'),
    'ollama_local':('https://docs.ollama.com/','https://ollama.com/'),
}

def bar(used, limit):
    if not isinstance(limit, (int,float)) or limit <= 0 or not isinstance(used,(int,float)):
        return '—'
    n = max(0, min(10, round(10*used/limit)))
    return '▓'*n+'░'*(10-n)

def render(models, usage, environ):
    """Return multiple self-contained Telegram HTML messages (<4096 characters each)."""
    groups = ['<b>📊 OrionAI · ресурси та ліміти</b>\nЛокальна статистика — не баланс кабінету провайдера.\n'
              'Шкала: використано ▓ / залишилось ░. «?» = невідомо.']
    for r in models:
        doc, api = MODEL_LINKS.get(r['id'], (None,None))
        remaining, reset, period = '?', 'невідомо', 'не визначено'
        limit = None
        if r['reported']:
            # First provider-reported quota; additional quotas appear as text.
            name, rem, lim, rst = r['reported'][0]
            remaining, reset, period = str(rem), str(rst or 'невідомо'), str(name)
            try: limit = float(lim) if lim is not None else None
            except (ValueError,TypeError): pass
        elif r['configured_limit']:
            period, limit = r['configured_limit']
            remaining = f'≈{max(0, limit - (usage(r["id"],period)[0]))} (оцінка)'
            reset = 'невідомо (провайдер не повідомив)'
        extra = ''
        if len(r['reported']) > 1:
            extra = '\n  Інші квоти: '+ '; '.join(f'{escape(str(n))}: {escape(str(v))} залишилось' for n,v,_,_ in r['reported'][1:])
        scale = bar(usage(r['id'], period)[0], limit) if limit and not r['reported'] else '— (окремий лічильник)'
        links = ' · '.join(f'<a href="{escape(url,quote=True)}">{title}</a>' for title,url in [('Довідка',doc),('Отримати API',api)] if url)
        groups.append(f'<b>{escape(r["name"])}</b> · {escape(r["status"])}\n'
                      f'  {escape(str(r["used"]))} запитів за добу · помилок {r["errors"]}\n'
                      f'  {escape(scale)}\n'
                      f'  Залишок: {escape(str(remaining))} · період: {escape(str(period))}\n'
                      f'  Скидання: {escape(str(reset))}{extra}\n  {links}')
    for name,key,doc,api in SEARCH:
        state = '▫️ не підключено' if key and not environ.get(key) else '🔑 ключ налаштовано (не перевірено)' if key else '🌐 без ключа (стан не перевірено)'
        links = f'<a href="{doc}">Довідка</a>' + (f' · <a href="{api}">Отримати API</a>' if api else '')
        groups.append(f'<b>{escape(name)}</b> · {state}\n  Залишок: невідомо · скидання: невідомо\n  {links}')
    chunks=[]; current=groups[0]
    for g in groups[1:]:
        if len(current)+len(g)+2>3500:
            chunks.append(current); current='<b>📊 OrionAI · продовження</b>'
        current+='\n\n'+g
    chunks.append(current)
    return chunks
