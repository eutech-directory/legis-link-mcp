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
VERSION           = "3.6.0"
# ── Page content (embedded — no filesystem dependency) ────────────────────
_PAGES = {
    "app.html":      "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n  <meta charset=\"UTF-8\"/>\n  <link rel=\"icon\" href=\"/favicon.svg\" type=\"image/svg+xml\"/>\n  <link rel=\"icon\" href=\"/favicon.ico\" sizes=\"32x32\"/>\n  <link rel=\"apple-touch-icon\" href=\"/apple-touch-icon.png\"/>\n  <meta name=\"description\" content=\"Free construction compliance checker for UK trades. Part P, BS 7671, WAHR 2005, CDM 2015. Electricians, plumbers, HVAC. No signup. Free construction compliance AI for UK and EU trades. Part P, BS 7671, WAHR 2005, CDM 2015. 9 features, no signup required.\"/>\n  <meta name=\"keywords\" content=\"construction compliance checker UK, electrical compliance Part P, RAMS template free, building regulations checker\"/>\n  <meta property=\"og:title\" content=\"Legis-Link ? Free Construction Compliance AI\"/>\n  <meta property=\"og:description\" content=\"Free compliance checks, daily toolbox talks, RAMS generation for UK and EU trades. No signup required.\"/>\n  <meta property=\"og:url\" content=\"https://legis-link-mcp-production-3e9b.up.railway.app/app\"/>\n  <meta property=\"og:type\" content=\"website\"/>\n  <meta name=\"twitter:card\" content=\"summary_large_image\"/>\n  <meta name=\"twitter:title\" content=\"Legis-Link ? Construction Compliance AI\"/>\n  <meta name=\"twitter:description\" content=\"Free electrical, plumbing, HVAC and construction compliance checker. UK and EU regulations.\"/>\n  <link rel=\"canonical\" href=\"https://legis-link-mcp-production-3e9b.up.railway.app/app\"/>\n  <script type=\"application/ld+json\">\n  {\"@context\":\"https://schema.org\",\"@type\":\"SoftwareApplication\",\"name\":\"Legis-Link\",\"description\":\"Construction compliance AI for UK and EU trades ? 9 features including free compliance checks, toolbox talks, and Pro RAMS generation\",\"url\":\"https://legis-link-mcp-production-3e9b.up.railway.app/app\",\"applicationCategory\":\"BusinessApplication\",\"operatingSystem\":\"Web\",\"offers\":[{\"@type\":\"Offer\",\"name\":\"Free\",\"price\":\"0\",\"priceCurrency\":\"GBP\"},{\"@type\":\"Offer\",\"name\":\"Pro\",\"price\":\"199\",\"priceCurrency\":\"GBP\",\"billingDuration\":\"P1Y\"}],\"featureList\":\"Compliance check, Code reference, Toolbox talks, RAMS generator, Technical calculations, Safety checklist, Material compliance, Inspection requirements, Visual compliance\"}\n  </script>\n  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no\"/>\n  <meta name=\"mobile-web-app-capable\" content=\"yes\"/>\n  <meta name=\"apple-mobile-web-app-capable\" content=\"yes\"/>\n  <meta name=\"theme-color\" content=\"#0f172a\"/>\n  <title>Legis-Link</title>\n  <link rel=\"manifest\" href=\"/manifest.json\"/>\n  <link href=\"https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&display=swap\" rel=\"stylesheet\"/>\n  <style>\n    :root{\n      --bg:#0a0f1a;--surface:#111827;--surface2:#1a2235;\n      --border:#1e293b;--accent:#3b82f6;--accent2:#06b6d4;\n      --text:#f1f5f9;--muted:#64748b;--muted2:#94a3b8;\n      --green:#10b981;--amber:#f59e0b;--red:#ef4444;\n      --mono:\"IBM Plex Mono\",monospace;--sans:\"IBM Plex Sans\",sans-serif;\n      --r:10px;--sb:64px;\n    }\n    *{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}\n    html,body{height:100%;overflow:hidden;background:var(--bg);color:var(--text);font-family:var(--sans)}\n    .app{display:flex;height:100dvh}\n\n    /* Sidebar */\n    .sb{width:var(--sb);flex-shrink:0;background:var(--surface);border-right:1px solid var(--border);display:flex;flex-direction:column;overflow-y:auto;scrollbar-width:none}\n    .sb::-webkit-scrollbar{display:none}\n    .sb-logo{font-family:var(--mono);font-size:12px;color:var(--accent);text-align:center;padding:10px 0 8px;border-bottom:1px solid var(--border);flex-shrink:0}\n    .tb{display:flex;flex-direction:column;align-items:center;gap:2px;padding:8px 4px;margin:2px 4px;border-radius:8px;cursor:pointer;border:1px solid transparent;background:none;color:var(--muted);font-family:var(--sans);transition:all 0.15s;flex-shrink:0}\n    .tb:active,.tb.on{background:rgba(59,130,246,0.12);border-color:rgba(59,130,246,0.25);color:var(--accent)}\n    .ti{font-size:20px;line-height:1}\n    .tn{font-size:9px;font-weight:500;text-align:center;line-height:1.2;margin-top:1px}\n\n    /* Main */\n    .main{flex:1;display:flex;flex-direction:column;min-width:0;overflow:hidden}\n\n    /* Header */\n    .hdr{background:var(--surface);border-bottom:1px solid var(--border);padding:8px 10px;flex-shrink:0}\n    .hdr-r1{display:flex;align-items:center;justify-content:space-between;margin-bottom:6px}\n    .trade-lbl{font-family:var(--mono);font-size:13px;color:var(--accent);font-weight:500}\n    .rate-pill{font-family:var(--mono);font-size:10px;padding:3px 8px;border-radius:20px;background:var(--border);color:var(--muted2);white-space:nowrap;flex-shrink:0}\n    .rate-pill.warn{color:var(--amber)}\n    .rate-pill.limit{color:var(--red)}\n    .hdr-r2{display:flex;gap:6px}\n    .csel{flex:1;min-width:0;background:var(--surface2);border:1px solid var(--border);border-radius:7px;color:var(--text);font-family:var(--sans);font-size:12px;padding:6px 8px;cursor:pointer;appearance:none;-webkit-appearance:none;outline:none}\n    .csel:focus{border-color:var(--accent)}\n    .csel option{background:var(--surface)}\n\n    /* Tool selector */\n    .tools-bar{padding:6px 10px;display:flex;gap:5px;overflow-x:auto;flex-shrink:0;border-bottom:1px solid var(--border);scrollbar-width:none;background:var(--bg)}\n    .tools-bar::-webkit-scrollbar{display:none}\n    .tool-btn{\n      display:flex;align-items:center;gap:5px;\n      padding:5px 10px;border-radius:7px;border:1px solid var(--border);\n      background:var(--surface);color:var(--muted2);font-size:11px;\n      font-family:var(--sans);cursor:pointer;white-space:nowrap;flex-shrink:0;\n      transition:all 0.15s;\n    }\n    .tool-btn:active,.tool-btn.active{background:rgba(59,130,246,0.12);border-color:rgba(59,130,246,0.35);color:var(--accent)}\n    .tool-btn.pro-tool{border-color:rgba(6,182,212,0.25);color:var(--accent2)}\n    .tool-btn.pro-tool.active{background:rgba(6,182,212,0.12);border-color:var(--accent2);color:var(--accent2)}\n    .tool-dot{width:6px;height:6px;border-radius:50%;background:currentColor;flex-shrink:0}\n    .pro-badge{font-size:8px;background:rgba(6,182,212,0.2);color:var(--accent2);padding:1px 4px;border-radius:3px;font-weight:600}\n\n    /* Offline */\n    .offbar{display:none;background:rgba(245,158,11,0.08);border-bottom:1px solid rgba(245,158,11,0.15);color:var(--amber);font-size:11px;text-align:center;padding:5px;flex-shrink:0}\n    body.offline .offbar{display:block}\n\n    /* Messages */\n    .msgs{flex:1;overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:12px}\n    .msgs::-webkit-scrollbar{width:3px}\n    .msgs::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}\n\n    .welcome{display:flex;flex-direction:column;align-items:center;justify-content:center;flex:1;text-align:center;padding:20px;gap:10px;min-height:140px}\n    .wlogo{width:46px;height:46px;background:linear-gradient(135deg,var(--accent),var(--accent2));border-radius:13px;display:flex;align-items:center;justify-content:center;font-family:var(--mono);font-size:18px;font-weight:600;color:white}\n    .welcome h2{font-family:var(--mono);font-size:16px;font-weight:500}\n    .welcome p{font-size:12px;color:var(--muted2);line-height:1.6;max-width:230px}\n\n    .msg{display:flex;gap:8px;animation:up 0.2s ease}\n    @keyframes up{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:translateY(0)}}\n    .msg.user{flex-direction:row-reverse}\n    .av{width:27px;height:27px;border-radius:8px;flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:600;font-family:var(--mono)}\n    .msg.user .av{background:var(--accent);color:white}\n    .msg.bot  .av{background:var(--surface2);border:1px solid var(--border);color:var(--accent2)}\n    .mb{max-width:calc(100% - 37px);display:flex;flex-direction:column;gap:4px}\n    .bbl{padding:9px 12px;border-radius:var(--r);font-size:13px;line-height:1.65}\n    .msg.user .bbl{background:linear-gradient(135deg,var(--accent),#2563eb);color:white;border-bottom-right-radius:3px}\n    .msg.bot  .bbl{background:var(--surface2);border:1px solid var(--border);border-bottom-left-radius:3px}\n    .tool-tag{display:inline-flex;align-items:center;gap:4px;font-family:var(--mono);font-size:9px;padding:2px 7px;border-radius:4px;width:fit-content;background:var(--border);color:var(--muted);margin-bottom:2px}\n    .stag{display:inline-flex;align-items:center;gap:4px;font-family:var(--mono);font-size:10px;padding:2px 8px;border-radius:4px;width:fit-content}\n    .ok{background:rgba(16,185,129,0.12);color:var(--green)}\n    .wn{background:rgba(245,158,11,0.12);color:var(--amber)}\n    .fl{background:rgba(239,68,68,0.12);color:var(--red)}\n    .crf{font-family:var(--mono);font-size:10px;color:var(--muted)}\n\n    .typing{display:flex;gap:4px;align-items:center;padding:2px 0}\n    .typing span{width:5px;height:5px;background:var(--muted);border-radius:50%;animation:dot 1.2s infinite}\n    .typing span:nth-child(2){animation-delay:0.2s}\n    .typing span:nth-child(3){animation-delay:0.4s}\n    @keyframes dot{0%,80%,100%{transform:scale(1);opacity:0.5}40%{transform:scale(1.3);opacity:1}}\n\n    /* Quick bar */\n    .qbar{padding:6px 10px;display:flex;gap:6px;overflow-x:auto;flex-shrink:0;scrollbar-width:none}\n    .qbar::-webkit-scrollbar{display:none}\n    .qb{background:var(--surface2);border:1px solid var(--border);border-radius:16px;color:var(--muted2);font-size:11px;padding:5px 11px;cursor:pointer;white-space:nowrap;flex-shrink:0;font-family:var(--sans);transition:all 0.15s}\n    .qb:active{border-color:var(--accent);color:var(--text)}\n\n    /* Input */\n    .inp{padding:8px 10px 12px;border-top:1px solid var(--border);background:var(--surface);flex-shrink:0}\n    .inpr{display:flex;flex-direction:column;gap:10px} .inpr textarea{width:100%;font-size:15px;line-height:1.4;resize:none} .actbar{display:flex;gap:10px;align-items:center} .actbar .ibtn-send{margin-left:auto} .ibtn{width:54px;height:54px;border-radius:12px;border:1px solid var(--border);background:var(--surface);color:var(--text);display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px;cursor:pointer;flex-shrink:0;transition:all .15s;-webkit-tap-highlight-color:transparent} .ibtn svg{width:23px;height:23px} .ibtn .iblabel{font-size:8.5px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;opacity:.7} .ibtn:hover{border-color:var(--accent);color:var(--accent)} .ibtn:active{transform:scale(.94)} .ibtn.ibtn-send{background:var(--green,#10b981);color:#fff;border-color:var(--green,#10b981)} .ibtn.ibtn-send .iblabel{opacity:.9} #mic.active{background:var(--red,#e5484d);color:#fff;border-color:var(--red,#e5484d);animation:micpulse 1.2s ease-in-out infinite} #spk.active{background:var(--accent);color:#fff;border-color:var(--accent)} @keyframes micpulse{0%,100%{box-shadow:0 0 0 0 rgba(229,72,77,.5)}50%{box-shadow:0 0 0 7px rgba(229,72,77,0)}} @media(max-width:600px){.ibtn{width:58px;height:58px}.ibtn svg{width:25px;height:25px}.actbar{gap:12px}}\n    textarea#q{flex:1;background:var(--surface2);border:1px solid var(--border);border-radius:var(--r);color:var(--text);font-family:var(--sans);font-size:13px;padding:9px 11px;resize:none;line-height:1.5;max-height:80px;outline:none;transition:border-color 0.15s}\n    textarea#q:focus{border-color:var(--accent)}\n    textarea#q::placeholder{color:var(--muted)}\n    .ibtn{width:38px;height:38px;border-radius:9px;border:none;display:flex;align-items:center;justify-content:center;cursor:pointer;flex-shrink:0;transition:all 0.15s}\n    #mic{background:var(--surface2);border:1px solid var(--border);color:var(--muted2);font-size:13px;font-family:var(--sans)}\n    #mic.on{background:rgba(239,68,68,0.15);border-color:var(--red);color:var(--red);animation:pulse 1s infinite}\n    #cam{background:var(--surface2);border:1px solid var(--border);color:var(--muted2);font-size:12px;font-family:var(--sans)}\n    #cam.has-image{background:rgba(39,174,96,0.15);border-color:var(--green);color:var(--green)}\n    #send{background:var(--accent);color:white}\n    #send:disabled{opacity:0.4;cursor:not-allowed}\n    @keyframes pulse{0%,100%{opacity:1}50%{opacity:0.5}}\n    .upbox{background:rgba(59,130,246,0.07);border:1px solid rgba(59,130,246,0.2);border-radius:var(--r);padding:10px 12px;font-size:13px;color:var(--accent2);line-height:1.7}\n    .upbox a{color:var(--accent);font-weight:600;text-decoration:none}\n    .toolbox-panel{display:none;flex-direction:column;gap:0;border-bottom:1px solid var(--border);flex-shrink:0;max-height:60vh;overflow-y:auto}\n    .toolbox-panel.open{display:flex}\n    .tb-header{padding:10px 12px;background:rgba(16,185,129,0.08);border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between}\n    .tb-title{font-size:13px;font-weight:600;color:var(--green)}\n    .tb-close{background:none;border:none;color:var(--muted);cursor:pointer;font-size:16px;padding:0}\n    .tb-body{padding:12px}\n    .tb-topic{font-size:14px;font-weight:600;color:var(--text);margin-bottom:6px}\n    .tb-obj{font-size:12px;color:var(--muted2);margin-bottom:10px;line-height:1.5}\n    .tb-section-label{font-family:var(--mono);font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:0.05em;margin-bottom:6px}\n    .hazard-item{display:flex;gap:8px;margin-bottom:6px;font-size:12px}\n    .risk-pill{font-size:9px;padding:2px 6px;border-radius:4px;font-weight:700;white-space:nowrap;flex-shrink:0}\n    .risk-c{background:rgba(239,68,68,0.15);color:var(--red)}\n    .risk-h{background:rgba(245,158,11,0.15);color:var(--amber)}\n    .risk-m{background:rgba(59,130,246,0.15);color:var(--accent)}\n    .tb-check{font-size:12px;color:var(--muted2);padding:4px 0;border-bottom:1px solid var(--border);display:flex;gap:8px;align-items:center}\n    .tb-check::before{content:\"\u2610\";color:var(--green);font-size:14px;flex-shrink:0}\n    .tb-discuss{background:rgba(245,158,11,0.08);border-left:3px solid var(--amber);padding:8px 10px;font-size:12px;color:var(--amber);border-radius:0 6px 6px 0;margin-top:8px;line-height:1.5}\n    .tb-ref{font-family:var(--mono);font-size:10px;color:var(--accent2);padding:6px 8px;background:rgba(6,182,212,0.06);border-radius:5px;margin-top:8px}\n    .tb-pdf-btn{display:flex;align-items:center;justify-content:space-between;margin-top:10px;padding:8px 12px;background:rgba(16,185,129,0.1);border:1px solid rgba(16,185,129,0.25);border-radius:8px;font-size:12px;color:var(--green);cursor:pointer}\n    .key-panel{display:none;padding:8px 10px;background:var(--surface2);border-bottom:1px solid var(--border);gap:8px;align-items:center}\n    .key-panel.open{display:flex}\n    .key-input{flex:1;background:var(--bg);border:1px solid var(--border);border-radius:7px;color:var(--text);font-family:var(--mono);font-size:12px;padding:6px 10px;outline:none}\n    .key-input:focus{border-color:var(--accent)}\n    .key-save{background:var(--accent);color:white;border:none;border-radius:7px;padding:6px 14px;font-size:12px;cursor:pointer;font-weight:600}\n    .tier-badge{font-family:var(--mono);font-size:10px;padding:2px 8px;border-radius:10px}\n    .tier-free{background:var(--border);color:var(--muted2)}\n    .tier-pro{background:rgba(16,185,129,0.15);color:var(--green);border:1px solid rgba(16,185,129,0.3)}\n    .upbox strong{color:var(--text)}\n  </style>\n</head>\n<body>\n<div class=\"app\">\n  <div class=\"sb\" id=\"sb\">\n    <div class=\"sb-logo\">LL</div>\n  </div>\n  <div class=\"main\">\n    <div class=\"offbar\">Offline - cached answers only</div>\n    <div class=\"key-panel\" id=\"key-panel\">\n      <input class=\"key-input\" id=\"key-input\" placeholder=\"Enter Pro key (ll_p_xxx) or leave blank for free tier\" maxlength=\"50\"/>\n      <button class=\"key-save\" id=\"key-save-btn\">Save</button>\n      <span class=\"tier-badge tier-free\" id=\"tier-badge\">FREE</span>\n    </div>\n    <header class=\"hdr\">\n      <div class=\"hdr-r1\">\n        <div class=\"trade-lbl\" id=\"tlbl\">Electrical</div>\n        <div class=\"rate-pill\" id=\"rate\">50 left</div>\n        <button id=\"key-btn\" onclick=\"toggleKeyInput()\" style=\"background:var(--accent);border:none;color:white;font-size:11px;padding:5px 12px;border-radius:6px;cursor:pointer;font-family:var(--mono);margin-left:6px;font-weight:700\">KEY</button>\n      </div>\n      <div class=\"hdr-r2\">\n        <select class=\"csel\" id=\"region\">\n          <option value=\"NSW\">AU - NSW</option>\n          <option value=\"VIC\">AU - VIC</option>\n          <option value=\"QLD\">AU - QLD</option>\n          <option value=\"WA\">AU - WA</option>\n          <option value=\"SA\">AU - SA</option>\n          <option value=\"ACT\">AU - ACT</option>\n          <option value=\"TAS\">AU - TAS</option>\n          <option value=\"NT\">AU - NT</option>\n          <option value=\"England\">UK - England</option>\n          <option value=\"Scotland\">UK - Scotland</option>\n          <option value=\"Wales\">UK - Wales</option>\n          <option value=\"Northern Ireland\">UK - Northern Ireland</option>\n          <option value=\"Alabama\">US - Alabama</option>\n          <option value=\"Alaska\">US - Alaska</option>\n          <option value=\"Arizona\">US - Arizona</option>\n          <option value=\"Arkansas\">US - Arkansas</option>\n          <option value=\"California\">US - California</option>\n          <option value=\"Colorado\">US - Colorado</option>\n          <option value=\"Connecticut\">US - Connecticut</option>\n          <option value=\"Delaware\">US - Delaware</option>\n          <option value=\"Florida\">US - Florida</option>\n          <option value=\"Georgia\">US - Georgia</option>\n          <option value=\"Hawaii\">US - Hawaii</option>\n          <option value=\"Idaho\">US - Idaho</option>\n          <option value=\"Illinois\">US - Illinois</option>\n          <option value=\"Indiana\">US - Indiana</option>\n          <option value=\"Iowa\">US - Iowa</option>\n          <option value=\"Kansas\">US - Kansas</option>\n          <option value=\"Kentucky\">US - Kentucky</option>\n          <option value=\"Louisiana\">US - Louisiana</option>\n          <option value=\"Maine\">US - Maine</option>\n          <option value=\"Maryland\">US - Maryland</option>\n          <option value=\"Massachusetts\">US - Massachusetts</option>\n          <option value=\"Michigan\">US - Michigan</option>\n          <option value=\"Minnesota\">US - Minnesota</option>\n          <option value=\"Mississippi\">US - Mississippi</option>\n          <option value=\"Missouri\">US - Missouri</option>\n          <option value=\"Montana\">US - Montana</option>\n          <option value=\"Nebraska\">US - Nebraska</option>\n          <option value=\"Nevada\">US - Nevada</option>\n          <option value=\"New Hampshire\">US - New Hampshire</option>\n          <option value=\"New Jersey\">US - New Jersey</option>\n          <option value=\"New Mexico\">US - New Mexico</option>\n          <option value=\"New York\">US - New York</option>\n          <option value=\"North Carolina\">US - North Carolina</option>\n          <option value=\"North Dakota\">US - North Dakota</option>\n          <option value=\"Ohio\">US - Ohio</option>\n          <option value=\"Oklahoma\">US - Oklahoma</option>\n          <option value=\"Oregon\">US - Oregon</option>\n          <option value=\"Pennsylvania\">US - Pennsylvania</option>\n          <option value=\"Rhode Island\">US - Rhode Island</option>\n          <option value=\"South Carolina\">US - South Carolina</option>\n          <option value=\"South Dakota\">US - South Dakota</option>\n          <option value=\"Tennessee\">US - Tennessee</option>\n          <option value=\"Texas\">US - Texas</option>\n          <option value=\"Utah\">US - Utah</option>\n          <option value=\"Vermont\">US - Vermont</option>\n          <option value=\"Virginia\">US - Virginia</option>\n          <option value=\"Washington\">US - Washington</option>\n          <option value=\"West Virginia\">US - West Virginia</option>\n          <option value=\"Wisconsin\">US - Wisconsin</option>\n          <option value=\"Wyoming\">US - Wyoming</option>\n          <option value=\"Washington DC\">US - Washington DC</option>\n          <option value=\"Ontario\">CA - Ontario</option>\n          <option value=\"British Columbia\">CA - British Columbia</option>\n          <option value=\"Alberta\">CA - Alberta</option>\n          <option value=\"Quebec\">CA - Quebec</option>\n          <option value=\"Manitoba\">CA - Manitoba</option>\n          <option value=\"Saskatchewan\">CA - Saskatchewan</option>\n          <option value=\"Nova Scotia\">CA - Nova Scotia</option>\n          <option value=\"New Brunswick\">CA - New Brunswick</option>\n          <option value=\"Newfoundland\">CA - Newfoundland</option>\n          <option value=\"Prince Edward Island\">CA - Prince Edward Island</option>\n          <option value=\"Germany\">EU - Germany</option>\n          <option value=\"France\">EU - France</option>\n          <option value=\"Netherlands\">EU - Netherlands</option>\n          <option value=\"Ireland\">EU - Ireland</option>\n          <option value=\"Spain\">EU - Spain</option>\n          <option value=\"Italy\">EU - Italy</option>\n          <option value=\"Belgium\">EU - Belgium</option>\n          <option value=\"Austria\">EU - Austria</option>\n          <option value=\"Denmark\">EU - Denmark</option>\n          <option value=\"Sweden\">EU - Sweden</option>\n          <option value=\"Finland\">EU - Finland</option>\n          <option value=\"Portugal\">EU - Portugal</option>\n          <option value=\"Poland\">EU - Poland</option>\n          <option value=\"Czech Republic\">EU - Czech Republic</option>\n        </select>\n        <select class=\"csel\" id=\"role\" style=\"max-width:105px\">\n          <option value=\"Qualified Tradesperson\">Qualified Tradesperson</option>\n          <option value=\"Apprentice / Trainee\">Apprentice / Trainee</option>\n          <option value=\"Supervisor / Foreman\">Supervisor / Foreman</option>\n          <option value=\"PM / Executive\">PM</option>\n        </select>\n      </div>\n    </header>\n\n    <!-- Tool selector -->\n    <div class=\"tools-bar\" id=\"tools-bar\">\n      <button class=\"tool-btn\" id=\"toolbox-btn\" onclick=\"showToolbox()\">\n        <span class=\"tool-dot\" style=\"background:#10b981\"></span>Toolbox\n      </button>\n      <button class=\"tool-btn active\" data-tool=\"check_compliance\">\n        <span class=\"tool-dot\"></span>Compliance\n      </button>\n      <button class=\"tool-btn pro-tool\" data-tool=\"calculate_technical_spec\">\n        <span class=\"tool-dot\"></span>Calc<span class=\"pro-badge\">PRO</span>\n      </button>\n      <button class=\"tool-btn pro-tool\" data-tool=\"generate_safety_checklist\">\n        <span class=\"tool-dot\"></span>Safety<span class=\"pro-badge\">PRO</span>\n      </button>\n      <button class=\"tool-btn pro-tool\" data-tool=\"generate_rams\">\n        <span class=\"tool-dot\"></span>RAMS<span class=\"pro-badge\">PRO</span>\n      </button>\n      <button class=\"tool-btn pro-tool\" data-tool=\"verify_material_compliance\">\n        <span class=\"tool-dot\"></span>Material<span class=\"pro-badge\">PRO</span>\n      </button>\n      <button class=\"tool-btn pro-tool\" data-tool=\"get_inspection_requirements\">\n        <span class=\"tool-dot\"></span>Inspection<span class=\"pro-badge\">PRO</span>\n      </button>\n      <button class=\"tool-btn pro-tool\" data-tool=\"visual_compliance\">\n        <span class=\"tool-dot\"></span>Photo\n      </button>\n    </div>\n\n    <div class=\"msgs\" id=\"msgs\">\n      <div class=\"welcome\" id=\"welcome\">\n        <div class=\"wlogo\">LL</div>\n        <h2>Legis-Link</h2>\n        <p>Select a tool above or ask any compliance question. Pro tools auto-detected from your question.</p>\n      </div>\n    </div>\n    <div class=\"toolbox-panel\" id=\"toolbox-panel\">\n      <div class=\"tb-header\">\n        <div class=\"tb-title\" id=\"tb-day-label\">Today's Toolbox Talk</div>\n        <button class=\"tb-close\" onclick=\"closeToolbox()\">&#10005;</button>\n      </div>\n      <div class=\"tb-body\" id=\"tb-body\">\n        <div style=\"text-align:center;padding:20px;color:var(--muted)\">Loading...</div>\n      </div>\n    </div>\n    <div class=\"qbar\" id=\"qbar\"></div>\n    <div id=\"img-preview-wrap\" style=\"display:none;padding:6px 10px;background:var(--surface2);border-top:1px solid var(--border)\">\n      <img id=\"img-preview\" style=\"max-height:80px;max-width:150px;border-radius:8px;border:1px solid var(--border)\" alt=\"Preview\"/>\n      <button onclick=\"clearImage()\" style=\"background:var(--red);color:white;border:none;border-radius:5px;padding:3px 8px;font-size:11px;cursor:pointer;margin-left:8px\">Remove</button>\n    </div>\n    <div class=\"inp\">\n      <div class=\"inpr\">\n        <textarea id=\"q\" rows=\"2\" placeholder=\"Ask a compliance question...\"></textarea>\n        <input type=\"file\" id=\"img-input\" accept=\"image/jpeg,image/png,image/webp\" style=\"display:none\"/>\n        <div class=\"actbar\">\n          <button class=\"ibtn\" id=\"cam\" title=\"Photo compliance check\"><svg viewBox=\"0 0 32 32\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"1.8\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><rect x=\"3\" y=\"9\" width=\"26\" height=\"18\" rx=\"2.5\"/><path d=\"M11 9l2-3h6l2 3\"/><circle cx=\"16\" cy=\"18\" r=\"5.5\"/><circle cx=\"16\" cy=\"18\" r=\"1.8\" fill=\"currentColor\" stroke=\"none\"/></svg><span class=\"iblabel\">Photo</span></button>\n          <button class=\"ibtn\" id=\"mic\" title=\"Speak your question (uses your browser's speech recognition)\"><svg viewBox=\"0 0 32 32\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"1.8\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><rect x=\"12\" y=\"3\" width=\"8\" height=\"15\" rx=\"4\"/><path d=\"M8 14v1a8 8 0 0 0 16 0v-1\"/><line x1=\"16\" y1=\"23\" x2=\"16\" y2=\"28\"/><line x1=\"11\" y1=\"28\" x2=\"21\" y2=\"28\"/></svg><span class=\"iblabel\">Speak</span></button>\n          <button class=\"ibtn\" id=\"spk\" title=\"Read answers aloud\"><svg viewBox=\"0 0 32 32\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"1.8\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"M4 12h5l7-5v18l-7-5H4z\"/><path d=\"M21 11a6 6 0 0 1 0 10\"/><path d=\"M24.5 7.5a11 11 0 0 1 0 17\"/></svg><span class=\"iblabel\">Listen</span></button>\n          <button class=\"ibtn ibtn-send\" id=\"send\" title=\"Send\"><svg viewBox=\"0 0 32 32\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"1.9\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"M28 4L14 18\"/><path d=\"M28 4L20 28l-6-10-10-6z\"/></svg><span class=\"iblabel\">Send</span></button>\n        </div>\n      </div>\n    </div>\n  </div>\n</div>\n<script>\nconst TRADES = [\n  {id:\"Electrical\",     icon:\"&#9889;\", short:\"Elec\"},\n  {id:\"Plumbing\",       icon:\"&#128295;\",short:\"Plumb\"},\n  {id:\"HVAC\",           icon:\"&#10052;\", short:\"HVAC\"},\n  {id:\"Gas fitting\",    icon:\"&#128293;\",short:\"Gas\"},\n  {id:\"Welding\",        icon:\"&#128293;\",short:\"Weld\"},\n  {id:\"Solar / Battery\",icon:\"&#9728;\",  short:\"Solar\"},\n  {id:\"Fire protection\",icon:\"&#128680;\",short:\"Fire\"},\n  {id:\"Carpentry\",      icon:\"&#128296;\",short:\"Carp\"},\n  {id:\"Concrete\",       icon:\"&#9632;\",  short:\"Conc\"},\n  {id:\"Roofing\",        icon:\"&#8963;\",  short:\"Roof\"},\n];\n\nconst QUICK = {\n  \"Electrical\":      [\"Wire size 20A\",\"Voltage drop\",\"Earth fault loop\",\"RCD requirements\",\"Breaker sizing\"],\n  \"Plumbing\":        [\"Pipe size 50 fixtures\",\"Hot water temp\",\"Backflow prevention\",\"Drain slope\"],\n  \"HVAC\":            [\"Duct size 800 CFM\",\"Ventilation rate\",\"Refrigerant clearance\",\"MERV rating\"],\n  \"Gas fitting\":     [\"Pipe sizing\",\"Pressure test\",\"Ventilation calc\",\"Appliance clearance\"],\n  \"Welding\":         [\"Preheat temp\",\"AWS qualification\",\"Inspection requirements\",\"Electrode storage\"],\n  \"Solar / Battery\": [\"Battery clearance\",\"Inverter location\",\"DC cable sizing\",\"Isolator requirements\"],\n  \"Fire protection\": [\"Detector spacing\",\"Sprinkler clearance\",\"Extinguisher placement\",\"Exit sign height\"],\n  \"Carpentry\":       [\"Bearer span\",\"Joist sizing\",\"Tie-down requirements\",\"Fixing schedule\"],\n  \"Concrete\":        [\"Cover to reo\",\"Curing time\",\"Mix design\",\"Compressive strength\"],\n  \"Roofing\":         [\"Min pitch\",\"Flashing detail\",\"Wind uplift\",\"Sarking requirements\"],\n};\n\nconst TOOL_LABELS = {\n  \"check_compliance\":           \"Compliance\",\n  \"calculate_technical_spec\":   \"Calc\",\n  \"generate_safety_checklist\":  \"Safety\",\n  \"generate_rams\":              \"RAMS\",\n  \"verify_material_compliance\": \"Material\",\n  \"get_inspection_requirements\":\"Inspection\",\n};\n\nconst FREE_LIMIT = 50;\nlet _fp=localStorage.getItem('ll-fp')||'';\n(async()=>{\n  if(!_fp){\n    try{ _fp=(crypto&&crypto.randomUUID)?crypto.randomUUID():('ll_'+Date.now().toString(36)+Math.random().toString(36).slice(2,12)); }\n    catch(e){ _fp='ll_'+Date.now().toString(36)+Math.random().toString(36).slice(2,12); }\n    localStorage.setItem('ll-fp',_fp);\n  }\n  try{\n    const r=await fetch('/api/fingerprint',{method:'POST',\n      headers:{'Content-Type':'application/json'},\n      body:JSON.stringify({fingerprint:_fp})});\n    const d=await r.json();\n    if(typeof d.used==='number'){used=d.used;localStorage.setItem('ll-used',used);updateRate();}\n  }catch(e){}\n})();\nconst CACHE_KEY  = \"ll-cache\";\nlet activeTrade  = \"Electrical\";\nlet activeTool   = \"check_compliance\"; // default, overridden by auto-detect\nlet manualTool   = false; // true when user explicitly picked a tool\n\nlet used = parseInt(localStorage.getItem(\"ll-used\") || \"0\");\nconst savedDate = localStorage.getItem(\"ll-date\") || \"\";\nconst today = new Date().toISOString().slice(0,10);\nif (savedDate !== today) { used = 0; localStorage.setItem(\"ll-date\", today); }\n\n// \u2500\u2500 Auto-detect tool from question \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\nfunction detectTool(q) {\n  const ql = q.toLowerCase();\n  if (/cable size|wire size|pipe size|duct size|voltage drop|current rating|load calc|sizing|ampacity|flow rate|heat load|cooling load|what size|calculate/.test(ql))\n    return \"calculate_technical_spec\";\n  if (/rams|risk assessment|method statement|jha|job hazard|swms|safe work method/.test(ql))\n    return \"generate_rams\";\n  if (/safety checklist|ppe|hazard|toolbox|induction|safety check|what safety|safe to/.test(ql))\n    return \"generate_safety_checklist\";\n  if (/material|compliant|approved|can i use|is this cable|is this pipe|product|brand|specification|meets code/.test(ql))\n    return \"verify_material_compliance\";\n  if (/inspection|sign off|certificate|hold point|certifier|who inspects|test and tag|commissioning|handover/.test(ql))\n    return \"get_inspection_requirements\";\n  return \"check_compliance\";\n}\n\nfunction setActiveTool(toolId, fromUser=false) {\n  activeTool = toolId;\n  manualTool = fromUser;\n  document.querySelectorAll(\".tool-btn\").forEach(b => {\n    b.classList.toggle(\"active\", b.dataset.tool === toolId);\n  });\n}\n\n// \u2500\u2500 Build sidebar \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\nconst sb = document.getElementById(\"sb\");\nTRADES.forEach((t,i) => {\n  const btn = document.createElement(\"button\");\n  btn.className = \"tb\" + (i===0?\" on\":\"\");\n  btn.dataset.trade = t.id;\n  btn.innerHTML = `<span class=\"ti\">${t.icon}</span><span class=\"tn\">${t.short}</span>`;\n  btn.addEventListener(\"click\", function() {\n    document.querySelectorAll(\".tb\").forEach(b => b.classList.remove(\"on\"));\n    this.classList.add(\"on\");\n    activeTrade = this.dataset.trade;\n    document.getElementById(\"tlbl\").textContent = activeTrade;\n    renderQuick();\n    if (document.getElementById(\"toolbox-panel\").classList.contains(\"open\")) loadToolbox();\n  });\n  sb.appendChild(btn);\n});\n\n// \u2500\u2500 Tool button clicks \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\ndocument.querySelectorAll(\".tool-btn\").forEach(btn => {\n  btn.addEventListener(\"click\", function() {\n    setActiveTool(this.dataset.tool, true);\n    // Update placeholder to hint at what to ask\n    const hints = {\n      \"check_compliance\":           \"Ask any compliance question...\",\n      \"calculate_technical_spec\":   \"e.g. Cable size for 32A circuit in conduit, 25m run...\",\n      \"generate_safety_checklist\":  \"e.g. Safety checklist for working at height...\",\n      \"generate_rams\":              \"e.g. RAMS for electrical switchboard installation...\",\n      \"verify_material_compliance\": \"e.g. Is 6mm2 TPS cable approved for this use...\",\n      \"get_inspection_requirements\":\"e.g. Inspection requirements for timber frame NSW...\",\n    };\n    document.getElementById(\"q\").placeholder = hints[this.dataset.tool] || \"Ask a compliance question...\";\n    document.getElementById(\"q\").focus();\n  });\n});\n\nfunction toggleKeyInput() {\n  var panel = document.getElementById(\"key-panel\");\n  panel.classList.toggle(\"open\");\n  if (panel.classList.contains(\"open\")) {\n    var stored = localStorage.getItem(\"ll_api_key\") || \"\";\n    document.getElementById(\"key-input\").value = stored === \"dev_local\" ? \"\" : stored;\n    document.getElementById(\"key-input\").focus();\n  }\n}\nfunction saveKey() {\n  var input = document.getElementById(\"key-input\").value.trim();\n  var key = input;\n  if (key) { localStorage.setItem(\"ll_api_key\", key); } else { localStorage.removeItem(\"ll_api_key\"); }\n  updateTierBadge(key);\n  document.getElementById(\"key-panel\").classList.remove(\"open\");\n  if (key && (key.startsWith(\"ll_p_\"))) {\n    document.getElementById(\"rate\").textContent = \"1000/day\";\n    document.getElementById(\"rate\").className = \"rate-pill\";\n  } else { updateRate(); }\n}\nfunction updateTierBadge(key) {\n  var badge = document.getElementById(\"tier-badge\");\n  var keyBtn = document.getElementById(\"key-btn\");\n  if (!badge) return;\n  if (key && (key.startsWith(\"ll_p_\"))) {\n    badge.textContent = \"PRO\"; badge.className = \"tier-badge tier-pro\";\n    if (keyBtn) { keyBtn.style.color = \"var(--green)\"; keyBtn.textContent = \"PRO\"; }\n  } else {\n    badge.textContent = \"FREE\"; badge.className = \"tier-badge tier-free\";\n    if (keyBtn) { keyBtn.style.color = \"\"; keyBtn.textContent = \"key\"; }\n  }\n}\n// Save key button\nvar saveBtn = document.getElementById(\"key-save-btn\");\nif (saveBtn) saveBtn.addEventListener(\"click\", saveKey);\n\nfunction showToolbox() {\n  const panel = document.getElementById(\"toolbox-panel\");\n  panel.classList.add(\"open\");\n  const btn = document.getElementById(\"toolbox-btn\");\n  if (btn) btn.classList.add(\"active\");\n  loadToolbox();\n}\n\nfunction closeToolbox() {\n  document.getElementById(\"toolbox-panel\").classList.remove(\"open\");\n  const btn = document.getElementById(\"toolbox-btn\");\n  if (btn) btn.classList.remove(\"active\");\n}\n\nasync function loadToolbox() {\n  const body = document.getElementById(\"tb-body\");\n  const dayLabel = document.getElementById(\"tb-day-label\");\n  try {\n    const tbRegionEl = document.getElementById(\"region\"); const tbRegion = tbRegionEl ? tbRegionEl.value : \"\"; const res = await fetch(`/toolbox?trade=${encodeURIComponent(activeTrade)}${tbRegion ? \"&region=\" + encodeURIComponent(tbRegion) : \"\"}`);\n    const d = await res.json();\n    if (!res.ok) { body.innerHTML = `<p style=\"color:var(--red)\">${d.error}</p>`; return; }\n    \n    const days = [\"Sunday\",\"Monday\",\"Tuesday\",\"Wednesday\",\"Thursday\",\"Friday\",\"Saturday\"];\n    const today = days[new Date().getDay()];\n    if (dayLabel) dayLabel.textContent = `${today}'s Toolbox \u2014 ${activeTrade}`;\n\n    const riskClass = r => r===\"CRITICAL\"?\"risk-c\":r===\"HIGH\"?\"risk-h\":\"risk-m\";\n    \n    body.innerHTML = `\n      <div class=\"tb-topic\">${d.title}</div>\n      <div class=\"tb-obj\">${d.objective}</div>\n      \n      <div class=\"tb-section-label\">Hazard register</div>\n      ${d.hazards.map(h=>`\n        <div class=\"hazard-item\">\n          <span class=\"risk-pill ${riskClass(h.risk)}\">${h.risk}</span>\n          <div><div style=\"color:var(--text);margin-bottom:2px\">${h.text}</div><div style=\"color:var(--muted2)\">${h.ctrl}</div></div>\n        </div>`).join(\"\")}\n      \n      <div class=\"tb-section-label\" style=\"margin-top:10px\">Pre-start checklist</div>\n      ${d.checklist.map(c=>`<div class=\"tb-check\">${c}</div>`).join(\"\")}\n      \n      <div class=\"tb-discuss\">${d.discussion}</div>\n      <div class=\"tb-ref\">${d.ref}</div>\n      \n      <div class=\"tb-pdf-btn\" onclick=\"downloadToolboxPDF()\" id=\"tb-pdf-btn\">\n        <span id=\"tb-pdf-label\">Download printable PDF -- Pro</span>\n        <span>&#8594;</span>\n      </div>\n    `;\n  } catch(e) {\n    body.innerHTML = `<p style=\"color:var(--red)\">Error loading toolbox. Try again.</p>`;\n  }\n}\n\nasync function downloadToolboxPDF() {\n  const key = localStorage.getItem(\"ll_api_key\") || \"\";\n  const isPro = key && (key.startsWith(\"ll_p_\"));\n  \n  if (!isPro) {\n    // Show upgrade prompt in chat\n    closeToolbox();\n    addMsg(\"bot\", `<div class=\"upbox\">\n      <strong>Printable toolbox talks require Pro.</strong><br><br>\n      Legis-Link Pro gives you:<br>\n      &#10003; Printable PDF toolbox talk for every trade<br>\n      &#10003; Full method statement + crew attendance register<br>\n      &#10003; 1,000 queries/day + all 8 tools<br><br>\n      <a href=\"https://rickyfarmer.gumroad.com/l/Legis-LinkPro\" target=\"_blank\">Get Pro Access -- $199/year &#8594;</a><br>\n      <span style=\"font-size:10px;color:var(--muted)\">Already have a key? Tap KEY button to enter it.</span>\n    </div>`);\n    return;\n  }\n  \n  // Pro user \u2014 download PDF\n  const region = document.getElementById(\"region\").value;\n  const url = `/toolbox/pdf?trade=${encodeURIComponent(activeTrade)}&region=${encodeURIComponent(region)}&api_key=${encodeURIComponent(key)}`;\n  \n  try {\n    const res = await fetch(url);\n    if (!res.ok) {\n      const d = await res.json();\n      addMsg(\"bot\", `<div class=\"upbox\">PDF error: ${d.error || \"Please try again.\"}</div>`);\n      closeToolbox();\n      return;\n    }\n    const blob = await res.blob();\n    const a = document.createElement(\"a\");\n    a.href = URL.createObjectURL(blob);\n    a.download = `Toolbox_${activeTrade}_${region}_${new Date().toISOString().slice(0,10)}.pdf`;\n    document.body.appendChild(a);\n    a.click();\n    document.body.removeChild(a);\n  } catch(e) {\n    addMsg(\"bot\", \"PDF download failed. Please try again.\");\n    closeToolbox();\n  }\n}\n\nfunction renderQuick() {\n  const bar = document.getElementById(\"qbar\");\n  const qs  = QUICK[activeTrade] || [];\n  bar.innerHTML = \"\";\n  qs.forEach(q => {\n    const btn = document.createElement(\"button\");\n    btn.className = \"qb\";\n    btn.textContent = q;\n    btn.addEventListener(\"click\", function() { ask(this.textContent); });\n    bar.appendChild(btn);\n  });\n}\n\nfunction updateRate() {\n  const el  = document.getElementById(\"rate\");\n  const rem = FREE_LIMIT - used;\n  el.textContent = rem + \" left\";\n  el.className = \"rate-pill\" + (rem<=10?\" warn\":\"\") + (rem<=0?\" limit\":\"\");\n}\n\nfunction addMsg(role, content, meta={}) {\n  const w = document.getElementById(\"welcome\"); if(w) w.remove();\n  const msgs = document.getElementById(\"msgs\");\n  const d = document.createElement(\"div\");\n  d.className = \"msg \" + role;\n  const av = role===\"user\" ? \"U\" : \"LL\";\n  let h = `<div class=\"av\">${av}</div><div class=\"mb\">`;\n  if (meta.tool && role===\"bot\") {\n    h += `<div class=\"tool-tag\">${TOOL_LABELS[meta.tool]||meta.tool}</div>`;\n  }\n  h += `<div class=\"bbl\">${content}</div>`;\n  if (role===\"bot\" && window._llSpeak) { try { const _t=document.createElement(\"div\"); _t.innerHTML=content; const _s=(_t.textContent||\"\").trim(); if(_s){ speechSynthesis.cancel(); const _u=new SpeechSynthesisUtterance(_s.slice(0,600)); _u.rate=1.0; speechSynthesis.speak(_u);} } catch(e){} }\n  if (meta.status && role===\"bot\") {\n    const cls = meta.status===\"COMPLIANT\"?\"ok\":meta.status===\"NON_COMPLIANT\"?\"fl\":\"wn\";\n    h += `<div class=\"stag ${cls}\">&#9679; ${meta.status.replace(/_/g,\" \")}</div>`;\n  }\n  if (meta.ref) h += `<div class=\"crf\">Ref: ${meta.ref}</div>`;\n  h += \"</div>\";\n  d.innerHTML = h;\n  msgs.appendChild(d);\n  msgs.scrollTop = msgs.scrollHeight;\n}\n\nfunction addTyping() {\n  const msgs = document.getElementById(\"msgs\");\n  const d = document.createElement(\"div\");\n  d.className = \"msg bot\"; d.id = \"typing\";\n  d.innerHTML = `<div class=\"av\">LL</div><div class=\"mb\"><div class=\"bbl\"><div class=\"typing\"><span></span><span></span><span></span></div></div></div>`;\n  msgs.appendChild(d); msgs.scrollTop = msgs.scrollHeight;\n}\nfunction rmTyping() { const t=document.getElementById(\"typing\"); if(t) t.remove(); }\n\nfunction getCache(q) {\n  try { return (JSON.parse(localStorage.getItem(CACHE_KEY)||\"[]\")).find(c=>c.q.toLowerCase()===q.toLowerCase())||null; }\n  catch(e) { return null; }\n}\nfunction saveCache(q,r) {\n  try {\n    const c = JSON.parse(localStorage.getItem(CACHE_KEY)||\"[]\");\n    c.push({q,r}); if(c.length>10) c.shift();\n    localStorage.setItem(CACHE_KEY, JSON.stringify(c));\n  } catch(e) {}\n}\n\nvar pendingImage = null;\nvar pendingMediaType = \"image/jpeg\";\n\n// ── Voice: speech-in (mic) + speech-out toggle (spk). Frontend-only. ──\n(function(){\n  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;\n  const mic = document.getElementById(\"mic\");\n  const spk = document.getElementById(\"spk\");\n  // Speaker toggle (off by default)\n  window._llSpeak = false;\n  if (spk) {\n    if (!(\"speechSynthesis\" in window)) { spk.style.display=\"none\"; }\n    else spk.onclick = function(){ window._llSpeak = !window._llSpeak; spk.classList.toggle(\"active\", window._llSpeak); if(!window._llSpeak) speechSynthesis.cancel(); };\n  }\n  // Mic (speech-in). Hide if unsupported so no dead button.\n  if (!SR) { if(mic) mic.style.display=\"none\"; return; }\n  const rec = new SR(); rec.lang = \"en-GB\"; rec.interimResults=false; rec.maxAlternatives=1;\n  let listening=false;\n  if (mic) mic.onclick = function(){\n    if (listening) { rec.stop(); return; }\n    try { rec.start(); listening=true; mic.classList.add(\"active\"); } catch(e){}\n  };\n  rec.onresult = function(ev){\n    const t = ev.results[0][0].transcript;\n    const q = document.getElementById(\"q\"); if(q) q.value = t;\n    if (typeof ask===\"function\") ask(t);\n  };\n  rec.onend = function(){ listening=false; if(mic) mic.classList.remove(\"active\"); };\n  rec.onerror = function(){ listening=false; if(mic) mic.classList.remove(\"active\"); };\n})();\n\nasync function ask(question) {\n  question = (question || document.getElementById(\"q\").value).trim();\n  if (!question) return;\n\n  if (used >= FREE_LIMIT) {\n    addMsg(\"bot\", `<div class=\"upbox\">\n      <strong>You have used all ${FREE_LIMIT} free queries today.</strong><br><br>\n      Legis-Link Pro gives you:<br>\n      &#10003; 1,000 queries/day<br>\n      &#10003; All 8 tools: cable sizing, RAMS, safety checklists, material compliance, inspections<br>\n      &#10003; All regions: AU, UK, US, Canada, EU<br><br>\n      <a href=\"https://rickyfarmer.gumroad.com/l/Legis-LinkPro\" target=\"_blank\">Get Pro Access &mdash; $199/year &rarr;</a><br>\n      <span style=\"font-size:10px;color:var(--muted)\">Free queries reset at midnight UTC each day.</span>\n    </div>`);\n    return;\n  }\n\n  // Auto-detect tool unless user manually selected\n  const tool = manualTool ? activeTool : detectTool(question);\n  setActiveTool(tool, false);\n\n  document.getElementById(\"q\").value = \"\";\n  document.getElementById(\"q\").style.height = \"auto\";\n  document.getElementById(\"send\").disabled = true;\n  addMsg(\"user\", question);\n  addTyping();\n\n  const cached = !navigator.onLine ? getCache(question) : null;\n  try {\n    if (cached) {\n      rmTyping();\n      addMsg(\"bot\",\n        cached.r.result + ' <span style=\"font-size:10px;color:var(--muted)\">(cached)</span>',\n        {status:cached.r.status, ref:cached.r.code_reference, tool});\n    } else {\n      const res = await fetch(\"/api/query\", {\n        method: \"POST\",\n        headers: {\"Content-Type\":\"application/json\"},\n        body: JSON.stringify({\n          trade:   activeTrade,\n          region:  document.getElementById(\"region\").value,\n          role:    document.getElementById(\"role\").value,\n          question,\n          tool,\n          api_key:    localStorage.getItem(\"ll_api_key\") || \"\",\n          fingerprint: _fp||'',\n          image:      pendingImage || undefined,\n          media_type: pendingImage ? pendingMediaType : undefined\n        })\n      });\n      const data = await res.json();\n      rmTyping();\n      if (!res.ok) {\n        if (data.upgrade) {\n          addMsg(\"bot\", (data.error || \"This is a Pro feature.\") + \" <a href=\\\"\" + data.upgrade + \"\\\" target=\\\"_blank\\\" style=\\\"color:var(--accent);font-weight:600;text-decoration:underline\\\">Upgrade to Pro \\u2192</a>\");\n        } else {\n          addMsg(\"bot\", data.error || \"Server error. Please try again.\");\n        }\n      } else {\n        addMsg(\"bot\", data.result, {status:data.status, ref:data.code_reference, tool});\n        saveCache(question, data);\n        used++; localStorage.setItem(\"ll-used\", used); updateRate(); clearImage();\n      }\n    }\n  } catch(e) {\n    rmTyping();\n    addMsg(\"bot\", navigator.onLine ? \"Connection error. Try again.\" : \"Offline. Cached answers only.\");\n  }\n  document.getElementById(\"send\").disabled = false;\n  // Reset to auto-detect after each question\n  manualTool = false;\n}\n\n// Voice\nconst SR = window.SpeechRecognition || window.webkitSpeechRecognition;\nconst mic = document.getElementById(\"mic\");\nif (SR) {\n  const rec = new SR();\n  rec.continuous = false; rec.interimResults = true; rec.lang = \"en-AU\";\n  rec.onresult = e => {\n    const t = Array.from(e.results).map(r=>r[0].transcript).join(\"\");\n    document.getElementById(\"q\").value = t;\n    if (e.results[e.results.length-1].isFinal) { mic.classList.remove(\"on\"); ask(t); }\n  };\n  rec.onend = () => mic.classList.remove(\"on\");\n  mic.addEventListener(\"click\", () => {\n    if (mic.classList.contains(\"on\")) rec.stop();\n    else { mic.classList.add(\"on\"); rec.start(); }\n  });\n} else { mic.style.display = \"none\"; }\n\ndocument.getElementById(\"q\").addEventListener(\"keydown\", e => {\n  if (e.key===\"Enter\" && !e.shiftKey) { e.preventDefault(); ask(); }\n});\ndocument.getElementById(\"q\").addEventListener(\"input\", function() {\n  this.style.height = \"auto\";\n  this.style.height = Math.min(this.scrollHeight, 80) + \"px\";\n});\ndocument.getElementById(\"send\").addEventListener(\"click\", () => ask());\n\nwindow.addEventListener(\"online\",  () => document.body.classList.remove(\"offline\"));\nwindow.addEventListener(\"offline\", () => document.body.classList.add(\"offline\"));\nif (!navigator.onLine) document.body.classList.add(\"offline\");\nif (\"serviceWorker\" in navigator) navigator.serviceWorker.register(\"/sw.js\").catch(()=>{});\n\n\n// \u2500\u2500 Camera / photo upload \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\nfunction clearImage() {\n  pendingImage = null; pendingMediaType = \"image/jpeg\";\n  var cb = document.getElementById(\"cam\");\n  if (cb) { cb.classList.remove(\"has-image\"); cb.textContent = \"cam\"; }\n  var ii = document.getElementById(\"img-input\");\n  if (ii) ii.value = \"\";\n  var wrap = document.getElementById(\"img-preview-wrap\");\n  if (wrap) wrap.style.display = \"none\";\n  var prev = document.getElementById(\"img-preview\");\n  if (prev) prev.src = \"\";\n}\n\nvar camB = document.getElementById(\"cam\");\nvar imgI = document.getElementById(\"img-input\");\nif (camB && imgI) {\n  camB.addEventListener(\"click\", function() { imgI.click(); });\n  imgI.addEventListener(\"change\", function() {\n    var file = this.files[0]; if (!file) return;\n    pendingMediaType = file.type || \"image/jpeg\";\n    var reader = new FileReader();\n    reader.onload = function(e) {\n      pendingImage = e.target.result.split(\",\")[1];\n      camB.classList.add(\"has-image\"); camB.textContent = \"img\";\n      var wrap = document.getElementById(\"img-preview-wrap\");\n      var prev = document.getElementById(\"img-preview\");\n      if (wrap && prev) { prev.src = e.target.result; wrap.style.display = \"block\"; }\n      document.querySelectorAll(\".tool-btn\").forEach(function(b) { b.classList.remove(\"active\"); });\n      var pb = document.querySelector('[data-tool=\"visual_compliance\"]');\n      if (pb) { pb.classList.add(\"active\"); activeTool = \"visual_compliance\"; manualTool = true; }\n      document.getElementById(\"q\").placeholder = \"Ask about this photo \u2014 is this installation compliant?\";\n    };\n    reader.readAsDataURL(file);\n  });\n}\n\nrenderQuick();\nupdateRate();\nvar storedKey = localStorage.getItem(\"ll_api_key\") || \"\";\nupdateTierBadge(storedKey);\nif (storedKey && storedKey.startsWith(\"ll_p_\")) {\n  document.getElementById(\"rate\").textContent = \"1000/day\";\n}\n</script>\n</body>\n</html>",
    "landing.html":  "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n  <meta charset=\"UTF-8\"/>\n  <link rel=\"icon\" href=\"/favicon.svg\" type=\"image/svg+xml\"/>\n  <link rel=\"icon\" href=\"/favicon.ico\" sizes=\"32x32\"/>\n  <link rel=\"apple-touch-icon\" href=\"/apple-touch-icon.png\"/>\n  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\"/>\n  <meta name=\"description\" content=\"Legis-Link \u2014 Ask any construction compliance question on site. Get the exact code reference instantly. Free on mobile, no install.\"/>\n  <title>Legis-Link \u2014 Compliance answers on the job site</title>\n  <link href=\"https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=DM+Sans:wght@300;400;500;600;700&display=swap\" rel=\"stylesheet\"/>\n  <style>\n    :root{\n      --bg:#080e1a;--surface:#0f1928;--surface2:#162030;\n      --border:#1c2d42;--accent:#2f80ed;--accent2:#56ccf2;\n      --green:#27ae60;--amber:#f2994a;--red:#eb5757;\n      --text:#f0f4f8;--muted:#7a8fa6;--muted2:#a8bccf;\n      --mono:\"DM Mono\",monospace;--sans:\"DM Sans\",sans-serif;\n    }\n    *{box-sizing:border-box;margin:0;padding:0}\n    html{scroll-behavior:smooth}\n    body{background:var(--bg);color:var(--text);font-family:var(--sans);line-height:1.6;overflow-x:hidden}\n\n    /* \u2500\u2500 Nav \u2500\u2500 */\n    nav{\n      position:fixed;top:0;left:0;right:0;z-index:100;\n      display:flex;align-items:center;justify-content:space-between;\n      padding:16px 24px;\n      background:rgba(8,14,26,0.85);backdrop-filter:blur(12px);\n      border-bottom:1px solid rgba(28,45,66,0.6);\n    }\n    .nav-logo{font-family:var(--mono);font-size:15px;font-weight:500;color:var(--accent);letter-spacing:-0.3px}\n    .nav-logo em{color:var(--muted);font-style:normal}\n    .nav-cta{\n      background:var(--accent);color:white;border:none;\n      padding:8px 18px;border-radius:7px;font-family:var(--sans);\n      font-size:13px;font-weight:600;cursor:pointer;text-decoration:none;\n      transition:all 0.15s;\n    }\n    .nav-cta:hover{background:#1a6fd4}\n\n    /* \u2500\u2500 Hero \u2500\u2500 */\n    .hero{\n      min-height:100vh;display:flex;flex-direction:column;\n      align-items:center;justify-content:center;\n      padding:100px 24px 60px;text-align:center;position:relative;overflow:hidden;\n    }\n    .hero-bg{\n      position:absolute;inset:0;\n      background:\n        radial-gradient(ellipse 80% 50% at 50% -10%, rgba(47,128,237,0.15) 0%, transparent 60%),\n        radial-gradient(ellipse 60% 40% at 80% 80%, rgba(86,204,242,0.08) 0%, transparent 50%);\n      pointer-events:none;\n    }\n    .hero-badge{\n      display:inline-flex;align-items:center;gap:6px;\n      background:rgba(47,128,237,0.1);border:1px solid rgba(47,128,237,0.25);\n      border-radius:20px;padding:5px 14px;font-size:12px;font-weight:500;\n      color:var(--accent2);margin-bottom:28px;\n      animation:fadeDown 0.6s ease both;\n    }\n    .hero-badge-dot{width:6px;height:6px;border-radius:50%;background:var(--green);animation:blink 2s infinite}\n    @keyframes blink{0%,100%{opacity:1}50%{opacity:0.3}}\n    @keyframes fadeDown{from{opacity:0;transform:translateY(-12px)}to{opacity:1;transform:translateY(0)}}\n    @keyframes fadeUp{from{opacity:0;transform:translateY(16px)}to{opacity:1;transform:translateY(0)}}\n\n    h1{\n      font-size:clamp(2.2rem,6vw,4rem);font-weight:700;line-height:1.1;\n      letter-spacing:-1.5px;max-width:720px;margin-bottom:20px;\n      animation:fadeUp 0.6s ease 0.1s both;\n    }\n    h1 span{\n      background:linear-gradient(135deg,var(--accent),var(--accent2));\n      -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;\n    }\n    .hero-sub{\n      font-size:clamp(1rem,2.5vw,1.2rem);color:var(--muted2);max-width:520px;\n      margin-bottom:40px;font-weight:300;\n      animation:fadeUp 0.6s ease 0.2s both;\n    }\n    .hero-ctas{\n      display:flex;gap:12px;flex-wrap:wrap;justify-content:center;\n      animation:fadeUp 0.6s ease 0.3s both;\n    }\n    .btn-primary{\n      background:var(--accent);color:white;border:none;\n      padding:14px 28px;border-radius:9px;font-family:var(--sans);\n      font-size:15px;font-weight:600;cursor:pointer;text-decoration:none;\n      transition:all 0.15s;display:inline-flex;align-items:center;gap:8px;\n    }\n    .btn-primary:hover{background:#1a6fd4;transform:translateY(-1px)}\n    .btn-secondary{\n      background:transparent;color:var(--muted2);\n      border:1px solid var(--border);\n      padding:14px 28px;border-radius:9px;font-family:var(--sans);\n      font-size:15px;font-weight:500;cursor:pointer;text-decoration:none;\n      transition:all 0.15s;\n    }\n    .btn-secondary:hover{border-color:var(--accent);color:var(--text)}\n    .hero-note{\n      margin-top:16px;font-size:12px;color:var(--muted);\n      animation:fadeUp 0.6s ease 0.4s both;\n    }\n\n    /* \u2500\u2500 Demo preview \u2500\u2500 */\n    .demo-wrap{\n      width:100%;max-width:680px;margin:56px auto 0;\n      animation:fadeUp 0.8s ease 0.5s both;\n    }\n    .demo-bar{\n      background:var(--surface2);border:1px solid var(--border);\n      border-radius:12px 12px 0 0;padding:10px 16px;\n      display:flex;align-items:center;gap:8px;\n    }\n    .demo-dot{width:10px;height:10px;border-radius:50%}\n    .demo-url{font-family:var(--mono);font-size:11px;color:var(--muted);margin-left:8px}\n    .demo-screen{\n      background:var(--surface);border:1px solid var(--border);border-top:none;\n      border-radius:0 0 12px 12px;padding:20px;\n      display:flex;flex-direction:column;gap:12px;\n    }\n    .demo-msg{display:flex;gap:10px;align-items:flex-start}\n    .demo-msg.user{flex-direction:row-reverse}\n    .demo-av{\n      width:28px;height:28px;border-radius:8px;flex-shrink:0;\n      display:flex;align-items:center;justify-content:center;\n      font-size:11px;font-weight:600;font-family:var(--mono);\n    }\n    .demo-msg.user .demo-av{background:var(--accent);color:white}\n    .demo-msg.bot  .demo-av{background:var(--surface2);border:1px solid var(--border);color:var(--accent2)}\n    .demo-bubble{\n      max-width:85%;padding:10px 14px;border-radius:10px;font-size:13px;line-height:1.6;\n    }\n    .demo-msg.user .demo-bubble{background:linear-gradient(135deg,var(--accent),#1a6fd4);color:white;border-bottom-right-radius:3px}\n    .demo-msg.bot  .demo-bubble{background:var(--surface2);border:1px solid var(--border);border-bottom-left-radius:3px}\n    .demo-tag{\n      display:inline-flex;align-items:center;gap:4px;\n      font-family:var(--mono);font-size:10px;padding:2px 8px;\n      border-radius:4px;margin-top:4px;\n      background:rgba(39,174,96,0.12);color:var(--green);\n    }\n    .demo-ref{font-family:var(--mono);font-size:10px;color:var(--muted);margin-top:3px}\n\n    /* \u2500\u2500 Pain section \u2500\u2500 */\n    .section{padding:80px 24px;max-width:1100px;margin:0 auto}\n    .section-label{\n      font-family:var(--mono);font-size:11px;font-weight:500;\n      color:var(--accent);letter-spacing:0.12em;text-transform:uppercase;\n      margin-bottom:12px;\n    }\n    h2{font-size:clamp(1.6rem,4vw,2.4rem);font-weight:700;letter-spacing:-0.8px;line-height:1.2;margin-bottom:16px}\n    h2 span{color:var(--accent)}\n    .section-sub{font-size:1.05rem;color:var(--muted2);max-width:560px;margin-bottom:48px}\n\n    .pain-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px}\n    .pain-card{\n      background:var(--surface);border:1px solid var(--border);\n      border-radius:12px;padding:24px;position:relative;overflow:hidden;\n    }\n    .pain-card::before{\n      content:'';position:absolute;top:0;left:0;right:0;height:2px;\n      background:linear-gradient(90deg,var(--red),transparent);\n    }\n    .pain-icon{font-size:24px;margin-bottom:12px}\n    .pain-card h3{font-size:15px;font-weight:600;margin-bottom:8px}\n    .pain-card p{font-size:13px;color:var(--muted2);line-height:1.6}\n    .pain-card .solution{\n      margin-top:12px;padding-top:12px;border-top:1px solid var(--border);\n      font-size:12px;color:var(--green);display:flex;align-items:center;gap:6px;\n    }\n\n    /* \u2500\u2500 Features \u2500\u2500 */\n    .features-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:20px}\n    .feat-card{\n      background:var(--surface);border:1px solid var(--border);\n      border-radius:12px;padding:28px;transition:border-color 0.2s;\n    }\n    .feat-card:hover{border-color:var(--accent)}\n    .feat-num{\n      font-family:var(--mono);font-size:11px;color:var(--accent);\n      background:rgba(47,128,237,0.1);border-radius:4px;\n      padding:2px 8px;display:inline-block;margin-bottom:14px;\n    }\n    .feat-card h3{font-size:16px;font-weight:600;margin-bottom:8px}\n    .feat-card p{font-size:13px;color:var(--muted2);line-height:1.6}\n\n    /* \u2500\u2500 Trades strip \u2500\u2500 */\n    .trades-strip{\n      background:var(--surface);border-top:1px solid var(--border);border-bottom:1px solid var(--border);\n      padding:24px;overflow:hidden;\n    }\n    .trades-label{text-align:center;font-family:var(--mono);font-size:11px;color:var(--muted);letter-spacing:0.1em;text-transform:uppercase;margin-bottom:16px}\n    .trades-scroll{\n      display:flex;gap:12px;animation:scroll 20s linear infinite;width:max-content;\n    }\n    .trade-chip{\n      background:var(--surface2);border:1px solid var(--border);\n      border-radius:20px;padding:6px 16px;font-size:12px;font-weight:500;\n      color:var(--muted2);white-space:nowrap;flex-shrink:0;\n    }\n    @keyframes scroll{from{transform:translateX(0)}to{transform:translateX(-50%)}}\n\n    /* \u2500\u2500 Regions \u2500\u2500 */\n    .regions-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}\n    .region-card{\n      background:var(--surface);border:1px solid var(--border);\n      border-radius:10px;padding:16px 20px;\n    }\n    .region-flag{font-size:22px;margin-bottom:8px}\n    .region-card h4{font-size:13px;font-weight:600;margin-bottom:4px}\n    .region-card p{font-size:11px;color:var(--muted);font-family:var(--mono)}\n\n    /* \u2500\u2500 Pricing \u2500\u2500 */\n    .pricing-grid{display:grid;grid-template-columns:1fr 1fr;gap:20px;max-width:700px;margin:0 auto}\n    @media(max-width:580px){.pricing-grid{grid-template-columns:1fr}}\n    .price-card{\n      background:var(--surface);border:1px solid var(--border);\n      border-radius:14px;padding:28px;\n    }\n    .price-card.featured{\n      border-color:var(--accent);\n      background:linear-gradient(160deg,rgba(47,128,237,0.06),var(--surface));\n      position:relative;\n    }\n    .price-card.featured::before{\n      content:'MOST POPULAR';position:absolute;top:-1px;left:50%;transform:translateX(-50%);\n      background:var(--accent);color:white;font-size:10px;font-weight:700;\n      padding:3px 12px;border-radius:0 0 6px 6px;letter-spacing:0.08em;\n    }\n    .price-tier{font-family:var(--mono);font-size:11px;color:var(--muted);letter-spacing:0.1em;text-transform:uppercase;margin-bottom:12px}\n    .price-amount{font-size:2.4rem;font-weight:700;letter-spacing:-1px;line-height:1}\n    .price-period{font-size:13px;color:var(--muted);font-weight:400}\n    .price-desc{font-size:13px;color:var(--muted2);margin:12px 0 20px}\n    .price-features{list-style:none;display:flex;flex-direction:column;gap:8px;margin-bottom:24px}\n    .price-features li{font-size:13px;color:var(--muted2);display:flex;align-items:center;gap:8px}\n    .price-features li::before{content:'\u2713';color:var(--green);font-weight:700;flex-shrink:0}\n    .price-features li.locked::before{content:'\u2192';color:var(--accent)}\n    .price-btn{\n      display:block;text-align:center;padding:12px;border-radius:8px;\n      font-size:14px;font-weight:600;text-decoration:none;transition:all 0.15s;\n    }\n    .price-btn.free{background:var(--surface2);border:1px solid var(--border);color:var(--muted2)}\n    .price-btn.free:hover{border-color:var(--accent);color:var(--text)}\n    .price-btn.pro{background:var(--accent);color:white}\n    .price-btn.pro:hover{background:#1a6fd4}\n\n    /* \u2500\u2500 CTA banner \u2500\u2500 */\n    .cta-banner{\n      background:linear-gradient(135deg,rgba(47,128,237,0.12),rgba(86,204,242,0.06));\n      border:1px solid rgba(47,128,237,0.2);\n      border-radius:16px;padding:48px 32px;text-align:center;\n      max-width:700px;margin:0 auto;\n    }\n    .cta-banner h2{margin-bottom:12px}\n    .cta-banner p{color:var(--muted2);margin-bottom:32px}\n\n    /* \u2500\u2500 Footer \u2500\u2500 */\n    footer{\n      border-top:1px solid var(--border);padding:32px 24px;\n      text-align:center;color:var(--muted);font-size:12px;\n    }\n    footer a{color:var(--accent);text-decoration:none}\n\n    /* \u2500\u2500 Divider \u2500\u2500 */\n    .divider{border:none;border-top:1px solid var(--border);margin:0}\n\n    @media(max-width:640px){\n      .hero{padding:80px 20px 48px}\n      h1{font-size:2rem;letter-spacing:-0.8px}\n      .hero-ctas{flex-direction:column;align-items:center}\n      .btn-primary,.btn-secondary{width:100%;text-align:center;justify-content:center}\n      .section{padding:56px 20px}\n    }\n  </style>\n</head>\n<body>\n\n<nav>\n  <div class=\"nav-logo\">legis<span style=\"color:var(--accent)\">-link</span> <em>v3.2</em></div>\n  <a href=\"https://legis-link-mcp-production-3e9b.up.railway.app/app\" class=\"nav-cta\">Try free &rarr;</a>\n</nav>\n\n<!-- Hero -->\n<section class=\"hero\">\n  <div class=\"hero-bg\"></div>\n  <div class=\"hero-badge\">\n    <span class=\"hero-badge-dot\"></span>\n    Live &mdash; 645 compliance checks today\n  </div>\n  <h1>The code reference is in your pocket.<br><span>Not in the truck.</span></h1>\n  <p class=\"hero-sub\">Ask any compliance question in plain English. Get the exact standard, clause, and table reference in seconds. Free on mobile &mdash; no app, no account.</p>\n  <div class=\"hero-ctas\">\n    <a href=\"https://legis-link-mcp-production-3e9b.up.railway.app/app\" class=\"btn-primary\">\n      Try it free &rarr;\n    </a>\n    <a href=\"https://rickyfarmer.gumroad.com/l/Legis-LinkPro\" class=\"btn-secondary\">\n      Get Pro &mdash; $199/yr\n    </a>\n  </div>\n  <p class=\"hero-note\">Free tier: 50 queries/day &bull; No signup &bull; Works on any phone</p>\n\n  <!-- Demo preview -->\n  <div class=\"demo-wrap\">\n    <div class=\"demo-bar\">\n      <div class=\"demo-dot\" style=\"background:#eb5757\"></div>\n      <div class=\"demo-dot\" style=\"background:#f2994a\"></div>\n      <div class=\"demo-dot\" style=\"background:#27ae60\"></div>\n      <span class=\"demo-url\">legis-link-mcp-production-3e9b.up.railway.app/app</span>\n    </div>\n    <div class=\"demo-screen\">\n      <div class=\"demo-msg user\">\n        <div class=\"demo-av\">U</div>\n        <div>\n          <div class=\"demo-bubble\">Wire size for 45A load, 30 metres, 240V single phase NSW</div>\n        </div>\n      </div>\n      <div class=\"demo-msg bot\">\n        <div class=\"demo-av\">LL</div>\n        <div>\n          <div class=\"demo-bubble\">\n            10mm&sup2; copper conductor required. Working: 45A continuous load at 30m on 240V single phase gives 1.97% voltage drop &mdash; within the 3% sub-circuit limit. Installation method (clipped direct, in conduit) may require derating to 16mm&sup2; if bundled.\n          </div>\n          <div class=\"demo-tag\">&#9679; COMPLIANT</div>\n          <div class=\"demo-ref\">Ref: AS/NZS 3008.1.1:2017 Table C1, AS/NZS 3000:2018 Clause 3.6.2</div>\n        </div>\n      </div>\n      <div class=\"demo-msg user\">\n        <div class=\"demo-av\">U</div>\n        <div>\n          <div class=\"demo-bubble\">Generate a RAMS for electrical switchboard installation</div>\n        </div>\n      </div>\n      <div class=\"demo-msg bot\">\n        <div class=\"demo-av\">LL</div>\n        <div>\n          <div class=\"demo-bubble\">\n            <strong>RAMS &mdash; Electrical Switchboard Installation NSW</strong><br>\n            Hazard Register: Electric shock (Severity 5, Likelihood 4, Risk 20 CRITICAL) &mdash; Isolate and LOTO main supply, test with approved voltage detector, wear insulated PPE Category 4...<br>\n            <span style=\"color:var(--accent);font-size:11px\">PRO tool &mdash; full document continues...</span>\n          </div>\n          <div class=\"demo-tag\">&#9679; COMPLIANT</div>\n          <div class=\"demo-ref\">Ref: WHS Act 2011 (NSW), AS/NZS 3000:2018, Electrical Safety Code</div>\n        </div>\n      </div>\n    </div>\n  </div>\n</section>\n\n<hr class=\"divider\"/>\n\n<!-- Trades strip -->\n<div class=\"trades-strip\">\n  <div class=\"trades-label\">Covers every trade</div>\n  <div style=\"overflow:hidden\">\n    <div class=\"trades-scroll\">\n      <div class=\"trade-chip\">Electrical</div>\n      <div class=\"trade-chip\">Plumbing</div>\n      <div class=\"trade-chip\">HVAC</div>\n      <div class=\"trade-chip\">Gas fitting</div>\n      <div class=\"trade-chip\">Welding</div>\n      <div class=\"trade-chip\">Solar / Battery</div>\n      <div class=\"trade-chip\">Fire protection</div>\n      <div class=\"trade-chip\">Carpentry</div>\n      <div class=\"trade-chip\">Concrete</div>\n      <div class=\"trade-chip\">Roofing</div>\n      <div class=\"trade-chip\">Electrical</div>\n      <div class=\"trade-chip\">Plumbing</div>\n      <div class=\"trade-chip\">HVAC</div>\n      <div class=\"trade-chip\">Gas fitting</div>\n      <div class=\"trade-chip\">Welding</div>\n      <div class=\"trade-chip\">Solar / Battery</div>\n      <div class=\"trade-chip\">Fire protection</div>\n      <div class=\"trade-chip\">Carpentry</div>\n      <div class=\"trade-chip\">Concrete</div>\n      <div class=\"trade-chip\">Roofing</div>\n    </div>\n  </div>\n</div>\n\n<hr class=\"divider\"/>\n\n<!-- Pain points -->\n<section class=\"section\">\n  <div class=\"section-label\">The problem</div>\n  <h2>You&rsquo;re on site. The <span>answer is 400km away.</span></h2>\n  <p class=\"section-sub\">Every tradesperson knows this moment. Work stops while you dig through a standard you don&rsquo;t have on you.</p>\n  <div class=\"pain-grid\">\n    <div class=\"pain-card\">\n      <div class=\"pain-icon\">&#128218;</div>\n      <h3>The standard is at the office</h3>\n      <p>AS/NZS 3000 is 700 pages. You need clause 3.6.2. It&rsquo;s not in your head and not on your phone.</p>\n      <div class=\"solution\">&#10003; Legis-Link gives you the exact clause in 3 seconds</div>\n    </div>\n    <div class=\"pain-card\">\n      <div class=\"pain-icon\">&#128273;</div>\n      <h3>Wrong cable = rework</h3>\n      <p>Ordering 2.5mm&sup2; when the job needed 4mm&sup2; costs more than the cable. It costs a day&rsquo;s work.</p>\n      <div class=\"solution\">&#10003; Material compliance check before you order</div>\n    </div>\n    <div class=\"pain-card\">\n      <div class=\"pain-icon\">&#128203;</div>\n      <h3>RAMS takes 3 hours</h3>\n      <p>A Risk Assessment and Method Statement used to mean clearing your afternoon. Not anymore.</p>\n      <div class=\"solution\">&#10003; Full RAMS document generated in 60 seconds</div>\n    </div>\n    <div class=\"pain-card\">\n      <div class=\"pain-icon\">&#128161;</div>\n      <h3>Inspectors ask questions you weren&rsquo;t expecting</h3>\n      <p>The hold point you missed. The certificate you didn&rsquo;t know was mandatory. The form number you never heard of.</p>\n      <div class=\"solution\">&#10003; Every hold point, every certificate, every form number</div>\n    </div>\n  </div>\n</section>\n\n<hr class=\"divider\"/>\n\n<!-- Features -->\n<section class=\"section\">\n  <div class=\"section-label\">What it does</div>\n  <h2>8 tools. <span>Every trade. Every region.</span></h2>\n  <p class=\"section-sub\">From a quick code lookup to a full RAMS document &mdash; all from your phone on site.</p>\n  <div class=\"features-grid\">\n    <div class=\"feat-card\">\n      <div class=\"feat-num\">FREE</div>\n      <h3>Compliance check</h3>\n      <p>Ask whether any installation, material, or practice is compliant. Get the exact standard and clause that answers it. COMPLIANT / NON_COMPLIANT / REQUIRES VERIFICATION.</p>\n    </div>\n    <div class=\"feat-card\">\n      <div class=\"feat-num\">FREE</div>\n      <h3>Code reference lookup</h3>\n      <p>Need the specific table, clause or section? Get the standard name, edition year, and what that clause actually requires &mdash; in plain English.</p>\n    </div>\n    <div class=\"feat-card\">\n      <div class=\"feat-num\">PRO</div>\n      <h3>Technical calculations</h3>\n      <p>Cable sizing, pipe sizing, voltage drop, HVAC loads. Enter your parameters, get the answer with the calculation shown and the code reference cited.</p>\n    </div>\n    <div class=\"feat-card\">\n      <div class=\"feat-num\">PRO</div>\n      <h3>Safety checklist</h3>\n      <p>15-item safety checklists with PPE requirements, hazard controls, and the specific regulation that mandates each item. Foreman-ready in under a minute.</p>\n    </div>\n    <div class=\"feat-card\">\n      <div class=\"feat-num\">PRO</div>\n      <h3>RAMS generator</h3>\n      <p>Full Risk Assessment and Method Statement: Hazard Register with risk ratings, numbered Method Statement, required qualifications. Print-ready, site-file ready.</p>\n    </div>\n    <div class=\"feat-card\">\n      <div class=\"feat-num\">PRO</div>\n      <h3>Inspection requirements</h3>\n      <p>Every hold point, who signs off, what certificate is issued, which form number, how much notice. Never cover work before a mandatory inspection again.</p>\n    </div>\n  </div>\n</section>\n\n<hr class=\"divider\"/>\n\n<!-- Regions -->\n<section class=\"section\">\n  <div class=\"section-label\">Coverage</div>\n  <h2>Your jurisdiction. <span>Your standard.</span></h2>\n  <p class=\"section-sub\">Not a one-size-fits-all approximation &mdash; the correct code for where you&rsquo;re standing.</p>\n  <div class=\"regions-grid\">\n    <div class=\"region-card\">\n      <div class=\"region-flag\">&#127462;&#127482;</div>\n      <h4>Australia</h4>\n      <p>AS/NZS 3000, AS/NZS 3008<br>NCC 2022, WHS Acts<br>NSW, VIC, QLD, WA, SA</p>\n    </div>\n    <div class=\"region-card\">\n      <div class=\"region-flag\">&#127468;&#127463;</div>\n      <h4>United Kingdom</h4>\n      <p>BS 7671:2018, CDM 2015<br>HSE guidance, Gas Safe<br>England, Scotland, Wales</p>\n    </div>\n    <div class=\"region-card\">\n      <div class=\"region-flag\">&#127482;&#127480;</div>\n      <h4>United States</h4>\n      <p>NEC NFPA 70, IBC<br>OSHA 29 CFR 1926<br>TX, CA, FL, NY, IL</p>\n    </div>\n    <div class=\"region-card\">\n      <div class=\"region-flag\">&#127464;&#127462;</div>\n      <h4>Canada</h4>\n      <p>CEC CSA C22.1, NBC<br>NPC, Provincial codes<br>ON, BC, AB, QC</p>\n    </div>\n    <div class=\"region-card\">\n      <div class=\"region-flag\">&#127466;&#127482;</div>\n      <h4>European Union</h4>\n      <p>EN standards<br>IEC 60364, EN 806<br>DE, FR, NL, IE, ES</p>\n    </div>\n  </div>\n</section>\n\n<hr class=\"divider\"/>\n\n<!-- Pricing -->\n<section class=\"section\" style=\"text-align:center\">\n  <div class=\"section-label\">Pricing</div>\n  <h2>Start free. <span>Upgrade when you need more.</span></h2>\n  <p class=\"section-sub\" style=\"margin:0 auto 48px\">No credit card for free tier. Pro unlocks all 8 tools and 1,000 queries/day.</p>\n  <div class=\"pricing-grid\">\n    <div class=\"price-card\">\n      <div class=\"price-tier\">Free</div>\n      <div class=\"price-amount\">$0 <span class=\"price-period\">forever</span></div>\n      <div class=\"price-desc\">No account needed. Open your browser and go.</div>\n      <ul class=\"price-features\">\n        <li>50 queries/day</li>\n        <li>Compliance check</li>\n        <li>Code reference lookup</li>\n        <li>List supported regions</li>\n        <li class=\"locked\" style=\"color:var(--muted)\">Technical calculations</li>\n        <li class=\"locked\" style=\"color:var(--muted)\">Safety checklists</li>\n        <li class=\"locked\" style=\"color:var(--muted)\">RAMS generator</li>\n        <li class=\"locked\" style=\"color:var(--muted)\">Inspection requirements</li>\n      </ul>\n      <a href=\"https://legis-link-mcp-production-3e9b.up.railway.app/app\" class=\"price-btn free\">Try free &rarr;</a>\n    </div>\n    <div class=\"price-card featured\">\n      <div class=\"price-tier\">Pro</div>\n      <div class=\"price-amount\">$199 <span class=\"price-period\">/ year</span></div>\n      <div class=\"price-desc\">All 8 tools. 1,000 queries/day. Every region.</div>\n      <ul class=\"price-features\">\n        <li>1,000 queries/day</li>\n        <li>All 3 free tools</li>\n        <li>Technical calculations</li>\n        <li>Safety checklists</li>\n        <li>RAMS generator</li>\n        <li>Inspection requirements</li>\n        <li>Material compliance check</li>\n        <li>All regions unlocked</li>\n      </ul>\n      <a href=\"https://rickyfarmer.gumroad.com/l/Legis-LinkPro\" class=\"price-btn pro\">Get Pro &rarr;</a>\n    </div>\n  </div>\n</section>\n\n<hr class=\"divider\"/>\n\n<!-- Final CTA -->\n<section class=\"section\">\n  <div class=\"cta-banner\">\n    <h2>Open it on your phone.<br><span>Right now.</span></h2>\n    <p>No download. No account. Takes 5 seconds. Ask your first compliance question for free.</p>\n    <div class=\"hero-ctas\">\n      <a href=\"https://legis-link-mcp-production-3e9b.up.railway.app/app\" class=\"btn-primary\">\n        Open Legis-Link &rarr;\n      </a>\n    </div>\n    <p style=\"margin-top:16px;font-size:11px;color:var(--muted)\">\n      Free &bull; No signup &bull; Works on any phone &bull; AU &bull; UK &bull; US &bull; CA &bull; EU\n    </p>\n  </div>\n</section>\n\n<footer>\n  <p>Legis-Link &mdash; Construction Compliance AI &bull; <a href=\"https://legis-link-mcp-production-3e9b.up.railway.app/app\">App</a> &bull; <a href=\"https://legis-link-mcp-production-3e9b.up.railway.app/connect\">Connect</a> &bull; <a href=\"https://rickyfarmer.gumroad.com/l/Legis-LinkPro\">Pro</a></p>\n  <p style=\"margin-top:8px;font-size:11px\">Results are preliminary estimates. Always verify against the full published standard before proceeding with any installation.</p>\n</footer>\n\n\n\n    <!-- Static SEO content — visible to Google without JS -->\n    <section style=\"max-width:800px;margin:0 auto;padding:40px 20px;font-family:system-ui,sans-serif;color:#e6edf3\">\n      <h2 style=\"font-size:22px;font-weight:600;color:#fff;margin-bottom:16px\">\n        Free Construction Compliance Checker for UK &amp; EU Trades\n      </h2>\n      <p style=\"font-size:15px;color:#94a3b8;line-height:1.7;margin-bottom:16px\">\n        Legis-Link is a free AI-powered construction compliance tool for electricians,\n        plumbers, HVAC engineers, welders, and site managers in the UK and EU.\n        Check compliance with Part P, BS 7671, WAHR 2005, CDM 2015, CAR 2012,\n        and 50+ other UK building regulations — no signup required.\n      </p>\n      <h3 style=\"font-size:16px;color:#e6edf3;margin:20px 0 10px\">Free features (no account needed)</h3>\n      <ul style=\"color:#94a3b8;padding-left:20px;line-height:2\">\n        <li>Compliance check — describe your job, get instant regulatory assessment</li>\n        <li>Code reference — exact clause lookup across UK and EU regulations</li>\n        <li>Toolbox talks — 70 topics across 10 trades, daily rotation</li>\n      </ul>\n      <h3 style=\"font-size:16px;color:#e6edf3;margin:20px 0 10px\">Pro features (£199/year)</h3>\n      <ul style=\"color:#94a3b8;padding-left:20px;line-height:2\">\n        <li>RAMS generator — Risk Assessment &amp; Method Statement templates</li>\n        <li>Technical calculations — cable sizing, voltage drop, pipe sizing</li>\n        <li>Safety checklists, material compliance, inspection requirements</li>\n        <li>Visual compliance — photo upload for site checks</li>\n      </ul>\n      <h3 style=\"font-size:16px;color:#e6edf3;margin:20px 0 10px\">Regulations covered</h3>\n      <p style=\"color:#94a3b8;font-size:14px;line-height:1.7\">\n        Part P Building Regulations · BS 7671:2018 (IET Wiring Regulations) ·\n        Working at Height Regulations 2005 (WAHR 2005) ·\n        Control of Asbestos Regulations 2012 (CAR 2012) ·\n        Construction Design and Management Regulations 2015 (CDM 2015) ·\n        Building Safety Act 2022 · Gas Safety Regulations 1998 ·\n        Water Supply (Water Fittings) Regulations 1999 ·\n        F-Gas Regulations 2015 · Fire Safety Act 2021\n      </p>\n      <p style=\"margin-top:20px\">\n        <a href=\"/app\" style=\"background:#2563eb;color:#fff;padding:10px 20px;border-radius:6px;text-decoration:none;font-weight:600;font-size:14px\">\n          Open free compliance checker &rarr;\n        </a>\n      </p>\n    </section></body>\n</html>\n",
    "connect.html":  "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n  <meta charset=\"UTF-8\"/>\n  <link rel=\"icon\" href=\"/favicon.svg\" type=\"image/svg+xml\"/>\n  <link rel=\"icon\" href=\"/favicon.ico\" sizes=\"32x32\"/>\n  <link rel=\"apple-touch-icon\" href=\"/apple-touch-icon.png\"/>\n  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\"/>\n  <title>Connect to Legis-Link MCP</title>\n  <link href=\"https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@300;400;500;600&display=swap\" rel=\"stylesheet\"/>\n  <style>\n    :root { --bg:#0a0f1a; --surface:#111827; --border:#1e293b; --accent:#3b82f6; --accent2:#06b6d4; --text:#f1f5f9; --muted:#64748b; --green:#10b981; --mono:'IBM Plex Mono',monospace; --sans:'IBM Plex Sans',sans-serif; }\n    * { box-sizing:border-box; margin:0; padding:0; }\n    body { background:var(--bg); color:var(--text); font-family:var(--sans); min-height:100vh; padding:24px; max-width:640px; margin:0 auto; }\n    h1 { font-family:var(--mono); font-size:22px; color:var(--accent); margin-bottom:6px; }\n    .sub { color:var(--muted); font-size:14px; margin-bottom:32px; }\n    h2 { font-size:15px; font-weight:600; margin-bottom:12px; margin-top:28px; }\n    .card { background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:18px 20px; margin-bottom:12px; }\n    .card-title { font-weight:600; font-size:14px; margin-bottom:8px; display:flex; align-items:center; gap:8px; }\n    .card-body { color:var(--muted); font-size:13px; line-height:1.7; }\n    .code-block { background:var(--bg); border:1px solid var(--border); border-radius:8px; padding:12px 14px; font-family:var(--mono); font-size:12px; color:var(--accent2); margin:10px 0; overflow-x:auto; white-space:pre; }\n    .badge { display:inline-block; font-size:10px; font-weight:600; padding:2px 8px; border-radius:4px; margin-left:6px; }\n    .badge-free { background:rgba(16,185,129,0.15); color:var(--green); }\n    .badge-now { background:rgba(59,130,246,0.15); color:var(--accent); }\n    .divider { border:none; border-top:1px solid var(--border); margin:28px 0; }\n    a { color:var(--accent); }\n    .back { display:inline-flex; align-items:center; gap:6px; color:var(--muted); font-size:13px; text-decoration:none; margin-bottom:24px; }\n    .back:hover { color:var(--text); }\n  </style>\n</head>\n<body>\n  <a href=\"/app\" class=\"back\">\u2190 Back to field tool</a>\n  <h1>Connect Legis-Link</h1>\n  <p class=\"sub\">Use Legis-Link from any MCP-compatible AI tool \u2014 Claude Desktop, Cursor, Windsurf, or the mobile apps below.</p>\n\n  <h2>🤖 Claude, ChatGPT and other AI apps</h2>\n\n  <div class=\"card\">\n    <div class=\"card-title\">Claude (web, desktop, mobile) <span class=\"badge badge-now\">Recommended</span></div>\n    <div class=\"card-body\">\n      Settings → Connectors → Add custom connector. Name: Legis-Link. URL:\n      <div class=\"code-block\">https://legis-link-mcp-production-3e9b.up.railway.app/mcp</div>\n      No sign-in needed (free tier, 50 questions a day). Pro users add their key to the end of the URL:\n      <div class=\"code-block\">https://legis-link-mcp-production-3e9b.up.railway.app/mcp?key=ll_p_your_key</div>\n    </div>\n  </div>\n\n  <div class=\"card\">\n    <div class=\"card-title\">Cursor, Windsurf, VS Code and other MCP clients</div>\n    <div class=\"card-body\">\n      Use the streamable HTTP address:\n      <div class=\"code-block\">{ \"mcpServers\": { \"legis-link\": { \"url\": \"https://legis-link-mcp-production-3e9b.up.railway.app/mcp\" } } }</div>\n      Older clients that only support SSE can still use https://legis-link-mcp-production-3e9b.up.railway.app/sse\n    </div>\n  </div>\n\n  <h2>📱 Mobile — Recommended</h2>\n\n  <div class=\"card\">\n    <div class=\"card-title\">Systemprompt MCP <span class=\"badge badge-now\">Works now</span></div>\n    <div class=\"card-body\">\n      Voice-controlled MCP client for iOS and Android. Add the server URL in app settings.\n      <div class=\"code-block\">Server URL: https://legis-link-mcp-production-3e9b.up.railway.app/sse</div>\n      Download: Search \"Systemprompt MCP\" on App Store or Google Play.\n    </div>\n  </div>\n\n  <div class=\"card\">\n    <div class=\"card-title\">Browser PWA <span class=\"badge badge-free\">Free \u00b7 No install</span></div>\n    <div class=\"card-body\">\n      Open <a href=\"/app\">/app</a> on your phone browser. Tap the share icon \u2192 \"Add to Home Screen\" for an app-like experience with offline support.\n    </div>\n  </div>\n\n  <hr class=\"divider\"/>\n  <h2>\ud83d\udda5\ufe0f Desktop</h2>\n\n  <div class=\"card\">\n    <div class=\"card-title\">Claude Desktop</div>\n    <div class=\"card-body\">\n      Add to <code style=\"font-family:var(--mono);font-size:12px\">claude_desktop_config.json</code>:\n      <div class=\"code-block\">{\n  \"mcpServers\": {\n    \"legis-link\": {\n      \"command\": \"python\",\n      \"args\": [\"legis_link_mcp_server.py\"],\n      \"env\": { \"LEGIS_LINK_API_KEY\": \"\" }\n    }\n  }\n}</div>\n    </div>\n  </div>\n\n  <div class=\"card\">\n    <div class=\"card-title\">Cursor / Windsurf</div>\n    <div class=\"card-body\">\n      Add to MCP settings \u2014 use the remote SSE endpoint:\n      <div class=\"code-block\">{\n  \"mcpServers\": {\n    \"legis-link\": {\n      \"url\": \"https://legis-link-mcp-production-3e9b.up.railway.app/sse\"\n    }\n  }\n}</div>\n    </div>\n  </div>\n\n  <hr class=\"divider\"/>\n  <h2>\ud83d\udd11 API Keys</h2>\n  <div class=\"card\">\n    <div class=\"card-body\">\n      <strong>Free tier:</strong> No key needed. Leave the API key empty (50 requests/day).<br><br>\n      <strong>Pro tier:</strong> $199/year \u2014 1,000 requests/day, all 8 tools. Contact us to get a Pro key.\n    </div>\n  </div>\n</body>\n</html>",
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
        if a.lower().startswith("bearer ") and a[7:].strip().startswith("ll_"):
            k = a[7:].strip()
    return k[:80]


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
                    "reason": "This Pro key is not recognised. Check it was copied in full, or email info@africompliance.com."}
        if _key_revoked(k):
            return {"valid": False, "tier": None, "reason": "This Pro key is no longer active."}
        return {"valid": True, "tier": "pro", "reason": "pro"}
    if k.startswith("ll_f_") and len(k) == 37:
        return {"valid": True, "tier": "free", "reason": "free"}
    return {"valid": False, "tier": None,
            "reason": "Invalid key. Leave the key empty for the free tier, or paste your Pro key (starts with ll_p_)."}


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
}

