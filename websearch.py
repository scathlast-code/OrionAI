"""Multi-source web search for OrionAI. No implicit paid fallback.

Search API keys are optional. All remote material is untrusted and may be incomplete.
"""
import asyncio
import html
from html.parser import HTMLParser
import ipaddress
import os
import re
import socket
from urllib.parse import parse_qs, quote, unquote, urlparse

import httpx

TRIGGER = re.compile(
    r'(?:пошукай|знайди\s+(?:в\s+)?інтернет|пошук\s+в\s+інтернет|'
    r'перевір\s+(?:в\s+)?(?:інтернет|мереж)|загугли|веб.?пошук|'
    r'search\s+(?:the\s+)?web|search\s+online|google\s+it|'
    r'поищи\s+в\s+интернет|найди\s+в\s+интернет|'
    r'(?:останні|свіжі|актуальні|сьогоднішні)\s+новини|'
    r'(?:що|які)\s+(?:зараз|сьогодні|нового)|курс\s+(?:долара|євро|гривні)|погода\s+(?:в|у|на|сьогодні|завтра)|'
    r'хто\s+(?:зараз|тепер)|скільки\s+(?:зараз\s+)?коштує|новини\s+(?:про|за|сьогодні))', re.I)


def wants_search(message):
    return isinstance(message, str) and bool(TRIGGER.search(message[:1500]))


def _items(rows):
    result = []
    for row in rows or []:
        url = str(row.get('url') or '').strip()
        if urlparse(url).scheme not in ('https', 'http'):
            continue
        result.append({'url': url, 'title': str(row.get('title') or '')[:180],
                       'snippet': str(row.get('snippet') or '')[:900]})
        if len(result) == 5:
            break
    return result


async def _tavily(c, q, k):
    r = await c.post('https://api.tavily.com/search', json={'api_key': k, 'query': q, 'search_depth': 'basic', 'max_results': 5})
    r.raise_for_status()
    return _items([dict(url=x.get('url'), title=x.get('title'), snippet=x.get('content')) for x in r.json().get('results', [])])


async def _brave(c, q, k):
    r = await c.get('https://api.search.brave.com/res/v1/web/search', params={'q': q, 'count': 5}, headers={'X-Subscription-Token': k})
    r.raise_for_status()
    return _items([dict(url=x.get('url'), title=x.get('title'), snippet=x.get('description')) for x in r.json().get('web', {}).get('results', [])])


async def _serper(c, q, k):
    r = await c.post('https://google.serper.dev/search', json={'q': q, 'num': 5}, headers={'X-API-KEY': k})
    r.raise_for_status()
    return _items([dict(url=x.get('link'), title=x.get('title'), snippet=x.get('snippet')) for x in r.json().get('organic', [])])


async def _serpapi(c, q, k):
    r = await c.get('https://serpapi.com/search.json', params={'engine': 'google', 'q': q, 'api_key': k, 'num': 5})
    r.raise_for_status()
    return _items([dict(url=x.get('link'), title=x.get('title'), snippet=x.get('snippet')) for x in r.json().get('organic_results', [])])


async def _exa(c, q, k):
    r = await c.post('https://api.exa.ai/search', json={'query': q, 'numResults': 5}, headers={'x-api-key': k})
    r.raise_for_status()
    return _items([dict(url=x.get('url'), title=x.get('title'), snippet=x.get('text') or '') for x in r.json().get('results', [])])


async def _you(c, q, k):
    r = await c.get('https://ydc-index.io/v1/search', params={'query': q, 'count': 5}, headers={'X-API-Key': k})
    r.raise_for_status()
    data = r.json()
    rows = data.get('results', {}).get('web', []) if isinstance(data.get('results'), dict) else data.get('results', [])
    return _items([dict(url=x.get('url'), title=x.get('title'), snippet=x.get('description') or x.get('snippet')) for x in rows])


async def _searchapi(c, q, k):
    r = await c.get('https://www.searchapi.io/api/v1/search', params={'engine': 'google', 'q': q, 'api_key': k})
    r.raise_for_status()
    return _items([dict(url=x.get('link'), title=x.get('title'), snippet=x.get('snippet')) for x in r.json().get('organic_results', [])])


async def _wikipedia(c, q):
    # Wikipedia is encyclopedic, not a general news/web search engine.
    r = await c.get('https://en.wikipedia.org/w/api.php', params={'action': 'query', 'list': 'search', 'srsearch': q, 'srlimit': 5, 'format': 'json'}, headers={'User-Agent': 'OrionAI/1.0 (websearch)'})
    r.raise_for_status()
    return _items([dict(url='https://en.wikipedia.org/?curid=' + str(x.get('pageid')), title=x.get('title'), snippet=re.sub('<[^>]+>', ' ', x.get('snippet', ''))) for x in r.json().get('query', {}).get('search', [])])


