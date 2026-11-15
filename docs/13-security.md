# OrionAI security — Stage 6

The main bot, AI bot, and sticker Mini App are guarded separately. Telegram Bot API DOES NOT provide the sender IP or phone number, therefore Telegram-only country blocking cannot be guaranteed. Restrict Telegram identities with SECURITY_ALLOWED_USERS, and use Mini App IP geolocation for web access. GeoIP is approximate and VPN/proxy can bypass it.

Blocked ISO countries: RU IR CN KP US HU PL TR. SECURITY_GEOIP_DB must point to a valid MaxMind GeoLite2 Country mmdb, with geoip2 installed. Without a database, web routes fail closed by default (HTTP 403). Provision the mmdb securely outside GitHub. SECURITY_TRUSTED_PROXIES must list only actual trusted reverse proxy CIDRs. Never trust arbitrary forwarded headers.

Set SECURITY_ALLOWED_USERS to Telegram numeric IDs allowed to use either bot; in production keep it restricted. SECURITY_DENIED_USERS explicitly denies IDs. SECURITY_REQUIRE_VERIFIED_COUNTRY=1 denies Telegram users without a verified country, which means essentially all normal Bot API users; use only if that lockout is intended.

Security is defense in depth, not a promise of protection from every attack. For GitHub: private repo or protected branches, 2FA/passkeys, least-privilege CI tokens, pinned dependencies, secret scanning, no .env/keys in commits, no unreviewed pull request deployments, isolated deploy secrets, rotate compromised tokens. Set strict permissions on Oracle VM and firewall, HTTPS, backups, and monitor logs.

Status: code checks and local tests only. No real GeoIP DB, reverse proxy trust chain, Oracle firewall, or Telegram live deployment validated.
