#!/usr/bin/env python3
"""
Legis-Link LL-1: security and limits fix (v3.3.0).

  1. Pro keys are signed with LEGIS_KEY_SECRET and checked on every use. Made-up keys,
     "ll_admin_..." and "dev_local" no longer give Pro. Refunded keys stop working.
     Admin access only with the exact LEGIS_ADMIN_KEY set in Railway.
  2. No secrets in code: the Telegram token and key secret come only from Railway variables.
  3. Daily limits are saved in the database (they survive restarts) and every key or
     visitor has their own count (before, all anonymous app users shared one count of 50).
  4. psycopg2 added to requirements (the database code could not run without it), and a
     working get_db() (the free-tier counter called a function that did not exist).
  5. MCP over /sse works again: tool calls were sent to /messages/, which was not connected (404).
  6. /test needs the admin key; /health shows which Railway settings are present (never their values);
     /key/lookup can issue a key by hand (&issue=1). Public tool descriptions no longer mention dev_local.

Put this file in the legis-link-mcp folder (next to legis_link_mcp_server.py) and run:
    python ll1_security_fix.py
Atomic: nothing is written if any part does not match. Backups are made of each changed file.
"""
import ast
import datetime
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MAIN = os.environ.get("LL_MAIN") or os.path.join(HERE, "legis_link_mcp_server.py")
NOTIFY_FILE = os.path.join(os.path.dirname(MAIN), "nano_notify.py")
REQS = os.path.join(os.path.dirname(MAIN), "requirements.txt")
MARK = "LL-1: keys are signed and checked"