async def _gdelt(c, q):
    # GDELT covers news articles; it is not a general web search engine.
    r = await c.get('https://api.gdeltproject.org/api/v2/doc/doc', params={'query': q, 'mode': 'artlist', 'maxrecords': 5, 'format': 'json', 'sort': 'datedesc'})
    r.raise_for_status()
    return _items([dict(url=x.get('url'), title=x.get('title'), snippet=x.get('seendate', '')) for x in r.json().get('articles', [])])


class _DDGParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.results, self.capture, self.url, self.title = [], False, '', []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'a' and 'result__a' in a.get('class', ''):
            self.capture, self.url, self.title = True, a.get('href', ''), []

    def handle_data(self, data):
        if self.capture:
            self.title.append(data)

    def handle_endtag(self, tag):
        if tag == 'a' and self.capture:
            url = html.unescape(self.url)
            if url.startswith('//'):
                url = 'https:' + url
            if 'uddg=' in url:
                url = unquote(parse_qs(urlparse(url).query).get('uddg', [url])[0])
            self.results.append(dict(url=url, title=''.join(self.title).strip(), snippet=''))
            self.capture = False


async def _duckduckgo(c, q):
    # Unofficial HTML interface; may stop working without notice.
    r = await c.get('https://html.duckduckgo.com/html/', params={'q': q}, headers={'User-Agent': 'Mozilla/5.0'})
    r.raise_for_status()
    p = _DDGParser()
    p.feed(r.text)
    return _items(p.results)


class _PageText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.skip = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'noscript', 'svg'):
            self.skip += 1

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'noscript', 'svg') and self.skip:
            self.skip -= 1
        if tag in ('p', 'h1', 'h2', 'h3', 'li', 'article'):
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def _public_host(url):
    parsed = urlparse(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443):
        return False
    host = parsed.hostname
    if host.lower() in ('localhost',) or host.lower().endswith(('.local', '.internal')):
        return False
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_global
    except ValueError:
        return True


async def _read_page(c, url):
    # Validate resolved addresses to avoid fetching private-network resources.
    if not _public_host(url):
        return ''
    host = urlparse(url).hostname
    try:
        addresses = await asyncio.to_thread(socket.getaddrinfo, host, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            return ''
        # Redirects are disabled. Avoid trusting redirects to private hosts.
        async with c.stream('GET', url, follow_redirects=False, headers={'Accept': 'text/html'}) as r:
            if r.status_code != 200 or 'text/html' not in r.headers.get('content-type', ''):
                return ''
            chunks, size = [], 0
            async for chunk in r.aiter_bytes():
                size += len(chunk)
                if size > 180_000:
                    break
                chunks.append(chunk)
            p = _PageText()
            p.feed(b''.join(chunks).decode('utf-8', 'replace'))
            return re.sub(r'\s+', ' ', ' '.join(p.parts)).strip()[:3000]
    except (OSError, ValueError, httpx.HTTPError):
        return ''


async def search(query):
    query = str(query).strip()[:500]
    if not query:
        return None, 'Порожній пошуковий запит.'
    # Optional keys: never activate a paid service without its explicit key.
    providers = [
        ('Tavily', 'TAVILY_API_KEY', _tavily),
        ('Brave', 'BRAVE_SEARCH_API_KEY', _brave),
        ('Serper', 'SERPER_API_KEY', _serper),
        ('SerpAPI', 'SERPAPI_API_KEY', _serpapi),
        ('Exa', 'EXA_API_KEY', _exa),
        ('You.com', 'YOU_API_KEY', _you),
        ('SearchAPI', 'SEARCHAPI_API_KEY', _searchapi),
    ]
    services = [(name, lambda c, f=fn, k=os.getenv(env, '').strip(): f(c, query, k))
                for name, env, fn in providers if os.getenv(env, '').strip()]
    services.extend([
        ('DuckDuckGo', lambda c: _duckduckgo(c, query)),
        ('Wikipedia', lambda c: _wikipedia(c, query)),
        ('GDELT News', lambda c: _gdelt(c, query)),
    ])
    failures = []
    async with httpx.AsyncClient(timeout=12, follow_redirects=False, trust_env=False) as c:
        for name, fn in services:
            try:
                rows = await fn(c)
                if rows:
                    # Read up to two source pages. If pages block bots, snippets remain usable.
                    for row in rows[:2]:
                        content = await _read_page(c, row['url'])
                        if content:
                            row['snippet'] = ('Текст сторінки: ' + content)[:3000]
                    lines = [f'Джерело пошуку: {name} (зовнішні дані, перевіряй достовірність)']
                    for i, x in enumerate(rows, 1):
                        lines.append(f"[{i}] {x['title']}\nURL: {x['url']}\n{x['snippet']}")
                    return '\n\n'.join(lines), None
                failures.append(f'{name}: немає результатів')
            except (httpx.HTTPError, ValueError, TypeError, KeyError, UnicodeError) as exc:
                failures.append(f'{name}: HTTP {exc.response.status_code}' if isinstance(exc, httpx.HTTPStatusError) else f'{name}: {type(exc).__name__}')
    return None, 'Пошук не вдався: ' + '; '.join(failures)