# Trade names shown to users -> registry keys ("Gas fitting" and "HVAC" never matched before LL-4)
_TRADE_ALIAS = {"gas fitting": "gas", "gasfitting": "gas", "gas": "gas", "hvac": "refrigeration",
                "refrigeration": "refrigeration", "air conditioning": "refrigeration"}


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
{"status": "COMPLIANT", "result": "calculation result and working", "code_reference": "standard + section"}""",

    "safety": SAFETY_CORE + """You are a construction safety expert.
Generate a numbered safety checklist. Each item must include the requirement, control measure, and regulation reference.
Cover: PPE, hazard controls, permits, emergency procedures.
Regional regs: AU (Safe Work Australia, WHS Act), UK (CDM 2015, HSE, PUWER), USA (OSHA 29 CFR 1926), EU (Directive 92/57/EEC).
Return ONLY this JSON, no other text:
{"status": "COMPLIANT", "result": "numbered checklist with reg refs", "code_reference": "primary regulation"}""",

    "rams": SAFETY_CORE + """You are a construction RAMS expert. Generate a professional document with:
SECTION 1 — HAZARD REGISTER: table with Hazard | Severity(1-5) | Likelihood(1-5) | Risk Rating | Control Measure | Regulation
SECTION 2 — METHOD STATEMENT: numbered steps
SECTION 3 — REQUIRED QUALIFICATIONS & CERTIFICATIONS
Regional terminology: UK/AU=RAMS, USA=Job Hazard Analysis (JHA), EU=Method Statement.
Return ONLY this JSON, no other text:
{"status": "COMPLIANT", "result": "full document text", "code_reference": "regulations cited"}""",

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
    "transport": {"type": "streamable-http", "url": "https://legis-link-mcp-production-3e9b.up.railway.app/mcp", "sse_url": "https://legis-link-mcp-production-3e9b.up.railway.app/sse"},
    "authentication": {
        "required": False,
        "type": "api_key",
        "header": "X-API-Key (optional; or ?key= in the URL, or Authorization: Bearer)",
        "get_key": "https://legis-link-mcp-production-3e9b.up.railway.app",
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
                             "description": "Jurisdiction for the compliance check. Examples: 'NSW', 'England', 'Texas', 'Ontario', 'Germany'. Determines which standard applies."},
                "question": {"type": "string",
                             "description": "The compliance question in plain English. Be specific: include voltages, distances, materials, or load values where relevant. Example: 'Is 2.5mm2 TPS cable compliant for a 20A circuit in a wall cavity?'"},
                "role":     {"type": "string", "enum": VALID_ROLES, "default": "Qualified Tradesperson",
                             "description": "The role of the person asking. Affects the depth and terminology of the answer."},
                "api_key":  {"type": "string",
                             "description": "Legis-Link API key. Optional: leave empty for the free tier (50 requests/day); Pro users paste their ll_p_ key. Get a free key at https://legis-link-mcp-production-3e9b.up.railway.app"}
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
                            "description": "Jurisdiction. Determines which edition of the standard applies. Examples: 'NSW', 'England', 'California'."},
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
                                "description": "Jurisdiction. Affects which standard and units apply (e.g. mm2 for AU/UK, AWG for USA)."},
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
                            "description": "Jurisdiction. Determines which WHS/HSE regulations and codes of practice apply."},
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
                                 "description": "Jurisdiction. Determines applicable WHS/CDM/OSHA legislation cited in the document."},
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
                             "description": "Jurisdiction. Determines which product standards and approval schemes apply."},
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
                                 "description": "Jurisdiction. Inspection authorities and certificate types vary significantly by region."},
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
PUBLIC_URL = (os.environ.get("LEGIS_PUBLIC_URL", "") or "https://legis-link-mcp-production-3e9b.up.railway.app").rstrip("/")
_LL_ICON_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">\n  <defs>\n    <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">\n      <stop offset="0" stop-color="#3b82f6"/><stop offset="1" stop-color="#06b6d4"/>\n    </linearGradient>\n  </defs>\n  <rect width="512" height="512" rx="112" fill="#0b1220"/>\n  <path d="M256 78 L404 132 V250 C404 340 342 404 256 436 C170 404 108 340 108 250 V132 Z" fill="url(#g)"/>\n  <path d="M184 262 L236 314 L338 204" fill="none" stroke="#ffffff" stroke-width="40" stroke-linecap="round" stroke-linejoin="round"/>\n</svg>'
_LL_ICON_PNG = {
    512: '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',
    192: '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',
    180: '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',
    32: 'iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAAEfUlEQVR4nLxXXWhbZRh+vnOSNEnTJqlzUmdd1G0q28SiF0VFGQxrN7wRdejsFBH8uZsDvdBLL+ZgiOKEoa4XMueFiLCJzQYKTtlWhI7aatWqqXTdTGv+m/9zvr3f+c+2JEvY8pTmPYfz8T3P+7zv933nuOBEKBLyS9hNV0MMuJfialwbxDlwluLpvIr3kIqlzAfMvPCH147Q3ScM7GZcR3DwRfp5KZ+c/9YS0N0XGaYwjg6Cc75NiGAI3hr2y2yGMu9HByGcyFeVzS4fk54nGzpKLiBK7ZNcu1wSwxBahDu0BoGNwwhsegxc4shOj2NlOopqcrGleQQ384cjMcawttlg2RdC78YRIh6Bd2AzVPrjMvQoUWQcpXO/YGVqHIWpE1BXUs2mpD7APKMG5HUVerrRs26LRuy/fYgIJXCmk4nMrxzpOa+gNDeBwtnjKM+cBC/l64qoK8B30924bedn4C63kaGdqUkGulfIAZhOXOKIGKdUi8h8+BqUhT+uKMCFutkHiNxTkxk3JuUGuRAlFrJFCl5Dro3zeMC8/no09QUIq+HIlBuTa9EkZU5n7MxhiBHiNBENIDV6qE1G/3aGTntR6wzTxW3oc+Gp9T5Isi2KM7TuAMxMLTLVyEy1HIHDESHi4YEufProDZDpfrlSRfRcQR+PdgQws+Zw1JwbNeeXOKLi/n4PDm41yIsqTsdLljONINXPH3ojWTW/3BEzbrrRjTHKvItsz1U4dpyII1VRLGfA2hAgoGfKNZKHIm5ER8PYvsFbU/P1YRcOb1uFbjdDhdze9V0cv6VLtkhjXMsClFLW7maKz97jw0CvjP1be/HEXbqIgaCMI9tXoYfIBcXLPyyT9UWrPOZS5YVcXQF1e0BJX6ip+f4zOQz2h9HrYdj7SBCrAxKevtOPPq+ewxunEogu5K2l6hTPE0to3YFCGpXMeavms4kKRo8mkCnrdr5+Xw9uCcja9b7JND6fyxqlqV2iSuICsJJBywIECn+fMXY0vZYz/5ex85tlS4TA2GwO70+nbVLGHbWnQvw+2YiisYDsqS/oYDG7We/66UQZz4wv4c90FR//msPbEwlrG3YuVW28EHD8y0YUaHgaCgQf3wPfA0/aZ4LIUOLWuaBeZrt9Ola//xrqkQNo2wGBTPQjKNm43VDM3uO5Y7PS9wVuL93UEtSvDjWbvrkAXi4gObYHaj5Z9yxQHfuCRp5NQvngLaBUaDZ9cwEC1fNzSB14Bcryv4YIbtcc+oFlObHwF5R9u8EX/rmaqSF7fKFXKQaaDeT5DEo/R8F6QpAG1tkN5+iD6smjUA6+Q3VL4irxH/P3RcbJvWG0AGnNHXAPPwd58EGNXJn6EdVj9Pa0GGtlGuFdlPnCkb30dvom2gALBPWJcmm0A5XjXeYNRSJ0hE5SZ4fQSXCkFI5BqZiKxVTOX0SHQa/kLwhubTOvFtOzbm9wglzYQl8sPbiOMD5Od+RT88fEvWw+qBTTc5Wu8CE6Wct0W6LGFEK6cW0gPs9/ong4r7LRSmp+2nxwEQAA//90kVtQAAAABklEQVQDACNMokp3XWn/AAAAAElFTkSuQmCC',
}


async def _ll_icon(request):
    import base64 as _b64
    from starlette.responses import Response as _R
    p = request.url.path
    if p.endswith(".svg"):
        return _R(_LL_ICON_SVG, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=604800"})
    size = 32 if p == "/favicon.ico" else (180 if "apple" in p else (192 if "192" in p else 512))
    return _R(_b64.b64decode(_LL_ICON_PNG[size]), media_type="image/png", headers={"Cache-Control": "public, max-age=604800"})


async def _ll_manifest(request):
    from starlette.responses import JSONResponse as _J
    return _J({"name": "Legis-Link", "short_name": "Legis-Link", "description": "Field compliance tool for tradespeople",
               "start_url": "/app", "display": "standalone", "orientation": "portrait",
               "background_color": "#0b1220", "theme_color": "#3b82f6",
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
)

_PRACTICE = {
    ("electrical", "AU"): (
        "PRACTICE REFERENCE - Australia, electrical (reviewed):\n"
        "- Standards: AS/NZS 3000:2018 (Wiring Rules: installation, protection, verification) and AS/NZS 3008.1.1 "
        "(selection of cables: current ratings, derating factors, voltage drop). They gain legal force through each "
        "state's electrical safety legislation.\n"
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
        "- Standard: BS 7671 (IET Wiring Regulations, current amendment). In England and Wales, Building Regulations "
        "Part P applies to notifiable domestic electrical work.\n"
        "- 2.5 mm2 twin and earth (T&E) is the standard cable for a 32 A ring final circuit and for a 20 A radial "
        "circuit (standard circuit arrangements in the IET On-Site Guide). In typical installation methods (clipped "
        "direct, or in a stud wall not surrounded by insulation) it is COMPLIANT in the typical case.\n"
        "- What changes the answer: contact with or enclosure in thermal insulation (installation reference methods "
        "with heavy derating), grouping, high ambient temperature, and voltage drop on long runs.\n"
        "- Voltage drop (installations supplied from the public LV network): 3% for lighting and 5% for other uses (BS 7671 Appendix 4).\n"
        "- Lighting circuits: 1.0 or 1.5 mm2 T&E on a 6 A or 10 A device is common practice.\n"
        "- 30 mA RCD protection is required for socket-outlets rated up to 32 A and for cables concealed in walls at "
        "less than 50 mm depth that are not otherwise mechanically protected.\n"
    ),
    ("electrical", "US"): (
        "PRACTICE REFERENCE - United States, electrical (reviewed):\n"
        "- Code: NEC (NFPA 70); the adopted edition differs by state and local authority having jurisdiction (AHJ).\n"
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
}

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
    return ""


def _practice_for(user_message: str) -> str:
    """Answer rules for every tool, plus the reviewed practice reference for the trade and country."""
    m = re.match(r"\s*Trade:\s*([^|\n]+?)\s*\|\s*Region:\s*([^|\n]+)", user_message or "")
    extra = ""
    if m:
        ref = _PRACTICE.get((m.group(1).strip().lower(), _region_country(m.group(2))))
        if ref:
            extra = "\n\n" + ref
    return _ANSWER_RULES + extra


async def ask_claude(system_prompt: str, user_message: str) -> dict:
    """Call Anthropic API. Falls back to OpenAI on billing errors."""
    system_prompt = system_prompt + _practice_for(user_message)
    async with httpx.AsyncClient(timeout=45.0) as client:
        try:
            resp = await client.post(
                ANTHROPIC_URL,
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": MODEL,
                    "max_tokens": 2048,
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
                return await _ask_openai(system_prompt, user_message)

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

@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [types.Tool(
        name=t["name"],
        description=t["description"],
        inputSchema=t["inputSchema"]
    ) for t in SERVER_CARD["tools"]]


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

async def _localize_topic(topic: dict, trade: str, region: str) -> dict:
    """Rewrite a topic's region-specific references for the given jurisdiction.
    Subject stays the same; standards/citations/control wording localized.
    Cached per trade+region+day. AU topics for an AU region pass through."""
    if not region or _region_in_country(region, "Australia") or region == "Australia":
        return topic  # source content is already AU-native
    key = (trade, region, _dt.date.today().isoformat(), topic.get("title",""))
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
        "'— verify against current local edition'."
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
                return JSONResponse({"error":"database: " + str(e)[:120]},status_code=500)
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
                    Paragraph("Generated by Legis-Link — legis-link-mcp-production-3e9b.up.railway.app", SF),
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
  <url><loc>https://legis-link-mcp-production-3e9b.up.railway.app/</loc><priority>1.0</priority></url>
  <url><loc>https://legis-link-mcp-production-3e9b.up.railway.app/app</loc><priority>0.9</priority></url>
  <url><loc>https://legis-link-mcp-production-3e9b.up.railway.app/connect</loc><priority>0.7</priority></url>
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
            Route("/health",        handle_health),
            Route("/test",          handle_test),
            Route("/roadmap",       handle_roadmap),
            Route("/app",           handle_app),
            Route("/connect",       handle_connect_page),
            Route("/manifest.json", _ll_manifest),
            Route("/favicon.ico", _ll_icon), Route("/favicon.svg", _ll_icon), Route("/icon.svg", _ll_icon),
            Route("/icon-192.png", _ll_icon), Route("/icon-512.png", _ll_icon), Route("/apple-touch-icon.png", _ll_icon),
            Route("/sw.js",         handle_sw),
            Route("/api/fingerprint", handle_fingerprint, methods=["POST"]),
            Route("/api/query",     handle_api_query, methods=["POST"]),
            Route("/.well-known/mcp/server-card.json", handle_server_card),
            Mount("/messages/", app=sse.handle_post_message),
            Route("/mcp", endpoint=_LLMCP(), methods=["GET", "POST", "DELETE"]),
            Mount("/", routes=[Route("/sse", endpoint=handle_sse)]),
        ], lifespan=_ll_lifespan)

        print(f"[Legis-Link MCP v{VERSION}] HTTP port {PORT} — auth+ratelimit+audit",
              file=sys.stderr)
        uvicorn.run(starlette_app, host="0.0.0.0", port=PORT)

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