AUTH = '# ── API Key Auth (LL-1: keys are signed and checked; limits survive restarts) ──\n# Pro key:  ll_p_<16 hex id><16 hex signature>  signed with LEGIS_KEY_SECRET (set in Railway only)\n# Free key: ll_f_...  or no key at all = free tier\n# Admin:    the exact value of LEGIS_ADMIN_KEY (Railway only)\n# dev_local: Pro only when LEGIS_DEV_MODE=1 (local testing); otherwise treated as the free tier\n\nimport contextvars as _cv\nimport hmac as _hmac_mod\nimport secrets as _secrets_mod\nimport time as _time_mod\nfrom contextlib import contextmanager as _ctxmgr\n\nFREE_DAILY_LIMIT = int(os.environ.get("LEGIS_FREE_DAILY", "50"))\nPRO_DAILY_LIMIT  = int(os.environ.get("LEGIS_PRO_DAILY", "1000"))\n\n_rate_store: dict = defaultdict(int)          # fallback only, when the database is unavailable\n_LL_CLIENT = _cv.ContextVar("ll_client", default="")\n_DB_READY = []\n_KEY_CACHE: dict = {}\n\n\ndef _client_ip(request) -> str:\n    ip = (request.headers.get("x-forwarded-for", "") or "").split(",")[0].strip()\n    if not ip and getattr(request, "client", None):\n        ip = request.client.host or ""\n    return ip\n\n\n@_ctxmgr\ndef get_db():\n    """A database connection that is always closed. Creates the tables once."""\n    if not DATABASE_URL:\n        raise RuntimeError("DATABASE_URL is not set")\n    import psycopg2\n    conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)\n    try:\n        if not _DB_READY:\n            cur = conn.cursor()\n            cur.execute("CREATE TABLE IF NOT EXISTS legis_link_keys (id SERIAL PRIMARY KEY,email VARCHAR(255) UNIQUE NOT NULL,"\n                        "api_key VARCHAR(50),sale_id VARCHAR(100),product VARCHAR(100),active BOOLEAN DEFAULT TRUE,"\n                        "created_at TIMESTAMPTZ DEFAULT NOW())")\n            cur.execute("CREATE TABLE IF NOT EXISTS legis_link_free_usage (id SERIAL PRIMARY KEY, fingerprint TEXT NOT NULL,"\n                        " ip_address TEXT, date_utc DATE NOT NULL DEFAULT CURRENT_DATE, query_count INTEGER DEFAULT 0,"\n                        " last_seen TIMESTAMPTZ DEFAULT NOW(), UNIQUE(fingerprint, date_utc))")\n            cur.execute("CREATE TABLE IF NOT EXISTS legis_link_usage (bucket TEXT NOT NULL, date_utc DATE NOT NULL,"\n                        " n INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (bucket, date_utc))")\n            conn.commit()\n            _DB_READY.append(1)\n        yield conn\n    finally:\n        conn.close()\n\n\ndef _key_sig(kind: str, ident: str) -> str:\n    import hashlib as _hs\n    return _hmac_mod.new(KEY_SECRET, (kind + ":" + ident).encode(), _hs.sha256).hexdigest()[:16]\n\n\ndef _key_signed(k: str, kind: str) -> bool:\n    if not KEY_SECRET or len(k) != 37:\n        return False\n    ident, sig = k[5:21], k[21:]\n    return _hmac_mod.compare_digest(sig, _key_sig(kind, ident))\n\n\ndef _key_revoked(k: str) -> bool:\n    """True if the key was refunded or switched off. Cached 5 minutes. A database error never locks out a paying user."""\n    now = _time_mod.time()\n    hit = _KEY_CACHE.get(k)\n    if hit and now - hit[0] < 300:\n        return hit[1]\n    revoked = False\n    if DATABASE_URL:\n        try:\n            with get_db() as conn:\n                cur = conn.cursor()\n                cur.execute("SELECT active FROM legis_link_keys WHERE api_key=%s", (k,))\n                row = cur.fetchone()\n                revoked = bool(row) and row[0] is False\n        except Exception as e:\n            logging.warning(f"Key check: {e}")\n            return hit[1] if hit else False\n    if len(_KEY_CACHE) > 5000:\n        _KEY_CACHE.clear()\n    _KEY_CACHE[k] = (now, revoked)\n    return revoked\n\n\ndef validate_api_key(key: str | None) -> dict:\n    """Validate API key. Returns {valid, tier, reason}."""\n    if not key or not key.strip():\n        return {"valid": True, "tier": "free", "reason": "free", "remaining": FREE_DAILY_LIMIT}\n    k = key.strip()\n    if ADMIN_KEY and _hmac_mod.compare_digest(k, ADMIN_KEY):\n        return {"valid": True, "tier": "pro", "remaining": 999999, "reason": "admin"}\n    if k == "dev_local":\n        if DEV_MODE:\n            return {"valid": True, "tier": "pro", "reason": "dev"}\n        return {"valid": True, "tier": "free", "reason": "free"}\n    if k.startswith("ll_p_"):\n        if not _key_signed(k, "pro"):\n            return {"valid": False, "tier": None,\n                    "reason": "This Pro key is not recognised. Check it was copied in full, or email info@africompliance.com."}\n        if _key_revoked(k):\n            return {"valid": False, "tier": None, "reason": "This Pro key is no longer active."}\n        return {"valid": True, "tier": "pro", "reason": "pro"}\n    if k.startswith("ll_f_") and len(k) == 37:\n        return {"valid": True, "tier": "free", "reason": "free"}\n    return {"valid": False, "tier": None,\n            "reason": "Invalid key. Leave the key empty for the free tier, or paste your Pro key (starts with ll_p_)."}\n\n\ndef _usage_hit(bucket: str, limit: int):\n    """Count one use. Returns the new count, or None when the daily limit is reached. Database first, memory if it is down."""\n    if DATABASE_URL:\n        try:\n            with get_db() as conn:\n                cur = conn.cursor()\n                cur.execute("INSERT INTO legis_link_usage (bucket, date_utc, n) VALUES (%s, (NOW() AT TIME ZONE \'UTC\')::date, 1) "\n                            "ON CONFLICT (bucket, date_utc) DO UPDATE SET n = legis_link_usage.n + 1 "\n                            "WHERE legis_link_usage.n < %s RETURNING n", (bucket, limit))\n                row = cur.fetchone()\n                if _secrets_mod.randbelow(200) == 0:\n                    cur.execute("DELETE FROM legis_link_usage WHERE date_utc < CURRENT_DATE - INTERVAL \'30 days\'")\n                conn.commit()\n                return row[0] if row else None\n        except Exception as e:\n            logging.warning(f"Usage store: {e}")\n    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")\n    sk = bucket + ":" + today\n    if _rate_store[sk] >= limit:\n        return None\n    _rate_store[sk] += 1\n    if len(_rate_store) > 20000:\n        _rate_store.clear()\n    return _rate_store[sk]\n\n\ndef check_rate_limit(api_key: str, tier: str, who: str = "") -> dict:\n    """Check and count one request. Each key, or each anonymous visitor (by IP), has its own daily count."""\n    if api_key and ADMIN_KEY and _hmac_mod.compare_digest(api_key.strip(), ADMIN_KEY):\n        return {"allowed": True, "remaining": 999999, "limit": 999999}\n    limit = PRO_DAILY_LIMIT if tier == "pro" else FREE_DAILY_LIMIT\n    k = (api_key or "").strip()\n    ident = k if (k and k != "dev_local") else "anon:" + (who or _LL_CLIENT.get() or "unknown")\n    import hashlib as _hs\n    n = _usage_hit(_hs.sha256(ident.encode()).hexdigest()[:32], limit)\n    if n is None:\n        return {"allowed": False, "remaining": 0, "limit": limit,\n                "reset": "tomorrow 00:00 UTC",\n                "upgrade": PRO_UPGRADE if tier == "free" else None}\n    return {"allowed": True, "remaining": max(0, limit - n), "limit": limit}\n\n\n'
KEYGEN = 'KEY_SECRET        = os.environ.get("LEGIS_KEY_SECRET", "").encode()   # Railway only - never in code\nADMIN_KEY         = os.environ.get("LEGIS_ADMIN_KEY", "")              # Railway only\nDEV_MODE          = os.environ.get("LEGIS_DEV_MODE", "") == "1"         # local testing only\nTELEGRAM_TOKEN    = os.environ.get("TELEGRAM_TOKEN", "")               # Railway only\nTELEGRAM_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID", "2119918902")\n\n\ndef generate_pro_key(email: str = "") -> str:\n    """A new signed Pro key (random id + signature). Needs LEGIS_KEY_SECRET."""\n    import hmac as _h, hashlib as _hs, secrets as _sc\n    if not KEY_SECRET:\n        raise RuntimeError("LEGIS_KEY_SECRET is not set")\n    ident = _sc.token_hex(8)\n    return "ll_p_" + ident + _h.new(KEY_SECRET, ("pro:" + ident).encode(), _hs.sha256).hexdigest()[:16]\n\n\ndef generate_free_key(email: str) -> str:\n    import hmac as _h, hashlib as _hs\n    return "ll_f_" + _h.new(KEY_SECRET or b"free", ("free:"+email.lower().strip()).encode(), _hs.sha256).hexdigest()[:32]\n\n'
NOTIFY = 'def _notify_sale(email, api_key, product, sale_id):\n    if not TELEGRAM_TOKEN:\n        logging.warning("Sale not sent to Telegram: TELEGRAM_TOKEN is not set")\n        return\n    try:\n        import httpx as _hx\n        _hx.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",\n            json={"chat_id":TELEGRAM_CHAT_ID,"text":f"NEW PRO SALE\\nEmail: {email}\\nKey: {api_key}\\nProduct: {product}\\nSale: {sale_id}"},timeout=10)\n    except Exception: pass\n'
LOOKUP = '        async def handle_key_lookup(request):\n            """Admin. GET /key/lookup?email=x&secret=LEGIS_ADMIN_SECRET  - add &issue=1 to issue and store a Pro key by hand."""\n            import hmac as _hm\n            secret = os.environ.get("LEGIS_ADMIN_SECRET","")\n            if not secret or not _hm.compare_digest(request.query_params.get("secret",""), secret):\n                return JSONResponse({"error":"Unauthorized"},status_code=401)\n            email = request.query_params.get("email","").strip().lower()\n            if not email: return JSONResponse({"error":"email required"},status_code=400)\n            if request.query_params.get("issue") == "1":\n                if not KEY_SECRET:\n                    return JSONResponse({"error":"LEGIS_KEY_SECRET is not set in Railway"},status_code=500)\n                key = generate_pro_key(email)\n                _store_key(email, key, "manual", "manual")\n                return JSONResponse({"email":email,"pro_key":key,"issued":True})\n            try:\n                with get_db() as conn:\n                    cur = conn.cursor()\n                    cur.execute("SELECT api_key, active, sale_id, created_at FROM legis_link_keys WHERE email=%s", (email,))\n                    row = cur.fetchone()\n            except Exception as e:\n                return JSONResponse({"error":"database: " + str(e)[:120]},status_code=500)\n            if not row: return JSONResponse({"email":email,"found":False})\n            return JSONResponse({"email":email,"found":True,"pro_key":row[0],"active":row[1],"sale_id":row[2],"created_at":str(row[3])})\n\n'
SWAPS = [('"required": True,\n        "type": "api_key",', '"required": False,\n        "type": "api_key",', 1), ('"free": "50 requests/day, 3 tools — use api_key: dev_local for testing",', '"free": "50 requests/day, 3 tools — no key needed",', 1), ("Use 'dev_local' for testing.", 'Optional: leave empty for the free tier (50 requests/day); Pro users paste their ll_p_ key.', 3), ('key.startsWith(\\"ll_p_\\") || key.startsWith(\\"ll_admin_\\") || key === \\"dev_local\\"', 'key.startsWith(\\"ll_p_\\")', 3), ('\\"env\\": { \\"LEGIS_LINK_API_KEY\\": \\"dev_local\\" }', '\\"env\\": { \\"LEGIS_LINK_API_KEY\\": \\"\\" }', 1), ('<strong>Free tier:</strong> Use <code style=\\"font-family:var(--mono)\\">dev_local</code> as your API key for testing (50 requests/day).', '<strong>Free tier:</strong> No key needed. Leave the API key empty (50 requests/day).', 1), ('VERSION           = "3.2.4"', 'VERSION           = "3.3.0"', 1), ('            rate = check_rate_limit(api_key, tier)\n', '            if tier == "free" and (not api_key or api_key.strip() == "dev_local"):\n                _ip = _client_ip(request)\n                _fp = str(body.get("fingerprint", "") or "")[:200]\n                rate = check_rate_limit("", "free", ("fp:" + _fp) if _fp else ("ip:" + _ip))\n                if rate["allowed"] and _usage_hit("ipcap:" + hashlib.sha256(_ip.encode()).hexdigest()[:32], FREE_DAILY_LIMIT * 6) is None:\n                    rate = {"allowed": False, "remaining": 0, "limit": FREE_DAILY_LIMIT}\n            else:\n                rate = check_rate_limit(api_key, tier, _client_ip(request))\n', 1), ('        async def handle_sse(request):\n            async with sse.connect_sse(', '        async def handle_sse(request):\n            _LL_CLIENT.set(_client_ip(request))\n            async with sse.connect_sse(', 1), ('            key = generate_pro_key(email)\n            _store_key(email, key,', '            if not KEY_SECRET:\n                _notify_sale(email, "NOT ISSUED - set LEGIS_KEY_SECRET in Railway, then issue by hand", str(body.get("product_name","")), str(body.get("sale_id","")))\n                return JSONResponse({"error": "server not configured"}, status_code=500)\n            key = generate_pro_key(email)\n            _store_key(email, key,', 1), ('if body.get("refunded"): _revoke_key(email); return JSONResponse({"status":"revoked"})', 'if body.get("refunded"): _revoke_key(email); _KEY_CACHE.clear(); return JSONResponse({"status":"revoked"})', 1), ('            Mount("/sse", app=sse.handle_post_message),\n', '            Mount("/messages/", app=sse.handle_post_message),\n', 1), ('        async def handle_test(request):\n', '        async def handle_test(request):\n            import hmac as _hm\n            if not ADMIN_KEY or not _hm.compare_digest(request.query_params.get("key", ""), ADMIN_KEY):\n                return JSONResponse({"error": "Not found"}, status_code=404)\n', 1), ('                "auth": "required",\n                "api_key_set": bool(key)\n', '                "auth": "optional (free tier needs no key)",\n                "api_key_set": bool(key),\n                "key_secret_set": bool(KEY_SECRET), "admin_key_set": bool(ADMIN_KEY),\n                "telegram_set": bool(TELEGRAM_TOKEN), "database_set": bool(DATABASE_URL),\n                "dev_mode": DEV_MODE\n', 1)]


