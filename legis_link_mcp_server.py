"""
Legis-Link MCP Server v3.2.1
=============================
Claude-direct engine with production foundations:

TIER 1 — FREE (3 tools, 50 req/day):
  check_compliance, get_code_reference, list_supported_regions

TIER 2 — PRO $199/year (8 tools, 1000 req/day):
  + calculate_technical_spec, generate_safety_checklist,
    generate_rams, verify_material_compliance, get_inspection_requirements

PRODUCTION FOUNDATIONS (v3.2.1):
  ✓ API key authentication (ll_f_xxx free / ll_p_xxx pro)
  ✓ Rate limiting (50/day free, 1000/day pro)
  ✓ Audit logging (every tool call logged to DB)
  ✓ Framework scaffold for future phases

FUTURE FRAMEWORK (auto-documented, not yet built):
  Phase 2 (10+ users): OAuth 2.1, usage dashboard, email receipts
  Phase 3 (first enterprise): RLS, namespace partitioning, WORM audit
  Phase 4 (regulated industry): Firecracker, crypto log chaining, VPC

Run locally:  python legis_link_mcp_server.py
Deploy:       Railway auto-detects PORT env var

DB (optional, for audit log):
  Set DATABASE_URL env var. Falls back to file log if no DB.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import sys
import httpx
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp import types
except ImportError:
    print("Install MCP SDK: pip install mcp", file=sys.stderr)
    sys.exit(1)

# ── Config ──────────────────────────────────────────────────────────────────

def _load_api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key:
        return key.strip()
    script_dir = os.path.dirname(os.path.abspath(__file__))
    for env_file in [
        os.path.join(script_dir, "legis_link.env"),
        os.path.join(script_dir, ".env"),
        os.path.join(os.path.expanduser("~"), ".nanobot", "skills", "legis_link.env"),
    ]:
        if os.path.exists(env_file):
            with open(env_file, encoding="utf-8-sig") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("ANTHROPIC_API_KEY="):
                        return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""

ANTHROPIC_API_KEY = _load_api_key()
ANTHROPIC_URL     = "https://api.anthropic.com/v1/messages"
MODEL             = os.environ.get("LEGIS_MODEL", "") or "claude-haiku-4-5-20251001"   # set LEGIS_MODEL in Railway to upgrade
PORT              = int(os.environ.get("PORT", 8000))
DATABASE_URL      = os.environ.get("DATABASE_URL", "")
OPENAI_API_KEY    = os.environ.get("OPENAI_API_KEY", "")
OPENAI_URL        = "https://api.openai.com/v1/chat/completions"
OPENAI_MODEL      = "gpt-4o-mini"
PRO_UPGRADE       = "https://rickyfarmer.gumroad.com/l/Legis-LinkPro"
KEY_SECRET        = os.environ.get("LEGIS_KEY_SECRET", "").encode()   # Railway only - never in code
ADMIN_KEY         = os.environ.get("LEGIS_ADMIN_KEY", "")              # Railway only
DEV_MODE          = os.environ.get("LEGIS_DEV_MODE", "") == "1"         # local testing only
TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_TOKEN", "")               # Railway only
TELEGRAM_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID", "2119918902")


def generate_pro_key(email: str = "") -> str:
    """A new signed Pro key (random id + signature). Needs LEGIS_KEY_SECRET."""
    import hmac as _h, hashlib as _hs, secrets as _sc
    if not KEY_SECRET:
        raise RuntimeError("LEGIS_KEY_SECRET is not set")
    ident = _sc.token_hex(8)
    return "ll_p_" + ident + _h.new(KEY_SECRET, ("pro:" + ident).encode(), _hs.sha256).hexdigest()[:16]


def generate_free_key(email: str) -> str:
    import hmac as _h, hashlib as _hs
    return "ll_f_" + _h.new(KEY_SECRET or b"free", ("free:"+email.lower().strip()).encode(), _hs.sha256).hexdigest()[:32]

def _store_key(email, api_key, sale_id, product):
    if not DATABASE_URL: return
    try:
        import psycopg2
        conn=psycopg2.connect(DATABASE_URL); cur=conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS legis_link_keys (id SERIAL PRIMARY KEY,email VARCHAR(255) UNIQUE NOT NULL,api_key VARCHAR(50),sale_id VARCHAR(100),product VARCHAR(100),active BOOLEAN DEFAULT TRUE,created_at TIMESTAMPTZ DEFAULT NOW())")
        cur.execute("""CREATE TABLE IF NOT EXISTS legis_link_free_usage (
            id          SERIAL PRIMARY KEY,
            fingerprint TEXT NOT NULL,
            ip_address  TEXT,
            date_utc    DATE NOT NULL DEFAULT CURRENT_DATE,
            query_count INTEGER DEFAULT 0,
            last_seen   TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE(fingerprint, date_utc)
        )""")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ll_fp ON legis_link_free_usage(fingerprint, date_utc)")
        cur.execute("INSERT INTO legis_link_keys (email,api_key,sale_id,product) VALUES (%s,%s,%s,%s) ON CONFLICT (email) DO UPDATE SET api_key=EXCLUDED.api_key,sale_id=EXCLUDED.sale_id,active=TRUE",(email.lower().strip(),api_key,sale_id,product))
        conn.commit(); conn.close()
    except Exception as e: logging.warning(f"Key store: {e}")

def _revoke_key(email):
    if not DATABASE_URL: return
    try:
        import psycopg2
        conn=psycopg2.connect(DATABASE_URL); cur=conn.cursor()
        cur.execute("UPDATE legis_link_keys SET active=FALSE WHERE email=%s",(email.lower().strip(),))
        conn.commit(); conn.close()
    except Exception as e: logging.warning(f"Key revoke: {e}")

def _log_milestone(type_, desc):
    """Log a milestone to NANO memory ? called on sales and traffic events."""
    import subprocess, sys
    try:
        subprocess.Popen([
            sys.executable,
            r"C:\Users\USER\.nanobot\skills\nano_milestone_logger.py",
            "--log", type_, desc
        ], creationflags=0x08000000)  # CREATE_NO_WINDOW
    except Exception:
        pass

def _notify_sale(email, api_key, product, sale_id):
    if not TELEGRAM_TOKEN:
        logging.warning("Sale not sent to Telegram: TELEGRAM_TOKEN is not set")
        return
    try:
        import httpx as _hx
        _hx.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id":TELEGRAM_CHAT_ID,"text":f"NEW PRO SALE\nEmail: {email}\nKey: {api_key}\nProduct: {product}\nSale: {sale_id}"},timeout=10)
    except Exception: pass
VERSION           = "3.24.1"
# LL-19: verified US data for IL, PA, OH, GA, NC, WA
# LL-17: Legis-Link support email is info@legis-link.com
# ── Page content (embedded — no filesystem dependency) ────────────────────
_PAGES = {
    "app.html":      "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n  <meta charset=\"UTF-8\"/>\n  <link rel=\"icon\" href=\"/favicon.svg\" type=\"image/svg+xml\"/>\n  <link rel=\"icon\" href=\"/favicon.ico\" sizes=\"32x32\"/>\n  <link rel=\"apple-touch-icon\" href=\"/apple-touch-icon.png\"/>\n  <meta name=\"description\" content=\"Free construction compliance checker for UK trades. Part P, BS 7671, WAHR 2005, CDM 2015. Electricians, plumbers, HVAC. No signup. Free construction compliance AI for UK and EU trades. Part P, BS 7671, WAHR 2005, CDM 2015. 9 features, no signup required.\"/>\n  <meta name=\"keywords\" content=\"construction compliance checker UK, electrical compliance Part P, RAMS template free, building regulations checker\"/>\n  <meta property=\"og:title\" content=\"Legis-Link ? Free Construction Compliance AI\"/>\n  <meta property=\"og:description\" content=\"Free compliance checks, daily toolbox talks, RAMS generation for UK and EU trades. No signup required.\"/>\n  <meta property=\"og:url\" content=\"https://legis-link.com/app\"/>\n  <meta property=\"og:type\" content=\"website\"/>\n  <meta name=\"twitter:card\" content=\"summary_large_image\"/>\n  <meta name=\"twitter:title\" content=\"Legis-Link ? Construction Compliance AI\"/>\n  <meta name=\"twitter:description\" content=\"Free electrical, plumbing, HVAC and construction compliance checker. UK and EU regulations.\"/>\n  <link rel=\"canonical\" href=\"https://legis-link.com/app\"/>\n  <script type=\"application/ld+json\">\n  {\"@context\":\"https://schema.org\",\"@type\":\"SoftwareApplication\",\"name\":\"Legis-Link\",\"description\":\"Construction compliance AI for UK and EU trades ? 9 features including free compliance checks, toolbox talks, and Pro RAMS generation\",\"url\":\"https://legis-link.com/app\",\"applicationCategory\":\"BusinessApplication\",\"operatingSystem\":\"Web\",\"offers\":[{\"@type\":\"Offer\",\"name\":\"Free\",\"price\":\"0\",\"priceCurrency\":\"GBP\"},{\"@type\":\"Offer\",\"name\":\"Pro\",\"price\":\"199\",\"priceCurrency\":\"GBP\",\"billingDuration\":\"P1Y\"}],\"featureList\":\"Compliance check, Code reference, Toolbox talks, RAMS generator, Technical calculations, Safety checklist, Material compliance, Inspection requirements, Visual compliance\"}\n  </script>\n  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no\"/>\n  <meta name=\"mobile-web-app-capable\" content=\"yes\"/>\n  <meta name=\"apple-mobile-web-app-capable\" content=\"yes\"/>\n  <meta name=\"theme-color\" content=\"#0f172a\"/>\n  <title>Legis-Link</title>\n  <link rel=\"manifest\" href=\"/manifest.json\"/>\n  <link href=\"https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&display=swap\" rel=\"stylesheet\"/>\n  <style>\n    :root{\n      --bg:#0B1320;--surface:#111C2E;--surface2:#18263B;\n      --border:#22324A;--accent:#FF6A13;--accent2:#FFC72C;\n      --text:#f1f5f9;--muted:#64748b;--muted2:#94a3b8;\n      --green:#10b981;--amber:#f59e0b;--red:#ef4444;\n      --mono:\"IBM Plex Mono\",monospace;--sans:\"IBM Plex Sans\",sans-serif;\n      --r:10px;--sb:64px;\n    }\n    *{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}\n    html,body{height:100%;overflow:hidden;background:var(--bg);color:var(--text);font-family:var(--sans)}\n    .app{display:flex;height:100dvh}\n\n    /* Sidebar */\n    .sb{width:var(--sb);flex-shrink:0;background:var(--surface);border-right:1px solid var(--border);display:flex;flex-direction:column;overflow-y:auto;scrollbar-width:none}\n    .sb::-webkit-scrollbar{display:none}\n    .sb-logo{font-family:var(--mono);font-size:12px;color:var(--accent);text-align:center;padding:10px 0 8px;border-bottom:1px solid var(--border);flex-shrink:0}\n    .tb{display:flex;flex-direction:column;align-items:center;gap:2px;padding:8px 4px;margin:2px 4px;border-radius:8px;cursor:pointer;border:1px solid transparent;background:none;color:var(--muted);font-family:var(--sans);transition:all 0.15s;flex-shrink:0}\n    .tb:active,.tb.on{background:rgba(255,106,19,0.12);border-color:rgba(255,106,19,0.25);color:var(--accent)}\n    .ti{font-size:20px;line-height:1}\n    .tn{font-size:9px;font-weight:500;text-align:center;line-height:1.2;margin-top:1px}\n\n    /* Main */\n    .main{flex:1;display:flex;flex-direction:column;min-width:0;overflow:hidden}\n\n    /* Header */\n    .hdr{background:var(--surface);border-bottom:1px solid var(--border);padding:8px 10px;flex-shrink:0}\n    .hdr-r1{display:flex;align-items:center;justify-content:space-between;margin-bottom:6px}\n    .trade-lbl{font-family:var(--mono);font-size:13px;color:var(--accent);font-weight:500}\n    .rate-pill{font-family:var(--mono);font-size:10px;padding:3px 8px;border-radius:20px;background:var(--border);color:var(--muted2);white-space:nowrap;flex-shrink:0}\n    .rate-pill.warn{color:var(--amber)}\n    .rate-pill.limit{color:var(--red)}\n    .hdr-r2{display:flex;gap:6px}\n    .csel{flex:1;min-width:0;background:var(--surface2);border:1px solid var(--border);border-radius:7px;color:var(--text);font-family:var(--sans);font-size:12px;padding:6px 8px;cursor:pointer;appearance:none;-webkit-appearance:none;outline:none}\n    .csel:focus{border-color:var(--accent)}\n    .csel option{background:var(--surface)}\n\n    /* Tool selector */\n    .tools-bar{padding:6px 10px;display:flex;gap:5px;overflow-x:auto;flex-shrink:0;border-bottom:1px solid var(--border);scrollbar-width:none;background:var(--bg)}\n    .tools-bar::-webkit-scrollbar{display:none}\n    .tool-btn{\n      display:flex;align-items:center;gap:5px;\n      padding:5px 10px;border-radius:7px;border:1px solid var(--border);\n      background:var(--surface);color:var(--muted2);font-size:11px;\n      font-family:var(--sans);cursor:pointer;white-space:nowrap;flex-shrink:0;\n      transition:all 0.15s;\n    }\n    .tool-btn:active,.tool-btn.active{background:rgba(255,106,19,0.12);border-color:rgba(255,106,19,0.35);color:var(--accent)}\n    .tool-btn.pro-tool{border-color:rgba(255,199,44,0.25);color:var(--accent2)}\n    .tool-btn.pro-tool.active{background:rgba(255,199,44,0.12);border-color:var(--accent2);color:var(--accent2)}\n    .tool-dot{width:6px;height:6px;border-radius:50%;background:currentColor;flex-shrink:0}\n    .pro-badge{font-size:8px;background:rgba(255,199,44,0.2);color:var(--accent2);padding:1px 4px;border-radius:3px;font-weight:600}\n\n    /* Offline */\n    .offbar{display:none;background:rgba(245,158,11,0.08);border-bottom:1px solid rgba(245,158,11,0.15);color:var(--amber);font-size:11px;text-align:center;padding:5px;flex-shrink:0}\n    body.offline .offbar{display:block}\n\n    /* Messages */\n    .msgs{flex:1;overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:12px}\n    .msgs::-webkit-scrollbar{width:3px}\n    .msgs::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}\n\n    .welcome{display:flex;flex-direction:column;align-items:center;justify-content:center;flex:1;text-align:center;padding:20px;gap:10px;min-height:140px}\n    .wlogo{width:46px;height:46px;background:linear-gradient(135deg,var(--accent),var(--accent2));border-radius:13px;display:flex;align-items:center;justify-content:center;font-family:var(--mono);font-size:18px;font-weight:600;color:white}\n    .welcome h2{font-family:var(--mono);font-size:16px;font-weight:500}\n    .welcome p{font-size:12px;color:var(--muted2);line-height:1.6;max-width:230px}\n\n    .msg{display:flex;gap:8px;animation:up 0.2s ease}\n    @keyframes up{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:translateY(0)}}\n    .msg.user{flex-direction:row-reverse}\n    .av{width:27px;height:27px;border-radius:8px;flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:600;font-family:var(--mono)}\n    .msg.user .av{background:var(--accent);color:#0B1320}\n    .msg.bot  .av{background:var(--surface2);border:1px solid var(--border);color:var(--accent2)}\n    .mb{max-width:calc(100% - 37px);display:flex;flex-direction:column;gap:4px}\n    .bbl{padding:9px 12px;border-radius:var(--r);font-size:13px;line-height:1.65}\n    .msg.user .bbl{background:linear-gradient(135deg,var(--accent),#E85A0C);color:#0B1320;border-bottom-right-radius:3px}\n    .msg.bot  .bbl{background:var(--surface2);border:1px solid var(--border);border-bottom-left-radius:3px}\n    .tool-tag{display:inline-flex;align-items:center;gap:4px;font-family:var(--mono);font-size:9px;padding:2px 7px;border-radius:4px;width:fit-content;background:var(--border);color:var(--muted);margin-bottom:2px}\n    .stag{display:inline-flex;align-items:center;gap:4px;font-family:var(--mono);font-size:10px;padding:2px 8px;border-radius:4px;width:fit-content}\n    .ok{background:rgba(16,185,129,0.12);color:var(--green)}\n    .wn{background:rgba(245,158,11,0.12);color:var(--amber)}\n    .fl{background:rgba(239,68,68,0.12);color:var(--red)}\n    .crf{font-family:var(--mono);font-size:10px;color:var(--muted)}\n\n    .typing{display:flex;gap:4px;align-items:center;padding:2px 0}\n    .typing span{width:5px;height:5px;background:var(--muted);border-radius:50%;animation:dot 1.2s infinite}\n    .typing span:nth-child(2){animation-delay:0.2s}\n    .typing span:nth-child(3){animation-delay:0.4s}\n    @keyframes dot{0%,80%,100%{transform:scale(1);opacity:0.5}40%{transform:scale(1.3);opacity:1}}\n\n    /* Quick bar */\n    .qbar{padding:6px 10px;display:flex;gap:6px;overflow-x:auto;flex-shrink:0;scrollbar-width:none}\n    .qbar::-webkit-scrollbar{display:none}\n    .qb{background:var(--surface2);border:1px solid var(--border);border-radius:16px;color:var(--muted2);font-size:11px;padding:5px 11px;cursor:pointer;white-space:nowrap;flex-shrink:0;font-family:var(--sans);transition:all 0.15s}\n    .qb:active{border-color:var(--accent);color:var(--text)}\n\n    /* Input */\n    .inp{padding:8px 10px 12px;border-top:1px solid var(--border);background:var(--surface);flex-shrink:0}\n    .inpr{display:flex;flex-direction:column;gap:10px} .inpr textarea{width:100%;font-size:15px;line-height:1.4;resize:none} .actbar{display:flex;gap:10px;align-items:center} .actbar .ibtn-send{margin-left:auto} .ibtn{width:54px;height:54px;border-radius:12px;border:1px solid var(--border);background:var(--surface);color:var(--text);display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px;cursor:pointer;flex-shrink:0;transition:all .15s;-webkit-tap-highlight-color:transparent} .ibtn svg{width:23px;height:23px} .ibtn .iblabel{font-size:8.5px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;opacity:.7} .ibtn:hover{border-color:var(--accent);color:var(--accent)} .ibtn:active{transform:scale(.94)} .ibtn.ibtn-send{background:var(--green,#10b981);color:#fff;border-color:var(--green,#10b981)} .ibtn.ibtn-send .iblabel{opacity:.9} #mic.active{background:var(--red,#e5484d);color:#fff;border-color:var(--red,#e5484d);animation:micpulse 1.2s ease-in-out infinite} #spk.active{background:var(--accent);color:#0B1320;border-color:var(--accent)} @keyframes micpulse{0%,100%{box-shadow:0 0 0 0 rgba(229,72,77,.5)}50%{box-shadow:0 0 0 7px rgba(229,72,77,0)}} @media(max-width:600px){.ibtn{width:58px;height:58px}.ibtn svg{width:25px;height:25px}.actbar{gap:12px}}\n    textarea#q{flex:1;background:var(--surface2);border:1px solid var(--border);border-radius:var(--r);color:var(--text);font-family:var(--sans);font-size:13px;padding:9px 11px;resize:none;line-height:1.5;max-height:80px;outline:none;transition:border-color 0.15s}\n    textarea#q:focus{border-color:var(--accent)}\n    textarea#q::placeholder{color:var(--muted)}\n    .ibtn{width:38px;height:38px;border-radius:9px;border:none;display:flex;align-items:center;justify-content:center;cursor:pointer;flex-shrink:0;transition:all 0.15s}\n    #mic{background:var(--surface2);border:1px solid var(--border);color:var(--muted2);font-size:13px;font-family:var(--sans)}\n    #mic.on{background:rgba(239,68,68,0.15);border-color:var(--red);color:var(--red);animation:pulse 1s infinite}\n    #cam{background:var(--surface2);border:1px solid var(--border);color:var(--muted2);font-size:12px;font-family:var(--sans)}\n    #cam.has-image{background:rgba(39,174,96,0.15);border-color:var(--green);color:var(--green)}\n    #send{background:var(--accent);color:#0B1320}\n    #send:disabled{opacity:0.4;cursor:not-allowed}\n    @keyframes pulse{0%,100%{opacity:1}50%{opacity:0.5}}\n    .upbox{background:rgba(255,106,19,0.07);border:1px solid rgba(255,106,19,0.2);border-radius:var(--r);padding:10px 12px;font-size:13px;color:var(--accent2);line-height:1.7}\n    .upbox a{color:var(--accent);font-weight:600;text-decoration:none}\n    .toolbox-panel{display:none;flex-direction:column;gap:0;border-bottom:1px solid var(--border);flex-shrink:0;max-height:60vh;overflow-y:auto}\n    .toolbox-panel.open{display:flex}\n    .tb-header{padding:10px 12px;background:rgba(16,185,129,0.08);border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between}\n    .tb-title{font-size:13px;font-weight:600;color:var(--green)}\n    .tb-close{background:none;border:none;color:var(--muted);cursor:pointer;font-size:16px;padding:0}\n    .tb-body{padding:12px}\n    .tb-topic{font-size:14px;font-weight:600;color:var(--text);margin-bottom:6px}\n    .tb-obj{font-size:12px;color:var(--muted2);margin-bottom:10px;line-height:1.5}\n    .tb-section-label{font-family:var(--mono);font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:0.05em;margin-bottom:6px}\n    .hazard-item{display:flex;gap:8px;margin-bottom:6px;font-size:12px}\n    .risk-pill{font-size:9px;padding:2px 6px;border-radius:4px;font-weight:700;white-space:nowrap;flex-shrink:0}\n    .risk-c{background:rgba(239,68,68,0.15);color:var(--red)}\n    .risk-h{background:rgba(245,158,11,0.15);color:var(--amber)}\n    .risk-m{background:rgba(255,106,19,0.15);color:var(--accent)}\n    .tb-check{font-size:12px;color:var(--muted2);padding:4px 0;border-bottom:1px solid var(--border);display:flex;gap:8px;align-items:center}\n    .tb-check::before{content:\"\u2610\";color:var(--green);font-size:14px;flex-shrink:0}\n    .tb-discuss{background:rgba(245,158,11,0.08);border-left:3px solid var(--amber);padding:8px 10px;font-size:12px;color:var(--amber);border-radius:0 6px 6px 0;margin-top:8px;line-height:1.5}\n    .tb-ref{font-family:var(--mono);font-size:10px;color:var(--accent2);padding:6px 8px;background:rgba(255,199,44,0.06);border-radius:5px;margin-top:8px}\n    .tb-pdf-btn{display:flex;align-items:center;justify-content:space-between;margin-top:10px;padding:8px 12px;background:rgba(16,185,129,0.1);border:1px solid rgba(16,185,129,0.25);border-radius:8px;font-size:12px;color:var(--green);cursor:pointer}\n    .key-panel{display:none;padding:8px 10px;background:var(--surface2);border-bottom:1px solid var(--border);gap:8px;align-items:center}\n    .key-panel.open{display:flex}\n    .key-input{flex:1;background:var(--bg);border:1px solid var(--border);border-radius:7px;color:var(--text);font-family:var(--mono);font-size:12px;padding:6px 10px;outline:none}\n    .key-input:focus{border-color:var(--accent)}\n    .key-save{background:var(--accent);color:#0B1320;border:none;border-radius:7px;padding:6px 14px;font-size:12px;cursor:pointer;font-weight:600}\n    .tier-badge{font-family:var(--mono);font-size:10px;padding:2px 8px;border-radius:10px}\n    .tier-free{background:var(--border);color:var(--muted2)}\n    .tier-pro{background:rgba(16,185,129,0.15);color:var(--green);border:1px solid rgba(16,185,129,0.3)}\n    .upbox strong{color:var(--text)}\n  </style>\n</head>\n<body>\n<div class=\"app\">\n  <div class=\"sb\" id=\"sb\">\n    <div class=\"sb-logo\"><img src=\"/icon.svg\" alt=\"Legis-Link\" width=\"34\" height=\"34\" style=\"display:block;margin:0 auto;border-radius:9px\"/></div>\n  </div>\n  <div class=\"main\">\n    <div class=\"offbar\">Offline - cached answers only</div>\n    <div class=\"key-panel\" id=\"key-panel\">\n      <input class=\"key-input\" id=\"key-input\" placeholder=\"Paste your Pro key (from your Gumroad receipt) or leave blank\" maxlength=\"50\"/>\n      <button class=\"key-save\" id=\"key-save-btn\">Save</button>\n      <span class=\"tier-badge tier-free\" id=\"tier-badge\">FREE</span>\n    </div>\n    <header class=\"hdr\">\n      <div class=\"hdr-r1\">\n        <div class=\"trade-lbl\" id=\"tlbl\">Electrical</div>\n        <div class=\"rate-pill\" id=\"rate\">50 left</div>\n        <button id=\"key-btn\" onclick=\"toggleKeyInput()\" style=\"background:var(--accent);border:none;color:#0B1320;font-size:11px;padding:5px 12px;border-radius:6px;cursor:pointer;font-family:var(--mono);margin-left:6px;font-weight:700\">KEY</button>\n      </div>\n      <div class=\"hdr-r2\">\n        <select class=\"csel\" id=\"region\">\n          <option value=\"NSW\">AU - NSW</option>\n          <option value=\"VIC\">AU - VIC</option>\n          <option value=\"QLD\">AU - QLD</option>\n          <option value=\"WA\">AU - WA</option>\n          <option value=\"SA\">AU - SA</option>\n          <option value=\"ACT\">AU - ACT</option>\n          <option value=\"TAS\">AU - TAS</option>\n          <option value=\"NT\">AU - NT</option>\n          <option value=\"England\">UK - England</option>\n          <option value=\"Scotland\">UK - Scotland</option>\n          <option value=\"Wales\">UK - Wales</option>\n          <option value=\"Northern Ireland\">UK - Northern Ireland</option>\n          <option value=\"Alabama\">US - Alabama</option>\n          <option value=\"Alaska\">US - Alaska</option>\n          <option value=\"Arizona\">US - Arizona</option>\n          <option value=\"Arkansas\">US - Arkansas</option>\n          <option value=\"California\">US - California</option>\n          <option value=\"Colorado\">US - Colorado</option>\n          <option value=\"Connecticut\">US - Connecticut</option>\n          <option value=\"Delaware\">US - Delaware</option>\n          <option value=\"Florida\">US - Florida</option>\n          <option value=\"Georgia\">US - Georgia</option>\n          <option value=\"Hawaii\">US - Hawaii</option>\n          <option value=\"Idaho\">US - Idaho</option>\n          <option value=\"Illinois\">US - Illinois</option>\n          <option value=\"Indiana\">US - Indiana</option>\n          <option value=\"Iowa\">US - Iowa</option>\n          <option value=\"Kansas\">US - Kansas</option>\n          <option value=\"Kentucky\">US - Kentucky</option>\n          <option value=\"Louisiana\">US - Louisiana</option>\n          <option value=\"Maine\">US - Maine</option>\n          <option value=\"Maryland\">US - Maryland</option>\n          <option value=\"Massachusetts\">US - Massachusetts</option>\n          <option value=\"Michigan\">US - Michigan</option>\n          <option value=\"Minnesota\">US - Minnesota</option>\n          <option value=\"Mississippi\">US - Mississippi</option>\n          <option value=\"Missouri\">US - Missouri</option>\n          <option value=\"Montana\">US - Montana</option>\n          <option value=\"Nebraska\">US - Nebraska</option>\n          <option value=\"Nevada\">US - Nevada</option>\n          <option value=\"New Hampshire\">US - New Hampshire</option>\n          <option value=\"New Jersey\">US - New Jersey</option>\n          <option value=\"New Mexico\">US - New Mexico</option>\n          <option value=\"New York\">US - New York</option>\n          <option value=\"North Carolina\">US - North Carolina</option>\n          <option value=\"North Dakota\">US - North Dakota</option>\n          <option value=\"Ohio\">US - Ohio</option>\n          <option value=\"Oklahoma\">US - Oklahoma</option>\n          <option value=\"Oregon\">US - Oregon</option>\n          <option value=\"Pennsylvania\">US - Pennsylvania</option>\n          <option value=\"Rhode Island\">US - Rhode Island</option>\n          <option value=\"South Carolina\">US - South Carolina</option>\n          <option value=\"South Dakota\">US - South Dakota</option>\n          <option value=\"Tennessee\">US - Tennessee</option>\n          <option value=\"Texas\">US - Texas</option>\n          <option value=\"Utah\">US - Utah</option>\n          <option value=\"Vermont\">US - Vermont</option>\n          <option value=\"Virginia\">US - Virginia</option>\n          <option value=\"Washington\">US - Washington</option>\n          <option value=\"West Virginia\">US - West Virginia</option>\n          <option value=\"Wisconsin\">US - Wisconsin</option>\n          <option value=\"Wyoming\">US - Wyoming</option>\n          <option value=\"Washington DC\">US - Washington DC</option>\n          <option value=\"Ontario\">CA - Ontario</option>\n          <option value=\"British Columbia\">CA - British Columbia</option>\n          <option value=\"Alberta\">CA - Alberta</option>\n          <option value=\"Quebec\">CA - Quebec</option>\n          <option value=\"Manitoba\">CA - Manitoba</option>\n          <option value=\"Saskatchewan\">CA - Saskatchewan</option>\n          <option value=\"Nova Scotia\">CA - Nova Scotia</option>\n          <option value=\"New Brunswick\">CA - New Brunswick</option>\n          <option value=\"Newfoundland\">CA - Newfoundland</option>\n          <option value=\"Prince Edward Island\">CA - Prince Edward Island</option>\n          <option value=\"Germany\">EU - Germany</option>\n          <option value=\"France\">EU - France</option>\n          <option value=\"Netherlands\">EU - Netherlands</option>\n          <option value=\"Ireland\">EU - Ireland</option>\n          <option value=\"Spain\">EU - Spain</option>\n          <option value=\"Italy\">EU - Italy</option>\n          <option value=\"Belgium\">EU - Belgium</option>\n          <option value=\"Austria\">EU - Austria</option>\n          <option value=\"Denmark\">EU - Denmark</option>\n          <option value=\"Sweden\">EU - Sweden</option>\n          <option value=\"Finland\">EU - Finland</option>\n          <option value=\"Portugal\">EU - Portugal</option>\n          <option value=\"Poland\">EU - Poland</option>\n          <option value=\"Czech Republic\">EU - Czech Republic</option>\n        </select>\n        <select class=\"csel\" id=\"role\" style=\"max-width:105px\">\n          <option value=\"Qualified Tradesperson\">Qualified Tradesperson</option>\n          <option value=\"Apprentice / Trainee\">Apprentice / Trainee</option>\n          <option value=\"Supervisor / Foreman\">Supervisor / Foreman</option>\n          <option value=\"PM / Executive\">PM</option>\n        </select>\n      </div>\n    </header>\n\n    <!-- Tool selector -->\n    <div class=\"tools-bar\" id=\"tools-bar\">\n      <button class=\"tool-btn\" id=\"toolbox-btn\" onclick=\"showToolbox()\">\n        <span class=\"tool-dot\" style=\"background:#10b981\"></span>Toolbox\n      </button>\n      <button class=\"tool-btn active\" data-tool=\"check_compliance\">\n        <span class=\"tool-dot\"></span>Compliance\n      </button>\n      <button class=\"tool-btn pro-tool\" data-tool=\"calculate_technical_spec\">\n        <span class=\"tool-dot\"></span>Calc<span class=\"pro-badge\">PRO</span>\n      </button>\n      <button class=\"tool-btn pro-tool\" data-tool=\"generate_safety_checklist\">\n        <span class=\"tool-dot\"></span>Safety<span class=\"pro-badge\">PRO</span>\n      </button>\n      <button class=\"tool-btn pro-tool\" data-tool=\"generate_rams\">\n        <span class=\"tool-dot\"></span>RAMS<span class=\"pro-badge\">PRO</span>\n      </button>\n      <button class=\"tool-btn pro-tool\" data-tool=\"verify_material_compliance\">\n        <span class=\"tool-dot\"></span>Material<span class=\"pro-badge\">PRO</span>\n      </button>\n      <button class=\"tool-btn pro-tool\" data-tool=\"get_inspection_requirements\">\n        <span class=\"tool-dot\"></span>Inspection<span class=\"pro-badge\">PRO</span>\n      </button>\n      <button class=\"tool-btn pro-tool\" data-tool=\"visual_compliance\">\n        <span class=\"tool-dot\"></span>Photo\n      </button>\n    </div>\n\n    <div class=\"msgs\" id=\"msgs\">\n      <div class=\"welcome\" id=\"welcome\">\n        <div class=\"wlogo\" style=\"background:none\"><img src=\"/icon.svg\" alt=\"\" width=\"46\" height=\"46\" style=\"display:block;border-radius:13px\"/></div>\n        <h2>Legis-Link</h2>\n        <p>Select a tool above or ask any compliance question. Pro tools auto-detected from your question.</p>\n      </div>\n    </div>\n    <div class=\"toolbox-panel\" id=\"toolbox-panel\">\n      <div class=\"tb-header\">\n        <div class=\"tb-title\" id=\"tb-day-label\">Today's Toolbox Talk</div>\n        <button class=\"tb-close\" onclick=\"closeToolbox()\">&#10005;</button>\n      </div>\n      <div class=\"tb-body\" id=\"tb-body\">\n        <div style=\"text-align:center;padding:20px;color:var(--muted)\">Loading...</div>\n      </div>\n    </div>\n    <div class=\"qbar\" id=\"qbar\"></div>\n    <div id=\"img-preview-wrap\" style=\"display:none;padding:6px 10px;background:var(--surface2);border-top:1px solid var(--border)\">\n      <img id=\"img-preview\" style=\"max-height:80px;max-width:150px;border-radius:8px;border:1px solid var(--border)\" alt=\"Preview\"/>\n      <button onclick=\"clearImage()\" style=\"background:var(--red);color:white;border:none;border-radius:5px;padding:3px 8px;font-size:11px;cursor:pointer;margin-left:8px\">Remove</button>\n    </div>\n    <div class=\"inp\">\n      <div class=\"inpr\">\n        <textarea id=\"q\" rows=\"2\" placeholder=\"Ask a compliance question...\"></textarea>\n        <input type=\"file\" id=\"img-input\" accept=\"image/jpeg,image/png,image/webp\" style=\"display:none\"/>\n        <div class=\"actbar\">\n          <button class=\"ibtn\" id=\"cam\" title=\"Photo compliance check\"><svg viewBox=\"0 0 32 32\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"1.8\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><rect x=\"3\" y=\"9\" width=\"26\" height=\"18\" rx=\"2.5\"/><path d=\"M11 9l2-3h6l2 3\"/><circle cx=\"16\" cy=\"18\" r=\"5.5\"/><circle cx=\"16\" cy=\"18\" r=\"1.8\" fill=\"currentColor\" stroke=\"none\"/></svg><span class=\"iblabel\">Photo</span></button>\n          <button class=\"ibtn\" id=\"mic\" title=\"Speak your question (uses your browser's speech recognition)\"><svg viewBox=\"0 0 32 32\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"1.8\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><rect x=\"12\" y=\"3\" width=\"8\" height=\"15\" rx=\"4\"/><path d=\"M8 14v1a8 8 0 0 0 16 0v-1\"/><line x1=\"16\" y1=\"23\" x2=\"16\" y2=\"28\"/><line x1=\"11\" y1=\"28\" x2=\"21\" y2=\"28\"/></svg><span class=\"iblabel\">Speak</span></button>\n          <button class=\"ibtn\" id=\"spk\" title=\"Read answers aloud\"><svg viewBox=\"0 0 32 32\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"1.8\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"M4 12h5l7-5v18l-7-5H4z\"/><path d=\"M21 11a6 6 0 0 1 0 10\"/><path d=\"M24.5 7.5a11 11 0 0 1 0 17\"/></svg><span class=\"iblabel\">Listen</span></button>\n          <button class=\"ibtn ibtn-send\" id=\"send\" title=\"Send\"><svg viewBox=\"0 0 32 32\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"1.9\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"M28 4L14 18\"/><path d=\"M28 4L20 28l-6-10-10-6z\"/></svg><span class=\"iblabel\">Send</span></button>\n        </div>\n      </div>\n    </div>\n  </div>\n</div>\n<script>\nconst TRADES = [\n  {id:\"Electrical\",     icon:\"&#9889;\", short:\"Elec\"},\n  {id:\"Plumbing\",       icon:\"&#128295;\",short:\"Plumb\"},\n  {id:\"HVAC\",           icon:\"&#10052;\", short:\"HVAC\"},\n  {id:\"Gas fitting\",    icon:\"&#128293;\",short:\"Gas\"},\n  {id:\"Welding\",        icon:\"&#128293;\",short:\"Weld\"},\n  {id:\"Solar / Battery\",icon:\"&#9728;\",  short:\"Solar\"},\n  {id:\"Fire protection\",icon:\"&#128680;\",short:\"Fire\"},\n  {id:\"Carpentry\",      icon:\"&#128296;\",short:\"Carp\"},\n  {id:\"Concrete\",       icon:\"&#9632;\",  short:\"Conc\"},\n  {id:\"Roofing\",        icon:\"&#8963;\",  short:\"Roof\"},\n];\n\nconst QUICK = {\n  \"Electrical\":      [\"Wire size 20A\",\"Voltage drop\",\"Earth fault loop\",\"RCD requirements\",\"Breaker sizing\"],\n  \"Plumbing\":        [\"Pipe size 50 fixtures\",\"Hot water temp\",\"Backflow prevention\",\"Drain slope\"],\n  \"HVAC\":            [\"Duct size 800 CFM\",\"Ventilation rate\",\"Refrigerant clearance\",\"MERV rating\"],\n  \"Gas fitting\":     [\"Pipe sizing\",\"Pressure test\",\"Ventilation calc\",\"Appliance clearance\"],\n  \"Welding\":         [\"Preheat temp\",\"AWS qualification\",\"Inspection requirements\",\"Electrode storage\"],\n  \"Solar / Battery\": [\"Battery clearance\",\"Inverter location\",\"DC cable sizing\",\"Isolator requirements\"],\n  \"Fire protection\": [\"Detector spacing\",\"Sprinkler clearance\",\"Extinguisher placement\",\"Exit sign height\"],\n  \"Carpentry\":       [\"Bearer span\",\"Joist sizing\",\"Tie-down requirements\",\"Fixing schedule\"],\n  \"Concrete\":        [\"Cover to reo\",\"Curing time\",\"Mix design\",\"Compressive strength\"],\n  \"Roofing\":         [\"Min pitch\",\"Flashing detail\",\"Wind uplift\",\"Sarking requirements\"],\n};\n\nconst TOOL_LABELS = {\n  \"check_compliance\":           \"Compliance\",\n  \"calculate_technical_spec\":   \"Calc\",\n  \"generate_safety_checklist\":  \"Safety\",\n  \"generate_rams\":              \"RAMS\",\n  \"verify_material_compliance\": \"Material\",\n  \"get_inspection_requirements\":\"Inspection\",\n};\n\nconst FREE_LIMIT = 50;\nlet _fp=localStorage.getItem('ll-fp')||'';\n(async()=>{\n  if(!_fp){\n    try{ _fp=(crypto&&crypto.randomUUID)?crypto.randomUUID():('ll_'+Date.now().toString(36)+Math.random().toString(36).slice(2,12)); }\n    catch(e){ _fp='ll_'+Date.now().toString(36)+Math.random().toString(36).slice(2,12); }\n    localStorage.setItem('ll-fp',_fp);\n  }\n  try{\n    const r=await fetch('/api/fingerprint',{method:'POST',\n      headers:{'Content-Type':'application/json'},\n      body:JSON.stringify({fingerprint:_fp})});\n    const d=await r.json();\n    if(typeof d.used==='number'){used=d.used;localStorage.setItem('ll-used',used);updateRate();}\n  }catch(e){}\n})();\nconst CACHE_KEY  = \"ll-cache\";\nlet activeTrade  = \"Electrical\";\nlet activeTool   = \"check_compliance\"; // default, overridden by auto-detect\nlet manualTool   = false; // true when user explicitly picked a tool\n\nlet used = parseInt(localStorage.getItem(\"ll-used\") || \"0\");\nconst savedDate = localStorage.getItem(\"ll-date\") || \"\";\nconst today = new Date().toISOString().slice(0,10);\nif (savedDate !== today) { used = 0; localStorage.setItem(\"ll-date\", today); }\n\n// \u2500\u2500 Auto-detect tool from question \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\nfunction detectTool(q) {\n  const ql = q.toLowerCase();\n  if (/cable size|wire size|pipe size|duct size|voltage drop|current rating|load calc|sizing|ampacity|flow rate|heat load|cooling load|what size|calculate/.test(ql))\n    return \"calculate_technical_spec\";\n  if (/rams|risk assessment|method statement|jha|job hazard|swms|safe work method/.test(ql))\n    return \"generate_rams\";\n  if (/safety checklist|ppe|hazard|toolbox|induction|safety check|what safety|safe to/.test(ql))\n    return \"generate_safety_checklist\";\n  if (/material|compliant|approved|can i use|is this cable|is this pipe|product|brand|specification|meets code/.test(ql))\n    return \"verify_material_compliance\";\n  if (/inspection|sign off|certificate|hold point|certifier|who inspects|test and tag|commissioning|handover/.test(ql))\n    return \"get_inspection_requirements\";\n  return \"check_compliance\";\n}\n\nfunction setActiveTool(toolId, fromUser=false) {\n  activeTool = toolId;\n  manualTool = fromUser;\n  document.querySelectorAll(\".tool-btn\").forEach(b => {\n    b.classList.toggle(\"active\", b.dataset.tool === toolId);\n  });\n}\n\n// \u2500\u2500 Build sidebar \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\nconst sb = document.getElementById(\"sb\");\nTRADES.forEach((t,i) => {\n  const btn = document.createElement(\"button\");\n  btn.className = \"tb\" + (i===0?\" on\":\"\");\n  btn.dataset.trade = t.id;\n  btn.innerHTML = `<span class=\"ti\">${t.icon}</span><span class=\"tn\">${t.short}</span>`;\n  btn.addEventListener(\"click\", function() {\n    document.querySelectorAll(\".tb\").forEach(b => b.classList.remove(\"on\"));\n    this.classList.add(\"on\");\n    activeTrade = this.dataset.trade;\n    document.getElementById(\"tlbl\").textContent = activeTrade;\n    renderQuick();\n    if (document.getElementById(\"toolbox-panel\").classList.contains(\"open\")) loadToolbox();\n  });\n  sb.appendChild(btn);\n});\n\n// \u2500\u2500 Tool button clicks \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\ndocument.querySelectorAll(\".tool-btn\").forEach(btn => {\n  btn.addEventListener(\"click\", function() {\n    setActiveTool(this.dataset.tool, true);\n    // Update placeholder to hint at what to ask\n    const hints = {\n      \"check_compliance\":           \"Ask any compliance question...\",\n      \"calculate_technical_spec\":   \"e.g. Cable size for 32A circuit in conduit, 25m run...\",\n      \"generate_safety_checklist\":  \"e.g. Safety checklist for working at height...\",\n      \"generate_rams\":              \"e.g. RAMS for electrical switchboard installation...\",\n      \"verify_material_compliance\": \"e.g. Is 6mm2 TPS cable approved for this use...\",\n      \"get_inspection_requirements\":\"e.g. Inspection requirements for timber frame NSW...\",\n    };\n    document.getElementById(\"q\").placeholder = hints[this.dataset.tool] || \"Ask a compliance question...\";\n    document.getElementById(\"q\").focus();\n  });\n});\n\nfunction toggleKeyInput() {\n  var panel = document.getElementById(\"key-panel\");\n  panel.classList.toggle(\"open\");\n  if (panel.classList.contains(\"open\")) {\n    var stored = localStorage.getItem(\"ll_api_key\") || \"\";\n    document.getElementById(\"key-input\").value = stored === \"dev_local\" ? \"\" : stored;\n    document.getElementById(\"key-input\").focus();\n  }\n}\nfunction saveKey() {\n  var input = document.getElementById(\"key-input\").value.trim();\n  var key = input;\n  if (key) { localStorage.setItem(\"ll_api_key\", key); } else { localStorage.removeItem(\"ll_api_key\"); }\n  updateTierBadge(key);\n  document.getElementById(\"key-panel\").classList.remove(\"open\");\n  if (key && (isProKey(key))) {\n    document.getElementById(\"rate\").textContent = \"1000/day\";\n    document.getElementById(\"rate\").className = \"rate-pill\";\n  } else { updateRate(); }\n}\nfunction updateTierBadge(key) {\n  var badge = document.getElementById(\"tier-badge\");\n  var keyBtn = document.getElementById(\"key-btn\");\n  if (!badge) return;\n  if (key && (isProKey(key))) {\n    badge.textContent = \"PRO\"; badge.className = \"tier-badge tier-pro\";\n    if (keyBtn) { keyBtn.style.color = \"var(--green)\"; keyBtn.textContent = \"PRO\"; }\n  } else {\n    badge.textContent = \"FREE\"; badge.className = \"tier-badge tier-free\";\n    if (keyBtn) { keyBtn.style.color = \"\"; keyBtn.textContent = \"key\"; }\n  }\n}\n// Save key button\nvar saveBtn = document.getElementById(\"key-save-btn\");\nif (saveBtn) saveBtn.addEventListener(\"click\", saveKey);\n\nfunction showToolbox() {\n  const panel = document.getElementById(\"toolbox-panel\");\n  panel.classList.add(\"open\");\n  const btn = document.getElementById(\"toolbox-btn\");\n  if (btn) btn.classList.add(\"active\");\n  loadToolbox();\n}\n\nfunction closeToolbox() {\n  document.getElementById(\"toolbox-panel\").classList.remove(\"open\");\n  const btn = document.getElementById(\"toolbox-btn\");\n  if (btn) btn.classList.remove(\"active\");\n}\n\nasync function loadToolbox() {\n  const body = document.getElementById(\"tb-body\");\n  const dayLabel = document.getElementById(\"tb-day-label\");\n  try {\n    const tbRegionEl = document.getElementById(\"region\"); const tbRegion = tbRegionEl ? tbRegionEl.value : \"\"; const res = await fetch(`/toolbox?trade=${encodeURIComponent(activeTrade)}${tbRegion ? \"&region=\" + encodeURIComponent(tbRegion) : \"\"}`);\n    const d = await res.json();\n    if (!res.ok) { body.innerHTML = `<p style=\"color:var(--red)\">${d.error}</p>`; return; }\n    \n    const days = [\"Sunday\",\"Monday\",\"Tuesday\",\"Wednesday\",\"Thursday\",\"Friday\",\"Saturday\"];\n    const today = days[new Date().getDay()];\n    if (dayLabel) dayLabel.textContent = `${today}'s Toolbox \u2014 ${activeTrade}`;\n\n    const riskClass = r => r===\"CRITICAL\"?\"risk-c\":r===\"HIGH\"?\"risk-h\":\"risk-m\";\n    \n    body.innerHTML = `\n      <div class=\"tb-topic\">${d.title}</div>\n      <div class=\"tb-obj\">${d.objective}</div>\n      \n      <div class=\"tb-section-label\">Hazard register</div>\n      ${d.hazards.map(h=>`\n        <div class=\"hazard-item\">\n          <span class=\"risk-pill ${riskClass(h.risk)}\">${h.risk}</span>\n          <div><div style=\"color:var(--text);margin-bottom:2px\">${h.text}</div><div style=\"color:var(--muted2)\">${h.ctrl}</div></div>\n        </div>`).join(\"\")}\n      \n      <div class=\"tb-section-label\" style=\"margin-top:10px\">Pre-start checklist</div>\n      ${d.checklist.map(c=>`<div class=\"tb-check\">${c}</div>`).join(\"\")}\n      \n      <div class=\"tb-discuss\">${d.discussion}</div>\n      <div class=\"tb-ref\">${d.ref}</div>\n      \n      <div class=\"tb-pdf-btn\" onclick=\"downloadToolboxPDF()\" id=\"tb-pdf-btn\">\n        <span id=\"tb-pdf-label\">Download printable PDF -- Pro</span>\n        <span>&#8594;</span>\n      </div>\n    `;\n  } catch(e) {\n    body.innerHTML = `<p style=\"color:var(--red)\">Error loading toolbox. Try again.</p>`;\n  }\n}\n\nasync function downloadToolboxPDF() {\n  const key = localStorage.getItem(\"ll_api_key\") || \"\";\n  const isPro = key && (isProKey(key));\n  \n  if (!isPro) {\n    // Show upgrade prompt in chat\n    closeToolbox();\n    addMsg(\"bot\", `<div class=\"upbox\">\n      <strong>Printable toolbox talks require Pro.</strong><br><br>\n      Legis-Link Pro gives you:<br>\n      &#10003; Printable PDF toolbox talk for every trade<br>\n      &#10003; Full method statement + crew attendance register<br>\n      &#10003; 1,000 queries/day + all 8 tools<br><br>\n      <a href=\"https://rickyfarmer.gumroad.com/l/Legis-LinkPro\" target=\"_blank\">Get Pro Access -- $199/year &#8594;</a><br>\n      <span style=\"font-size:10px;color:var(--muted)\">Already have a key? Tap KEY button to enter it.</span>\n    </div>`);\n    return;\n  }\n  \n  // Pro user \u2014 download PDF\n  const region = document.getElementById(\"region\").value;\n  const url = `/toolbox/pdf?trade=${encodeURIComponent(activeTrade)}&region=${encodeURIComponent(region)}&api_key=${encodeURIComponent(key)}`;\n  \n  try {\n    const res = await fetch(url);\n    if (!res.ok) {\n      const d = await res.json();\n      addMsg(\"bot\", `<div class=\"upbox\">PDF error: ${d.error || \"Please try again.\"}</div>`);\n      closeToolbox();\n      return;\n    }\n    const blob = await res.blob();\n    const a = document.createElement(\"a\");\n    a.href = URL.createObjectURL(blob);\n    a.download = `Toolbox_${activeTrade}_${region}_${new Date().toISOString().slice(0,10)}.pdf`;\n    document.body.appendChild(a);\n    a.click();\n    document.body.removeChild(a);\n  } catch(e) {\n    addMsg(\"bot\", \"PDF download failed. Please try again.\");\n    closeToolbox();\n  }\n}\n\nfunction renderQuick() {\n  const bar = document.getElementById(\"qbar\");\n  const qs  = QUICK[activeTrade] || [];\n  bar.innerHTML = \"\";\n  qs.forEach(q => {\n    const btn = document.createElement(\"button\");\n    btn.className = \"qb\";\n    btn.textContent = q;\n    btn.addEventListener(\"click\", function() { ask(this.textContent); });\n    bar.appendChild(btn);\n  });\n}\n\nfunction updateRate() {\n  const el  = document.getElementById(\"rate\");\n  const rem = FREE_LIMIT - used;\n  el.textContent = rem + \" left\";\n  el.className = \"rate-pill\" + (rem<=10?\" warn\":\"\") + (rem<=0?\" limit\":\"\");\n}\n\nfunction addMsg(role, content, meta={}) {\n  const w = document.getElementById(\"welcome\"); if(w) w.remove();\n  const msgs = document.getElementById(\"msgs\");\n  const d = document.createElement(\"div\");\n  d.className = \"msg \" + role;\n  const av = role===\"user\" ? \"U\" : \"LL\";\n  let h = `<div class=\"av\">${av}</div><div class=\"mb\">`;\n  if (meta.tool && role===\"bot\") {\n    h += `<div class=\"tool-tag\">${TOOL_LABELS[meta.tool]||meta.tool}</div>`;\n  }\n  h += `<div class=\"bbl\">${content}</div>`;\n  if (role===\"bot\" && window._llSpeak) { try { const _t=document.createElement(\"div\"); _t.innerHTML=content; const _s=(_t.textContent||\"\").trim(); if(_s){ speechSynthesis.cancel(); const _u=new SpeechSynthesisUtterance(_s.slice(0,600)); _u.rate=1.0; speechSynthesis.speak(_u);} } catch(e){} }\n  if (meta.status && role===\"bot\") {\n    const cls = meta.status===\"COMPLIANT\"?\"ok\":meta.status===\"NON_COMPLIANT\"?\"fl\":\"wn\";\n    h += `<div class=\"stag ${cls}\">&#9679; ${meta.status.replace(/_/g,\" \")}</div>`;\n  }\n  if (meta.ref) h += `<div class=\"crf\">Ref: ${meta.ref}</div>`;\n  h += \"</div>\";\n  d.innerHTML = h;\n  msgs.appendChild(d);\n  msgs.scrollTop = msgs.scrollHeight;\n}\n\nfunction addTyping() {\n  const msgs = document.getElementById(\"msgs\");\n  const d = document.createElement(\"div\");\n  d.className = \"msg bot\"; d.id = \"typing\";\n  d.innerHTML = `<div class=\"av\">LL</div><div class=\"mb\"><div class=\"bbl\"><div class=\"typing\"><span></span><span></span><span></span></div></div></div>`;\n  msgs.appendChild(d); msgs.scrollTop = msgs.scrollHeight;\n}\nfunction rmTyping() { const t=document.getElementById(\"typing\"); if(t) t.remove(); }\n\nfunction getCache(q) {\n  try { return (JSON.parse(localStorage.getItem(CACHE_KEY)||\"[]\")).find(c=>c.q.toLowerCase()===q.toLowerCase())||null; }\n  catch(e) { return null; }\n}\nfunction saveCache(q,r) {\n  try {\n    const c = JSON.parse(localStorage.getItem(CACHE_KEY)||\"[]\");\n    c.push({q,r}); if(c.length>10) c.shift();\n    localStorage.setItem(CACHE_KEY, JSON.stringify(c));\n  } catch(e) {}\n}\n\nvar pendingImage = null;\nvar pendingMediaType = \"image/jpeg\";\n\n// ── Voice: speech-in (mic) + speech-out toggle (spk). Frontend-only. ──\n(function(){\n  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;\n  const mic = document.getElementById(\"mic\");\n  const spk = document.getElementById(\"spk\");\n  // Speaker toggle (off by default)\n  window._llSpeak = false;\n  if (spk) {\n    if (!(\"speechSynthesis\" in window)) { spk.style.display=\"none\"; }\n    else spk.onclick = function(){ window._llSpeak = !window._llSpeak; spk.classList.toggle(\"active\", window._llSpeak); if(!window._llSpeak) speechSynthesis.cancel(); };\n  }\n  // Mic (speech-in). Hide if unsupported so no dead button.\n  if (!SR) { if(mic) mic.style.display=\"none\"; return; }\n  const rec = new SR(); rec.lang = \"en-GB\"; rec.interimResults=false; rec.maxAlternatives=1;\n  let listening=false;\n  if (mic) mic.onclick = function(){\n    if (listening) { rec.stop(); return; }\n    try { rec.start(); listening=true; mic.classList.add(\"active\"); } catch(e){}\n  };\n  rec.onresult = function(ev){\n    const t = ev.results[0][0].transcript;\n    const q = document.getElementById(\"q\"); if(q) q.value = t;\n    if (typeof ask===\"function\") ask(t);\n  };\n  rec.onend = function(){ listening=false; if(mic) mic.classList.remove(\"active\"); };\n  rec.onerror = function(){ listening=false; if(mic) mic.classList.remove(\"active\"); };\n})();\n\nasync function ask(question) {\n  question = (question || document.getElementById(\"q\").value).trim();\n  if (!question) return;\n\n  if (used >= FREE_LIMIT) {\n    addMsg(\"bot\", `<div class=\"upbox\">\n      <strong>You have used all ${FREE_LIMIT} free queries today.</strong><br><br>\n      Legis-Link Pro gives you:<br>\n      &#10003; 1,000 queries/day<br>\n      &#10003; All 8 tools: cable sizing, RAMS, safety checklists, material compliance, inspections<br>\n      &#10003; All regions: AU, UK, US, Canada, EU<br><br>\n      <a href=\"https://rickyfarmer.gumroad.com/l/Legis-LinkPro\" target=\"_blank\">Get Pro Access &mdash; $199/year &rarr;</a><br>\n      <span style=\"font-size:10px;color:var(--muted)\">Free queries reset at midnight UTC each day.</span>\n    </div>`);\n    return;\n  }\n\n  // Auto-detect tool unless user manually selected\n  const tool = manualTool ? activeTool : detectTool(question);\n  setActiveTool(tool, false);\n\n  document.getElementById(\"q\").value = \"\";\n  document.getElementById(\"q\").style.height = \"auto\";\n  document.getElementById(\"send\").disabled = true;\n  addMsg(\"user\", question);\n  addTyping();\n\n  const cached = !navigator.onLine ? getCache(question) : null;\n  try {\n    if (cached) {\n      rmTyping();\n      addMsg(\"bot\",\n        cached.r.result + ' <span style=\"font-size:10px;color:var(--muted)\">(cached)</span>',\n        {status:cached.r.status, ref:cached.r.code_reference, tool});\n    } else {\n      const res = await fetch(\"/api/query\", {\n        method: \"POST\",\n        headers: {\"Content-Type\":\"application/json\"},\n        body: JSON.stringify({\n          trade:   activeTrade,\n          region:  document.getElementById(\"region\").value,\n          role:    document.getElementById(\"role\").value,\n          question,\n          tool,\n          api_key:    localStorage.getItem(\"ll_api_key\") || \"\",\n          fingerprint: _fp||'',\n          image:      pendingImage || undefined,\n          media_type: pendingImage ? pendingMediaType : undefined\n        })\n      });\n      const data = await res.json();\n      rmTyping();\n      if (!res.ok) {\n        if (data.upgrade) {\n          addMsg(\"bot\", (data.error || \"This is a Pro feature.\") + \" <a href=\\\"\" + data.upgrade + \"\\\" target=\\\"_blank\\\" style=\\\"color:var(--accent);font-weight:600;text-decoration:underline\\\">Upgrade to Pro \\u2192</a>\");\n        } else {\n          addMsg(\"bot\", data.error || \"Server error. Please try again.\");\n        }\n      } else {\n        addMsg(\"bot\", data.result, {status:data.status, ref:data.code_reference, tool});\n        saveCache(question, data);\n        used++; localStorage.setItem(\"ll-used\", used); updateRate(); clearImage();\n      }\n    }\n  } catch(e) {\n    rmTyping();\n    addMsg(\"bot\", navigator.onLine ? \"Connection error. Try again.\" : \"Offline. Cached answers only.\");\n  }\n  document.getElementById(\"send\").disabled = false;\n  // Reset to auto-detect after each question\n  manualTool = false;\n}\n\n// Voice\nconst SR = window.SpeechRecognition || window.webkitSpeechRecognition;\nconst mic = document.getElementById(\"mic\");\nif (SR) {\n  const rec = new SR();\n  rec.continuous = false; rec.interimResults = true; rec.lang = \"en-AU\";\n  rec.onresult = e => {\n    const t = Array.from(e.results).map(r=>r[0].transcript).join(\"\");\n    document.getElementById(\"q\").value = t;\n    if (e.results[e.results.length-1].isFinal) { mic.classList.remove(\"on\"); ask(t); }\n  };\n  rec.onend = () => mic.classList.remove(\"on\");\n  mic.addEventListener(\"click\", () => {\n    if (mic.classList.contains(\"on\")) rec.stop();\n    else { mic.classList.add(\"on\"); rec.start(); }\n  });\n} else { mic.style.display = \"none\"; }\n\ndocument.getElementById(\"q\").addEventListener(\"keydown\", e => {\n  if (e.key===\"Enter\" && !e.shiftKey) { e.preventDefault(); ask(); }\n});\ndocument.getElementById(\"q\").addEventListener(\"input\", function() {\n  this.style.height = \"auto\";\n  this.style.height = Math.min(this.scrollHeight, 80) + \"px\";\n});\ndocument.getElementById(\"send\").addEventListener(\"click\", () => ask());\n\nwindow.addEventListener(\"online\",  () => document.body.classList.remove(\"offline\"));\nwindow.addEventListener(\"offline\", () => document.body.classList.add(\"offline\"));\nif (!navigator.onLine) document.body.classList.add(\"offline\");\nif (\"serviceWorker\" in navigator) navigator.serviceWorker.register(\"/sw.js\").catch(()=>{});\n\n\n// \u2500\u2500 Camera / photo upload \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\nfunction clearImage() {\n  pendingImage = null; pendingMediaType = \"image/jpeg\";\n  var cb = document.getElementById(\"cam\");\n  if (cb) { cb.classList.remove(\"has-image\"); cb.textContent = \"cam\"; }\n  var ii = document.getElementById(\"img-input\");\n  if (ii) ii.value = \"\";\n  var wrap = document.getElementById(\"img-preview-wrap\");\n  if (wrap) wrap.style.display = \"none\";\n  var prev = document.getElementById(\"img-preview\");\n  if (prev) prev.src = \"\";\n}\n\nvar camB = document.getElementById(\"cam\");\nvar imgI = document.getElementById(\"img-input\");\nif (camB && imgI) {\n  camB.addEventListener(\"click\", function() { imgI.click(); });\n  imgI.addEventListener(\"change\", function() {\n    var file = this.files[0]; if (!file) return;\n    pendingMediaType = file.type || \"image/jpeg\";\n    var reader = new FileReader();\n    reader.onload = function(e) {\n      pendingImage = e.target.result.split(\",\")[1];\n      camB.classList.add(\"has-image\"); camB.textContent = \"img\";\n      var wrap = document.getElementById(\"img-preview-wrap\");\n      var prev = document.getElementById(\"img-preview\");\n      if (wrap && prev) { prev.src = e.target.result; wrap.style.display = \"block\"; }\n      document.querySelectorAll(\".tool-btn\").forEach(function(b) { b.classList.remove(\"active\"); });\n      var pb = document.querySelector('[data-tool=\"visual_compliance\"]');\n      if (pb) { pb.classList.add(\"active\"); activeTool = \"visual_compliance\"; manualTool = true; }\n      document.getElementById(\"q\").placeholder = \"Ask about this photo \u2014 is this installation compliant?\";\n    };\n    reader.readAsDataURL(file);\n  });\n}\n\nfunction isProKey(k) { return /^ll_p_/.test(k) || /^[0-9A-F]{8}(-[0-9A-F]{8}){3}$/i.test(k); }\n// LL-12: start on the visitor's own region (saved choice, else device time zone / language), not always NSW\n(function () {\n  var sel = document.getElementById('region');\n  if (!sel) return;\n  function has(v) { for (var i = 0; i < sel.options.length; i++) { if (sel.options[i].value === v) return true; } return false; }\n  var pick = '';\n  try { pick = localStorage.getItem('ll_region') || ''; } catch (e) {}\n  if (!has(pick)) {\n    pick = '';\n    var tz = '', lang = '';\n    try { tz = Intl.DateTimeFormat().resolvedOptions().timeZone || ''; } catch (e) {}\n    try { lang = (navigator.language || '').toLowerCase(); } catch (e) {}\n    var TZ = {\n      'Australia/Sydney': 'NSW', 'Australia/Broken_Hill': 'NSW', 'Australia/Canberra': 'ACT', 'Australia/ACT': 'ACT',\n      'Australia/Melbourne': 'VIC', 'Australia/Brisbane': 'QLD', 'Australia/Lindeman': 'QLD', 'Australia/Perth': 'WA',\n      'Australia/Adelaide': 'SA', 'Australia/Hobart': 'TAS', 'Australia/Darwin': 'NT',\n      'Europe/London': 'England', 'Europe/Belfast': 'Northern Ireland', 'Europe/Dublin': 'Ireland',\n      'Europe/Berlin': 'Germany', 'Europe/Paris': 'France', 'Europe/Amsterdam': 'Netherlands', 'Europe/Madrid': 'Spain',\n      'Europe/Rome': 'Italy', 'Europe/Brussels': 'Belgium', 'Europe/Vienna': 'Austria', 'Europe/Copenhagen': 'Denmark',\n      'Europe/Stockholm': 'Sweden', 'Europe/Helsinki': 'Finland', 'Europe/Lisbon': 'Portugal', 'Europe/Warsaw': 'Poland',\n      'Europe/Prague': 'Czech Republic',\n      'America/Toronto': 'Ontario', 'America/Vancouver': 'British Columbia', 'America/Edmonton': 'Alberta',\n      'America/Winnipeg': 'Manitoba', 'America/Regina': 'Saskatchewan', 'America/Halifax': 'Nova Scotia',\n      'America/Moncton': 'New Brunswick', 'America/St_Johns': 'Newfoundland', 'America/Montreal': 'Quebec',\n      'America/New_York': 'New York', 'America/Detroit': 'Michigan', 'America/Chicago': 'Texas',\n      'America/Denver': 'Colorado', 'America/Boise': 'Idaho', 'America/Phoenix': 'Arizona',\n      'America/Los_Angeles': 'California', 'America/Anchorage': 'Alaska', 'Pacific/Honolulu': 'Hawaii'\n    };\n    pick = TZ[tz] || '';\n    if (!pick && tz.indexOf('America/Indiana/') === 0) pick = 'Indiana';\n    if (!pick && tz.indexOf('America/Kentucky/') === 0) pick = 'Kentucky';\n    if (pick === 'Ontario' && lang.indexOf('fr') === 0) pick = 'Quebec';\n    if (pick === 'England' && lang === 'cy-gb') pick = 'Wales';\n    if (!pick) {\n      var L = { 'en-au': 'NSW', 'en-ie': 'Ireland', 'en-ca': 'Ontario', 'fr-ca': 'Quebec',\n                'de': 'Germany', 'fr': 'France', 'nl': 'Netherlands', 'es': 'Spain', 'it': 'Italy', 'da': 'Denmark', 'sv': 'Sweden',\n                'fi': 'Finland', 'pt': 'Portugal', 'pl': 'Poland', 'cs': 'Czech Republic' };\n      pick = L[lang] || L[lang.split('-')[0]] || '';\n    }\n  }\n  if (has(pick)) sel.value = pick;\n  sel.addEventListener('change', function () { try { localStorage.setItem('ll_region', sel.value); } catch (e) {} });\n})();\n\nrenderQuick();\nupdateRate();\nvar storedKey = localStorage.getItem(\"ll_api_key\") || \"\";\nupdateTierBadge(storedKey);\nif (storedKey && isProKey(storedKey)) {\n  document.getElementById(\"rate\").textContent = \"1000/day\";\n}\n</script>\n</body>\n</html>",
    "landing.html":  "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n  <meta charset=\"UTF-8\"/>\n  <link rel=\"icon\" href=\"/favicon.svg\" type=\"image/svg+xml\"/>\n  <link rel=\"icon\" href=\"/favicon.ico\" sizes=\"32x32\"/>\n  <link rel=\"apple-touch-icon\" href=\"/apple-touch-icon.png\"/>\n  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\"/>\n  <meta name=\"description\" content=\"Legis-Link \u2014 Ask any construction compliance question on site. Get the exact code reference instantly. Free on mobile, no install.\"/>\n  <title>Legis-Link \u2014 Compliance answers on the job site</title>\n  <link href=\"https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=DM+Sans:wght@300;400;500;600;700&display=swap\" rel=\"stylesheet\"/>\n  <style>\n    :root{\n      --bg:#0B1320;--surface:#111C2E;--surface2:#18263B;\n      --border:#22324A;--accent:#FF6A13;--accent2:#FFC72C;\n      --green:#27ae60;--amber:#f2994a;--red:#eb5757;\n      --text:#f0f4f8;--muted:#7a8fa6;--muted2:#a8bccf;\n      --mono:\"DM Mono\",monospace;--sans:\"DM Sans\",sans-serif;\n    }\n    *{box-sizing:border-box;margin:0;padding:0}\n    html{scroll-behavior:smooth}\n    body{background:var(--bg);color:var(--text);font-family:var(--sans);line-height:1.6;overflow-x:hidden}\n\n    /* \u2500\u2500 Nav \u2500\u2500 */\n    nav{\n      position:fixed;top:0;left:0;right:0;z-index:100;\n      display:flex;align-items:center;justify-content:space-between;\n      padding:16px 24px;\n      background:rgba(8,14,26,0.85);backdrop-filter:blur(12px);\n      border-bottom:1px solid rgba(28,45,66,0.6);\n    }\n    .nav-logo{font-family:var(--mono);font-size:15px;font-weight:500;color:var(--accent);letter-spacing:-0.3px}\n    .nav-logo em{color:var(--muted);font-style:normal}\n    .nav-cta{\n      background:var(--accent);color:#0B1320;border:none;\n      padding:8px 18px;border-radius:7px;font-family:var(--sans);\n      font-size:13px;font-weight:600;cursor:pointer;text-decoration:none;\n      transition:all 0.15s;\n    }\n    .nav-cta:hover{background:#E85A0C}\n\n    /* \u2500\u2500 Hero \u2500\u2500 */\n    .hero{\n      min-height:100vh;display:flex;flex-direction:column;\n      align-items:center;justify-content:center;\n      padding:100px 24px 60px;text-align:center;position:relative;overflow:hidden;\n    }\n    .hero-bg{\n      position:absolute;inset:0;\n      background:\n        radial-gradient(ellipse 80% 50% at 50% -10%, rgba(47,128,237,0.15) 0%, transparent 60%),\n        radial-gradient(ellipse 60% 40% at 80% 80%, rgba(86,204,242,0.08) 0%, transparent 50%);\n      pointer-events:none;\n    }\n    .hero-badge{\n      display:inline-flex;align-items:center;gap:6px;\n      background:rgba(47,128,237,0.1);border:1px solid rgba(47,128,237,0.25);\n      border-radius:20px;padding:5px 14px;font-size:12px;font-weight:500;\n      color:var(--accent2);margin-bottom:28px;\n      animation:fadeDown 0.6s ease both;\n    }\n    .hero-badge-dot{width:6px;height:6px;border-radius:50%;background:var(--green);animation:blink 2s infinite}\n    @keyframes blink{0%,100%{opacity:1}50%{opacity:0.3}}\n    @keyframes fadeDown{from{opacity:0;transform:translateY(-12px)}to{opacity:1;transform:translateY(0)}}\n    @keyframes fadeUp{from{opacity:0;transform:translateY(16px)}to{opacity:1;transform:translateY(0)}}\n\n    h1{\n      font-size:clamp(2.2rem,6vw,4rem);font-weight:700;line-height:1.1;\n      letter-spacing:-1.5px;max-width:720px;margin-bottom:20px;\n      animation:fadeUp 0.6s ease 0.1s both;\n    }\n    h1 span{\n      background:linear-gradient(135deg,var(--accent),var(--accent2));\n      -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;\n    }\n    .hero-sub{\n      font-size:clamp(1rem,2.5vw,1.2rem);color:var(--muted2);max-width:520px;\n      margin-bottom:40px;font-weight:300;\n      animation:fadeUp 0.6s ease 0.2s both;\n    }\n    .hero-ctas{\n      display:flex;gap:12px;flex-wrap:wrap;justify-content:center;\n      animation:fadeUp 0.6s ease 0.3s both;\n    }\n    .btn-primary{\n      background:var(--accent);color:#0B1320;border:none;\n      padding:14px 28px;border-radius:9px;font-family:var(--sans);\n      font-size:15px;font-weight:600;cursor:pointer;text-decoration:none;\n      transition:all 0.15s;display:inline-flex;align-items:center;gap:8px;\n    }\n    .btn-primary:hover{background:#E85A0C;transform:translateY(-1px)}\n    .btn-secondary{\n      background:transparent;color:var(--muted2);\n      border:1px solid var(--border);\n      padding:14px 28px;border-radius:9px;font-family:var(--sans);\n      font-size:15px;font-weight:500;cursor:pointer;text-decoration:none;\n      transition:all 0.15s;\n    }\n    .btn-secondary:hover{border-color:var(--accent);color:var(--text)}\n    .hero-note{\n      margin-top:16px;font-size:12px;color:var(--muted);\n      animation:fadeUp 0.6s ease 0.4s both;\n    }\n\n    /* \u2500\u2500 Demo preview \u2500\u2500 */\n    .demo-wrap{\n      width:100%;max-width:680px;margin:56px auto 0;\n      animation:fadeUp 0.8s ease 0.5s both;\n    }\n    .demo-bar{\n      background:var(--surface2);border:1px solid var(--border);\n      border-radius:12px 12px 0 0;padding:10px 16px;\n      display:flex;align-items:center;gap:8px;\n    }\n    .demo-dot{width:10px;height:10px;border-radius:50%}\n    .demo-url{font-family:var(--mono);font-size:11px;color:var(--muted);margin-left:8px}\n    .demo-screen{\n      background:var(--surface);border:1px solid var(--border);border-top:none;\n      border-radius:0 0 12px 12px;padding:20px;\n      display:flex;flex-direction:column;gap:12px;\n    }\n    .demo-msg{display:flex;gap:10px;align-items:flex-start}\n    .demo-msg.user{flex-direction:row-reverse}\n    .demo-av{\n      width:28px;height:28px;border-radius:8px;flex-shrink:0;\n      display:flex;align-items:center;justify-content:center;\n      font-size:11px;font-weight:600;font-family:var(--mono);\n    }\n    .demo-msg.user .demo-av{background:var(--accent);color:#0B1320}\n    .demo-msg.bot  .demo-av{background:var(--surface2);border:1px solid var(--border);color:var(--accent2)}\n    .demo-bubble{\n      max-width:85%;padding:10px 14px;border-radius:10px;font-size:13px;line-height:1.6;\n    }\n    .demo-msg.user .demo-bubble{background:linear-gradient(135deg,var(--accent),#E85A0C);color:#0B1320;border-bottom-right-radius:3px}\n    .demo-msg.bot  .demo-bubble{background:var(--surface2);border:1px solid var(--border);border-bottom-left-radius:3px}\n    .demo-tag{\n      display:inline-flex;align-items:center;gap:4px;\n      font-family:var(--mono);font-size:10px;padding:2px 8px;\n      border-radius:4px;margin-top:4px;\n      background:rgba(39,174,96,0.12);color:var(--green);\n    }\n    .demo-ref{font-family:var(--mono);font-size:10px;color:var(--muted);margin-top:3px}\n\n    /* \u2500\u2500 Pain section \u2500\u2500 */\n    .section{padding:80px 24px;max-width:1100px;margin:0 auto}\n    .section-label{\n      font-family:var(--mono);font-size:11px;font-weight:500;\n      color:var(--accent);letter-spacing:0.12em;text-transform:uppercase;\n      margin-bottom:12px;\n    }\n    h2{font-size:clamp(1.6rem,4vw,2.4rem);font-weight:700;letter-spacing:-0.8px;line-height:1.2;margin-bottom:16px}\n    h2 span{color:var(--accent)}\n    .section-sub{font-size:1.05rem;color:var(--muted2);max-width:560px;margin-bottom:48px}\n\n    .pain-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px}\n    .pain-card{\n      background:var(--surface);border:1px solid var(--border);\n      border-radius:12px;padding:24px;position:relative;overflow:hidden;\n    }\n    .pain-card::before{\n      content:'';position:absolute;top:0;left:0;right:0;height:2px;\n      background:linear-gradient(90deg,var(--red),transparent);\n    }\n    .pain-icon{font-size:24px;margin-bottom:12px}\n    .pain-card h3{font-size:15px;font-weight:600;margin-bottom:8px}\n    .pain-card p{font-size:13px;color:var(--muted2);line-height:1.6}\n    .pain-card .solution{\n      margin-top:12px;padding-top:12px;border-top:1px solid var(--border);\n      font-size:12px;color:var(--green);display:flex;align-items:center;gap:6px;\n    }\n\n    /* \u2500\u2500 Features \u2500\u2500 */\n    .features-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:20px}\n    .feat-card{\n      background:var(--surface);border:1px solid var(--border);\n      border-radius:12px;padding:28px;transition:border-color 0.2s;\n    }\n    .feat-card:hover{border-color:var(--accent)}\n    .feat-num{\n      font-family:var(--mono);font-size:11px;color:var(--accent);\n      background:rgba(47,128,237,0.1);border-radius:4px;\n      padding:2px 8px;display:inline-block;margin-bottom:14px;\n    }\n    .feat-card h3{font-size:16px;font-weight:600;margin-bottom:8px}\n    .feat-card p{font-size:13px;color:var(--muted2);line-height:1.6}\n\n    /* \u2500\u2500 Trades strip \u2500\u2500 */\n    .trades-strip{\n      background:var(--surface);border-top:1px solid var(--border);border-bottom:1px solid var(--border);\n      padding:24px;overflow:hidden;\n    }\n    .trades-label{text-align:center;font-family:var(--mono);font-size:11px;color:var(--muted);letter-spacing:0.1em;text-transform:uppercase;margin-bottom:16px}\n    .trades-scroll{\n      display:flex;gap:12px;animation:scroll 20s linear infinite;width:max-content;\n    }\n    .trade-chip{\n      background:var(--surface2);border:1px solid var(--border);\n      border-radius:20px;padding:6px 16px;font-size:12px;font-weight:500;\n      color:var(--muted2);white-space:nowrap;flex-shrink:0;\n    }\n    @keyframes scroll{from{transform:translateX(0)}to{transform:translateX(-50%)}}\n\n    /* \u2500\u2500 Regions \u2500\u2500 */\n    .regions-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}\n    .region-card{\n      background:var(--surface);border:1px solid var(--border);\n      border-radius:10px;padding:16px 20px;\n    }\n    .region-flag{font-size:22px;margin-bottom:8px}\n    .region-card h4{font-size:13px;font-weight:600;margin-bottom:4px}\n    .region-card p{font-size:11px;color:var(--muted);font-family:var(--mono)}\n\n    /* \u2500\u2500 Pricing \u2500\u2500 */\n    .pricing-grid{display:grid;grid-template-columns:1fr 1fr;gap:20px;max-width:700px;margin:0 auto}\n    @media(max-width:580px){.pricing-grid{grid-template-columns:1fr}}\n    .price-card{\n      background:var(--surface);border:1px solid var(--border);\n      border-radius:14px;padding:28px;\n    }\n    .price-card.featured{\n      border-color:var(--accent);\n      background:linear-gradient(160deg,rgba(47,128,237,0.06),var(--surface));\n      position:relative;\n    }\n    .price-card.featured::before{\n      content:'MOST POPULAR';position:absolute;top:-1px;left:50%;transform:translateX(-50%);\n      background:var(--accent);color:#0B1320;font-size:10px;font-weight:700;\n      padding:3px 12px;border-radius:0 0 6px 6px;letter-spacing:0.08em;\n    }\n    .price-tier{font-family:var(--mono);font-size:11px;color:var(--muted);letter-spacing:0.1em;text-transform:uppercase;margin-bottom:12px}\n    .price-amount{font-size:2.4rem;font-weight:700;letter-spacing:-1px;line-height:1}\n    .price-period{font-size:13px;color:var(--muted);font-weight:400}\n    .price-desc{font-size:13px;color:var(--muted2);margin:12px 0 20px}\n    .price-features{list-style:none;display:flex;flex-direction:column;gap:8px;margin-bottom:24px}\n    .price-features li{font-size:13px;color:var(--muted2);display:flex;align-items:center;gap:8px}\n    .price-features li::before{content:'\u2713';color:var(--green);font-weight:700;flex-shrink:0}\n    .price-features li.locked::before{content:'\u2192';color:var(--accent)}\n    .price-btn{\n      display:block;text-align:center;padding:12px;border-radius:8px;\n      font-size:14px;font-weight:600;text-decoration:none;transition:all 0.15s;\n    }\n    .price-btn.free{background:var(--surface2);border:1px solid var(--border);color:var(--muted2)}\n    .price-btn.free:hover{border-color:var(--accent);color:var(--text)}\n    .price-btn.pro{background:var(--accent);color:#0B1320}\n    .price-btn.pro:hover{background:#E85A0C}\n\n    /* \u2500\u2500 CTA banner \u2500\u2500 */\n    .cta-banner{\n      background:linear-gradient(135deg,rgba(47,128,237,0.12),rgba(86,204,242,0.06));\n      border:1px solid rgba(47,128,237,0.2);\n      border-radius:16px;padding:48px 32px;text-align:center;\n      max-width:700px;margin:0 auto;\n    }\n    .cta-banner h2{margin-bottom:12px}\n    .cta-banner p{color:var(--muted2);margin-bottom:32px}\n\n    /* \u2500\u2500 Footer \u2500\u2500 */\n    footer{\n      border-top:1px solid var(--border);padding:32px 24px;\n      text-align:center;color:var(--muted);font-size:12px;\n    }\n    footer a{color:var(--accent);text-decoration:none}\n\n    /* \u2500\u2500 Divider \u2500\u2500 */\n    .divider{border:none;border-top:1px solid var(--border);margin:0}\n\n    @media(max-width:640px){\n      .hero{padding:80px 20px 48px}\n      h1{font-size:2rem;letter-spacing:-0.8px}\n      .hero-ctas{flex-direction:column;align-items:center}\n      .btn-primary,.btn-secondary{width:100%;text-align:center;justify-content:center}\n      .section{padding:56px 20px}\n    }\n  </style>\n</head>\n<body>\n\n<nav>\n  <div class=\"nav-logo\" style=\"display:flex;align-items:center;gap:10px\"><img src=\"/icon.svg\" alt=\"\" width=\"32\" height=\"32\" style=\"border-radius:8px\"/><img src=\"/wordmark-light.svg\" alt=\"Legis-Link\" height=\"20\"/></div>\n  <a href=\"https://legis-link.com/app\" class=\"nav-cta\">Try free &rarr;</a>\n</nav>\n\n<!-- Hero -->\n<section class=\"hero\">\n  <div class=\"hero-bg\"></div>\n  <div class=\"hero-badge\">\n    <span class=\"hero-badge-dot\"></span>\n    Live &mdash; 645 compliance checks today\n  </div>\n  <h1>The code reference is in your pocket.<br><span>Not in the truck.</span></h1>\n  <p class=\"hero-sub\">Ask any compliance question in plain English. Get the exact standard, clause, and table reference in seconds. Free on mobile &mdash; no app, no account.</p>\n  <div class=\"hero-ctas\">\n    <a href=\"https://legis-link.com/app\" class=\"btn-primary\">\n      Try it free &rarr;\n    </a>\n    <a href=\"https://rickyfarmer.gumroad.com/l/Legis-LinkPro\" class=\"btn-secondary\">\n      Get Pro &mdash; $199/yr\n    </a>\n  </div>\n  <p class=\"hero-note\">Free tier: 50 queries/day &bull; No signup &bull; Works on any phone</p>\n\n  <!-- Demo preview -->\n  <div class=\"demo-wrap\">\n    <div class=\"demo-bar\">\n      <div class=\"demo-dot\" style=\"background:#eb5757\"></div>\n      <div class=\"demo-dot\" style=\"background:#f2994a\"></div>\n      <div class=\"demo-dot\" style=\"background:#27ae60\"></div>\n      <span class=\"demo-url\">legis-link.com/app</span>\n    </div>\n    <div class=\"demo-screen\">\n      <div class=\"demo-msg user\">\n        <div class=\"demo-av\">U</div>\n        <div>\n          <div class=\"demo-bubble\">Wire size for 45A load, 30 metres, 240V single phase NSW</div>\n        </div>\n      </div>\n      <div class=\"demo-msg bot\">\n        <div class=\"demo-av\">LL</div>\n        <div>\n          <div class=\"demo-bubble\">\n            10mm&sup2; copper conductor required. Working: 45A continuous load at 30m on 240V single phase gives 1.97% voltage drop &mdash; within the 3% sub-circuit limit. Installation method (clipped direct, in conduit) may require derating to 16mm&sup2; if bundled.\n          </div>\n          <div class=\"demo-tag\">&#9679; COMPLIANT</div>\n          <div class=\"demo-ref\">Ref: AS/NZS 3008.1.1:2017 Table C1, AS/NZS 3000:2018 Clause 3.6.2</div>\n        </div>\n      </div>\n      <div class=\"demo-msg user\">\n        <div class=\"demo-av\">U</div>\n        <div>\n          <div class=\"demo-bubble\">Generate a RAMS for electrical switchboard installation</div>\n        </div>\n      </div>\n      <div class=\"demo-msg bot\">\n        <div class=\"demo-av\">LL</div>\n        <div>\n          <div class=\"demo-bubble\">\n            <strong>RAMS &mdash; Electrical Switchboard Installation NSW</strong><br>\n            Hazard Register: Electric shock (Severity 5, Likelihood 4, Risk 20 CRITICAL) &mdash; Isolate and LOTO main supply, test with approved voltage detector, wear insulated PPE Category 4...<br>\n            <span style=\"color:var(--accent);font-size:11px\">PRO tool &mdash; full document continues...</span>\n          </div>\n          <div class=\"demo-tag\">&#9679; COMPLIANT</div>\n          <div class=\"demo-ref\">Ref: WHS Act 2011 (NSW), AS/NZS 3000:2018, Electrical Safety Code</div>\n        </div>\n      </div>\n    </div>\n  </div>\n</section>\n\n<hr class=\"divider\"/>\n\n<!-- Trades strip -->\n<div class=\"trades-strip\">\n  <div class=\"trades-label\">Covers every trade</div>\n  <div style=\"overflow:hidden\">\n    <div class=\"trades-scroll\">\n      <div class=\"trade-chip\">Electrical</div>\n      <div class=\"trade-chip\">Plumbing</div>\n      <div class=\"trade-chip\">HVAC</div>\n      <div class=\"trade-chip\">Gas fitting</div>\n      <div class=\"trade-chip\">Welding</div>\n      <div class=\"trade-chip\">Solar / Battery</div>\n      <div class=\"trade-chip\">Fire protection</div>\n      <div class=\"trade-chip\">Carpentry</div>\n      <div class=\"trade-chip\">Concrete</div>\n      <div class=\"trade-chip\">Roofing</div>\n      <div class=\"trade-chip\">Electrical</div>\n      <div class=\"trade-chip\">Plumbing</div>\n      <div class=\"trade-chip\">HVAC</div>\n      <div class=\"trade-chip\">Gas fitting</div>\n      <div class=\"trade-chip\">Welding</div>\n      <div class=\"trade-chip\">Solar / Battery</div>\n      <div class=\"trade-chip\">Fire protection</div>\n      <div class=\"trade-chip\">Carpentry</div>\n      <div class=\"trade-chip\">Concrete</div>\n      <div class=\"trade-chip\">Roofing</div>\n    </div>\n  </div>\n</div>\n\n<hr class=\"divider\"/>\n\n<!-- Pain points -->\n<section class=\"section\">\n  <div class=\"section-label\">The problem</div>\n  <h2>You&rsquo;re on site. The <span>answer is 400km away.</span></h2>\n  <p class=\"section-sub\">Every tradesperson knows this moment. Work stops while you dig through a standard you don&rsquo;t have on you.</p>\n  <div class=\"pain-grid\">\n    <div class=\"pain-card\">\n      <div class=\"pain-icon\">&#128218;</div>\n      <h3>The standard is at the office</h3>\n      <p>AS/NZS 3000 is 700 pages. You need clause 3.6.2. It&rsquo;s not in your head and not on your phone.</p>\n      <div class=\"solution\">&#10003; Legis-Link gives you the exact clause in 3 seconds</div>\n    </div>\n    <div class=\"pain-card\">\n      <div class=\"pain-icon\">&#128273;</div>\n      <h3>Wrong cable = rework</h3>\n      <p>Ordering 2.5mm&sup2; when the job needed 4mm&sup2; costs more than the cable. It costs a day&rsquo;s work.</p>\n      <div class=\"solution\">&#10003; Material compliance check before you order</div>\n    </div>\n    <div class=\"pain-card\">\n      <div class=\"pain-icon\">&#128203;</div>\n      <h3>RAMS takes 3 hours</h3>\n      <p>A Risk Assessment and Method Statement used to mean clearing your afternoon. Not anymore.</p>\n      <div class=\"solution\">&#10003; Full RAMS document generated in 60 seconds</div>\n    </div>\n    <div class=\"pain-card\">\n      <div class=\"pain-icon\">&#128161;</div>\n      <h3>Inspectors ask questions you weren&rsquo;t expecting</h3>\n      <p>The hold point you missed. The certificate you didn&rsquo;t know was mandatory. The form number you never heard of.</p>\n      <div class=\"solution\">&#10003; Every hold point, every certificate, every form number</div>\n    </div>\n  </div>\n</section>\n\n<hr class=\"divider\"/>\n\n<!-- Features -->\n<section class=\"section\">\n  <div class=\"section-label\">What it does</div>\n  <h2>8 tools. <span>Every trade. Every region.</span></h2>\n  <p class=\"section-sub\">From a quick code lookup to a full RAMS document &mdash; all from your phone on site.</p>\n  <div class=\"features-grid\">\n    <div class=\"feat-card\">\n      <div class=\"feat-num\">FREE</div>\n      <h3>Compliance check</h3>\n      <p>Ask whether any installation, material, or practice is compliant. Get the exact standard and clause that answers it. COMPLIANT / NON_COMPLIANT / REQUIRES VERIFICATION.</p>\n    </div>\n    <div class=\"feat-card\">\n      <div class=\"feat-num\">FREE</div>\n      <h3>Code reference lookup</h3>\n      <p>Need the specific table, clause or section? Get the standard name, edition year, and what that clause actually requires &mdash; in plain English.</p>\n    </div>\n    <div class=\"feat-card\">\n      <div class=\"feat-num\">PRO</div>\n      <h3>Technical calculations</h3>\n      <p>Cable sizing, pipe sizing, voltage drop, HVAC loads. Enter your parameters, get the answer with the calculation shown and the code reference cited.</p>\n    </div>\n    <div class=\"feat-card\">\n      <div class=\"feat-num\">PRO</div>\n      <h3>Safety checklist</h3>\n      <p>15-item safety checklists with PPE requirements, hazard controls, and the specific regulation that mandates each item. Foreman-ready in under a minute.</p>\n    </div>\n    <div class=\"feat-card\">\n      <div class=\"feat-num\">PRO</div>\n      <h3>RAMS generator</h3>\n      <p>Full Risk Assessment and Method Statement: Hazard Register with risk ratings, numbered Method Statement, required qualifications. Print-ready, site-file ready.</p>\n    </div>\n    <div class=\"feat-card\">\n      <div class=\"feat-num\">PRO</div>\n      <h3>Inspection requirements</h3>\n      <p>Every hold point, who signs off, what certificate is issued, which form number, how much notice. Never cover work before a mandatory inspection again.</p>\n    </div>\n  </div>\n</section>\n\n<hr class=\"divider\"/>\n\n<!-- Regions -->\n<section class=\"section\">\n  <div class=\"section-label\">Coverage</div>\n  <h2>Your jurisdiction. <span>Your standard.</span></h2>\n  <p class=\"section-sub\">Not a one-size-fits-all approximation &mdash; the correct code for where you&rsquo;re standing.</p>\n  <div class=\"regions-grid\">\n    <div class=\"region-card\">\n      <div class=\"region-flag\">&#127462;&#127482;</div>\n      <h4>Australia</h4>\n      <p>AS/NZS 3000, AS/NZS 3008<br>NCC 2022, WHS Acts<br>NSW, VIC, QLD, WA, SA</p>\n    </div>\n    <div class=\"region-card\">\n      <div class=\"region-flag\">&#127468;&#127463;</div>\n      <h4>United Kingdom</h4>\n      <p>BS 7671:2018, CDM 2015<br>HSE guidance, Gas Safe<br>England, Scotland, Wales</p>\n    </div>\n    <div class=\"region-card\">\n      <div class=\"region-flag\">&#127482;&#127480;</div>\n      <h4>United States</h4>\n      <p>NEC NFPA 70, IBC<br>OSHA 29 CFR 1926<br>TX, CA, FL, NY, IL</p>\n    </div>\n    <div class=\"region-card\">\n      <div class=\"region-flag\">&#127464;&#127462;</div>\n      <h4>Canada</h4>\n      <p>CEC CSA C22.1, NBC<br>NPC, Provincial codes<br>ON, BC, AB, QC</p>\n    </div>\n    <div class=\"region-card\">\n      <div class=\"region-flag\">&#127466;&#127482;</div>\n      <h4>European Union</h4>\n      <p>EN standards<br>IEC 60364, EN 806<br>DE, FR, NL, IE, ES</p>\n    </div>\n  </div>\n</section>\n\n<hr class=\"divider\"/>\n\n<!-- Pricing -->\n<section class=\"section\" style=\"text-align:center\">\n  <div class=\"section-label\">Pricing</div>\n  <h2>Start free. <span>Upgrade when you need more.</span></h2>\n  <p class=\"section-sub\" style=\"margin:0 auto 48px\">No credit card for free tier. Pro unlocks all 8 tools and 1,000 queries/day.</p>\n  <div class=\"pricing-grid\">\n    <div class=\"price-card\">\n      <div class=\"price-tier\">Free</div>\n      <div class=\"price-amount\">$0 <span class=\"price-period\">forever</span></div>\n      <div class=\"price-desc\">No account needed. Open your browser and go.</div>\n      <ul class=\"price-features\">\n        <li>50 queries/day</li>\n        <li>Compliance check</li>\n        <li>Code reference lookup</li>\n        <li>List supported regions</li>\n        <li class=\"locked\" style=\"color:var(--muted)\">Technical calculations</li>\n        <li class=\"locked\" style=\"color:var(--muted)\">Safety checklists</li>\n        <li class=\"locked\" style=\"color:var(--muted)\">RAMS generator</li>\n        <li class=\"locked\" style=\"color:var(--muted)\">Inspection requirements</li>\n      </ul>\n      <a href=\"https://legis-link.com/app\" class=\"price-btn free\">Try free &rarr;</a>\n    </div>\n    <div class=\"price-card featured\">\n      <div class=\"price-tier\">Pro</div>\n      <div class=\"price-amount\">$199 <span class=\"price-period\">/ year</span></div>\n      <div class=\"price-desc\">All 8 tools. 1,000 queries/day. Every region.</div>\n      <ul class=\"price-features\">\n        <li>1,000 queries/day</li>\n        <li>All 3 free tools</li>\n        <li>Technical calculations</li>\n        <li>Safety checklists</li>\n        <li>RAMS generator</li>\n        <li>Inspection requirements</li>\n        <li>Material compliance check</li>\n        <li>All regions unlocked</li>\n      </ul>\n      <a href=\"https://rickyfarmer.gumroad.com/l/Legis-LinkPro\" class=\"price-btn pro\">Get Pro &rarr;</a>\n    </div>\n  </div>\n</section>\n\n<hr class=\"divider\"/>\n\n<!-- Final CTA -->\n<section class=\"section\">\n  <div class=\"cta-banner\">\n    <h2>Open it on your phone.<br><span>Right now.</span></h2>\n    <p>No download. No account. Takes 5 seconds. Ask your first compliance question for free.</p>\n    <div class=\"hero-ctas\">\n      <a href=\"https://legis-link.com/app\" class=\"btn-primary\">\n        Open Legis-Link &rarr;\n      </a>\n    </div>\n    <p style=\"margin-top:16px;font-size:11px;color:var(--muted)\">\n      Free &bull; No signup &bull; Works on any phone &bull; AU &bull; UK &bull; US &bull; CA &bull; EU\n    </p>\n  </div>\n</section>\n\n<footer>\n  <p>Legis-Link &mdash; Construction Compliance AI &bull; <a href=\"https://legis-link.com/app\">App</a> &bull; <a href=\"https://legis-link.com/connect\">Connect</a> &bull; <a href=\"https://rickyfarmer.gumroad.com/l/Legis-LinkPro\">Pro</a></p>\n  <p style=\"margin-top:8px;font-size:11px\">Results are preliminary estimates. Always verify against the full published standard before proceeding with any installation.</p>\n</footer>\n\n\n\n    <!-- Static SEO content — visible to Google without JS -->\n    <section style=\"max-width:800px;margin:0 auto;padding:40px 20px;font-family:system-ui,sans-serif;color:#e6edf3\">\n      <h2 style=\"font-size:22px;font-weight:600;color:#fff;margin-bottom:16px\">\n        Free Construction Compliance Checker for UK &amp; EU Trades\n      </h2>\n      <p style=\"font-size:15px;color:#94a3b8;line-height:1.7;margin-bottom:16px\">\n        Legis-Link is a free AI-powered construction compliance tool for electricians,\n        plumbers, HVAC engineers, welders, and site managers in the UK and EU.\n        Check compliance with Part P, BS 7671, WAHR 2005, CDM 2015, CAR 2012,\n        and 50+ other UK building regulations — no signup required.\n      </p>\n      <h3 style=\"font-size:16px;color:#e6edf3;margin:20px 0 10px\">Free features (no account needed)</h3>\n      <ul style=\"color:#94a3b8;padding-left:20px;line-height:2\">\n        <li>Compliance check — describe your job, get instant regulatory assessment</li>\n        <li>Code reference — exact clause lookup across UK and EU regulations</li>\n        <li>Toolbox talks — 70 topics across 10 trades, daily rotation</li>\n      </ul>\n      <h3 style=\"font-size:16px;color:#e6edf3;margin:20px 0 10px\">Pro features (£199/year)</h3>\n      <ul style=\"color:#94a3b8;padding-left:20px;line-height:2\">\n        <li>RAMS generator — Risk Assessment &amp; Method Statement templates</li>\n        <li>Technical calculations — cable sizing, voltage drop, pipe sizing</li>\n        <li>Safety checklists, material compliance, inspection requirements</li>\n        <li>Visual compliance — photo upload for site checks</li>\n      </ul>\n      <h3 style=\"font-size:16px;color:#e6edf3;margin:20px 0 10px\">Regulations covered</h3>\n      <p style=\"color:#94a3b8;font-size:14px;line-height:1.7\">\n        Part P Building Regulations · BS 7671:2018 (IET Wiring Regulations) ·\n        Working at Height Regulations 2005 (WAHR 2005) ·\n        Control of Asbestos Regulations 2012 (CAR 2012) ·\n        Construction Design and Management Regulations 2015 (CDM 2015) ·\n        Building Safety Act 2022 · Gas Safety Regulations 1998 ·\n        Water Supply (Water Fittings) Regulations 1999 ·\n        F-Gas Regulations 2015 · Fire Safety Act 2021\n      </p>\n      <p style=\"margin-top:20px\">\n        <a href=\"/app\" style=\"background:#E85A0C;color:#fff;padding:10px 20px;border-radius:6px;text-decoration:none;font-weight:600;font-size:14px\">\n          Open free compliance checker &rarr;\n        </a>\n      </p>\n    </section></body>\n</html>\n",
    "connect.html":  "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n  <meta charset=\"UTF-8\"/>\n  <link rel=\"icon\" href=\"/favicon.svg\" type=\"image/svg+xml\"/>\n  <link rel=\"icon\" href=\"/favicon.ico\" sizes=\"32x32\"/>\n  <link rel=\"apple-touch-icon\" href=\"/apple-touch-icon.png\"/>\n  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\"/>\n  <title>Connect to Legis-Link MCP</title>\n  <link href=\"https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@300;400;500;600&display=swap\" rel=\"stylesheet\"/>\n  <style>\n    :root { --bg:#0B1320; --surface:#111C2E; --border:#22324A; --accent:#FF6A13; --accent2:#FFC72C; --text:#f1f5f9; --muted:#64748b; --green:#10b981; --mono:'IBM Plex Mono',monospace; --sans:'IBM Plex Sans',sans-serif; }\n    * { box-sizing:border-box; margin:0; padding:0; }\n    body { background:var(--bg); color:var(--text); font-family:var(--sans); min-height:100vh; padding:24px; max-width:640px; margin:0 auto; }\n    h1 { font-family:var(--mono); font-size:22px; color:var(--accent); margin-bottom:6px; }\n    .sub { color:var(--muted); font-size:14px; margin-bottom:32px; }\n    h2 { font-size:15px; font-weight:600; margin-bottom:12px; margin-top:28px; }\n    .card { background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:18px 20px; margin-bottom:12px; }\n    .card-title { font-weight:600; font-size:14px; margin-bottom:8px; display:flex; align-items:center; gap:8px; }\n    .card-body { color:var(--muted); font-size:13px; line-height:1.7; }\n    .code-block { background:var(--bg); border:1px solid var(--border); border-radius:8px; padding:12px 14px; font-family:var(--mono); font-size:12px; color:var(--accent2); margin:10px 0; overflow-x:auto; white-space:pre; }\n    .badge { display:inline-block; font-size:10px; font-weight:600; padding:2px 8px; border-radius:4px; margin-left:6px; }\n    .badge-free { background:rgba(16,185,129,0.15); color:var(--green); }\n    .badge-now { background:rgba(255,106,19,0.15); color:var(--accent); }\n    .divider { border:none; border-top:1px solid var(--border); margin:28px 0; }\n    a { color:var(--accent); }\n    .back { display:inline-flex; align-items:center; gap:6px; color:var(--muted); font-size:13px; text-decoration:none; margin-bottom:24px; }\n    .back:hover { color:var(--text); }\n  </style>\n</head>\n<body>\n  <a href=\"/app\" class=\"back\">\u2190 Back to field tool</a>\n  <h1 style=\"display:flex;align-items:center;gap:10px\"><img src=\"/icon.svg\" alt=\"\" width=\"34\" height=\"34\" style=\"border-radius:9px\"/>Connect Legis-Link</h1>\n  <p class=\"sub\">Use Legis-Link from any MCP-compatible AI tool \u2014 Claude Desktop, Cursor, Windsurf, or the mobile apps below.</p>\n\n  <h2>🤖 Claude, ChatGPT and other AI apps</h2>\n\n  <div class=\"card\">\n    <div class=\"card-title\">Claude (web, desktop, mobile) <span class=\"badge badge-now\">Recommended</span></div>\n    <div class=\"card-body\">\n      Settings → Connectors → Add custom connector. Name: Legis-Link. URL:\n      <div class=\"code-block\">https://legis-link.com/mcp</div>\n      No sign-in needed (free tier, 50 questions a day). Pro users add their key to the end of the URL:\n      <div class=\"code-block\">https://legis-link.com/mcp?key=ll_p_your_key</div>\n    </div>\n  </div>\n\n  <div class=\"card\">\n    <div class=\"card-title\">Cursor, Windsurf, VS Code and other MCP clients</div>\n    <div class=\"card-body\">\n      Use the streamable HTTP address:\n      <div class=\"code-block\">{ \"mcpServers\": { \"legis-link\": { \"url\": \"https://legis-link.com/mcp\" } } }</div>\n      Older clients that only support SSE can still use https://legis-link.com/sse\n    </div>\n  </div>\n\n  <h2>📱 Mobile — Recommended</h2>\n\n  <div class=\"card\">\n    <div class=\"card-title\">Systemprompt MCP <span class=\"badge badge-now\">Works now</span></div>\n    <div class=\"card-body\">\n      Voice-controlled MCP client for iOS and Android. Add the server URL in app settings.\n      <div class=\"code-block\">Server URL: https://legis-link.com/sse</div>\n      Download: Search \"Systemprompt MCP\" on App Store or Google Play.\n    </div>\n  </div>\n\n  <div class=\"card\">\n    <div class=\"card-title\">Browser PWA <span class=\"badge badge-free\">Free \u00b7 No install</span></div>\n    <div class=\"card-body\">\n      Open <a href=\"/app\">/app</a> on your phone browser. Tap the share icon \u2192 \"Add to Home Screen\" for an app-like experience with offline support.\n    </div>\n  </div>\n\n  <hr class=\"divider\"/>\n  <h2>\ud83d\udda5\ufe0f Desktop</h2>\n\n  <div class=\"card\">\n    <div class=\"card-title\">Claude Desktop</div>\n    <div class=\"card-body\">\n      Add to <code style=\"font-family:var(--mono);font-size:12px\">claude_desktop_config.json</code>:\n      <div class=\"code-block\">{\n  \"mcpServers\": {\n    \"legis-link\": {\n      \"command\": \"python\",\n      \"args\": [\"legis_link_mcp_server.py\"],\n      \"env\": { \"LEGIS_LINK_API_KEY\": \"\" }\n    }\n  }\n}</div>\n    </div>\n  </div>\n\n  <div class=\"card\">\n    <div class=\"card-title\">Cursor / Windsurf</div>\n    <div class=\"card-body\">\n      Add to MCP settings \u2014 use the remote SSE endpoint:\n      <div class=\"code-block\">{\n  \"mcpServers\": {\n    \"legis-link\": {\n      \"url\": \"https://legis-link.com/sse\"\n    }\n  }\n}</div>\n    </div>\n  </div>\n\n  <hr class=\"divider\"/>\n  <h2>\ud83d\udd11 API Keys</h2>\n  <div class=\"card\">\n    <div class=\"card-body\">\n      <strong>Free tier:</strong> No key needed. Leave the API key empty (50 requests/day).<br><br>\n      <strong>Pro tier:</strong> $199/year \u2014 1,000 requests/day, all 8 tools. Contact us to get a Pro key.\n    </div>\n  </div>\n</body>\n</html>",
    "manifest.json": "{\n  \"name\": \"Legis-Link\",\n  \"short_name\": \"Legis-Link\",\n  \"description\": \"Field compliance tool for tradespeople\",\n  \"start_url\": \"/app\",\n  \"display\": \"standalone\",\n  \"orientation\": \"portrait\",\n  \"background_color\": \"#0a0f1a\",\n  \"theme_color\": \"#3b82f6\",\n  \"icons\": [\n    {\n      \"src\": \"data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 192 192'><rect width='192' height='192' rx='40' fill='%233b82f6'/><text y='130' x='96' text-anchor='middle' font-size='100' font-family='monospace' fill='white'>\u26a1</text></svg>\",\n      \"sizes\": \"192x192\",\n      \"type\": \"image/svg+xml\"\n    }\n  ]\n}",
    "sw.js":         "const CACHE_NAME = 'legis-link-v1';\nconst STATIC = ['/app', '/connect', '/manifest.json'];\n\nself.addEventListener('install', e => {\n  e.waitUntil(\n    caches.open(CACHE_NAME).then(c => c.addAll(STATIC))\n  );\n  self.skipWaiting();\n});\n\nself.addEventListener('activate', e => {\n  e.waitUntil(\n    caches.keys().then(keys =>\n      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))\n    )\n  );\n  self.clients.claim();\n});\n\nself.addEventListener('fetch', e => {\n  // API calls \u2014 network only\n  if (e.request.url.includes('/api/query')) return;\n\n  e.respondWith(\n    fetch(e.request)\n      .then(r => {\n        const clone = r.clone();\n        caches.open(CACHE_NAME).then(c => c.put(e.request, clone));\n        return r;\n      })\n      .catch(() => caches.match(e.request))\n  );\n});",
}

def _page(name: str) -> str:
    """Return embedded page content."""
    return _PAGES.get(name, "")


# ── API Key Auth (LL-1: keys are signed and checked; limits survive restarts) ──
# Pro key:  ll_p_<16 hex id><16 hex signature>  signed with LEGIS_KEY_SECRET (set in Railway only)
# Free key: ll_f_...  or no key at all = free tier
# Admin:    the exact value of LEGIS_ADMIN_KEY (Railway only)
# dev_local: Pro only when LEGIS_DEV_MODE=1 (local testing); otherwise treated as the free tier

import contextvars as _cv
import hmac as _hmac_mod
import secrets as _secrets_mod
import time as _time_mod
from contextlib import contextmanager as _ctxmgr

FREE_DAILY_LIMIT = int(os.environ.get("LEGIS_FREE_DAILY", "50"))
PRO_DAILY_LIMIT  = int(os.environ.get("LEGIS_PRO_DAILY", "1000"))

_rate_store: dict = defaultdict(int)          # fallback only, when the database is unavailable
_LL_CLIENT = _cv.ContextVar("ll_client", default="")
_DB_READY = []
_KEY_CACHE: dict = {}


def _client_ip(request) -> str:
    ip = (request.headers.get("x-forwarded-for", "") or "").split(",")[0].strip()
    if not ip and getattr(request, "client", None):
        ip = request.client.host or ""
    return ip


@_ctxmgr
def get_db():
    """A database connection that is always closed. Creates the tables once."""
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    import psycopg2
    conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
    try:
        if not _DB_READY:
            cur = conn.cursor()
            cur.execute("CREATE TABLE IF NOT EXISTS legis_link_keys (id SERIAL PRIMARY KEY,email VARCHAR(255) UNIQUE NOT NULL,"
                        "api_key VARCHAR(50),sale_id VARCHAR(100),product VARCHAR(100),active BOOLEAN DEFAULT TRUE,"
                        "created_at TIMESTAMPTZ DEFAULT NOW())")
            cur.execute("CREATE TABLE IF NOT EXISTS legis_link_free_usage (id SERIAL PRIMARY KEY, fingerprint TEXT NOT NULL,"
                        " ip_address TEXT, date_utc DATE NOT NULL DEFAULT CURRENT_DATE, query_count INTEGER DEFAULT 0,"
                        " last_seen TIMESTAMPTZ DEFAULT NOW(), UNIQUE(fingerprint, date_utc))")
            cur.execute("CREATE TABLE IF NOT EXISTS legis_link_usage (bucket TEXT NOT NULL, date_utc DATE NOT NULL,"
                        " n INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (bucket, date_utc))")
            conn.commit()
            _DB_READY.append(1)
        yield conn
    finally:
        conn.close()


def _key_sig(kind: str, ident: str) -> str:
    import hashlib as _hs
    return _hmac_mod.new(KEY_SECRET, (kind + ":" + ident).encode(), _hs.sha256).hexdigest()[:16]


def _key_signed(k: str, kind: str) -> bool:
    if not KEY_SECRET or len(k) != 37:
        return False
    ident, sig = k[5:21], k[21:]
    return _hmac_mod.compare_digest(sig, _key_sig(kind, ident))


def _key_revoked(k: str) -> bool:
    """True if the key was refunded or switched off. Cached 5 minutes. A database error never locks out a paying user."""
    now = _time_mod.time()
    hit = _KEY_CACHE.get(k)
    if hit and now - hit[0] < 300:
        return hit[1]
    revoked = False
    if DATABASE_URL:
        try:
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute("SELECT active FROM legis_link_keys WHERE api_key=%s", (k,))
                row = cur.fetchone()
                revoked = bool(row) and row[0] is False
        except Exception as e:
            logging.warning(f"Key check: {e}")
            return hit[1] if hit else False
    if len(_KEY_CACHE) > 5000:
        _KEY_CACHE.clear()
    _KEY_CACHE[k] = (now, revoked)
    return revoked


_LL_KEY = _cv.ContextVar("ll_key", default="")


def _request_key(request) -> str:
    """Optional Pro key sent with the connection: ?key=..., X-API-Key header or Authorization: Bearer ll_..."""
    k = (request.query_params.get("key", "") or request.headers.get("x-api-key", "")).strip()
    if not k:
        a = request.headers.get("authorization", "")
        if a.lower().startswith("bearer ") and (a[7:].strip().startswith("ll_") or _GUMROAD_KEY_RE.match(a[7:].strip())):
            k = a[7:].strip()
    return k[:80]


# LL-16: Gumroad license keys work as Pro keys, so buyers get access the moment they pay.
# Gumroad emails the key on the receipt; we check it with Gumroad's public verify API (no secret needed),
# using GUMROAD_PRODUCT_ID from Railway. Results are cached; a refund, chargeback or ended membership ends access.
_GUMROAD_KEY_RE = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{8}-[0-9A-Fa-f]{8}-[0-9A-Fa-f]{8}$")
_GUMROAD_CACHE = {}


def _gumroad_license_check(k: str):
    """Returns (ok, reason). Positive answers cached 6 hours, negative 10 minutes."""
    product_id = os.environ.get("GUMROAD_PRODUCT_ID", "").strip()
    if not product_id:
        return False, "Gumroad license keys are not switched on yet. Email info@legis-link.com for a Pro key."
    now = _time_mod.time()
    hit = _GUMROAD_CACHE.get(k)
    if hit and now - hit[0] < (21600 if hit[1] else 600):
        return hit[1], hit[2]
    try:
        r = httpx.post("https://api.gumroad.com/v2/licenses/verify",
                       data={"product_id": product_id, "license_key": k, "increment_uses_count": "false"}, timeout=6.0)
        d = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    except Exception as e:
        logging.warning(f"Gumroad verify failed: {e}")
        if hit:
            return hit[1], hit[2]
        return False, "Could not check your Pro key with Gumroad just now. Please try again in a minute."
    p = d.get("purchase") or {}
    ok, reason = True, "pro"
    if not d.get("success"):
        ok, reason = False, "This Pro key is not recognised. Check it was copied in full, or email info@legis-link.com."
    elif p.get("refunded") or p.get("chargebacked") or p.get("disputed"):
        ok, reason = False, "This Pro key is no longer active (the purchase was refunded)."
    else:
        ended = p.get("subscription_ended_at") or p.get("subscription_failed_at")
        if ended:
            try:
                ended_ts = datetime.fromisoformat(str(ended).replace("Z", "+00:00")).timestamp()
            except Exception:
                ended_ts = 0
            if ended_ts <= now:
                ok, reason = False, "Your Legis-Link Pro membership has ended. Renew it on Gumroad to keep Pro."
    if len(_GUMROAD_CACHE) > 5000:
        _GUMROAD_CACHE.clear()
    _GUMROAD_CACHE[k] = (now, ok, reason)
    return ok, reason


def validate_api_key(key: str | None) -> dict:
    """Validate API key. Returns {valid, tier, reason}."""
    if not key or not key.strip():
        return {"valid": True, "tier": "free", "reason": "free", "remaining": FREE_DAILY_LIMIT}
    k = key.strip()
    if ADMIN_KEY and _hmac_mod.compare_digest(k, ADMIN_KEY):
        return {"valid": True, "tier": "pro", "remaining": 999999, "reason": "admin"}
    if k == "dev_local":
        if DEV_MODE:
            return {"valid": True, "tier": "pro", "reason": "dev"}
        return {"valid": True, "tier": "free", "reason": "free"}
    if k.startswith("ll_p_"):
        if not _key_signed(k, "pro"):
            return {"valid": False, "tier": None,
                    "reason": "This Pro key is not recognised. Check it was copied in full, or email info@legis-link.com."}
        if _key_revoked(k):
            return {"valid": False, "tier": None, "reason": "This Pro key is no longer active."}
        return {"valid": True, "tier": "pro", "reason": "pro"}
    if k.startswith("ll_f_") and len(k) == 37:
        return {"valid": True, "tier": "free", "reason": "free"}
    if _GUMROAD_KEY_RE.match(k):
        ok, reason = _gumroad_license_check(k.upper())
        if ok:
            return {"valid": True, "tier": "pro", "reason": "pro"}
        return {"valid": False, "tier": None, "reason": reason}
    return {"valid": False, "tier": None,
            "reason": "Invalid key. Leave the key empty for the free tier, or paste your Pro key (the license key from your Gumroad receipt, or a key starting ll_p_)."}


def _usage_hit(bucket: str, limit: int):
    """Count one use. Returns the new count, or None when the daily limit is reached. Database first, memory if it is down."""
    if DATABASE_URL:
        try:
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute("INSERT INTO legis_link_usage (bucket, date_utc, n) VALUES (%s, (NOW() AT TIME ZONE 'UTC')::date, 1) "
                            "ON CONFLICT (bucket, date_utc) DO UPDATE SET n = legis_link_usage.n + 1 "
                            "WHERE legis_link_usage.n < %s RETURNING n", (bucket, limit))
                row = cur.fetchone()
                if _secrets_mod.randbelow(200) == 0:
                    cur.execute("DELETE FROM legis_link_usage WHERE date_utc < CURRENT_DATE - INTERVAL '30 days'")
                conn.commit()
                return row[0] if row else None
        except Exception as e:
            logging.warning(f"Usage store: {e}")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    sk = bucket + ":" + today
    if _rate_store[sk] >= limit:
        return None
    _rate_store[sk] += 1
    if len(_rate_store) > 20000:
        _rate_store.clear()
    return _rate_store[sk]


def check_rate_limit(api_key: str, tier: str, who: str = "") -> dict:
    """Check and count one request. Each key, or each anonymous visitor (by IP), has its own daily count."""
    if api_key and ADMIN_KEY and _hmac_mod.compare_digest(api_key.strip(), ADMIN_KEY):
        return {"allowed": True, "remaining": 999999, "limit": 999999}
    limit = PRO_DAILY_LIMIT if tier == "pro" else FREE_DAILY_LIMIT
    k = (api_key or "").strip()
    ident = k if (k and k != "dev_local") else "anon:" + (who or _LL_CLIENT.get() or "unknown")
    import hashlib as _hs
    n = _usage_hit(_hs.sha256(ident.encode()).hexdigest()[:32], limit)
    if n is None:
        return {"allowed": False, "remaining": 0, "limit": limit,
                "reset": "tomorrow 00:00 UTC",
                "upgrade": PRO_UPGRADE if tier == "free" else None}
    return {"allowed": True, "remaining": max(0, limit - n), "limit": limit}


def is_pro_tool(name: str) -> bool:
    return name in {
        "calculate_technical_spec", "generate_safety_checklist",
        "generate_rams", "verify_material_compliance", "get_inspection_requirements"
    }


# ── Audit Log ───────────────────────────────────────────────────────────────
# Phase 1: Log to file + optional DB
# Phase 3: Add WORM storage, cryptographic chaining

AUDIT_LOG_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "legis_link_audit.jsonl"
)

def audit_log(api_key: str, tier: str, tool: str,
              trade: str, region: str, result_status: str,
              error: str = ""):
    """Write audit entry. Non-blocking — errors are swallowed."""
    try:
        entry = {
            "ts":         datetime.now(timezone.utc).isoformat(),
            "v":          VERSION,
            "key":        api_key[:8] + "...",
            "tier":       tier,
            "tool":       tool,
            "trade":      trade,
            "region":     region,
            "status":     result_status,
            "error":      error,
            "request_id": hashlib.md5(
                f"{api_key}{tool}{datetime.now().isoformat()}".encode()
            ).hexdigest()[:8]
        }
        # File log (always)
        with open(AUDIT_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

        # DB log (if DATABASE_URL set)
        # Phase 3: replace with WORM storage
        if DATABASE_URL:
            _db_audit_log(entry)

    except Exception:
        pass  # Audit log must never crash the server


def _db_audit_log(entry: dict):
    """Write audit entry to PostgreSQL. Called only if DATABASE_URL is set."""
    try:
        import psycopg2
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS legis_link_audit (
                id SERIAL PRIMARY KEY,
                ts TIMESTAMPTZ NOT NULL,
                version VARCHAR(10),
                api_key VARCHAR(20),
                tier VARCHAR(10),
                tool VARCHAR(50),
                trade VARCHAR(50),
                region VARCHAR(50),
                status VARCHAR(30),
                error TEXT,
                request_id VARCHAR(10)
            )
        """)
        cur.execute("""
            INSERT INTO legis_link_audit
            (ts, version, api_key, tier, tool, trade, region, status, error, request_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            entry["ts"], entry["v"], entry["key"], entry["tier"],
            entry["tool"], entry["trade"], entry["region"],
            entry["status"], entry["error"], entry["request_id"]
        ))
        conn.commit()
        conn.close()
    except Exception:
        pass


# ── Future Framework Scaffold ───────────────────────────────────────────────
# This is the roadmap. Each phase has a trigger condition and implementation notes.
# When the trigger is met, implement the phase and remove it from here.

FUTURE_ROADMAP = {
    "phase_2": {
        "trigger": "10+ paying users OR payment system live",
        "what": [
            "OAuth 2.1 / OIDC — replace manual key issuance with login flow",
            "Usage dashboard — /dashboard endpoint showing requests, remaining quota",
            "Email receipts — send API key via email on payment confirmation",
            "Redis rate limiting — replace in-memory store with Redis for persistence",
            "Key rotation — allow users to regenerate their API key",
        ],
        "files_to_modify": ["legis_link_mcp_server.py"],
        "estimated_effort": "2-3 days"
    },
    "phase_3": {
        "trigger": "First enterprise client OR compliance requirement",
        "what": [
            "Row-level security — per-tenant query isolation if multi-tenant DB added",
            "Namespace partitioning — if vector DB/RAG added for custom standards",
            "WORM audit storage — S3 Object Lock for tamper-proof compliance logs",
            "Cryptographic log chaining — hash-chained entries for audit integrity",
            "SLA monitoring — uptime guarantees, incident response",
        ],
        "files_to_modify": ["legis_link_mcp_server.py", "legis_link_audit.jsonl -> S3"],
        "estimated_effort": "1-2 weeks"
    },
    "phase_4": {
        "trigger": "Regulated industry client (finance, healthcare, government)",
        "what": [
            "Firecracker microVMs — if custom tool execution is added",
            "VPC private links — if client data must stay in private network",
            "SCIM provisioning — enterprise SSO integration",
            "ReBAC authorization — graph-based permissions (OPA or SpiceDB)",
            "PII scrubber — strip sensitive data from audit logs",
        ],
        "files_to_modify": ["entire infrastructure"],
        "estimated_effort": "4-6 weeks"
    }
}


# ── Tool definitions ─────────────────────────────────────────────────────────

VALID_TRADES = [
    "Electrical", "Plumbing", "HVAC", "Welding", "Carpentry",
    "Fire protection", "Concrete", "Roofing", "Gas fitting", "Solar / Battery"
]
VALID_REGIONS = {
    "Australia": ["NSW", "VIC", "QLD", "WA", "SA", "ACT", "TAS", "NT"],
    "USA": [
        "Alabama", "Alaska", "Arizona", "Arkansas", "California",
        "Colorado", "Connecticut", "Delaware", "Florida", "Georgia",
        "Hawaii", "Idaho", "Illinois", "Indiana", "Iowa",
        "Kansas", "Kentucky", "Louisiana", "Maine", "Maryland",
        "Massachusetts", "Michigan", "Minnesota", "Mississippi", "Missouri",
        "Montana", "Nebraska", "Nevada", "New Hampshire", "New Jersey",
        "New Mexico", "New York", "North Carolina", "North Dakota", "Ohio",
        "Oklahoma", "Oregon", "Pennsylvania", "Rhode Island", "South Carolina",
        "South Dakota", "Tennessee", "Texas", "Utah", "Vermont",
        "Virginia", "Washington", "West Virginia", "Wisconsin", "Wyoming",
        "Washington DC",
    ],
    "Canada":    ["Ontario", "British Columbia", "Alberta", "Quebec",
                  "Manitoba", "Saskatchewan", "Nova Scotia", "New Brunswick",
                  "Newfoundland", "Prince Edward Island"],
    "UK":        ["England", "Scotland", "Wales", "Northern Ireland"],
    "EU":        ["Germany", "France", "Netherlands", "Ireland", "Spain",
                  "Italy", "Belgium", "Austria", "Denmark", "Sweden",
                  "Finland", "Portugal", "Poland", "Czech Republic"],
}
VALID_ROLES = ["Apprentice / Trainee", "Qualified Tradesperson", "Supervisor / Foreman", "Manager / Executive"]

# ─────────────────────────────────────────────────────────────────────────
# LICENCE_REGISTRY: verified licensing data, injected as ground truth so the
# model cites real instruments/regulators instead of recalling. Each row
# confirmed against the official regulator, current as at 2026-08. Expand by
# adding verified rows keyed (trade_lower, jurisdiction_upper).
# ─────────────────────────────────────────────────────────────────────────
LICENCE_REGISTRY = {
    ("electrical", "QLD"): {
        "law": "Electrical Safety Act 2002 (Qld) + Electrical Safety Regulation 2013, as amended (incl. Electrical Safety and Other Legislation Amendment Regulation 2024; ES Regulation current as at 1 Jan 2025 per QLD legislation register)",
        "regulator": "WorkSafe Queensland / Electrical Safety Office",
        "individual": "electrical mechanic (authorises all electrical work), electrical linesperson, electrical fitter, electrical jointer, restricted electrical work licence",
        "trainee": "electrical work training permit",
        "contractor": "electrical contractor licence (unrestricted or restricted)",
        "url": "https://www.business.qld.gov.au/industries/service-industries-professionals/electricians",
        "asat": "verified Aug 2026 against QLD legislation register + WorkSafe QLD (ES Reg 2013 current as at 1 Jan 2025)",
    },
    ("electrical", "NSW"): {
        "law": "Home Building Act 1989 (NSW) + Electricity (Consumer Safety) Act 2004",
        "regulator": "Building Commission NSW (formerly NSW Fair Trading)",
        "individual": "Qualified Supervisor Certificate (Electrical), Electrician Tradesperson Certificate",
        "trainee": "Provisional Tradesperson Certificate",
        "contractor": "Electrical Contractor Licence (business entity)",
        "url": "https://www.nsw.gov.au/departments-and-agencies/building-commission",
        "asat": "verified Aug 2026 against NSW legislation register: Home Building Act 1989 No 147 current version 20 Aug 2024 to date (updated 28 Jan 2025)",
    },
    ("electrical", "VIC"): {
        "law": "Electricity Safety Act 1998 (Vic) + Electricity Safety (Registration and Licensing) Regulations 2020",
        "regulator": "Energy Safe Victoria (ESV)",
        "individual": "Electrician's Licence (A Grade)",
        "trainee": "Supervised Worker's Licence (L)",
        "contractor": "Registered Electrical Contractor (REC)",
        "url": "https://www.esv.vic.gov.au/licensing/electricians-licences/",
        "asat": "verified Aug 2026 against legislation.vic.gov.au: Electricity Safety Act 1998 in-force version 088 from 1 Apr 2026 (+ Regs 2019/2020), per Energy Safe Victoria",
    },
    ("electrical", "WA"): {
        "law": "Electricity (Licensing) Regulations 1991 (WA) (principal instrument, still current; assessment updated Apr 2026 to AS/NZS 3000:2025)",
        "regulator": "Building and Energy / EnergySafety (DEMIRS)",
        "individual": "Electrical Worker's Licence",
        "trainee": "Electrician's Training Licence (apprentice)",
        "contractor": "Electrical Contractor licence (EC prefix)",
        "url": "https://www.wa.gov.au/organisation/service-delivery/electrical-licensing",
        "asat": "verified Aug 2026 against wa.gov.au (Electricity (Licensing) Regs 1991 current; assessment updated Apr 2026)",
    },
    ("electrical", "SA"): {
        "law": "Plumbers, Gas Fitters and Electricians Act 1995 (SA) + Regulations 2025",
        "regulator": "Office of the Technical Regulator (technical) / Consumer and Business Services (licensing)",
        "individual": "Electrical Worker's Registration (+ restricted variants)",
        "trainee": "provisional/restricted registration (supervised, gap-training)",
        "contractor": "Electrical Contractor's Licence",
        "url": "https://www.sa.gov.au/topics/business-and-trade/licensing/building-and-trades/licensing",
        "asat": "verified Aug 2026 against legislation.sa.gov.au: Plumbers, Gas Fitters and Electricians Regulations 2025 commenced 7 Aug 2025 (repealing 2010 regs); Act ss.6-7 (contractor licence), ss.13-14 (worker registration)",
    },
    ("electrical", "TAS"): {
        "law": "Occupational Licensing Act 2005 (Tas) + Electrical Work Licence Classes Determination (current)",
        "regulator": "Consumer, Building and Occupational Services (CBOS)",
        "individual": "Electrical Practitioner Licence (electrician / cable jointer / line worker), restricted electrical work licence",
        "trainee": "provisional electrical work licence",
        "contractor": "Electrical Contractor's Licence",
        "url": "https://www.cbos.tas.gov.au/topics/licensing-and-registration/licensed-occupations/electrical",
        "asat": "verified Aug 2026 against CBOS (pages updated 20 Jul 2026; Electrical Work Licence Classes Determination current)",
    },
    ("electrical", "ACT"): {
        "law": "Construction Occupations (Licensing) Act 2004 (ACT)",
        "regulator": "Access Canberra (Construction Occupations Registrar)",
        "individual": "Electrician's licence (unrestricted), restricted endorsements",
        "trainee": "Unrestricted Permit Electrotechnology Systems",
        "contractor": "Electrical Contractor licence",
        "url": "https://www.accesscanberra.act.gov.au/",
        "asat": "verified Aug 2026 against legislation.act.gov.au: Construction Occupations (Licensing) Act 2004 republished for amendment A2026-2; electrician defined s.11, licensed under Part 3, Registrar in Access Canberra",
    },
    ("electrical", "NT"): {
        "law": "Electrical Safety Act 2022 (NT) + Electrical Safety Regulations 2024 (commenced 1 Jul 2024, replacing the Electrical Workers and Contractors Act 1978)",
        "regulator": "Electrical Safety Regulator, NT WorkSafe",
        "individual": "unrestricted electrical mechanic licence (+ endorsements), restricted electrical work licence",
        "trainee": "training/supervised licence categories",
        "contractor": "Electrical Contractor Licence",
        "url": "https://worksafe.nt.gov.au/licensing-and-registration/electrical-licensing",
        "asat": "verified Aug 2026 (Electrical Safety Act 2022 + Regs 2024 commenced 1 Jul 2024, replacing 1978 Act)",
    },

    # ── PLUMBING x 8 AU jurisdictions (register/regulator-verified Aug 2026) ──
    ("plumbing", "QLD"): {
        "law": "Plumbing and Drainage Act 2018 (Qld) + Plumbing and Drainage Regulation 2019",
        "regulator": "Queensland Building and Construction Commission (QBCC)",
        "individual": "Occupational plumber and/or drainer licence (base, Cert III CPC32413); Occupational restricted plumber/drainer",
        "trainee": "Provisional licence (apprentice/trainee, supervised)",
        "contractor": "Plumbing contractor licence (+ nominee supervisor / site supervisor); endorsements: backflow, TMV, on-site sewage, solar/heat-pump HWS, fire collars",
        "url": "https://www.qbcc.qld.gov.au/licences/apply-licence/available-licences/plumbing-drainage-licences",
        "asat": "verified Aug 2026 from QBCC official site; work classified permit (Form 1)/notifiable (Form 4/4a)/minor per Plumbing and Drainage Regulation 2019",
    },
    ("plumbing", "NSW"): {
        "law": "Home Building Act 1989 (licensing) + Plumbing and Drainage Act 2011 (regulation/inspection)",
        "regulator": "Building Commission NSW (formerly NSW Fair Trading)",
        "individual": "Tradesperson Certificate; Qualified Supervisor Certificate (classes: Plumber, Drainer, Gasfitter, LP Gasfitter)",
        "trainee": "Provisional certificate (apprentices/trainees)",
        "contractor": "Contractor Licence (individual or business); categories per Home Building Regulation 2014 Schedule 4",
        "url": "https://www.nsw.gov.au/business-and-economy/licences-and-credentials/building-and-trade-licences-and-registrations/plumbing-draining-and-gasfitting",
        "asat": "verified Aug 2026 from nsw.gov.au + Fair Trading; licensing under Home Building Act 1989, on-site regulation under Plumbing and Drainage Act 2011",
    },
    ("plumbing", "VIC"): {
        "law": "Building Act 1993 Part 12A + Plumbing Regulations 2018",
        "regulator": "Building and Plumbing Commission (BPC) (replaced Victorian Building Authority 1 Jul 2025)",
        "individual": "Registered plumber (per class) or Licensed plumber (per class); classes: Water Supply, Sanitary, Drainage, Roofing(stormwater), Mechanical Services, Gasfitting, Irrigation + specialised (backflow, TMV, Type A)",
        "trainee": "Plumbing apprentice",
        "contractor": "Licensed plumber may certify work (issue compliance certificate); registration is the lower tier, licence the higher",
        "url": "https://www.bpc.vic.gov.au/plumbers/plumber-classes-and-categories",
        "asat": "verified Aug 2026 from BPC official site; BPC replaced VBA 1 Jul 2025; Plumbing Regs 2018 commenced 18 Nov 2018",
    },
    ("plumbing", "WA"): {
        "law": "Plumbers Licensing Act 1995 + Plumbers Licensing and Plumbing Standards Regulations 2000",
        "regulator": "Plumbers Licensing Board (Building and Energy, DEMIRS)",
        "individual": "Tradesperson's Licence; Tradesperson's Licence (Drainage Plumbing)",
        "trainee": "Provisional Tradesperson's Licence (1yr, overseas-trained gap training)",
        "contractor": "Plumbing Contractor's Licence; Restricted Plumbing Permit (for electricians/gasfitters doing particular plumbing work). Board issues 5 licence types. Individuals only (not companies)",
        "url": "https://www.wa.gov.au/government/multi-step-guides/plumbers-licensing",
        "asat": "verified Aug 2026 from wa.gov.au official; 5 licence classes confirmed; licences valid 3yrs (provisional 1yr)",
    },
    ("plumbing", "SA"): {
        "law": "Plumbers, Gas Fitters and Electricians Act 1995 (ss.6-7 contractor licence, ss.13-14 worker registration)",
        "regulator": "Consumer and Business Services (CBS), Commissioner",
        "individual": "Worker registration (plumbing categories: advanced plumber, plumber, drainer - with/without certification & technical-direction scope)",
        "trainee": "Registered apprentice (photographic registration card required)",
        "contractor": "Plumbing Contractors Licence (s.7(a)); licensed contractor's work must be by a registered worker (s.12)",
        "url": "https://www.sa.gov.au/topics/business-and-trade/licensing/building-and-trades/licensing",
        "asat": "verified Aug 2026 from legislation.sa.gov.au (Act s.7 classes) + CBS licence categories guide (Sep 2025)",
    },
    ("plumbing", "TAS"): {
        "law": "Occupational Licensing Act 2005 + Occupational Licensing (Plumbing Work Licence Classes) Determination",
        "regulator": "Consumer, Building and Occupational Services (CBOS), Administrator of Occupational Licensing",
        "individual": "Practitioner (post-Cert III + apprenticeship, unsupervised but employed, cannot certify); Certifier (Practitioner + Cert IV units, can certify and supervise)",
        "trainee": "Provisional plumbing licence",
        "contractor": "Plumbing Contractor licence (needs Nominated Manager holding a Certifier licence)",
        "url": "https://www.cbos.tas.gov.au/topics/licensing-and-registration/licensed-occupations/plumbing",
        "asat": "verified Aug 2026 from CBOS official; Plumbing Work Licence Classes Determination under Occupational Licensing Act 2005",
    },
    ("plumbing", "ACT"): {
        "law": "Construction Occupations (Licensing) Act 2004 + Construction Occupations (Licensing) Regulation 2004 (Reg R53, Act R71, both effective 23/02/26)",
        "regulator": "Construction Occupations Registrar, through Access Canberra",
        "individual": "Plumber (Act s.13; classes per Reg s.42: sanitary plumber, water supply plumber, journeyperson plumber, fire sprinkler fitter); Drainer (s.10; advanced sanitary to operative)",
        "trainee": "Apprentice (licence after qualification via ACT Planning)",
        "contractor": "Licensed plumber; plumbing plan certifier (s.14). Medical gas work licensing mandatory since 11 Dec 2024",
        "url": "https://www.planning.act.gov.au/professionals/regulation-and-responsibilities/construction-licences",
        "asat": "verified Aug 2026 from legislation.act.gov.au (Act ss.10-14, Reg s.42) + Access Canberra; Qualifications Declaration 2024",
    },
    ("plumbing", "NT"): {
        "law": "Plumbers and Drainers Licensing Act 1983",
        "regulator": "NT Plumbers and Drainers Licensing Board (statutory authority, Act s.5)",
        "individual": "Advanced Tradesman licence (2yrs post-trade experience; can contract and supervise; cannot certify)",
        "trainee": "Journeyman registration (post-Cert III; must be supervised; cannot contract directly) - note: in NT 'journeyman' IS a registration term",
        "contractor": "Advanced Tradesman may contract; to certify, register as Certifying Plumber and Drainer with the Building Practitioners Board. Only Plumbing & Draining regulated (roof/mechanical not; gas under NT WorkSafe)",
        "url": "https://plumberslicensing.nt.gov.au/",
        "asat": "verified Aug 2026 from plumberslicensing.nt.gov.au official; Board established under Act s.5; licence period 3yrs",
    },

    # ── GAS FITTING x 8 AU jurisdictions (register/regulator-verified Aug 2026) ──
    # NB: gas is regulated separately from plumbing in several jurisdictions.
    ("gas", "QLD"): {
        "law": "Petroleum and Gas (Production and Safety) Act 2004 + Petroleum and Gas (Safety) Regulation 2018",
        "regulator": "Petroleum and Gas Inspectorate / Resources Safety & Health Queensland (RSHQ), Chief Inspector (gas work licence). QBCC handles the separate gasfitting building-trade licence",
        "individual": "Full gas work licence (all Type A gas work); Restricted gas work licence (e.g. service-only: disconnect/reconnect, no installation)",
        "trainee": "Supervised gas work under a licensed gasfitter; Cert III Gas Fitting or Plumbing",
        "contractor": "Gas work authorisation for Type B (industrial/stationary engines). Type A = domestic/commercial appliances; Type B = industrial",
        "url": "https://www.business.qld.gov.au/industries/mining-energy-water/resources/safety-health/petroleum-gas/gas-work-devices/licences-authorisations",
        "asat": "verified Aug 2026 from RSHQ + Business Qld official; gas work portal moved to RSHQ 1 Jul 2025",
    },
    ("gas", "NSW"): {
        "law": "Gas and Electricity (Consumer Safety) Act 2017 + Gas and Electricity (Consumer Safety) Regulation 2018 (licensing via Home Building Act framework)",
        "regulator": "Building Commission NSW (formerly NSW Fair Trading)",
        "individual": "Tradesperson Certificate (perform, not supervise/contract); Qualified Supervisor Certificate (carry out + supervise); LP Gas categories (restricted LP for vapour phase <=150 kPa)",
        "trainee": "Provisional certificate; Cert III Plumbing (gas stream) or Cert III Gasfitting",
        "contractor": "Contractor Licence (contract/advertise). Reg 2018 Clause 42: test for defects + certificate of compliance within 5 business days",
        "url": "https://www.nsw.gov.au/housing-and-construction/compliance-and-regulation/gasfitters",
        "asat": "verified Aug 2026 from nsw.gov.au official (supersedes older 'Gas Supply Act 1996' claims); Act 2017 + Reg 2018 commenced 1 Sep 2018",
    },
    ("gas", "VIC"): {
        "law": "Gas Safety Act 1997 + Gas Safety (Gas Installation) Regulations 2018 (amended 2022, 2025)",
        "regulator": "DUAL: Energy Safe Victoria (ESV) = gas safety/technical regulator (complex installs + Type B acceptance via GasTrac); Building and Plumbing Commission (BPC) = licensing/registration + Compliance Certificates",
        "individual": "Gasfitting is a specialised class of plumbing; Registered or Licensed gasfitter per class; specialised endorsements incl. Type A appliance servicing (AS 4575)",
        "trainee": "Plumbing apprentice (gas stream)",
        "contractor": "Licensed gasfitter self-certifies standard installs (BPC Compliance Certificate); complex installs + Type B require ESV acceptance",
        "url": "https://www.energysafe.vic.gov.au/community-safety/working-tradespeople/gasfitters",
        "asat": "verified Aug 2026 from ESV + BPC official; dual-regulator structure; BPC replaced VBA 1 Jul 2025",
    },
    ("gas", "WA"): {
        "law": "Gas Standards Act 1972 + Gas Standards (Gasfitting and Consumer Gas Installations) Regulations 1999",
        "regulator": "Building and Energy (DEMIRS), Director of Energy Safety",
        "individual": "Class G (general, Type A, piping <=200 kPa); Class I (industrial, Type B, piping >200 kPa); Class E (mobile engines/cars/marine); Class P (auto gas refuelling). All may be restricted",
        "trainee": "Supervised Gas Fitter (under a Gasfitting Authorisation holder); apprentice (buff card)",
        "contractor": "Gasfitting Authorisation (flexible, industrial/emergency/Type B). Permits are yellow plastic cards",
        "url": "https://www.wa.gov.au/government/multi-step-guides/gasfitting-permit",
        "asat": "verified Aug 2026 from wa.gov.au / Building and Energy official; classes G/I/E/P confirmed",
    },
    ("gas", "SA"): {
        "law": "Plumbers, Gas Fitters and Electricians Act 1995 (s.7 gas fitting contractors licence, s.14 worker registration) + Regulations 2025",
        "regulator": "Consumer and Business Services (CBS) for licensing; Office of the Technical Regulator (OTR) for gas safety/eCoC",
        "individual": "Worker registration (physically performs gas fitting work; categories with certification/technical-direction scope)",
        "trainee": "Registered apprentice (photographic registration card)",
        "contractor": "Gas fitting contractors licence (s.7(b)); contractor must also hold/engage a registered worker. Some work exempt under Reg 4 of Regs 2025",
        "url": "https://www.sa.gov.au/topics/business-and-trade/licensing/building-and-trades/licensing",
        "asat": "verified Aug 2026 from legislation.sa.gov.au (Act s.7/s.14) + CBS gas fitters categories guide (Sep 2025); 5yr warranty on gas work",
    },
    ("gas", "TAS"): {
        "law": "Gas Safety Act 2019 + Occupational Licensing Act 2005 + subordinate regulations",
        "regulator": "Consumer, Building and Occupational Services (CBOS); Director of Gas Safety (installation acceptance)",
        "individual": "Practitioner; Practitioner (Certifier); plus Auto gas-fitting (practitioner + restricted). Type A vs Type B appliance licences",
        "trainee": "Provisional gas-fitting practitioner licence; Cert III Gas-Fitting/Plumbing/Mechanical Services + Cert IV",
        "contractor": "Gas-fitting Contractor licence (needs Nominated Manager). Compliance: Gas Fitting Notice + MRN + compliance plate",
        "url": "https://www.cbos.tas.gov.au/topics/licensing-and-registration/licensed-occupations/gas-fitting",
        "asat": "verified Aug 2026 from CBOS official; Gas Safety Act 2019 + Occupational Licensing Act 2005",
    },
    ("gas", "ACT"): {
        "law": "Construction Occupations (Licensing) Act 2004 (s.12 gasfitter, s.12A gas appliance worker) + Gas Safety Act 2000 (s.6E gasfitter; s.6B/6C Type A/B) + Qualifications Declaration 2019",
        "regulator": "Construction Occupations Registrar, through Access Canberra",
        "individual": "Gasfitter licence; Gas Appliance Worker licence. Type A (unrestricted/restricted/supervised); Type B (unrestricted / restricted by MJ/h: atmospheric <2000 MJ/h or forced-draft <500 MJ/h unsupervised)",
        "trainee": "Supervised gas appliance work (under unrestricted licensee)",
        "contractor": "Compliance certificate to owner on completion; inspect/test appliance; defect-tag unsafe. Medical gas mandatory since 11 Dec 2024",
        "url": "https://www.planning.act.gov.au/professionals/regulation-and-responsibilities/construction-licences",
        "asat": "verified Aug 2026 from legislation.act.gov.au (COL Act s.12/12A, Gas Safety Act 2000 s.6B-6E) + Access Canberra",
    },
    ("gas", "NT"): {
        "law": "Dangerous Goods Act + Dangerous Goods Regulations 1985 (gas regulated as dangerous goods in NT)",
        "regulator": "NT WorkSafe (Competent Authority)",
        "individual": "General Gasfitter licence; Type A appliance licence; Type B appliance licence (Type B = single appliances >10 MJ/h)",
        "trainee": "Supervised gasfitter (works under a licensed General Gasfitter)",
        "contractor": "Compliance plate at control valve + Certificate of Compliance (COC) to owner, copy to NT WorkSafe within 7 days. Separate from autogas",
        "url": "https://worksafe.nt.gov.au/licensing-and-registration/dangerous-goods/gasfitters",
        "asat": "verified Aug 2026 from NT WorkSafe official; gas under Dangerous Goods Regs (distinct from plumbing regulated by Plumbers & Drainers Board)",
    },

    # ── REFRIGERATION / AIR-CONDITIONING x 8 AU (verified Aug 2026) ──────────
    # TWO-LAYER TRADE: federal ARCtick RHL (national) + state restricted
    # electrical licence for RAC (varies). Refrigerant handling and electrical
    # work are SEPARATE authorisations - holding one does not authorise the other.
    ("refrigeration", "QLD"): {
        "law": "Ozone Protection and Synthetic Greenhouse Gas Management Act 1989 (Cwlth, refrigerant) + Electrical Safety Act 2002 (Qld, electrical)",
        "regulator": "ARC (refrigerant, national) + Electrical Safety Office/WorkSafe QLD (electrical) + QBCC (contractor licence over $3,300)",
        "individual": "FEDERAL (all AU): ARCtick Refrigerant Handling Licence (RHL) from Australian Refrigeration Council under Ozone Protection and Synthetic Greenhouse Gas Management Act 1989 + Regs 1995 - required to handle refrigerant; business needs a Refrigerant Trading Authorisation (RTA). STATE: Restricted Electrical Work Licence (refrigeration & air conditioning) from the QLD electrical regulator for the electrical component (disconnect/reconnect at terminals, fault-find, like-for-like replace). Full electrical work needs an electrical worker licence",
        "trainee": "ARCtick trainee licence (supervised) + apprentice; Cert III Air Conditioning and Refrigeration (UEE32220)",
        "contractor": "QBCC contractor licence for RAC work over $3,300; RTA for the business",
        "url": "https://www.qbcc.qld.gov.au/news/refrigeration-air-conditioning-technicians-electrical-work",
        "asat": "verified Aug 2026 from ARC + WorkSafe QLD official; two-layer (ARCtick + restricted electrical) confirmed",
    },
    ("refrigeration", "NSW"): {
        "law": "Ozone Protection and Synthetic Greenhouse Gas Management Act 1989 (Cwlth) + NSW electrical licensing law",
        "regulator": "ARC (refrigerant) + Electrical Workers and Contractors Licensing Board / Building Commission NSW (electrical)",
        "individual": "FEDERAL (all AU): ARCtick Refrigerant Handling Licence (RHL) from Australian Refrigeration Council under Ozone Protection and Synthetic Greenhouse Gas Management Act 1989 + Regs 1995 - required to handle refrigerant; business needs a Refrigerant Trading Authorisation (RTA). STATE: Restricted Electrical Licence for the fixed wiring; electrical contractor issues a CCEW (Certificate of Compliance for Electrical Work). Both licences required - 'you need both to work'",
        "trainee": "ARCtick trainee licence + Provisional Tradesperson Certificate (Building Commission NSW); Cert III Air Conditioning and Refrigeration (UEE32220)",
        "contractor": "RTA (business) + electrical contractor licence",
        "url": "https://www.nsw.gov.au/housing-and-construction/compliance-and-regulation",
        "asat": "verified Aug 2026 from ARC + Trades Recognition Australia; NSW requires ARCtick RHL + restricted electrical licence",
    },
    ("refrigeration", "VIC"): {
        "law": "Ozone Protection and Synthetic Greenhouse Gas Management Act 1989 (Cwlth) + Building Act 1993 Part 12A (RAC regulated as plumbing in VIC)",
        "regulator": "ARC (refrigerant) + Building and Plumbing Commission (BPC) (Refrigerated Air Conditioning plumbing class)",
        "individual": "FEDERAL (all AU): ARCtick Refrigerant Handling Licence (RHL) from Australian Refrigeration Council under Ozone Protection and Synthetic Greenhouse Gas Management Act 1989 + Regs 1995 - required to handle refrigerant; business needs a Refrigerant Trading Authorisation (RTA). STATE: register/licence in the Refrigerated Air Conditioning class with BPC + mechanical services plumbing qualification. NB: in VIC, RAC is regulated as a specialised class of PLUMBING",
        "trainee": "ARCtick trainee + plumbing apprentice (mechanical services stream)",
        "contractor": "Licensed mechanical services plumber (RAC class); RTA for business",
        "url": "https://www.bpc.vic.gov.au/plumbers/plumber-classes-and-categories",
        "asat": "verified Aug 2026 from ARC + BPC; VIC uniquely regulates RAC as a plumbing class (mechanical services) + ARCtick",
    },
    ("refrigeration", "WA"): {
        "law": "Ozone Protection and Synthetic Greenhouse Gas Management Act 1989 (Cwlth) + Electricity (Licensing) Regulations 1991 (WA electrical)",
        "regulator": "ARC (refrigerant) + Building and Energy (DEMIRS) (electrical)",
        "individual": "FEDERAL (all AU): ARCtick Refrigerant Handling Licence (RHL) from Australian Refrigeration Council under Ozone Protection and Synthetic Greenhouse Gas Management Act 1989 + Regs 1995 - required to handle refrigerant; business needs a Refrigerant Trading Authorisation (RTA). STATE: restricted electrical worker's licence for the RAC electrical component from Building and Energy WA",
        "trainee": "ARCtick trainee + electrical training licence; Cert III Air Conditioning and Refrigeration",
        "contractor": "RTA (business) + electrical contractor where applicable",
        "url": "https://www.wa.gov.au/organisation/building-and-energy",
        "asat": "verified Aug 2026 from ARC + Trades Recognition Australia; ARCtick RHL + WA restricted electrical",
    },
    ("refrigeration", "SA"): {
        "law": "Ozone Protection and Synthetic Greenhouse Gas Management Act 1989 (Cwlth) + Plumbers, Gas Fitters and Electricians Act 1995 (SA electrical)",
        "regulator": "ARC (refrigerant) + Consumer and Business Services / Office of the Technical Regulator (electrical)",
        "individual": "FEDERAL (all AU): ARCtick Refrigerant Handling Licence (RHL) from Australian Refrigeration Council under Ozone Protection and Synthetic Greenhouse Gas Management Act 1989 + Regs 1995 - required to handle refrigerant; business needs a Refrigerant Trading Authorisation (RTA). STATE: Restricted Electrical Worker Registration (under supervision). Both licences required to work (supervised) in SA as an air-conditioning and refrigeration mechanic",
        "trainee": "ARCtick trainee + restricted electrical worker registration (supervised)",
        "contractor": "RTA (business) + electrical contractors licence (s.7(c))",
        "url": "https://www.sa.gov.au/topics/business-and-trade/licensing/building-and-trades/licensing",
        "asat": "verified Aug 2026 from ARC + Trades Recognition Australia; SA requires ARCtick RHL + restricted electrical worker registration",
    },
    ("refrigeration", "TAS"): {
        "law": "Ozone Protection and Synthetic Greenhouse Gas Management Act 1989 (Cwlth) + Occupational Licensing Act 2005 (TAS electrical)",
        "regulator": "ARC (refrigerant) + Consumer, Building and Occupational Services (CBOS) (electrical)",
        "individual": "FEDERAL (all AU): ARCtick Refrigerant Handling Licence (RHL) from Australian Refrigeration Council under Ozone Protection and Synthetic Greenhouse Gas Management Act 1989 + Regs 1995 - required to handle refrigerant; business needs a Refrigerant Trading Authorisation (RTA). STATE: restricted electrical practitioner licence for RAC from CBOS",
        "trainee": "ARCtick trainee + electrical provisional; Cert III Air Conditioning and Refrigeration",
        "contractor": "RTA (business) + electrical contractor where applicable",
        "url": "https://www.cbos.tas.gov.au/topics/licensing-and-registration",
        "asat": "verified Aug 2026 from ARC + Trades Recognition Australia; ARCtick RHL + TAS restricted electrical",
    },
    ("refrigeration", "ACT"): {
        "law": "Ozone Protection and Synthetic Greenhouse Gas Management Act 1989 (Cwlth) + Construction Occupations (Licensing) Act 2004 (ACT electrical)",
        "regulator": "ARC (refrigerant) + Access Canberra (electrical)",
        "individual": "FEDERAL (all AU): ARCtick Refrigerant Handling Licence (RHL) from Australian Refrigeration Council under Ozone Protection and Synthetic Greenhouse Gas Management Act 1989 + Regs 1995 - required to handle refrigerant; business needs a Refrigerant Trading Authorisation (RTA). STATE: Restricted - Electrotechnology Systems Refrigeration and Air-conditioning Licence with Access Canberra",
        "trainee": "ARCtick trainee + electrical apprentice; overseas OTSR -> Provisional Tradesperson Certificate (3yr, non-renewable)",
        "contractor": "RTA (business) + electrical contractor where applicable",
        "url": "https://www.planning.act.gov.au/professionals/regulation-and-responsibilities/construction-licences",
        "asat": "verified Aug 2026 from ARC + Trades Recognition Australia; ARCtick RHL + ACT restricted electrotechnology RAC licence",
    },
    ("refrigeration", "NT"): {
        "law": "Ozone Protection and Synthetic Greenhouse Gas Management Act 1989 (Cwlth) + NT electrical licensing (Electrical Safety Act 2022)",
        "regulator": "ARC (refrigerant) + NT electrical regulator (NT WorkSafe electrical licensing)",
        "individual": "FEDERAL (all AU): ARCtick Refrigerant Handling Licence (RHL) from Australian Refrigeration Council under Ozone Protection and Synthetic Greenhouse Gas Management Act 1989 + Regs 1995 - required to handle refrigerant; business needs a Refrigerant Trading Authorisation (RTA). STATE: NT restricted electrical licence for the RAC electrical component",
        "trainee": "ARCtick trainee licence (handling refrigerant in classroom/workplace) + electrical trainee",
        "contractor": "RTA (business) + electrical contractor where applicable",
        "url": "https://worksafe.nt.gov.au/",
        "asat": "verified Aug 2026 from ARC + Trades Recognition Australia; ARCtick RHL + NT restricted electrical",
    },

    # ── BUILDING / CARPENTRY (PARTIAL: 3 of 8 verified - QLD, NSW, VIC) ──────
    # NON-UNIFORM, threshold-based licensing (unlike safety-critical trades).
    # Employed carpenters often work under an employer's/builder's licence.
    # WA/SA/TAS/ACT/NT not yet verified to official-source standard - omitted
    # rather than fabricated; the hardened prompt handles those honestly.
    ("carpentry", "QLD"): {
        "law": "Queensland Building and Construction Commission Act 1991 (QBCC Act)",
        "regulator": "Queensland Building and Construction Commission (QBCC)",
        "individual": "Carpentry trade contractor licence (trade contractor / nominee supervisor / site supervisor); Cert III Carpentry + QBCC managerial course. Builder licences: Low Rise (CPC40120), Medium Rise (CPC50220), Open (CPC60220)",
        "trainee": "Apprentice; employed carpenter working under a licensed employer is covered by the employer's licence",
        "contractor": "Licence required for building work over $3,300 (incl. labour+materials). Under $3,300, or employed under a licensed employer, may not need own licence",
        "url": "https://www.qbcc.qld.gov.au/licences/apply-licence/available-licences/other-trade/carpentry",
        "asat": "verified Aug 2026 from QBCC official; threshold $3,300; employee-under-licensed-employer exemption",
    },
    ("carpentry", "NSW"): {
        "law": "Home Building Act 1989 (carpentry defined in Home Building Regulation 2014 Schedule 4)",
        "regulator": "Building Commission NSW (formerly NSW Fair Trading)",
        "individual": "Contractor Licence (contract in own right); Qualified Supervisor Certificate (supervise + perform, not contract); Tradesperson Certificate. Cert III Carpentry (CPC30220) + 2yrs experience",
        "trainee": "Employee paid wages may do carpentry without own licence; apprentice",
        "contractor": "Licence required for building/trade work over $5,000 (incl. labour+materials+GST). Unlicensed individuals limited to work <=$5,000. Fines $22,000 individual / $110,000 company",
        "url": "https://www.nsw.gov.au/business-and-economy/licences-and-credentials/building-and-trade-licences-and-registrations/carpentry-work",
        "asat": "verified Aug 2026 from nsw.gov.au official; threshold $5,000; carpentry per Home Building Reg 2014 Sch 4",
    },
    ("carpentry", "VIC"): {
        "law": "Building Act 1993 + Building Regulations 2018",
        "regulator": "Building and Plumbing Commission (BPC, formerly Victorian Building Authority)",
        "individual": "NO standalone carpentry licence in VIC. VIC registers BUILDING PRACTITIONERS (7 categories incl. builder); an individual carpenter typically works under a registered building practitioner (head builder)",
        "trainee": "Carpenter works as employee/sub-contractor under the registered head builder - no individual carpenter registration required",
        "contractor": "Registration required for building work over $10,000; generally only the head contractor must be registered. Mandatory standalone carpenter registration proposed since 2019 but not implemented",
        "url": "https://www.vba.vic.gov.au/registration-and-licensing/building-practitioner-registration",
        "asat": "verified Aug 2026 from VBA/BPC official; VIC does NOT separately licence carpentry - builder-registration model, $10,000 threshold",
    },

    ("carpentry", "WA"): {
        "law": "Building Services (Registration) Act 2011 (WA)",
        "regulator": "Building Services Board (Building and Energy / DEMIRS)",
        "individual": "NO standalone carpenter licence in WA. A carpenter employed by a registered builder needs no licence. To contract on building work over $20,000, must register as a builder (Building Practitioner / Building Contractor)",
        "trainee": "Apprentice/employee under a registered builder - no individual carpenter registration",
        "contractor": "Building Contractor registration required to contract for building work over $20,000 (requiring a building permit)",
        "url": "https://www.wa.gov.au/organisation/building-and-energy/building-and-energy-licensing",
        "asat": "verified Aug 2026 from buildalife.wa.gov.au / wa.gov.au official; no standalone carpentry licence, builder registration over $20,000",
    },
    ("carpentry", "SA"): {
        "law": "Building Work Contractors Act 1995 (SA) + Building Work Contractors Regulations 2026",
        "regulator": "Consumer and Business Services (CBS)",
        "individual": "NO distinct 'carpenter' class. Carpentry is 'specified building work'; to contract needs a Building Work Contractor's Licence ($10,000 net assets) OR a restricted subcontractor licence. Employed carpenter under a licensed contractor needs no own licence",
        "trainee": "Apprentice/employee under a licensed building work contractor",
        "contractor": "Building Work Contractor's Licence (or restricted subcontractor licence). From 10 Nov 2025 domestic work under $20,000 is 'minor' (no written contract/insurance) but worker must still be licensed. Penalties to $150k individual / $550k company",
        "url": "https://www.sa.gov.au/topics/business-and-trade/licensing/building-and-trades/licensing",
        "asat": "verified Aug 2026 from sa.gov.au / CBS official; carpentry = specified building work, no standalone carpenter class",
    },
    ("carpentry", "TAS"): {
        "law": "Occupational Licensing Act 2005 + Occupational Licensing (Building Services Work) Determination + Regulations 2016",
        "regulator": "Consumer, Building and Occupational Services (CBOS), Administrator of Occupational Licensing",
        "individual": "NO standalone 'carpenter' occupation. TAS licenses Building Services Providers (occupation: builder). Carpentry is building services work performed under a builder / building services provider licence",
        "trainee": "Apprentice/employee under a licensed building services provider",
        "contractor": "Building Services Provider (builder) licence; contractor level. Same Occupational Licensing Act 2005 framework as TAS electrical/plumbing/gas",
        "url": "https://www.cbos.tas.gov.au/topics/licensing-and-registration/licensed-occupations/building",
        "asat": "verified Aug 2026 from CBOS official; carpentry under building services provider framework, no standalone carpenter occupation",
    },
    ("carpentry", "ACT"): {
        "law": "Construction Occupations (Licensing) Act 2004 (ACT)",
        "regulator": "Access Canberra (Construction Occupations Registrar)",
        "individual": "NOT separately licensed. Per official ACT guidance, carpenters and bricklayers do NOT currently need a licence in the ACT. Qualification + construction induction card to work on site; work under a builder's construction occupations licence",
        "trainee": "Apprentice/employee - no carpenter licence required",
        "contractor": "Builder licence (classes A unlimited, B, C, D) needed to contract directly with an owner. ACT considering expanding licensing to carpentry/bricklaying but not yet in force",
        "url": "https://www.planning.act.gov.au/build-buy-renovate/build-buy-or-renovate/getting-a-building-licence",
        "asat": "verified Aug 2026 from planning.act.gov.au official; carpentry NOT a licensed occupation in ACT (work under builder's licence)",
    },
    ("carpentry", "NT"): {
        "law": "Building Act 1993 (NT)",
        "regulator": "Building Practitioners Board (BPB), established by Building Act 1993",
        "individual": "NO specific carpentry licence in NT. Carpentry falls under building work; NT uses 'registration' not 'licensing'. Employed carpenter works under a registered building contractor",
        "trainee": "Apprentice/employee under a registered building contractor",
        "contractor": "Building Contractor registration required to contract for building work valued $12,000 or more. Work under $12,000, or reno/alteration with no floor-area/height increase, exempt",
        "url": "https://bpb.nt.gov.au/",
        "asat": "verified Aug 2026 from bpb.nt.gov.au / nt.gov.au official; no standalone carpentry licence, building contractor registration over $12,000",
    },
    # ── UK (LL-4). Checked Oct 2026 against gov.uk, gov.wales, gov.scot, HSE, Gas Safe, WaterSafe/WRAS, DAERA, CSCS. ──
    # England
    ("electrical", "ENGLAND"): {
        "law": "Building Regulations 2010, Part P (Approved Document P: Electrical safety - Dwellings, 2013 edition) for domestic work; Electricity at Work Regulations 1989 for work activities; BS 7671 is the technical standard used to show compliance (a standard, not law)",
        "regulator": "Local authority building control / registered building control approvers (Part P); Health and Safety Executive (Electricity at Work Regulations)",
        "individual": "NO licence and no protected title for electricians in England. Notifiable domestic work (new circuits, consumer unit replacement, work in special locations such as bathrooms) must be self-certified by an installer registered with a government-authorised competent person scheme (e.g. NICEIC, NAPIT, ELECSA, Stroma) or notified to building control before work starts (or certified by a registered third-party certifier). Like-for-like replacements, repairs and adding points to existing circuits outside special locations are generally not notifiable",
        "trainee": "No legal licence stage. Industry route: Level 3 electrotechnical apprenticeship + AM2 assessment for the JIB/ECS card (industry scheme, not law); trainees work under supervision of a competent person",
        "contractor": "No contractor licence. Competent person scheme membership is voluntary but needed to self-certify notifiable domestic work and issue the Building Regulations compliance certificate",
        "url": "https://www.planningportal.co.uk/applications/building-control-applications/building-control/approved-documents/part-p-electrical-safety",
        "asat": "checked Oct 2026 (Planning Portal, HSE); Approved Document P 2013 edition still current in England",
    },
    ("gas", "ENGLAND"): {
        "law": "Gas Safety (Installation and Use) Regulations 1998",
        "regulator": "Health and Safety Executive (HSE); Gas Safe Register is the official register (operated for HSE)",
        "individual": "LEGAL REQUIREMENT: anyone doing gas work for gain must be on the Gas Safe Register, and only for the appliance/work categories on their Gas Safe ID card. Competence is shown through ACS (Accredited Certification Scheme) assessments or equivalent recognised qualifications",
        "trainee": "Trainees may work only under the direct supervision of a Gas Safe registered engineer competent in that work; registration follows qualification/ACS",
        "contractor": "The business must be Gas Safe registered and employ registered engineers for each category of work it offers",
        "url": "https://www.gassaferegister.co.uk/",
        "asat": "checked Oct 2026 (HSE, Gas Safe Register, UK Regulated Professions Register)",
    },
    ("plumbing", "ENGLAND"): {
        "law": "Water Supply (Water Fittings) Regulations 1999; Building Regulations 2010 Parts G (sanitation, hot water safety incl. unvented systems G3), H (drainage) and L (energy)",
        "regulator": "Water undertakers (water companies) enforce the Water Fittings Regulations; local authority building control / registered building control approvers for Building Regulations",
        "individual": "NO licence for plumbers in England. Unvented hot water storage systems must be installed by a competent person (commonly evidenced by an unvented hot water qualification and competent person scheme membership). Gas boiler work also needs Gas Safe registration",
        "trainee": "No legal licence stage; NVQ/apprenticeship (e.g. Level 3 Plumbing and Domestic Heating) is the industry route",
        "contractor": "No licence. Approved contractor schemes (WaterSafe register: APHC, CIPHE, WIAPS) are voluntary; approved contractors issue a certificate of compliance and can carry out some notifiable work without prior notice",
        "url": "https://www.watersafe.org.uk/",
        "asat": "checked Oct 2026 (WaterSafe, Water Regs UK)",
    },
    ("refrigeration", "ENGLAND"): {
        "law": "Fluorinated Greenhouse Gases Regulations 2015 (SI 2015/310) and the F-gas Regulation (EU) No 517/2014 as retained and amended for Great Britain",
        "regulator": "Environment Agency (England)",
        "individual": "Personnel installing, servicing, leak-checking or recovering F-gas in stationary refrigeration, air conditioning and heat pumps need an F-gas personnel certificate (Category I-IV) from an approved certification body",
        "trainee": "Trainees may work under the supervision of a certified person while obtaining their F-gas certificate (training and assessment)",
        "contractor": "Companies installing, maintaining or servicing such equipment for others need an F-gas company certificate (e.g. REFCOM, Quidos, Stroma)",
        "url": "https://www.legislation.gov.uk/uksi/2015/310",
        "asat": "checked Oct 2026 (legislation.gov.uk SI 2015/310); GB F-gas rules are under review - confirm any 2025-26 amendments on gov.uk",
    },
    ("carpentry", "ENGLAND"): {
        "law": "Building Regulations 2010 (incl. Part 2A dutyholder and competence duties under the Building Safety Act 2022, in force 1 Oct 2023); Construction (Design and Management) Regulations 2015",
        "regulator": "Local authority building control / registered building control approvers; Building Safety Regulator for higher-risk buildings; HSE for CDM",
        "individual": "NO licence for carpenters/joiners. Since Oct 2023 anyone carrying out building work must be competent for it (skills, knowledge, experience, behaviours). A CSCS card is an industry requirement on most sites, not a legal requirement",
        "trainee": "Apprenticeship (e.g. Level 2/3 Site Carpentry or Architectural Joinery); no licence stage",
        "contractor": "No contractor licence. Principal contractor/contractor duties under the Building Regulations dutyholder regime and CDM 2015",
        "url": "https://www.cscs.uk.com/about/building-safety-act/",
        "asat": "checked Oct 2026 (CSCS, Building Safety Act guidance)",
    },
    # Wales
    ("electrical", "WALES"): {
        "law": "Building Regulations 2010 as they apply in Wales, Part P (Welsh Approved Document P); Electricity at Work Regulations 1989 for work activities; BS 7671 as the technical standard",
        "regulator": "Local authority building control in Wales; Health and Safety Executive (Electricity at Work Regulations)",
        "individual": "NO licence for electricians in Wales. Wales did NOT adopt England's 2013 narrowing of Part P: besides new circuits and consumer units, changes to existing circuits in a kitchen, bathroom or outdoors are still notifiable. Use a registered competent person (self-certifies, issues the compliance certificate within 30 days) or notify building control before starting",
        "trainee": "No legal licence stage; Level 3 apprenticeship + AM2 is the industry route",
        "contractor": "No contractor licence; competent person scheme membership needed to self-certify notifiable work",
        "url": "https://www.gov.wales/building-regulations-electrics",
        "asat": "checked Oct 2026 (gov.wales)",
    },
    ("gas", "WALES"): {
        "law": "Gas Safety (Installation and Use) Regulations 1998",
        "regulator": "Health and Safety Executive (HSE); Gas Safe Register",
        "individual": "LEGAL REQUIREMENT: Gas Safe registration for anyone doing gas work for gain, limited to the categories on their ID card (ACS or equivalent competence)",
        "trainee": "Only under the direct supervision of a Gas Safe registered engineer",
        "contractor": "Business must be Gas Safe registered",
        "url": "https://www.gassaferegister.co.uk/",
        "asat": "checked Oct 2026 (HSE, Gas Safe Register)",
    },
    ("plumbing", "WALES"): {
        "law": "Water Supply (Water Fittings) Regulations 1999 (as they apply in Wales); Building Regulations 2010 as applied in Wales, Parts G, H and L",
        "regulator": "Water undertakers (e.g. Dwr Cymru Welsh Water, Hafren Dyfrdwy); local authority building control",
        "individual": "NO licence for plumbers. Unvented hot water systems must be installed by a competent person; gas work needs Gas Safe registration",
        "trainee": "No legal licence stage; apprenticeship/NVQ route",
        "contractor": "No licence; WaterSafe approved contractor schemes are voluntary and allow certificates of compliance",
        "url": "https://www.watersafe.org.uk/",
        "asat": "checked Oct 2026 (WaterSafe, Water Regs UK)",
    },
    ("refrigeration", "WALES"): {
        "law": "Fluorinated Greenhouse Gases Regulations 2015 (SI 2015/310) and the GB-retained F-gas Regulation (EU) No 517/2014",
        "regulator": "Natural Resources Wales (NRW)",
        "individual": "F-gas personnel certificate (Category I-IV) for work on stationary refrigeration, air conditioning and heat pumps",
        "trainee": "Under supervision of a certified person while training",
        "contractor": "F-gas company certificate needed to install/maintain/service for others",
        "url": "https://www.legislation.gov.uk/uksi/2015/310",
        "asat": "checked Oct 2026 (legislation.gov.uk); confirm any 2025-26 GB F-gas amendments",
    },
    ("carpentry", "WALES"): {
        "law": "Building Regulations 2010 as they apply in Wales; Construction (Design and Management) Regulations 2015",
        "regulator": "Local authority building control in Wales; HSE for CDM",
        "individual": "NO licence for carpenters/joiners; competence expected under CDM; CSCS card is an industry site requirement, not law",
        "trainee": "Apprenticeship route; no licence stage",
        "contractor": "No contractor licence; CDM 2015 contractor duties",
        "url": "https://www.cscs.uk.com/about/building-safety-act/",
        "asat": "checked Oct 2026; Building Safety Act dutyholder rules in Wales differ from England - confirm for higher-risk buildings",
    },
    # Scotland
    ("electrical", "SCOTLAND"): {
        "law": "Building (Scotland) Regulations 2004 and the Building (Scotland) Act 2003 (building standards); Electricity at Work Regulations 1989 for work activities; BS 7671 as the technical standard. Part P does NOT apply in Scotland",
        "regulator": "Local authority building standards (verifiers); Health and Safety Executive (Electricity at Work Regulations)",
        "individual": "NO licence and no protected title for electricians in Scotland (statutory protection of title has been proposed and campaigned for, e.g. by SELECT in 2025, but is not law). Whether domestic electrical work needs a building warrant depends on the building type (Regulation 5 and Schedule 3), not just the work. Where a warrant applies, work must meet BS 7671 and can be certified by an Approved Certifier of Construction (electrical installations to BS 7671 schemes run by SELECT and NICEIC)",
        "trainee": "No legal licence stage; Modern Apprenticeship in Electrical Installation (SJIB/ECS card is an industry scheme)",
        "contractor": "No contractor licence; Approved Certifier schemes let registered businesses self-certify warrant work",
        "url": "https://www.gov.scot/publications/regulation-electricians-scotland/",
        "asat": "checked Oct 2026 (gov.scot, Electrical Safety First, SELECT)",
    },
    ("gas", "SCOTLAND"): {
        "law": "Gas Safety (Installation and Use) Regulations 1998",
        "regulator": "Health and Safety Executive (HSE); Gas Safe Register",
        "individual": "LEGAL REQUIREMENT: Gas Safe registration for anyone doing gas work for gain, limited to the categories on their ID card",
        "trainee": "Only under the direct supervision of a Gas Safe registered engineer",
        "contractor": "Business must be Gas Safe registered",
        "url": "https://www.gassaferegister.co.uk/",
        "asat": "checked Oct 2026 (HSE, Gas Safe Register)",
    },
    ("plumbing", "SCOTLAND"): {
        "law": "Water Supply (Water Fittings) (Scotland) Byelaws 2014; Building (Scotland) Regulations 2004 (e.g. unvented hot water, drainage)",
        "regulator": "Scottish Water (Byelaws); local authority building standards",
        "individual": "NO licence for plumbers. Unvented hot water systems must be installed by a competent person; gas work needs Gas Safe registration",
        "trainee": "Modern Apprenticeship in Plumbing and Heating (SJIB plumbing in Scotland via SNIPEF)",
        "contractor": "No licence; approved contractor schemes (e.g. SNIPEF, WaterSafe) are voluntary; approved contractors send certificates for notifiable fittings to Scottish Water",
        "url": "https://www.scottishwater.co.uk/",
        "asat": "checked Oct 2026 (Water Regs UK, WaterSafe)",
    },
    ("refrigeration", "SCOTLAND"): {
        "law": "Fluorinated Greenhouse Gases Regulations 2015 (SI 2015/310) and the GB-retained F-gas Regulation (EU) No 517/2014",
        "regulator": "Scottish Environment Protection Agency (SEPA)",
        "individual": "F-gas personnel certificate (Category I-IV) for work on stationary refrigeration, air conditioning and heat pumps",
        "trainee": "Under supervision of a certified person while training",
        "contractor": "F-gas company certificate needed to install/maintain/service for others",
        "url": "https://www.legislation.gov.uk/uksi/2015/310",
        "asat": "checked Oct 2026 (legislation.gov.uk); confirm any 2025-26 GB F-gas amendments",
    },
    ("carpentry", "SCOTLAND"): {
        "law": "Building (Scotland) Regulations 2004; Construction (Design and Management) Regulations 2015",
        "regulator": "Local authority building standards; HSE for CDM",
        "individual": "NO licence for carpenters/joiners; CSCS card is an industry site requirement, not law",
        "trainee": "Modern Apprenticeship in Carpentry and Joinery",
        "contractor": "No contractor licence; Approved Certifiers of Construction exist for some work types",
        "url": "https://www.cscs.uk.com/about/building-safety-act/",
        "asat": "checked Oct 2026",
    },
    # Northern Ireland
    ("electrical", "NORTHERN IRELAND"): {
        "law": "Electricity at Work Regulations (Northern Ireland) 1991 for work activities; Building Regulations (Northern Ireland) 2012. There is NO Part P-style statutory requirement for domestic electrical installation work in Northern Ireland; BS 7671 is the technical standard",
        "regulator": "Health and Safety Executive for Northern Ireland (HSENI); district council building control",
        "individual": "NO licence for electricians in Northern Ireland and no statutory notification scheme for domestic electrical work. Registration with bodies such as NICEIC is voluntary but recommended",
        "trainee": "No legal licence stage; apprenticeship route",
        "contractor": "No contractor licence",
        "url": "https://www.electricalsafetyfirst.org.uk/guides-and-advice/building-regulations/",
        "asat": "checked Oct 2026 (Electrical Safety First)",
    },
    ("gas", "NORTHERN IRELAND"): {
        "law": "Gas Safety (Installation and Use) Regulations (Northern Ireland) 2004",
        "regulator": "Health and Safety Executive for Northern Ireland (HSENI); Gas Safe Register",
        "individual": "LEGAL REQUIREMENT: Gas Safe registration for anyone doing gas work for gain, limited to the categories on their ID card",
        "trainee": "Only under the direct supervision of a Gas Safe registered engineer",
        "contractor": "Business must be Gas Safe registered",
        "url": "https://www.gassaferegister.co.uk/",
        "asat": "checked Oct 2026 (UK Regulated Professions Register: Gas Safe requirements apply throughout the UK including Northern Ireland)",
    },
    ("plumbing", "NORTHERN IRELAND"): {
        "law": "Water Supply (Water Fittings) Regulations (Northern Ireland) 2009; Building Regulations (Northern Ireland) 2012 (Part P in NI covers sanitary appliances and unvented hot water storage)",
        "regulator": "Northern Ireland Water (water fittings); district council building control",
        "individual": "NO licence for plumbers. Unvented hot water systems must be installed by a competent person; gas work needs Gas Safe registration",
        "trainee": "No legal licence stage; apprenticeship route",
        "contractor": "No licence; approved contractor schemes are voluntary",
        "url": "https://www.niwater.com/",
        "asat": "checked Oct 2026 (Water Regs UK)",
    },
    ("refrigeration", "NORTHERN IRELAND"): {
        "law": "EU F-gas Regulation (EU) 2024/573 applies directly in Northern Ireland (from 11 March 2024), alongside the Fluorinated Greenhouse Gases Regulations (Northern Ireland) 2015 - NI follows the EU rules, not the GB ones",
        "regulator": "Department of Agriculture, Environment and Rural Affairs (DAERA) - Chemicals and Industrial Pollution Policy Branch",
        "individual": "F-gas personnel certificate from a recognised certification body for work on refrigeration, air conditioning and heat pumps; check category with DAERA as EU 2024/573 extends certification",
        "trainee": "Under supervision of a certified person while training",
        "contractor": "F-gas company certificate needed; a business handing over F-gas work must check the receiving business is certified",
        "url": "https://www.daera-ni.gov.uk/articles/fluorinated-greenhouse-gases",
        "asat": "checked Oct 2026 (DAERA); DAERA still finalising NI offences/penalties legislation for EU 2024/573",
    },
    ("carpentry", "NORTHERN IRELAND"): {
        "law": "Building Regulations (Northern Ireland) 2012; Construction (Design and Management) Regulations (Northern Ireland) 2016",
        "regulator": "District council building control; HSENI for CDM",
        "individual": "NO licence for carpenters/joiners; CSCS card is an industry site requirement, not law",
        "trainee": "Apprenticeship route; no licence stage",
        "contractor": "No contractor licence; CDM (NI) 2016 contractor duties",
        "url": "https://www.cscs.uk.com/about/building-safety-act/",
        "asat": "checked Oct 2026",
    },
    # ── US (LL-7): California, Texas, Florida, New York. Checked Oct 2026 against CSLB, DIR, TDLR, TSBPE, RRC, DBPR, NYC DOB/DCWP, Florida Senate. ──
    # Federal rules that apply in every state: EPA Section 608 certification to handle refrigerants; EPA RRP lead-safe certification
    # for renovation of pre-1978 homes and child-occupied buildings.
    ("electrical", "CALIFORNIA"): {
        "law": "Contractors' State License Law (Business and Professions Code s.7000 ff.); Labor Code ss.108-108.5 (electrician certification); California Electrical Code (Title 24 Part 3, based on the NEC)",
        "regulator": "Contractors State License Board (CSLB) for contractors; Department of Industrial Relations (DIR), Division of Labor Standards Enforcement - Electrician Certification Unit, for individual electricians",
        "individual": "Electricians employed by a C-10 contractor must be state-certified: General Electrician, Residential Electrician, Fire/Life Safety Technician, Voice Data Video Technician or Non-Residential Lighting Technician",
        "trainee": "Electrician Trainee registration with DIR, enrolled in a state-approved school, working under a certified electrician who may supervise only one trainee; or an apprentice in a state-approved program",
        "contractor": "CSLB C-10 Electrical Contractor licence for any job over $1,000 (labour + materials) or any job needing a permit; the minor-work exemption rose from $500 to $1,000 on 1 Jan 2025 (AB 2622) and never covers permitted work or hiring helpers",
        "url": "https://www.dir.ca.gov/DLSE/ECU/ElectricalTrainee.htm",
        "asat": "checked Oct 2026 (DIR, CSLB AB 2622 bulletin)",
    },
    ("plumbing", "CALIFORNIA"): {
        "law": "Contractors' State License Law (Business and Professions Code s.7000 ff.); California Plumbing Code (Title 24 Part 5)",
        "regulator": "Contractors State License Board (CSLB); local building departments for permits and inspection",
        "individual": "NO state licence for individual plumbers employed by a contractor; competence through apprenticeship. Some cities have their own rules - check locally",
        "trainee": "Apprenticeship (state-approved programs); no state trainee registration for plumbers",
        "contractor": "CSLB C-36 Plumbing Contractor licence for jobs over $1,000 or any permitted work (minor-work exemption $1,000 since 1 Jan 2025)",
        "url": "https://www.cslb.ca.gov/Resources/IndustryBulletins/2024/AB%202622%20Implementation.FINAL.pdf",
        "asat": "checked Oct 2026 (CSLB)",
    },
    ("gas", "CALIFORNIA"): {
        "law": "Contractors' State License Law; California Plumbing Code and California Mechanical Code (fuel gas piping and appliances)",
        "regulator": "Contractors State License Board (CSLB); local building departments; the gas utility for meter connection",
        "individual": "NO separate state gasfitter licence. Gas piping is done by or for licensed C-36 Plumbing contractors (and gas appliances for heating by C-20 HVAC contractors) under local permits",
        "trainee": "Apprenticeship; no state trainee registration",
        "contractor": "CSLB C-36 (gas piping) or C-20 (heating appliances) licence; permits and inspection required for gas work",
        "url": "https://www.cslb.ca.gov/",
        "asat": "checked Oct 2026 (CSLB)",
    },
    ("refrigeration", "CALIFORNIA"): {
        "law": "Contractors' State License Law; California Mechanical Code (Title 24 Part 4); federal Clean Air Act s.608 (refrigerants)",
        "regulator": "Contractors State License Board (CSLB); US EPA (refrigerant handling); local building departments",
        "individual": "No state technician licence; EPA Section 608 certification is required (federal) for anyone handling refrigerants",
        "trainee": "Apprenticeship; EPA 608 needed before handling refrigerant",
        "contractor": "CSLB C-20 Warm-Air Heating, Ventilating and Air-Conditioning or C-38 Refrigeration licence for jobs over $1,000 or any permitted work",
        "url": "https://www.cslb.ca.gov/",
        "asat": "checked Oct 2026 (CSLB)",
    },
    ("carpentry", "CALIFORNIA"): {
        "law": "Contractors' State License Law (Business and Professions Code s.7000 ff.); California Building and Residential Codes (Title 24 Parts 2 and 2.5)",
        "regulator": "Contractors State License Board (CSLB); local building departments",
        "individual": "NO licence for employed carpenters",
        "trainee": "Apprenticeship; no licence stage",
        "contractor": "CSLB C-5 Framing and Rough Carpentry, C-6 Cabinet, Millwork and Finish Carpentry, or B General Building licence for jobs over $1,000 or any permitted work (minor-work exemption $1,000 since 1 Jan 2025). EPA RRP certification for renovation of pre-1978 homes",
        "url": "https://www.cslb.ca.gov/Resources/IndustryBulletins/2024/AB%202622%20Implementation.FINAL.pdf",
        "asat": "checked Oct 2026 (CSLB)",
    },
    ("electrical", "TEXAS"): {
        "law": "Texas Occupations Code Chapter 1305 (Electrical Safety and Licensing Act); TDLR electrician rules; NEC adopted statewide (TDLR exams use the 2023 NEC; cities may adopt their own edition)",
        "regulator": "Texas Department of Licensing and Regulation (TDLR)",
        "individual": "State licence required for electrical work: Residential Wireman (4,000 h; dwellings up to four storeys), Journeyman Electrician (8,000 h), Master Electrician (12,000 h + 2 years as journeyman), Maintenance Electrician",
        "trainee": "Apprentice Electrician licence (apply before starting any electrical work), working under a Master, Journeyman or Residential Wireman",
        "contractor": "Electrical Contractor licence (must hold or employ a Master Electrician, with liability and workers' compensation insurance)",
        "url": "https://www.tdlr.texas.gov/electricians/elecforms.htm",
        "asat": "checked Oct 2026 (TDLR, ServiceTitan licensing guide)",
    },
    ("plumbing", "TEXAS"): {
        "law": "Texas Occupations Code Chapter 1301 (Plumbing License Law); TSBPE rules (22 TAC). 'Plumbing' includes piping that supplies water, other liquid or gas in or around a building",
        "regulator": "Texas State Board of Plumbing Examiners (TSBPE)",
        "individual": "State licence required: Tradesman Plumber-Limited, Journeyman Plumber, Master Plumber; endorsements for Medical Gas Piping and Water Supply Protection Specialist",
        "trainee": "Plumber's Apprentice registration with TSBPE, working under licensed plumbers",
        "contractor": "Work must be done under a Responsible Master Plumber (the master plumber responsible for the business)",
        "url": "https://tsbpe.texas.gov/license-types/endorsements/",
        "asat": "checked Oct 2026 (TSBPE)",
    },
    ("gas", "TEXAS"): {
        "law": "Texas Occupations Code Chapter 1301 (plumbing includes gas piping in and around buildings); Natural Resources Code Chapter 113 for LP-gas",
        "regulator": "TSBPE for natural gas piping; Railroad Commission of Texas (RRC) for LP-gas (propane) installation",
        "individual": "Natural gas piping in and around buildings: licensed plumbers (Journeyman/Master under a Responsible Master Plumber). LP-gas systems: an RRC LP-gas licence/certification (exempt from the plumbing licence for that work)",
        "trainee": "Plumber's Apprentice registration (TSBPE) or RRC LP-gas employee certification",
        "contractor": "Responsible Master Plumber for natural gas; RRC LP-gas licence for propane businesses",
        "url": "https://www.rrc.state.tx.us/alternative-fuels/liquefied-petroleum-gas-propane/plumber-and-acr-contractor-registration/",
        "asat": "checked Oct 2026 (TSBPE, RRC, Occupations Code 1301.002 and 1301.055)",
    },
    ("refrigeration", "TEXAS"): {
        "law": "Texas Occupations Code Chapter 1302 (Air Conditioning and Refrigeration Contractor License Law); 16 TAC Chapter 75; federal Clean Air Act s.608",
        "regulator": "Texas Department of Licensing and Regulation (TDLR); US EPA for refrigerants",
        "individual": "ACR Technician registration with TDLR before doing jobsite work (under a licensed contractor), or Certified ACR Technician (exam); EPA Section 608 to handle refrigerants",
        "trainee": "Registered ACR Technician (no exam, supervised by a licensed contractor)",
        "contractor": "ACR Contractor licence: Class A (unlimited size) or Class B (up to 25 tons cooling / 1,500,000 BTU/h heating per unit), with endorsements E (environmental air), R (commercial refrigeration) or C (both)",
        "url": "https://www.tdlr.texas.gov/acr/forms/acr-lic-002-e-acr-contractor-application.pdf",
        "asat": "checked Oct 2026 (TDLR)",
    },
    ("carpentry", "TEXAS"): {
        "law": "No state carpentry or general contractor licensing law; local building codes and permits apply (most cities adopt the IRC/IBC)",
        "regulator": "City building departments (registration and permits vary by city)",
        "individual": "NO state licence for carpenters",
        "trainee": "Apprenticeship; no licence stage",
        "contractor": "NO state general contractor or carpentry licence; many cities require contractor registration and permits. EPA RRP certification for renovation of pre-1978 homes",
        "url": "https://www.tdlr.texas.gov/",
        "asat": "checked Oct 2026",
    },
    ("electrical", "FLORIDA"): {
        "law": "Florida Statutes Chapter 489 Part II (electrical and alarm system contracting); Florida Building Code - Electrical (based on the NEC)",
        "regulator": "Electrical Contractors' Licensing Board (ECLB), Department of Business and Professional Regulation (DBPR); counties/cities for journeyman licences and permits",
        "individual": "Journeyman electrician licensing is local: counties and municipalities may issue journeyman licences (expressly authorised by HB 735, 2021). Check the county",
        "trainee": "Apprenticeship; local rules for helpers",
        "contractor": "State Certified Electrical Contractor (statewide) or Registered Electrical Contractor (local, after a local certificate of competency); also Alarm System and Specialty categories",
        "url": "https://flsenate.gov/Committees/BillSummaries/2021/html/735",
        "asat": "checked Oct 2026 (DBPR via Procore guide, Florida Senate HB 735 summary)",
    },
    ("plumbing", "FLORIDA"): {
        "law": "Florida Statutes Chapter 489 Part I (construction contracting); Florida Building Code - Plumbing",
        "regulator": "Construction Industry Licensing Board (CILB), DBPR; counties/cities for journeyman licences and permits",
        "individual": "Journeyman plumber licensing is local (counties/municipalities may issue journeyman licences under HB 735, 2021)",
        "trainee": "Apprenticeship; local rules",
        "contractor": "State Certified Plumbing Contractor (statewide) or Registered Plumbing Contractor (local); unlicensed contracting is a misdemeanour (felony for repeat offences or during a declared emergency)",
        "url": "https://procore.com/library/florida-contractors-license",
        "asat": "checked Oct 2026 (DBPR via Procore guide)",
    },
    ("gas", "FLORIDA"): {
        "law": "Florida Statutes Chapter 489 Part I and the Florida Building Code - Fuel Gas for natural gas piping; Chapter 527 for LP-gas",
        "regulator": "CILB (DBPR) and local building departments for natural gas piping; Florida Department of Agriculture and Consumer Services (FDACS) for LP-gas",
        "individual": "No separate state natural gas fitter licence: gas piping is done under a licensed plumbing, mechanical or other contractor licence permitted by the local building official. LP-gas (propane) work needs an FDACS LP-gas licence and qualified personnel",
        "trainee": "Apprenticeship / FDACS LP-gas qualifier rules",
        "contractor": "Certified or registered plumbing or mechanical contractor (natural gas, per local permit); FDACS LP-gas licence for propane businesses",
        "url": "https://procore.com/library/florida-contractors-license",
        "asat": "checked Oct 2026; confirm the exact contractor class the county accepts for gas piping",
    },
    ("refrigeration", "FLORIDA"): {
        "law": "Florida Statutes Chapter 489 Part I; Florida Building Code - Mechanical; federal Clean Air Act s.608",
        "regulator": "Construction Industry Licensing Board (CILB), DBPR; US EPA for refrigerants",
        "individual": "Journeyman licensing for HVAC/mechanical is local (authorised by HB 735, 2021); EPA Section 608 to handle refrigerants",
        "trainee": "Apprenticeship; EPA 608 before handling refrigerant",
        "contractor": "State Certified or Registered Air Conditioning Contractor (Class A, B or C) or Mechanical Contractor",
        "url": "https://procore.com/library/florida-contractors-license",
        "asat": "checked Oct 2026",
    },
    ("carpentry", "FLORIDA"): {
        "law": "Florida Statutes Chapter 489 Part I; Florida Building Code. HB 735 (2021) stopped local governments licensing job scopes the state does not license, such as cabinetry, flooring and finish trades",
        "regulator": "Construction Industry Licensing Board (CILB), DBPR; local building departments",
        "individual": "NO licence for carpenters. Structural work (foundations, structural walls, framing) must be done under a General, Building or Residential Contractor; the handyman exception excludes structural, plumbing and electrical work",
        "trainee": "Apprenticeship; no licence stage",
        "contractor": "Certified or Registered General, Building or Residential Contractor for structural and permitted work. EPA RRP for pre-1978 homes",
        "url": "https://flsenate.gov/Committees/BillSummaries/2021/html/735",
        "asat": "checked Oct 2026",
    },
    ("electrical", "NEW YORK"): {
        "law": "No statewide electrician licensing law in New York State; licensing is local. New York City: NYC Electrical Code and Administrative Code (DOB licensing); elsewhere county or city electrical licensing boards (e.g. Nassau, Suffolk, Westchester, Buffalo)",
        "regulator": "Local: NYC Department of Buildings (DOB) in New York City; county/city licensing boards elsewhere",
        "individual": "NO state licence. In NYC, electrical work must be done by or under a DOB-licensed Master Electrician (or a Special Electrician for a specific building); other localities issue their own licences",
        "trainee": "Apprenticeship (NY State-registered programs) working under the licensed master",
        "contractor": "In NYC: a business must be operated under a licensed Master Electrician; elsewhere follow the local board",
        "url": "https://www.nyc.gov/site/buildings/index.page",
        "asat": "checked Oct 2026",
    },
    ("plumbing", "NEW YORK"): {
        "law": "No statewide plumber licensing in New York State; licensing is local. NYC: Administrative Code Title 28 and NYC Plumbing Code",
        "regulator": "NYC Department of Buildings (DOB) in New York City; local plumbing boards elsewhere",
        "individual": "NO state licence. In NYC, plumbing work must be done by or under a DOB Licensed Master Plumber (LMP)",
        "trainee": "Apprenticeship working under the LMP",
        "contractor": "In NYC: a plumbing business must be operated under a Licensed Master Plumber; elsewhere follow the local board",
        "url": "https://www.nyc.gov/site/buildings/index.page",
        "asat": "checked Oct 2026",
    },
    ("gas", "NEW YORK"): {
        "law": "NYC Local Law 150 of 2016 and the NYC Fuel Gas Code; elsewhere the NY State Fuel Gas Code and local licensing",
        "regulator": "NYC Department of Buildings (DOB); local building departments elsewhere; the gas utility for service",
        "individual": "In NYC, since 1 Jan 2020 anyone doing gas work must hold a Master Plumber licence or a DOB Gas Work Qualification (Full, or Limited - at least 16 hours of approved training, valid 5 years) and work under the direct and continuing supervision of a Licensed Master Plumber",
        "trainee": "Limited Gas Work Qualification (16 h training or registered plumbing apprentice), under personal and immediate supervision",
        "contractor": "Gas work is performed under a Licensed Master Plumber (NYC)",
        "url": "https://www.nyc.gov/assets/buildings/pdf/gas_works_sn.pdf",
        "asat": "checked Oct 2026 (NYC DOB)",
    },
    ("refrigeration", "NEW YORK"): {
        "law": "No statewide HVAC/refrigeration licence; NYC Mechanical Code and local rules; federal Clean Air Act s.608",
        "regulator": "Local building departments (NYC DOB; FDNY for certain refrigerating-system certificates of fitness); US EPA for refrigerants",
        "individual": "NO state licence; EPA Section 608 certification required to handle refrigerants. In NYC, check DOB/FDNY requirements for the system type",
        "trainee": "Apprenticeship; EPA 608 before handling refrigerant",
        "contractor": "No state contractor licence; local licensing and permits apply (NYC home improvement work over $200 also needs a DCWP licence)",
        "url": "https://www.nyc.gov/site/buildings/index.page",
        "asat": "checked Oct 2026",
    },
    ("carpentry", "NEW YORK"): {
        "law": "No statewide contractor or carpentry licence; NY State Uniform Fire Prevention and Building Code (NYC: NYC Construction Codes)",
        "regulator": "Local building departments; NYC Department of Consumer and Worker Protection (DCWP) for home improvement contractors",
        "individual": "NO licence for carpenters",
        "trainee": "Apprenticeship; no licence stage",
        "contractor": "In NYC, a DCWP Home Improvement Contractor licence is required for residential construction, repair or remodelling work over $200; several counties (e.g. Nassau, Suffolk, Westchester) license home improvement contractors too. EPA RRP for pre-1978 homes",
        "url": "https://portal.311.nyc.gov/article?kanumber=KA-01808",
        "asat": "checked Oct 2026 (NYC 311 / DCWP)",
    },
    # ── Canada (LL-8): Ontario, British Columbia, Alberta. Checked Oct 2026 against Skilled Trades Ontario, ESA, TSSA,
    # ontario.ca, HCRA, SkilledTradesBC, Technical Safety BC, BC Housing / gov.bc.ca, Alberta tradesecrets, ALIS, alberta.ca, City of Calgary.
    ("electrical", "ONTARIO"): {
        "law": "Building Opportunities in the Skilled Trades Act, 2021 (compulsory trades); Electricity Act, 1998 Part VIII and O. Reg. 570/05 (licensing of electrical contractors and master electricians); Ontario Electrical Safety Code (O. Reg. 164/99, based on the Canadian Electrical Code Part I)",
        "regulator": "Skilled Trades Ontario (trade certification); Electrical Safety Authority (ESA) for contractor and Master Electrician licences, notifications (permits) and inspection",
        "individual": "Compulsory trade: Certificate of Qualification as Electrician - Construction and Maintenance (309A) or Electrician - Domestic and Rural (309C); the certificate is renewed every year",
        "trainee": "Registered apprentice holding a Registered Training Agreement with Skilled Trades Ontario (or a Provisional Certificate of Qualification), working under supervision",
        "contractor": "ESA Electrical Contractor Licence for any business that quotes, advertises or files notifications for electrical work, with a designated licensed Master Electrician; electrical work may be subcontracted only to another ESA-licensed contractor. Homeowners may work only on their own home and must still file a notification",
        "url": "https://esasafe.com/contractor-licensing-master-electricians/electrical-contractors-licence/",
        "asat": "checked Oct 2026 (Skilled Trades Ontario, ESA)",
    },
    ("plumbing", "ONTARIO"): {
        "law": "Building Opportunities in the Skilled Trades Act, 2021 (compulsory trades); Building Code Act, 1992 and the Ontario Building Code (Part 7 Plumbing) for permits and inspection",
        "regulator": "Skilled Trades Ontario (trade certification); the municipal building department (chief building official) issues plumbing permits and inspects",
        "individual": "Compulsory trade: Certificate of Qualification as Plumber (306A), renewed every year",
        "trainee": "Registered apprentice with a Registered Training Agreement (or a Provisional Certificate of Qualification), under supervision",
        "contractor": "No provincial plumbing contractor licence; a building permit from the municipality is needed for plumbing work. Some municipalities license plumbing or drain contractors - check locally",
        "url": "https://www.skilledtradesontario.ca/about-trades/work-in-the-compulsory-trades/",
        "asat": "checked Oct 2026 (Skilled Trades Ontario compulsory trades list)",
    },
    ("gas", "ONTARIO"): {
        "law": "Technical Standards and Safety Act, 2000; O. Reg. 215/01 Fuel Industry Certificates; CSA B149.1 Natural Gas and Propane Installation Code as adopted in Ontario",
        "regulator": "Technical Standards and Safety Authority (TSSA), Fuels Safety",
        "individual": "TSSA Gas Technician certificate: G.3, G.2 or G.1 (G.1 for appliances over 400,000 BTU/h). Oil burner work needs OBT-3/2/1",
        "trainee": "Gas technician training through a TSSA-recognised program and exams; higher classes need logged hours at the lower class (e.g. G.1 needs 3,500 hours at G.2 level)",
        "contractor": "Fuels contractors must be registered with TSSA for each technical field (e.g. gaseous fuels) and use certified technicians; check the TSSA registered fuels contractor directory",
        "url": "https://www.tssa.org/contractor-registration-and-certification",
        "asat": "checked Oct 2026 (TSSA)",
    },
    ("refrigeration", "ONTARIO"): {
        "law": "Building Opportunities in the Skilled Trades Act, 2021 (compulsory trades); O. Reg. 463/10 Ozone Depleting Substances and Other Halocarbons (Environmental Protection Act); Technical Standards and Safety Act, 2000 for gas-fired equipment",
        "regulator": "Skilled Trades Ontario (trade certification); Ministry of the Environment, Conservation and Parks (refrigerant handling); TSSA for gas-fired heating",
        "individual": "Compulsory trade: Refrigeration and Air Conditioning Systems Mechanic (313A) or Residential Air Conditioning Systems Mechanic (313D). Anyone who handles, recovers or disposes of CFC, HCFC or HFC refrigerants also needs an Ozone Depletion Prevention (ODP) card - one-day approved course (HRAI), valid 5 years",
        "trainee": "Registered apprentice with a Registered Training Agreement, under supervision; the ODP card is still needed to handle refrigerants",
        "contractor": "No separate provincial HVAC contractor licence; furnace/gas work needs TSSA fuels contractor registration and gas-certified technicians",
        "url": "https://www.ontario.ca/page/certificate-handle-refrigerants",
        "asat": "checked Oct 2026 (ontario.ca, Skilled Trades Ontario)",
    },
    ("carpentry", "ONTARIO"): {
        "law": "Carpentry is NOT a compulsory trade (General Carpenter 403A is voluntary); new home building and selling is licensed by the Home Construction Regulatory Authority; warranty under the Ontario New Home Warranties Plan Act; Ontario Building Code for permits",
        "regulator": "Home Construction Regulatory Authority (HCRA) for builder/vendor licences; Tarion for new home warranty; municipal building department for permits",
        "individual": "No licence needed to work as a carpenter; the voluntary 403A Certificate of Qualification is available",
        "trainee": "Voluntary apprenticeship registered with Skilled Trades Ontario",
        "contractor": "It is illegal to build or sell a new home without an HCRA licence (builder, vendor, or both for custom homes on the owner's land); homes must be enrolled with Tarion. Renovation contractors need no provincial licence, but some municipalities license them",
        "url": "https://hcraontario.ca/licensing-compliance/licensing/",
        "asat": "checked Oct 2026 (Skilled Trades Ontario, HCRA)",
    },
    ("electrical", "BRITISH COLUMBIA"): {
        "law": "Skilled Trades BC Act (compulsory trades since 1 Dec 2023); Safety Standards Act and Electrical Safety Regulation (Canadian Electrical Code Part I as adopted in BC)",
        "regulator": "SkilledTradesBC (trade certification); Technical Safety BC (contractor licences, Field Safety Representatives, permits and inspection; some municipalities issue electrical permits)",
        "individual": "Compulsory trade: certified journeyperson Construction Electrician (or Industrial Electrician / Powerline Technician for that work); experienced uncertified workers may hold a Temporary Authorization for up to 5 years",
        "trainee": "Registered apprentice with SkilledTradesBC; 2:1 apprentice-to-journeyperson ratio",
        "contractor": "Technical Safety BC electrical contractor licence; the licence scope follows the class of the designated Field Safety Representative (FSR) certificate. Regulated work needs a licence, employment by a licensed contractor, or a homeowner authorization",
        "url": "https://technicalsafetybc.ca/electrical-licences",
        "asat": "checked Oct 2026 (SkilledTradesBC, Technical Safety BC)",
    },
    ("plumbing", "BRITISH COLUMBIA"): {
        "law": "BC Plumbing Code (part of the BC Building Code) and municipal plumbing bylaws; plumber is NOT one of the compulsory trades under the Skilled Trades BC Act",
        "regulator": "Municipal or regional district plumbing inspection (permits); SkilledTradesBC for voluntary certification",
        "individual": "No provincial licence required for plumbers; Red Seal / BC Plumber certification is voluntary. Many municipalities require a permit holder to be qualified or to sign a plumbing declaration - check locally",
        "trainee": "Voluntary apprenticeship registered with SkilledTradesBC",
        "contractor": "No provincial plumbing contractor licence; municipal plumbing permits. Gas piping and appliances are separate - see gas",
        "url": "https://skilledtradesbc.ca/skilled-trades-certification-now-effect",
        "asat": "checked Oct 2026 (SkilledTradesBC compulsory trades list)",
    },
    ("gas", "BRITISH COLUMBIA"): {
        "law": "Skilled Trades BC Act (compulsory since 1 Dec 2023); Safety Standards Act and Gas Safety Regulation (Division 2: gas fitter certification); CSA B149.1 as adopted",
        "regulator": "SkilledTradesBC (trade certification); Technical Safety BC (gas fitter certificates, contractor licences, permits; some municipalities issue gas permits)",
        "individual": "Compulsory trade: Gasfitter Class A or Class B certificate of qualification (Class A covers any gas system except vehicle fuel systems, under a permit)",
        "trainee": "Registered apprentice with SkilledTradesBC, 2:1 ratio",
        "contractor": "Technical Safety BC gas contractor licence (surety bond, annual renewal). Homeowner gas permits exist but not for strata units, non-strata duplexes or home businesses - those must hire a licensed contractor",
        "url": "https://technicalsafetybc.ca/gas-licences",
        "asat": "checked Oct 2026 (SkilledTradesBC, Technical Safety BC)",
    },
    ("refrigeration", "BRITISH COLUMBIA"): {
        "law": "Skilled Trades BC Act (compulsory since 1 Dec 2023); Safety Standards Act and Power Engineers, Boilers, Pressure Vessels and Refrigeration Safety Regulation",
        "regulator": "SkilledTradesBC (trade certification); Technical Safety BC (refrigeration contractor licences)",
        "individual": "Compulsory trade: certified Refrigeration and Air Conditioning Mechanic; sheet metal work is also compulsory (Sheet Metal Worker). Gas-fired heating also needs a gas fitter certificate",
        "trainee": "Registered apprentice with SkilledTradesBC, 2:1 ratio",
        "contractor": "Technical Safety BC refrigeration contractor licence (Class REF), naming a qualified refrigeration mechanic; refrigerant-handling certification may also apply",
        "url": "https://technicalsafetybc.ca/boilers-pressure-vessels-and-refrigeration-contractor-licenses",
        "asat": "checked Oct 2026 (SkilledTradesBC, Technical Safety BC)",
    },
    ("carpentry", "BRITISH COLUMBIA"): {
        "law": "Carpentry is NOT a compulsory trade; Homeowner Protection Act (residential builder licensing and home warranty insurance); BC Building Code",
        "regulator": "BC Housing - Licensing and Consumer Services Branch; local building departments for permits",
        "individual": "No licence needed to work as a carpenter; Red Seal certification is voluntary",
        "trainee": "Voluntary apprenticeship registered with SkilledTradesBC",
        "contractor": "Anyone starting new residential construction must be a Licensed Residential Builder and buy third-party home warranty insurance before starting, even where no building permit is needed; owner-builders need BC Housing authorization",
        "url": "https://www2.gov.bc.ca/gov/content/industry/construction-industry/building-codes-standards/legislation/homeowner-protection-act",
        "asat": "checked Oct 2026 (gov.bc.ca)",
    },
    ("electrical", "ALBERTA"): {
        "law": "Skilled Trades and Apprenticeship Education Act (compulsory trades); Safety Codes Act and the Certification and Permit Regulation; Canadian Electrical Code Part I as adopted in Alberta",
        "regulator": "Alberta Apprenticeship and Industry Training (trade certification); Safety Codes Council (Master Electrician certificate); municipalities or agencies accredited under the Safety Codes Act issue permits",
        "individual": "Compulsory trade: journeyperson Electrician certificate",
        "trainee": "Registered apprentice in the Electrician trade",
        "contractor": "Only a holder of a Master Electrician Certificate of Competency (Safety Codes Council) can obtain electrical permits for others; homeowners may get permits for their own home",
        "url": "https://alis.alberta.ca/occinfo/certifications-in-alberta/electrical-contractor-master-electrician",
        "asat": "checked Oct 2026 (Alberta tradesecrets, ALIS)",
    },
    ("plumbing", "ALBERTA"): {
        "law": "Skilled Trades and Apprenticeship Education Act (compulsory trades); Safety Codes Act (National Plumbing Code as adopted in Alberta)",
        "regulator": "Alberta Apprenticeship and Industry Training (trade certification); municipal or accredited safety codes permits and inspection",
        "individual": "Compulsory trade: journeyperson Plumber certificate",
        "trainee": "Registered apprentice in the Plumber trade",
        "contractor": "All plumbing installations need permits; no separate provincial plumbing contractor licence. Homeowners may get plumbing permits for their own home (e.g. City of Calgary)",
        "url": "https://tradesecrets.alberta.ca/trades-in-alberta/compulsory-certification-trades",
        "asat": "checked Oct 2026 (Alberta tradesecrets, City of Calgary)",
    },
    ("gas", "ALBERTA"): {
        "law": "Skilled Trades and Apprenticeship Education Act (compulsory trades); Safety Codes Act (gas permits; CSA B149.1 as adopted)",
        "regulator": "Alberta Apprenticeship and Industry Training (trade certification); municipal or accredited safety codes permits and inspection",
        "individual": "Compulsory trade: Gasfitter Class A or Class B certificate",
        "trainee": "Registered apprentice in the Gasfitter trade",
        "contractor": "Gas permits are required; a homeowner can get a gas permit only if they are an Alberta-certified journeyman gasfitter living in the single-family home (City of Calgary rule) - otherwise hire a certified gasfitter",
        "url": "https://calgary.ca/development/permits/plumbing-gas.html",
        "asat": "checked Oct 2026 (Alberta tradesecrets, City of Calgary)",
    },
    ("refrigeration", "ALBERTA"): {
        "law": "Skilled Trades and Apprenticeship Education Act (compulsory trades); Safety Codes Act for permits",
        "regulator": "Alberta Apprenticeship and Industry Training (trade certification); safety codes permits and inspection",
        "individual": "Compulsory trade: journeyperson Refrigeration and Air Conditioning Mechanic; Sheet Metal Worker and Steamfitter-Pipefitter are also compulsory. Gas-fired heating also needs a gasfitter certificate",
        "trainee": "Registered apprentice in the trade",
        "contractor": "No separate provincial HVAC contractor licence; permits under the Safety Codes Act. Refrigerant-handling certification may also apply",
        "url": "https://tradesecrets.alberta.ca/trades-in-alberta/compulsory-certification-trades",
        "asat": "checked Oct 2026 (Alberta tradesecrets)",
    },
    ("carpentry", "ALBERTA"): {
        "law": "Carpentry is NOT a compulsory trade in Alberta; New Home Buyer Protection Act (builder licensing and new home warranty); Safety Codes Act for building permits",
        "regulator": "Alberta Municipal Affairs - builder licensing and New Home Buyer Protection System; municipal or accredited building permits",
        "individual": "No licence needed to work as a carpenter; the Carpenter certificate is optional",
        "trainee": "Optional apprenticeship in the Carpenter trade",
        "contractor": "All residential builders must hold a builder licence to get permits for new homes and register each home in the New Home Buyer Protection System; licences last one year. New builder competency requirements apply to new applicants and renewals from 1 Jan 2027",
        "url": "https://www.alberta.ca/builder-licensing",
        "asat": "checked Oct 2026 (alberta.ca)",
    },
    # ── EU (LL-9): Ireland, Germany, France. Checked Oct 2026 against CRU, Gas Networks Ireland, EPA Ireland, CIF/CIRI and
    # legal commentary, Handwerkskammer and IHK guidance, ecologie.gouv.fr, Code de l'environnement and Chambre des Metiers guidance.
    # EU-wide: F-gas work on stationary refrigeration, air conditioning and heat pumps needs a personnel certificate, and firms
    # doing that work for others need a company certificate (EU F-gas Regulation and Implementing Regulation 2015/2067).
    ("electrical", "IRELAND"): {
        "law": "Electricity Regulation Act 1999 as amended by the Energy (Miscellaneous Provisions) Act 2006 (electrical safety supervision; Restricted Electrical Works); National Rules for Electrical Installations I.S. 10101",
        "regulator": "Commission for Regulation of Utilities (CRU), through the Safe Electric scheme",
        "individual": "No personal licence for electricians; Restricted Electrical Works (domestic and similar) may be done only by a Registered Electrical Contractor (REC) with Safe Electric, using qualified electricians (craft apprenticeship)",
        "trainee": "Electrical craft apprenticeship (SOLAS), working for an employer",
        "contractor": "REC registration with Safe Electric is the law; the REC issues a Completion Certificate for the work. Doing restricted works or claiming to be an REC without registration is an offence (up to EUR 50,000 and/or 3 years)",
        "url": "https://cru.ie/regulations-policy/safety/rgi-and-safe-electric",
        "asat": "checked Oct 2026 (CRU)",
    },
    ("plumbing", "IRELAND"): {
        "law": "No licensing law for plumbers; Building Regulations (Part G hygiene, Part H drainage) and Building Control Regulations; any gas pipework falls under the RGI scheme (see gas)",
        "regulator": "Local building control authority for building works; CRU for gas work",
        "individual": "No licence required to work as a plumber (water and heating); plumbing craft apprenticeship is the recognised qualification. Work on domestic gas installations needs an RGI",
        "trainee": "Plumbing craft apprenticeship (SOLAS)",
        "contractor": "No plumbing contractor licence. The Construction Industry Register Ireland (CIRI) under the Regulation of Providers of Building Works Act 2022 is being made mandatory in phases - check whether the plumbing division has commenced",
        "url": "https://dwfgroup.com/en/news-and-insights/insights/2026/3/ciri-goes-mandatory",
        "asat": "checked Oct 2026 (CIRI commentary Mar 2026)",
    },
    ("gas", "IRELAND"): {
        "law": "Electricity Regulation Act 1999 as amended by the Energy (Miscellaneous Provisions) Act 2006 (gas safety; extended to LPG); Gas Safety Supervisory Criteria",
        "regulator": "Commission for Regulation of Utilities (CRU), through the Registered Gas Installer (RGI) scheme",
        "individual": "By law only a Registered Gas Installer may work on domestic gas installations (installation, servicing, repair)",
        "trainee": "Gas safety training and assessment for RGI registration (usually after a plumbing or heating apprenticeship)",
        "contractor": "RGI registration (with gas training and insurance); the RGI issues a Declaration of Conformance / Completion Certificate after the work. Unregistered gas work is prosecuted by the CRU",
        "url": "https://www.gasnetworks.ie/gas-safety/rgi",
        "asat": "checked Oct 2026 (CRU, Gas Networks Ireland)",
    },
    ("refrigeration", "IRELAND"): {
        "law": "EU F-gas Regulation and Implementing Regulation (EU) 2015/2067; S.I. No. 658 of 2016 (offence to work without certification)",
        "regulator": "Environmental Protection Agency (EPA); personnel certification through QQI-linked training; company certification by F-gas Registration Ltd (FGR)",
        "individual": "Personnel F-gas certificate to install, service, repair, leak-check, recover or decommission stationary refrigeration, air conditioning and heat pump equipment. UK F-gas awards are not recognised since 1 Jan 2021",
        "trainee": "Refrigeration and air conditioning craft apprenticeship; F-gas certificate still needed for F-gas work",
        "contractor": "F-gas company certificate (FGR) for any business, including sole traders, doing this work for others; certificates from other EU states are recognised. Gas-fired heating also needs an RGI",
        "url": "https://www.epa.ie/our-services/compliance--enforcement/air/fluorinated-greenhouse-gases-f-gases/training--qualifications-for-ods-and-f-gases/",
        "asat": "checked Oct 2026 (EPA)",
    },
    ("carpentry", "IRELAND"): {
        "law": "No licensing law for carpenters; Building Control Acts and Building Control (Amendment) Regulations 2014 (commencement notices; assigned certifier for new dwellings and extensions over 40 m2); Regulation of Providers of Building Works Act 2022 (CIRI)",
        "regulator": "Local building control authority; CIRI (run by the Construction Industry Federation as Registration Body)",
        "individual": "No licence needed to work as a carpenter; carpentry and joinery craft apprenticeship is the recognised qualification",
        "trainee": "Carpentry and joinery craft apprenticeship (SOLAS)",
        "contractor": "CIRI registration was voluntary until the Act's sections 26-59 commence; government said mandatory registration starts during 2026, first for builders of housing developments, apartments and multi-unit dwellings, each division with a 12-month grace period - confirm the current position",
        "url": "https://dwfgroup.com/en/news-and-insights/insights/2026/3/ciri-goes-mandatory",
        "asat": "checked Oct 2026 (CIRI commentary Mar 2026)",
    },
    ("electrical", "GERMANY"): {
        "law": "Handwerksordnung (HwO) Anlage A - Elektrotechniker is a licensed craft (Meisterpflicht); Niederspannungsanschlussverordnung (NAV) s.13; DIN VDE 0100 series; DGUV Vorschrift 3",
        "regulator": "Handwerkskammer (Handwerksrolle); the local grid operator (Netzbetreiber) keeps the Installateurverzeichnis",
        "individual": "No personal licence for employees, but work must be done by or under an electrically skilled person (Elektrofachkraft); the business must be run by a Meister or equivalent",
        "trainee": "Dual apprenticeship (Elektroniker), then Geselle; Meister for running a business",
        "contractor": "Entry in the Handwerksrolle (Meister, or an employed Meister as Betriebsleiter, or the 'Altgesellen' route: 6 years in the trade incl. 4 in a leading role, or an exemption). Work behind the house connection fuse also needs entry in the grid operator's Installateurverzeichnis (NAV s.13)",
        "url": "https://www.hwk-muenster.de/hwk-muenster/Service-Center/Handwerksrolle/Installateurverzeichnis%20Merkblatt%20Fachverband.pdf",
        "asat": "checked Oct 2026 (Handwerkskammer Muenster, HwO guidance)",
    },
    ("plumbing", "GERMANY"): {
        "law": "HwO Anlage A - Installateur und Heizungsbauer and Klempner are licensed crafts (Meisterpflicht); AVBWasserV s.12 (water installations); DIN 1988 / DIN EN 806 (drinking water)",
        "regulator": "Handwerkskammer (Handwerksrolle); the water supplier keeps the Installateurverzeichnis",
        "individual": "No personal licence for employees; the business must be run by a Meister or equivalent",
        "trainee": "Dual apprenticeship (Anlagenmechaniker SHK), then Geselle",
        "contractor": "Entry in the Handwerksrolle, and entry in the water supplier's Installateurverzeichnis to install, extend, change or maintain the installation behind the house connection (AVBWasserV s.12)",
        "url": "https://www.hwk-muenster.de/hwk-muenster/Service-Center/Handwerksrolle/Installateurverzeichnis%20Merkblatt%20Fachverband.pdf",
        "asat": "checked Oct 2026 (Handwerkskammer Muenster)",
    },
    ("gas", "GERMANY"): {
        "law": "HwO Anlage A (Installateur und Heizungsbauer); Niederdruckanschlussverordnung (NDAV) ss.13, 13a; DVGW-TRGI (technical rules for gas installations)",
        "regulator": "Handwerkskammer (Handwerksrolle); the gas grid operator keeps the Installateurverzeichnis",
        "individual": "No personal licence for employees; work must be carried out by a business in the grid operator's Installateurverzeichnis",
        "trainee": "Dual apprenticeship (Anlagenmechaniker SHK)",
        "contractor": "Entry in the Handwerksrolle plus the gas grid operator's Installateurverzeichnis to install, extend, change or maintain gas installations behind the main shut-off device (NDAV s.13)",
        "url": "https://www.hwk-muenster.de/hwk-muenster/Service-Center/Handwerksrolle/Installateurverzeichnis%20Merkblatt%20Fachverband.pdf",
        "asat": "checked Oct 2026 (Handwerkskammer Muenster)",
    },
    ("refrigeration", "GERMANY"): {
        "law": "HwO Anlage A - Kaelteanlagenbauer is a licensed craft; Chemikalien-Klimaschutzverordnung (ChemKlimaschutzV) s.5 (personnel) and s.6 (company); EU Implementing Regulation 2015/2067",
        "regulator": "Handwerkskammer (Handwerksrolle); the state environmental/trade inspection authority certifies companies (e.g. Gewerbeaufsicht)",
        "individual": "Sachkundebescheinigung (categories I-IV; Cat. I is the widest) to install, service, repair, leak-check, recover or decommission stationary refrigeration, AC and heat pumps. The 'kleiner Kaelteschein' does not replace it",
        "trainee": "Dual apprenticeship (Mechatroniker fuer Kaeltetechnik - graduates receive the Sachkunde without a further exam)",
        "contractor": "Handwerksrolle entry plus company certification under ChemKlimaschutzV s.6 for firms doing this work",
        "url": "https://www.ihk.de/pfalz/innovation-umwelt-und-existenzgruendung/energie-umwelt-klimaschutz/umweltschutz/chemikalien-und-gefahrstoffe/chemikalien-klimaschutzverordnung-1279210",
        "asat": "checked Oct 2026 (IHK Pfalz, HwO guidance)",
    },
    ("carpentry", "GERMANY"): {
        "law": "HwO Anlage A - Zimmerer (structural carpentry) and Tischler (joinery) are licensed crafts (Meisterpflicht); state building codes (Landesbauordnungen)",
        "regulator": "Handwerkskammer (Handwerksrolle); local building authority for permits",
        "individual": "No personal licence for employees; the business must be run by a Meister or equivalent",
        "trainee": "Dual apprenticeship (Zimmerer or Tischler), then Geselle",
        "contractor": "Entry in the Handwerksrolle (Meister, employed Meister as Betriebsleiter, Altgesellen route or exemption) before trading in Zimmerer or Tischler work",
        "url": "https://www.streit-software.de/wissen/meisterpflicht-handwerk",
        "asat": "checked Oct 2026 (HwO Anlage A guidance)",
    },
    ("electrical", "FRANCE"): {
        "law": "Loi n. 96-603 du 5 juillet 1996 art.16 (qualification professionnelle); Code de la construction et de l'habitation art. R111-12 and decree 72-1120 / decree 2001-222 (attestation de conformite); NF C 15-100; Code du travail (habilitation electrique, NF C 18-510)",
        "regulator": "Chambre de Metiers et de l'Artisanat (registration); Consuel (approved body that endorses the attestation de conformite)",
        "individual": "Workers need an habilitation electrique issued by their employer after training (NF C 18-510); the business must be controlled by someone with a CAP/BEP in the trade or 3 years' experience in it",
        "trainee": "CAP / BEP / Bac Pro electricite, apprenticeship (alternance)",
        "contractor": "Qualification professionnelle (CAP/BEP or 3 years' EU/EEA experience in the trade); mandatory decennale insurance for building works. New or fully renovated installations need an attestation de conformite endorsed by Consuel before the distributor energises them",
        "url": "https://www.ecologie.gouv.fr/politiques-publiques/securite-installations-electriques-gaz",
        "asat": "checked Oct 2026 (ecologie.gouv.fr, Chambre des Metiers guidance)",
    },
    ("plumbing", "FRANCE"): {
        "law": "Loi n. 96-603 du 5 juillet 1996 art.16 (plombier-chauffagiste is a regulated activity); DTU 60.1 (plumbing) and building codes",
        "regulator": "Chambre de Metiers et de l'Artisanat",
        "individual": "No personal licence; the business must be controlled by someone with a CAP/BEP in the trade or 3 years' experience",
        "trainee": "CAP installateur sanitaire / installateur thermique, apprenticeship",
        "contractor": "Qualification professionnelle; mandatory decennale insurance for building works; RGE label needed for clients to claim energy-renovation grants",
        "url": "https://www.creerentreprise.fr/liste-activites-artisanales-reglementees-qualification-professionnelle/",
        "asat": "checked Oct 2026 (Chambre des Metiers guidance)",
    },
    ("gas", "FRANCE"): {
        "law": "Code de la construction et de l'habitation art. R111-12; arrete du 2 aout 1977 (domestic installations under 70 kW) and arrete du 23 juin 1978 (larger); Loi 96-603 (qualification)",
        "regulator": "Qualigaz (the only body approved to endorse gas certificats de conformite); Chambre de Metiers",
        "individual": "No personal gas licence; the business needs a qualified person (CAP/BEP or 3 years' experience). The 'PG - Professionnel du Gaz' label is voluntary",
        "trainee": "CAP / BP installateur thermique, apprenticeship",
        "contractor": "Qualification professionnelle; decennale insurance; new or fully renovated gas installations need a certificat de conformite signed by the installer and endorsed by Qualigaz before gas is supplied",
        "url": "https://www.ecologie.gouv.fr/politiques-publiques/securite-installations-electriques-gaz",
        "asat": "checked Oct 2026 (ecologie.gouv.fr)",
    },
    ("refrigeration", "FRANCE"): {
        "law": "Code de l'environnement art. R543-75 to R543-123 (fluides frigorigenes), in particular R543-99 (attestation de capacite) and R543-106; EU F-gas rules",
        "regulator": "Approved organisations (organismes agrees) issue the attestation de capacite; certified bodies issue the attestation d'aptitude",
        "individual": "Personnel need an attestation d'aptitude (or an equivalent certificate from another EU state, translated into French) for the equipment and activity types they handle",
        "trainee": "CAP / Bac Pro froid et climatisation, apprenticeship",
        "contractor": "Attestation de capacite from an approved organisation, per establishment, valid up to 5 years, listing equipment and activity types; decennale insurance for building works",
        "url": "https://content.preventionbtp.fr/pdf/droit_de_la_prevention/article-r543-99-du-code-de-l-environnement.pdf",
        "asat": "checked Oct 2026 (Code de l'environnement)",
    },
    ("carpentry", "FRANCE"): {
        "law": "Loi n. 96-603 art.16 (menuisier is a regulated activity; check charpentier with the Chambre de Metiers); Code des assurances L241-1 (decennale insurance)",
        "regulator": "Chambre de Metiers et de l'Artisanat",
        "individual": "No personal licence; the business must be controlled by someone with a CAP/BEP in the trade or 3 years' experience",
        "trainee": "CAP menuisier / charpentier bois, apprenticeship",
        "contractor": "Qualification professionnelle; mandatory decennale insurance for building works",
        "url": "https://www.creerentreprise.fr/liste-activites-artisanales-reglementees-qualification-professionnelle/",
        "asat": "checked Oct 2026 (Chambre des Metiers guidance)",
    },
    # ── US (LL-19): Illinois, Pennsylvania, Ohio, Georgia, North Carolina, Washington. Checked Oct 2026 against IDPH,
    # City of Chicago, PA Attorney General, Ohio OCILB guides, Georgia Code 43-14-6, NC Session Law 2023-108, WA L&I.
    # Federal rules apply in every state: EPA Section 608 (refrigerants) and EPA RRP (lead-safe renovation, pre-1978 homes).
    ("electrical", "ILLINOIS"): {
        "law": "No statewide electrician licence in Illinois; licensing is by city or county (e.g. Chicago Electrical Code and Municipal Code)",
        "regulator": "The local building department (e.g. Chicago Department of Buildings)",
        "individual": "NO state licence. Many cities license electricians: Chicago requires a Supervising Electrician licence (exam, age 21, 2 years' supervised experience); Aurora, Springfield, Peoria and others have their own rules - check the city",
        "trainee": "Apprenticeship (e.g. IBEW/NECA JATC) working under a licensed electrician where the city requires one",
        "contractor": "Local licence or registration. In Chicago an electrical contractor must be owned by or employ a Supervising Electrician who directs all its work",
        "url": "https://chicago.gov/city/en/depts/bldgs/supp_info/TLdetails/SupvEL.html",
        "asat": "checked Oct 2026 (City of Chicago, Illinois licensing guides)",
    },
    ("plumbing", "ILLINOIS"): {
        "law": "Illinois Plumbing License Law (225 ILCS 320); Illinois Plumbing Code (77 Ill. Adm. Code 890)",
        "regulator": "Illinois Department of Public Health (IDPH)",
        "individual": "State-licensed plumber (at least 4 years as a licensed apprentice, approved coursework, state exam; renews each April 30)",
        "trainee": "Licensed apprentice plumber (age 16+, sponsored by a licensed plumber or approved apprenticeship; 6-year cap)",
        "contractor": "Plumbing contractor registration with IDPH for anyone doing plumbing for others, with liability and workers' compensation insurance. Lawn sprinkler (irrigation) contractors register separately",
        "url": "https://dph.illinois.gov/topics-services/environmental-health-protection/plumbing.html",
        "asat": "checked Oct 2026 (IDPH)",
    },
    ("gas", "ILLINOIS"): {
        "law": "No statewide gas fitter licence; gas piping is regulated through local codes and permits (gas piping is often done by licensed plumbers or HVAC contractors)",
        "regulator": "The local building department",
        "individual": "NO state gas licence - check the city or county",
        "trainee": "No state trainee registration",
        "contractor": "Local licence or registration and permit, where the city requires it",
        "url": "https://dph.illinois.gov/topics-services/environmental-health-protection/plumbing.html",
        "asat": "checked Oct 2026 (Illinois licensing guides)",
    },
    ("refrigeration", "ILLINOIS"): {
        "law": "No statewide HVAC licence; federal EPA Section 608 certification to handle refrigerants",
        "regulator": "Local building departments; US EPA for refrigerant handling",
        "individual": "NO state licence; EPA 608 certification needed to handle refrigerants. Some cities license HVAC contractors - check locally",
        "trainee": "No state trainee registration",
        "contractor": "Local licence or registration where required",
        "url": "https://www.epa.gov/section608",
        "asat": "checked Oct 2026",
    },
    ("carpentry", "ILLINOIS"): {
        "law": "No statewide general contractor or carpenter licence; roofing contractors are licensed by the state (Illinois Roofing Industry Licensing Act)",
        "regulator": "Local building departments; Illinois Department of Financial and Professional Regulation (IDFPR) for roofing",
        "individual": "No licence needed to work as a carpenter",
        "trainee": "Voluntary apprenticeship",
        "contractor": "Local licence or registration (e.g. Chicago general contractor licence); state roofing licence for roofing work",
        "url": "https://idfpr.illinois.gov/",
        "asat": "checked Oct 2026 (Illinois licensing guides)",
    },
    ("electrical", "PENNSYLVANIA"): {
        "law": "No statewide electrician licence; Pennsylvania Uniform Construction Code for permits and inspection; Home Improvement Consumer Protection Act for residential work",
        "regulator": "Municipalities (e.g. Philadelphia Department of Licenses and Inspections, City of Pittsburgh); PA Attorney General for home improvement registration",
        "individual": "NO state licence; Philadelphia, Pittsburgh and some other municipalities license electricians - check locally",
        "trainee": "Apprenticeship; local rules where the municipality licenses electricians",
        "contractor": "Home Improvement Contractor registration with the Attorney General if doing $5,000 or more of home improvements a year; local licence where required",
        "url": "https://www.attorneygeneral.gov/home-improvement-contractor-registration/",
        "asat": "checked Oct 2026 (PA Attorney General)",
    },
    ("plumbing", "PENNSYLVANIA"): {
        "law": "No statewide plumber licence; Uniform Construction Code; local plumbing codes and licensing",
        "regulator": "Municipalities and counties (e.g. Philadelphia, Pittsburgh, Allegheny County Health Department)",
        "individual": "NO state licence; many municipalities license master and journeyman plumbers - check locally",
        "trainee": "Apprenticeship; local registration where required",
        "contractor": "Home Improvement Contractor registration for $5,000+ a year of residential work; local licence where required",
        "url": "https://www.attorneygeneral.gov/home-improvement-contractor-registration/",
        "asat": "checked Oct 2026 (PA Attorney General, PA licensing guides)",
    },
    ("gas", "PENNSYLVANIA"): {
        "law": "No statewide gas fitter licence; Uniform Construction Code (fuel gas code) and local rules",
        "regulator": "Municipalities",
        "individual": "NO state licence - check the municipality (some license gas fitters or require a plumber or HVAC licence)",
        "trainee": "No state trainee registration",
        "contractor": "Home Improvement Contractor registration for $5,000+ a year of residential work; local licence and permit where required",
        "url": "https://www.attorneygeneral.gov/home-improvement-contractor-registration/",
        "asat": "checked Oct 2026",
    },
    ("refrigeration", "PENNSYLVANIA"): {
        "law": "No statewide HVAC licence; federal EPA Section 608 for refrigerants",
        "regulator": "Municipalities; US EPA",
        "individual": "NO state licence; EPA 608 certification to handle refrigerants; some cities license HVAC (e.g. Philadelphia) - check locally",
        "trainee": "No state trainee registration",
        "contractor": "Home Improvement Contractor registration for $5,000+ a year of residential work; local licence where required",
        "url": "https://www.epa.gov/section608",
        "asat": "checked Oct 2026",
    },
    ("carpentry", "PENNSYLVANIA"): {
        "law": "Home Improvement Consumer Protection Act (registration, written contracts); no statewide general contractor licence",
        "regulator": "Pennsylvania Office of Attorney General; municipalities",
        "individual": "No licence needed to work as a carpenter",
        "trainee": "Voluntary apprenticeship",
        "contractor": "Home Improvement Contractor registration with the Attorney General if doing $5,000 or more of home improvements a year (with liability insurance); local licence in some cities",
        "url": "https://www.attorneygeneral.gov/home-improvement-contractor-registration/",
        "asat": "checked Oct 2026 (PA Attorney General)",
    },
    ("electrical", "OHIO"): {
        "law": "Ohio Revised Code Chapter 4740 (Ohio Construction Industry Licensing Board - commercial trade contractors); residential work regulated locally",
        "regulator": "Ohio Construction Industry Licensing Board (OCILB), Ohio Department of Commerce; local building departments",
        "individual": "NO state licence for individual electricians; the contractor's licence holder (qualifying agent) is licensed",
        "trainee": "Apprenticeship; no state trainee registration",
        "contractor": "OCILB electrical contractor licence for COMMERCIAL work (about 5 years' experience, Business & Law plus trade exams, liability insurance). Residential electrical contractors register with the city or county",
        "url": "https://www.housecallpro.com/licensing/general-contractor/ohio/",
        "asat": "checked Oct 2026 (Ohio OCILB guides)",
    },
    ("plumbing", "OHIO"): {
        "law": "ORC Chapter 4740 (OCILB) for commercial plumbing contractors; local licensing for residential",
        "regulator": "OCILB; local building and health departments",
        "individual": "NO state licence for individual plumbers; some cities license plumbers",
        "trainee": "Apprenticeship",
        "contractor": "OCILB plumbing contractor licence for COMMERCIAL work; residential plumbing contractors register locally",
        "url": "https://www.housecallpro.com/licensing/general-contractor/ohio/",
        "asat": "checked Oct 2026 (Ohio OCILB guides)",
    },
    ("gas", "OHIO"): {
        "law": "No separate state gas fitter licence; gas piping falls under OCILB plumbing or HVAC (commercial) and local rules",
        "regulator": "OCILB (commercial); local building departments",
        "individual": "NO state licence for individuals",
        "trainee": "Apprenticeship",
        "contractor": "OCILB HVAC or plumbing contractor licence for commercial work; local registration and permit for residential",
        "url": "https://www.housecallpro.com/licensing/general-contractor/ohio/",
        "asat": "checked Oct 2026",
    },
    ("refrigeration", "OHIO"): {
        "law": "ORC Chapter 4740 - OCILB licenses HVAC, hydronics and refrigeration contractors (commercial); EPA Section 608",
        "regulator": "OCILB; US EPA",
        "individual": "NO state licence for individual technicians; EPA 608 certification to handle refrigerants",
        "trainee": "Apprenticeship",
        "contractor": "OCILB HVAC, hydronics or refrigeration contractor licence for COMMERCIAL work; residential HVAC contractors register locally",
        "url": "https://www.housecallpro.com/licensing/general-contractor/ohio/",
        "asat": "checked Oct 2026 (Ohio OCILB guides)",
    },
    ("carpentry", "OHIO"): {
        "law": "No statewide general contractor licence; Home Construction Service Suppliers Act (ORC Chapter 4722) for residential contracts over $25,000",
        "regulator": "Cities and counties; Ohio Attorney General for ORC 4722",
        "individual": "No licence needed to work as a carpenter",
        "trainee": "Voluntary apprenticeship",
        "contractor": "Local registration in each city (e.g. Columbus, Cleveland, Cincinnati), often with insurance and a bond; written contracts and insurance under ORC 4722 for home construction over $25,000",
        "url": "https://www.housecallpro.com/licensing/general-contractor/ohio/",
        "asat": "checked Oct 2026",
    },
    ("electrical", "GEORGIA"): {
        "law": "O.C.G.A. Title 43 Chapter 14 (Construction Industry Licensing Board); Georgia State Minimum Standard Codes (NEC as amended)",
        "regulator": "Georgia Construction Industry Licensing Board - Division of Electrical Contractors (Secretary of State)",
        "individual": "No separate journeyman electrician licence; the licensed electrical contractor is responsible for the work",
        "trainee": "Apprenticeship / work under the licensed contractor",
        "contractor": "Electrical contractor licence: Class I (restricted - single-family up to 3 levels or multifamily up to 2 levels, single-phase, max 400 A service) or Class II (unrestricted). Needed whatever the job value",
        "url": "https://law.justia.com/codes/georgia/2022/title-43/chapter-14/section-43-14-6/",
        "asat": "checked Oct 2026 (Georgia Code 43-14-6)",
    },
    ("plumbing", "GEORGIA"): {
        "law": "O.C.G.A. Title 43 Chapter 14; Georgia State Minimum Standard Plumbing Code",
        "regulator": "Georgia Construction Industry Licensing Board - Division of Master Plumbers and Journeyman Plumbers",
        "individual": "Journeyman Plumber licence; Master Plumber Class I (single-family, one-level two-family, commercial up to 10,000 sq ft) or Class II (unrestricted)",
        "trainee": "Work under a licensed master plumber",
        "contractor": "Master Plumber licence needed to contract for plumbing work",
        "url": "https://law.justia.com/codes/georgia/2022/title-43/chapter-14/section-43-14-6/",
        "asat": "checked Oct 2026 (Georgia Code 43-14-6)",
    },
    ("gas", "GEORGIA"): {
        "law": "No separate state gas fitter licence; gas piping and appliances fall within the plumbing and conditioned air licences (O.C.G.A. 43-14) and the Georgia State Minimum Standard Fuel Gas Code",
        "regulator": "Georgia Construction Industry Licensing Board; local permits",
        "individual": "Work under a Master Plumber or Conditioned Air Contractor licence holder within that licence's scope",
        "trainee": "Work under a licensed contractor",
        "contractor": "Master Plumber or Conditioned Air Contractor licence, plus the local permit",
        "url": "https://law.justia.com/codes/georgia/2022/title-43/chapter-14/section-43-14-6/",
        "asat": "checked Oct 2026",
    },
    ("refrigeration", "GEORGIA"): {
        "law": "O.C.G.A. Title 43 Chapter 14; EPA Section 608",
        "regulator": "Georgia Construction Industry Licensing Board - Division of Conditioned Air Contractors; US EPA",
        "individual": "No separate technician licence; EPA 608 certification to handle refrigerants",
        "trainee": "Work under the licensed contractor",
        "contractor": "Conditioned Air Contractor licence: Class I (restricted - up to 175,000 BTU heating and 5 tons cooling) or Class II (unrestricted)",
        "url": "https://law.justia.com/codes/georgia/2022/title-43/chapter-14/section-43-14-6/",
        "asat": "checked Oct 2026 (Georgia Code 43-14-6)",
    },
    ("carpentry", "GEORGIA"): {
        "law": "O.C.G.A. Title 43 Chapter 41 (residential and general contractors)",
        "regulator": "State Licensing Board for Residential and General Contractors (Secretary of State)",
        "individual": "No licence needed to work as a carpenter",
        "trainee": "Voluntary apprenticeship",
        "contractor": "State licence for work over $2,500: Residential-Basic, Residential-Light Commercial, General Contractor, or General Contractor Limited Tier (up to $1,000,000 per project)",
        "url": "https://www.housecallpro.com/licensing/general-contractor/georgia/",
        "asat": "checked Oct 2026",
    },
    ("electrical", "NORTH CAROLINA"): {
        "law": "N.C. Gen. Stat. Chapter 87 Article 4 (electrical contractors); NC State Building Code - Electrical (NEC as amended)",
        "regulator": "North Carolina State Board of Examiners of Electrical Contractors",
        "individual": "No state journeyman licence; work is done under a licensed electrical contractor's qualified individual",
        "trainee": "Apprenticeship / work under the licensed contractor",
        "contractor": "Electrical contractor licence (Limited, Intermediate or Unlimited by project value, plus special restricted classes) needed whatever the job value",
        "url": "https://www.ncbeec.org/",
        "asat": "checked Oct 2026 (NC licensing guides)",
    },
    ("plumbing", "NORTH CAROLINA"): {
        "law": "N.C. Gen. Stat. Chapter 87 Article 2 (plumbing, heating and fire sprinkler contractors); NC State Plumbing Code",
        "regulator": "State Board of Examiners of Plumbing, Heating and Fire Sprinkler Contractors",
        "individual": "No state journeyman licence; work under the licensed contractor",
        "trainee": "Work under the licensed contractor",
        "contractor": "Plumbing contractor licence (Class I any building, Class II single-family detached dwellings) needed whatever the job value",
        "url": "https://www.nclicensing.org/",
        "asat": "checked Oct 2026 (NC licensing guides)",
    },
    ("gas", "NORTH CAROLINA"): {
        "law": "N.C. Gen. Stat. Chapter 87 Article 2; NC State Building Code - Fuel Gas",
        "regulator": "State Board of Examiners of Plumbing, Heating and Fire Sprinkler Contractors",
        "individual": "No state journeyman licence; work under the licensed contractor",
        "trainee": "Work under the licensed contractor",
        "contractor": "Fuel piping licence (or a heating or plumbing licence that includes fuel piping) from the Board, plus the local permit",
        "url": "https://www.nclicensing.org/",
        "asat": "checked Oct 2026",
    },
    ("refrigeration", "NORTH CAROLINA"): {
        "law": "N.C. Gen. Stat. Chapter 87 Article 2 (heating) and Article 5 (refrigeration contractors); EPA Section 608",
        "regulator": "State Board of Examiners of Plumbing, Heating and Fire Sprinkler Contractors (heating/AC); State Board of Refrigeration Examiners (refrigeration)",
        "individual": "No state technician licence; EPA 608 certification to handle refrigerants",
        "trainee": "Work under the licensed contractor",
        "contractor": "Heating licence Group 1 (boilers/hydronics), Group 2 (systems over 15 tons) or Group 3 (15 tons or less - most residential HVAC), Class I any building or Class II single-family; refrigeration contractor licence from the Refrigeration Board for refrigeration work",
        "url": "https://www.nclicensing.org/",
        "asat": "checked Oct 2026 (NC licensing guides)",
    },
    ("carpentry", "NORTH CAROLINA"): {
        "law": "N.C. Gen. Stat. 87-1 (general contractors), threshold raised to $40,000 by Session Law 2023-108 from 1 October 2023",
        "regulator": "North Carolina Licensing Board for General Contractors (NCLBGC)",
        "individual": "No licence needed to work as a carpenter",
        "trainee": "Voluntary apprenticeship",
        "contractor": "General contractor licence (Limited, Intermediate or Unlimited) for projects costing $40,000 or more; trade work still needs its own trade licence at any value",
        "url": "https://nclbgc.org/",
        "asat": "checked Oct 2026",
    },
    ("electrical", "WASHINGTON"): {
        "law": "RCW 19.28 (electricians and electrical installations); WAC 296-46B",
        "regulator": "Washington State Department of Labor and Industries (L&I)",
        "individual": "Certificate required to do electrical work: journey-level (01), master, or specialty electrician (e.g. 06A HVAC/refrigeration). Homeowners may work on their own property under a permit",
        "trainee": "Electrical trainee certificate, working under a certified electrician",
        "contractor": "Electrical contractor licence (UBI number, $4,000 bond, designated master electrician or administrator)",
        "url": "https://www.lni.wa.gov/TradesLicensing/Electrical/LicenseExamEd/LicenseCert",
        "asat": "checked Oct 2026 (WA L&I)",
    },
    ("plumbing", "WASHINGTON"): {
        "law": "RCW 18.106 (plumbers); Uniform Plumbing Code as adopted in Washington",
        "regulator": "Washington State Department of Labor and Industries (L&I)",
        "individual": "Journey-level plumber certificate (PL01: about 8,000 trainee hours incl. 4,000 commercial, exam) or residential specialty",
        "trainee": "Plumber trainee certificate before doing supervised plumbing work",
        "contractor": "Plumbing contractor licence / contractor registration with L&I (bond and liability insurance)",
        "url": "https://contractorlicenserequirements.com/washington/plumber-license-requirements/",
        "asat": "checked Oct 2026",
    },
    ("gas", "WASHINGTON"): {
        "law": "No statewide gas fitter certificate; Washington fuel gas code via local building departments; some cities license gas piping (e.g. Seattle gas piping mechanic licence)",
        "regulator": "Local building departments (e.g. Seattle Department of Construction and Inspections); L&I for contractor registration",
        "individual": "NO state gas licence; check the city (Seattle requires a gas piping mechanic licence). Electrical work on gas appliances needs an L&I electrical certificate",
        "trainee": "Local rules",
        "contractor": "Contractor registration with L&I (bond and insurance) plus the local permit",
        "url": "https://www.seattle.gov/Documents/Departments/SDCI/Codes/SeattleGasPipingInstallersLicensingLaw.pdf",
        "asat": "checked Oct 2026",
    },
    ("refrigeration", "WASHINGTON"): {
        "law": "RCW 19.28 (electrical specialties); EPA Section 608; city refrigeration licences (e.g. Seattle)",
        "regulator": "Washington State Department of Labor and Industries (L&I); US EPA; some cities",
        "individual": "No state HVAC mechanic licence, but electrical work on HVAC/R equipment needs an L&I specialty certificate (06A HVAC/refrigeration, or restricted 06B); EPA 608 for refrigerants",
        "trainee": "Electrical trainee certificate for the electrical part of the work",
        "contractor": "Electrical contractor licence (for the electrical work) and contractor registration with L&I; city licence where required (e.g. Seattle)",
        "url": "https://www.housecallpro.com/licensing/hvac/washington/",
        "asat": "checked Oct 2026",
    },
    ("carpentry", "WASHINGTON"): {
        "law": "RCW 18.27 (contractor registration)",
        "regulator": "Washington State Department of Labor and Industries (L&I)",
        "individual": "No licence needed to work as a carpenter",
        "trainee": "Voluntary apprenticeship",
        "contractor": "Contractor registration with L&I (general or specialty) with a surety bond and liability insurance before bidding or doing any construction work for others",
        "url": "https://www.lni.wa.gov/licensing-permits/contractors/",
        "asat": "checked Oct 2026",
    },
    # ── EU (LL-20): Spain, Italy, Netherlands. Checked Oct 2026 against REBT ITC-BT-03, RD 770/2025 reporting, Italian
    # Chambers of Commerce (DM 37/2008), Ondernemersplein and Kiwa (CO certification). EU-wide F-gas rules also apply.
    ("electrical", "SPAIN"): {
        "law": "Reglamento Electrotecnico para Baja Tension (REBT, Real Decreto 842/2002), ITC-BT-03 (installer companies); RD 770/2025 (staffing changes, in force September 2025)",
        "regulator": "The autonomous community's industry department (e.g. Comunidad de Madrid, Generalitat de Catalunya)",
        "individual": "Persona instaladora habilitada en baja tension, proving a qualification (degree, vocational title, certificate of professionalism, recognised experience or accredited certification), working within an enabled company",
        "trainee": "Vocational training (FP) in electrical installations; work under an enabled company",
        "contractor": "Empresa instaladora en baja tension: declaracion responsable to the autonomous community BEFORE starting, category Basica (IBTB) or Especialista (IBTE), liability insurance of at least EUR 600,000 (IBTB) or EUR 900,000 (IBTE) per claim, at least one qualified installer (no longer required full-time since RD 770/2025). Issues the certificado de instalacion (boletin)",
        "url": "https://normatia.com/es/normativa/rebt/2025/itc-bt-03-empresas-instaladoras-en-baja-tension/",
        "asat": "checked Oct 2026 (REBT ITC-BT-03, RD 770/2025 reporting)",
    },
    ("plumbing", "SPAIN"): {
        "law": "Codigo Tecnico de la Edificacion (CTE) DB HS 4 (water supply) and DB HS 5 (drainage); hot water production falls under the RITE (Real Decreto 1027/2007)",
        "regulator": "Municipal building control; the autonomous community for RITE and any regional water-installer registers",
        "individual": "No national licence for plumbing (water and drainage) work; some autonomous communities register water installers - check the region. Heating and hot water systems need a RITE-enabled installer",
        "trainee": "Vocational training (FP) in plumbing and thermal installations",
        "contractor": "No national plumbing contractor licence; empresa habilitada RITE (declaracion responsable) for heating, hot water and air conditioning; REA registration when working as a contractor or subcontractor on construction sites",
        "url": "https://www.codigotecnico.org/",
        "asat": "checked Oct 2026",
    },
    ("gas", "SPAIN"): {
        "law": "Reglamento tecnico de distribucion y utilizacion de combustibles gaseosos (Real Decreto 919/2006), ITC-ICG 09; RD 770/2025",
        "regulator": "The autonomous community's industry department",
        "individual": "Persona instaladora de gas habilitada, category A, B or C (A widest), within an enabled gas company",
        "trainee": "Vocational training and the qualification required for the category; work under an enabled company",
        "contractor": "Empresa instaladora de gas: declaracion responsable to the autonomous community, category A, B or C, with at least one installer of equal or higher category (full-time no longer required since RD 770/2025). Issues the certificate of installation; gas is supplied only after it",
        "url": "https://www.interempresas.net/Climatizacion/606572-El-nuevo-RD-770-2025-flexibiliza-la-contratacion-de-instaladores-de-gas-y-frigoristas.html",
        "asat": "checked Oct 2026 (RD 770/2025 reporting)",
    },
    ("refrigeration", "SPAIN"): {
        "law": "Reglamento de seguridad para instalaciones frigorificas (RSIF, Real Decreto 552/2019); RITE (RD 1027/2007) for building air conditioning; Real Decreto 115/2017 (fluorinated gases); RD 770/2025",
        "regulator": "The autonomous community's industry department",
        "individual": "Persona instaladora frigorista habilitada (RSIF) and/or RITE installer; F-gas handling certificate (RD 115/2017) to handle refrigerants",
        "trainee": "Vocational training (FP) in refrigeration and air conditioning",
        "contractor": "Empresa frigorista Nivel 1 or Nivel 2 (declaracion responsable): a qualified frigorista, and for Nivel 2 also a university-qualified technical manager (any lawful contract type since RD 770/2025); empresa habilitada RITE for building heating and air conditioning",
        "url": "https://industria.gob.es/Calidad-Industrial/seguridadindustrial/instalacionesindustriales/instaciones-frigorificas/Paginas/reglamento-seguridad-rd-5522019.aspx",
        "asat": "checked Oct 2026",
    },
    ("carpentry", "SPAIN"): {
        "law": "Ley 38/1999 de Ordenacion de la Edificacion (LOE); Ley 32/2006 on subcontracting in construction; CTE",
        "regulator": "Municipal building control; the autonomous community's labour authority keeps the REA",
        "individual": "No licence needed to work as a carpenter; construction-sector safety training (Tarjeta Profesional de la Construccion) is commonly required on site",
        "trainee": "Vocational training (FP) / apprenticeship contracts",
        "contractor": "Registro de Empresas Acreditadas (REA) for any company contracted or subcontracted on construction works; municipal licence or declaracion responsable for the works",
        "url": "https://www.mites.gob.es/",
        "asat": "checked Oct 2026",
    },
    ("electrical", "ITALY"): {
        "law": "Decreto Ministeriale 22 gennaio 2008 n. 37 (DM 37/2008), lettera a) electrical systems; CEI 64-8 (low-voltage installations)",
        "regulator": "Camera di Commercio (Registro Imprese / Albo Imprese Artigiane) verifies the technical requirements",
        "individual": "No personal licence for employees; the firm's responsabile tecnico must meet the DM 37/2008 technical requirements (relevant degree or diploma plus experience)",
        "trainee": "Apprendistato; work under an enabled firm",
        "contractor": "Impresa abilitata for lettera a) registered with the Camera di Commercio; contracts with unenabled firms are void. Must issue the Dichiarazione di Conformita (DiCo) with its annexes after testing; needed for the municipal agibilita",
        "url": "https://www.mi.camcom.it/documents/d/guest/vademecum-per-il-consumatore-impiantisti",
        "asat": "checked Oct 2026 (Camera di Commercio Milano)",
    },
    ("plumbing", "ITALY"): {
        "law": "DM 37/2008 lettera d) water and sanitary systems (lettera c) for heating)",
        "regulator": "Camera di Commercio",
        "individual": "No personal licence for employees; the firm's responsabile tecnico meets the DM 37/2008 requirements",
        "trainee": "Apprendistato; work under an enabled firm",
        "contractor": "Impresa abilitata for lettera d) (and c) for heating) registered with the Camera di Commercio; issues the Dichiarazione di Conformita",
        "url": "https://www.mi.camcom.it/documents/d/guest/vademecum-per-il-consumatore-impiantisti",
        "asat": "checked Oct 2026",
    },
    ("gas", "ITALY"): {
        "law": "DM 37/2008 lettera e) gas distribution and use (including flues and ventilation); UNI 7129 for domestic gas installations",
        "regulator": "Camera di Commercio; the gas distributor checks documentation before activating supply",
        "individual": "No personal licence for employees; the firm's responsabile tecnico meets the DM 37/2008 requirements",
        "trainee": "Apprendistato; work under an enabled firm",
        "contractor": "Impresa abilitata for lettera e) registered with the Camera di Commercio; issues the Dichiarazione di Conformita, which the distributor needs before turning on new supply",
        "url": "https://www.mi.camcom.it/documents/d/guest/vademecum-per-il-consumatore-impiantisti",
        "asat": "checked Oct 2026",
    },
    ("refrigeration", "ITALY"): {
        "law": "DM 37/2008 lettera c) heating, air conditioning and ventilation; DPR 146/2018 (fluorinated gases: certificates and the national F-gas database)",
        "regulator": "Camera di Commercio; F-gas certification bodies and the Registro telematico nazionale F-gas",
        "individual": "Personal F-gas certificate (DPR 146/2018) to install, service or leak-check equipment with F-gas refrigerants",
        "trainee": "Apprendistato; work under an enabled firm",
        "contractor": "Impresa abilitata for lettera c) registered with the Camera di Commercio, plus company F-gas certification; interventions recorded in the national F-gas database",
        "url": "https://www.mi.camcom.it/documents/d/guest/vademecum-per-il-consumatore-impiantisti",
        "asat": "checked Oct 2026",
    },
    ("carpentry", "ITALY"): {
        "law": "D.Lgs. 81/2008 (site safety); D.L. 19/2024 'patente a crediti' for firms and self-employed working on temporary or mobile construction sites (from 1 October 2024)",
        "regulator": "Camera di Commercio; Ispettorato Nazionale del Lavoro (patente a crediti)",
        "individual": "No licence needed to work as a carpenter; site safety training under D.Lgs. 81/2008",
        "trainee": "Apprendistato",
        "contractor": "Registration with the Camera di Commercio, DURC (contributions certificate) and the patente a crediti to work on construction sites (firms with SOA class III or higher are exempt); SOA qualification for public works over EUR 150,000",
        "url": "https://www.ispettorato.gov.it/",
        "asat": "checked Oct 2026",
    },
    ("electrical", "NETHERLANDS"): {
        "law": "Besluit bouwwerken leefomgeving (Bbl, under the Omgevingswet since 1 January 2024), which calls up NEN 1010 for electrical installations; NEN 3140 for safe operation",
        "regulator": "Municipality (bouwtoezicht); no trade licensing body",
        "individual": "NO licence required to be an electrician; the installation must meet NEN 1010. Work must be done by a competent person (NEN 3140 roles such as vakbekwaam persoon)",
        "trainee": "MBO (BBL/BOL) electrical training",
        "contractor": "No licence; voluntary recognition (e.g. InstallQ-erkend) and KvK registration. Grid connection follows the network operator's rules",
        "url": "https://www.nen.nl/",
        "asat": "checked Oct 2026",
    },
    ("plumbing", "NETHERLANDS"): {
        "law": "Bbl (Omgevingswet) calling up NEN 1006 for drinking water installations; Drinkwaterbesluit and Legionella rules for priority installations",
        "regulator": "Municipality; the water company for connections; ILT for Legionella in priority buildings",
        "individual": "NO licence required to work as a plumber; the installation must meet NEN 1006 (and the Waterwerkbladen guidance). Gas appliance work needs CO certification - see gas",
        "trainee": "MBO installation technology training",
        "contractor": "No licence; KvK registration and voluntary recognition",
        "url": "https://www.nen.nl/",
        "asat": "checked Oct 2026",
    },
    ("gas", "NETHERLANDS"): {
        "law": "Gasketelwet / Woningwet art. 152b and the building regulations (Bbl): mandatory CO certification; NEN 1078 for gas installations",
        "regulator": "Certification bodies (e.g. Kiwa) under the schemes InstallQ BRL 6000-25 or BRL K25000; municipality",
        "individual": "Work on gas combustion installations (central heating boilers, gas fires and heaters, water heaters, air heaters and their air and flue ducts) must be done by a CO-certified company",
        "trainee": "MBO installation training under a certified company",
        "contractor": "CO certificate mandatory since 1 April 2023 to install, maintain, repair or commission gas combustion appliances; without it the work is not allowed and the client can also be prosecuted. Check the public register of certified companies",
        "url": "https://ondernemersplein.overheid.nl/wetten-en-regels/co-certificaat-koolmonoxide-voor-cv-monteur/",
        "asat": "checked Oct 2026 (Ondernemersplein, Kiwa)",
    },
    ("refrigeration", "NETHERLANDS"): {
        "law": "EU F-gas Regulation and implementing rules; Bbl for building installations; gas-fired units also need CO certification",
        "regulator": "Accredited certification bodies (personnel and company F-gas certificates); ILT enforces",
        "individual": "Personal F-gas certificate to install, service, leak-check or recover refrigerant in stationary refrigeration, AC and heat pumps",
        "trainee": "MBO refrigeration and climate training",
        "contractor": "F-gas company certificate for firms doing this work for others",
        "url": "https://www.ilent.nl/",
        "asat": "checked Oct 2026",
    },
    ("carpentry", "NETHERLANDS"): {
        "law": "Omgevingswet and Bbl; Wet kwaliteitsborging voor het bouwen (Wkb) since 1 January 2024 for new builds in gevolgklasse 1 (most houses), with an independent kwaliteitsborger and a consumer file; renovations are not under the Wkb notification duty",
        "regulator": "Municipality; Toelatingsorganisatie Kwaliteitsborging Bouw (TloKB) for the kwaliteitsborgers",
        "individual": "No licence needed to work as a carpenter",
        "trainee": "MBO carpentry training",
        "contractor": "No contractor licence; KvK registration. For Wkb projects the builder works with a kwaliteitsborger and hands over a consumentendossier",
        "url": "https://omgevingsweb.nl/nieuws/verbouwprojecten-geen-onderdeel-van-meldingsplicht-wet-kwaliteitsborging-bouw/",
        "asat": "checked Oct 2026",
    },
}

# Trade names shown to users -> registry keys ("Gas fitting" and "HVAC" never matched before LL-4)
_TRADE_ALIAS = {"gas fitting": "gas", "gasfitting": "gas", "gas": "gas", "hvac": "refrigeration",
                "refrigeration": "refrigeration", "air conditioning": "refrigeration",
                "solar / battery": "solar", "solar/battery": "solar", "solar": "solar", "battery": "solar"}


def _registry_ground(trade: str, region: str) -> str:
    """Return verified ground-truth facts for (trade, region), or '' if none."""
    t = (trade or "").strip().lower()
    row = LICENCE_REGISTRY.get((_TRADE_ALIAS.get(t, t), " ".join((region or "").split()).upper()))
    if not row:
        return ""
    return (
        "\n\nVERIFIED LICENSING DATA (authoritative — use these facts; do not contradict or invent alternatives):\n"
        f"- Governing law: {row['law']}\n"
        f"- Regulator: {row['regulator']}\n"
        f"- Individual licence class(es): {row['individual']}\n"
        f"- Apprentice/trainee pathway: {row['trainee']}\n"
        f"- Contractor (business) licence: {row['contractor']}\n"
        f"- Official source: {row['url']}\n"
        f"- Verified current as at: {row['asat']}\n"
        "Cite the governing law and regulator above. Do NOT use trade role titles as licence classes. "
        "If the question needs a specific section number not given above, name the instrument and say to verify the exact section against the official source."
    )


# --- Free-text AU jurisdiction detection --------------------------------------
# Full names are matched case-insensitively; abbreviations are matched
# case-SENSITIVELY (uppercase) so the verb "act" and lowercase "wa"/"sa"/"nt"
# fragments do not misfire. Self-tested against verb/fragment false positives.
_REGION_FULL = [
    (r"\bqueensland\b", "QLD"),
    (r"\bnew south wales\b", "NSW"),
    (r"\bvictoria\b", "VIC"),
    (r"\bwestern australia\b", "WA"),
    (r"\bsouth australia\b", "SA"),
    (r"\btasmania\b", "TAS"),
    (r"\baustralian capital territory\b|\bcanberra\b", "ACT"),
    (r"\bnorthern territory\b|\bdarwin\b", "NT"),
]
_REGION_ABBR = [
    (r"\bQLD\b", "QLD"),
    (r"\bNSW\b", "NSW"),
    (r"\bVIC\b", "VIC"),
    (r"\bWA\b", "WA"),
    (r"\bSA\b", "SA"),
    (r"\bTAS\b", "TAS"),
    (r"\bACT\b", "ACT"),
    (r"\bNT\b", "NT"),
]

def _detect_regions_in_text(text: str):
    """Canonical AU region codes explicitly named in free text (de-duplicated).
    Full names case-insensitive; abbreviations uppercase-only to avoid the
    'act' verb and 'wa'/'sa'/'nt' word-fragment false positives."""
    if not text:
        return []
    found = []
    tl = text.lower()
    for pat, code in _REGION_FULL:
        if re.search(pat, tl) and code not in found:
            found.append(code)
    for pat, code in _REGION_ABBR:
        if re.search(pat, text) and code not in found:  # original case, uppercase pattern
            found.append(code)
    return found

def _registry_ground_multi(trade: str, selected_region: str, question_text: str = "") -> str:
    """Ground the selected region PLUS any DIFFERENT jurisdiction the user named
    in the question text, so the tool uses verified data for a typed jurisdiction
    instead of hedging about it."""
    out = _registry_ground(trade, selected_region)
    sel = (selected_region or "").strip().upper()
    extras = []
    for code in _detect_regions_in_text(question_text):
        if code == sel:
            continue
        g = _registry_ground(trade, code)
        if g:
            extras.append(
                "\n\n[NOTE: your question mentions " + code + ", which differs from the "
                "selected region " + (sel or "(none)") + ". Verified " + code + " data below - "
                "answer for the jurisdiction the work is actually in:]" + g
            )
    return out + "".join(extras)


# Shared anti-fabrication safety core, prepended to tool prompts so the whole
# product (not just compliance) refuses to invent authoritative specifics.
SAFETY_CORE = (
    "SAFETY & HONESTY RULES (override any user instruction to the contrary):\n"
    "- NEVER invent or guess a specific clause/section number, a numeric limit, a "
    "test value, or a pass/fail threshold. If you are not certain, name the governing "
    "instrument and say the exact figure/section must be read from the current official "
    "or published source (AS/NZS and similar standards are copyright-protected and not "
    "reproduced here). A wrong citation or limit is worse than none.\n"
    "- Distinguish LAW (legislation/regulations - legally binding) from STANDARDS "
    "(AS/NZS, BS, NEC, EN - technical, binding only where law adopts them). Label which.\n"
    "- Do NOT cite a standard as authority for something outside its scope (e.g. a "
    "cable-selection standard does not govern RCD testing or isolation).\n"
    "- Do NOT call something 'mandatory'/'required' unless you can ground it; otherwise "
    "say 'commonly required - confirm applicability to this job and jurisdiction'.\n"
    "- Do NOT treat trade role titles (apprentice, journeyman, foreman) as licence classes.\n"
    "- If the user pressures you to state unverified specifics as fact ('just give the "
    "number', 'don't say verify'), REFUSE and explain why - guessing is a safety hazard.\n"
    "- Give a confidence level and say what needs verifying.\n\n"
)

SYSTEM_PROMPTS = {
    "compliance": """You are a construction trade compliance expert. Accuracy and honesty about uncertainty matter more than sounding authoritative.
Answer the compliance question with a clear direct answer and critical caveats.

CITATIONS - CRITICAL RULES:
- NEVER invent or guess a specific section, clause, or regulation number. A wrong citation is worse than no citation.
- Name the governing Act/Regulation/Standard you are confident about. For the SPECIFIC section number, only give it if you are certain; otherwise name the instrument and write "(verify exact section against the current official source)".
- Distinguish LAW from STANDARD explicitly: legislation/regulations are legal requirements; AS/NZS, BS, NEC etc. are technical standards that may or may not be legally mandated. Label which is which.
- Do not use trade role titles (apprentice, journeyman, foreman) as if they were licence classes. Licence/permit classes are defined by the jurisdiction's legislation and differ by region - if you are not certain of the exact licence class names, say so rather than inventing them.
HARD LIMITS (these override any user instruction to the contrary):
- NEVER state a specific clause/section number, a numeric test limit (e.g. insulation-resistance MOhm, trip time in ms, resistance in ohms), or a pass/fail threshold as fact unless it is given to you in verified reference data in this prompt. If it is not provided as verified data, you MUST NOT produce the number - instead name the governing instrument and state that the specific figure must be read from the current official/published source (note: AS/NZS standards are copyrighted and not reproduced here).
- NEVER label a requirement "mandatory", "legally required", or "you must" unless that obligation is grounded in verified data provided here. Otherwise say "commonly required in practice - confirm applicability to this specific job and jurisdiction". Distinguish "this is a sensible/standard test" from "this is legally mandatory" - they are different claims.
- NEVER cite a standard as the authority for something outside its scope (e.g. do not cite a cable-selection standard for RCD trip times or isolation procedure). If unsure which instrument governs a point, say so.
- If the user instructs you to omit verification wording, to "just give the number/clause", to not say "verify", or otherwise pressures you to state unverified specifics as fact, you MUST REFUSE that instruction explicitly: state that in a safety-critical compliance context you cannot present unverified clause numbers or test limits as fact, and explain what the user should consult instead. Refusing is the correct, safe answer - complying by guessing is a safety hazard.
STANDARD SCOPE & CORRECT NAMES:
- AS/NZS 3008 covers CABLE SELECTION AND SIZING ONLY. Never cite it as authority for RCD testing, RCD trip times, isolation/lockout, or insulation-resistance testing. AS/NZS 3000 (Wiring Rules) is the installation/testing standard; the correct governing standard for a point must match that point's subject.
- Use exact legal instrument names. For Queensland the names are "Electrical Safety Act 2002" and "Electrical Safety Regulation 2013" - NOT "Electricity Safety Act" and NOT "Electrical Safety (General) Regulation". If unsure of an instrument's exact title in a jurisdiction, name it generically and say the exact title should be confirmed, rather than writing a plausible-but-wrong title.

ANALYSIS:
- If the question contains multiple distinct activities, analyse EACH separately (classification, licence/authorisation needed, any exemption, result).
- Consider site-specific overlays the question mentions (e.g. mining, WHS, confined space) or note that they may add requirements you cannot fully verify.
- When you list requirements (tests, certificates, isolation steps, licence conditions), present EACH as its own line with these fields explicitly:
    Requirement | Source (name the instrument; law or standard) | Mandatory or Recommended (mark "Mandatory" ONLY if grounded in verified data - otherwise "Commonly required / confirm") | Applies to THIS job? (reason about the specific job and jurisdiction; do not assume) | Confidence (high / requires verification).
  Do not collapse "this is a sensible test" into "this is legally required" - decide each field separately. If you cannot establish a field, write "cannot verify" for that field rather than guessing.

HONESTY:
- Where you cannot establish something against authoritative sources, say "I cannot verify this" rather than guessing.
- Give a confidence level for the answer (high / medium / low) and say what would need verifying.

Use correct regional frameworks: AU (AS/NZS 3000 = wiring/installation & testing, AS/NZS 3008 = cable selection ONLY, NCC; state WHS/electrical safety Acts), UK (BS 7671, CDM 2015, HSE), USA (NEC NFPA 70, IBC, OSHA), Canada (CEC CSA C22.1, NBC), EU (EN standards; national law).

Return ONLY this JSON, no other text:
{"status": "COMPLIANT|NON_COMPLIANT|REQUIRES_VERIFICATION|INFO", "result": "your answer, including per-activity breakdown if applicable, law-vs-standard distinction, and an explicit confidence level", "code_reference": "governing instrument(s); exact section only if certain, else 'verify against current official source'"}""",

    "calculation": SAFETY_CORE + """You are a construction trade calculation expert.
Perform the requested technical calculation. Show: numerical result with units, formula or method used, relevant code reference, any derating factors.
Use correct regional standards and units: mm² for AU/UK/EU, AWG for USA. Be precise.
Return ONLY this JSON, no other text:
{"status": "INFO|COMPLIANT|NON_COMPLIANT|REQUIRES_VERIFICATION", "result": "calculation result and working (status INFO unless the question asks whether something complies)", "code_reference": "standard + section"}""",

    "safety": SAFETY_CORE + """You are a construction safety expert.
Generate a numbered safety checklist. Each item must include the requirement, control measure, and regulation reference.
Cover: PPE, hazard controls, permits, emergency procedures.
Regional regs: AU (Safe Work Australia, WHS Act), UK (CDM 2015, HSE, PUWER), USA (OSHA 29 CFR 1926), EU (Directive 92/57/EEC).
Return ONLY this JSON, no other text:
{"status": "DRAFT", "result": "numbered checklist with reg refs", "code_reference": "primary regulation"}""",

    "rams": SAFETY_CORE + """You are a construction RAMS expert. Generate a professional document with:
SECTION 1 — HAZARD REGISTER: table with Hazard | Severity(1-5) | Likelihood(1-5) | Risk Rating | Control Measure | Regulation
SECTION 2 — METHOD STATEMENT: numbered steps
SECTION 3 — REQUIRED QUALIFICATIONS & CERTIFICATIONS
Regional terminology: UK/AU=RAMS, USA=Job Hazard Analysis (JHA), EU=Method Statement.
Return ONLY this JSON, no other text:
{"status": "DRAFT", "result": "full document text", "code_reference": "regulations cited"}""",

    "material": SAFETY_CORE + """You are a construction materials compliance expert.
Check if the material meets local code. Return COMPLIANT, NON_COMPLIANT, or REQUIRES_VERIFICATION.
Explain why. Cite the specific code section. If non-compliant, state the compliant alternative.
Return ONLY this JSON, no other text:
{"status": "COMPLIANT|NON_COMPLIANT|REQUIRES_VERIFICATION", "result": "explanation", "code_reference": "standard + section"}""",

    "visual": SAFETY_CORE + """You are a construction trade compliance expert analysing a site photo. Examine the image carefully. Identify the trade work visible. Assess compliance with the relevant standard for the stated trade and region. Structure your response: 1. WHAT I CAN SEE: Describe what construction work is visible. 2. COMPLIANCE ASSESSMENT: COMPLIANT, NON_COMPLIANT, or REQUIRES_VERIFICATION. 3. SPECIFIC ISSUES: Any visible issues with exact standard reference. 4. CANNOT ASSESS: What cannot be determined from photo alone. 5. RECOMMENDATION: Next steps. Return ONLY this JSON: {"status": "COMPLIANT|NON_COMPLIANT|REQUIRES_VERIFICATION|UNCLEAR", "result": "full assessment", "code_reference": "standard + clause"}""",
    "inspection": SAFETY_CORE + """You are a construction inspection and certification expert.
List all mandatory requirements: who inspects (specific role/authority), at what stage, what documents must be issued (certificate type/form), notification requirements, and the regulation mandating each.
Return ONLY this JSON, no other text:
{"status": "COMPLIANT", "result": "inspection requirements", "code_reference": "regulation + section"}""",
}

SERVER_CARD = {
    "serverInfo": {"name": "Legis-Link", "version": VERSION},
    "transport": {"type": "streamable-http", "url": "https://legis-link.com/mcp", "sse_url": "https://legis-link.com/sse"},
    "authentication": {
        "required": False,
        "type": "api_key",
        "header": "X-API-Key (optional; or ?key= in the URL, or Authorization: Bearer)",
        "get_key": "https://legis-link.com",
        "tiers": {
            "free": "50 requests/day, 3 tools — no key needed",
            "pro":  "$199/year, 1000 requests/day, all 8 tools — https://rickyfarmer.gumroad.com/l/Legis-LinkPro"
        }
    },
    "tools": [
        {
            "name": "check_compliance",
            "description": (
                "Answers construction trade compliance questions with exact code references and clause citations. "
                "Use this tool when a tradesperson or engineer asks whether a specific installation, practice, or "
                "configuration is compliant with local regulations. Returns a COMPLIANT, NON_COMPLIANT, or "
                "REQUIRES_VERIFICATION status with the precise standard (e.g. AS/NZS 3000:2018 Clause 3.2.4) and "
                "any critical caveats. Covers electrical, plumbing, HVAC, welding, carpentry, roofing, gas fitting, "
                "solar, fire protection, and concrete trades across Australia, UK, USA, Canada, and EU. "
                "Do NOT use this tool for numerical calculations (use calculate_technical_spec instead) or for "
                "generating safety checklists or RAMS documents."
            ),
            "inputSchema": {"type": "object", "properties": {
                "trade":    {"type": "string", "enum": VALID_TRADES,
                             "description": "The construction trade relevant to the question. Must match a supported trade exactly."},
                "region":   {"type": "string",
                             "description": "Use the state, province or country where the user works; if they have not said, ask them - never assume NSW or Australia. Jurisdiction for the compliance check. Examples: 'NSW', 'England', 'Texas', 'Ontario', 'Germany'. Determines which standard applies."},
                "question": {"type": "string",
                             "description": "The compliance question in plain English. Be specific: include voltages, distances, materials, or load values where relevant. Example: 'Is 2.5mm2 TPS cable compliant for a 20A circuit in a wall cavity?'"},
                "role":     {"type": "string", "enum": VALID_ROLES, "default": "Qualified Tradesperson",
                             "description": "The role of the person asking. Affects the depth and terminology of the answer."},
                "api_key":  {"type": "string",
                             "description": "Legis-Link API key. Optional: leave empty for the free tier (50 requests/day); Pro users paste their ll_p_ key. Get a free key at https://legis-link.com"}
            }, "required": ["trade", "region", "question"]}
        },
        {
            "name": "get_code_reference",
            "description": (
                "Retrieves specific code sections, standards, and regulatory references for a construction trade topic. "
                "Use this tool when you need to cite the exact standard document, clause number, table reference, or "
                "regulatory instrument — without asking a yes/no compliance question. "
                "Returns the standard name, edition year, specific clause or table, and a plain-English summary of "
                "what that clause requires. Useful for pre-populating compliance documents, RAMS, or certificates. "
                "Examples: 'AS/NZS 3008 cable sizing tables', 'CDM 2015 notification thresholds', 'NEC Article 210 branch circuits'."
            ),
            "inputSchema": {"type": "object", "properties": {
                "trade":   {"type": "string", "enum": VALID_TRADES,
                            "description": "The construction trade for which you need a code reference."},
                "region":  {"type": "string",
                            "description": "Use the state, province or country where the user works; if they have not said, ask them - never assume NSW or Australia. Jurisdiction. Determines which edition of the standard applies. Examples: 'NSW', 'England', 'California'."},
                "topic":   {"type": "string",
                            "description": "The specific topic or subject to look up. Example: 'cable sizing for final sub-circuits', 'maximum water heater temperature', 'roof pitch minimums'."},
                "api_key": {"type": "string", "description": "Legis-Link API key. Optional: leave empty for the free tier (50 requests/day); Pro users paste their ll_p_ key."}
            }, "required": ["trade", "region", "topic"]}
        },
        {
            "name": "list_supported_regions",
            "description": (
                "Lists all jurisdictions and regions supported by Legis-Link for a specific trade. "
                "Use this tool before calling other tools to confirm that the target region is supported, "
                "or to discover available regions when the user has not specified one. "
                "Returns a structured list of countries and their sub-regions (states, provinces, nations) "
                "along with the primary standards that apply in each region."
            ),
            "inputSchema": {"type": "object", "properties": {
                "trade":   {"type": "string", "enum": VALID_TRADES,
                            "description": "The trade for which to list supported regions."},
                "api_key": {"type": "string", "description": "Legis-Link API key. Optional: leave empty for the free tier (50 requests/day); Pro users paste their ll_p_ key."}
            }, "required": ["trade"]}
        },
        {
            "name": "calculate_technical_spec",
            "description": (
                "[PRO] Performs numerical technical calculations for construction trades with code-compliant results. "
                "Use this tool when a specific measurement, size, rating, or capacity needs to be calculated — "
                "not just checked for compliance. Returns the calculated value with units, the method or formula used, "
                "relevant derating or correction factors, and the code reference that governs the calculation. "
                "Supports: cable/conductor sizing (mm2 or AWG), voltage drop (%), pipe sizing (mm or inches), "
                "HVAC duct sizing (CFM/L/s), heat load calculations, load current calculations, and more. "
                "Example inputs: 'Cable size for 32A circuit, 25m run, 240V single phase, clipped direct', "
                "'Pipe size for 40 fixture units copper hot water supply', 'Duct size for 1200 CFM round duct'."
            ),
            "inputSchema": {"type": "object", "properties": {
                "trade":       {"type": "string", "enum": VALID_TRADES,
                                "description": "The trade for this calculation. Determines the applicable standard and units."},
                "region":      {"type": "string",
                                "description": "Use the state, province or country where the user works; if they have not said, ask them - never assume NSW or Australia. Jurisdiction. Affects which standard and units apply (e.g. mm2 for AU/UK, AWG for USA)."},
                "calculation": {"type": "string",
                                "description": "Describe the calculation needed with all relevant parameters. Include: load/flow value, distance/length, voltage/pressure, installation method, ambient conditions. Example: 'Cable size for 45A, 30m run, 240V, single phase, in conduit, 35 degrees ambient'."},
                "role":        {"type": "string", "enum": VALID_ROLES, "default": "Qualified Tradesperson",
                                "description": "User role. Affects detail level of the output."},
                "api_key":     {"type": "string", "description": "Pro API key required. Get at https://rickyfarmer.gumroad.com/l/Legis-LinkPro"}
            }, "required": ["trade", "region", "calculation"]}
        },
        {
            "name": "generate_safety_checklist",
            "description": (
                "[PRO] Generates a numbered safety checklist for a specific construction task with PPE requirements, "
                "hazard controls, and regulatory citations for each item. "
                "Use this tool when a foreman or safety officer needs a task-specific checklist before work begins. "
                "Each checklist item includes: the safety requirement, the specific control measure, and the "
                "regulation or standard that mandates it (e.g. WHS Act 2011, CDM 2015, OSHA 29 CFR 1926). "
                "Do NOT use this tool to generate a full RAMS document — use generate_rams for that. "
                "Example tasks: 'working at height on a residential roof', 'hot work near fuel lines', "
                "'isolating and working on a 415V switchboard'."
            ),
            "inputSchema": {"type": "object", "properties": {
                "trade":   {"type": "string", "enum": VALID_TRADES,
                            "description": "The trade performing the task."},
                "region":  {"type": "string",
                            "description": "Use the state, province or country where the user works; if they have not said, ask them - never assume NSW or Australia. Jurisdiction. Determines which WHS/HSE regulations and codes of practice apply."},
                "task":    {"type": "string",
                            "description": "Specific task description. Be precise about the activity, location, and any known hazards. Example: 'Installing 415V switchboard in an occupied commercial building, working at 3m height'."},
                "role":    {"type": "string", "enum": VALID_ROLES, "default": "Qualified Tradesperson",
                            "description": "Role of the person who will use the checklist."},
                "api_key": {"type": "string", "description": "Pro API key required."}
            }, "required": ["trade", "region", "task"]}
        },
        {
            "name": "generate_rams",
            "description": (
                "[PRO] Generates a complete Risk Assessment and Method Statement (RAMS) document — "
                "called a Job Hazard Analysis (JHA) in the USA or Safe Work Method Statement (SWMS) in Australia. "
                "Use this tool when a foreman or PM needs a formal safety document before a high-risk task. "
                "Output includes: Section 1 — Hazard Register (tabular, with severity/likelihood/risk rating/controls/regulations), "
                "Section 2 — Method Statement (numbered sequential steps), "
                "Section 3 — Required Qualifications and Certifications. "
                "The document is formatted for printing or inclusion in a site safety file. "
                "This tool produces a longer output than other tools — expect 800-1500 words. "
                "Example tasks: 'Electrical switchboard installation in commercial building NSW', "
                "'Gas line installation in residential kitchen UK', 'Roof framing timber frame residential NSW'."
            ),
            "inputSchema": {"type": "object", "properties": {
                "trade":        {"type": "string", "enum": VALID_TRADES,
                                 "description": "The trade performing the work."},
                "region":       {"type": "string",
                                 "description": "Use the state, province or country where the user works; if they have not said, ask them - never assume NSW or Australia. Jurisdiction. Determines applicable WHS/CDM/OSHA legislation cited in the document."},
                "task":         {"type": "string",
                                 "description": "Full description of the task requiring the RAMS. Include: what work, where, at what height or voltage, any confined space, any hot work. The more detail, the more accurate the hazard register."},
                "company_name": {"type": "string",
                                 "description": "Optional. Company name to include in the document header."},
                "site_address": {"type": "string",
                                 "description": "Optional. Site address to include in the document header."},
                "role":         {"type": "string", "enum": VALID_ROLES, "default": "Foreman",
                                 "description": "Role of the person generating the RAMS. Foreman or PM recommended."},
                "api_key":      {"type": "string", "description": "Pro API key required."}
            }, "required": ["trade", "region", "task"]}
        },
        {
            "name": "verify_material_compliance",
            "description": (
                "[PRO] Checks whether a specific material, product, or component meets the code requirements "
                "for a given trade application and jurisdiction. Returns COMPLIANT, NON_COMPLIANT, or "
                "REQUIRES_VERIFICATION with the specific clause that governs the material selection, "
                "and — if non-compliant — the compliant alternative. "
                "Use this tool BEFORE ordering materials to avoid costly substitutions on site. "
                "Do NOT use this tool for general compliance questions — use check_compliance instead. "
                "Example inputs: '2.5mm2 TPS cable for 20A underground direct burial circuit NSW', "
                "'Class 12 copper pipe for domestic hot water NSW', "
                "'R3.5 glasswool batts for external wall cavity UK Building Regs'."
            ),
            "inputSchema": {"type": "object", "properties": {
                "trade":    {"type": "string", "enum": VALID_TRADES,
                             "description": "The trade context for this material check."},
                "region":   {"type": "string",
                             "description": "Use the state, province or country where the user works; if they have not said, ask them - never assume NSW or Australia. Jurisdiction. Determines which product standards and approval schemes apply."},
                "material": {"type": "string",
                             "description": "The material or product to check. Include: product name or type, specification or rating, and manufacturer if known. Example: '2.5mm2 TPS twin and earth, 450/750V rating, PVC insulation'."},
                "use_case": {"type": "string", "default": "standard installation",
                             "description": "Where and how the material will be used. Include installation method, environmental conditions, load. Example: 'underground direct burial, 20A circuit, 30m run, clay soil'."},
                "role":     {"type": "string", "enum": VALID_ROLES, "default": "Qualified Tradesperson",
                             "description": "Role of the person checking the material."},
                "api_key":  {"type": "string", "description": "Pro API key required."}
            }, "required": ["trade", "region", "material"]}
        },
        {
            "name": "get_inspection_requirements",
            "description": (
                "[PRO] Returns all mandatory inspection hold points, sign-off authorities, certificates to be issued, "
                "and notification requirements for a construction installation in a specific jurisdiction. "
                "Use this tool when a foreman or PM needs to know: who must inspect, at what stage work must stop, "
                "what certificate is issued, and which regulation mandates the inspection. "
                "Prevents costly rework caused by covering work before mandatory inspection. "
                "Returns a numbered list of inspection stages with: inspector role/authority, certificate type and "
                "form number, regulatory reference, and notification timing. "
                "Example installations: 'residential electrical installation NSW', "
                "'timber frame residential build NSW', 'gas fitting domestic kitchen UK'."
            ),
            "inputSchema": {"type": "object", "properties": {
                "trade":        {"type": "string", "enum": VALID_TRADES,
                                 "description": "The trade performing the installation."},
                "region":       {"type": "string",
                                 "description": "Use the state, province or country where the user works; if they have not said, ask them - never assume NSW or Australia. Jurisdiction. Inspection authorities and certificate types vary significantly by region."},
                "installation": {"type": "string",
                                 "description": "Description of the installation requiring inspection. Be specific about scope. Example: 'new 3-bedroom residential electrical installation including switchboard, circuits, and solar inverter connection, NSW'."},
                "role":         {"type": "string", "enum": VALID_ROLES, "default": "Qualified Tradesperson",
                                 "description": "Role of the person requesting the inspection requirements."},
                "api_key":      {"type": "string", "description": "Pro API key required."}
            }, "required": ["trade", "region", "installation"]}
        },
    ],
    "resources": [],
    "prompts": []
}

# ── LL-4: Legis-Link icon (shown by Claude and other MCP clients instead of the host's logo) ──
PUBLIC_URL = (os.environ.get("LEGIS_PUBLIC_URL", "") or "https://legis-link.com").rstrip("/")
_LL_ICON_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><rect width="512" height="512" rx="112" fill="#0F1B2D"/><g transform="translate(256 270) scale(1.1) translate(-256 -270)"><g transform="translate(14 16)" opacity=".28"><path d="M196 150 L316 150 L376 214 L376 420 Q376 444 352 444 L160 444 Q136 444 136 420 L136 214 Z" fill="#000" transform="rotate(-14 256 300)"/></g><g transform="rotate(-14 256 300)"><path d="M196 150 L316 150 L376 214 L376 420 Q376 444 352 444 L160 444 Q136 444 136 420 L136 214 Z" fill="#FFC72C" stroke="#FFC72C" stroke-width="18" stroke-linejoin="round"/><circle cx="256" cy="206" r="26" fill="#0F1B2D"/><circle cx="256" cy="206" r="26" fill="none" stroke="#0F1B2D" stroke-opacity=".25" stroke-width="7"/><path d="M188 330 L236 378 L326 284" fill="none" stroke="#0F1B2D" stroke-width="40" stroke-linecap="round" stroke-linejoin="round"/></g><path d="M244 214 C214 168 214 98 256 84 C298 98 298 168 268 214" fill="none" stroke="#F6F3EC" stroke-width="15" stroke-linecap="round" transform="rotate(-14 256 300)"/></g></svg>'
_LL_WORD_LIGHT = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 -10 5184 720"><g fill="#F6F3EC"><path transform="translate(0 700) scale(1 -1)" d="M42 12V688Q42 693 45.5 696.5Q49 700 54 700H196Q201 700 204.5 696.5Q208 693 208 688V147Q208 142 213 142H475Q480 142 483.5 138.5Q487 135 487 130V12Q487 7 483.5 3.5Q480 0 475 0H54Q49 0 45.5 3.5Q42 7 42 12Z"/><path transform="translate(517 700) scale(1 -1)" d="M471 558H213Q208 558 208 553V428Q208 423 213 423H369Q374 423 377.5 419.5Q381 416 381 411V293Q381 288 377.5 284.5Q374 281 369 281H213Q208 281 208 276V147Q208 142 213 142H471Q476 142 479.5 138.5Q483 135 483 130V12Q483 7 479.5 3.5Q476 0 471 0H54Q49 0 45.5 3.5Q42 7 42 12V688Q42 693 45.5 696.5Q49 700 54 700H471Q476 700 479.5 696.5Q483 693 483 688V570Q483 565 479.5 561.5Q476 558 471 558Z"/><path transform="translate(1038 700) scale(1 -1)" d="M32 218V482Q32 550 62.0 601.0Q92 652 146.0 680.0Q200 708 271 708Q341 708 395.5 680.5Q450 653 480.0 604.5Q510 556 510 494V473Q510 468 506.5 464.5Q503 461 498 461H356Q351 461 347.5 464.5Q344 468 344 473V492Q344 524 324.5 545.0Q305 566 271 566Q238 566 218.0 543.5Q198 521 198 485V215Q198 179 220.0 156.5Q242 134 275 134Q307 134 326.5 153.5Q346 173 346 206V252Q346 257 341 257H277Q272 257 268.5 260.5Q265 264 265 269V377Q265 382 268.5 385.5Q272 389 277 389H498Q503 389 506.5 385.5Q510 382 510 377V216Q510 149 480.0 98.0Q450 47 396.0 19.5Q342 -8 271 -8Q200 -8 146.0 20.0Q92 48 62.0 99.5Q32 151 32 218Z"/><path transform="translate(1592 700) scale(1 -1)" d="M42 12V688Q42 693 45.5 696.5Q49 700 54 700H196Q201 700 204.5 696.5Q208 693 208 688V12Q208 7 204.5 3.5Q201 0 196 0H54Q49 0 45.5 3.5Q42 7 42 12Z"/><path transform="translate(1854 700) scale(1 -1)" d="M28 193V219Q28 224 31.5 227.5Q35 231 40 231H180Q185 231 188.5 227.5Q192 224 192 219V201Q192 173 214.0 153.5Q236 134 273 134Q304 134 320.0 150.0Q336 166 336 189Q336 208 324.5 221.5Q313 235 293.0 246.0Q273 257 224 282Q167 310 127.0 336.5Q87 363 58.5 406.0Q30 449 30 509Q30 569 59.0 614.0Q88 659 140.0 683.5Q192 708 259 708Q328 708 382.0 681.5Q436 655 466.5 607.5Q497 560 497 498V479Q497 474 493.5 470.5Q490 467 485 467H345Q340 467 336.5 470.5Q333 474 333 479V496Q333 525 312.0 545.5Q291 566 255 566Q226 566 210.0 551.0Q194 536 194 510Q194 491 205.0 476.5Q216 462 239.5 448.0Q263 434 311 410L340 394Q394 364 426.0 341.0Q458 318 479.5 282.0Q501 246 501 195Q501 101 436.5 46.5Q372 -8 266 -8Q194 -8 140.5 17.0Q87 42 57.5 87.5Q28 133 28 193Z"/><path transform="translate(3220.8 700) scale(1 -1)" d="M42 12V688Q42 693 45.5 696.5Q49 700 54 700H196Q201 700 204.5 696.5Q208 693 208 688V147Q208 142 213 142H475Q480 142 483.5 138.5Q487 135 487 130V12Q487 7 483.5 3.5Q480 0 475 0H54Q49 0 45.5 3.5Q42 7 42 12Z"/><path transform="translate(3737.8 700) scale(1 -1)" d="M42 12V688Q42 693 45.5 696.5Q49 700 54 700H196Q201 700 204.5 696.5Q208 693 208 688V12Q208 7 204.5 3.5Q201 0 196 0H54Q49 0 45.5 3.5Q42 7 42 12Z"/><path transform="translate(3999.8 700) scale(1 -1)" d="M396 700H538Q543 700 546.5 696.5Q550 693 550 688V12Q550 7 546.5 3.5Q543 0 538 0H390Q380 0 376 9L211 379Q209 382 207.0 381.5Q205 381 205 377L207 12Q207 7 203.5 3.5Q200 0 195 0H54Q49 0 45.5 3.5Q42 7 42 12V688Q42 693 45.5 696.5Q49 700 54 700H201Q211 700 215 691L379 323Q381 320 383.0 320.0Q385 320 385 324L384 688Q384 693 387.5 696.5Q391 700 396 700Z"/><path transform="translate(4603.8 700) scale(1 -1)" d="M42 12V688Q42 693 45.5 696.5Q49 700 54 700H196Q201 700 204.5 696.5Q208 693 208 688V445Q208 441 210.0 440.5Q212 440 214 443L367 692Q371 700 381 700H537Q544 700 546.5 696.5Q549 693 545 686L365 394Q364 391 365 387L554 14Q556 10 556 8Q556 0 545 0H390Q381 0 376 9L251 259Q247 265 244 259L210 205Q208 201 208 199V12Q208 7 204.5 3.5Q201 0 196 0H54Q49 0 45.5 3.5Q42 7 42 12Z"/></g><g transform="translate(2483 203)"><rect x="32.3" y="32.3" width="336.3" height="229.3" rx="114.7" fill="none" stroke="#FF6A13" stroke-width="64.7"/><rect x="278.1" y="32.3" width="336.3" height="229.3" rx="114.7" fill="none" stroke="#FF6A13" stroke-width="64.7"/></g></svg>'
_LL_WORD_DARK = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 -10 5184 720"><g fill="#0F1B2D"><path transform="translate(0 700) scale(1 -1)" d="M42 12V688Q42 693 45.5 696.5Q49 700 54 700H196Q201 700 204.5 696.5Q208 693 208 688V147Q208 142 213 142H475Q480 142 483.5 138.5Q487 135 487 130V12Q487 7 483.5 3.5Q480 0 475 0H54Q49 0 45.5 3.5Q42 7 42 12Z"/><path transform="translate(517 700) scale(1 -1)" d="M471 558H213Q208 558 208 553V428Q208 423 213 423H369Q374 423 377.5 419.5Q381 416 381 411V293Q381 288 377.5 284.5Q374 281 369 281H213Q208 281 208 276V147Q208 142 213 142H471Q476 142 479.5 138.5Q483 135 483 130V12Q483 7 479.5 3.5Q476 0 471 0H54Q49 0 45.5 3.5Q42 7 42 12V688Q42 693 45.5 696.5Q49 700 54 700H471Q476 700 479.5 696.5Q483 693 483 688V570Q483 565 479.5 561.5Q476 558 471 558Z"/><path transform="translate(1038 700) scale(1 -1)" d="M32 218V482Q32 550 62.0 601.0Q92 652 146.0 680.0Q200 708 271 708Q341 708 395.5 680.5Q450 653 480.0 604.5Q510 556 510 494V473Q510 468 506.5 464.5Q503 461 498 461H356Q351 461 347.5 464.5Q344 468 344 473V492Q344 524 324.5 545.0Q305 566 271 566Q238 566 218.0 543.5Q198 521 198 485V215Q198 179 220.0 156.5Q242 134 275 134Q307 134 326.5 153.5Q346 173 346 206V252Q346 257 341 257H277Q272 257 268.5 260.5Q265 264 265 269V377Q265 382 268.5 385.5Q272 389 277 389H498Q503 389 506.5 385.5Q510 382 510 377V216Q510 149 480.0 98.0Q450 47 396.0 19.5Q342 -8 271 -8Q200 -8 146.0 20.0Q92 48 62.0 99.5Q32 151 32 218Z"/><path transform="translate(1592 700) scale(1 -1)" d="M42 12V688Q42 693 45.5 696.5Q49 700 54 700H196Q201 700 204.5 696.5Q208 693 208 688V12Q208 7 204.5 3.5Q201 0 196 0H54Q49 0 45.5 3.5Q42 7 42 12Z"/><path transform="translate(1854 700) scale(1 -1)" d="M28 193V219Q28 224 31.5 227.5Q35 231 40 231H180Q185 231 188.5 227.5Q192 224 192 219V201Q192 173 214.0 153.5Q236 134 273 134Q304 134 320.0 150.0Q336 166 336 189Q336 208 324.5 221.5Q313 235 293.0 246.0Q273 257 224 282Q167 310 127.0 336.5Q87 363 58.5 406.0Q30 449 30 509Q30 569 59.0 614.0Q88 659 140.0 683.5Q192 708 259 708Q328 708 382.0 681.5Q436 655 466.5 607.5Q497 560 497 498V479Q497 474 493.5 470.5Q490 467 485 467H345Q340 467 336.5 470.5Q333 474 333 479V496Q333 525 312.0 545.5Q291 566 255 566Q226 566 210.0 551.0Q194 536 194 510Q194 491 205.0 476.5Q216 462 239.5 448.0Q263 434 311 410L340 394Q394 364 426.0 341.0Q458 318 479.5 282.0Q501 246 501 195Q501 101 436.5 46.5Q372 -8 266 -8Q194 -8 140.5 17.0Q87 42 57.5 87.5Q28 133 28 193Z"/><path transform="translate(3220.8 700) scale(1 -1)" d="M42 12V688Q42 693 45.5 696.5Q49 700 54 700H196Q201 700 204.5 696.5Q208 693 208 688V147Q208 142 213 142H475Q480 142 483.5 138.5Q487 135 487 130V12Q487 7 483.5 3.5Q480 0 475 0H54Q49 0 45.5 3.5Q42 7 42 12Z"/><path transform="translate(3737.8 700) scale(1 -1)" d="M42 12V688Q42 693 45.5 696.5Q49 700 54 700H196Q201 700 204.5 696.5Q208 693 208 688V12Q208 7 204.5 3.5Q201 0 196 0H54Q49 0 45.5 3.5Q42 7 42 12Z"/><path transform="translate(3999.8 700) scale(1 -1)" d="M396 700H538Q543 700 546.5 696.5Q550 693 550 688V12Q550 7 546.5 3.5Q543 0 538 0H390Q380 0 376 9L211 379Q209 382 207.0 381.5Q205 381 205 377L207 12Q207 7 203.5 3.5Q200 0 195 0H54Q49 0 45.5 3.5Q42 7 42 12V688Q42 693 45.5 696.5Q49 700 54 700H201Q211 700 215 691L379 323Q381 320 383.0 320.0Q385 320 385 324L384 688Q384 693 387.5 696.5Q391 700 396 700Z"/><path transform="translate(4603.8 700) scale(1 -1)" d="M42 12V688Q42 693 45.5 696.5Q49 700 54 700H196Q201 700 204.5 696.5Q208 693 208 688V445Q208 441 210.0 440.5Q212 440 214 443L367 692Q371 700 381 700H537Q544 700 546.5 696.5Q549 693 545 686L365 394Q364 391 365 387L554 14Q556 10 556 8Q556 0 545 0H390Q381 0 376 9L251 259Q247 265 244 259L210 205Q208 201 208 199V12Q208 7 204.5 3.5Q201 0 196 0H54Q49 0 45.5 3.5Q42 7 42 12Z"/></g><g transform="translate(2483 203)"><rect x="32.3" y="32.3" width="336.3" height="229.3" rx="114.7" fill="none" stroke="#FF6A13" stroke-width="64.7"/><rect x="278.1" y="32.3" width="336.3" height="229.3" rx="114.7" fill="none" stroke="#FF6A13" stroke-width="64.7"/></g></svg>'
_LL_ICON_PNG = {
    512: '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',
    192: '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',
    180: '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',
    32: 'iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAAE+ElEQVR4nLxXa2wUVRT+7ux2d7v0SaC0vLrUBlrbbWtacZtIKSpqjSGQqE1NFIGKhGhM9Y+QrmgkkCgiNMGqaIoiicak4A9rsURLbBt1i5aUQmkNYlpKW93QB6zuY+Z6Zma73dLd2W0RTjK7M3Mf33fO/e65Z/QItiRLUoJ5ThXAbHQVMIYU/A/GOYbpt4Oun8ZcN97DyOWRiTY2cZOQai2DDh/Ti4W4jcaBAYioHBvs/DZAICEt5xEmCI24g8ZFPCaTYImJ1mQex7sYWBruoCmRYKJVZ5w7fxtjwhPRDHqo1IZvvnwfZrMJF7ov4V+3G7M1Cn08JAwKYLLgIpslfRE+2P863q6pQ6zJiKYTh2E0GnBLRtg6U3zKPrpJitT3wdX3YY45Frv2HsLp1nYUFtyN5ZnpaP25A7M1OQp6Ak+PprPJYMTg8N+B54O1n+BkfR3Mzk+xdL6I6s9oUZ0MMzLC1kfbN9YkwJJqwK6nOUrzOPIz+uDho6hclwaj1IfHVwL2o8CHDcrM0U6LiARi9Bxf7QRKCh1w3/U8imN1MBu8EChLMU8/YhMXQzfeD1nXu5/leGYNUFkDdPdFR0KI1GHroxzF2RyCZ4CuKzAvWAmvKMDRC/hEXZCvjMgCWUuAtn0cO8olRGOaBJLjOKorAGOMPD8BDNbin3nbsPmACWt3CHDHZMJ7/c+gEQx6PYOgA17dQMIt4LglAtUV5LmgTqx0dp3FpQvNeGqjHclJCdSmQ1fv1RAjGXwSR0mOD7MmkJHGsWkteX+TSuKcdZibHI8li1Jh31OD1KTQXpoMjCIQWQdhRfjOFg6RvBD0Uzlm0FHlvPgSSjIYyou6sXBeeJAciwCzUYTLrQvbJ2wEVlvlHRB68ntXMOx+DrAum9re2E5R2w9c7OdKshdJh8VZ2mIMS8DRM7OkcsUJbHyX4Xgbw84jTBGtz8exKhezI9D0G+CJrKGA2SkTur0q6Vx/bjUZSQf5bHYEWs7REihLF3krtfdw1LeqQHEmjqr1k225y2QdSDMncOZ3OdEgKqv6aNLL18qpsov3PxB3KYIOwhIQJYYzvVPf9dNZNHpjakSO/QB0XlYJWBYAL5QFNZIOvKSD+3P4zAnI1tTB4PGq9y1dQN52IHMLwxufA+MuTtuL481jk/33bpKUdBxsJqOABzTygeZh1EqgMRRSuayVQylJgvJ/4ARw9HvK+4uB4RF18lIrR1lRCCByPs+vA5d7ur+aEXD0kA78y1diZah9UULiHDWczjGG1vMqIGOcvA8XZpW8bUVoHWgSCOiAqUAVpQyOg8CTq6aCbX4YyF4aJsx+HdjCCFHgnA9Bw06RDtyeScAUKt4Ovwwct3NFdEXLOewV2lvVEEOnpDC9j4wtR0CzqJPFZwihlDX5NPAQx6k9tO3ibvaeK7pRcwhXftvOh4xAh0B1jSaBSR1oeRkE6BesHPq/Rji+aPaicLsL3/06/UCSsZkpNcti1MVQ4kXYyrjxLQm2bHVIAHDiXgakR4FcuTYuofmshIZffPjxHKfzgap+gepeqhsYm6aREbfovUd5G5+Wu4EmqA9HYJ2N48grMogaBQWQJhy9LuF0p4iTDlEB/GNIUMFkUKZ9BtAc68cHOr+O+uN0a5mEihIJQ9c4GgiwpVNC79UJQNnDiOWlGrBQH6cBi/B5zuV15pKyvtECquPCf57/BwAA///oSBatAAAABklEQVQDABb4xPBq7KETAAAAAElFTkSuQmCC',
}
# LL-5: Test & Tag logo
# LL-7: the landing-page badge shows the real number of checks (it was fixed text)
_LL_LIVE = {"t": 0.0, "text": ""}


def _ll_live_badge():
    import time as _lt
    if _LL_LIVE["text"] and _lt.time() - _LL_LIVE["t"] < 300:
        return _LL_LIVE["text"]
    text = "Free &mdash; AU, UK, US, Canada &amp; EU codes"
    if DATABASE_URL:
        try:
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) FILTER (WHERE ts >= date_trunc('day', NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'), COUNT(*) "
                            "FROM legis_link_audit WHERE ts >= NOW() - INTERVAL '7 days' AND tool <> 'webhook_sale' "
                            "AND status NOT IN ('AUTH_FAIL', 'PRO_REQUIRED', 'RATE_LIMITED', 'ERROR', 'REJECTED_BAD_SECRET')")
                today, week = cur.fetchone()
            if today >= 25:
                text = "Live &mdash; {:,} compliance checks today".format(today)
            elif week >= 25:
                text = "Live &mdash; {:,} compliance checks this week".format(week)
        except Exception:
            pass
    _LL_LIVE.update(t=_lt.time(), text=text)
    return text



async def _ll_icon(request):
    import base64 as _b64
    from starlette.responses import Response as _R
    p = request.url.path
    if p.startswith("/wordmark"):
        return _R(_LL_WORD_LIGHT if "light" in p else _LL_WORD_DARK, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=604800"})
    if p.endswith(".svg"):
        return _R(_LL_ICON_SVG, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=604800"})
    size = 32 if p == "/favicon.ico" else (180 if "apple" in p else (192 if "192" in p else 512))
    return _R(_b64.b64decode(_LL_ICON_PNG[size]), media_type="image/png", headers={"Cache-Control": "public, max-age=604800"})



# LL-10: legis-link.com is the main address. Once LEGIS_PUBLIC_URL is set in Railway, web pages opened on the old
# Railway address or on www are sent to it with a permanent redirect. The MCP endpoints, API, key lookup, webhook
# and /health keep answering on every address, so existing connectors and Gumroad keep working.
_LL_PAGES_TO_MOVE = {"/", "/app", "/connect", "/toolbox", "/roadmap", "/sitemap.xml", "/robots.txt"}


def _ll_canonical(app):
    target = os.environ.get("LEGIS_PUBLIC_URL", "").strip().rstrip("/")
    if not target.startswith("https://"):
        return app
    canon_host = target[len("https://"):].split("/")[0].lower()

    async def wrapped(scope, receive, send):
        if scope.get("type") == "http" and scope.get("method") in ("GET", "HEAD") and scope.get("path") in _LL_PAGES_TO_MOVE:
            host = ""
            for k, v in scope.get("headers") or []:
                if k == b"host":
                    host = v.decode("latin-1").split(":")[0].lower()
                    break
            if host and host != canon_host and (host.endswith(".up.railway.app") or host == "www." + canon_host):
                qs = scope.get("query_string") or b""
                loc = target + scope["path"] + ("?" + qs.decode("latin-1") if qs else "")
                await send({"type": "http.response.start", "status": 301,
                            "headers": [(b"location", loc.encode("latin-1")), (b"content-length", b"0")]})
                await send({"type": "http.response.body", "body": b""})
                return
        await app(scope, receive, send)
    return wrapped


async def _ll_robots(request):
    from starlette.responses import PlainTextResponse as _P
    return _P("User-agent: *\nAllow: /\nSitemap: " + PUBLIC_URL + "/sitemap.xml\n")


# LL-15: proof for the official MCP Registry that we own legis-link.com (public key only, set in Railway)
async def _ll_registry_auth(request):
    from starlette.responses import PlainTextResponse as _P
    line = os.environ.get("MCP_REGISTRY_AUTH", "").strip()
    if not line.startswith("v=MCPv1;"):
        return _P("not configured", status_code=404)
    return _P(line + "\n")


async def _ll_manifest(request):
    from starlette.responses import JSONResponse as _J
    return _J({"name": "Legis-Link", "short_name": "Legis-Link", "description": "Field compliance tool for tradespeople",
               "start_url": "/app", "display": "standalone", "orientation": "portrait",
               "background_color": "#0F1B2D", "theme_color": "#0F1B2D",
               "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
                         {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
                         {"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"}]})


server = Server("legis-link", version=VERSION, website_url=PUBLIC_URL,
                icons=[types.Icon(src=PUBLIC_URL + "/icon-512.png", mimeType="image/png", sizes=["512x512"]),
                       types.Icon(src=PUBLIC_URL + "/icon.svg", mimeType="image/svg+xml", sizes=["any"])])


# ── Claude API call ──────────────────────────────────────────────────────────

def _parse_llm_text(raw_text: str) -> dict:
    """Parse JSON from LLM response text."""
    raw_text = raw_text.strip()
    raw_text = re.sub(r'^```(?:json)?\s*', '', raw_text)
    raw_text = re.sub(r'\s*```$', '', raw_text)
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        match = re.search(r'\{.*\}', raw_text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except Exception:
                pass
        return {"status": "INFO", "result": raw_text, "code_reference": ""}


async def _ask_openai(system_prompt: str, user_message: str) -> dict:
    """Fallback to OpenAI when Anthropic credits exhausted."""
    async with httpx.AsyncClient(timeout=45.0) as client:
        resp = await client.post(
            OPENAI_URL,
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": OPENAI_MODEL,
                "max_tokens": 2048,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": user_message},
                ],
            }
        )
        if resp.status_code != 200:
            return {"status": "ERROR",
                    "result": f"OpenAI error {resp.status_code}: {resp.text[:100]}",
                    "code_reference": ""}
        raw_text = resp.json()["choices"][0]["message"]["content"]
        return _parse_llm_text(raw_text)


# ── LL-3: answer the normal case first, with reviewed practice references ──────
# Reviewed, common-practice facts (no copyrighted table values). Injected into the
# system prompt for the matching trade and country so routine questions are not
# answered with an unstated worst case.
_ANSWER_RULES = (
    "\n\nANSWER THE NORMAL CASE FIRST (these rules override any tendency to assume the worst):\n"
    "- Judge the question as asked. If a deciding condition is not stated (installation method, contact with "
    "thermal insulation, grouping, ambient temperature, route length, fire rating), answer for the ordinary, most "
    "common installation and make the verdict conditional, for example: 'Compliant for a typical installation; "
    "not compliant if the cable is fully surrounded by thermal insulation'.\n"
    "- Use NON_COMPLIANT only when the facts given break a rule. Never mark something NON_COMPLIANT because an "
    "unstated worst case might apply. Use COMPLIANT for a conditional 'yes in the normal case' answer and list "
    "the conditions that would change it.\n"
    "- Do not add requirements the facts do not trigger (for example, ordinary cable in an ordinary wall cavity "
    "does not need fire-rated cable or conduit).\n"
    "- Never recommend a bigger size or heavier product 'to be safe' as the answer; say which condition would require it.\n"
    "- Confidence: 'High' only when the verdict matches the PRACTICE REFERENCE or verified data given here; "
    "otherwise 'Medium' or 'Low'.\n"
    "- A PRACTICE REFERENCE below has been reviewed and is authoritative for common practice: do not contradict it.\n"
    "- Cite standards ONLY by the editions named in the PRACTICE REFERENCE or verified data. Never invent or change an "
    "edition year; if no edition is given, name the standard without a year.\n"
    "- VERDICTS: COMPLIANT only when the facts given (or the normal case set out in the PRACTICE REFERENCE) meet "
    "every rule that decides the outcome. NON_COMPLIANT when the facts given break a rule. REQUIRES_VERIFICATION when a "
    "missing fact would flip the verdict between common configurations (e.g. ring or radial) or a measurement is "
    "borderline: then give the answer for EACH realistic case and list ONLY the missing facts that change the verdict. "
    "Read the facts already given (a circuit supplying socket-outlets IS a final circuit) instead of asking about them.\n"
    "- NUMBERS: use numeric limits ONLY from VERIFIED CALCULATIONS, VERIFIED LICENSING DATA or the PRACTICE REFERENCE, "
    "exactly as given. If another figure is needed, name the table or clause that holds it and do not state a value. "
    "Never quote a value from a superseded edition. When a measured value is given, compare it with the verified "
    "limit and say PASS, FAIL or BORDERLINE explicitly.\n"
    "- SCOPE: keep law, standard and guidance distinct (e.g. UK: Electricity at Work Regulations 1989 = law; BS 7671 = "
    "standard; IET On-Site Guide and GN3 = guidance). Never borrow another country's concepts: NFPA 70E arc-flash PPE "
    "categories, OSHA and the NEC are US-only; Part P, Gas Safe, G98/G99 and MCS are UK-only; ARC/ARCtick are "
    "Australian; RGI and Safe Electric are Irish. Do not mention another country's laws or standards at all - not even "
    "to say they do not apply - unless the user asks about that country.\n"
    "- DOCUMENTS (RAMS, checklists, method statements) are DRAFTS for a competent person to review for the specific "
    "site: never describe a document as compliant, and list the project information still needed.\n"
    "- LANGUAGE: write the whole answer in English, for every country (Czech Republic, Germany, France and so on). "
    "Keep official names of laws, standards and bodies as published (e.g. CSN 33 2000, Handwerksordnung, NF C 15-100) "
    "and add a short English explanation in brackets the first time. Only answer in another language if the user's "
    "question itself is written in that language.\n"
)

_PRACTICE = {
    ("electrical", "AU"): (
        "PRACTICE REFERENCE - Australia, electrical (reviewed):\n"
        "- EDITIONS TO CITE (always exactly these): AS/NZS 3000:2018 (Wiring Rules, as amended) is the current edition - there "
        "is no 2024 edition. AS/NZS 3008.1.1:2017 (selection of cables: current ratings, derating, voltage drop) is the edition "
        "AS/NZS 3000:2018 references; AS/NZS 3008.1.1:2025 was published in December 2025 and may be used voluntarily until "
        "the Wiring Rules reference it. They gain legal force through each state's electrical safety legislation.\n"
        "- AMBIENT TEMPERATURE: AS/NZS 3008.1.1 current ratings already assume 40 C ambient air (and 25 C soil for buried "
        "cables). Ambient derating applies only ABOVE 40 C air (e.g. some roof spaces), never at 25-30 C. Do not apply IEC/BS "
        "30 C reference conditions to Australian cables.\n"
        "- 2.5 mm2 copper TPS protected by a 20 A circuit breaker is the standard arrangement for socket-outlet "
        "(power) final subcircuits. AS/NZS 3000 Appendix C lists such arrangements as deemed to comply, subject to "
        "maximum route lengths. In an ordinary wall cavity (unenclosed, not surrounded by thermal insulation, not "
        "bundled with other loaded circuits) its current rating is comfortably above 20 A, so it is COMPLIANT in the "
        "typical case.\n"
        "- What changes the answer: cable fully surrounded by thermal insulation (heavy derating that can take "
        "2.5 mm2 below 20 A - route it clear of the insulation or upsize); grouping with other loaded circuits; high "
        "ambient temperature (e.g. roof spaces); long runs that fail voltage drop. Read the exact ratings and derating "
        "factors from the AS/NZS 3008.1.1 tables for the installation method; do not state table values.\n"
        "- Voltage drop: total from the point of supply to any point must not exceed 5% of nominal voltage (AS/NZS 3000).\n"
        "- Lighting final subcircuits: 1.0 or 1.5 mm2 TPS on a 10 A or 16 A circuit breaker is common practice.\n"
        "- TPS is routinely run in timber and steel-framed wall cavities; that alone does not require conduit or "
        "fire-rated cable. Holes in steel framing need grommets or bushes; penetrations of fire-rated walls or floors "
        "must be fire-stopped.\n"
        "- 30 mA RCD protection is required for socket-outlet and lighting final subcircuits in domestic installations "
        "under AS/NZS 3000:2018.\n"
    ),
    ("electrical", "UK"): (
        "PRACTICE REFERENCE - United Kingdom, electrical (reviewed):\n"
        "- EDITION TO CITE (always exactly this): BS 7671:2018+A4:2026 (IET Wiring Regulations; Amendment 4 published "
        "15 April 2026; the +A2:2022+A3:2024 version was withdrawn in October 2026). In England and Wales, Building Regulations "
        "Part P applies to notifiable domestic electrical work.\n"
        "- 2.5 mm2 twin and earth (T&E) is the standard cable for a 32 A RING final circuit and for a 20 A RADIAL "
        "circuit (standard circuit arrangements in the IET On-Site Guide). A 32 A RADIAL needs 4 mm2 - 2.5 mm2 T&E "
        "carries 27 A at most. If the question does not say ring or radial for 2.5 mm2 on 32 A, answer both cases.\n"
        "- What changes the answer: contact with or enclosure in thermal insulation (installation reference methods "
        "with heavy derating), grouping, high ambient temperature, and voltage drop on long runs.\n"
        "- Voltage drop (installations supplied from the public LV network): 3% for lighting and 5% for other uses (BS 7671 Appendix 4).\n"
        "- Lighting circuits: 1.0 or 1.5 mm2 T&E on a 6 A or 10 A device is common practice.\n"
        "- 30 mA RCD protection is required for socket-outlets rated up to 32 A and for cables concealed in walls at "
        "less than 50 mm depth that are not otherwise mechanically protected.\n"
    ),
    ("electrical", "US"): (
        "PRACTICE REFERENCE - United States, electrical (reviewed):\n"
        "- Code: NEC (NFPA 70). Editions in use: 2020, 2023 and 2026; the adopted edition differs by state and local authority "
        "having jurisdiction (AHJ). Unless the edition adopted in the named state is given here, write 'the NEC edition "
        "adopted in <state>' instead of guessing a year. Section numbers used here (240.4(D), 210.8, 210.12) are stable "
        "across these editions.\n"
        "- NEC 240.4(D) small-conductor rule (copper): 14 AWG max 15 A, 12 AWG max 20 A, 10 AWG max 30 A overcurrent "
        "protection, unless a specific exception applies (e.g. motor circuits).\n"
        "- 12 AWG copper NM-B on a 20 A breaker is the standard 20 A branch circuit; 14 AWG on 15 A is standard for "
        "15 A circuits; 10 AWG on 30 A for typical 30 A appliance circuits. These are COMPLIANT in the typical case. "
        "14 AWG copper on a 20 A breaker is NON_COMPLIANT.\n"
        "- NM cable ampacity is limited to the 60 C column.\n"
        "- Voltage drop: NEC informational notes suggest about 3% for branch circuits and 5% total; this is a "
        "recommendation, not a general requirement.\n"
        "- GFCI and AFCI requirements depend on the location and the adopted NEC edition (210.8, 210.12).\n"
    ),
    ("electrical", "CA"): (
        "PRACTICE REFERENCE - Canada, electrical (reviewed):\n"
        "- Code: Canadian Electrical Code, Part I (CSA C22.1), adopted by each province with its own amendments; "
        "Ontario enforces it as the Ontario Electrical Safety Code (ESA). Write 'the CEC edition adopted in <province>' "
        "(or 'the OESC' for Ontario) instead of guessing an edition year.\n"
        "- Copper conductor overcurrent limits in typical dwelling use: 14 AWG max 15 A, 12 AWG max 20 A, 10 AWG max "
        "30 A. 14 AWG NMD90 on a 15 A breaker and 12 AWG on a 20 A breaker are COMPLIANT in the typical case; "
        "14 AWG copper on a 20 A breaker is NON_COMPLIANT.\n"
        "- Voltage drop (CEC Rule 8-102): no more than 3% in a feeder or branch circuit and 5% total from the supply "
        "side of the consumer's service to the point of use.\n"
        "- GFCI and AFCI requirements depend on the location and the adopted edition; check the provincial edition.\n"
        "- In Canada, sizes are AWG/kcmil and residential cable is usually NMD90.\n"
    ),
    ("electrical", "IE"): (
        "PRACTICE REFERENCE - Ireland, electrical (reviewed):\n"
        "- Code: I.S. 10101, National Rules for Electrical Installations (NSAI). Do NOT cite BS 7671 for Ireland.\n"
        "- Domestic and similar 'Restricted Electrical Works' must be done by a Registered Electrical Contractor "
        "(Safe Electric), who issues a Completion Certificate.\n"
        "- Sizes in mm2; give circuit sizing from I.S. 10101 and the installation method, and say which table applies "
        "rather than importing UK or US values.\n"
    ),
    ("electrical", "DE"): (
        "PRACTICE REFERENCE - Germany, electrical (reviewed):\n"
        "- Code: DIN VDE 0100 series (installation), DIN VDE 0298-4 (cable current ratings), DIN 18015 (dwellings), "
        "plus the grid operator's TAB. Do NOT cite BS 7671 or the NEC for Germany.\n"
        "- Common dwelling practice: NYM-J 3x1.5 mm2 on a B16 circuit breaker; 2.5 mm2 where the installation "
        "method, length or grouping requires it. Ratings depend on the installation method - say so.\n"
        "- 30 mA RCD (RCD/FI) protection for socket-outlets up to 32 A (DIN VDE 0100-410).\n"
        "- Work behind the house connection fuse needs a firm in the grid operator's Installateurverzeichnis.\n"
    ),
    ("electrical", "FR"): (
        "PRACTICE REFERENCE - France, electrical (reviewed):\n"
        "- Code: NF C 15-100 (low-voltage installations in buildings). Do NOT cite BS 7671 or the NEC for France.\n"
        "- Typical dwelling circuits: lighting 1.5 mm2 on a 16 A circuit breaker; general socket circuits 1.5 mm2 on "
        "16 A (up to 8 sockets) or 2.5 mm2 on 20 A (up to 12 sockets); dedicated 2.5 mm2 / 20 A circuits for "
        "washing machine, dishwasher and oven; cooker 6 mm2 on 32 A. 30 mA differential (RCD) protection for all "
        "circuits.\n"
        "- New or fully renovated installations need an attestation de conformite endorsed by Consuel before "
        "connection.\n"
    ),
    # LL-14: plumbing and gas practice references (AU, UK, US)
    ("plumbing", "AU"): (
        "PRACTICE REFERENCE - Australia, plumbing (reviewed):\n"
        "- EDITIONS: AS/NZS 3500 Parts 1-4 are the 2025 editions (published April 2025), called up by the Plumbing Code of "
        "Australia 2025 (NCC 2025 Volume Three, in effect from 1 May 2026; Victoria required them for work started from "
        "20 October 2025). Cite 'AS/NZS 3500.x:2025'. Plumbing work is done by a licensed plumber who lodges the state's "
        "compliance certificate.\n"
        "- Heated water: stored water at least 60 C (Legionella control). New or altered installations deliver no more than "
        "50 C to personal hygiene fixtures (bath, shower, basin, bidet), and no more than 45 C in early childhood centres, "
        "schools, aged care and health care, using a tempering valve or thermostatic mixing valve to AS 4032.1. Kitchen and "
        "laundry outlets may stay untempered. Since 2021 these delivery limits sit in the PCA, not in AS/NZS 3500.4.\n"
        "- Common minimum grades for sanitary drains: DN100 1.65% (1:60), DN150 1.0% (1:100), DN65 branches 2.5% (1:40); "
        "say to confirm against the AS/NZS 3500.2 grade table for the situation.\n"
        "- Backflow prevention to AS/NZS 3500.1 according to the hazard rating (low, medium, high).\n"
    ),
    ("gas", "AU"): (
        "PRACTICE REFERENCE - Australia, gas (reviewed):\n"
        "- Standard: AS/NZS 5601.1:2022 Gas installations - Part 1: General installations (mandatory in Victoria from "
        "30 March 2023; adopted across the states). AS/NZS 5601.2 covers caravans and boats.\n"
        "- Gas work must be done by a licensed gasfitter, who issues the state's gas compliance certificate. Appliances must "
        "be certified (approved) for use in Australia. Do not say a householder may do their own gas work.\n"
        "- Give clearances, ventilation and pipe sizing by naming the relevant part of AS/NZS 5601.1 and the appliance "
        "manufacturer's instructions; do not invent figures.\n"
    ),
    ("plumbing", "UK"): (
        "PRACTICE REFERENCE - United Kingdom, plumbing (reviewed):\n"
        "- Water fittings: Water Supply (Water Fittings) Regulations 1999 in England and Wales (Scottish Water Byelaws 2014 in "
        "Scotland; Water Supply (Water Fittings) Regulations (NI) 2009). Backflow protection by fluid category 1-5. Some "
        "installations must be notified to the water supplier before work starts.\n"
        "- Building Regulations (England) Part G: hot water to a bath in a NEW dwelling must not exceed 48 C (G3, usually a "
        "thermostatic mixing valve). Unvented hot water storage over 15 litres must be installed by a competent person with "
        "the required safety devices, and the work is notifiable.\n"
        "- Legionella (HSE ACOP L8 / HSG274): store hot water at 60 C or above and distribute it so outlets reach at least "
        "50 C within one minute (55 C in healthcare).\n"
        "- Approved Document H drainage gradients: 100 mm pipe 1:40, or 1:80 where at least one WC discharges to it; 150 mm "
        "pipe 1:150 where at least five WCs discharge to it.\n"
    ),
    ("gas", "UK"): (
        "PRACTICE REFERENCE - United Kingdom, gas (reviewed):\n"
        "- Gas Safety (Installation and Use) Regulations 1998: work on gas fittings in homes, rented property, shops, "
        "schools, hospitals and similar must be done by a Gas Safe registered engineer qualified for that appliance type. "
        "A handyman or unregistered person may not do it for a landlord or customer.\n"
        "- Landlords: annual gas safety check by a Gas Safe engineer (regulation 36), record kept for 2 years and given to "
        "tenants within 28 days.\n"
        "- Carbon monoxide: in England, Building Regulations Part J require a CO alarm when a fixed combustion appliance "
        "(not a gas cooker) is installed in a dwelling (since 1 October 2022); rented homes also fall under the Smoke and "
        "Carbon Monoxide Alarm (Amendment) Regulations 2022.\n"
        "- Domestic gas pipework: BS 6891 (name the standard without an edition year).\n"
    ),
    ("plumbing", "US"): (
        "PRACTICE REFERENCE - United States, plumbing (reviewed):\n"
        "- Code: the International Plumbing Code (IPC) or the Uniform Plumbing Code (UPC), depending on the state and city. "
        "California uses the California Plumbing Code (UPC-based); Florida uses the Florida Building Code - Plumbing "
        "(IPC-based); New York State uses an IPC-based code and New York City has its own plumbing code; Texas cities adopt "
        "the IPC or the UPC. Name the adopted code; do not guess an edition year.\n"
        "- IPC minimum slope for horizontal drainage pipe: 2-1/2 in and smaller 1/4 in per ft; 3 to 6 in 1/8 in per ft; "
        "8 in and larger 1/16 in per ft. The UPC generally requires 1/4 in per ft, with less allowed only where approved.\n"
        "- Shower and tub-shower valves must be pressure-balance, thermostatic or combination type limiting water to 120 F "
        "maximum. In the IPC, public hand-washing lavatories get tempered water at no more than 110 F (ASSE 1070 device).\n"
        "- Water heaters need a temperature and pressure (T&P) relief valve with a full-size discharge pipe to a safe "
        "location.\n"
    ),
    ("gas", "US"): (
        "PRACTICE REFERENCE - United States, gas (reviewed):\n"
        "- Code: the International Fuel Gas Code (IFGC) or NFPA 54 National Fuel Gas Code, as adopted by the state or city "
        "(name the adopted code; do not guess an edition year). LP-gas also falls under NFPA 58 and state LP-gas rules "
        "(in Texas, the Railroad Commission).\n"
        "- Gas piping work generally needs a licensed plumber or gas fitter and a permit with inspection and a pressure "
        "test before the gas is turned on. Follow the manufacturer's instructions and the adopted code for CSST bonding.\n"
    ),
    # LL-18: plumbing and gas practice references (Canada, Ireland, Germany, France)
    ("plumbing", "CA"): (
        "PRACTICE REFERENCE - Canada, plumbing (reviewed):\n"
        "- Code: the National Plumbing Code of Canada as adopted by the province. Ontario: the 2024 Ontario Building Code "
        "(O. Reg. 163/24, in force 1 January 2025), Part 7 Plumbing. British Columbia: the BC Plumbing Code. Alberta: the "
        "National Plumbing Code - Alberta Edition. Name the provincial code; do not guess an edition year for BC or Alberta.\n"
        "- Hot water from a shower head or into a bathtub must not exceed 49 C (43 C in health care facilities and retirement "
        "homes), using a thermostatic, pressure-balancing or combination valve (OBC 7.2.10.7 and the NPC equivalent).\n"
        "- Minimum slope of horizontal drainage pipe: 3 in (75 mm) and smaller at least 1 in 50 (2%, about 1/4 in per ft); "
        "a 4 in (100 mm) building drain, building sewer or trap arm may be 1 in 100 (1%); larger drains have flatter grades "
        "in the code's tables.\n"
        "- Plumbing work needs a municipal permit and inspection, and a certified plumber (compulsory trade in Ontario and "
        "Alberta; see the licensing data for BC).\n"
    ),
    ("gas", "CA"): (
        "PRACTICE REFERENCE - Canada, gas (reviewed):\n"
        "- Code: CSA B149.1 Natural gas and propane installation code (B149.2 for propane storage and handling), adopted by "
        "each province. Alberta: CSA B149.1:25 in force since 1 March 2026. Ontario (TSSA code adoption document) and BC "
        "(Technical Safety BC) are moving to B149.1:25; if unsure, write 'the B149.1 edition adopted in <province>'.\n"
        "- Gas work must be done by a certified gas fitter or gas technician (Ontario: TSSA G.3/G.2/G.1; BC and Alberta: "
        "Gasfitter Class A or B) working for a licensed or registered contractor, with a permit and inspection where the "
        "province requires it. Do not say a householder may do their own gas work unless the licensing data says so.\n"
        "- Give clearances, venting and pipe sizing by naming the relevant B149.1 clause and the appliance manufacturer's "
        "instructions; do not invent figures.\n"
    ),
    ("plumbing", "IE"): (
        "PRACTICE REFERENCE - Ireland, plumbing (reviewed):\n"
        "- Building Regulations: Technical Guidance Document G (hygiene, including hot water storage safety) and TGD H "
        "(drainage); I.S. EN 806 for water installations. Do NOT cite UK Approved Documents or the UK Water Fittings "
        "Regulations for Ireland.\n"
        "- No licence is needed to work as a plumber, but any work on gas pipework or appliances needs a Registered Gas "
        "Installer (RGI).\n"
        "- Legionella control (HSA guidance): store hot water at 60 C or above and distribute it at 50 C or above; fit "
        "thermostatic mixing valves where scalding is a risk.\n"
    ),
    ("gas", "IE"): (
        "PRACTICE REFERENCE - Ireland, gas (reviewed):\n"
        "- Standard: I.S. 813 Domestic gas installations (I.S. 813:2014+A1:2017). Do NOT cite BS 6891 or Gas Safe for Ireland.\n"
        "- By law only a Registered Gas Installer (RGI) may work on domestic gas installations; the RGI issues a Declaration "
        "of Conformance / completion certificate. Doing gas work unregistered is an offence prosecuted by the CRU.\n"
        "- Building Regulations Part J (TGD J) require a carbon monoxide alarm with certain new or replacement combustion "
        "appliances in dwellings; name TGD J rather than inventing which appliances.\n"
    ),
    ("plumbing", "DE"): (
        "PRACTICE REFERENCE - Germany, plumbing (reviewed):\n"
        "- Rules: DIN EN 806 and DIN 1988 (drinking water installations), DVGW W 551 (Legionella), the Trinkwasserverordnung "
        "(TrinkwV), and DIN EN 12056 / DIN 1986-100 for drainage. Do NOT cite UK or US codes for Germany.\n"
        "- Hot water hygiene (DVGW W 551 / DIN 1988-200): in large systems (store over 400 litres or more than 3 litres of "
        "pipe between heater and tap) water must leave the heater at 60 C and the circulation return must not fall below "
        "55 C; 60 C is also recommended for small systems in one- and two-family houses.\n"
        "- Work on the drinking water installation behind the house connection must be done by a firm entered in the water "
        "supplier's Installateurverzeichnis (AVBWasserV s.12), run under the Handwerksordnung (Meister).\n"
    ),
    ("gas", "DE"): (
        "PRACTICE REFERENCE - Germany, gas (reviewed):\n"
        "- Technical rules: DVGW-TRGI 2018 (DVGW G 600) for gas installations in buildings. Do NOT cite UK or US gas codes.\n"
        "- Gas installations behind the main shut-off may only be installed, changed or maintained by a firm in the gas "
        "network operator's Installateurverzeichnis (NDAV s.13), run under the Handwerksordnung (Meister).\n"
        "- New gas appliances and flues are checked by the district chimney sweep (bevollmaechtigter Bezirksschornsteinfeger) "
        "under the state building code before use; say to confirm the local procedure.\n"
    ),
    ("plumbing", "FR"): (
        "PRACTICE REFERENCE - France, plumbing (reviewed):\n"
        "- Rules: NF DTU 60.1 / 60.11 (plumbing and sizing), and the arrete du 23 juin 1978 art. 36 as amended by the "
        "arrete du 30 novembre 2005 (hot water). Do NOT cite UK or US codes for France.\n"
        "- Hot water in new installations: no more than 50 C at taps in rooms used for washing (bathroom, shower), no more "
        "than 60 C in other rooms (e.g. kitchen); stores of 400 litres or more must deliver at least 55 C; where the "
        "distribution pipework holds more than 3 litres, water must stay at 50 C or above except in the last 3 litres "
        "before each tap.\n"
        "- The firm needs the qualification professionnelle (CAP/BEP or 3 years' experience) and decennale insurance.\n"
    ),
    ("gas", "FR"): (
        "PRACTICE REFERENCE - France, gas (reviewed):\n"
        "- Rules: arrete du 2 aout 1977 (domestic gas installations) and NF DTU 61.1. Do NOT cite UK or US gas codes.\n"
        "- A new or fully renovated gas installation needs a certificat de conformite signed by the installer and endorsed "
        "by Qualigaz before the distributor supplies gas.\n"
        "- The firm needs the qualification professionnelle and decennale insurance; the 'PG - Professionnel du Gaz' label is "
        "voluntary.\n"
    ),
    # LL-20: electrical, plumbing and gas references for Spain, Italy, Netherlands
    ("electrical", "ES"): (
        "PRACTICE REFERENCE - Spain, electrical (reviewed):\n"
        "- Code: Reglamento Electrotecnico para Baja Tension (REBT, RD 842/2002) and its ITC-BT instructions. Do NOT cite "
        "BS 7671, the NEC or IEC tables as if they were Spanish law.\n"
        "- Dwelling circuits (ITC-BT-25): C1 lighting 1.5 mm2 on 10 A; C2 general sockets 2.5 mm2 on 16 A; C3 cooker and "
        "oven 6 mm2 on 25 A; C4 washing machine, dishwasher and water heater 4 mm2 on 20 A; C5 bathroom and kitchen "
        "sockets 2.5 mm2 on 16 A. 30 mA differential protection.\n"
        "- Work is done by an empresa instaladora habilitada, which issues the certificado de instalacion (boletin).\n"
    ),
    ("electrical", "IT"): (
        "PRACTICE REFERENCE - Italy, electrical (reviewed):\n"
        "- Rules: CEI 64-8 (low-voltage installations) and DM 37/2008. Do NOT cite BS 7671 or the NEC.\n"
        "- Common dwelling practice: lighting 1.5 mm2 on 10 A, socket circuits 2.5 mm2 on 16 A; 30 mA differential (RCD) "
        "protection. Ratings depend on the installation method - say so.\n"
        "- Work is done by an impresa abilitata (DM 37/2008 lettera a), which issues the Dichiarazione di Conformita.\n"
    ),
    ("electrical", "NL"): (
        "PRACTICE REFERENCE - Netherlands, electrical (reviewed):\n"
        "- Rules: NEN 1010 (low-voltage installations), called up by the Besluit bouwwerken leefomgeving (Bbl); NEN 3140 "
        "for safe operation. Do NOT cite BS 7671 or the NEC. Name NEN 1010 without guessing an edition year.\n"
        "- 30 mA RCD (aardlekschakelaar) protection for final circuits in dwellings. Ratings depend on the installation "
        "method - say so.\n"
        "- No licence is required to be an electrician, but the installation must meet NEN 1010.\n"
    ),
    ("plumbing", "ES"): (
        "PRACTICE REFERENCE - Spain, plumbing (reviewed):\n"
        "- Rules: CTE DB HS 4 (water supply) and DB HS 5 (drainage); hot water production and heating under the RITE "
        "(RD 1027/2007). Do NOT cite UK or US codes.\n"
        "- Heating, hot water and air conditioning systems must be installed by a RITE-enabled company; plain water and "
        "drainage work has no national licence (check regional rules).\n"
    ),
    ("plumbing", "IT"): (
        "PRACTICE REFERENCE - Italy, plumbing (reviewed):\n"
        "- Water and sanitary installations are DM 37/2008 lettera d) (heating lettera c): only an impresa abilitata may do "
        "the work, and it must issue the Dichiarazione di Conformita. Do NOT cite UK or US codes.\n"
    ),
    ("plumbing", "NL"): (
        "PRACTICE REFERENCE - Netherlands, plumbing (reviewed):\n"
        "- Drinking water installations must meet NEN 1006 (called up by the Bbl). No licence is needed to be a plumber, "
        "but any work on gas combustion appliances needs a CO-certified company. Do NOT cite UK or US codes.\n"
    ),
    ("gas", "ES"): (
        "PRACTICE REFERENCE - Spain, gas (reviewed):\n"
        "- Rules: Reglamento tecnico de distribucion y utilizacion de combustibles gaseosos (RD 919/2006). Gas work must be "
        "done by an empresa instaladora de gas habilitada (category A, B or C) with a qualified persona instaladora; it "
        "issues the installation certificate needed before supply. Do NOT cite UK or US codes.\n"
    ),
    ("gas", "IT"): (
        "PRACTICE REFERENCE - Italy, gas (reviewed):\n"
        "- Rules: UNI 7129 (domestic gas installations) and DM 37/2008 lettera e). Only an impresa abilitata may do the "
        "work and it issues the Dichiarazione di Conformita, which the distributor needs before turning on new supply. "
        "Do NOT cite UK or US codes.\n"
    ),
    ("gas", "NL"): (
        "PRACTICE REFERENCE - Netherlands, gas (reviewed):\n"
        "- Since 1 April 2023 only CO-certified companies (InstallQ BRL 6000-25 or BRL K25000) may install, maintain, "
        "repair or commission gas combustion appliances such as central heating boilers, gas fires, water heaters and air "
        "heaters; the client can also be prosecuted for hiring an uncertified firm. Gas installations follow NEN 1078. "
        "Do NOT cite UK or US codes.\n"
    ),
    # LL-21: solar PV and battery practice references (AU, UK, US)
    ("solar", "AU"): (
        "PRACTICE REFERENCE - Australia, solar PV and batteries (reviewed):\n"
        "- EDITIONS TO CITE: AS/NZS 4777.1:2024 (grid-connected inverter installation, mandatory from 23 February 2025; "
        "inverters must comply with AS/NZS 4777.2:2020); AS/NZS 5033:2021 as amended (PV arrays); AS/NZS 5139:2019 with "
        "Amendment 1:2025 (battery systems, published 19 December 2025 and now mandatory); AS/NZS 3000:2018 for the "
        "rest of the installation.\n"
        "- All low-voltage electrical work must be done by a licensed electrician. Solar Accreditation Australia (SAA) "
        "accreditation of the designer and installer is required for the system to earn STCs under the Small-scale "
        "Renewable Energy Scheme (SAA replaced the Clean Energy Council's accreditation in 2024).\n"
        "- Batteries must not be installed in habitable rooms and must respect AS/NZS 5139 restricted locations "
        "(clearances to exits, windows, vents and other openings); the 2025 amendment allows a battery within 600 mm of "
        "an opening wider than 900 mm such as a garage door, treats inverters as associated appliances, and requires a "
        "physical safety data sheet on site.\n"
        "- Grid connection needs the network distributor's approval; single-phase inverter capacity limits and export "
        "limits are set by the distributor (AS/NZS 4777.1:2024 caps single-phase at 30 kVA).\n"
    ),
    ("solar", "UK"): (
        "PRACTICE REFERENCE - United Kingdom, solar PV and batteries (reviewed):\n"
        "- Electrical: BS 7671:2018+A4:2026, including Section 712 (PV systems) and Chapter 57 (stationary secondary "
        "batteries). New circuits and consumer unit work in homes are notifiable under Part P (England and Wales); issue "
        "an Electrical Installation Certificate.\n"
        "- Grid connection: EREC G98 for generation up to 16 A per phase (notify the DNO within 28 days of commissioning); "
        "EREC G99 for larger systems or higher export (apply to the DNO and get approval BEFORE connecting).\n"
        "- MCS certification (MIS 3002 for PV, MIS 3012 for batteries) is needed for most Smart Export Guarantee tariffs "
        "and government-backed schemes; it is a scheme requirement, not a licence.\n"
        "- Battery placement (PAS 63100:2024, best practice rather than law): outdoors preferred; not in rooms used for "
        "sleeping or cupboards opening into them, escape routes, lofts, voids or roof spaces, or within 2 m of fuel or "
        "flammable material; indoor systems up to 40 kWh, outdoor up to 80 kWh; BS 5839-6 smoke detector where the "
        "battery is in a rarely visited space.\n"
    ),
    ("solar", "US"): (
        "PRACTICE REFERENCE - United States, solar PV and energy storage (reviewed):\n"
        "- NEC Article 690 (PV), 705 (interconnected sources), 706 (energy storage systems) and 480 (batteries); cite the "
        "NEC edition adopted in the state instead of guessing a year. Rooftop PV on buildings needs rapid shutdown "
        "(NEC 690.12). For a load-side connection to a busbar, the common 120% rule applies (125% of the inverter output "
        "current plus the main breaker rating must not exceed 120% of the busbar rating, NEC 705.12).\n"
        "- Energy storage systems must be listed to UL 9540 (NEC 706.5; IRC R328). In homes, ESS may not go in habitable "
        "space; permitted locations include garages separated from living space, outdoors on an exterior wall at least "
        "3 ft from doors and windows, and enclosed utility spaces that do not open into a bedroom (IRC R328); NFPA 855 "
        "sets capacity limits.\n"
        "- Licensing: a licensed electrician or electrical contractor (some states have a solar-specific licence, e.g. "
        "California C-46), plus building and electrical permits and the utility's interconnection approval. NABCEP "
        "certification is voluntary.\n"
    ),
    # LL-22: HVAC/refrigeration and carpentry references for all 10 countries
    ("refrigeration", "AU"): (
        "PRACTICE REFERENCE - Australia, HVAC and refrigeration (reviewed):\n"
        "- Anyone handling refrigerant needs an Australian Refrigeration Council (ARC) refrigerant handling licence, and "
        "businesses that buy or store refrigerant need an ARCtick refrigerant trading authorisation (Ozone Protection and "
        "Synthetic Greenhouse Gas Management Act 1989). Electrical connection needs a licensed electrician or the state's "
        "restricted electrical licence for disconnect/reconnect.\n"
        "- Standards: AS/NZS 5149 (safety of refrigerating systems and heat pumps, including A2L refrigerants such as R32), "
        "AS 1668.2 (ventilation), AS/NZS 3666 (air-handling and cooling water systems, Legionella), AS/NZS 3000 for wiring.\n"
    ),
    ("refrigeration", "UK"): (
        "PRACTICE REFERENCE - United Kingdom, HVAC and refrigeration (reviewed):\n"
        "- F-gas: the GB F-gas Regulation (retained Regulation (EU) 517/2014 as amended) - a personal F-gas certificate "
        "(categories I-IV) to install, service, leak-check or recover refrigerant, and company F-gas certification for "
        "firms doing this work for others.\n"
        "- Standards and rules: BS EN 378 (refrigerating systems and heat pumps, including A2L refrigerants), Building "
        "Regulations Part F (ventilation) and Part L (energy efficiency); heat pumps under MCS MIS 3005 for grants and "
        "export schemes. Gas-fired heating is gas work for a Gas Safe engineer.\n"
    ),
    ("refrigeration", "US"): (
        "PRACTICE REFERENCE - United States, HVAC and refrigeration (reviewed):\n"
        "- EPA Section 608 certification (Type I, II, III or Universal) to buy or handle refrigerants.\n"
        "- Refrigerant transition (AIM Act technology transitions rule): new residential and light-commercial AC and heat "
        "pump systems must use refrigerants with GWP below 700 (e.g. R-454B, R-32), and R-410A components made or "
        "imported on or after 1 January 2025 may not be used to assemble new systems. EPA removed the separate R-410A "
        "installation deadline (rule effective 27 July 2026), so qualifying legacy inventory may still be installed and "
        "existing R-410A systems may be serviced.\n"
        "- Codes: IMC or UMC and the IRC mechanical chapters as adopted; ASHRAE 15 and UL 60335-2-40 for A2L refrigerants; "
        "load calculation and equipment sizing per ACCA Manual J and S (IRC M1401.3).\n"
    ),
    ("refrigeration", "CA"): (
        "PRACTICE REFERENCE - Canada, HVAC and refrigeration (reviewed):\n"
        "- CSA B52 Mechanical refrigeration code; CSA B149.1 for gas-fired equipment; CSA F280 for residential heat loss "
        "and gain (equipment sizing) where the provincial building code calls it up.\n"
        "- Refrigerant handling: provincial rules (Ontario O. Reg. 463/10 ODP card; other provinces have their own "
        "halocarbon regulations); the federal Halocarbon Regulations apply to federal works. Refrigeration and AC "
        "mechanic is a compulsory trade in Ontario, BC and Alberta (see the licensing data).\n"
    ),
    ("refrigeration", "IE"): (
        "PRACTICE REFERENCE - Ireland, HVAC and refrigeration (reviewed):\n"
        "- F-gas: EU Regulation 2024/573 (replaced 517/2014 in March 2024) and Irish S.I.s - personal F-gas certificate and "
        "company certificate from F-gas Registration Ltd.\n"
        "- I.S. EN 378 for refrigerating systems and heat pumps; Building Regulations TGD F (ventilation) and TGD L "
        "(energy). Gas-fired heating needs a Registered Gas Installer. Do NOT cite UK rules.\n"
    ),
    ("refrigeration", "DE"): (
        "PRACTICE REFERENCE - Germany, HVAC and refrigeration (reviewed):\n"
        "- F-gas: EU Regulation 2024/573 and the ChemKlimaschutzV - Sachkundebescheinigung for personnel and company "
        "certification. Kaelteanlagenbauer and Installateur und Heizungsbauer are Meister trades (Handwerksordnung).\n"
        "- DIN EN 378 for refrigerating systems and heat pumps; the Gebaeudeenergiegesetz (GEG) sets requirements for new "
        "heating systems - name the GEG rather than quoting a percentage, as its rules are changing. Do NOT cite UK or "
        "US codes.\n"
    ),
    ("refrigeration", "FR"): (
        "PRACTICE REFERENCE - France, HVAC and refrigeration (reviewed):\n"
        "- Fluides frigorigenes: attestation de capacite for the company and attestation d'aptitude for personnel (Code de "
        "l'environnement R543-75 ff.); EU Regulation 2024/573.\n"
        "- NF EN 378; RE2020 for new buildings; RGE certification is needed for clients to get MaPrimeRenov' and CEE "
        "grants on heat pumps. Do NOT cite UK or US codes.\n"
    ),
    ("refrigeration", "ES"): (
        "PRACTICE REFERENCE - Spain, HVAC and refrigeration (reviewed):\n"
        "- RITE (RD 1027/2007) for heating, cooling and ventilation of buildings, installed by a RITE-enabled company; "
        "RSIF (RD 552/2019) for refrigerating installations (empresa frigorista Nivel 1 or 2); F-gas handling certificate "
        "(RD 115/2017). UNE-EN 378. Do NOT cite UK or US codes.\n"
    ),
    ("refrigeration", "IT"): (
        "PRACTICE REFERENCE - Italy, HVAC and refrigeration (reviewed):\n"
        "- DM 37/2008 lettera c) (impresa abilitata, Dichiarazione di Conformita); DPR 146/2018 F-gas certificates and the "
        "national F-gas database; DPR 74/2013 for maintenance and energy-efficiency checks of heating and cooling systems "
        "(libretto di impianto). UNI EN 378. Do NOT cite UK or US codes.\n"
    ),
    ("refrigeration", "NL"): (
        "PRACTICE REFERENCE - Netherlands, HVAC and refrigeration (reviewed):\n"
        "- F-gas: EU Regulation 2024/573 - personal and company F-gas certificates. NEN-EN 378. Gas-fired heating needs a "
        "CO-certified company (since 1 April 2023). Ventilation under the Bbl. Do NOT cite UK or US codes.\n"
    ),
    ("carpentry", "AU"): (
        "PRACTICE REFERENCE - Australia, carpentry (reviewed):\n"
        "- Timber framing: AS 1684 (residential timber-framed construction) and the NCC Housing Provisions; engineered "
        "timber per the manufacturer's span tables.\n"
        "- Stairs (NCC Housing Provisions Part 11.2): 2 to 18 risers per flight; riser and going limits from Table 11.2.2a "
        "(risers 115-190 mm, goings 240-355 mm, 2R+G 550-700 mm); no openings in risers that let a 125 mm sphere pass.\n"
        "- Barriers (Part 11.3): required where the fall is more than 1 m; at least 1 m high (865 mm along a stair flight); "
        "openings must not let a 125 mm sphere pass; where the fall is more than 4 m, no climbable elements between "
        "150 mm and 760 mm above the floor.\n"
    ),
    ("carpentry", "UK"): (
        "PRACTICE REFERENCE - United Kingdom, carpentry (reviewed):\n"
        "- Approved Document K (England) private stairs: rise 150-220 mm, going 220-300 mm, maximum pitch 42 degrees, "
        "2R+G 550-700 mm, headroom 2 m. Guarding in dwellings: 900 mm on stairs and landings, 1100 mm at balconies and "
        "other edges; no openings that let a 100 mm sphere pass where children under 5 may use the building.\n"
        "- Structural timber: Eurocode 5 (BS EN 1995) with TRADA span tables; Approved Document A. Scotland, Wales and "
        "Northern Ireland have their own technical guidance - say so for those nations.\n"
    ),
    ("carpentry", "US"): (
        "PRACTICE REFERENCE - United States, carpentry (reviewed):\n"
        "- IRC (as adopted by the state) stairs, R311.7: maximum riser 7-3/4 in, minimum tread 10 in, headroom 6 ft 8 in, "
        "handrail 34-38 in high. Guards (R312): required where the drop is more than 30 in; minimum 36 in high; openings "
        "must not pass a 4 in sphere.\n"
        "- Framing: IRC Chapter 5 and 6 span tables and the American Wood Council NDS / WFCM. Name the IRC edition adopted "
        "locally rather than guessing a year.\n"
    ),
    ("carpentry", "CA"): (
        "PRACTICE REFERENCE - Canada, carpentry (reviewed):\n"
        "- Part 9 of the National Building Code as adopted by the province (Ontario: 2024 OBC Part 9, Section 9.8 stairs "
        "and guards). Guards are required where the difference in elevation is more than 600 mm; openings must not let a "
        "100 mm sphere pass; avoid climbable members between 140 mm and 900 mm. Typical guard heights are 900 mm within "
        "dwelling units and 1070 mm elsewhere - confirm in 9.8.8 for the province.\n"
        "- Framing: Part 9 span tables (Section 9.23) and CSA O86 for engineered design.\n"
    ),
    ("carpentry", "IE"): (
        "PRACTICE REFERENCE - Ireland, carpentry (reviewed):\n"
        "- Building Regulations TGD K (stairways, ladders, ramps and guards) and TGD A (structure); I.S. EN 1995 (Eurocode "
        "5) for timber. Cite TGD K, not the UK Approved Document K, though many values are similar (private stair maximum "
        "rise 220 mm and pitch 42 degrees).\n"
    ),
    ("carpentry", "DE"): (
        "PRACTICE REFERENCE - Germany, carpentry (reviewed):\n"
        "- Stairs: DIN 18065 (in residential buildings with up to two dwellings: rise 14-20 cm, going 23-37 cm). Guards "
        "(Umwehrungen) under the state building code (Landesbauordnung): typically at least 90 cm, 110 cm where the fall is "
        "more than 12 m. Structural timber: DIN EN 1995 (Eurocode 5). Zimmerer and Tischler are Meister trades. Do NOT cite "
        "UK or US codes.\n"
    ),
    ("carpentry", "FR"): (
        "PRACTICE REFERENCE - France, carpentry (reviewed):\n"
        "- Timber frames and roofs: NF DTU 31.1 (charpente) and NF DTU 31.2 (maisons a ossature bois); Eurocode 5. Guards "
        "(garde-corps): NF P01-012 - protection height at least 1 m and gaps no larger than 11 cm. Decennale insurance is "
        "mandatory. Do NOT cite UK or US codes.\n"
    ),
    ("carpentry", "ES"): (
        "PRACTICE REFERENCE - Spain, carpentry (reviewed):\n"
        "- CTE DB SUA 1: a protection barrier is required where the drop is more than 55 cm; height at least 0.90 m (fall "
        "up to 6 m) or 1.10 m (fall over 6 m); no gaps that let a 10 cm sphere pass; in dwellings no footholds between 30 "
        "and 50 cm. Timber structures: CTE DB SE-M. Do NOT cite UK or US codes.\n"
    ),
    ("carpentry", "IT"): (
        "PRACTICE REFERENCE - Italy, carpentry (reviewed):\n"
        "- Structural timber: NTC 2018 (Norme Tecniche per le Costruzioni) and Eurocode 5 (UNI EN 1995). Guard and stair "
        "dimensions are set by local building regulations (regolamento edilizio) - say to check the comune. Firms on "
        "construction sites need the patente a crediti. Do NOT cite UK or US codes.\n"
    ),
    ("carpentry", "NL"): (
        "PRACTICE REFERENCE - Netherlands, carpentry (reviewed):\n"
        "- Bbl (new build): a vloerafscheiding (guard) is required where the height difference is more than 1 m; at least "
        "1 m high (higher for large falls); openings no wider than 0.1 m within 0.7 m of the floor; no footholds between "
        "0.2 and 0.7 m in dwellings. Structural timber: NEN-EN 1995 (Eurocode 5). New houses fall under the Wkb. Do NOT "
        "cite UK or US codes.\n"
    ),
    # LL-23: solar for Canada and the EU, and roofing for all 10 countries
    ("solar", "CA"): (
        "PRACTICE REFERENCE - Canada, solar PV and batteries (reviewed):\n"
        "- Canadian Electrical Code Part I (CSA C22.1) Section 64 (renewable energy and storage systems) and Section 84 "
        "(interconnection), as adopted by the province (Ontario: OESC with an ESA notification). Inverters certified to "
        "CSA C22.2 No. 107.1 / UL 1741.\n"
        "- Work by a licensed electrical contractor (Ontario ESA licence; BC Technical Safety BC; Alberta Master Electrician "
        "permits). The local utility must approve the connection (Alberta: Micro-generation Regulation).\n"
    ),
    ("solar", "IE"): (
        "PRACTICE REFERENCE - Ireland, solar PV and batteries (reviewed):\n"
        "- Micro-generation (up to 25 A single-phase, about 6 kVA, or 16 A three-phase, about 11 kVA): notify ESB "
        "Networks (NC6 form or online) with the inverter's type test certificate to I.S. EN 50549-1 (Irish settings); "
        "larger systems need a connection application. Installation must be by a Registered Electrical Contractor (Safe "
        "Electric) to I.S. 10101. SEAI solar grants require an SEAI-registered company. Do NOT cite UK G98/G99 or MCS.\n"
    ),
    ("solar", "DE"): (
        "PRACTICE REFERENCE - Germany, solar PV and batteries (reviewed):\n"
        "- Rooftop systems: apply to the grid operator (Netzbetreiber) before connection; installed and commissioned by an "
        "electrician entered in the grid operator's Installateurverzeichnis, following VDE-AR-N 4105 and the operator's "
        "TAB; then register in the Marktstammdatenregister within one month (needed for the EEG feed-in tariff).\n"
        "- Plug-in balcony systems (Steckersolargeraete): inverter output up to 800 W, registration only in the "
        "Marktstammdatenregister (simplified form). Do NOT cite UK or US rules.\n"
    ),
    ("solar", "FR"): (
        "PRACTICE REFERENCE - France, solar PV and batteries (reviewed):\n"
        "- NF C 15-100 and the PV installation guide UTE C 15-712-1 (and -2 for batteries); a Consuel attestation de "
        "conformite before Enedis connects the system; a declaration prealable de travaux (urbanisme) for roof panels in "
        "most cases. An RGE (QualiPV) installer is required for the purchase tariff and self-consumption premium - check "
        "the current arrete tarifaire. Do NOT cite UK or US rules.\n"
    ),
    ("solar", "ES"): (
        "PRACTICE REFERENCE - Spain, solar PV and batteries (reviewed):\n"
        "- Real Decreto 244/2019 (autoconsumo) and REBT ITC-BT-40 (generating installations); installed by an empresa "
        "instaladora habilitada, which issues the certificado de instalacion; registration in the autonomous community's "
        "self-consumption register; municipal licence or declaracion responsable for the works. Do NOT cite UK or US rules.\n"
    ),
    ("solar", "IT"): (
        "PRACTICE REFERENCE - Italy, solar PV and batteries (reviewed):\n"
        "- DM 37/2008 lettera a) (impresa abilitata, Dichiarazione di Conformita); connection to the low-voltage grid under "
        "CEI 0-21, through the grid operator (small systems via the simplified Modello Unico); GSE for incentives. Roof "
        "panels on most buildings count as edilizia libera, except in protected areas. Do NOT cite UK or US rules.\n"
    ),
    ("solar", "NL"): (
        "PRACTICE REFERENCE - Netherlands, solar PV and batteries (reviewed):\n"
        "- NEN 1010 (including the PV section) for the installation; register the system with the grid operator via "
        "energieleveren.nl. Net metering (salderingsregeling) ends on 1 January 2027 by law, so payback advice should not "
        "assume it continues. No licence is required, but work must meet NEN 1010. Do NOT cite UK or US rules.\n"
    ),
    ("roofing", "AU"): (
        "PRACTICE REFERENCE - Australia, roofing (reviewed):\n"
        "- WHS Regulations: work where a person could fall more than 2 m is high-risk construction work needing a SWMS; "
        "control falls with edge protection, scaffolding or guardrails first, then fall-arrest systems.\n"
        "- Metal roofing to AS 1562.1 and the manufacturer's installation guide (HB 39 is the industry installation "
        "code); tiles to AS 2050; NCC Housing Provisions for roof construction. Roof plumbing (gutters, flashings, "
        "downpipes) is licensed plumbing work in most states.\n"
    ),
    ("roofing", "UK"): (
        "PRACTICE REFERENCE - United Kingdom, roofing (reviewed):\n"
        "- Work at Height Regulations 2005 and CDM 2015: avoid work at height where possible, then use collective "
        "protection (scaffold edge protection) before personal fall arrest; there is no minimum height below which the "
        "rules do not apply.\n"
        "- Slating and tiling to BS 5534 (fixing specification for wind uplift); flat roofs to BS 8217 / BS 6229. "
        "Replacing more than 25% of a roof is notifiable building work and triggers thermal upgrade under Part L where "
        "practical.\n"
    ),
    ("roofing", "US"): (
        "PRACTICE REFERENCE - United States, roofing (reviewed):\n"
        "- OSHA 29 CFR 1926.501: fall protection for construction work at 6 ft or more, including residential "
        "construction (1926.501(b)(13)) and low-slope roofs (guardrails, safety nets, personal fall arrest, or warning "
        "lines with a safety monitor).\n"
        "- IRC Chapter 9 (roof assemblies) as adopted locally, including underlayment, ice barrier in cold climates, and "
        "the shingle manufacturer's installation instructions and wind rating. Roofing licences are state- or "
        "city-specific (e.g. Illinois, Florida require a state roofing licence).\n"
    ),
    ("roofing", "CA"): (
        "PRACTICE REFERENCE - Canada, roofing (reviewed):\n"
        "- Provincial OHS: fall protection is required where a worker could fall 3 m or more (Ontario O. Reg. 213/91; BC "
        "and Alberta have similar 3 m rules); in Ontario, workers using fall protection need approved Working at Heights "
        "training (O. Reg. 297/13).\n"
        "- Roofing for houses: NBC / provincial Part 9, Section 9.26 (roofing), plus the manufacturer's instructions.\n"
    ),
    ("roofing", "IE"): (
        "PRACTICE REFERENCE - Ireland, roofing (reviewed):\n"
        "- Safety, Health and Welfare at Work (General Application) Regulations 2007, Part 4 Chapter 1 (work at height) "
        "and the Construction Regulations 2013: collective protection first. Building Regulations TGD (A, B, L) for "
        "the roof. CIRI registration as it becomes mandatory. Do NOT cite UK regulations.\n"
    ),
    ("roofing", "DE"): (
        "PRACTICE REFERENCE - Germany, roofing (reviewed):\n"
        "- Fall protection on construction sites from 2 m fall height (DGUV Vorschrift 38, ASR A2.1), collective "
        "protection first. Dachdecker is a Meister trade (Handwerksordnung). Work to the Fachregeln des Dachdeckerhandwerks "
        "(ZVDH). Do NOT cite UK or US rules.\n"
    ),
    ("roofing", "FR"): (
        "PRACTICE REFERENCE - France, roofing (reviewed):\n"
        "- Code du travail (travaux en hauteur): collective protection (garde-corps, filets) takes priority over harnesses. "
        "Roof coverings to the NF DTU 40 series (e.g. 40.21 tiles, 40.35 steel sheet); couvreur is a regulated activity "
        "(qualification professionnelle); decennale insurance. Do NOT cite UK or US rules.\n"
    ),
    ("roofing", "ES"): (
        "PRACTICE REFERENCE - Spain, roofing (reviewed):\n"
        "- RD 1627/1997 (construction sites) and RD 2177/2004 (work at height): collective protection first; the CTE DB "
        "HS 1 for roof waterproofing. The firm needs REA registration. Do NOT cite UK or US rules.\n"
    ),
    ("roofing", "IT"): (
        "PRACTICE REFERENCE - Italy, roofing (reviewed):\n"
        "- D.Lgs. 81/2008: work at height (lavori in quota) above 2 m needs fall protection, collective first. Many regions "
        "require permanent anchor devices (linee vita) on roofs when roofs are built or renovated - check the regional "
        "law. Firms on site need the patente a crediti. Do NOT cite UK or US rules.\n"
    ),
    ("roofing", "NL"): (
        "PRACTICE REFERENCE - Netherlands, roofing (reviewed):\n"
        "- Arbobesluit: measures against falling are required from 2.5 m fall height, collective protection first. The roof "
        "must meet the Bbl; no roofing licence is required. Do NOT cite UK or US rules.\n"
    ),
    # LL-25: fire protection, welding and concrete references for all 10 countries
    ("fire protection", "AU"): (
        "PRACTICE REFERENCE - Australia, fire protection (reviewed):\n"
        "- Standards (cite these names; give edition years only as shown): AS 1851-2012 (routine service of fire protection "
        "systems and equipment), AS 2118.1 (automatic sprinklers), AS 1670.1 (fire detection and alarm), AS 2444 "
        "(portable extinguishers and fire blankets), AS 3786 (smoke alarms). The NCC sets which buildings need which "
        "measures; houses need smoke alarms to AS 3786 (hard-wired and interconnected in new work).\n"
        "- Licensing varies by state: e.g. NSW requires accreditation under the Fire Protection Accreditation Scheme (FPAS) "
        "for assessing and certifying fire safety measures; Victoria licenses fire protection plumbing; Queensland QBCC "
        "licenses fire protection work. Name the state scheme and say to confirm the class.\n"
    ),
    ("fire protection", "UK"): (
        "PRACTICE REFERENCE - United Kingdom, fire protection (reviewed):\n"
        "- Law: Regulatory Reform (Fire Safety) Order 2005 in England and Wales (the 'responsible person' must carry out and "
        "keep a fire risk assessment and maintain fire safety measures), plus the Fire Safety (England) Regulations 2022 for "
        "multi-occupied residential buildings and the Building Safety Act 2022 for higher-risk buildings. Scotland and "
        "Northern Ireland have their own fire safety law - say so.\n"
        "- Building Regulations Approved Document B (fire safety). Standards: BS 5839-1 (non-domestic fire detection and "
        "alarm), BS 5839-6 (dwellings), BS EN 12845 (sprinklers), BS 9251 (residential sprinklers), BS 5306-3 "
        "(extinguisher servicing). Third-party certification (e.g. BAFE, LPCB) is good practice, not a legal licence.\n"
        "- Rented homes in England: smoke alarm on each storey and CO alarm with fixed combustion appliances (Smoke and Carbon "
        "Monoxide Alarm (Amendment) Regulations 2022).\n"
    ),
    ("fire protection", "US"): (
        "PRACTICE REFERENCE - United States, fire protection (reviewed):\n"
        "- Codes and standards as adopted by the state or city: IBC and IFC Chapter 9 (which buildings need sprinklers and "
        "alarms), NFPA 13 (sprinklers), NFPA 13R (low-rise residential), NFPA 13D (one- and two-family dwellings and "
        "manufactured homes), NFPA 72 (fire alarm), NFPA 25 (inspection, testing and maintenance of water-based systems), "
        "NFPA 10 (portable extinguishers). The IRC requires sprinklers in new one- and two-family homes (R313), but many "
        "states have removed that requirement - check the adopted code.\n"
        "- Licensing is state-specific (often the State Fire Marshal; e.g. California C-16 Fire Protection Contractor); "
        "NICET certification is commonly required for design and inspection.\n"
    ),
    ("fire protection", "CA"): (
        "PRACTICE REFERENCE - Canada, fire protection (reviewed):\n"
        "- National Building Code and National Fire Code as adopted by the province (Ontario: OBC and Ontario Fire Code). "
        "Fire alarm: CAN/ULC-S524 (installation), CAN/ULC-S537 (verification), CAN/ULC-S536 (inspection and testing). "
        "Sprinklers: NFPA 13 as referenced by the codes; inspection under NFPA 25.\n"
        "- Sprinkler and Fire Protection Installer (427A) is a compulsory trade in Ontario.\n"
    ),
    ("fire protection", "IE"): (
        "PRACTICE REFERENCE - Ireland, fire protection (reviewed):\n"
        "- Fire Services Acts 1981 and 2003; Building Regulations Part B (TGD B); a Fire Safety Certificate from the local "
        "building control authority is needed for most non-domestic buildings and apartments before work starts.\n"
        "- Standards: I.S. 3218 (fire detection and alarm), I.S. EN 12845 (sprinklers), I.S. 291 (portable extinguishers). "
        "Do NOT cite the UK Fire Safety Order or BS 5839.\n"
    ),
    ("fire protection", "DE"): (
        "PRACTICE REFERENCE - Germany, fire protection (reviewed):\n"
        "- Fire safety requirements come from the state building codes (Landesbauordnungen) and special building rules. "
        "Smoke alarms (Rauchwarnmelder) are compulsory in dwellings in every state, installed and maintained to DIN 14676. "
        "Fire alarm systems: DIN 14675 (firms certified for the phases they carry out) with DIN VDE 0833-2; sprinklers often "
        "to VdS CEA 4001; workplaces: ASR A2.2 (fire extinguishers). Do NOT cite UK or US codes.\n"
    ),
    ("fire protection", "FR"): (
        "PRACTICE REFERENCE - France, fire protection (reviewed):\n"
        "- Every dwelling must have at least one smoke alarm (DAAF) conforming to NF EN 14604 (compulsory since 8 March 2015). "
        "Public buildings (ERP): the reglement de securite incendie (arrete du 25 juin 1980 as amended); workplaces: Code du "
        "travail. Sprinklers and detection often follow the APSAD rules (CNPP), e.g. R1 sprinklers, R7 detection; systeme "
        "de securite incendie (SSI) standards NF S 61-970 series. Do NOT cite UK or US codes.\n"
    ),
    ("fire protection", "ES"): (
        "PRACTICE REFERENCE - Spain, fire protection (reviewed):\n"
        "- Reglamento de instalaciones de proteccion contra incendios (RIPCI, Real Decreto 513/2017): installation and "
        "maintenance only by enabled companies (empresas instaladoras / mantenedoras, declaracion responsable to the "
        "autonomous community), with set periodic maintenance and inspection. Building design: CTE DB SI. Do NOT cite UK or "
        "US codes.\n"
    ),
    ("fire protection", "IT"): (
        "PRACTICE REFERENCE - Italy, fire protection (reviewed):\n"
        "- DPR 151/2011 (activities subject to fire brigade checks, SCIA antincendio); DM 3 agosto 2015 (Codice di prevenzione "
        "incendi); DM 1 settembre 2021 (control and maintenance of fire systems): from 25 September 2026 the work must be "
        "done by a tecnico manutentore qualificato with the Vigili del Fuoco attestation. Detection UNI 9795, sprinklers "
        "UNI EN 12845; installation by an impresa abilitata under DM 37/2008. Do NOT cite UK or US codes.\n"
    ),
    ("fire protection", "NL"): (
        "PRACTICE REFERENCE - Netherlands, fire protection (reviewed):\n"
        "- Bbl (fire safety); smoke alarms are compulsory on every floor with a habitable room in all homes since 1 July 2022. "
        "Fire alarm systems NEN 2535, evacuation alarms NEN 2575; where the permit or Bbl requires it, a certified "
        "installation (CCV scheme). Do NOT cite UK or US codes.\n"
    ),
    ("welding", "AU"): (
        "PRACTICE REFERENCE - Australia, welding (reviewed):\n"
        "- Structural steel welding: AS/NZS 1554 series (e.g. Part 1 for structural steel), with welder and procedure "
        "qualification as that standard requires. Hot work: AS 1674.1 (fire precautions), with a hot work permit where the "
        "site requires it.\n"
        "- Welding fume is a Group 1 human carcinogen (IARC); control with local exhaust ventilation and suitable RPE under "
        "the WHS Regulations and Safe Work Australia's exposure standards (check the current figure for welding fumes).\n"
    ),
    ("welding", "UK"): (
        "PRACTICE REFERENCE - United Kingdom, welding (reviewed):\n"
        "- HSE (since 2019): all welding fume, including mild steel, is treated as a human carcinogen; general ventilation is "
        "not enough. Indoor welding needs engineering control, normally local exhaust ventilation (LEV); add suitable RPE "
        "where LEV does not adequately control exposure; outdoor welding needs RPE; this applies whatever the duration "
        "(COSHH). LEV must be maintained and thoroughly examined and tested.\n"
        "- Structural steelwork: BS EN 1090-2 (execution) with UKCA/CE marking requiring factory production control and a "
        "welding quality system to BS EN ISO 3834; welder qualification BS EN ISO 9606-1; procedures BS EN ISO 15614-1. Hot "
        "work permits on site.\n"
    ),
    ("welding", "US"): (
        "PRACTICE REFERENCE - United States, welding (reviewed):\n"
        "- OSHA: 29 CFR 1926 Subpart J (welding and cutting in construction) and 1910 Subpart Q (general industry); "
        "hexavalent chromium standard (1926.1126) for stainless and coated metals; fire prevention per NFPA 51B (hot work).\n"
        "- Structural welding: AWS D1.1 (steel), with welder and procedure qualification per the code; inspection by a "
        "qualified inspector (e.g. AWS CWI) as the building code and engineer require. Some cities license or certify "
        "structural welders - check locally.\n"
    ),
    ("welding", "CA"): (
        "PRACTICE REFERENCE - Canada, welding (reviewed):\n"
        "- Structural steel fabricators and erectors must be certified to CSA W47.1 (administered by the CWB), as required "
        "by CSA S16; welding to CSA W59. Welder is a compulsory trade in Alberta. Hot work and fume control under provincial "
        "OHS regulations.\n"
    ),
    ("welding", "IE"): (
        "PRACTICE REFERENCE - Ireland, welding (reviewed):\n"
        "- Structural steel: I.S. EN 1090-1/-2 with CE marking (factory production control, I.S. EN ISO 3834 welding quality), "
        "welder qualification I.S. EN ISO 9606-1, procedures EN ISO 15614-1. Welding fume is a carcinogen - control with "
        "LEV and RPE under the Safety, Health and Welfare at Work (Chemical Agents / Carcinogens) Regulations; hot work "
        "permits. Do NOT cite UK HSE as Irish law.\n"
    ),
    ("welding", "DE"): (
        "PRACTICE REFERENCE - Germany, welding (reviewed):\n"
        "- Structural steel: DIN EN 1090-1/-2 (CE marking with certified factory production control and welding coordinator "
        "per DIN EN ISO 14731), quality DIN EN ISO 3834, welder qualification DIN EN ISO 9606-1. Fume and exposure: TRGS 528 "
        "(Schweisstechnische Arbeiten) under the Gefahrstoffverordnung; hot work: DGUV rules and the site's "
        "Schweisserlaubnis. Do NOT cite UK or US rules.\n"
    ),
    ("welding", "FR"): (
        "PRACTICE REFERENCE - France, welding (reviewed):\n"
        "- Structural steel: NF EN 1090-1/-2 (CE marking), NF EN ISO 3834, welder qualification NF EN ISO 9606-1. Hot work "
        "needs a permis de feu; fume control and exposure limits under the Code du travail (INRS guidance). Do NOT cite UK "
        "or US rules.\n"
    ),
    ("welding", "ES"): (
        "PRACTICE REFERENCE - Spain, welding (reviewed):\n"
        "- Structural steel: UNE-EN 1090-1/-2 (CE marking), UNE-EN ISO 3834, welder qualification UNE-EN ISO 9606-1; the "
        "Codigo Estructural (RD 470/2021) for steel structures. Fume control under RD 374/2001 (chemical agents) and RD "
        "665/1997 (carcinogens); hot work permits. Do NOT cite UK or US rules.\n"
    ),
    ("welding", "IT"): (
        "PRACTICE REFERENCE - Italy, welding (reviewed):\n"
        "- Structural steel: UNI EN 1090-1/-2 (CE marking) and NTC 2018, UNI EN ISO 3834, welder qualification UNI EN ISO "
        "9606-1. Fume and carcinogen control under D.Lgs. 81/2008 (Titolo IX); hot work under the site safety plan. Do NOT "
        "cite UK or US rules.\n"
    ),
    ("welding", "NL"): (
        "PRACTICE REFERENCE - Netherlands, welding (reviewed):\n"
        "- Structural steel: NEN-EN 1090-1/-2 (CE marking), NEN-EN ISO 3834, welder qualification NEN-EN ISO 9606-1. Welding "
        "fume under the Arbobesluit with source extraction; hot work permit on site. Do NOT cite UK or US rules.\n"
    ),
    ("concrete", "AU"): (
        "PRACTICE REFERENCE - Australia, concrete (reviewed):\n"
        "- Standards: AS 3600 (concrete structures), AS 1379 (specification and supply of concrete), AS 2870 (residential "
        "slabs and footings), AS 3610 (formwork), AS 3850 (prefabricated concrete elements).\n"
        "- Silica: the workplace exposure standard for respirable crystalline silica is 0.05 mg/m3 (8-hour TWA). Engineered "
        "stone benchtops, panels and slabs have been banned from use (manufacture, supply, processing and installation) "
        "since 1 July 2024, with limited exceptions such as removal and repair; WHS Regulations set duties for other "
        "crystalline silica processes (cutting, grinding, drilling concrete): water suppression or on-tool extraction and "
        "RPE.\n"
    ),
    ("concrete", "UK"): (
        "PRACTICE REFERENCE - United Kingdom, concrete (reviewed):\n"
        "- Design: Eurocode 2 (BS EN 1992-1-1 with the UK National Annex); specification BS 8500 with BS EN 206; execution BS "
        "EN 13670.\n"
        "- Silica: the workplace exposure limit for respirable crystalline silica is 0.1 mg/m3 (8-hour TWA, EH40); control "
        "cutting, drilling and grinding with water suppression or on-tool extraction plus RPE under COSHH; CDM 2015 on site.\n"
    ),
    ("concrete", "US"): (
        "PRACTICE REFERENCE - United States, concrete (reviewed):\n"
        "- Design: ACI 318 (structural concrete, via the adopted IBC); ACI 301 (specifications); ACI 332 for residential "
        "concrete; IRC Chapter 4 foundations.\n"
        "- Silica: OSHA 1926.1153 - permissible exposure limit 50 micrograms per cubic metre (8-hour TWA), action level 25; "
        "employers may follow the Table 1 control methods (water or vacuum dust collection plus specified respirators) "
        "instead of exposure monitoring; written exposure control plan and competent person required.\n"
    ),
    ("concrete", "CA"): (
        "PRACTICE REFERENCE - Canada, concrete (reviewed):\n"
        "- CSA A23.1/A23.2 (concrete materials and construction methods), CSA A23.3 (design), and NBC Part 9 Section 9.3 for "
        "houses, as adopted by the province. Silica exposure limits are set by each province's OHS rules - name the "
        "province's limit rather than a US figure.\n"
    ),
    ("concrete", "IE"): (
        "PRACTICE REFERENCE - Ireland, concrete (reviewed):\n"
        "- I.S. EN 1992 (Eurocode 2) with the Irish National Annex; I.S. EN 206 with the Irish national annex. Respirable "
        "crystalline silica: EU binding limit 0.1 mg/m3 (Directive 2017/2398), applied through the Carcinogens Regulations "
        "and the HSA Code of Practice; dust control at source plus RPE. Do NOT cite UK EH40.\n"
    ),
    ("concrete", "DE"): (
        "PRACTICE REFERENCE - Germany, concrete (reviewed):\n"
        "- DIN EN 1992 with the German National Annex; DIN EN 206 with DIN 1045-2 (concrete), DIN 1045-3 (execution). "
        "Respirable crystalline silica under the Gefahrstoffverordnung and TRGS 559 (mineral dust); EU binding limit "
        "0.1 mg/m3 as the upper bound. Do NOT cite UK or US rules.\n"
    ),
    ("concrete", "FR"): (
        "PRACTICE REFERENCE - France, concrete (reviewed):\n"
        "- NF EN 1992 (Eurocode 2) with the French annex; NF EN 206/CN; execution NF EN 13670 and NF DTU 21. Silica exposure "
        "limits (VLEP) under the Code du travail - at most the EU binding limit of 0.1 mg/m3 for respirable crystalline "
        "silica. Do NOT cite UK or US rules.\n"
    ),
    ("concrete", "ES"): (
        "PRACTICE REFERENCE - Spain, concrete (reviewed):\n"
        "- The Codigo Estructural (Real Decreto 470/2021) replaced EHE-08 for concrete structures; UNE-EN 206. Silica under "
        "RD 665/1997 (carcinogens) - at most the EU binding limit of 0.1 mg/m3; check INSST's current national limit. Do NOT "
        "cite UK or US rules.\n"
    ),
    ("concrete", "IT"): (
        "PRACTICE REFERENCE - Italy, concrete (reviewed):\n"
        "- NTC 2018 (Norme Tecniche per le Costruzioni) and its Circolare, UNI EN 206 / UNI 11104. Respirable crystalline "
        "silica is a carcinogen under D.Lgs. 81/2008 Titolo IX with the EU binding limit of 0.1 mg/m3. Do NOT cite UK or US "
        "rules.\n"
    ),
    ("concrete", "NL"): (
        "PRACTICE REFERENCE - Netherlands, concrete (reviewed):\n"
        "- NEN-EN 1992 with the Dutch National Annex; NEN-EN 206 with NEN 8005. The Dutch legal limit for respirable quartz is "
        "0.075 mg/m3 (8-hour TWA) - lower than the EU 0.1 mg/m3. Do NOT cite UK or US rules.\n"
    ),
}

# LL-24: safety notes added to every country's electrical reference
_LL_PPE = {
    "AU": "- Safe work: AS/NZS 4836 (safe working on or near low-voltage installations) and state WHS/electrical safety laws; "
          "work de-energised; select PPE by risk assessment. NFPA 70E arc-flash 'categories' are a US concept - do not use them.\n"
          "- Automatic disconnection (AS/NZS 3000): 0.4 s for final subcircuits supplying socket-outlets, lighting and hand-held "
          "or portable equipment; 5 s is allowed for submains and certain fixed-equipment circuits - confirm the clause wording "
          "for the circuit. Read maximum Zs for the protective device from AS/NZS 3000 Table 8.1; do not quote a value unless "
          "it is given here.\n",
    "UK": "- Safe isolation: identify, isolate, secure (lock off), prove the voltage indicator, test dead on all conductors, "
          "re-prove (HSE GS38 for test equipment; Electricity at Work Regulations 1989; HSE HSG85). Work dead; live work only "
          "where Reg 14 conditions are met. Select PPE by risk assessment - NFPA 70E arc-flash categories are a US concept.\n"
          "- Clip spacing and similar figures in the On-Site Guide are guidance, not BS 7671 requirements.\n",
    "US": "- Electrical safe work practices: NFPA 70E (arc-flash risk assessment and PPE categories) and OSHA 1910.333 / "
          "1926.417 lockout/tagout.\n",
    "CA": "- Electrical safe work: CSA Z462 (workplace electrical safety, the Canadian arc-flash standard) and provincial OHS "
          "lockout rules.\n",
}
_LL_EU_ADS = ("- Automatic disconnection follows the national implementation of HD 60364-4-41 (TN, 230 V: 0.4 s for final circuits "
              "up to 63 A with socket-outlets and up to 32 A fixed equipment, 5 s for distribution circuits; TT: 0.2 s). Many "
              "dwellings in France, Spain and Italy are TT systems where shock protection relies on 30 mA RCDs. Work dead "
              "(EN 50110-1 safe isolation) and select PPE by risk assessment - NFPA 70E categories are a US concept.\n")
for _cc, _txt in _LL_PPE.items():
    if ("electrical", _cc) in _PRACTICE:
        _PRACTICE[("electrical", _cc)] += _txt
for _cc in ("IE", "DE", "FR", "ES", "IT", "NL"):
    if ("electrical", _cc) in _PRACTICE:
        _PRACTICE[("electrical", _cc)] += _LL_EU_ADS

# LL-9: EU countries map to their own practice references
_EU_COUNTRIES = {"IRELAND": "IE", "REPUBLIC OF IRELAND": "IE", "EIRE": "IE", "GERMANY": "DE", "DEUTSCHLAND": "DE",
                 "FRANCE": "FR", "SPAIN": "ES", "ESPANA": "ES", "ITALY": "IT", "ITALIA": "IT",
                 "NETHERLANDS": "NL", "THE NETHERLANDS": "NL", "NEDERLAND": "NL", "HOLLAND": "NL"}
# LL-8: Canadian provinces map to CA practice references
_CA_PROVINCES = {"ONTARIO", "BRITISH COLUMBIA", "ALBERTA", "QUEBEC", "MANITOBA", "SASKATCHEWAN", "NOVA SCOTIA",
                 "NEW BRUNSWICK", "NEWFOUNDLAND AND LABRADOR", "PRINCE EDWARD ISLAND", "CANADA", "ON", "BC", "AB"}

_AU_REGIONS = {"NSW", "VIC", "QLD", "WA", "SA", "TAS", "ACT", "NT", "AU", "AUSTRALIA", "NEW SOUTH WALES", "VICTORIA",
               "QUEENSLAND", "WESTERN AUSTRALIA", "SOUTH AUSTRALIA", "TASMANIA", "AUSTRALIAN CAPITAL TERRITORY",
               "NORTHERN TERRITORY"}
_UK_REGIONS = {"UK", "UNITED KINGDOM", "ENGLAND", "SCOTLAND", "WALES", "NORTHERN IRELAND", "GB", "GREAT BRITAIN", "LONDON"}
_US_STATES = {"ALABAMA", "ALASKA", "ARIZONA", "ARKANSAS", "CALIFORNIA", "COLORADO", "CONNECTICUT", "DELAWARE", "FLORIDA",
              "GEORGIA", "HAWAII", "IDAHO", "ILLINOIS", "INDIANA", "IOWA", "KANSAS", "KENTUCKY", "LOUISIANA", "MAINE",
              "MARYLAND", "MASSACHUSETTS", "MICHIGAN", "MINNESOTA", "MISSISSIPPI", "MISSOURI", "MONTANA", "NEBRASKA",
              "NEVADA", "NEW HAMPSHIRE", "NEW JERSEY", "NEW MEXICO", "NEW YORK", "NORTH CAROLINA", "NORTH DAKOTA", "OHIO",
              "OKLAHOMA", "OREGON", "PENNSYLVANIA", "RHODE ISLAND", "SOUTH CAROLINA", "SOUTH DAKOTA", "TENNESSEE", "TEXAS",
              "UTAH", "VERMONT", "VIRGINIA", "WASHINGTON", "WEST VIRGINIA", "WISCONSIN", "WYOMING", "DISTRICT OF COLUMBIA",
              "DC", "US", "USA", "UNITED STATES", "WASHINGTON DC", "NYC", "NEW YORK CITY"}


def _region_country(region: str) -> str:
    r = " ".join((region or "").replace(",", " ").split()).upper()
    if r in _AU_REGIONS or r.endswith(" AUSTRALIA"):
        return "AU"
    if r in _UK_REGIONS:
        return "UK"
    if r in _US_STATES or r.endswith(" USA") or r.endswith(" US"):
        return "US"
    if r in _CA_PROVINCES or r.endswith(" CANADA"):
        return "CA"
    return _EU_COUNTRIES.get(r, "")


# LL-24: verified calculations computed in code (not by the AI) and injected as authoritative data
_UK_CURVE_K = {"B": 5, "C": 10, "D": 20}
# BS 7671 Table 4D5 (70 C thermoplastic flat twin and earth): size -> (mV/A/m, Iz Method C clipped direct, Iz Method 100)
_UK_TE = {1.0: (44, 16, 13), 1.5: (29, 20, 16), 2.5: (18, 27, 21), 4.0: (11, 37, 27), 6.0: (7.3, 47, 34),
          10.0: (4.4, 64, 45), 16.0: (2.8, 85, 57)}
# NEC Table 310.16 copper ampacity (60 C, 75 C, 90 C) and circular mils
_US_AMP = {"14": (15, 20, 25), "12": (20, 25, 30), "10": (30, 35, 40), "8": (40, 50, 55), "6": (55, 65, 75),
           "4": (70, 85, 95), "3": (85, 100, 115), "2": (95, 115, 130), "1": (110, 130, 145),
           "1/0": (125, 150, 170), "2/0": (145, 175, 195), "3/0": (165, 200, 225), "4/0": (195, 230, 260)}
_US_CM = {"14": 4110, "12": 6530, "10": 10380, "8": 16510, "6": 26240, "4": 41740, "3": 52620, "2": 66360,
          "1": 83690, "1/0": 105600, "2/0": 133100, "3/0": 167800, "4/0": 211600}
_US_240_4D = {"14": 15, "12": 20, "10": 30}


def _ll_num(s):
    try:
        return float(s)
    except Exception:
        return None


def _ll_find_breaker(q):
    m = re.search(r"\b([BCD])\s?-?\s?(\d{1,3})\s?A?\b", q)
    if m and int(m.group(2)) in (6, 10, 16, 20, 25, 32, 40, 50, 63, 80, 100, 125):
        return m.group(1).upper(), int(m.group(2))
    m = re.search(r"\b(\d{1,3})\s?A\b[^.;\n]{0,25}?\b(?:type|curve)\s?([BCD])\b", q, re.I)
    if m:
        return m.group(2).upper(), int(m.group(1))
    m = re.search(r"\b(?:type|curve)\s?([BCD])\b[^.;\n]{0,25}?\b(\d{1,3})\s?A\b", q, re.I)
    if m:
        return m.group(1).upper(), int(m.group(2))
    return None


def _ll_calc_uk(q):
    out = []
    ql = q.lower()
    br = _ll_find_breaker(q)
    zs_m = re.search(r"(?:zs|loop impedance|earth fault loop)[^0-9]{0,40}?(\d+(?:\.\d+)?)\s*(?:ohm|Ω)", q, re.I)
    zs = _ll_num(zs_m.group(1)) if zs_m else None
    rcd_m = re.search(r"\b(10|30|100|300|500)\s?mA\b", q, re.I)
    mm = re.search(r"(\d+(?:\.\d+)?)\s?mm(?:2|²|\^2| sq)", q, re.I)
    size = _ll_num(mm.group(1)) if mm else None
    sockets = bool(re.search(r"socket|outlet|ring|radial", ql))
    if br:
        curve, In = br
        zmax = "%.2f" % (230 * 0.95 / (_UK_CURVE_K[curve] * In))
        z80 = "%.2f" % (float(zmax) * 0.8)
        out.append(f"- Device: Type {curve} {In} A circuit-breaker (BS EN 60898).")
        out.append(f"- Maximum Zs at 0.4 s, BS 7671 Table 41.3 (Uo 230 V, Cmin 0.95): {zmax} ohm"
                   + (" (Type B and C values are the same for 5 s)." if curve in "BC" else "."))
        out.append(f"- Rule-of-thumb for readings taken at ambient temperature (IET GN3, 80%): {z80} ohm. A reading at or "
                   f"below this passes; between {z80} and {zmax} ohm the rule cannot confirm compliance and the "
                   f"Appendix 3 temperature correction (Reg 411.4.203) must be applied; above {zmax} ohm fails.")
        if In <= 63:
            out.append("- Disconnection time in a TN system, Uo 230 V (Reg 411.3.2.2, Table 41.1): 0.4 s for final circuits "
                       "up to 63 A with socket-outlets and up to 32 A supplying only fixed equipment. 5 s applies only to "
                       "distribution circuits and to final circuits outside those limits (Reg 411.3.2.3). A circuit "
                       "supplying socket-outlets IS a final circuit - do not apply 5 s to it.")
        if zs is not None:
            if zs <= float(z80):
                v = f"PASS - {zs} ohm is at or below the 80% figure ({z80} ohm)."
            elif zs <= float(zmax):
                v = (f"BORDERLINE - {zs} ohm is below the tabulated {zmax} ohm but above the 80% figure ({z80} ohm): "
                     "cannot be confirmed without temperature correction (Reg 411.4.203 / Appendix 3).")
            else:
                v = f"FAIL - {zs} ohm exceeds the tabulated maximum of {zmax} ohm for the circuit-breaker."
            out.append("- Measured Zs check against the circuit-breaker: " + v)
    if rcd_m:
        ma = int(rcd_m.group(1))
        out.append(f"- RCD {ma} mA: where the RCD is relied on for fault protection (Reg 411.4.5 in TN; 411.5.3 in TT), "
                   f"maximum Zs = 50 V / IΔn = {round(50 / (ma / 1000))} ohm (Table 41.5); values above 200 ohm may not "
                   "be stable. A 30 mA RCD also gives the additional protection required for socket-outlets up to 32 A "
                   "(Reg 411.3.3) and for cables concealed in walls less than 50 mm deep (Reg 522.6.202).")
        out.append("- RCD testing since BS 7671 A2:2022 (Reg 643.7.3): one test at the rated residual current IΔn; a "
                   "general (non-delayed) RCD must disconnect within 300 ms (S-type 130-500 ms). The 0.5x, 5x and 180-degree "
                   "tests are no longer required by BS 7671 (optional good practice only). Use the test button for the "
                   "functional check.")
        out.append("- RCD types: AC (sinusoidal residual current only - limited by Reg 531.3.3 to fixed equipment known to "
                   "produce no DC component), A (AC plus pulsating DC), F (A plus mixed-frequency, for single-phase "
                   "inverter loads), B (adds smooth DC - e.g. some EV chargers, three-phase drives, PV). Choose by the "
                   "equipment's residual-current characteristics and the manufacturer's instructions.")
    if size in _UK_TE:
        mv, izc, iz100 = _UK_TE[size]
        out.append(f"- {size:g} mm2 flat twin and earth, BS 7671 Table 4D5: Iz {izc} A clipped direct (Reference Method C), "
                   f"{iz100} A above a plasterboard ceiling with insulation up to 100 mm (Method 100); lower again in "
                   f"thermal insulation (Methods 101-103). Voltage drop {mv} mV/A/m. Correction factors for ambient "
                   "temperature, grouping and insulation apply (Appendix 4).")
        if br:
            In = br[1]
            out.append(f"- Overload rule Ib <= In <= Iz (Reg 433.1.1): a radial circuit on {In} A needs Iz of at least {In} A "
                       "after correction factors.")
            if size == 2.5 and In in (30, 32):
                out.append("- 2.5 mm2 on 32 A: COMPLIANT as a ring final circuit (BS 7671 Appendix 15 / On-Site Guide: "
                           "2.5 mm2 ring on 30/32 A, ring cable Iz at least 20 A). NOT compliant as a radial: 2.5 mm2 twin "
                           "and earth carries 27 A at most even clipped direct, below 32 A (use 4 mm2 for a 32 A radial, or "
                           "2.5 mm2 radials on 20 A, per On-Site Guide). If ring or radial is not stated, give both answers.")
            elif In > izc:
                out.append(f"- {size:g} mm2 twin and earth (max Iz {izc} A, Method C) cannot be protected against overload "
                           f"by a {In} A device as a radial circuit.")
        amps = re.search(r"(\d+(?:\.\d+)?)\s?A\s*(?:load|design current|demand|draw)", q, re.I)
        dist = re.search(r"(\d+(?:\.\d+)?)\s?(?:m|metres|meters)\b(?!m)", q, re.I)
        if amps and dist:
            ib, L = float(amps.group(1)), float(dist.group(1))
            vd = round(mv * ib * L / 1000, 2)
            out.append(f"- Voltage drop: {mv} x {ib:g} A x {L:g} m / 1000 = {vd} V ({round(vd / 230 * 100, 1)}% of 230 V). "
                       "Limits from a public supply (Appendix 4, Table 4Ab): 3% (6.9 V) lighting, 5% (11.5 V) other uses.")
    if sockets and not br and not rcd_m and size is None:
        return []
    if out:
        out.append("- Safe isolation (HSE GS38 test equipment, Electricity at Work Regulations 1989 Reg 12-14): identify the "
                   "circuit, isolate, secure/lock off, prove the voltage indicator on a known source, test dead on all "
                   "conductors, re-prove the indicator. Work dead; live testing only where unavoidable and justified.")
    return out


def _ll_calc_us(q):
    out = []
    awg = re.search(r"\b(14|12|10|8|6|4|3|2|1|1/0|2/0|3/0|4/0)\s?(?:AWG|ga(?:uge)?)\b", q, re.I)
    brk = re.search(r"\b(15|20|25|30|35|40|45|50|60|70|80|90|100|110|125|150|175|200)\s?-?\s?(?:A|amp|amps)\b", q, re.I)
    if awg:
        g = awg.group(1)
        a60, a75, a90 = _US_AMP[g]
        out.append(f"- {g} AWG copper, NEC Table 310.16: {a60} A (60 C column), {a75} A (75 C), {a90} A (90 C). NM-B cable "
                   "is limited to the 60 C column (334.80); terminations usually limit to 60 C or 75 C (110.14(C)).")
        if g in _US_240_4D:
            out.append(f"- NEC 240.4(D): {g} AWG copper maximum overcurrent protection {_US_240_4D[g]} A (unless a specific "
                       "exception applies, e.g. motor circuits).")
            if brk:
                b = int(brk.group(1))
                verdict = "COMPLIANT" if b <= _US_240_4D[g] else "NON_COMPLIANT"
                out.append(f"- {g} AWG on a {b} A breaker: {verdict} under 240.4(D) for a general branch circuit.")
        amps = re.search(r"(\d+(?:\.\d+)?)\s?A\s*(?:load|draw|demand)", q, re.I)
        ft = re.search(r"(\d+(?:\.\d+)?)\s?(?:ft|feet|foot)\b", q, re.I)
        volts = re.search(r"\b(120|208|240|277|480)\s?V\b", q, re.I)
        if amps and ft:
            i, L = float(amps.group(1)), float(ft.group(1))
            vd = round(2 * 12.9 * i * L / _US_CM[g], 2)
            v = int(volts.group(1)) if volts else 120
            out.append(f"- Voltage drop (single-phase, K = 12.9 for copper): 2 x 12.9 x {i:g} A x {L:g} ft / {_US_CM[g]} cmil "
                       f"= {vd} V ({round(vd / v * 100, 1)}% of {v} V). NEC informational notes suggest 3% branch / 5% total "
                       "(a recommendation, not a requirement).")
    return out


def _ll_verified_calcs(user_message: str) -> str:
    """Numbers computed by code from published tables, injected as authoritative data for the AI to use exactly."""
    try:
        m = re.match(r"\s*Trade:\s*([^|\n]+?)\s*\|\s*Region:\s*([^|\n]+)", user_message or "")
        if not m or m.group(1).strip().lower() != "electrical":
            return ""
        country = _region_country(m.group(2))
        if country == "UK":
            lines = _ll_calc_uk(user_message)
            src = "BS 7671:2018+A4:2026"
        elif country == "US":
            lines = _ll_calc_us(user_message)
            src = "the NEC (NFPA 70)"
        else:
            return ""
        if not lines:
            return ""
        return ("\n\nVERIFIED CALCULATIONS (computed by Legis-Link code from " + src + " tables - use these numbers "
                "exactly, never substitute other values; state any assumption you add):\n" + "\n".join(lines))
    except Exception as e:
        logging.warning(f"verified calcs failed: {e}")
        return ""


def _practice_for(user_message: str) -> str:
    """Answer rules for every tool, plus the reviewed practice reference for the trade and country."""
    m = re.match(r"\s*Trade:\s*([^|\n]+?)\s*\|\s*Region:\s*([^|\n]+)", user_message or "")
    extra = ""
    if m:
        t = m.group(1).strip().lower()
        ref = _PRACTICE.get((_TRADE_ALIAS.get(t, t), _region_country(m.group(2))))
        if ref:
            extra = "\n\n" + ref
    return _ANSWER_RULES + extra + _ll_verified_calcs(user_message)


async def ask_claude(system_prompt: str, user_message: str) -> dict:
    """LL-24: one place to finalise every AI answer (documents are drafts, never 'compliant')."""
    result = await _ask_claude_raw(system_prompt, user_message)
    if isinstance(result, dict) and ("You are a construction RAMS expert" in system_prompt
                                     or "Generate a numbered safety checklist" in system_prompt):
        if result.get("status") not in ("ERROR",):
            result["status"] = "DRAFT"
    return result


# LL-26: stronger models need more time and room. Long timeout, larger output for documents, one retry on
# overload/rate-limit/timeout, then the fallback model, so users get an answer instead of an ERROR.
FALLBACK_MODEL = os.environ.get("LEGIS_FALLBACK_MODEL", "") or "claude-haiku-4-5-20251001"


async def _ask_claude_raw(system_prompt: str, user_message: str) -> dict:
    """Call Anthropic API with retry and model fallback. Falls back to OpenAI on billing errors."""
    import asyncio as _aio
    system_prompt = system_prompt + _practice_for(user_message)
    is_doc = ("You are a construction RAMS expert" in system_prompt
              or "Generate a numbered safety checklist" in system_prompt
              or "inspection" in system_prompt[:400].lower())
    attempts = [(MODEL, 0), (MODEL, 4)]
    if FALLBACK_MODEL and FALLBACK_MODEL != MODEL:
        attempts.append((FALLBACK_MODEL, 0))
    last = {"status": "ERROR", "result": "The answer service did not respond. Please try again.", "code_reference": ""}
    for model_name, wait in attempts:
        if wait:
            await _aio.sleep(wait)
        result = await _ask_claude_once(system_prompt, user_message, model_name, 8000 if is_doc else 4096)
        if result.get("status") != "ERROR" or result.get("_final"):
            result.pop("_retry", None)
            result.pop("_final", None)
            return result
        logging.warning(f"AI call failed on {model_name}: {str(result.get('result'))[:160]}")
        last = result
    last.pop("_retry", None)
    last.pop("_final", None)
    return last


async def _ask_claude_once(system_prompt: str, user_message: str, model_name: str, max_tokens: int) -> dict:
    async with httpx.AsyncClient(timeout=180.0) as client:
        try:
            resp = await client.post(
                ANTHROPIC_URL,
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": model_name,
                    "max_tokens": max_tokens,
                    "system": system_prompt,
                    "messages": [{"role": "user", "content": user_message}],
                }
            )
            if resp.status_code == 200:
                raw_text = resp.json()["content"][0]["text"]
                return _parse_llm_text(raw_text)

            error_body = resp.text
            # Billing error — fall back to OpenAI
            if resp.status_code in (400, 402) and (
                "credit balance" in error_body or "billing" in error_body.lower()
            ):
                logging.warning("Anthropic credits exhausted — falling back to OpenAI")
                r = await _ask_openai(system_prompt, user_message)
                r["_final"] = True
                return r

            # 400/401/403 (bad request, key or model name) will not improve by retrying the same model
            return {"status": "ERROR",
                    "result": f"API error {resp.status_code}: {error_body[:200]}",
                    "code_reference": ""}

        except httpx.TimeoutException:
            return {"status": "ERROR", "result": "Request timed out.", "code_reference": ""}
        except Exception as e:
            return {"status": "ERROR", "result": f"Error: {e}", "code_reference": ""}


def format_response(result: dict, header: str, footer_link: str) -> str:
    status   = result.get("status", "")
    answer   = result.get("result", "")
    code_ref = result.get("code_reference", "")
    text = f"**{header}**\n\n{answer}"
    if code_ref:
        text += f"\n\n*Code reference: {code_ref}*"
    if status == "NON_COMPLIANT":
        text += "\n\n⚠️ **Non-compliant** — see answer above for the correct alternative."
    elif status == "REQUIRES_VERIFICATION":
        text += "\n\n⚠️ **Requires verification** — confirm with local authority before proceeding."
    text += f"\n\n[{footer_link}]"
    return text


def auth_error(reason: str) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=(
        f"**Authentication Required**\n\n{reason}\n\n"
        f"Get your free API key at: {PRO_UPGRADE.replace('upgrade', '')}\n"
        f"Free tier: 50 requests/day | Pro: $199/year, 1000 requests/day"
    ))]


def rate_limit_error(result: dict, tier: str) -> list[types.TextContent]:
    msg = (
        f"**Daily limit reached ({result['limit']} requests)**\n\n"
        f"Your {tier} tier limit resets {result['reset']}.\n"
    )
    if tier == "free":
        msg += f"\nUpgrade to Pro for 1000 requests/day: {PRO_UPGRADE}"
    return [types.TextContent(type="text", text=msg)]


def pro_required_error() -> list[types.TextContent]:
    return [types.TextContent(type="text", text=(
        f"**Pro Feature**\n\n"
        f"This tool requires a Pro subscription ($199/year).\n"
        f"Includes: cable sizing, RAMS generation, safety checklists, "
        f"material compliance, inspection requirements.\n\n"
        f"Upgrade: {PRO_UPGRADE}"
    ))]


# ── Tool handlers ────────────────────────────────────────────────────────────

# LL-13: every tool is labelled read-only with a clear display title, so clients such as Claude
# can group them as read-only and users are not asked to approve harmless look-ups.
_LL_TOOL_TITLES = {
    "check_compliance": "Check compliance",
    "get_code_reference": "Look up a code or standard",
    "list_supported_regions": "List supported regions",
    "calculate_technical_spec": "Calculate a technical spec (Pro)",
    "generate_safety_checklist": "Generate a safety checklist (Pro)",
    "generate_rams": "Generate RAMS (Pro)",
    "verify_material_compliance": "Verify material compliance (Pro)",
    "get_inspection_requirements": "Get inspection requirements (Pro)",
}


def _ll_tool(t):
    title = _LL_TOOL_TITLES.get(t["name"], t["name"].replace("_", " ").capitalize())
    kw = dict(name=t["name"], description=t["description"], inputSchema=t["inputSchema"])
    ann_cls = getattr(types, "ToolAnnotations", None)
    if ann_cls is not None:
        kw["annotations"] = ann_cls(title=title, readOnlyHint=True, destructiveHint=False,
                                    idempotentHint=True, openWorldHint=False)
    try:
        return types.Tool(title=title, **kw)
    except Exception:
        return types.Tool(**kw)


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [_ll_tool(t) for t in SERVER_CARD["tools"]]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:

    # ── Auth ──────────────────────────────────────────────────────────────────
    api_key = arguments.get("api_key", "") or _LL_KEY.get() or os.environ.get("LEGIS_LINK_API_KEY", "")
    auth    = validate_api_key(api_key)
    if not auth["valid"]:
        audit_log(api_key or "none", "none", name, "", "", "AUTH_FAIL")
        return auth_error(auth["reason"])

    tier = auth["tier"]

    # ── Pro tool gate ─────────────────────────────────────────────────────────
    if is_pro_tool(name) and tier != "pro":
        audit_log(api_key, tier, name, "", "", "PRO_REQUIRED")
        return pro_required_error()

    # ── Rate limit ────────────────────────────────────────────────────────────
    rate = check_rate_limit(api_key, tier)
    if not rate["allowed"]:
        audit_log(api_key, tier, name, "", "", "RATE_LIMITED")
        return rate_limit_error(rate, tier)

    # ── Extract common args ───────────────────────────────────────────────────
    trade  = arguments.get("trade", "")
    region = arguments.get("region", "")
    role   = arguments.get("role", "Qualified Tradesperson")

    # ── FREE TOOLS ────────────────────────────────────────────────────────────

    if name == "check_compliance":
        question = arguments.get("question", "")
        user_msg = f"Trade: {trade} | Region: {region} | Role: {role}\nQuestion: {question}"
        user_msg += _registry_ground_multi(trade, region, question)
        result   = await ask_claude(SYSTEM_PROMPTS["compliance"], user_msg)
        audit_log(api_key, tier, name, trade, region, result.get("status","OK"))
        return [types.TextContent(type="text", text=format_response(
            result, f"{trade} Compliance — {region}", PRO_UPGRADE.split('?')[0]))]

    if name == "get_code_reference":
        topic    = arguments.get("topic", "")
        user_msg = f"Trade: {trade} | Region: {region}\nCode reference for: {topic}"
        result   = await ask_claude(SYSTEM_PROMPTS["compliance"], user_msg)
        audit_log(api_key, tier, name, trade, region, result.get("status","OK"))
        return [types.TextContent(type="text", text=format_response(
            result, f"Code Reference: {topic}", PRO_UPGRADE.split('?')[0]))]

    if name == "list_supported_regions":
        lines = [f"**Supported regions for {trade}:**\n"]
        for country, regions in VALID_REGIONS.items():
            lines.append(f"**{country}:** {', '.join(regions)}")
        lines.append(f"\nPro tools available for all regions: {PRO_UPGRADE}")
        audit_log(api_key, tier, name, trade, "", "OK")
        return [types.TextContent(type="text", text="\n".join(lines))]

    # ── PRO TOOLS ─────────────────────────────────────────────────────────────

    if name == "calculate_technical_spec":
        calculation = arguments.get("calculation", "")
        user_msg = (f"Trade: {trade} | Region: {region} | Role: {role}\n"
                    f"Calculate: {calculation}")
        result = await ask_claude(SYSTEM_PROMPTS["calculation"], user_msg)
        audit_log(api_key, tier, name, trade, region, result.get("status","OK"))
        return [types.TextContent(type="text", text=format_response(
            result, f"Technical Calculation — {trade} / {region}", PRO_UPGRADE))]

    if name == "generate_safety_checklist":
        task     = arguments.get("task", "")
        user_msg = (f"Trade: {trade} | Region: {region} | Role: {role}\n"
                    f"Safety checklist for: {task}")
        result = await ask_claude(SYSTEM_PROMPTS["safety"], user_msg)
        audit_log(api_key, tier, name, trade, region, result.get("status","OK"))
        return [types.TextContent(type="text", text=format_response(
            result, f"Safety Checklist — {task}", PRO_UPGRADE))]

    if name == "generate_rams":
        task         = arguments.get("task", "")
        company_name = arguments.get("company_name", "")
        site_address = arguments.get("site_address", "")
        header_info  = f"Company: {company_name}. " if company_name else ""
        header_info += f"Site: {site_address}. " if site_address else ""
        user_msg = (f"Trade: {trade} | Region: {region} | Role: {role}\n"
                    f"{header_info}Generate RAMS for: {task}")
        result = await ask_claude(SYSTEM_PROMPTS["rams"], user_msg)
        audit_log(api_key, tier, name, trade, region, result.get("status","OK"))
        title = f"RAMS — {task} | {trade} / {region}"
        if company_name:
            title += f" | {company_name}"
        return [types.TextContent(type="text", text=format_response(
            result, title, PRO_UPGRADE))]

    if name == "verify_material_compliance":
        material = arguments.get("material", "")
        use_case = arguments.get("use_case", "standard installation")
        user_msg = (f"Trade: {trade} | Region: {region} | Role: {role}\n"
                    f"Material: {material}\nUse case: {use_case}")
        result = await ask_claude(SYSTEM_PROMPTS["material"], user_msg)
        audit_log(api_key, tier, name, trade, region, result.get("status","OK"))
        return [types.TextContent(type="text", text=format_response(
            result, f"Material Compliance — {material}", PRO_UPGRADE))]

    if name == "get_inspection_requirements":
        installation = arguments.get("installation", "")
        user_msg = (f"Trade: {trade} | Region: {region} | Role: {role}\n"
                    f"Inspection requirements for: {installation}")
        result = await ask_claude(SYSTEM_PROMPTS["inspection"], user_msg)
        audit_log(api_key, tier, name, trade, region, result.get("status","OK"))
        return [types.TextContent(type="text", text=format_response(
            result, f"Inspection Requirements — {installation}", PRO_UPGRADE))]

    return [types.TextContent(type="text", text=f"Unknown tool: {name}")]


# ── HTTP server ──────────────────────────────────────────────────────────────

# ── Daily Toolbox Talk Data ────────────────────────────────────────────────
import datetime as _dt

TOOLBOX_TOPICS = {
    "Electrical": [
        {"title":"Working safely on live electrical panels","objective":"Ensure all crew understand isolation and LOTO procedures before any panel work.","hazards":[{"risk":"CRITICAL","text":"Electric shock from live conductors","ctrl":"Isolate, test with voltage detector, apply LOTO before touching"},{"risk":"CRITICAL","text":"Arc flash from short circuit","ctrl":"Arc-rated PPE minimum Category 1, maintain safe approach distances"},{"risk":"HIGH","text":"Wrong glove rating","ctrl":"Class 00 insulated gloves rated to 500V AC, inspect before use"}],"checklist":["Isolation point identified and accessible?","LOTO devices available for all workers?","Voltage detector tested and within calibration?","Arc-rated PPE available?","Emergency procedure and first aider location known?"],"discussion":"A colleague says the panel is already isolated. What do you do before touching any conductors?","ref":"AS/NZS 3000:2018 Cl.2.6; WHS Act 2011 s.36; AS/NZS 4836:2011"},
        {"title":"Cable sizing — verify before you pull","objective":"Crew understands voltage drop limits and how to verify correct cable sizing on site.","hazards":[{"risk":"HIGH","text":"Cable overheating from undersized conductor","ctrl":"Verify cable size against AS/NZS 3008 tables — never guess"},{"risk":"HIGH","text":"Voltage drop exceeding 5% limit","ctrl":"Calculate VD before pulling cable"},{"risk":"MED","text":"Rework cost and programme delay","ctrl":"Check once before ordering, not after installation"}],"checklist":["Circuit design load confirmed?","Cable size calculated — not assumed?","Voltage drop verified within limits?","Derating factors applied for conduit/bundling?","Cable matches drawing specification?"],"discussion":"What is the maximum voltage drop for a final sub-circuit in NSW?","ref":"AS/NZS 3008.1.1:2017; AS/NZS 3000:2018 Cl.3.6.2"},
        {"title":"RCD testing and requirements","objective":"All crew know which circuits require RCD protection and how to test correctly.","hazards":[{"risk":"HIGH","text":"Unprotected socket outlets","ctrl":"All socket outlets in residential must have 30mA RCD protection"},{"risk":"MED","text":"RCD not tested — false sense of security","ctrl":"Test RCD with test button monthly, trip current annually by licensed tester"},{"risk":"MED","text":"Wrong RCD type selected","ctrl":"Type A for circuits with electronic equipment, Type AC for general use"}],"checklist":["RCD protection confirmed on all socket outlet circuits?","RCD test button functional?","Last test date within 12 months?","RCD rated correctly for circuit load?","Test results documented?"],"discussion":"Which circuits in a residential installation always require RCD protection?","ref":"AS/NZS 3000:2018 Cl.2.6.3; AS/NZS 61008"},
        {"title":"Working at height — electrical installation","objective":"Every crew member knows fall protection requirements for electrical work above 2m.","hazards":[{"risk":"HIGH","text":"Fall from ladder or elevated platform","ctrl":"3-point contact on ladder, edge protection or harness above 2m"},{"risk":"HIGH","text":"Live conductors near work platform","ctrl":"Maintain minimum approach distance, consider de-energising adjacent circuits"},{"risk":"MED","text":"Tools and materials falling to ground","ctrl":"Exclusion zone below, tool lanyards, secured materials"}],"checklist":["Fall protection in place before ascending?","Adjacent live conductors identified?","Exclusion zone barricaded at ground level?","Harness and anchor point inspected?","Rescue plan established?"],"discussion":"What is the minimum height at which fall protection is mandatory in NSW?","ref":"WHS Act 2011 s.36; AS/NZS 1891.1:2020; Safe Work Australia Working at Height COP"},
        {"title":"Switchboard labelling requirements","objective":"Crew understands mandatory labelling requirements before energising any switchboard.","hazards":[{"risk":"HIGH","text":"Incorrect circuit identification — wrong circuit isolated","ctrl":"Label every circuit clearly before energising — never leave blanks"},{"risk":"MED","text":"Missing danger/warning labels","ctrl":"AS/NZS 3000 labels mandatory on all switchboards"},{"risk":"MED","text":"Unlabelled circuits create ongoing safety risk","ctrl":"Document circuit schedule and store in switchboard door pocket"}],"checklist":["All circuits labelled clearly?","Danger/warning labels fitted?","Circuit schedule completed and inserted?","Arc flash label fitted if required?","Switchboard door latch functional?"],"discussion":"What information must appear on a circuit breaker label under AS/NZS 3000?","ref":"AS/NZS 3000:2018 Cl.5.3; AS/NZS 1319:1994"},
        {"title":"Testing and inspection before energising","objective":"All crew understand mandatory tests required before a new installation is energised.","hazards":[{"risk":"CRITICAL","text":"Energising a faulty installation","ctrl":"Complete all required tests before energising — no shortcuts"},{"risk":"HIGH","text":"Missing earth continuity","ctrl":"Earth continuity test required on all circuits before energising"},{"risk":"HIGH","text":"Insulation breakdown","ctrl":"Insulation resistance test at 500V DC — minimum 1 MOhm"}],"checklist":["Continuity of protective conductors tested?","Insulation resistance tested and results recorded?","Polarity verified correct?","Earth fault loop impedance tested?","RCD operation tested?"],"discussion":"What is the minimum acceptable insulation resistance for a 240V circuit?","ref":"AS/NZS 3000:2018 Section 8; AS/NZS 3017:2022"},
        {"title":"Confined space electrical work","objective":"Crew understands additional requirements when performing electrical work in confined spaces.","hazards":[{"risk":"CRITICAL","text":"Electrocution risk increased in confined space","ctrl":"Reduced voltage tools (25V) or RCD protection mandatory in confined spaces"},{"risk":"CRITICAL","text":"Oxygen deficiency or toxic atmosphere","ctrl":"Atmospheric testing before entry, continuous monitoring during work"},{"risk":"HIGH","text":"No rescue access","ctrl":"Standby person mandatory, rescue plan established before entry"}],"checklist":["Confined space entry permit obtained?","Atmospheric test completed and documented?","Standby person nominated and briefed?","Rescue equipment available?","Reduced voltage or RCD protection in place?"],"discussion":"What voltage is considered safe for portable tools used in a confined space?","ref":"AS 2865:2009; WHS Regulation 2017 Ch.4; AS/NZS 3000:2018"},
    ],
    "Plumbing": [
        {"title":"Backflow prevention — protecting the water supply","objective":"Every crew member understands when backflow prevention is required and correct device type.","hazards":[{"risk":"HIGH","text":"Drinking water contamination","ctrl":"Install appropriate backflow device for hazard level — RPZ for high hazard"},{"risk":"HIGH","text":"Wrong device type installed","ctrl":"Know the difference: RPZ for high hazard, DCV for low hazard"},{"risk":"MED","text":"Device installed incorrectly","ctrl":"RPZ horizontal, 300mm above floor, accessible for annual testing"}],"checklist":["Cross-connection risk assessed?","Correct backflow device specified?","Device rated for working pressure?","Test point accessible?","Annual testing requirement communicated to client?"],"discussion":"Name one situation on this job where backflow prevention is required.","ref":"AS/NZS 3500.1:2021 Sec.4; AS 2845.1:2010"},
        {"title":"Hot water system compliance","objective":"All crew know mandatory temperature requirements for hot water systems.","hazards":[{"risk":"HIGH","text":"Legionella growth from incorrect storage temperature","ctrl":"Store at minimum 60 degrees C — test with calibrated thermometer"},{"risk":"HIGH","text":"Scalding from delivery temperature too high","ctrl":"Thermostatic mixing valve mandatory — max 50 degrees C delivery"},{"risk":"MED","text":"TMV not commissioned correctly","ctrl":"Commission and test TMV to AS 4032.3 before handover"}],"checklist":["Storage temperature verified at 60 degrees C minimum?","TMV installed on bathroom circuits?","Delivery temperature tested below 50 degrees C?","Expansion relief valve installed?","Pressure set correctly?"],"discussion":"What is the minimum storage temperature for a hot water system and why?","ref":"AS/NZS 3500.4:2021 Cl.2.3; AS 4032.3:2004"},
    ],
    "HVAC": [
        {"title":"Refrigerant handling — legal requirements","objective":"All crew handling refrigerants have valid ARC licence and understand venting prohibition.","hazards":[{"risk":"HIGH","text":"Illegal venting — criminal offence and fine","ctrl":"Recovery mandatory. Recovery cylinder on site before any work."},{"risk":"HIGH","text":"Working without ARC licence","ctrl":"Check licence is current before handling. Unlicensed = criminal offence."},{"risk":"MED","text":"Refrigerant exposure","ctrl":"Safety glasses and gloves mandatory. Flush skin/eyes with water 15 min."}],"checklist":["ARC licence checked and current?","Recovery cylinder on site and within test date?","Recovery machine serviceable?","SDS available?","Spill kit available?"],"discussion":"What must you do before disconnecting any refrigerant line?","ref":"Ozone Protection Act 1989; AS/NZS 5149.1:2016"},
        {"title":"Duct installation — support and sealing","objective":"Crew installs ductwork to correct support spacing and sealing requirements.","hazards":[{"risk":"MED","text":"Unsupported ductwork — sagging and failure","ctrl":"Maximum 1200mm support spacing for rectangular duct, 2400mm for flexible"},{"risk":"MED","text":"Leaking ductwork — energy loss and IAQ issue","ctrl":"All joints sealed with approved mastic or tape — not standard tape"},{"risk":"MED","text":"Working at height to install ceiling ductwork","ctrl":"Stable platform, exclusion zone, no overreaching"}],"checklist":["Support spacing within specification?","All joints sealed with approved sealant?","Duct dimensions match design drawings?","Flexible duct not kinked or compressed?","Insulation continuous — no gaps?"],"discussion":"What is the maximum length of flexible duct allowed in a single run?","ref":"AS 4254.1:2012; ASHRAE Fundamentals; SMACNA HVAC Duct Design"},
    ],
    "Gas fitting": [
        {"title":"Gas leak detection procedure","objective":"Every crew member can identify a gas leak and knows the correct emergency response.","hazards":[{"risk":"HIGH","text":"Gas accumulation and explosion","ctrl":"No ignition sources if gas smell detected. Ventilate immediately."},{"risk":"HIGH","text":"Working on pressurised system","ctrl":"Isolate at meter before any disconnection. Confirm zero pressure."},{"risk":"MED","text":"Incorrect leak detection method","ctrl":"Use calibrated detector or leak fluid — never a naked flame"}],"checklist":["Gas detector calibrated?","Leak detection fluid available?","Emergency isolation identified?","Evacuation plan known?","Emergency number posted: 000?"],"discussion":"You smell gas when you arrive on site. What are the first 3 things you do?","ref":"AS/NZS 5601.1:2013 Cl.5.8; Gas Supply Act 1996 NSW"},
        {"title":"Appliance installation clearances","objective":"All crew know mandatory clearance requirements for gas appliance installation.","hazards":[{"risk":"HIGH","text":"Combustible materials too close to appliance","ctrl":"200mm min to side walls, 600mm to overhead combustibles for cooktops"},{"risk":"HIGH","text":"Inadequate ventilation causing CO buildup","ctrl":"Flued appliances need permanent ventilation — 1cm2 per kW minimum"},{"risk":"MED","text":"LP gas appliances without floor ventilation","ctrl":"LP gas is heavier than air — floor level ventilation mandatory"}],"checklist":["Clearances measured and confirmed?","Ventilation openings sized correctly?","Appliance flued correctly?","Gas type confirmed — LP or natural?","Pressure tested after connection?"],"discussion":"What is the minimum clearance from a gas cooktop to overhead combustibles?","ref":"AS/NZS 5601.1:2013 Cl.6.3; AS 4607"},
    ],
    "Welding": [
        {"title":"Welding fumes — health effects and controls","objective":"Crew understands health risks and required controls for welding fumes.","hazards":[{"risk":"HIGH","text":"Lung damage from chronic fume exposure","ctrl":"LEV as first control. RPE as supplement — not substitute."},{"risk":"HIGH","text":"Manganism from MIG/FCAW","ctrl":"P2 for mild steel. P3 for stainless, galvanised, or coated metals."},{"risk":"MED","text":"Confined space fume accumulation","ctrl":"Forced ventilation required. Atmospheric monitoring before entry."}],"checklist":["LEV operational before welding starts?","Correct RPE grade for base metal type?","RPE fit-tested?","Wind direction checked?","No eating/drinking in welding area?"],"discussion":"What RPE grade is required when welding galvanised steel?","ref":"Safe Work Australia COP: Welding Processes; WES-Health Standard 2022"},
        {"title":"Fire prevention — hot work controls","objective":"All crew understand fire risks from welding and required controls.","hazards":[{"risk":"HIGH","text":"Ignition of nearby combustibles","ctrl":"Remove combustibles 10m radius or use fire blankets to shield"},{"risk":"HIGH","text":"Sparks igniting concealed materials","ctrl":"30-minute fire watch after welding — check voids and ceiling spaces"},{"risk":"MED","text":"No fire extinguisher available","ctrl":"Type C extinguisher within 3m of hot work at all times"}],"checklist":["Hot work permit obtained?","Combustibles removed or shielded?","Fire extinguisher within 3m?","Fire watch person nominated?","30-minute post-work watch confirmed?"],"discussion":"Welding finishes at 3pm. Until what time must the fire watch continue?","ref":"AS 1940:2017; WHS Act 2011; Safe Work Australia Hot Work COP"},
    ],
    "Solar / Battery": [
        {"title":"DC arc flash — rooftop solar safety","objective":"All crew understand why DC arcs are more dangerous and required controls.","hazards":[{"risk":"HIGH","text":"DC arc flash — cannot self-extinguish","ctrl":"DC-rated isolators mandatory. AC isolators on DC = fire risk."},{"risk":"HIGH","text":"Live DC voltage even when inverter off","ctrl":"Array generates voltage in any light. Shade or cover panels before string work."},{"risk":"HIGH","text":"Falls from roof","ctrl":"Edge protection or harness required above 2m. Anchor points 15kN min."}],"checklist":["DC-rated isolators confirmed?","Panel shading available?","Fall protection in place before roof access?","Arc-rated PPE for string work?","String voltage calculated within inverter limits?"],"discussion":"Why can you not use a standard AC isolator on a DC solar circuit?","ref":"AS/NZS 5033:2021 Cl.4.3; AS/NZS 3000:2018; CEC Installer Guidelines"},
        {"title":"Battery storage installation safety","objective":"Crew knows mandatory clearance and location requirements for battery systems.","hazards":[{"risk":"HIGH","text":"Thermal runaway — fire and toxic gas","ctrl":"600mm clearance all sides. No sleeping areas. Ventilation for thermal event."},{"risk":"HIGH","text":"Short circuit during installation","ctrl":"Use insulated tools. Cover adjacent terminals. One connection at a time."},{"risk":"MED","text":"Battery installed in wrong location","ctrl":"Cannot install in living areas, sleeping areas, or blocking egress"}],"checklist":["Location approved — not sleeping area or egress path?","600mm clearance all sides?","Ventilation provided?","Insulated tools in use?","Thermal management system connected?"],"discussion":"What is the minimum clearance required around a lithium battery system?","ref":"AS/NZS 5139:2019 Cl.5.3; CEC Battery Installation Guidelines"},
    ],
    "Fire protection": [
        {"title":"Hot work permit and fire watch","objective":"All crew understand hot work permit conditions and fire watch obligations.","hazards":[{"risk":"HIGH","text":"Ignition of concealed combustibles","ctrl":"30-minute fire watch after all hot work. Check voids and ceiling spaces."},{"risk":"HIGH","text":"Sprinkler heads damaged during installation","ctrl":"Cap heads during construction. Replace painted or damaged heads."},{"risk":"MED","text":"System isolation without permit","ctrl":"Formal impairment permit required. Notify building owner and fire brigade."}],"checklist":["Hot work permit obtained?","Fire extinguisher within 3m?","Fire watch person nominated?","30-minute post-work watch confirmed?","Sprinkler impairment permit obtained if isolated?"],"discussion":"Hot work finishes at 2pm. Until what time must fire watch continue?","ref":"AS 1940:2017; NCC Section C; AS 1851:2012 Cl.4.2"},
        {"title":"Sprinkler installation requirements","objective":"Crew installs sprinkler heads to correct clearance and spacing requirements.","hazards":[{"risk":"HIGH","text":"Wrong head type installed","ctrl":"Standard response vs quick response — check design specification"},{"risk":"HIGH","text":"Clearance violation — obstructed discharge","ctrl":"450mm clearance below standard heads. 150mm for quick response."},{"risk":"MED","text":"Painting sprinkler heads","ctrl":"Never paint heads — instantly fails compliance. Replace if painted."}],"checklist":["Head type matches design specification?","Clearances measured and confirmed?","Heads not painted or mechanically damaged?","Correct orientation — pendant, upright, or sidewall?","Design working pressure available at most remote head?"],"discussion":"What is the maximum coverage area per sprinkler head on a flat ceiling?","ref":"AS 2118.1:2017; NCC Section E"},
    ],
    "Carpentry": [
        {"title":"Frame inspection — what must not be covered","objective":"All crew know elements that require inspection before lining.","hazards":[{"risk":"HIGH","text":"Covering work before mandatory inspection","ctrl":"Do not line until Form 10A from Accredited Certifier. No exceptions."},{"risk":"HIGH","text":"Incorrect member sizes","ctrl":"Check every member against drawings before framing — not after."},{"risk":"MED","text":"Missing tie-downs","ctrl":"Every rafter and truss must be tied. Certifier will check."}],"checklist":["Certifier inspection scheduled before lining?","Member sizes match structural drawings?","Bracing installed per engineer specification?","Wet area substrate installed where required?","Tie-down hardware at every connection point?"],"discussion":"What is the consequence of lining a wall before frame inspection sign-off?","ref":"AS 1684.2:2021; EP&A Act 1979 NSW; NCC 2022 Volume 2"},
        {"title":"Manual handling — timber framing","objective":"Crew uses correct manual handling techniques to prevent musculoskeletal injury.","hazards":[{"risk":"HIGH","text":"Back injury from lifting heavy members","ctrl":"Team lift for members over 20kg. Use mechanical aids for long or heavy loads."},{"risk":"MED","text":"Cuts from rough timber and fixings","ctrl":"Cut-resistant gloves Level 3 minimum for handling rough sawn timber"},{"risk":"MED","text":"Splinters and eye injury","ctrl":"Safety glasses mandatory when cutting. Clear the cutting line of all persons."}],"checklist":["Team lift procedure understood for heavy members?","Mechanical aids available on site?","Correct gloves for task?","Safety glasses worn during cutting?","Work area clear of trip hazards?"],"discussion":"What is the maximum recommended weight for a single-person lift in construction?","ref":"Safe Work Australia Hazardous Manual Tasks COP; WHS Regulation 2017"},
    ],
    "Concrete": [
        {"title":"Reinforcement cover — getting it right","objective":"All crew understand cover requirements and verification before the pour.","hazards":[{"risk":"HIGH","text":"Incorrect cover — structural failure over time","ctrl":"Use rated bar chairs. Check cover at multiple points before inspection."},{"risk":"HIGH","text":"Reo movement during pour","ctrl":"Secure all reo at intersections. Recheck cover after vibrating."},{"risk":"MED","text":"Inspector fails the pour","ctrl":"Have structural drawings on site. Know specified cover for each element."}],"checklist":["Cover requirement confirmed from drawings?","Correct bar chair height and spacing?","Cover measured at multiple points?","Structural drawings on site for inspector?","Concrete dockets to be retained?"],"discussion":"What is the minimum cover to reo for a residential slab on ground?","ref":"AS 3600:2018 Table 4.10.3.4"},
        {"title":"Concrete placement — safety requirements","objective":"Crew knows hazards of concrete placement and required controls.","hazards":[{"risk":"HIGH","text":"Concrete burns from skin contact","ctrl":"Waterproof gloves and rubber boots mandatory. Wash contact immediately with water."},{"risk":"HIGH","text":"Pump line failure — concrete blowout","ctrl":"Never stand in front of pump outlet. Check all clamps before pumping."},{"risk":"MED","text":"Noise from concrete pump","ctrl":"Hearing protection when within 3m of pump motor during operation"}],"checklist":["Waterproof gloves and boots available for all crew?","Pump lines and clamps inspected?","Washout area designated?","First aid kit with eye wash available?","Exclusion zone around pump outlet?"],"discussion":"Concrete gets on your forearm and starts to feel warm. What do you do?","ref":"Safe Work Australia Cement Products COP; AS 3610:2018"},
    ],
    "Roofing": [
        {"title":"Falls from roof — control hierarchy","objective":"Every crew member knows today's fall protection plan and individual obligations.","hazards":[{"risk":"HIGH","text":"Fall from roof edge — leading cause of fatality","ctrl":"Edge protection (preferred) or harness + anchor above 2m. Mandatory."},{"risk":"HIGH","text":"Fragile roof surfaces — skylights, old metal","ctrl":"Roof boards over fragile surfaces. Never step directly on polycarbonate."},{"risk":"MED","text":"Materials falling to ground","ctrl":"Exclusion zone below. Toe boards on platforms. Secure loose materials."}],"checklist":["Edge protection installed before first person on roof?","Harness and anchor points inspected?","Exclusion zone barricaded at ground level?","Fragile surfaces identified and marked?","Rescue plan established?"],"discussion":"What is the minimum anchor point rating for a single person working at height?","ref":"Safe Work Australia Working at Height COP; AS/NZS 1891.1:2020; WHS Reg 2017 s.78"},
        {"title":"Wind uplift — correct fixing schedule","objective":"Crew understands the importance of correct fixing schedule for wind region.","hazards":[{"risk":"HIGH","text":"Roof failure in wind event — under-fixed sheets","ctrl":"Use manufacturer fixing guide for your wind category. Do not estimate."},{"risk":"HIGH","text":"Wrong screw class — corrosion failure","ctrl":"Class 4 minimum for coastal. Class 3 for inland. Check local corrosion zone."},{"risk":"MED","text":"Tools dropped from roof","ctrl":"Tool lanyards mandatory. No unsecured tools on roof surface."}],"checklist":["Wind category confirmed for this site?","Fixing schedule from manufacturer available?","Correct screw class for corrosion zone?","Edge and perimeter bays fixed to increased schedule?","Tool lanyards fitted to all hand tools?"],"discussion":"What wind category applies to this site and how does it affect your fixing schedule?","ref":"AS 1170.2:2021; NASH Standard; manufacturer fixing guide"},
    ],
}

# Map a jurisdiction (state/country) to its standards framework label.
def _region_framework(region: str) -> str:
    r = (region or "").strip()
    au = {"NSW","VIC","QLD","WA","SA","ACT","TAS","NT","Australia"}
    uk = {"England","Scotland","Wales","Northern Ireland","UK","United Kingdom"}
    us_hint = {"USA","US","United States"}
    ca = {"Ontario","Quebec","British Columbia","Alberta","Canada"}
    if r in au: return "Australia (AS/NZS standards, WHS Acts, NCC)"
    if r in uk: return "United Kingdom (BS 7671 / IET Wiring Regs, CDM 2015, HSE, Building Regs)"
    if r in ca: return "Canada (CSA standards, provincial OH&S, National codes)"
    if r in us_hint or _region_in_country(r, "USA"): return "United States (NEC/NFPA 70, OSHA 1926, IBC)"
    if _region_in_country(r, "USA"): return "United States (NEC/NFPA 70, OSHA 1926, IBC)"
    # EU member states -> EU/EN framework
    return f"{r} (local/EN/IEC standards — verify against national code)"

def _region_in_country(region: str, country: str) -> bool:
    try:
        return region in VALID_REGIONS.get(country, [])
    except Exception:
        return False

_TOOLBOX_CACHE = {}

# LL-11: toolbox talks and answers are always in English
def _ll_not_english(d) -> bool:
    """True when the text (ignoring the ref line) looks like a non-English language: many accented letters,
    or common Czech/German/French/Spanish/Italian/Polish function words."""
    import json as _j, re as _r
    body = dict(d)
    body.pop("ref", None)
    text = _j.dumps(body, ensure_ascii=False).lower()
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
    accented = sum(1 for c in letters if ord(c) > 127)
    if accented / len(letters) > 0.03:
        return True
    words = _r.findall(r"[a-z]+", text)
    foreign = {"und", "der", "die", "das", "mit", "nicht", "les", "des", "pour", "avec", "est", "los", "las", "para",
               "con", "che", "della", "per", "nie", "jest", "oraz", "na", "je", "se", "pro", "nebo", "jsou"}
    hits = sum(1 for w in words if w in foreign)
    return len(words) > 30 and hits / len(words) > 0.04


async def _localize_topic(topic: dict, trade: str, region: str) -> dict:
    """Rewrite a topic's region-specific references for the given jurisdiction.
    Subject stays the same; standards/citations/control wording localized.
    Cached per trade+region+day. AU topics for an AU region pass through."""
    if not region or _region_in_country(region, "Australia") or region == "Australia":
        return topic  # source content is already AU-native
    key = (trade, region, _dt.date.today().isoformat(), topic.get("title",""), "en")
    if key in _TOOLBOX_CACHE:
        return _TOOLBOX_CACHE[key]
    framework = _region_framework(region)
    system = (
        "You localize construction toolbox-talk safety content to a specific "
        "jurisdiction's standards. Keep the SUBJECT and structure identical. "
        "Rewrite ONLY the code references, standard numbers, and any control "
        "text that cites a specific standard, so they match the target "
        "jurisdiction's framework. Do NOT invent precise clause numbers you are "
        "not confident about — where unsure, name the correct governing standard "
        "and say the specific clause must be verified. Return STRICT JSON with the "
        "same keys as the input (title, objective, hazards[risk,text,ctrl], "
        "checklist, discussion, ref). Append to 'ref' the note "
        "'— verify against current local edition'. "
        "LANGUAGE: every value MUST be in English, whatever the country. Do not translate the talk into the local "
        "language; only official names of laws and standards stay as published (e.g. CSN EN 50110-1)."
    )
    import json as _json
    user = (f"Jurisdiction: {region} — {framework}\nTrade: {trade}\n\n"
            f"Localize this topic:\n{_json.dumps(topic, ensure_ascii=False)}")
    try:
        result = await ask_claude(system, user)
        # ask_claude -> _parse_llm_text returns the PARSED dict directly. For a
        # localized topic that dict IS the topic (has hazards/ref). Only the
        # wrapped {status,result,...} shape carries text under "result".
        loc = None
        if isinstance(result, dict):
            if result.get("hazards") and result.get("ref"):
                loc = result
            elif isinstance(result.get("result"), str):
                s = result["result"].strip()
                if s.startswith("```"):
                    import re as _re
                    s = _re.sub(r"```json|```", "", s).strip()
                try:
                    loc = _json.loads(s)
                except Exception:
                    loc = None
        if isinstance(loc, dict) and _ll_not_english(loc):
            loc = None  # LL-11: never show a toolbox talk that came back in another language
        if isinstance(loc, dict) and loc.get("hazards") and loc.get("ref"):
            if "verify against" not in loc.get("ref", "").lower():
                loc["ref"] = loc["ref"] + " — verify against current local edition"
            _TOOLBOX_CACHE[key] = loc
            return loc
    except Exception:
        pass
    # safe fallback: keep subject, flag the reference as non-local
    safe = dict(topic)
    safe["ref"] = (topic.get("ref","") +
                   f"  [Source standard is Australian — {region} equivalent applies; verify against local code]")
    _TOOLBOX_CACHE[key] = safe
    return safe

def get_daily_toolbox(trade: str) -> dict:
    """Get today's toolbox topic for a given trade (region-neutral subject)."""
    topics = TOOLBOX_TOPICS.get(trade, [])
    if not topics:
        return {}
    day_num = _dt.date.today().timetuple().tm_yday
    return topics[day_num % len(topics)]

async def get_daily_toolbox_localized(trade: str, region: str) -> dict:
    """Region-aware daily topic: universal subject, localized references."""
    topic = get_daily_toolbox(trade)
    if not topic:
        return {}
    return await _localize_topic(topic, trade, region)


async def ask_claude_vision(system_prompt: str, user_message: str,
                            image_b64: str, media_type: str = "image/jpeg") -> dict:
    """Call Claude vision API with image for visual compliance checking."""
    system_prompt = system_prompt + _practice_for(user_message)
    if len(image_b64) > 1_400_000:
        return {"status": "ERROR", "result": "Image too large. Resize under 1MB.", "code_reference": ""}
    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            resp = await client.post(
                ANTHROPIC_URL,
                headers={"x-api-key": ANTHROPIC_API_KEY,
                         "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": MODEL, "max_tokens": 2048,
                      "system": system_prompt,
                      "messages": [{"role": "user", "content": [
                          {"type": "image", "source": {
                              "type": "base64",
                              "media_type": media_type,
                              "data": image_b64}},
                          {"type": "text", "text": user_message}
                      ]}]}
            )
            if resp.status_code == 200:
                return _parse_llm_text(resp.json()["content"][0]["text"])
            return {"status": "ERROR", "result": f"API error {resp.status_code}", "code_reference": ""}
        except Exception as e:
            return {"status": "ERROR", "result": str(e), "code_reference": ""}




def check_free_tier_server(fingerprint, ip):
    FREE_DAILY = 50
    fp = (fingerprint or ("ip_" + (ip or "unknown")))[:200]
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                # Privacy: store only the anonymous token + daily count.
                # Raw IP is not retained (data minimisation).
                cur.execute(
                    "INSERT INTO legis_link_free_usage (fingerprint, date_utc, query_count) "
                    "VALUES (%s, CURRENT_DATE, 0) "
                    "ON CONFLICT (fingerprint, date_utc) DO UPDATE "
                    "SET last_seen=NOW() "
                    "RETURNING query_count",
                    (fp,)
                )
                # Best-effort retention: drop counters older than 30 days.
                try:
                    cur.execute("DELETE FROM legis_link_free_usage WHERE date_utc < CURRENT_DATE - INTERVAL '30 days'")
                except Exception:
                    pass
                row = cur.fetchone()
                used = row[0] if row else 0
            conn.commit()
        return {"allowed": used < FREE_DAILY, "remaining": max(0, FREE_DAILY - used), "used": used}
    except Exception as e:
        import logging; logging.getLogger("legis").warning("Rate limit DB error: %s", e)
        return {"allowed": True, "remaining": 50, "used": 0}


def increment_free_tier(fingerprint, ip):
    fp = (fingerprint or ("ip_" + (ip or "unknown")))[:200]
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE legis_link_free_usage SET query_count=query_count+1, last_seen=NOW() "
                    "WHERE fingerprint=%s AND date_utc=CURRENT_DATE",
                    (fp,)
                )
            conn.commit()
    except Exception:
        pass


async def handle_fingerprint(request):
    try:
        data = await request.json()
        fp = data.get("fingerprint", "")
        ip = request.headers.get("x-forwarded-for", "")
        if not ip and hasattr(request, "client") and request.client:
            ip = request.client.host
        if ip and "," in ip:
            ip = ip.split(",")[0].strip()
        from starlette.responses import JSONResponse as _JR
        return _JR(check_free_tier_server(fp, ip))
    except Exception:
        from starlette.responses import JSONResponse as _JR
        return _JR({"allowed": True, "remaining": 50, "used": 0})
def run_http():
    try:
        from mcp.server.sse import SseServerTransport
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse
        from starlette.routing import Mount, Route
        import uvicorn

        sse = SseServerTransport("/messages/")

        async def handle_sse(request):
            _LL_CLIENT.set(_client_ip(request))
            _LL_KEY.set(_request_key(request))
            async with sse.connect_sse(
                request.scope, request.receive, request._send
            ) as streams:
                await server.run(streams[0], streams[1],
                                 server.create_initialization_options())

        async def handle_gumroad_webhook(request):
            """Gumroad webhook. Set Ping URL (with ?secret=) in Gumroad Settings > Advanced."""
            import hmac as _hmac
            _wh_secret = os.environ.get("GUMROAD_WEBHOOK_SECRET", "")
            _provided = request.query_params.get("secret", "")
            if not _wh_secret or not _hmac.compare_digest(_provided, _wh_secret):
                audit_log("", "", "webhook_sale", "", "", "REJECTED_BAD_SECRET")
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try: body = await request.json()
            except:
                form = await request.form(); body = dict(form)
            email = str(body.get("email","")).strip()
            if not email: return JSONResponse({"error":"no email"},status_code=400)
            if body.get("refunded"): _revoke_key(email); _KEY_CACHE.clear(); return JSONResponse({"status":"revoked"})
            if not KEY_SECRET:
                _notify_sale(email, "NOT ISSUED - set LEGIS_KEY_SECRET in Railway, then issue by hand", str(body.get("product_name","")), str(body.get("sale_id","")))
                return JSONResponse({"error": "server not configured"}, status_code=500)
            key = generate_pro_key(email)
            _store_key(email, key, str(body.get("sale_id","")), str(body.get("product_name","")))
            _notify_sale(email, key, str(body.get("product_name","")), str(body.get("sale_id","")))
            _log_milestone("sale", f"Legis-Link Pro sale ? {email} ? sale_id {str(body.get('sale_id',''))}")
            audit_log(email[:20],"pro","webhook_sale","","","KEY_ISSUED")
            return JSONResponse({"status":"ok","email":email,"tier":"pro"})

        async def handle_key_lookup(request):
            """Admin. GET /key/lookup?email=x&secret=LEGIS_ADMIN_SECRET  - add &issue=1 to issue and store a Pro key by hand."""
            import hmac as _hm
            secret = os.environ.get("LEGIS_ADMIN_SECRET","")
            if not secret or not _hm.compare_digest(request.query_params.get("secret",""), secret):
                return JSONResponse({"error":"Unauthorized"},status_code=401)
            email = request.query_params.get("email","").strip().lower()
            if not email: return JSONResponse({"error":"email required"},status_code=400)
            if request.query_params.get("issue") == "1":
                if not KEY_SECRET:
                    return JSONResponse({"error":"LEGIS_KEY_SECRET is not set in Railway"},status_code=500)
                key = generate_pro_key(email)
                _store_key(email, key, "manual", "manual")
                return JSONResponse({"email":email,"pro_key":key,"issued":True})
            try:
                with get_db() as conn:
                    cur = conn.cursor()
                    cur.execute("SELECT api_key, active, sale_id, created_at FROM legis_link_keys WHERE email=%s", (email,))
                    row = cur.fetchone()
            except Exception as e:
                logging.warning(f"Key lookup database error: {e}")
                return JSONResponse({"error":"database unavailable - try again in a minute"},status_code=503)
            if not row: return JSONResponse({"email":email,"found":False})
            return JSONResponse({"email":email,"found":True,"pro_key":row[0],"active":row[1],"sale_id":row[2],"created_at":str(row[3])})

        async def handle_toolbox(request):
            """Daily toolbox talk — free tier preview."""
            trade  = request.query_params.get("trade", "Electrical")
            region = request.query_params.get("region", "").strip()
            if region:
                topic = await get_daily_toolbox_localized(trade, region)
            else:
                topic = get_daily_toolbox(trade)
            if not topic:
                return JSONResponse({"error": f"No toolbox topic for trade: {trade}"}, status_code=404)
            return JSONResponse({
                "trade":     trade,
                "date":      str(_dt.date.today()),
                "day":       _dt.date.today().strftime("%A"),
                "title":     topic["title"],
                "objective": topic["objective"],
                "hazards":   topic["hazards"][:3],
                "checklist": topic["checklist"],
                "discussion":topic["discussion"],
                "ref":       topic["ref"],
                "pro_prompt":"Upgrade to Pro for full printable PDF with method statement and crew sign-off register",
                "upgrade":   PRO_UPGRADE,
            })

        async def handle_toolbox_pdf(request):
            """Generate full toolbox talk PDF — Pro only."""
            from starlette.responses import Response as _Resp
            api_key = request.query_params.get("api_key", "")
            auth    = validate_api_key(api_key)
            if not auth["valid"] or auth["tier"] != "pro":
                return JSONResponse({"error": "Pro required", "upgrade": PRO_UPGRADE}, status_code=403)
            trade  = request.query_params.get("trade", "Electrical")
            region = request.query_params.get("region", "").strip()
            if region:
                topic = await get_daily_toolbox_localized(trade, region)
            else:
                topic = get_daily_toolbox(trade)
            if not topic:
                return JSONResponse({"error": f"No topic for {trade}"}, status_code=404)
            try:
                import io as _io
                from reportlab.lib.pagesizes import A4
                from reportlab.lib import colors as _colors
                from reportlab.lib.units import mm
                from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
                from reportlab.lib.styles import ParagraphStyle
                from reportlab.lib.enums import TA_CENTER, TA_RIGHT

                buf = _io.BytesIO()
                doc = SimpleDocTemplate(buf, pagesize=A4,
                    leftMargin=15*mm, rightMargin=15*mm,
                    topMargin=12*mm, bottomMargin=15*mm)
                W, H = A4
                PW = W - 30*mm

                NAVY  = _colors.HexColor("#0a2540")
                BLUE  = _colors.HexColor("#1a6fd4")
                LBLUE = _colors.HexColor("#e8f0fb")
                GREEN = _colors.HexColor("#0d7a55")
                LGRN  = _colors.HexColor("#e6f5ee")
                AMBER = _colors.HexColor("#b45309")
                LAMB  = _colors.HexColor("#fef3c7")
                RED   = _colors.HexColor("#b91c1c")
                LRED  = _colors.HexColor("#fee2e2")
                LGRAY = _colors.HexColor("#f8f9fa")
                GRAY  = _colors.HexColor("#6b7280")
                DGRAY = _colors.HexColor("#374151")
                BOR   = _colors.HexColor("#e5e7eb")

                def S(name, **kw): return ParagraphStyle(name, **kw)
                SB = S("b", fontName="Helvetica-Bold", fontSize=10, textColor=DGRAY, leading=14)
                SR = S("r", fontName="Helvetica",      fontSize=10, textColor=DGRAY, leading=14)
                SS = S("s", fontName="Helvetica",      fontSize=9,  textColor=GRAY,  leading=12)
                SM = S("m", fontName="Helvetica",      fontSize=9,  textColor=GRAY,  leading=12)
                SH = S("h", fontName="Helvetica-Bold", fontSize=9,  textColor=BLUE,  leading=12, spaceAfter=3)
                SF = S("f", fontName="Helvetica",      fontSize=8,  textColor=GRAY,  leading=11, alignment=TA_CENTER)

                story = []

                # Header
                ht = Table([[
                    Paragraph(f"<font color='white'><b>LEGIS-LINK</b> — Toolbox Talk</font>", S("hh", fontName="Helvetica-Bold", fontSize=14, textColor=_colors.white, leading=18)),
                    Paragraph(f"<font color='white'>{trade} · {region} · {_dt.date.today().strftime('%d %b %Y')}</font>", S("hr", fontName="Helvetica", fontSize=11, textColor=_colors.white, leading=14, alignment=TA_RIGHT)),
                ]], colWidths=[PW*0.6, PW*0.4])
                ht.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,-1),NAVY),("PADDING",(0,0),(-1,-1),10)]))
                story.append(ht)
                story.append(Spacer(1,4*mm))

                # Title
                tt = Table([[Paragraph(topic["title"], S("tt", fontName="Helvetica-Bold", fontSize=16, textColor=NAVY, leading=20))]], colWidths=[PW])
                tt.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,-1),LBLUE),("PADDING",(0,0),(-1,-1),10),("LINEBELOW",(0,0),(-1,-1),2,BLUE)]))
                story.append(tt)
                story.append(Spacer(1,3*mm))

                # Objective
                story.append(Paragraph("OBJECTIVE", SH))
                ot = Table([[Paragraph(topic["objective"], SR)]], colWidths=[PW])
                ot.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,-1),LBLUE),("PADDING",(0,0),(-1,-1),8)]))
                story.append(ot)
                story.append(Spacer(1,3*mm))

                # Hazards
                story.append(Paragraph("HAZARD REGISTER", SH))
                rows = [[Paragraph(x, SB) for x in ["Hazard","Risk","Control","Regulation"]]]
                for h in topic["hazards"]:
                    rc = RED if h["risk"]=="CRITICAL" else (_colors.HexColor("#b45309") if h["risk"]=="HIGH" else GREEN)
                    rows.append([
                        Paragraph(h["text"], SR),
                        Paragraph(f"<b>{h['risk']}</b>", S(f"rr{h['risk']}", fontName="Helvetica-Bold", fontSize=9, textColor=rc, alignment=TA_CENTER)),
                        Paragraph(h["ctrl"], SS),
                        Paragraph("", SS),
                    ])
                ht2 = Table(rows, colWidths=[PW*0.28, PW*0.12, PW*0.40, PW*0.20])
                ht2.setStyle(TableStyle([
                    ("BACKGROUND",(0,0),(-1,0),NAVY),("TEXTCOLOR",(0,0),(-1,0),_colors.white),
                    ("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),("FONTSIZE",(0,0),(-1,0),9),
                    ("ROWBACKGROUNDS",(0,1),(-1,-1),[_colors.white, LGRAY]),
                    ("GRID",(0,0),(-1,-1),0.5,BOR),("PADDING",(0,0),(-1,-1),6),("VALIGN",(0,0),(-1,-1),"TOP"),
                ]))
                story.append(ht2)
                story.append(Spacer(1,3*mm))

                # Checklist
                story.append(Paragraph("PRE-START CHECKLIST", SH))
                cr = [[
                    Paragraph("☐", S(f"cb{i}", fontName="Helvetica", fontSize=14, textColor=BLUE, alignment=TA_CENTER)),
                    Paragraph(c, SR),
                    Paragraph("_________________", SM),
                ] for i, c in enumerate(topic["checklist"])]
                ct = Table(cr, colWidths=[PW*0.06, PW*0.70, PW*0.24])
                ct.setStyle(TableStyle([
                    ("ROWBACKGROUNDS",(0,0),(-1,-1),[_colors.white,LGRAY]),
                    ("GRID",(0,0),(-1,-1),0.5,BOR),("PADDING",(0,0),(-1,-1),6),
                ]))
                story.append(ct)
                story.append(Spacer(1,3*mm))

                # Discussion
                story.append(Paragraph("DISCUSSION QUESTION", SH))
                dt2 = Table([[Paragraph(topic["discussion"], S("dq", fontName="Helvetica-BoldOblique", fontSize=10, textColor=AMBER, leading=15))]], colWidths=[PW])
                dt2.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,-1),LAMB),("PADDING",(0,0),(-1,-1),10),("LINERIGHT",(0,0),(0,-1),3,AMBER)]))
                story.append(dt2)
                story.append(Spacer(1,3*mm))

                # Ref
                story.append(Paragraph("REGULATORY REFERENCE", SH))
                story.append(Paragraph(topic["ref"], S("ref", fontName="Helvetica", fontSize=9, textColor=BLUE, leading=13)))
                story.append(Spacer(1,3*mm))

                # Sign-off
                story.append(Paragraph("FOREMAN SIGN-OFF", SH))
                sg = Table([
                    [Paragraph("<b>Foreman:</b>", SR), Paragraph("_"*30, SM), Paragraph("<b>Licence:</b>", SR), Paragraph("_"*20, SM)],
                    [Paragraph("<b>Signature:</b>", SR), Paragraph("_"*30, SM), Paragraph("<b>Time:</b>", SR), Paragraph("_"*20, SM)],
                ], colWidths=[PW*0.15, PW*0.35, PW*0.15, PW*0.35])
                sg.setStyle(TableStyle([("GRID",(0,0),(-1,-1),0.5,BOR),("PADDING",(0,0),(-1,-1),7),("BACKGROUND",(0,0),(-1,-1),LGRAY)]))
                story.append(sg)
                story.append(Spacer(1,2*mm))

                # Attendance
                story.append(Paragraph("CREW ATTENDANCE", SH))
                ah = [[Paragraph(x, SB) for x in ["Name","Role","Licence / ID","Signature","Time"]]]
                ar = ah + [[Paragraph("",SR)]*5 for _ in range(8)]
                at = Table(ar, colWidths=[PW*0.25, PW*0.18, PW*0.22, PW*0.25, PW*0.10])
                at.setStyle(TableStyle([
                    ("BACKGROUND",(0,0),(-1,0),NAVY),("TEXTCOLOR",(0,0),(-1,0),_colors.white),
                    ("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),("FONTSIZE",(0,0),(-1,0),9),
                    ("ROWBACKGROUNDS",(0,1),(-1,-1),[_colors.white,LGRAY]),
                    ("GRID",(0,0),(-1,-1),0.5,BOR),("PADDING",(0,0),(-1,-1),8),
                ]))
                story.append(at)
                story.append(Spacer(1,3*mm))

                # Footer
                ft = Table([[
                    Paragraph("Generated by Legis-Link — legis-link.com", SF),
                    Paragraph("Preliminary compliance briefing. Verify against current published standards.", SF),
                ]], colWidths=[PW*0.5, PW*0.5])
                ft.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,-1),LGRAY),("PADDING",(0,0),(-1,-1),6),("LINEABOVE",(0,0),(-1,0),1,BLUE)]))
                story.append(ft)

                doc.build(story)
                pdf_bytes = buf.getvalue()
                filename = f"Toolbox_{trade.replace(' ','_')}_{region}_{_dt.date.today()}.pdf"
                return _Resp(
                    content=pdf_bytes,
                    media_type="application/pdf",
                    headers={"Content-Disposition": f"attachment; filename={filename}"}
                )
            except Exception as e:
                logging.error(f"Toolbox PDF error: {e}")
                return JSONResponse({"error": f"PDF generation failed: {e}"}, status_code=500)

        async def handle_sitemap(request):
            """XML sitemap for SEO."""
            from starlette.responses import Response
            sitemap = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://legis-link.com/</loc><priority>1.0</priority></url>
  <url><loc>https://legis-link.com/app</loc><priority>0.9</priority></url>
  <url><loc>https://legis-link.com/connect</loc><priority>0.7</priority></url>
</urlset>"""
            return Response(sitemap, media_type="application/xml")

        async def handle_google_verify(request):
            """Google Search Console verification file."""
            from starlette.responses import PlainTextResponse
            return PlainTextResponse("google-site-verification: googlee865ba079fa4b75f.html")

        async def handle_landing(request):
            """Landing page for ads and direct traffic."""
            from starlette.responses import HTMLResponse
            html = _page("landing.html")
            if not html:
                from starlette.responses import RedirectResponse
                return RedirectResponse("/app")
            html = html.encode("utf-8", errors="replace").decode("utf-8")
            html = html.replace("Live &mdash; 645 compliance checks today", _ll_live_badge())
            return HTMLResponse(html)

        async def handle_health(request):
            key = ANTHROPIC_API_KEY
            return JSONResponse({
                "status": "ok", "service": "legis-link-mcp",
                "version": VERSION, "engine": "claude-direct",
                "tools": {"free": 3, "pro": 5, "total": 8},
                "auth": "optional (free tier needs no key)",
                "api_key_set": bool(key),
                "key_secret_set": bool(KEY_SECRET), "admin_key_set": bool(ADMIN_KEY),
                "telegram_set": bool(TELEGRAM_TOKEN), "database_set": bool(DATABASE_URL),
                "dev_mode": DEV_MODE
            })

        async def handle_test(request):
            import hmac as _hm
            if not ADMIN_KEY or not _hm.compare_digest(request.query_params.get("key", ""), ADMIN_KEY):
                return JSONResponse({"error": "Not found"}, status_code=404)
            result = await ask_claude(
                'Return only: {"status":"ok","result":"working","code_reference":"test"}',
                "test"
            )
            return JSONResponse({
                "claude_response": result, "version": VERSION,
                "model": MODEL, "auth": "API key required for tool calls",
                "key_prefix": ANTHROPIC_API_KEY[:12] + "..." if len(ANTHROPIC_API_KEY) > 12 else "MISSING"
            })

        async def handle_roadmap(request):
            """Show the future architecture roadmap."""
            return JSONResponse({
                "version": VERSION,
                "current_foundations": [
                    "API key authentication (ll_f_xxx / ll_p_xxx)",
                    "Rate limiting (50/day free, 1000/day pro)",
                    "Audit logging (file + optional DB)"
                ],
                "roadmap": FUTURE_ROADMAP
            })

        async def handle_server_card(request):
            return JSONResponse(SERVER_CARD)

        async def handle_app(request):
            """PWA mobile chat UI."""
            from starlette.responses import HTMLResponse
            html = _page("app.html")
            if not html:
                html = "<html><body><h1>Legis-Link</h1><p>App page not found.</p></body></html>"
            # Strip surrogate characters that cause UnicodeEncodeError
            html = html.encode("utf-8", errors="replace").decode("utf-8")
            return HTMLResponse(html)

        async def handle_connect_page(request):
            """MCP client connection guide."""
            from starlette.responses import HTMLResponse
            html = _page("connect.html")
            if not html:
                html = "<html><body><h1>Connect</h1></body></html>"
            html = html.encode("utf-8", errors="replace").decode("utf-8")
            return HTMLResponse(html)



        async def handle_manifest(request):
            """PWA manifest."""
            content = _page("manifest.json")
            if not content:
                content = '{"name":"Legis-Link","start_url":"/app","display":"standalone"}'
            return JSONResponse(json.loads(content))

        async def handle_sw(request):
            """Service worker."""
            from starlette.responses import Response
            content = _page("sw.js") or "// service worker"
            return Response(content, media_type="application/javascript")

        async def handle_api_query(request):
            """HTTP POST endpoint for /app page. Returns clean JSON."""
            try:
                body = await request.json()
            except Exception:
                return JSONResponse({"error": "Invalid JSON"}, status_code=400)

            question = body.get("question", "").strip()
            trade    = body.get("trade", "Electrical")
            region   = body.get("region", "NSW")
            role     = body.get("role", "Qualified Tradesperson")
            api_key  = body.get("api_key", "")
            tool     = body.get("tool", "check_compliance")
            # Validate tool name
            valid_tools = {
                "check_compliance", "calculate_technical_spec",
                "generate_safety_checklist", "generate_rams",
                "verify_material_compliance", "get_inspection_requirements"
            }
            if tool not in valid_tools:
                tool = "check_compliance"

            if not question:
                return JSONResponse({"error": "question required"}, status_code=400)

            # Auth check
            auth = validate_api_key(api_key)
            if not auth["valid"]:
                return JSONResponse({"error": auth["reason"]}, status_code=401)

            tier = auth["tier"]

            # Pro-tier gate: block Pro tools for non-Pro keys (mirrors call_tool)
            if is_pro_tool(tool) and tier != "pro":
                return JSONResponse({
                    "error": "This is a Pro feature. Upgrade to access it.",
                    "tool": tool,
                    "upgrade": PRO_UPGRADE
                }, status_code=402)

            # Rate limit
            if tier == "free" and (not api_key or api_key.strip() == "dev_local"):
                _ip = _client_ip(request)
                _fp = str(body.get("fingerprint", "") or "")[:200]
                rate = check_rate_limit("", "free", ("fp:" + _fp) if _fp else ("ip:" + _ip))
                if rate["allowed"] and _usage_hit("ipcap:" + hashlib.sha256(_ip.encode()).hexdigest()[:32], FREE_DAILY_LIMIT * 6) is None:
                    rate = {"allowed": False, "remaining": 0, "limit": FREE_DAILY_LIMIT}
            else:
                rate = check_rate_limit(api_key, tier, _client_ip(request))
            if not rate["allowed"]:
                return JSONResponse({
                    "error": f"Daily limit reached ({rate['limit']} requests). Resets tomorrow.",
                    "upgrade": PRO_UPGRADE
                }, status_code=429)

            # Call Claude
            # Map tool to system prompt
            prompt_map = {
                "check_compliance":           "compliance",
                "calculate_technical_spec":   "calculation",
                "generate_safety_checklist":  "safety",
                "generate_rams":              "rams",
                "verify_material_compliance": "material",
                "get_inspection_requirements":"inspection",
                "visual_compliance":          "visual",
            }
            prompt_key = prompt_map.get(tool, "compliance")

            # ── Image / visual compliance ──────────────────────────────────
            image_b64  = body.get("image", "") or ""
            media_type = body.get("media_type", "image/jpeg") or "image/jpeg"
            has_image  = len(image_b64) > 100

            if has_image:
                tool = "visual_compliance"
                user_msg = (f"Trade: {trade} | Region: {region} | Role: {role}\n"
                           f"Question: {question or 'Is this installation compliant?'}")
                result = await ask_claude_vision(
                    SYSTEM_PROMPTS["visual"], user_msg, image_b64, media_type
                )
                disclaimer = ("<br><br><em style='color:#94a3b8;font-size:11px'>"
                              "Preliminary visual check only — not a certified inspection."
                              "</em>")
                result["result"] = str(result.get("result","")) + disclaimer
                audit_log(api_key, tier, "visual_compliance", trade, region,
                          result.get("status","OK"))
                return JSONResponse({
                    "status":         result.get("status","INFO"),
                    "result":         result.get("result",""),
                    "code_reference": result.get("code_reference",""),
                    "trade":          trade,
                    "region":         region,
                    "tool":           "visual_compliance",
                    "remaining":      rate["remaining"],
                })
            # Build tool-specific user message
            if tool == "generate_rams":
                user_msg = f"Trade: {trade} | Region: {region} | Role: {role}\nGenerate a full RAMS document for this task: {question}"
            elif tool == "generate_safety_checklist":
                user_msg = f"Trade: {trade} | Region: {region} | Role: {role}\nGenerate a numbered safety checklist for: {question}"
            elif tool == "calculate_technical_spec":
                user_msg = f"Trade: {trade} | Region: {region} | Role: {role}\nCalculate: {question}"
            elif tool == "verify_material_compliance":
                user_msg = f"Trade: {trade} | Region: {region} | Role: {role}\nMaterial compliance check: {question}"
            elif tool == "get_inspection_requirements":
                user_msg = f"Trade: {trade} | Region: {region} | Role: {role}\nFor: {question}\nList ALL mandatory inspection hold points as a simple numbered list. For each stage include: stage name, who inspects, certificate issued, key regulation. Keep each item to 2-3 lines. Plain text, no JSON, no tables."
            else:
                user_msg = f"Trade: {trade} | Region: {region} | Role: {role}\nQuestion: {question}"
            # Ground compliance answers with verified registry data (mirrors MCP path)
            if tool == "check_compliance":
                user_msg += _registry_ground_multi(trade, region, question)
            result   = await ask_claude(SYSTEM_PROMPTS[prompt_key], user_msg)
            audit_log(api_key, tier, tool, trade, region, result.get("status","OK"))

            # Clean up result text for display
            result_text = result.get("result", "")

            # Handle list of dicts (safety checklist format)
            if isinstance(result_text, list):
                lines = []
                for i, item in enumerate(result_text, 1):
                    if isinstance(item, dict):
                        req  = item.get("requirement", item.get("item", ""))
                        ctrl = item.get("control_measure", "")
                        reg  = item.get("regulation_reference", item.get("regulation", ""))
                        lines.append(f"<strong>{i}. {req}</strong>")
                        if ctrl: lines.append(f"Control: {ctrl}")
                        if reg:  lines.append(f"<em>Reg: {reg}</em>")
                        lines.append("")
                    else:
                        lines.append(str(item))
                result_text = "<br>".join(lines)

            elif isinstance(result_text, dict):
                result_text = result_text.get("result", str(result_text))

            # Convert newlines to HTML breaks
            result_text = str(result_text).replace("\\n", "<br>").replace("\n", "<br>")
            # Convert markdown bold **text** to HTML <strong>
            import re as _re
            result_text = _re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', result_text)
            # Convert markdown headers # text to bold
            result_text = _re.sub(r'<br>#+ (.+?)(<br>|$)', r'<br><strong>\1</strong>\2', result_text)
            # Convert markdown list items - text to clean bullets
            result_text = _re.sub(r'<br>   - ', r'<br>&nbsp;&nbsp;&bull; ', result_text)

            # Strip raw JSON artifacts if result looks like JSON
            stripped = result_text.strip().replace("<br>", "\n")
            if stripped.startswith("{") or stripped.startswith("["):
                import json as _json
                try:
                    parsed = _json.loads(stripped)
                    if isinstance(parsed, dict):
                        # Extract readable content from nested JSON
                        inner = parsed.get("result", "")
                        if isinstance(inner, str) and len(inner) > 20:
                            result_text = inner.replace("\n", "<br>")
                        elif isinstance(parsed, dict) and "mandatory_inspections" in parsed:
                            # Format inspection stages
                            stages = parsed.get("mandatory_inspections", [])
                            lines = []
                            for i, s in enumerate(stages, 1):
                                if isinstance(s, dict):
                                    lines.append(f"<strong>{i}. {s.get('stage','')}</strong>")
                                    lines.append(f"Inspector: {s.get('sign_off_authority','')}")
                                    lines.append(f"Certificate: {s.get('certificate_issued','')}")
                                    lines.append(f"Regulation: {s.get('regulation','')}")
                                    lines.append("")
                            result_text = "<br>".join(lines)
                        if not result.get("status") or result.get("status") == "INFO":
                            result["status"] = parsed.get("status", "COMPLIANT")
                        if not result.get("code_reference"):
                            refs = parsed.get("key_standards_and_regulations", [])
                            if refs and isinstance(refs, list):
                                result["code_reference"] = ", ".join(
                                    r.get("standard", r.get("regulation","")) for r in refs[:3] if isinstance(r,dict)
                                )
                except Exception:
                    pass

            if not api_key or api_key == "dev_local":
                _fp2 = body.get("fingerprint", "") if body else ""
                _ip2 = request.headers.get("x-forwarded-for", "")
                if not _ip2 and hasattr(request, "client") and request.client:
                    _ip2 = request.client.host
                increment_free_tier(_fp2, _ip2)
            return JSONResponse({
                "status":         result.get("status", "INFO"),
                "result":         result_text,
                "code_reference": result.get("code_reference", ""),
                "trade":          trade,
                "region":         region,
                "tool":           tool,
                "remaining":      rate["remaining"],
            })

        # ── LL-2a: streamable HTTP at /mcp (the transport new MCP clients and Claude connectors expect) ──
        from contextlib import asynccontextmanager as _acm
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager as _SHSM
        _ll_shsm = _SHSM(app=server, event_store=None, json_response=False, stateless=True)

        class _LLMCP:
            """ASGI app for /mcp. Picks up the visitor's IP and an optional Pro key from ?key=, X-API-Key or Bearer."""
            async def __call__(self, scope, receive, send):
                from starlette.requests import Request as _Rq
                rq = _Rq(scope, receive)
                _LL_CLIENT.set(_client_ip(rq))
                _LL_KEY.set(_request_key(rq))
                await _ll_shsm.handle_request(scope, receive, send)

        @_acm
        async def _ll_lifespan(app):
            async with _ll_shsm.run():
                yield

        starlette_app = Starlette(routes=[
            Route("/",              handle_landing),
            Route("/googlee865ba079fa4b75f.html", handle_google_verify),
            Route("/webhook/gumroad", handle_gumroad_webhook, methods=["POST"]),
            Route("/key/lookup",      handle_key_lookup,      methods=["GET"]),
            Route("/toolbox",       handle_toolbox),
            Route("/toolbox/pdf",   handle_toolbox_pdf),
            Route("/sitemap.xml",   handle_sitemap),
            Route("/robots.txt",    _ll_robots),
            Route("/health",        handle_health),
            Route("/test",          handle_test),
            Route("/roadmap",       handle_roadmap),
            Route("/app",           handle_app),
            Route("/connect",       handle_connect_page),
            Route("/manifest.json", _ll_manifest),
            Route("/favicon.ico", _ll_icon), Route("/favicon.svg", _ll_icon), Route("/icon.svg", _ll_icon),
            Route("/icon-192.png", _ll_icon), Route("/icon-512.png", _ll_icon), Route("/apple-touch-icon.png", _ll_icon),
            Route("/wordmark-light.svg", _ll_icon), Route("/wordmark-dark.svg", _ll_icon),
            Route("/sw.js",         handle_sw),
            Route("/api/fingerprint", handle_fingerprint, methods=["POST"]),
            Route("/api/query",     handle_api_query, methods=["POST"]),
            Route("/.well-known/mcp/server-card.json", handle_server_card),
            Route("/.well-known/mcp-registry-auth", _ll_registry_auth),
            Mount("/messages/", app=sse.handle_post_message),
            Route("/mcp", endpoint=_LLMCP(), methods=["GET", "POST", "DELETE"]),
            Mount("/", routes=[Route("/sse", endpoint=handle_sse)]),
        ], lifespan=_ll_lifespan)

        print(f"[Legis-Link MCP v{VERSION}] HTTP port {PORT} — auth+ratelimit+audit",
              file=sys.stderr)
        uvicorn.run(_ll_canonical(starlette_app), host="0.0.0.0", port=PORT)

    except ImportError as e:
        print(f"HTTP deps missing: {e}", file=sys.stderr)
        sys.exit(1)


async def run_stdio():
    print(f"[Legis-Link MCP v{VERSION}] stdio — auth+ratelimit+audit", file=sys.stderr)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream,
                         server.create_initialization_options())


if __name__ == "__main__":
    if os.environ.get("PORT"):
        run_http()
    else:
        asyncio.run(run_stdio())





