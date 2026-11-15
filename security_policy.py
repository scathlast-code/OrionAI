"""Shared OrionAI access controls. Telegram updates do not expose client IP/phone."""
import ipaddress
import os
import time
from collections import defaultdict, deque

BLOCKED_COUNTRIES = frozenset('RU IR CN KP US HU PL TR AF YE PS IL ID RS'.split())
# UA is explicitly never blocked by regional rules, including occupied and border areas.
# GeoIP country data alone cannot reliably identify disputed/partially recognized regions.
# Configure exact provider-specific subdivision ISO codes after verifying them against
# the installed GeoIP City database. Never block an entire parent country.
REGIONAL_BLOCKS = {'BA': 'SECURITY_BLOCKED_BA_SUBDIVISIONS',
                   'MD': 'SECURITY_BLOCKED_MD_SUBDIVISIONS',
                   'GE': 'SECURITY_BLOCKED_GE_SUBDIVISIONS'}
_events = defaultdict(deque)


def region_blocked(country, subdivision=None):
    country = (country or '').upper()
    if country == 'UA':
        return False
    if country in BLOCKED_COUNTRIES:
        return True
    env_name = REGIONAL_BLOCKS.get(country)
    if not env_name or not subdivision:
        return False
    blocked = {v.strip().upper() for v in os.getenv(env_name, '').split(',') if v.strip()}
    return subdivision.upper() in blocked



def _ids(name):
    result = set()
    for value in os.getenv(name, '').replace(' ', '').split(','):
        if value:
            try:
                result.add(int(value))
            except ValueError:
                raise ValueError(f'{name} must contain Telegram numeric IDs')
    return result


def telegram_access(user_id, country=None, subdivision=None, scope=None):
    """Allowlist always wins only for identity, never for a known blocked country.

    Strict mode denies unknown location, because Bot API has no end-user IP.
    """
    if not user_id:
        return False, 'unknown identity'
    if region_blocked(country, subdivision):
        return False, 'blocked country'
    denied = _ids('SECURITY_DENIED_USERS')
    if int(user_id) in denied:
        return False, 'denied identity'
    # Bot-specific grants persist across restarts, IP changes, and GeoIP outages.
    # Revocation is explicit. Country blocks remain a separate security restriction.
    grant = 'absent'
    if scope is not None:
        import access_grants
        grant = access_grants.status(scope, user_id)
        if grant in ('revoked', 'expired'):
            return False, 'grant revoked or expired'
        if os.getenv('SECURITY_REQUIRE_GRANT', '0') == '1' and grant != 'granted':
            return False, 'no grant for this bot'
    allowed = _ids('SECURITY_ALLOWED_USERS')
    if allowed and int(user_id) not in allowed and grant != 'granted':
        return False, 'not on allowlist'
    if os.getenv('SECURITY_REQUIRE_VERIFIED_COUNTRY', '0') == '1' and not country:
        return False, 'location not verified'
    return True, 'allowed'


def rate_allowed(user_id, scope='telegram', limit=25, window=60):
    now = time.monotonic()
    key = (scope, int(user_id))
    q = _events[key]
    while q and q[0] <= now - window:
        q.popleft()
    if len(q) >= limit:
        return False
    q.append(now)
    return True


def client_ip(peer_ip, forwarded_for=None):
    """Trust X-Forwarded-For only when direct peer is explicitly trusted."""
    peer = ipaddress.ip_address(peer_ip)
    trusted = [ipaddress.ip_network(v.strip(), strict=False) for v in os.getenv('SECURITY_TRUSTED_PROXIES', '').split(',') if v.strip()]
    if forwarded_for and any(peer in net for net in trusted):
        # Rightmost untrusted hop; never blindly use leftmost user-supplied IP.
        hops = [ipaddress.ip_address(v.strip()) for v in forwarded_for.split(',')] + [peer]
        for hop in reversed(hops):
            if not any(hop in net for net in trusted):
                return hop
    return peer


def ip_geolocation(ip):
    """Return (country, subdivision) from local GeoIP City DB; unknown -> (None,None).

    GeoIP is approximate, especially for VPNs, roaming and conflict zones.
    """
    path = os.getenv('SECURITY_GEOIP_DB', '')
    if not path:
        return None, None
    try:
        import geoip2.database
        with geoip2.database.Reader(path) as reader:
            try:
                record = reader.city(str(ip))
                return record.country.iso_code, record.subdivisions.most_specific.iso_code
            except TypeError:
                # Country-only DB cannot resolve subnational blocks.
                record = reader.country(str(ip))
                return record.country.iso_code, None
    except Exception:
        return None, None


def ip_country(ip):
    return ip_geolocation(ip)[0]


def web_access(peer_ip, forwarded_for=None):
    try:
        ip = client_ip(peer_ip, forwarded_for)
    except ValueError:
        return False, 'invalid IP'
    country, subdivision = ip_geolocation(ip)
    if region_blocked(country, subdivision):
        return False, 'blocked location'
    if country is None and os.getenv('SECURITY_WEB_FAIL_CLOSED', '1') == '1':
        return False, 'country unavailable'
    return True, 'allowed'


def _trusted_ids():
    """Власник і довірені: ALLOWED_USERS (обидва боти) + SECURITY_ALLOWED_USERS."""
    out = set()
    for name in ('ALLOWED_USERS', 'SECURITY_ALLOWED_USERS'):
        for v in os.getenv(name, '').replace(' ', '').split(','):
            if v.lstrip('-').isdigit():
                out.add(int(v))
    return out


async def telegram_guard(update, context, scope=None):
    """PTB group=-100 guard for both bot applications."""
    from telegram.ext import ApplicationHandlerStop
    user = update.effective_user
    if not user:
        # Дописи каналів, де бот адміністратор, не мають автора-користувача. Пропускаємо їх далі
        # (🏘 Спільноти, 📰 Ньюзрум); усе інше без користувача, як і раніше, зупиняється.
        if getattr(update, "channel_post", None) or getattr(update, "edited_channel_post", None):
            return
        raise ApplicationHandlerStop
    ok, reason = telegram_access(user.id, scope=scope)
    chat = update.effective_chat
    private = bool(chat and getattr(chat, 'type', '') == 'private')
    if ok and not private and not update.callback_query:
        # Групи й канали: модерація (🏘 Спільноти) має бачити КОЖНЕ повідомлення, тож ліміт тут не діє,
        # і бот нічого не пише в чат від імені «захисту».
        return
    if ok and int(user.id) not in _trusted_ids():
        # Ліміт частоти — лише для сторонніх. Власник і люди з ALLOWED_USERS можуть швидко клацати меню
        # й пересилати в ньюзрум десятки дописів поспіль.
        ok = rate_allowed(user.id, limit=int(os.getenv('SECURITY_RATE_LIMIT', '40')))
        reason = 'rate limit' if not ok else reason
    if not ok:
        if not private and not update.callback_query:
            raise ApplicationHandlerStop
        # No location-specific disclosure; avoids account enumeration.
        try:
            if update.callback_query:
                await update.callback_query.answer('Забагато дій за хвилину — зачекайте трохи.' if reason == 'rate limit' else 'Доступ обмежено.', show_alert=True)
            elif update.effective_message:
                await update.effective_message.reply_text('Забагато повідомлень за хвилину — зачекайте трохи.' if reason == 'rate limit' else 'Доступ обмежено.')
        except Exception:
            pass
        raise ApplicationHandlerStop