def abort(msg):
    print("ABORTED - no changes written.")
    print(msg)
    sys.exit(1)


def cut(src, start, end, new, what):
    i = src.find(start)
    j = src.find(end, i + len(start)) if i >= 0 else -1
    if i < 0 or j < 0 or src.count(start) != 1:
        abort("Could not find " + what + " exactly once.")
    return src[:i] + new + src[j:]


def main():
    for p in (MAIN, REQS):
        if not os.path.exists(p):
            abort("Not found: " + p + "\nPut this script in the legis-link-mcp folder.")
    with open(MAIN, "r", encoding="utf-8-sig", newline="") as f:
        src = f.read()
    if MARK in src:
        abort("LL-1 is already applied.")
    nl = "\r\n" if "\r\n" in src else "\n"
    out = src.replace(nl, "\n")
    out = cut(out, "# ── API Key Auth", "def is_pro_tool(name: str) -> bool:", AUTH, "the key check")
    out = cut(out, 'KEY_SECRET        = os.environ.get("LEGIS_KEY_SECRET"', "def _store_key(", KEYGEN, "the key secret")
    out = cut(out, "def _notify_sale(email, api_key, product, sale_id):", 'VERSION           = "', NOTIFY, "the sale message")
    out = cut(out, "        async def handle_key_lookup(request):", "        async def handle_toolbox(request):", LOOKUP, "the key lookup")
    for old, new, n in SWAPS:
        if out.count(old) != n:
            abort("Text not found exactly " + str(n) + " time(s):\n  " + old[:110])
        out = out.replace(old, new)
    try:
        ast.parse(out)
    except SyntaxError as e:
        abort("Patched file failed Python syntax check: " + str(e))
    out = out.replace("\n", nl)

    note = None
    if os.path.exists(NOTIFY_FILE):
        with open(NOTIFY_FILE, "r", encoding="utf-8-sig", newline="") as f:
            ns = f.read()
        i = ns.find('TELEGRAM_TOKEN   = "')
        if i >= 0:
            j = ns.find("\n", i)
            ns = ns[:i] + 'TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")   # Railway / .env only' + ns[j:]
            try:
                ast.parse(ns)
            except SyntaxError as e:
                abort("nano_notify.py failed syntax check: " + str(e))
            note = ns
    with open(REQS, "r", encoding="utf-8-sig") as f:
        reqs = f.read()
    if "psycopg2" not in reqs:
        reqs = reqs.rstrip("\n") + "\npsycopg2-binary==2.9.10\n"

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    for p in (MAIN, NOTIFY_FILE, REQS):
        if os.path.exists(p):
            shutil.copy2(p, p + ".bak_" + stamp)
    with open(MAIN, "w", encoding="utf-8", newline="") as f:
        f.write(out)
    if note is not None:
        with open(NOTIFY_FILE, "w", encoding="utf-8", newline="") as f:
            f.write(note)
    with open(REQS, "w", encoding="utf-8") as f:
        f.write(reqs)
    print("OK - LL-1 applied (v3.3.0).")
    print("  done: signed Pro keys, refunds blocked, no dev_local / ll_admin_ bypass")
    print("  done: Telegram token and key secret read from Railway only")
    print("  done: daily limits saved in the database, one count per key or visitor")
    print("  done: psycopg2 added, /test locked, /health shows which settings are present")
    print("Backups saved with .bak_" + stamp)


if __name__ == "__main__":
    main()
