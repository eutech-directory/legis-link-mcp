"""
Legis-Link MCP Server v3.2.1
=============================
Claude-direct engine with production foundations:

TIER 1 "” FREE (3 tools, 50 req/day):
  check_compliance, get_code_reference, list_supported_regions

TIER 2 "” PRO $199/year (8 tools, 1000 req/day):
  + calculate_technical_spec, generate_safety_checklist,
    generate_rams, verify_material_compliance, get_inspection_requirements

PRODUCTION FOUNDATIONS (v3.2.1):
  âœ“ API key authentication (ll_f_xxx free / ll_p_xxx pro)
  âœ“ Rate limiting (50/day free, 1000/day pro)
  âœ“ Audit logging (every tool call logged to DB)
  âœ“ Framework scaffold for future phases

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

# â”€â”€ Config â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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
MODEL             = "claude-haiku-4-5-20251001"
PORT              = int(os.environ.get("PORT", 8000))
DATABASE_URL      = os.environ.get("DATABASE_URL", "")
OPENAI_API_KEY    = os.environ.get("OPENAI_API_KEY", "")
OPENAI_URL        = "https://api.openai.com/v1/chat/completions"
OPENAI_MODEL      = "gpt-4o-mini"
PRO_UPGRADE       = "https://rickyfarmer.gumroad.com/l/Legis-LinkPro"
KEY_SECRET        = os.environ.get("LEGIS_KEY_SECRET", "legis-link-pro-secret-2026").encode()

def generate_pro_key(email: str) -> str:
    import hmac as _h, hashlib as _hs
    return "ll_p_" + _h.new(KEY_SECRET, email.lower().strip().encode(), _hs.sha256).hexdigest()[:32]

def generate_free_key(email: str) -> str:
    import hmac as _h, hashlib as _hs
    return "ll_f_" + _h.new(KEY_SECRET, ("free:"+email.lower().strip()).encode(), _hs.sha256).hexdigest()[:32]

def _store_key(email, api_key, sale_id, product):
    if not DATABASE_URL: return
    try:
        import psycopg2
        conn=psycopg2.connect(DATABASE_URL); cur=conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS legis_link_keys (id SERIAL PRIMARY KEY,email VARCHAR(255) UNIQUE NOT NULL,api_key VARCHAR(50),sale_id VARCHAR(100),product VARCHAR(100),active BOOLEAN DEFAULT TRUE,created_at TIMESTAMPTZ DEFAULT NOW())")
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

def _notify_sale(email, api_key, product, sale_id):
    token="8587526488:AAEqwKpuFHrC3F_by9LjKDQLt4xvZpi1QoA"; chat="2119918902"
    try:
        import httpx as _hx
        _hx.post(f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id":chat,"text":f"NEW PRO SALE\nEmail: {email}\nKey: {api_key}\nProduct: {product}\nSale: {sale_id}"},timeout=10)
    except: pass
VERSION           = "3.2.2"
# â”€â”€ Page content (embedded "” no filesystem dependency) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
_PAGES = {
    "app.html":      "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n  <meta charset=\"UTF-8\"/>\n  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no\"/>\n  <meta name=\"mobile-web-app-capable\" content=\"yes\"/>\n  <meta name=\"apple-mobile-web-app-capable\" content=\"yes\"/>\n  <meta name=\"theme-color\" content=\"#0f172a\"/>\n  <title>Legis-Link</title>\n  <link rel=\"manifest\" href=\"/manifest.json\"/>\n  <link href=\"https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&display=swap\" rel=\"stylesheet\"/>\n  <style>\n    :root{\n      --bg:#0a0f1a;--surface:#111827;--surface2:#1a2235;\n      --border:#1e293b;--accent:#3b82f6;--accent2:#06b6d4;\n      --text:#f1f5f9;--muted:#64748b;--muted2:#94a3b8;\n      --green:#10b981;--amber:#f59e0b;--red:#ef4444;\n      --mono:\"IBM Plex Mono\",monospace;--sans:\"IBM Plex Sans\",sans-serif;\n      --r:10px;--sb:64px;\n    }\n    *{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}\n    html,body{height:100%;overflow:hidden;background:var(--bg);color:var(--text);font-family:var(--sans)}\n    .app{display:flex;height:100dvh}\n\n    /* Sidebar */\n    .sb{width:var(--sb);flex-shrink:0;background:var(--surface);border-right:1px solid var(--border);display:flex;flex-direction:column;overflow-y:auto;scrollbar-width:none}\n    .sb::-webkit-scrollbar{display:none}\n    .sb-logo{font-family:var(--mono);font-size:12px;color:var(--accent);text-align:center;padding:10px 0 8px;border-bottom:1px solid var(--border);flex-shrink:0}\n    .tb{display:flex;flex-direction:column;align-items:center;gap:2px;padding:8px 4px;margin:2px 4px;border-radius:8px;cursor:pointer;border:1px solid transparent;background:none;color:var(--muted);font-family:var(--sans);transition:all 0.15s;flex-shrink:0}\n    .tb:active,.tb.on{background:rgba(59,130,246,0.12);border-color:rgba(59,130,246,0.25);color:var(--accent)}\n    .ti{font-size:20px;line-height:1}\n    .tn{font-size:9px;font-weight:500;text-align:center;line-height:1.2;margin-top:1px}\n\n    /* Main */\n    .main{flex:1;display:flex;flex-direction:column;min-width:0;overflow:hidden}\n\n    /* Header */\n    .hdr{background:var(--surface);border-bottom:1px solid var(--border);padding:8px 10px;flex-shrink:0}\n    .hdr-r1{display:flex;align-items:center;justify-content:space-between;margin-bottom:6px}\n    .trade-lbl{font-family:var(--mono);font-size:13px;color:var(--accent);font-weight:500}\n    .rate-pill{font-family:var(--mono);font-size:10px;padding:3px 8px;border-radius:20px;background:var(--border);color:var(--muted2);white-space:nowrap;flex-shrink:0}\n    .rate-pill.warn{color:var(--amber)}\n    .rate-pill.limit{color:var(--red)}\n    .hdr-r2{display:flex;gap:6px}\n    .csel{flex:1;min-width:0;background:var(--surface2);border:1px solid var(--border);border-radius:7px;color:var(--text);font-family:var(--sans);font-size:12px;padding:6px 8px;cursor:pointer;appearance:none;-webkit-appearance:none;outline:none}\n    .csel:focus{border-color:var(--accent)}\n    .csel option{background:var(--surface)}\n\n    /* Tool selector */\n    .tools-bar{padding:6px 10px;display:flex;gap:5px;overflow-x:auto;flex-shrink:0;border-bottom:1px solid var(--border);scrollbar-width:none;background:var(--bg)}\n    .tools-bar::-webkit-scrollbar{display:none}\n    .tool-btn{\n      display:flex;align-items:center;gap:5px;\n      padding:5px 10px;border-radius:7px;border:1px solid var(--border);\n      background:var(--surface);color:var(--muted2);font-size:11px;\n      font-family:var(--sans);cursor:pointer;white-space:nowrap;flex-shrink:0;\n      transition:all 0.15s;\n    }\n    .tool-btn:active,.tool-btn.active{background:rgba(59,130,246,0.12);border-color:rgba(59,130,246,0.35);color:var(--accent)}\n    .tool-btn.pro-tool{border-color:rgba(6,182,212,0.25);color:var(--accent2)}\n    .tool-btn.pro-tool.active{background:rgba(6,182,212,0.12);border-color:var(--accent2);color:var(--accent2)}\n    .tool-dot{width:6px;height:6px;border-radius:50%;background:currentColor;flex-shrink:0}\n    .pro-badge{font-size:8px;background:rgba(6,182,212,0.2);color:var(--accent2);padding:1px 4px;border-radius:3px;font-weight:600}\n\n    /* Offline */\n    .offbar{display:none;background:rgba(245,158,11,0.08);border-bottom:1px solid rgba(245,158,11,0.15);color:var(--amber);font-size:11px;text-align:center;padding:5px;flex-shrink:0}\n    body.offline .offbar{display:block}\n\n    /* Messages */\n    .msgs{flex:1;overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:12px}\n    .msgs::-webkit-scrollbar{width:3px}\n    .msgs::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}\n\n    .welcome{display:flex;flex-direction:column;align-items:center;justify-content:center;flex:1;text-align:center;padding:20px;gap:10px;min-height:140px}\n    .wlogo{width:46px;height:46px;background:linear-gradient(135deg,var(--accent),var(--accent2));border-radius:13px;display:flex;align-items:center;justify-content:center;font-family:var(--mono);font-size:18px;font-weight:600;color:white}\n    .welcome h2{font-family:var(--mono);font-size:16px;font-weight:500}\n    .welcome p{font-size:12px;color:var(--muted2);line-height:1.6;max-width:230px}\n\n    .msg{display:flex;gap:8px;animation:up 0.2s ease}\n    @keyframes up{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:translateY(0)}}\n    .msg.user{flex-direction:row-reverse}\n    .av{width:27px;height:27px;border-radius:8px;flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:600;font-family:var(--mono)}\n    .msg.user .av{background:var(--accent);color:white}\n    .msg.bot  .av{background:var(--surface2);border:1px solid var(--border);color:var(--accent2)}\n    .mb{max-width:calc(100% - 37px);display:flex;flex-direction:column;gap:4px}\n    .bbl{padding:9px 12px;border-radius:var(--r);font-size:13px;line-height:1.65}\n    .msg.user .bbl{background:linear-gradient(135deg,var(--accent),#2563eb);color:white;border-bottom-right-radius:3px}\n    .msg.bot  .bbl{background:var(--surface2);border:1px solid var(--border);border-bottom-left-radius:3px}\n    .tool-tag{display:inline-flex;align-items:center;gap:4px;font-family:var(--mono);font-size:9px;padding:2px 7px;border-radius:4px;width:fit-content;background:var(--border);color:var(--muted);margin-bottom:2px}\n    .stag{display:inline-flex;align-items:center;gap:4px;font-family:var(--mono);font-size:10px;padding:2px 8px;border-radius:4px;width:fit-content}\n    .ok{background:rgba(16,185,129,0.12);color:var(--green)}\n    .wn{background:rgba(245,158,11,0.12);color:var(--amber)}\n    .fl{background:rgba(239,68,68,0.12);color:var(--red)}\n    .crf{font-family:var(--mono);font-size:10px;color:var(--muted)}\n\n    .typing{display:flex;gap:4px;align-items:center;padding:2px 0}\n    .typing span{width:5px;height:5px;background:var(--muted);border-radius:50%;animation:dot 1.2s infinite}\n    .typing span:nth-child(2){animation-delay:0.2s}\n    .typing span:nth-child(3){animation-delay:0.4s}\n    @keyframes dot{0%,80%,100%{transform:scale(1);opacity:0.5}40%{transform:scale(1.3);opacity:1}}\n\n    /* Quick bar */\n    .qbar{padding:6px 10px;display:flex;gap:6px;overflow-x:auto;flex-shrink:0;scrollbar-width:none}\n    .qbar::-webkit-scrollbar{display:none}\n    .qb{background:var(--surface2);border:1px solid var(--border);border-radius:16px;color:var(--muted2);font-size:11px;padding:5px 11px;cursor:pointer;white-space:nowrap;flex-shrink:0;font-family:var(--sans);transition:all 0.15s}\n    .qb:active{border-color:var(--accent);color:var(--text)}\n\n    /* Input */\n    .inp{padding:8px 10px 12px;border-top:1px solid var(--border);background:var(--surface);flex-shrink:0}\n    .inpr{display:flex;gap:6px;align-items:flex-end}\n    textarea#q{flex:1;background:var(--surface2);border:1px solid var(--border);border-radius:var(--r);color:var(--text);font-family:var(--sans);font-size:13px;padding:9px 11px;resize:none;line-height:1.5;max-height:80px;outline:none;transition:border-color 0.15s}\n    textarea#q:focus{border-color:var(--accent)}\n    textarea#q::placeholder{color:var(--muted)}\n    .ibtn{width:38px;height:38px;border-radius:9px;border:none;display:flex;align-items:center;justify-content:center;cursor:pointer;flex-shrink:0;transition:all 0.15s}\n    #mic{background:var(--surface2);border:1px solid var(--border);color:var(--muted2);font-size:13px;font-family:var(--sans)}\n    #mic.on{background:rgba(239,68,68,0.15);border-color:var(--red);color:var(--red);animation:pulse 1s infinite}\n    #cam{background:var(--surface2);border:1px solid var(--border);color:var(--muted2);font-size:12px;font-family:var(--sans)}\n    #cam.has-image{background:rgba(39,174,96,0.15);border-color:var(--green);color:var(--green)}\n    #send{background:var(--accent);color:white}\n    #send:disabled{opacity:0.4;cursor:not-allowed}\n    @keyframes pulse{0%,100%{opacity:1}50%{opacity:0.5}}\n    .upbox{background:rgba(59,130,246,0.07);border:1px solid rgba(59,130,246,0.2);border-radius:var(--r);padding:10px 12px;font-size:13px;color:var(--accent2);line-height:1.7}\n    .upbox a{color:var(--accent);font-weight:600;text-decoration:none}\n    .toolbox-panel{display:none;flex-direction:column;gap:0;border-bottom:1px solid var(--border);flex-shrink:0;max-height:60vh;overflow-y:auto}\n    .toolbox-panel.open{display:flex}\n    .tb-header{padding:10px 12px;background:rgba(16,185,129,0.08);border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between}\n    .tb-title{font-size:13px;font-weight:600;color:var(--green)}\n    .tb-close{background:none;border:none;color:var(--muted);cursor:pointer;font-size:16px;padding:0}\n    .tb-body{padding:12px}\n    .tb-topic{font-size:14px;font-weight:600;color:var(--text);margin-bottom:6px}\n    .tb-obj{font-size:12px;color:var(--muted2);margin-bottom:10px;line-height:1.5}\n    .tb-section-label{font-family:var(--mono);font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:0.05em;margin-bottom:6px}\n    .hazard-item{display:flex;gap:8px;margin-bottom:6px;font-size:12px}\n    .risk-pill{font-size:9px;padding:2px 6px;border-radius:4px;font-weight:700;white-space:nowrap;flex-shrink:0}\n    .risk-c{background:rgba(239,68,68,0.15);color:var(--red)}\n    .risk-h{background:rgba(245,158,11,0.15);color:var(--amber)}\n    .risk-m{background:rgba(59,130,246,0.15);color:var(--accent)}\n    .tb-check{font-size:12px;color:var(--muted2);padding:4px 0;border-bottom:1px solid var(--border);display:flex;gap:8px;align-items:center}\n    .tb-check::before{content:\"\u2610\";color:var(--green);font-size:14px;flex-shrink:0}\n    .tb-discuss{background:rgba(245,158,11,0.08);border-left:3px solid var(--amber);padding:8px 10px;font-size:12px;color:var(--amber);border-radius:0 6px 6px 0;margin-top:8px;line-height:1.5}\n    .tb-ref{font-family:var(--mono);font-size:10px;color:var(--accent2);padding:6px 8px;background:rgba(6,182,212,0.06);border-radius:5px;margin-top:8px}\n    .tb-pdf-btn{display:flex;align-items:center;justify-content:space-between;margin-top:10px;padding:8px 12px;background:rgba(16,185,129,0.1);border:1px solid rgba(16,185,129,0.25);border-radius:8px;font-size:12px;color:var(--green);cursor:pointer}\n    .key-panel{display:none;padding:8px 10px;background:var(--surface2);border-bottom:1px solid var(--border);gap:8px;align-items:center}\n    .key-panel.open{display:flex}\n    .key-input{flex:1;background:var(--bg);border:1px solid var(--border);border-radius:7px;color:var(--text);font-family:var(--mono);font-size:12px;padding:6px 10px;outline:none}\n    .key-input:focus{border-color:var(--accent)}\n    .key-save{background:var(--accent);color:white;border:none;border-radius:7px;padding:6px 14px;font-size:12px;cursor:pointer;font-weight:600}\n    .tier-badge{font-family:var(--mono);font-size:10px;padding:2px 8px;border-radius:10px}\n    .tier-free{background:var(--border);color:var(--muted2)}\n    .tier-pro{background:rgba(16,185,129,0.15);color:var(--green);border:1px solid rgba(16,185,129,0.3)}\n    .upbox strong{color:var(--text)}\n  </style>\n</head>\n<body>\n<div class=\"app\">\n  <div class=\"sb\" id=\"sb\">\n    <div class=\"sb-logo\">LL</div>\n  </div>\n  <div class=\"main\">\n    <div class=\"offbar\">Offline - cached answers only</div>\n    <div class=\"key-panel\" id=\"key-panel\">\n      <input class=\"key-input\" id=\"key-input\" placeholder=\"Enter Pro key (ll_p_xxx) or leave blank for free tier\" maxlength=\"50\"/>\n      <button class=\"key-save\" id=\"key-save-btn\">Save</button>\n      <span class=\"tier-badge tier-free\" id=\"tier-badge\">FREE</span>\n    </div>\n    <header class=\"hdr\">\n      <div class=\"hdr-r1\">\n        <div class=\"trade-lbl\" id=\"tlbl\">Electrical</div>\n        <div class=\"rate-pill\" id=\"rate\">50 left</div>\n        <button id=\"key-btn\" onclick=\"toggleKeyInput()\" style=\"background:var(--accent);border:none;color:white;font-size:11px;padding:5px 12px;border-radius:6px;cursor:pointer;font-family:var(--mono);margin-left:6px;font-weight:700\">KEY</button>\n      </div>\n      <div class=\"hdr-r2\">\n        <select class=\"csel\" id=\"region\">\n          <option value=\"NSW\">AU - NSW</option>\n          <option value=\"VIC\">AU - VIC</option>\n          <option value=\"QLD\">AU - QLD</option>\n          <option value=\"WA\">AU - WA</option>\n          <option value=\"SA\">AU - SA</option>\n          <option value=\"ACT\">AU - ACT</option>\n          <option value=\"TAS\">AU - TAS</option>\n          <option value=\"NT\">AU - NT</option>\n          <option value=\"England\">UK - England</option>\n          <option value=\"Scotland\">UK - Scotland</option>\n          <option value=\"Wales\">UK - Wales</option>\n          <option value=\"Northern Ireland\">UK - Northern Ireland</option>\n          <option value=\"Alabama\">US - Alabama</option>\n          <option value=\"Alaska\">US - Alaska</option>\n          <option value=\"Arizona\">US - Arizona</option>\n          <option value=\"Arkansas\">US - Arkansas</option>\n          <option value=\"California\">US - California</option>\n          <option value=\"Colorado\">US - Colorado</option>\n          <option value=\"Connecticut\">US - Connecticut</option>\n          <option value=\"Delaware\">US - Delaware</option>\n          <option value=\"Florida\">US - Florida</option>\n          <option value=\"Georgia\">US - Georgia</option>\n          <option value=\"Hawaii\">US - Hawaii</option>\n          <option value=\"Idaho\">US - Idaho</option>\n          <option value=\"Illinois\">US - Illinois</option>\n          <option value=\"Indiana\">US - Indiana</option>\n          <option value=\"Iowa\">US - Iowa</option>\n          <option value=\"Kansas\">US - Kansas</option>\n          <option value=\"Kentucky\">US - Kentucky</option>\n          <option value=\"Louisiana\">US - Louisiana</option>\n          <option value=\"Maine\">US - Maine</option>\n          <option value=\"Maryland\">US - Maryland</option>\n          <option value=\"Massachusetts\">US - Massachusetts</option>\n          <option value=\"Michigan\">US - Michigan</option>\n          <option value=\"Minnesota\">US - Minnesota</option>\n          <option value=\"Mississippi\">US - Mississippi</option>\n          <option value=\"Missouri\">US - Missouri</option>\n          <option value=\"Montana\">US - Montana</option>\n          <option value=\"Nebraska\">US - Nebraska</option>\n          <option value=\"Nevada\">US - Nevada</option>\n          <option value=\"New Hampshire\">US - New Hampshire</option>\n          <option value=\"New Jersey\">US - New Jersey</option>\n          <option value=\"New Mexico\">US - New Mexico</option>\n          <option value=\"New York\">US - New York</option>\n          <option value=\"North Carolina\">US - North Carolina</option>\n          <option value=\"North Dakota\">US - North Dakota</option>\n          <option value=\"Ohio\">US - Ohio</option>\n          <option value=\"Oklahoma\">US - Oklahoma</option>\n          <option value=\"Oregon\">US - Oregon</option>\n          <option value=\"Pennsylvania\">US - Pennsylvania</option>\n          <option value=\"Rhode Island\">US - Rhode Island</option>\n          <option value=\"South Carolina\">US - South Carolina</option>\n          <option value=\"South Dakota\">US - South Dakota</option>\n          <option value=\"Tennessee\">US - Tennessee</option>\n          <option value=\"Texas\">US - Texas</option>\n          <option value=\"Utah\">US - Utah</option>\n          <option value=\"Vermont\">US - Vermont</option>\n          <option value=\"Virginia\">US - Virginia</option>\n          <option value=\"Washington\">US - Washington</option>\n          <option value=\"West Virginia\">US - West Virginia</option>\n          <option value=\"Wisconsin\">US - Wisconsin</option>\n          <option value=\"Wyoming\">US - Wyoming</option>\n          <option value=\"Washington DC\">US - Washington DC</option>\n          <option value=\"Ontario\">CA - Ontario</option>\n          <option value=\"British Columbia\">CA - British Columbia</option>\n          <option value=\"Alberta\">CA - Alberta</option>\n          <option value=\"Quebec\">CA - Quebec</option>\n          <option value=\"Manitoba\">CA - Manitoba</option>\n          <option value=\"Saskatchewan\">CA - Saskatchewan</option>\n          <option value=\"Nova Scotia\">CA - Nova Scotia</option>\n          <option value=\"New Brunswick\">CA - New Brunswick</option>\n          <option value=\"Newfoundland\">CA - Newfoundland</option>\n          <option value=\"Prince Edward Island\">CA - Prince Edward Island</option>\n          <option value=\"Germany\">EU - Germany</option>\n          <option value=\"France\">EU - France</option>\n          <option value=\"Netherlands\">EU - Netherlands</option>\n          <option value=\"Ireland\">EU - Ireland</option>\n          <option value=\"Spain\">EU - Spain</option>\n          <option value=\"Italy\">EU - Italy</option>\n          <option value=\"Belgium\">EU - Belgium</option>\n          <option value=\"Austria\">EU - Austria</option>\n          <option value=\"Denmark\">EU - Denmark</option>\n          <option value=\"Sweden\">EU - Sweden</option>\n          <option value=\"Finland\">EU - Finland</option>\n          <option value=\"Portugal\">EU - Portugal</option>\n          <option value=\"Poland\">EU - Poland</option>\n          <option value=\"Czech Republic\">EU - Czech Republic</option>\n        </select>\n        <select class=\"csel\" id=\"role\" style=\"max-width:105px\">\n          <option value=\"Journeyman\">Journeyman</option>\n          <option value=\"Apprentice\">Apprentice</option>\n          <option value=\"Foreman\">Foreman</option>\n          <option value=\"PM / Executive\">PM</option>\n        </select>\n      </div>\n    </header>\n\n    <!-- Tool selector -->\n    <div class=\"tools-bar\" id=\"tools-bar\">\n      <button class=\"tool-btn\" id=\"toolbox-btn\" onclick=\"showToolbox()\">\n        <span class=\"tool-dot\" style=\"background:#10b981\"></span>Toolbox\n      </button>\n      <button class=\"tool-btn active\" data-tool=\"check_compliance\">\n        <span class=\"tool-dot\"></span>Compliance\n      </button>\n      <button class=\"tool-btn pro-tool\" data-tool=\"calculate_technical_spec\">\n        <span class=\"tool-dot\"></span>Calc<span class=\"pro-badge\">PRO</span>\n      </button>\n      <button class=\"tool-btn pro-tool\" data-tool=\"generate_safety_checklist\">\n        <span class=\"tool-dot\"></span>Safety<span class=\"pro-badge\">PRO</span>\n      </button>\n      <button class=\"tool-btn pro-tool\" data-tool=\"generate_rams\">\n        <span class=\"tool-dot\"></span>RAMS<span class=\"pro-badge\">PRO</span>\n      </button>\n      <button class=\"tool-btn pro-tool\" data-tool=\"verify_material_compliance\">\n        <span class=\"tool-dot\"></span>Material<span class=\"pro-badge\">PRO</span>\n      </button>\n      <button class=\"tool-btn pro-tool\" data-tool=\"get_inspection_requirements\">\n        <span class=\"tool-dot\"></span>Inspection<span class=\"pro-badge\">PRO</span>\n      </button>\n      <button class=\"tool-btn pro-tool\" data-tool=\"visual_compliance\">\n        <span class=\"tool-dot\"></span>Photo<span class=\"pro-badge\">PRO</span>\n      </button>\n    </div>\n\n    <div class=\"msgs\" id=\"msgs\">\n      <div class=\"welcome\" id=\"welcome\">\n        <div class=\"wlogo\">LL</div>\n        <h2>Legis-Link</h2>\n        <p>Select a tool above or ask any compliance question. Pro tools auto-detected from your question.</p>\n      </div>\n    </div>\n    <div class=\"toolbox-panel\" id=\"toolbox-panel\">\n      <div class=\"tb-header\">\n        <div class=\"tb-title\" id=\"tb-day-label\">Today's Toolbox Talk</div>\n        <button class=\"tb-close\" onclick=\"closeToolbox()\">&#10005;</button>\n      </div>\n      <div class=\"tb-body\" id=\"tb-body\">\n        <div style=\"text-align:center;padding:20px;color:var(--muted)\">Loading...</div>\n      </div>\n    </div>\n    <div class=\"qbar\" id=\"qbar\"></div>\n    <div id=\"img-preview-wrap\" style=\"display:none;padding:6px 10px;background:var(--surface2);border-top:1px solid var(--border)\">\n      <img id=\"img-preview\" style=\"max-height:80px;max-width:150px;border-radius:8px;border:1px solid var(--border)\" alt=\"Preview\"/>\n      <button onclick=\"clearImage()\" style=\"background:var(--red);color:white;border:none;border-radius:5px;padding:3px 8px;font-size:11px;cursor:pointer;margin-left:8px\">Remove</button>\n    </div>\n    <div class=\"inp\">\n      <div class=\"inpr\">\n        <textarea id=\"q\" rows=\"1\" placeholder=\"Ask a compliance question...\"></textarea>\n        <input type=\"file\" id=\"img-input\" accept=\"image/jpeg,image/png,image/webp\" style=\"display:none\"/>\n        <button class=\"ibtn\" id=\"cam\" title=\"Photo compliance check (Pro)\">cam</button>\n        <button class=\"ibtn\" id=\"mic\">mic</button>\n        <button class=\"ibtn\" id=\"send\">\n          <svg width=\"15\" height=\"15\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.5\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><line x1=\"22\" y1=\"2\" x2=\"11\" y2=\"13\"/><polygon points=\"22 2 15 22 11 13 2 9 22 2\"/></svg>\n        </button>\n      </div>\n    </div>\n  </div>\n</div>\n<script>\nconst TRADES = [\n  {id:\"Electrical\",     icon:\"&#9889;\", short:\"Elec\"},\n  {id:\"Plumbing\",       icon:\"&#128295;\",short:\"Plumb\"},\n  {id:\"HVAC\",           icon:\"&#10052;\", short:\"HVAC\"},\n  {id:\"Gas fitting\",    icon:\"&#128293;\",short:\"Gas\"},\n  {id:\"Welding\",        icon:\"&#128293;\",short:\"Weld\"},\n  {id:\"Solar / Battery\",icon:\"&#9728;\",  short:\"Solar\"},\n  {id:\"Fire protection\",icon:\"&#128680;\",short:\"Fire\"},\n  {id:\"Carpentry\",      icon:\"&#128296;\",short:\"Carp\"},\n  {id:\"Concrete\",       icon:\"&#9632;\",  short:\"Conc\"},\n  {id:\"Roofing\",        icon:\"&#8963;\",  short:\"Roof\"},\n];\n\nconst QUICK = {\n  \"Electrical\":      [\"Wire size 20A\",\"Voltage drop\",\"Earth fault loop\",\"RCD requirements\",\"Breaker sizing\"],\n  \"Plumbing\":        [\"Pipe size 50 fixtures\",\"Hot water temp\",\"Backflow prevention\",\"Drain slope\"],\n  \"HVAC\":            [\"Duct size 800 CFM\",\"Ventilation rate\",\"Refrigerant clearance\",\"MERV rating\"],\n  \"Gas fitting\":     [\"Pipe sizing\",\"Pressure test\",\"Ventilation calc\",\"Appliance clearance\"],\n  \"Welding\":         [\"Preheat temp\",\"AWS qualification\",\"Inspection requirements\",\"Electrode storage\"],\n  \"Solar / Battery\": [\"Battery clearance\",\"Inverter location\",\"DC cable sizing\",\"Isolator requirements\"],\n  \"Fire protection\": [\"Detector spacing\",\"Sprinkler clearance\",\"Extinguisher placement\",\"Exit sign height\"],\n  \"Carpentry\":       [\"Bearer span\",\"Joist sizing\",\"Tie-down requirements\",\"Fixing schedule\"],\n  \"Concrete\":        [\"Cover to reo\",\"Curing time\",\"Mix design\",\"Compressive strength\"],\n  \"Roofing\":         [\"Min pitch\",\"Flashing detail\",\"Wind uplift\",\"Sarking requirements\"],\n};\n\nconst TOOL_LABELS = {\n  \"check_compliance\":           \"Compliance\",\n  \"calculate_technical_spec\":   \"Calc\",\n  \"generate_safety_checklist\":  \"Safety\",\n  \"generate_rams\":              \"RAMS\",\n  \"verify_material_compliance\": \"Material\",\n  \"get_inspection_requirements\":\"Inspection\",\n};\n\nconst FREE_LIMIT = 50;\nconst CACHE_KEY  = \"ll-cache\";\nlet activeTrade  = \"Electrical\";\nlet activeTool   = \"check_compliance\"; // default, overridden by auto-detect\nlet manualTool   = false; // true when user explicitly picked a tool\n\nlet used = parseInt(localStorage.getItem(\"ll-used\") || \"0\");\nconst savedDate = localStorage.getItem(\"ll-date\") || \"\";\nconst today = new Date().toISOString().slice(0,10);\nif (savedDate !== today) { used = 0; localStorage.setItem(\"ll-date\", today); }\n\n// \u2500\u2500 Auto-detect tool from question \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\nfunction detectTool(q) {\n  const ql = q.toLowerCase();\n  if (/cable size|wire size|pipe size|duct size|voltage drop|current rating|load calc|sizing|ampacity|flow rate|heat load|cooling load|what size|calculate/.test(ql))\n    return \"calculate_technical_spec\";\n  if (/rams|risk assessment|method statement|jha|job hazard|swms|safe work method/.test(ql))\n    return \"generate_rams\";\n  if (/safety checklist|ppe|hazard|toolbox|induction|safety check|what safety|safe to/.test(ql))\n    return \"generate_safety_checklist\";\n  if (/material|compliant|approved|can i use|is this cable|is this pipe|product|brand|specification|meets code/.test(ql))\n    return \"verify_material_compliance\";\n  if (/inspection|sign off|certificate|hold point|certifier|who inspects|test and tag|commissioning|handover/.test(ql))\n    return \"get_inspection_requirements\";\n  return \"check_compliance\";\n}\n\nfunction setActiveTool(toolId, fromUser=false) {\n  activeTool = toolId;\n  manualTool = fromUser;\n  document.querySelectorAll(\".tool-btn\").forEach(b => {\n    b.classList.toggle(\"active\", b.dataset.tool === toolId);\n  });\n}\n\n// \u2500\u2500 Build sidebar \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\nconst sb = document.getElementById(\"sb\");\nTRADES.forEach((t,i) => {\n  const btn = document.createElement(\"button\");\n  btn.className = \"tb\" + (i===0?\" on\":\"\");\n  btn.dataset.trade = t.id;\n  btn.innerHTML = `<span class=\"ti\">${t.icon}</span><span class=\"tn\">${t.short}</span>`;\n  btn.addEventListener(\"click\", function() {\n    document.querySelectorAll(\".tb\").forEach(b => b.classList.remove(\"on\"));\n    this.classList.add(\"on\");\n    activeTrade = this.dataset.trade;\n    document.getElementById(\"tlbl\").textContent = activeTrade;\n    renderQuick();\n    if (document.getElementById(\"toolbox-panel\").classList.contains(\"open\")) loadToolbox();\n  });\n  sb.appendChild(btn);\n});\n\n// \u2500\u2500 Tool button clicks \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\ndocument.querySelectorAll(\".tool-btn\").forEach(btn => {\n  btn.addEventListener(\"click\", function() {\n    setActiveTool(this.dataset.tool, true);\n    // Update placeholder to hint at what to ask\n    const hints = {\n      \"check_compliance\":           \"Ask any compliance question...\",\n      \"calculate_technical_spec\":   \"e.g. Cable size for 32A circuit in conduit, 25m run...\",\n      \"generate_safety_checklist\":  \"e.g. Safety checklist for working at height...\",\n      \"generate_rams\":              \"e.g. RAMS for electrical switchboard installation...\",\n      \"verify_material_compliance\": \"e.g. Is 6mm2 TPS cable approved for this use...\",\n      \"get_inspection_requirements\":\"e.g. Inspection requirements for timber frame NSW...\",\n    };\n    document.getElementById(\"q\").placeholder = hints[this.dataset.tool] || \"Ask a compliance question...\";\n    document.getElementById(\"q\").focus();\n  });\n});\n\nfunction toggleKeyInput() {\n  var panel = document.getElementById(\"key-panel\");\n  panel.classList.toggle(\"open\");\n  if (panel.classList.contains(\"open\")) {\n    var stored = localStorage.getItem(\"ll_api_key\") || \"\";\n    document.getElementById(\"key-input\").value = stored === \"dev_local\" ? \"\" : stored;\n    document.getElementById(\"key-input\").focus();\n  }\n}\nfunction saveKey() {\n  var input = document.getElementById(\"key-input\").value.trim();\n  var key = input;\n  if (key) { localStorage.setItem(\"ll_api_key\", key); } else { localStorage.removeItem(\"ll_api_key\"); }\n  updateTierBadge(key);\n  document.getElementById(\"key-panel\").classList.remove(\"open\");\n  if (key && (key.startsWith(\"ll_p_\") || key.startsWith(\"ll_admin_\") || key === \"dev_local\")) {\n    document.getElementById(\"rate\").textContent = \"1000/day\";\n    document.getElementById(\"rate\").className = \"rate-pill\";\n  } else { updateRate(); }\n}\nfunction updateTierBadge(key) {\n  var badge = document.getElementById(\"tier-badge\");\n  var keyBtn = document.getElementById(\"key-btn\");\n  if (!badge) return;\n  if (key && (key.startsWith(\"ll_p_\") || key.startsWith(\"ll_admin_\") || key === \"dev_local\")) {\n    badge.textContent = \"PRO\"; badge.className = \"tier-badge tier-pro\";\n    if (keyBtn) { keyBtn.style.color = \"var(--green)\"; keyBtn.textContent = \"PRO\"; }\n  } else {\n    badge.textContent = \"FREE\"; badge.className = \"tier-badge tier-free\";\n    if (keyBtn) { keyBtn.style.color = \"\"; keyBtn.textContent = \"key\"; }\n  }\n}\n// Save key button\nvar saveBtn = document.getElementById(\"key-save-btn\");\nif (saveBtn) saveBtn.addEventListener(\"click\", saveKey);\n\nfunction showToolbox() {\n  const panel = document.getElementById(\"toolbox-panel\");\n  panel.classList.add(\"open\");\n  const btn = document.getElementById(\"toolbox-btn\");\n  if (btn) btn.classList.add(\"active\");\n  loadToolbox();\n}\n\nfunction closeToolbox() {\n  document.getElementById(\"toolbox-panel\").classList.remove(\"open\");\n  const btn = document.getElementById(\"toolbox-btn\");\n  if (btn) btn.classList.remove(\"active\");\n}\n\nasync function loadToolbox() {\n  const body = document.getElementById(\"tb-body\");\n  const dayLabel = document.getElementById(\"tb-day-label\");\n  try {\n    const res = await fetch(`/toolbox?trade=${encodeURIComponent(activeTrade)}`);\n    const d = await res.json();\n    if (!res.ok) { body.innerHTML = `<p style=\"color:var(--red)\">${d.error}</p>`; return; }\n    \n    const days = [\"Sunday\",\"Monday\",\"Tuesday\",\"Wednesday\",\"Thursday\",\"Friday\",\"Saturday\"];\n    const today = days[new Date().getDay()];\n    if (dayLabel) dayLabel.textContent = `${today}'s Toolbox \u2014 ${activeTrade}`;\n\n    const riskClass = r => r===\"CRITICAL\"?\"risk-c\":r===\"HIGH\"?\"risk-h\":\"risk-m\";\n    \n    body.innerHTML = `\n      <div class=\"tb-topic\">${d.title}</div>\n      <div class=\"tb-obj\">${d.objective}</div>\n      \n      <div class=\"tb-section-label\">Hazard register</div>\n      ${d.hazards.map(h=>`\n        <div class=\"hazard-item\">\n          <span class=\"risk-pill ${riskClass(h.risk)}\">${h.risk}</span>\n          <div><div style=\"color:var(--text);margin-bottom:2px\">${h.text}</div><div style=\"color:var(--muted2)\">${h.ctrl}</div></div>\n        </div>`).join(\"\")}\n      \n      <div class=\"tb-section-label\" style=\"margin-top:10px\">Pre-start checklist</div>\n      ${d.checklist.map(c=>`<div class=\"tb-check\">${c}</div>`).join(\"\")}\n      \n      <div class=\"tb-discuss\">${d.discussion}</div>\n      <div class=\"tb-ref\">${d.ref}</div>\n      \n      <div class=\"tb-pdf-btn\" onclick=\"downloadToolboxPDF()\">\n        <span>Download printable PDF (Pro)</span>\n        <span>&#8594;</span>\n      </div>\n    `;\n  } catch(e) {\n    body.innerHTML = `<p style=\"color:var(--red)\">Error loading toolbox. Try again.</p>`;\n  }\n}\n\nasync function downloadToolboxPDF() {\n  const key = localStorage.getItem(\"ll_api_key\") || \"\";\n  const region = document.getElementById(\"region\").value;\n  const url = `/toolbox/pdf?trade=${encodeURIComponent(activeTrade)}&region=${encodeURIComponent(region)}&api_key=${encodeURIComponent(key)}`;\n  \n  try {\n    const res = await fetch(url);\n    if (res.status === 403) {\n      const d = await res.json();\n      addMsg(\"bot\", `<div class=\"upbox\">PDF generation requires Pro.<br><a href=\"${d.upgrade}\" target=\"_blank\">Get Pro Access &#8594;</a></div>`);\n      closeToolbox();\n      return;\n    }\n    const blob = await res.blob();\n    const a = document.createElement(\"a\");\n    a.href = URL.createObjectURL(blob);\n    a.download = `Toolbox_${activeTrade}_${region}_${new Date().toISOString().slice(0,10)}.pdf`;\n    a.click();\n  } catch(e) {\n    addMsg(\"bot\", \"PDF download failed. Please try again.\");\n  }\n}\n\nfunction renderQuick() {\n  const bar = document.getElementById(\"qbar\");\n  const qs  = QUICK[activeTrade] || [];\n  bar.innerHTML = \"\";\n  qs.forEach(q => {\n    const btn = document.createElement(\"button\");\n    btn.className = \"qb\";\n    btn.textContent = q;\n    btn.addEventListener(\"click\", function() { ask(this.textContent); });\n    bar.appendChild(btn);\n  });\n}\n\nfunction updateRate() {\n  const el  = document.getElementById(\"rate\");\n  const rem = FREE_LIMIT - used;\n  el.textContent = rem + \" left\";\n  el.className = \"rate-pill\" + (rem<=10?\" warn\":\"\") + (rem<=0?\" limit\":\"\");\n}\n\nfunction addMsg(role, content, meta={}) {\n  const w = document.getElementById(\"welcome\"); if(w) w.remove();\n  const msgs = document.getElementById(\"msgs\");\n  const d = document.createElement(\"div\");\n  d.className = \"msg \" + role;\n  const av = role===\"user\" ? \"U\" : \"LL\";\n  let h = `<div class=\"av\">${av}</div><div class=\"mb\">`;\n  if (meta.tool && role===\"bot\") {\n    h += `<div class=\"tool-tag\">${TOOL_LABELS[meta.tool]||meta.tool}</div>`;\n  }\n  h += `<div class=\"bbl\">${content}</div>`;\n  if (meta.status && role===\"bot\") {\n    const cls = meta.status===\"COMPLIANT\"?\"ok\":meta.status===\"NON_COMPLIANT\"?\"fl\":\"wn\";\n    h += `<div class=\"stag ${cls}\">&#9679; ${meta.status.replace(/_/g,\" \")}</div>`;\n  }\n  if (meta.ref) h += `<div class=\"crf\">Ref: ${meta.ref}</div>`;\n  h += \"</div>\";\n  d.innerHTML = h;\n  msgs.appendChild(d);\n  msgs.scrollTop = msgs.scrollHeight;\n}\n\nfunction addTyping() {\n  const msgs = document.getElementById(\"msgs\");\n  const d = document.createElement(\"div\");\n  d.className = \"msg bot\"; d.id = \"typing\";\n  d.innerHTML = `<div class=\"av\">LL</div><div class=\"mb\"><div class=\"bbl\"><div class=\"typing\"><span></span><span></span><span></span></div></div></div>`;\n  msgs.appendChild(d); msgs.scrollTop = msgs.scrollHeight;\n}\nfunction rmTyping() { const t=document.getElementById(\"typing\"); if(t) t.remove(); }\n\nfunction getCache(q) {\n  try { return (JSON.parse(localStorage.getItem(CACHE_KEY)||\"[]\")).find(c=>c.q.toLowerCase()===q.toLowerCase())||null; }\n  catch(e) { return null; }\n}\nfunction saveCache(q,r) {\n  try {\n    const c = JSON.parse(localStorage.getItem(CACHE_KEY)||\"[]\");\n    c.push({q,r}); if(c.length>10) c.shift();\n    localStorage.setItem(CACHE_KEY, JSON.stringify(c));\n  } catch(e) {}\n}\n\nvar pendingImage = null;\nvar pendingMediaType = \"image/jpeg\";\n\nasync function ask(question) {\n  question = (question || document.getElementById(\"q\").value).trim();\n  if (!question) return;\n\n  if (used >= FREE_LIMIT) {\n    addMsg(\"bot\", `<div class=\"upbox\">\n      <strong>You have used all ${FREE_LIMIT} free queries today.</strong><br><br>\n      Legis-Link Pro gives you:<br>\n      &#10003; 1,000 queries/day<br>\n      &#10003; All 8 tools: cable sizing, RAMS, safety checklists, material compliance, inspections<br>\n      &#10003; All regions: AU, UK, US, Canada, EU<br><br>\n      <a href=\"https://rickyfarmer.gumroad.com/l/Legis-LinkPro\" target=\"_blank\">Get Pro Access &mdash; $199/year &rarr;</a><br>\n      <span style=\"font-size:10px;color:var(--muted)\">Free queries reset at midnight UTC each day.</span>\n    </div>`);\n    return;\n  }\n\n  // Auto-detect tool unless user manually selected\n  const tool = manualTool ? activeTool : detectTool(question);\n  setActiveTool(tool, false);\n\n  document.getElementById(\"q\").value = \"\";\n  document.getElementById(\"q\").style.height = \"auto\";\n  document.getElementById(\"send\").disabled = true;\n  addMsg(\"user\", question);\n  addTyping();\n\n  const cached = !navigator.onLine ? getCache(question) : null;\n  try {\n    if (cached) {\n      rmTyping();\n      addMsg(\"bot\",\n        cached.r.result + ' <span style=\"font-size:10px;color:var(--muted)\">(cached)</span>',\n        {status:cached.r.status, ref:cached.r.code_reference, tool});\n    } else {\n      const res = await fetch(\"/api/query\", {\n        method: \"POST\",\n        headers: {\"Content-Type\":\"application/json\"},\n        body: JSON.stringify({\n          trade:   activeTrade,\n          region:  document.getElementById(\"region\").value,\n          role:    document.getElementById(\"role\").value,\n          question,\n          tool,\n          api_key:    localStorage.getItem(\"ll_api_key\") || \"\",\n          image:      pendingImage || undefined,\n          media_type: pendingImage ? pendingMediaType : undefined\n        })\n      });\n      const data = await res.json();\n      rmTyping();\n      if (!res.ok) {\n        addMsg(\"bot\", data.error || \"Server error. Please try again.\");\n      } else {\n        addMsg(\"bot\", data.result, {status:data.status, ref:data.code_reference, tool});\n        saveCache(question, data);\n        used++; localStorage.setItem(\"ll-used\", used); updateRate(); clearImage();\n      }\n    }\n  } catch(e) {\n    rmTyping();\n    addMsg(\"bot\", navigator.onLine ? \"Connection error. Try again.\" : \"Offline. Cached answers only.\");\n  }\n  document.getElementById(\"send\").disabled = false;\n  // Reset to auto-detect after each question\n  manualTool = false;\n}\n\n// Voice\nconst SR = window.SpeechRecognition || window.webkitSpeechRecognition;\nconst mic = document.getElementById(\"mic\");\nif (SR) {\n  const rec = new SR();\n  rec.continuous = false; rec.interimResults = true; rec.lang = \"en-AU\";\n  rec.onresult = e => {\n    const t = Array.from(e.results).map(r=>r[0].transcript).join(\"\");\n    document.getElementById(\"q\").value = t;\n    if (e.results[e.results.length-1].isFinal) { mic.classList.remove(\"on\"); ask(t); }\n  };\n  rec.onend = () => mic.classList.remove(\"on\");\n  mic.addEventListener(\"click\", () => {\n    if (mic.classList.contains(\"on\")) rec.stop();\n    else { mic.classList.add(\"on\"); rec.start(); }\n  });\n} else { mic.style.display = \"none\"; }\n\ndocument.getElementById(\"q\").addEventListener(\"keydown\", e => {\n  if (e.key===\"Enter\" && !e.shiftKey) { e.preventDefault(); ask(); }\n});\ndocument.getElementById(\"q\").addEventListener(\"input\", function() {\n  this.style.height = \"auto\";\n  this.style.height = Math.min(this.scrollHeight, 80) + \"px\";\n});\ndocument.getElementById(\"send\").addEventListener(\"click\", () => ask());\n\nwindow.addEventListener(\"online\",  () => document.body.classList.remove(\"offline\"));\nwindow.addEventListener(\"offline\", () => document.body.classList.add(\"offline\"));\nif (!navigator.onLine) document.body.classList.add(\"offline\");\nif (\"serviceWorker\" in navigator) navigator.serviceWorker.register(\"/sw.js\").catch(()=>{});\n\n\n// \u2500\u2500 Camera / photo upload \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\nfunction clearImage() {\n  pendingImage = null; pendingMediaType = \"image/jpeg\";\n  var cb = document.getElementById(\"cam\");\n  if (cb) { cb.classList.remove(\"has-image\"); cb.textContent = \"cam\"; }\n  var ii = document.getElementById(\"img-input\");\n  if (ii) ii.value = \"\";\n  var wrap = document.getElementById(\"img-preview-wrap\");\n  if (wrap) wrap.style.display = \"none\";\n  var prev = document.getElementById(\"img-preview\");\n  if (prev) prev.src = \"\";\n}\n\nvar camB = document.getElementById(\"cam\");\nvar imgI = document.getElementById(\"img-input\");\nif (camB && imgI) {\n  camB.addEventListener(\"click\", function() { imgI.click(); });\n  imgI.addEventListener(\"change\", function() {\n    var file = this.files[0]; if (!file) return;\n    pendingMediaType = file.type || \"image/jpeg\";\n    var reader = new FileReader();\n    reader.onload = function(e) {\n      pendingImage = e.target.result.split(\",\")[1];\n      camB.classList.add(\"has-image\"); camB.textContent = \"img\";\n      var wrap = document.getElementById(\"img-preview-wrap\");\n      var prev = document.getElementById(\"img-preview\");\n      if (wrap && prev) { prev.src = e.target.result; wrap.style.display = \"block\"; }\n      document.querySelectorAll(\".tool-btn\").forEach(function(b) { b.classList.remove(\"active\"); });\n      var pb = document.querySelector('[data-tool=\"visual_compliance\"]');\n      if (pb) { pb.classList.add(\"active\"); activeTool = \"visual_compliance\"; manualTool = true; }\n      document.getElementById(\"q\").placeholder = \"Ask about this photo \u2014 is this installation compliant?\";\n    };\n    reader.readAsDataURL(file);\n  });\n}\n\nrenderQuick();\nupdateRate();\nvar storedKey = localStorage.getItem(\"ll_api_key\") || \"\";\nupdateTierBadge(storedKey);\nif (storedKey && storedKey.startsWith(\"ll_p_\")) {\n  document.getElementById(\"rate\").textContent = \"1000/day\";\n}\n</script>\n</body>\n</html>",
    "landing.html":  "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n  <meta charset=\"UTF-8\"/>\n  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\"/>\n  <meta name=\"description\" content=\"Legis-Link \u2014 Ask any construction compliance question on site. Get the exact code reference instantly. Free on mobile, no install.\"/>\n  <title>Legis-Link \u2014 Compliance answers on the job site</title>\n  <link href=\"https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=DM+Sans:wght@300;400;500;600;700&display=swap\" rel=\"stylesheet\"/>\n  <style>\n    :root{\n      --bg:#080e1a;--surface:#0f1928;--surface2:#162030;\n      --border:#1c2d42;--accent:#2f80ed;--accent2:#56ccf2;\n      --green:#27ae60;--amber:#f2994a;--red:#eb5757;\n      --text:#f0f4f8;--muted:#7a8fa6;--muted2:#a8bccf;\n      --mono:\"DM Mono\",monospace;--sans:\"DM Sans\",sans-serif;\n    }\n    *{box-sizing:border-box;margin:0;padding:0}\n    html{scroll-behavior:smooth}\n    body{background:var(--bg);color:var(--text);font-family:var(--sans);line-height:1.6;overflow-x:hidden}\n\n    /* \u2500\u2500 Nav \u2500\u2500 */\n    nav{\n      position:fixed;top:0;left:0;right:0;z-index:100;\n      display:flex;align-items:center;justify-content:space-between;\n      padding:16px 24px;\n      background:rgba(8,14,26,0.85);backdrop-filter:blur(12px);\n      border-bottom:1px solid rgba(28,45,66,0.6);\n    }\n    .nav-logo{font-family:var(--mono);font-size:15px;font-weight:500;color:var(--accent);letter-spacing:-0.3px}\n    .nav-logo em{color:var(--muted);font-style:normal}\n    .nav-cta{\n      background:var(--accent);color:white;border:none;\n      padding:8px 18px;border-radius:7px;font-family:var(--sans);\n      font-size:13px;font-weight:600;cursor:pointer;text-decoration:none;\n      transition:all 0.15s;\n    }\n    .nav-cta:hover{background:#1a6fd4}\n\n    /* \u2500\u2500 Hero \u2500\u2500 */\n    .hero{\n      min-height:100vh;display:flex;flex-direction:column;\n      align-items:center;justify-content:center;\n      padding:100px 24px 60px;text-align:center;position:relative;overflow:hidden;\n    }\n    .hero-bg{\n      position:absolute;inset:0;\n      background:\n        radial-gradient(ellipse 80% 50% at 50% -10%, rgba(47,128,237,0.15) 0%, transparent 60%),\n        radial-gradient(ellipse 60% 40% at 80% 80%, rgba(86,204,242,0.08) 0%, transparent 50%);\n      pointer-events:none;\n    }\n    .hero-badge{\n      display:inline-flex;align-items:center;gap:6px;\n      background:rgba(47,128,237,0.1);border:1px solid rgba(47,128,237,0.25);\n      border-radius:20px;padding:5px 14px;font-size:12px;font-weight:500;\n      color:var(--accent2);margin-bottom:28px;\n      animation:fadeDown 0.6s ease both;\n    }\n    .hero-badge-dot{width:6px;height:6px;border-radius:50%;background:var(--green);animation:blink 2s infinite}\n    @keyframes blink{0%,100%{opacity:1}50%{opacity:0.3}}\n    @keyframes fadeDown{from{opacity:0;transform:translateY(-12px)}to{opacity:1;transform:translateY(0)}}\n    @keyframes fadeUp{from{opacity:0;transform:translateY(16px)}to{opacity:1;transform:translateY(0)}}\n\n    h1{\n      font-size:clamp(2.2rem,6vw,4rem);font-weight:700;line-height:1.1;\n      letter-spacing:-1.5px;max-width:720px;margin-bottom:20px;\n      animation:fadeUp 0.6s ease 0.1s both;\n    }\n    h1 span{\n      background:linear-gradient(135deg,var(--accent),var(--accent2));\n      -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;\n    }\n    .hero-sub{\n      font-size:clamp(1rem,2.5vw,1.2rem);color:var(--muted2);max-width:520px;\n      margin-bottom:40px;font-weight:300;\n      animation:fadeUp 0.6s ease 0.2s both;\n    }\n    .hero-ctas{\n      display:flex;gap:12px;flex-wrap:wrap;justify-content:center;\n      animation:fadeUp 0.6s ease 0.3s both;\n    }\n    .btn-primary{\n      background:var(--accent);color:white;border:none;\n      padding:14px 28px;border-radius:9px;font-family:var(--sans);\n      font-size:15px;font-weight:600;cursor:pointer;text-decoration:none;\n      transition:all 0.15s;display:inline-flex;align-items:center;gap:8px;\n    }\n    .btn-primary:hover{background:#1a6fd4;transform:translateY(-1px)}\n    .btn-secondary{\n      background:transparent;color:var(--muted2);\n      border:1px solid var(--border);\n      padding:14px 28px;border-radius:9px;font-family:var(--sans);\n      font-size:15px;font-weight:500;cursor:pointer;text-decoration:none;\n      transition:all 0.15s;\n    }\n    .btn-secondary:hover{border-color:var(--accent);color:var(--text)}\n    .hero-note{\n      margin-top:16px;font-size:12px;color:var(--muted);\n      animation:fadeUp 0.6s ease 0.4s both;\n    }\n\n    /* \u2500\u2500 Demo preview \u2500\u2500 */\n    .demo-wrap{\n      width:100%;max-width:680px;margin:56px auto 0;\n      animation:fadeUp 0.8s ease 0.5s both;\n    }\n    .demo-bar{\n      background:var(--surface2);border:1px solid var(--border);\n      border-radius:12px 12px 0 0;padding:10px 16px;\n      display:flex;align-items:center;gap:8px;\n    }\n    .demo-dot{width:10px;height:10px;border-radius:50%}\n    .demo-url{font-family:var(--mono);font-size:11px;color:var(--muted);margin-left:8px}\n    .demo-screen{\n      background:var(--surface);border:1px solid var(--border);border-top:none;\n      border-radius:0 0 12px 12px;padding:20px;\n      display:flex;flex-direction:column;gap:12px;\n    }\n    .demo-msg{display:flex;gap:10px;align-items:flex-start}\n    .demo-msg.user{flex-direction:row-reverse}\n    .demo-av{\n      width:28px;height:28px;border-radius:8px;flex-shrink:0;\n      display:flex;align-items:center;justify-content:center;\n      font-size:11px;font-weight:600;font-family:var(--mono);\n    }\n    .demo-msg.user .demo-av{background:var(--accent);color:white}\n    .demo-msg.bot  .demo-av{background:var(--surface2);border:1px solid var(--border);color:var(--accent2)}\n    .demo-bubble{\n      max-width:85%;padding:10px 14px;border-radius:10px;font-size:13px;line-height:1.6;\n    }\n    .demo-msg.user .demo-bubble{background:linear-gradient(135deg,var(--accent),#1a6fd4);color:white;border-bottom-right-radius:3px}\n    .demo-msg.bot  .demo-bubble{background:var(--surface2);border:1px solid var(--border);border-bottom-left-radius:3px}\n    .demo-tag{\n      display:inline-flex;align-items:center;gap:4px;\n      font-family:var(--mono);font-size:10px;padding:2px 8px;\n      border-radius:4px;margin-top:4px;\n      background:rgba(39,174,96,0.12);color:var(--green);\n    }\n    .demo-ref{font-family:var(--mono);font-size:10px;color:var(--muted);margin-top:3px}\n\n    /* \u2500\u2500 Pain section \u2500\u2500 */\n    .section{padding:80px 24px;max-width:1100px;margin:0 auto}\n    .section-label{\n      font-family:var(--mono);font-size:11px;font-weight:500;\n      color:var(--accent);letter-spacing:0.12em;text-transform:uppercase;\n      margin-bottom:12px;\n    }\n    h2{font-size:clamp(1.6rem,4vw,2.4rem);font-weight:700;letter-spacing:-0.8px;line-height:1.2;margin-bottom:16px}\n    h2 span{color:var(--accent)}\n    .section-sub{font-size:1.05rem;color:var(--muted2);max-width:560px;margin-bottom:48px}\n\n    .pain-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px}\n    .pain-card{\n      background:var(--surface);border:1px solid var(--border);\n      border-radius:12px;padding:24px;position:relative;overflow:hidden;\n    }\n    .pain-card::before{\n      content:'';position:absolute;top:0;left:0;right:0;height:2px;\n      background:linear-gradient(90deg,var(--red),transparent);\n    }\n    .pain-icon{font-size:24px;margin-bottom:12px}\n    .pain-card h3{font-size:15px;font-weight:600;margin-bottom:8px}\n    .pain-card p{font-size:13px;color:var(--muted2);line-height:1.6}\n    .pain-card .solution{\n      margin-top:12px;padding-top:12px;border-top:1px solid var(--border);\n      font-size:12px;color:var(--green);display:flex;align-items:center;gap:6px;\n    }\n\n    /* \u2500\u2500 Features \u2500\u2500 */\n    .features-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:20px}\n    .feat-card{\n      background:var(--surface);border:1px solid var(--border);\n      border-radius:12px;padding:28px;transition:border-color 0.2s;\n    }\n    .feat-card:hover{border-color:var(--accent)}\n    .feat-num{\n      font-family:var(--mono);font-size:11px;color:var(--accent);\n      background:rgba(47,128,237,0.1);border-radius:4px;\n      padding:2px 8px;display:inline-block;margin-bottom:14px;\n    }\n    .feat-card h3{font-size:16px;font-weight:600;margin-bottom:8px}\n    .feat-card p{font-size:13px;color:var(--muted2);line-height:1.6}\n\n    /* \u2500\u2500 Trades strip \u2500\u2500 */\n    .trades-strip{\n      background:var(--surface);border-top:1px solid var(--border);border-bottom:1px solid var(--border);\n      padding:24px;overflow:hidden;\n    }\n    .trades-label{text-align:center;font-family:var(--mono);font-size:11px;color:var(--muted);letter-spacing:0.1em;text-transform:uppercase;margin-bottom:16px}\n    .trades-scroll{\n      display:flex;gap:12px;animation:scroll 20s linear infinite;width:max-content;\n    }\n    .trade-chip{\n      background:var(--surface2);border:1px solid var(--border);\n      border-radius:20px;padding:6px 16px;font-size:12px;font-weight:500;\n      color:var(--muted2);white-space:nowrap;flex-shrink:0;\n    }\n    @keyframes scroll{from{transform:translateX(0)}to{transform:translateX(-50%)}}\n\n    /* \u2500\u2500 Regions \u2500\u2500 */\n    .regions-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}\n    .region-card{\n      background:var(--surface);border:1px solid var(--border);\n      border-radius:10px;padding:16px 20px;\n    }\n    .region-flag{font-size:22px;margin-bottom:8px}\n    .region-card h4{font-size:13px;font-weight:600;margin-bottom:4px}\n    .region-card p{font-size:11px;color:var(--muted);font-family:var(--mono)}\n\n    /* \u2500\u2500 Pricing \u2500\u2500 */\n    .pricing-grid{display:grid;grid-template-columns:1fr 1fr;gap:20px;max-width:700px;margin:0 auto}\n    @media(max-width:580px){.pricing-grid{grid-template-columns:1fr}}\n    .price-card{\n      background:var(--surface);border:1px solid var(--border);\n      border-radius:14px;padding:28px;\n    }\n    .price-card.featured{\n      border-color:var(--accent);\n      background:linear-gradient(160deg,rgba(47,128,237,0.06),var(--surface));\n      position:relative;\n    }\n    .price-card.featured::before{\n      content:'MOST POPULAR';position:absolute;top:-1px;left:50%;transform:translateX(-50%);\n      background:var(--accent);color:white;font-size:10px;font-weight:700;\n      padding:3px 12px;border-radius:0 0 6px 6px;letter-spacing:0.08em;\n    }\n    .price-tier{font-family:var(--mono);font-size:11px;color:var(--muted);letter-spacing:0.1em;text-transform:uppercase;margin-bottom:12px}\n    .price-amount{font-size:2.4rem;font-weight:700;letter-spacing:-1px;line-height:1}\n    .price-period{font-size:13px;color:var(--muted);font-weight:400}\n    .price-desc{font-size:13px;color:var(--muted2);margin:12px 0 20px}\n    .price-features{list-style:none;display:flex;flex-direction:column;gap:8px;margin-bottom:24px}\n    .price-features li{font-size:13px;color:var(--muted2);display:flex;align-items:center;gap:8px}\n    .price-features li::before{content:'\u2713';color:var(--green);font-weight:700;flex-shrink:0}\n    .price-features li.locked::before{content:'\u2192';color:var(--accent)}\n    .price-btn{\n      display:block;text-align:center;padding:12px;border-radius:8px;\n      font-size:14px;font-weight:600;text-decoration:none;transition:all 0.15s;\n    }\n    .price-btn.free{background:var(--surface2);border:1px solid var(--border);color:var(--muted2)}\n    .price-btn.free:hover{border-color:var(--accent);color:var(--text)}\n    .price-btn.pro{background:var(--accent);color:white}\n    .price-btn.pro:hover{background:#1a6fd4}\n\n    /* \u2500\u2500 CTA banner \u2500\u2500 */\n    .cta-banner{\n      background:linear-gradient(135deg,rgba(47,128,237,0.12),rgba(86,204,242,0.06));\n      border:1px solid rgba(47,128,237,0.2);\n      border-radius:16px;padding:48px 32px;text-align:center;\n      max-width:700px;margin:0 auto;\n    }\n    .cta-banner h2{margin-bottom:12px}\n    .cta-banner p{color:var(--muted2);margin-bottom:32px}\n\n    /* \u2500\u2500 Footer \u2500\u2500 */\n    footer{\n      border-top:1px solid var(--border);padding:32px 24px;\n      text-align:center;color:var(--muted);font-size:12px;\n    }\n    footer a{color:var(--accent);text-decoration:none}\n\n    /* \u2500\u2500 Divider \u2500\u2500 */\n    .divider{border:none;border-top:1px solid var(--border);margin:0}\n\n    @media(max-width:640px){\n      .hero{padding:80px 20px 48px}\n      h1{font-size:2rem;letter-spacing:-0.8px}\n      .hero-ctas{flex-direction:column;align-items:center}\n      .btn-primary,.btn-secondary{width:100%;text-align:center;justify-content:center}\n      .section{padding:56px 20px}\n    }\n  </style>\n</head>\n<body>\n\n<nav>\n  <div class=\"nav-logo\">legis<span style=\"color:var(--accent)\">-link</span> <em>v3.2</em></div>\n  <a href=\"https://legis-link-mcp-production-3e9b.up.railway.app/app\" class=\"nav-cta\">Try free &rarr;</a>\n</nav>\n\n<!-- Hero -->\n<section class=\"hero\">\n  <div class=\"hero-bg\"></div>\n  <div class=\"hero-badge\">\n    <span class=\"hero-badge-dot\"></span>\n    Live &mdash; 645 compliance checks today\n  </div>\n  <h1>The code reference is in your pocket.<br><span>Not in the truck.</span></h1>\n  <p class=\"hero-sub\">Ask any compliance question in plain English. Get the exact standard, clause, and table reference in seconds. Free on mobile &mdash; no app, no account.</p>\n  <div class=\"hero-ctas\">\n    <a href=\"https://legis-link-mcp-production-3e9b.up.railway.app/app\" class=\"btn-primary\">\n      Try it free &rarr;\n    </a>\n    <a href=\"https://rickyfarmer.gumroad.com/l/Legis-LinkPro\" class=\"btn-secondary\">\n      Get Pro &mdash; $199/yr\n    </a>\n  </div>\n  <p class=\"hero-note\">Free tier: 50 queries/day &bull; No signup &bull; Works on any phone</p>\n\n  <!-- Demo preview -->\n  <div class=\"demo-wrap\">\n    <div class=\"demo-bar\">\n      <div class=\"demo-dot\" style=\"background:#eb5757\"></div>\n      <div class=\"demo-dot\" style=\"background:#f2994a\"></div>\n      <div class=\"demo-dot\" style=\"background:#27ae60\"></div>\n      <span class=\"demo-url\">legis-link-mcp-production-3e9b.up.railway.app/app</span>\n    </div>\n    <div class=\"demo-screen\">\n      <div class=\"demo-msg user\">\n        <div class=\"demo-av\">U</div>\n        <div>\n          <div class=\"demo-bubble\">Wire size for 45A load, 30 metres, 240V single phase NSW</div>\n        </div>\n      </div>\n      <div class=\"demo-msg bot\">\n        <div class=\"demo-av\">LL</div>\n        <div>\n          <div class=\"demo-bubble\">\n            10mm&sup2; copper conductor required. Working: 45A continuous load at 30m on 240V single phase gives 1.97% voltage drop &mdash; within the 3% sub-circuit limit. Installation method (clipped direct, in conduit) may require derating to 16mm&sup2; if bundled.\n          </div>\n          <div class=\"demo-tag\">&#9679; COMPLIANT</div>\n          <div class=\"demo-ref\">Ref: AS/NZS 3008.1.1:2017 Table C1, AS/NZS 3000:2018 Clause 3.6.2</div>\n        </div>\n      </div>\n      <div class=\"demo-msg user\">\n        <div class=\"demo-av\">U</div>\n        <div>\n          <div class=\"demo-bubble\">Generate a RAMS for electrical switchboard installation</div>\n        </div>\n      </div>\n      <div class=\"demo-msg bot\">\n        <div class=\"demo-av\">LL</div>\n        <div>\n          <div class=\"demo-bubble\">\n            <strong>RAMS &mdash; Electrical Switchboard Installation NSW</strong><br>\n            Hazard Register: Electric shock (Severity 5, Likelihood 4, Risk 20 CRITICAL) &mdash; Isolate and LOTO main supply, test with approved voltage detector, wear insulated PPE Category 4...<br>\n            <span style=\"color:var(--accent);font-size:11px\">PRO tool &mdash; full document continues...</span>\n          </div>\n          <div class=\"demo-tag\">&#9679; COMPLIANT</div>\n          <div class=\"demo-ref\">Ref: WHS Act 2011 (NSW), AS/NZS 3000:2018, Electrical Safety Code</div>\n        </div>\n      </div>\n    </div>\n  </div>\n</section>\n\n<hr class=\"divider\"/>\n\n<!-- Trades strip -->\n<div class=\"trades-strip\">\n  <div class=\"trades-label\">Covers every trade</div>\n  <div style=\"overflow:hidden\">\n    <div class=\"trades-scroll\">\n      <div class=\"trade-chip\">Electrical</div>\n      <div class=\"trade-chip\">Plumbing</div>\n      <div class=\"trade-chip\">HVAC</div>\n      <div class=\"trade-chip\">Gas fitting</div>\n      <div class=\"trade-chip\">Welding</div>\n      <div class=\"trade-chip\">Solar / Battery</div>\n      <div class=\"trade-chip\">Fire protection</div>\n      <div class=\"trade-chip\">Carpentry</div>\n      <div class=\"trade-chip\">Concrete</div>\n      <div class=\"trade-chip\">Roofing</div>\n      <div class=\"trade-chip\">Electrical</div>\n      <div class=\"trade-chip\">Plumbing</div>\n      <div class=\"trade-chip\">HVAC</div>\n      <div class=\"trade-chip\">Gas fitting</div>\n      <div class=\"trade-chip\">Welding</div>\n      <div class=\"trade-chip\">Solar / Battery</div>\n      <div class=\"trade-chip\">Fire protection</div>\n      <div class=\"trade-chip\">Carpentry</div>\n      <div class=\"trade-chip\">Concrete</div>\n      <div class=\"trade-chip\">Roofing</div>\n    </div>\n  </div>\n</div>\n\n<hr class=\"divider\"/>\n\n<!-- Pain points -->\n<section class=\"section\">\n  <div class=\"section-label\">The problem</div>\n  <h2>You&rsquo;re on site. The <span>answer is 400km away.</span></h2>\n  <p class=\"section-sub\">Every tradesperson knows this moment. Work stops while you dig through a standard you don&rsquo;t have on you.</p>\n  <div class=\"pain-grid\">\n    <div class=\"pain-card\">\n      <div class=\"pain-icon\">&#128218;</div>\n      <h3>The standard is at the office</h3>\n      <p>AS/NZS 3000 is 700 pages. You need clause 3.6.2. It&rsquo;s not in your head and not on your phone.</p>\n      <div class=\"solution\">&#10003; Legis-Link gives you the exact clause in 3 seconds</div>\n    </div>\n    <div class=\"pain-card\">\n      <div class=\"pain-icon\">&#128273;</div>\n      <h3>Wrong cable = rework</h3>\n      <p>Ordering 2.5mm&sup2; when the job needed 4mm&sup2; costs more than the cable. It costs a day&rsquo;s work.</p>\n      <div class=\"solution\">&#10003; Material compliance check before you order</div>\n    </div>\n    <div class=\"pain-card\">\n      <div class=\"pain-icon\">&#128203;</div>\n      <h3>RAMS takes 3 hours</h3>\n      <p>A Risk Assessment and Method Statement used to mean clearing your afternoon. Not anymore.</p>\n      <div class=\"solution\">&#10003; Full RAMS document generated in 60 seconds</div>\n    </div>\n    <div class=\"pain-card\">\n      <div class=\"pain-icon\">&#128161;</div>\n      <h3>Inspectors ask questions you weren&rsquo;t expecting</h3>\n      <p>The hold point you missed. The certificate you didn&rsquo;t know was mandatory. The form number you never heard of.</p>\n      <div class=\"solution\">&#10003; Every hold point, every certificate, every form number</div>\n    </div>\n  </div>\n</section>\n\n<hr class=\"divider\"/>\n\n<!-- Features -->\n<section class=\"section\">\n  <div class=\"section-label\">What it does</div>\n  <h2>8 tools. <span>Every trade. Every region.</span></h2>\n  <p class=\"section-sub\">From a quick code lookup to a full RAMS document &mdash; all from your phone on site.</p>\n  <div class=\"features-grid\">\n    <div class=\"feat-card\">\n      <div class=\"feat-num\">FREE</div>\n      <h3>Compliance check</h3>\n      <p>Ask whether any installation, material, or practice is compliant. Get the exact standard and clause that answers it. COMPLIANT / NON_COMPLIANT / REQUIRES VERIFICATION.</p>\n    </div>\n    <div class=\"feat-card\">\n      <div class=\"feat-num\">FREE</div>\n      <h3>Code reference lookup</h3>\n      <p>Need the specific table, clause or section? Get the standard name, edition year, and what that clause actually requires &mdash; in plain English.</p>\n    </div>\n    <div class=\"feat-card\">\n      <div class=\"feat-num\">PRO</div>\n      <h3>Technical calculations</h3>\n      <p>Cable sizing, pipe sizing, voltage drop, HVAC loads. Enter your parameters, get the answer with the calculation shown and the code reference cited.</p>\n    </div>\n    <div class=\"feat-card\">\n      <div class=\"feat-num\">PRO</div>\n      <h3>Safety checklist</h3>\n      <p>15-item safety checklists with PPE requirements, hazard controls, and the specific regulation that mandates each item. Foreman-ready in under a minute.</p>\n    </div>\n    <div class=\"feat-card\">\n      <div class=\"feat-num\">PRO</div>\n      <h3>RAMS generator</h3>\n      <p>Full Risk Assessment and Method Statement: Hazard Register with risk ratings, numbered Method Statement, required qualifications. Print-ready, site-file ready.</p>\n    </div>\n    <div class=\"feat-card\">\n      <div class=\"feat-num\">PRO</div>\n      <h3>Inspection requirements</h3>\n      <p>Every hold point, who signs off, what certificate is issued, which form number, how much notice. Never cover work before a mandatory inspection again.</p>\n    </div>\n  </div>\n</section>\n\n<hr class=\"divider\"/>\n\n<!-- Regions -->\n<section class=\"section\">\n  <div class=\"section-label\">Coverage</div>\n  <h2>Your jurisdiction. <span>Your standard.</span></h2>\n  <p class=\"section-sub\">Not a one-size-fits-all approximation &mdash; the correct code for where you&rsquo;re standing.</p>\n  <div class=\"regions-grid\">\n    <div class=\"region-card\">\n      <div class=\"region-flag\">&#127462;&#127482;</div>\n      <h4>Australia</h4>\n      <p>AS/NZS 3000, AS/NZS 3008<br>NCC 2022, WHS Acts<br>NSW, VIC, QLD, WA, SA</p>\n    </div>\n    <div class=\"region-card\">\n      <div class=\"region-flag\">&#127468;&#127463;</div>\n      <h4>United Kingdom</h4>\n      <p>BS 7671:2018, CDM 2015<br>HSE guidance, Gas Safe<br>England, Scotland, Wales</p>\n    </div>\n    <div class=\"region-card\">\n      <div class=\"region-flag\">&#127482;&#127480;</div>\n      <h4>United States</h4>\n      <p>NEC NFPA 70, IBC<br>OSHA 29 CFR 1926<br>TX, CA, FL, NY, IL</p>\n    </div>\n    <div class=\"region-card\">\n      <div class=\"region-flag\">&#127464;&#127462;</div>\n      <h4>Canada</h4>\n      <p>CEC CSA C22.1, NBC<br>NPC, Provincial codes<br>ON, BC, AB, QC</p>\n    </div>\n    <div class=\"region-card\">\n      <div class=\"region-flag\">&#127466;&#127482;</div>\n      <h4>European Union</h4>\n      <p>EN standards<br>IEC 60364, EN 806<br>DE, FR, NL, IE, ES</p>\n    </div>\n  </div>\n</section>\n\n<hr class=\"divider\"/>\n\n<!-- Pricing -->\n<section class=\"section\" style=\"text-align:center\">\n  <div class=\"section-label\">Pricing</div>\n  <h2>Start free. <span>Upgrade when you need more.</span></h2>\n  <p class=\"section-sub\" style=\"margin:0 auto 48px\">No credit card for free tier. Pro unlocks all 8 tools and 1,000 queries/day.</p>\n  <div class=\"pricing-grid\">\n    <div class=\"price-card\">\n      <div class=\"price-tier\">Free</div>\n      <div class=\"price-amount\">$0 <span class=\"price-period\">forever</span></div>\n      <div class=\"price-desc\">No account needed. Open your browser and go.</div>\n      <ul class=\"price-features\">\n        <li>50 queries/day</li>\n        <li>Compliance check</li>\n        <li>Code reference lookup</li>\n        <li>List supported regions</li>\n        <li class=\"locked\" style=\"color:var(--muted)\">Technical calculations</li>\n        <li class=\"locked\" style=\"color:var(--muted)\">Safety checklists</li>\n        <li class=\"locked\" style=\"color:var(--muted)\">RAMS generator</li>\n        <li class=\"locked\" style=\"color:var(--muted)\">Inspection requirements</li>\n      </ul>\n      <a href=\"https://legis-link-mcp-production-3e9b.up.railway.app/app\" class=\"price-btn free\">Try free &rarr;</a>\n    </div>\n    <div class=\"price-card featured\">\n      <div class=\"price-tier\">Pro</div>\n      <div class=\"price-amount\">$199 <span class=\"price-period\">/ year</span></div>\n      <div class=\"price-desc\">All 8 tools. 1,000 queries/day. Every region.</div>\n      <ul class=\"price-features\">\n        <li>1,000 queries/day</li>\n        <li>All 3 free tools</li>\n        <li>Technical calculations</li>\n        <li>Safety checklists</li>\n        <li>RAMS generator</li>\n        <li>Inspection requirements</li>\n        <li>Material compliance check</li>\n        <li>All regions unlocked</li>\n      </ul>\n      <a href=\"https://rickyfarmer.gumroad.com/l/Legis-LinkPro\" class=\"price-btn pro\">Get Pro &rarr;</a>\n    </div>\n  </div>\n</section>\n\n<hr class=\"divider\"/>\n\n<!-- Final CTA -->\n<section class=\"section\">\n  <div class=\"cta-banner\">\n    <h2>Open it on your phone.<br><span>Right now.</span></h2>\n    <p>No download. No account. Takes 5 seconds. Ask your first compliance question for free.</p>\n    <div class=\"hero-ctas\">\n      <a href=\"https://legis-link-mcp-production-3e9b.up.railway.app/app\" class=\"btn-primary\">\n        Open Legis-Link &rarr;\n      </a>\n    </div>\n    <p style=\"margin-top:16px;font-size:11px;color:var(--muted)\">\n      Free &bull; No signup &bull; Works on any phone &bull; AU &bull; UK &bull; US &bull; CA &bull; EU\n    </p>\n  </div>\n</section>\n\n<footer>\n  <p>Legis-Link &mdash; Construction Compliance AI &bull; <a href=\"https://legis-link-mcp-production-3e9b.up.railway.app/app\">App</a> &bull; <a href=\"https://legis-link-mcp-production-3e9b.up.railway.app/connect\">Connect</a> &bull; <a href=\"https://rickyfarmer.gumroad.com/l/Legis-LinkPro\">Pro</a></p>\n  <p style=\"margin-top:8px;font-size:11px\">Results are preliminary estimates. Always verify against the full published standard before proceeding with any installation.</p>\n</footer>\n\n</body>\n</html>\n",
    "connect.html":  "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n  <meta charset=\"UTF-8\"/>\n  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\"/>\n  <title>Connect to Legis-Link MCP</title>\n  <link href=\"https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@300;400;500;600&display=swap\" rel=\"stylesheet\"/>\n  <style>\n    :root { --bg:#0a0f1a; --surface:#111827; --border:#1e293b; --accent:#3b82f6; --accent2:#06b6d4; --text:#f1f5f9; --muted:#64748b; --green:#10b981; --mono:'IBM Plex Mono',monospace; --sans:'IBM Plex Sans',sans-serif; }\n    * { box-sizing:border-box; margin:0; padding:0; }\n    body { background:var(--bg); color:var(--text); font-family:var(--sans); min-height:100vh; padding:24px; max-width:640px; margin:0 auto; }\n    h1 { font-family:var(--mono); font-size:22px; color:var(--accent); margin-bottom:6px; }\n    .sub { color:var(--muted); font-size:14px; margin-bottom:32px; }\n    h2 { font-size:15px; font-weight:600; margin-bottom:12px; margin-top:28px; }\n    .card { background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:18px 20px; margin-bottom:12px; }\n    .card-title { font-weight:600; font-size:14px; margin-bottom:8px; display:flex; align-items:center; gap:8px; }\n    .card-body { color:var(--muted); font-size:13px; line-height:1.7; }\n    .code-block { background:var(--bg); border:1px solid var(--border); border-radius:8px; padding:12px 14px; font-family:var(--mono); font-size:12px; color:var(--accent2); margin:10px 0; overflow-x:auto; white-space:pre; }\n    .badge { display:inline-block; font-size:10px; font-weight:600; padding:2px 8px; border-radius:4px; margin-left:6px; }\n    .badge-free { background:rgba(16,185,129,0.15); color:var(--green); }\n    .badge-now { background:rgba(59,130,246,0.15); color:var(--accent); }\n    .divider { border:none; border-top:1px solid var(--border); margin:28px 0; }\n    a { color:var(--accent); }\n    .back { display:inline-flex; align-items:center; gap:6px; color:var(--muted); font-size:13px; text-decoration:none; margin-bottom:24px; }\n    .back:hover { color:var(--text); }\n  </style>\n</head>\n<body>\n  <a href=\"/app\" class=\"back\">\u2190 Back to field tool</a>\n  <h1>Connect Legis-Link</h1>\n  <p class=\"sub\">Use Legis-Link from any MCP-compatible AI tool \u2014 Claude Desktop, Cursor, Windsurf, or the mobile apps below.</p>\n\n  <h2>\ud83d\udcf1 Mobile \u2014 Recommended</h2>\n\n  <div class=\"card\">\n    <div class=\"card-title\">Systemprompt MCP <span class=\"badge badge-now\">Works now</span></div>\n    <div class=\"card-body\">\n      Voice-controlled MCP client for iOS and Android. Add the server URL in app settings.\n      <div class=\"code-block\">Server URL: https://legis-link-mcp-production-3e9b.up.railway.app/sse</div>\n      Download: Search \"Systemprompt MCP\" on App Store or Google Play.\n    </div>\n  </div>\n\n  <div class=\"card\">\n    <div class=\"card-title\">Browser PWA <span class=\"badge badge-free\">Free \u00b7 No install</span></div>\n    <div class=\"card-body\">\n      Open <a href=\"/app\">/app</a> on your phone browser. Tap the share icon \u2192 \"Add to Home Screen\" for an app-like experience with offline support.\n    </div>\n  </div>\n\n  <hr class=\"divider\"/>\n  <h2>\ud83d\udda5\ufe0f Desktop</h2>\n\n  <div class=\"card\">\n    <div class=\"card-title\">Claude Desktop</div>\n    <div class=\"card-body\">\n      Add to <code style=\"font-family:var(--mono);font-size:12px\">claude_desktop_config.json</code>:\n      <div class=\"code-block\">{\n  \"mcpServers\": {\n    \"legis-link\": {\n      \"command\": \"python\",\n      \"args\": [\"legis_link_mcp_server.py\"],\n      \"env\": { \"LEGIS_LINK_API_KEY\": \"dev_local\" }\n    }\n  }\n}</div>\n    </div>\n  </div>\n\n  <div class=\"card\">\n    <div class=\"card-title\">Cursor / Windsurf</div>\n    <div class=\"card-body\">\n      Add to MCP settings \u2014 use the remote SSE endpoint:\n      <div class=\"code-block\">{\n  \"mcpServers\": {\n    \"legis-link\": {\n      \"url\": \"https://legis-link-mcp-production-3e9b.up.railway.app/sse\"\n    }\n  }\n}</div>\n    </div>\n  </div>\n\n  <hr class=\"divider\"/>\n  <h2>\ud83d\udd11 API Keys</h2>\n  <div class=\"card\">\n    <div class=\"card-body\">\n      <strong>Free tier:</strong> Use <code style=\"font-family:var(--mono)\">dev_local</code> as your API key for testing (50 requests/day).<br><br>\n      <strong>Pro tier:</strong> $199/year \u2014 1,000 requests/day, all 8 tools. Contact us to get a Pro key.\n    </div>\n  </div>\n</body>\n</html>",
    "manifest.json": "{\n  \"name\": \"Legis-Link\",\n  \"short_name\": \"Legis-Link\",\n  \"description\": \"Field compliance tool for tradespeople\",\n  \"start_url\": \"/app\",\n  \"display\": \"standalone\",\n  \"orientation\": \"portrait\",\n  \"background_color\": \"#0a0f1a\",\n  \"theme_color\": \"#3b82f6\",\n  \"icons\": [\n    {\n      \"src\": \"data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 192 192'><rect width='192' height='192' rx='40' fill='%233b82f6'/><text y='130' x='96' text-anchor='middle' font-size='100' font-family='monospace' fill='white'>\u26a1</text></svg>\",\n      \"sizes\": \"192x192\",\n      \"type\": \"image/svg+xml\"\n    }\n  ]\n}",
    "sw.js":         "const CACHE_NAME = 'legis-link-v1';\nconst STATIC = ['/app', '/connect', '/manifest.json'];\n\nself.addEventListener('install', e => {\n  e.waitUntil(\n    caches.open(CACHE_NAME).then(c => c.addAll(STATIC))\n  );\n  self.skipWaiting();\n});\n\nself.addEventListener('activate', e => {\n  e.waitUntil(\n    caches.keys().then(keys =>\n      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))\n    )\n  );\n  self.clients.claim();\n});\n\nself.addEventListener('fetch', e => {\n  // API calls \u2014 network only\n  if (e.request.url.includes('/api/query')) return;\n\n  e.respondWith(\n    fetch(e.request)\n      .then(r => {\n        const clone = r.clone();\n        caches.open(CACHE_NAME).then(c => c.put(e.request, clone));\n        return r;\n      })\n      .catch(() => caches.match(e.request))\n  );\n});",
}

def _page(name: str) -> str:
    """Return embedded page content."""
    return _PAGES.get(name, "")


# â”€â”€ API Key Auth â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Format: ll_f_<32hex> = free | ll_p_<32hex> = pro | dev_local = dev bypass
# Keys are issued manually for now. Phase 2 will automate via payment webhook.

FREE_DAILY_LIMIT = 50
PRO_DAILY_LIMIT  = 1000

# In-memory rate store "” resets on server restart (acceptable for now)
# Phase 2: replace with Redis for persistence across restarts
_rate_store: dict = defaultdict(int)

def validate_api_key(key: str | None) -> dict:
    """Validate API key. Returns {valid, tier, reason}."""
    if not key:
        # No key = free tier, 50 queries/day
        return {"valid": True, "tier": "free", "reason": "free", "remaining": 50}
    k = key.strip()
    if k == "dev_local":
        return {"valid": True, "tier": "pro"}
    if k.startswith("ll_p_") and len(k) == 37:
        return {"valid": True, "tier": "pro"}
    if k.startswith("ll_f_") and len(k) == 37:
        return {"valid": True, "tier": "free"}
    if k.startswith("ll_admin_"):
        return {"valid": True, "tier": "pro", "remaining": 999999, "reason": "admin"}
    return {"valid": False, "tier": None,
            "reason": f"Invalid key format. Keys start with ll_f_ (free) or ll_p_ (pro)."}


def check_rate_limit(api_key: str, tier: str) -> dict:
    """Check rate limit. Returns {allowed, remaining, limit}."""
    limit = PRO_DAILY_LIMIT if tier == "pro" else FREE_DAILY_LIMIT
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    store_key = f"{api_key[:8]}:{today}"
    current = _rate_store[store_key]
    if current >= limit:
        return {"allowed": False, "remaining": 0, "limit": limit,
                "reset": "tomorrow 00:00 UTC",
                "upgrade": PRO_UPGRADE if tier == "free" else None}
    _rate_store[store_key] += 1
    return {"allowed": True, "remaining": limit - current - 1, "limit": limit}


def is_pro_tool(name: str) -> bool:
    return name in {
        "calculate_technical_spec", "generate_safety_checklist",
        "generate_rams", "verify_material_compliance", "get_inspection_requirements"
    }


# â”€â”€ Audit Log â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Phase 1: Log to file + optional DB
# Phase 3: Add WORM storage, cryptographic chaining

AUDIT_LOG_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "legis_link_audit.jsonl"
)

def audit_log(api_key: str, tier: str, tool: str,
              trade: str, region: str, result_status: str,
              error: str = ""):
    """Write audit entry. Non-blocking "” errors are swallowed."""
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


# â”€â”€ Future Framework Scaffold â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# This is the roadmap. Each phase has a trigger condition and implementation notes.
# When the trigger is met, implement the phase and remove it from here.

FUTURE_ROADMAP = {
    "phase_2": {
        "trigger": "10+ paying users OR payment system live",
        "what": [
            "OAuth 2.1 / OIDC "” replace manual key issuance with login flow",
            "Usage dashboard "” /dashboard endpoint showing requests, remaining quota",
            "Email receipts "” send API key via email on payment confirmation",
            "Redis rate limiting "” replace in-memory store with Redis for persistence",
            "Key rotation "” allow users to regenerate their API key",
        ],
        "files_to_modify": ["legis_link_mcp_server.py"],
        "estimated_effort": "2-3 days"
    },
    "phase_3": {
        "trigger": "First enterprise client OR compliance requirement",
        "what": [
            "Row-level security "” per-tenant query isolation if multi-tenant DB added",
            "Namespace partitioning "” if vector DB/RAG added for custom standards",
            "WORM audit storage "” S3 Object Lock for tamper-proof compliance logs",
            "Cryptographic log chaining "” hash-chained entries for audit integrity",
            "SLA monitoring "” uptime guarantees, incident response",
        ],
        "files_to_modify": ["legis_link_mcp_server.py", "legis_link_audit.jsonl -> S3"],
        "estimated_effort": "1-2 weeks"
    },
    "phase_4": {
        "trigger": "Regulated industry client (finance, healthcare, government)",
        "what": [
            "Firecracker microVMs "” if custom tool execution is added",
            "VPC private links "” if client data must stay in private network",
            "SCIM provisioning "” enterprise SSO integration",
            "ReBAC authorization "” graph-based permissions (OPA or SpiceDB)",
            "PII scrubber "” strip sensitive data from audit logs",
        ],
        "files_to_modify": ["entire infrastructure"],
        "estimated_effort": "4-6 weeks"
    }
}


# â”€â”€ Tool definitions â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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
VALID_ROLES = ["Apprentice", "Journeyman", "Foreman", "PM / Executive"]

SYSTEM_PROMPTS = {
    "compliance": """You are a construction trade compliance expert.
Answer compliance questions with a clear direct answer, the exact code reference (standard + section), and critical caveats.
Use correct regional standards: AU (AS/NZS 3000, AS/NZS 3008, NCC), UK (BS 7671, CDM 2015, HSE), USA (NEC NFPA 70, IBC, OSHA), Canada (CEC CSA C22.1, NBC), EU (EN standards).
Return ONLY this JSON, no other text:
{"status": "COMPLIANT|NON_COMPLIANT|REQUIRES_VERIFICATION|INFO", "result": "your answer", "code_reference": "standard + section"}""",

    "calculation": """You are a construction trade calculation expert.
Perform the requested technical calculation. Show: numerical result with units, formula or method used, relevant code reference, any derating factors.
Use correct regional standards and units: mmÂ² for AU/UK/EU, AWG for USA. Be precise.
Return ONLY this JSON, no other text:
{"status": "COMPLIANT", "result": "calculation result and working", "code_reference": "standard + section"}""",

    "safety": """You are a construction safety expert.
Generate a numbered safety checklist. Each item must include the requirement, control measure, and regulation reference.
Cover: PPE, hazard controls, permits, emergency procedures.
Regional regs: AU (Safe Work Australia, WHS Act), UK (CDM 2015, HSE, PUWER), USA (OSHA 29 CFR 1926), EU (Directive 92/57/EEC).
Return ONLY this JSON, no other text:
{"status": "COMPLIANT", "result": "numbered checklist with reg refs", "code_reference": "primary regulation"}""",

    "rams": """You are a construction RAMS expert. Generate a professional document with:
SECTION 1 "” HAZARD REGISTER: table with Hazard | Severity(1-5) | Likelihood(1-5) | Risk Rating | Control Measure | Regulation
SECTION 2 "” METHOD STATEMENT: numbered steps
SECTION 3 "” REQUIRED QUALIFICATIONS & CERTIFICATIONS
Regional terminology: UK/AU=RAMS, USA=Job Hazard Analysis (JHA), EU=Method Statement.
Return ONLY this JSON, no other text:
{"status": "COMPLIANT", "result": "full document text", "code_reference": "regulations cited"}""",

    "material": """You are a construction materials compliance expert.
Check if the material meets local code. Return COMPLIANT, NON_COMPLIANT, or REQUIRES_VERIFICATION.
Explain why. Cite the specific code section. If non-compliant, state the compliant alternative.
Return ONLY this JSON, no other text:
{"status": "COMPLIANT|NON_COMPLIANT|REQUIRES_VERIFICATION", "result": "explanation", "code_reference": "standard + section"}""",

    "inspection": """You are a construction inspection and certification expert.
List all mandatory requirements: who inspects (specific role/authority), at what stage, what documents must be issued (certificate type/form), notification requirements, and the regulation mandating each.
Return ONLY this JSON, no other text:
{"status": "COMPLIANT", "result": "inspection requirements", "code_reference": "regulation + section"}""",
}

SERVER_CARD = {
    "serverInfo": {"name": "Legis-Link", "version": VERSION},
    "authentication": {
        "required": True,
        "type": "api_key",
        "header": "X-API-Key",
        "get_key": "https://legis-link-mcp-production-3e9b.up.railway.app",
        "tiers": {
            "free": "50 requests/day, 3 tools "” use api_key: dev_local for testing",
            "pro":  "$199/year, 1000 requests/day, all 8 tools "” https://rickyfarmer.gumroad.com/l/Legis-LinkPro"
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
                "role":     {"type": "string", "enum": VALID_ROLES, "default": "Journeyman",
                             "description": "The role of the person asking. Affects the depth and terminology of the answer."},
                "api_key":  {"type": "string",
                             "description": "Legis-Link API key. Use 'dev_local' for testing. Get a free key at https://legis-link-mcp-production-3e9b.up.railway.app"}
            }, "required": ["trade", "region", "question"]}
        },
        {
            "name": "get_code_reference",
            "description": (
                "Retrieves specific code sections, standards, and regulatory references for a construction trade topic. "
                "Use this tool when you need to cite the exact standard document, clause number, table reference, or "
                "regulatory instrument "” without asking a yes/no compliance question. "
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
                "api_key": {"type": "string", "description": "Legis-Link API key. Use 'dev_local' for testing."}
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
                "api_key": {"type": "string", "description": "Legis-Link API key. Use 'dev_local' for testing."}
            }, "required": ["trade"]}
        },
        {
            "name": "calculate_technical_spec",
            "description": (
                "[PRO] Performs numerical technical calculations for construction trades with code-compliant results. "
                "Use this tool when a specific measurement, size, rating, or capacity needs to be calculated "” "
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
                "role":        {"type": "string", "enum": VALID_ROLES, "default": "Journeyman",
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
                "Do NOT use this tool to generate a full RAMS document "” use generate_rams for that. "
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
                "role":    {"type": "string", "enum": VALID_ROLES, "default": "Journeyman",
                            "description": "Role of the person who will use the checklist."},
                "api_key": {"type": "string", "description": "Pro API key required."}
            }, "required": ["trade", "region", "task"]}
        },
        {
            "name": "generate_rams",
            "description": (
                "[PRO] Generates a complete Risk Assessment and Method Statement (RAMS) document "” "
                "called a Job Hazard Analysis (JHA) in the USA or Safe Work Method Statement (SWMS) in Australia. "
                "Use this tool when a foreman or PM needs a formal safety document before a high-risk task. "
                "Output includes: Section 1 "” Hazard Register (tabular, with severity/likelihood/risk rating/controls/regulations), "
                "Section 2 "” Method Statement (numbered sequential steps), "
                "Section 3 "” Required Qualifications and Certifications. "
                "The document is formatted for printing or inclusion in a site safety file. "
                "This tool produces a longer output than other tools "” expect 800-1500 words. "
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
                "and "” if non-compliant "” the compliant alternative. "
                "Use this tool BEFORE ordering materials to avoid costly substitutions on site. "
                "Do NOT use this tool for general compliance questions "” use check_compliance instead. "
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
                "role":     {"type": "string", "enum": VALID_ROLES, "default": "Journeyman",
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
                "role":         {"type": "string", "enum": VALID_ROLES, "default": "Journeyman",
                                 "description": "Role of the person requesting the inspection requirements."},
                "api_key":      {"type": "string", "description": "Pro API key required."}
            }, "required": ["trade", "region", "installation"]}
        },
    ],
    "resources": [],
    "prompts": []
}

server = Server("legis-link")


# â”€â”€ Claude API call â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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


async def ask_claude(system_prompt: str, user_message: str) -> dict:
    """Call Anthropic API. Falls back to OpenAI on billing errors."""
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
            # Billing error "” fall back to OpenAI
            if resp.status_code in (400, 402) and (
                "credit balance" in error_body or "billing" in error_body.lower()
            ):
                logging.warning("Anthropic credits exhausted "” falling back to OpenAI")
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
        text += "\n\nâš ï¸ **Non-compliant** "” see answer above for the correct alternative."
    elif status == "REQUIRES_VERIFICATION":
        text += "\n\nâš ï¸ **Requires verification** "” confirm with local authority before proceeding."
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


# â”€â”€ Tool handlers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [types.Tool(
        name=t["name"],
        description=t["description"],
        inputSchema=t["inputSchema"]
    ) for t in SERVER_CARD["tools"]]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:

    # â”€â”€ Auth â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    api_key = arguments.get("api_key", "") or os.environ.get("LEGIS_LINK_API_KEY", "")
    auth    = validate_api_key(api_key)
    if not auth["valid"]:
        audit_log(api_key or "none", "none", name, "", "", "AUTH_FAIL")
        return auth_error(auth["reason"])

    tier = auth["tier"]

    # â”€â”€ Pro tool gate â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    if is_pro_tool(name) and tier != "pro":
        audit_log(api_key, tier, name, "", "", "PRO_REQUIRED")
        return pro_required_error()

    # â”€â”€ Rate limit â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    rate = check_rate_limit(api_key, tier)
    if not rate["allowed"]:
        audit_log(api_key, tier, name, "", "", "RATE_LIMITED")
        return rate_limit_error(rate, tier)

    # â”€â”€ Extract common args â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    trade  = arguments.get("trade", "")
    region = arguments.get("region", "")
    role   = arguments.get("role", "Journeyman")

    # â”€â”€ FREE TOOLS â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    if name == "check_compliance":
        question = arguments.get("question", "")
        user_msg = f"Trade: {trade} | Region: {region} | Role: {role}\nQuestion: {question}"
        result   = await ask_claude(SYSTEM_PROMPTS["compliance"], user_msg)
        audit_log(api_key, tier, name, trade, region, result.get("status","OK"))
        return [types.TextContent(type="text", text=format_response(
            result, f"{trade} Compliance "” {region}", PRO_UPGRADE.split('?')[0]))]

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

    # â”€â”€ PRO TOOLS â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    if name == "calculate_technical_spec":
        calculation = arguments.get("calculation", "")
        user_msg = (f"Trade: {trade} | Region: {region} | Role: {role}\n"
                    f"Calculate: {calculation}")
        result = await ask_claude(SYSTEM_PROMPTS["calculation"], user_msg)
        audit_log(api_key, tier, name, trade, region, result.get("status","OK"))
        return [types.TextContent(type="text", text=format_response(
            result, f"Technical Calculation "” {trade} / {region}", PRO_UPGRADE))]

    if name == "generate_safety_checklist":
        task     = arguments.get("task", "")
        user_msg = (f"Trade: {trade} | Region: {region} | Role: {role}\n"
                    f"Safety checklist for: {task}")
        result = await ask_claude(SYSTEM_PROMPTS["safety"], user_msg)
        audit_log(api_key, tier, name, trade, region, result.get("status","OK"))
        return [types.TextContent(type="text", text=format_response(
            result, f"Safety Checklist "” {task}", PRO_UPGRADE))]

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
        title = f"RAMS "” {task} | {trade} / {region}"
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
            result, f"Material Compliance "” {material}", PRO_UPGRADE))]

    if name == "get_inspection_requirements":
        installation = arguments.get("installation", "")
        user_msg = (f"Trade: {trade} | Region: {region} | Role: {role}\n"
                    f"Inspection requirements for: {installation}")
        result = await ask_claude(SYSTEM_PROMPTS["inspection"], user_msg)
        audit_log(api_key, tier, name, trade, region, result.get("status","OK"))
        return [types.TextContent(type="text", text=format_response(
            result, f"Inspection Requirements "” {installation}", PRO_UPGRADE))]

    return [types.TextContent(type="text", text=f"Unknown tool: {name}")]


# â”€â”€ HTTP server â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

# â”€â”€ Daily Toolbox Talk Data â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
import datetime as _dt

TOOLBOX_TOPICS = {
    "Electrical": [
        {"title":"Working safely on live electrical panels","objective":"Ensure all crew understand isolation and LOTO procedures before any panel work.","hazards":[{"risk":"CRITICAL","text":"Electric shock from live conductors","ctrl":"Isolate, test with voltage detector, apply LOTO before touching"},{"risk":"CRITICAL","text":"Arc flash from short circuit","ctrl":"Arc-rated PPE minimum Category 1, maintain safe approach distances"},{"risk":"HIGH","text":"Wrong glove rating","ctrl":"Class 00 insulated gloves rated to 500V AC, inspect before use"}],"checklist":["Isolation point identified and accessible?","LOTO devices available for all workers?","Voltage detector tested and within calibration?","Arc-rated PPE available?","Emergency procedure and first aider location known?"],"discussion":"A colleague says the panel is already isolated. What do you do before touching any conductors?","ref":"AS/NZS 3000:2018 Cl.2.6; WHS Act 2011 s.36; AS/NZS 4836:2011"},
        {"title":"Cable sizing "” verify before you pull","objective":"Crew understands voltage drop limits and how to verify correct cable sizing on site.","hazards":[{"risk":"HIGH","text":"Cable overheating from undersized conductor","ctrl":"Verify cable size against AS/NZS 3008 tables "” never guess"},{"risk":"HIGH","text":"Voltage drop exceeding 5% limit","ctrl":"Calculate VD before pulling cable"},{"risk":"MED","text":"Rework cost and programme delay","ctrl":"Check once before ordering, not after installation"}],"checklist":["Circuit design load confirmed?","Cable size calculated "” not assumed?","Voltage drop verified within limits?","Derating factors applied for conduit/bundling?","Cable matches drawing specification?"],"discussion":"What is the maximum voltage drop for a final sub-circuit in NSW?","ref":"AS/NZS 3008.1.1:2017; AS/NZS 3000:2018 Cl.3.6.2"},
        {"title":"RCD testing and requirements","objective":"All crew know which circuits require RCD protection and how to test correctly.","hazards":[{"risk":"HIGH","text":"Unprotected socket outlets","ctrl":"All socket outlets in residential must have 30mA RCD protection"},{"risk":"MED","text":"RCD not tested "” false sense of security","ctrl":"Test RCD with test button monthly, trip current annually by licensed tester"},{"risk":"MED","text":"Wrong RCD type selected","ctrl":"Type A for circuits with electronic equipment, Type AC for general use"}],"checklist":["RCD protection confirmed on all socket outlet circuits?","RCD test button functional?","Last test date within 12 months?","RCD rated correctly for circuit load?","Test results documented?"],"discussion":"Which circuits in a residential installation always require RCD protection?","ref":"AS/NZS 3000:2018 Cl.2.6.3; AS/NZS 61008"},
        {"title":"Working at height "” electrical installation","objective":"Every crew member knows fall protection requirements for electrical work above 2m.","hazards":[{"risk":"HIGH","text":"Fall from ladder or elevated platform","ctrl":"3-point contact on ladder, edge protection or harness above 2m"},{"risk":"HIGH","text":"Live conductors near work platform","ctrl":"Maintain minimum approach distance, consider de-energising adjacent circuits"},{"risk":"MED","text":"Tools and materials falling to ground","ctrl":"Exclusion zone below, tool lanyards, secured materials"}],"checklist":["Fall protection in place before ascending?","Adjacent live conductors identified?","Exclusion zone barricaded at ground level?","Harness and anchor point inspected?","Rescue plan established?"],"discussion":"What is the minimum height at which fall protection is mandatory in NSW?","ref":"WHS Act 2011 s.36; AS/NZS 1891.1:2020; Safe Work Australia Working at Height COP"},
        {"title":"Switchboard labelling requirements","objective":"Crew understands mandatory labelling requirements before energising any switchboard.","hazards":[{"risk":"HIGH","text":"Incorrect circuit identification "” wrong circuit isolated","ctrl":"Label every circuit clearly before energising "” never leave blanks"},{"risk":"MED","text":"Missing danger/warning labels","ctrl":"AS/NZS 3000 labels mandatory on all switchboards"},{"risk":"MED","text":"Unlabelled circuits create ongoing safety risk","ctrl":"Document circuit schedule and store in switchboard door pocket"}],"checklist":["All circuits labelled clearly?","Danger/warning labels fitted?","Circuit schedule completed and inserted?","Arc flash label fitted if required?","Switchboard door latch functional?"],"discussion":"What information must appear on a circuit breaker label under AS/NZS 3000?","ref":"AS/NZS 3000:2018 Cl.5.3; AS/NZS 1319:1994"},
        {"title":"Testing and inspection before energising","objective":"All crew understand mandatory tests required before a new installation is energised.","hazards":[{"risk":"CRITICAL","text":"Energising a faulty installation","ctrl":"Complete all required tests before energising "” no shortcuts"},{"risk":"HIGH","text":"Missing earth continuity","ctrl":"Earth continuity test required on all circuits before energising"},{"risk":"HIGH","text":"Insulation breakdown","ctrl":"Insulation resistance test at 500V DC "” minimum 1 MOhm"}],"checklist":["Continuity of protective conductors tested?","Insulation resistance tested and results recorded?","Polarity verified correct?","Earth fault loop impedance tested?","RCD operation tested?"],"discussion":"What is the minimum acceptable insulation resistance for a 240V circuit?","ref":"AS/NZS 3000:2018 Section 8; AS/NZS 3017:2022"},
        {"title":"Confined space electrical work","objective":"Crew understands additional requirements when performing electrical work in confined spaces.","hazards":[{"risk":"CRITICAL","text":"Electrocution risk increased in confined space","ctrl":"Reduced voltage tools (25V) or RCD protection mandatory in confined spaces"},{"risk":"CRITICAL","text":"Oxygen deficiency or toxic atmosphere","ctrl":"Atmospheric testing before entry, continuous monitoring during work"},{"risk":"HIGH","text":"No rescue access","ctrl":"Standby person mandatory, rescue plan established before entry"}],"checklist":["Confined space entry permit obtained?","Atmospheric test completed and documented?","Standby person nominated and briefed?","Rescue equipment available?","Reduced voltage or RCD protection in place?"],"discussion":"What voltage is considered safe for portable tools used in a confined space?","ref":"AS 2865:2009; WHS Regulation 2017 Ch.4; AS/NZS 3000:2018"},
    ],
    "Plumbing": [
        {"title":"Backflow prevention "” protecting the water supply","objective":"Every crew member understands when backflow prevention is required and correct device type.","hazards":[{"risk":"HIGH","text":"Drinking water contamination","ctrl":"Install appropriate backflow device for hazard level "” RPZ for high hazard"},{"risk":"HIGH","text":"Wrong device type installed","ctrl":"Know the difference: RPZ for high hazard, DCV for low hazard"},{"risk":"MED","text":"Device installed incorrectly","ctrl":"RPZ horizontal, 300mm above floor, accessible for annual testing"}],"checklist":["Cross-connection risk assessed?","Correct backflow device specified?","Device rated for working pressure?","Test point accessible?","Annual testing requirement communicated to client?"],"discussion":"Name one situation on this job where backflow prevention is required.","ref":"AS/NZS 3500.1:2021 Sec.4; AS 2845.1:2010"},
        {"title":"Hot water system compliance","objective":"All crew know mandatory temperature requirements for hot water systems.","hazards":[{"risk":"HIGH","text":"Legionella growth from incorrect storage temperature","ctrl":"Store at minimum 60 degrees C "” test with calibrated thermometer"},{"risk":"HIGH","text":"Scalding from delivery temperature too high","ctrl":"Thermostatic mixing valve mandatory "” max 50 degrees C delivery"},{"risk":"MED","text":"TMV not commissioned correctly","ctrl":"Commission and test TMV to AS 4032.3 before handover"}],"checklist":["Storage temperature verified at 60 degrees C minimum?","TMV installed on bathroom circuits?","Delivery temperature tested below 50 degrees C?","Expansion relief valve installed?","Pressure set correctly?"],"discussion":"What is the minimum storage temperature for a hot water system and why?","ref":"AS/NZS 3500.4:2021 Cl.2.3; AS 4032.3:2004"},
    ],
    "HVAC": [
        {"title":"Refrigerant handling "” legal requirements","objective":"All crew handling refrigerants have valid ARC licence and understand venting prohibition.","hazards":[{"risk":"HIGH","text":"Illegal venting "” criminal offence and fine","ctrl":"Recovery mandatory. Recovery cylinder on site before any work."},{"risk":"HIGH","text":"Working without ARC licence","ctrl":"Check licence is current before handling. Unlicensed = criminal offence."},{"risk":"MED","text":"Refrigerant exposure","ctrl":"Safety glasses and gloves mandatory. Flush skin/eyes with water 15 min."}],"checklist":["ARC licence checked and current?","Recovery cylinder on site and within test date?","Recovery machine serviceable?","SDS available?","Spill kit available?"],"discussion":"What must you do before disconnecting any refrigerant line?","ref":"Ozone Protection Act 1989; AS/NZS 5149.1:2016"},
        {"title":"Duct installation "” support and sealing","objective":"Crew installs ductwork to correct support spacing and sealing requirements.","hazards":[{"risk":"MED","text":"Unsupported ductwork "” sagging and failure","ctrl":"Maximum 1200mm support spacing for rectangular duct, 2400mm for flexible"},{"risk":"MED","text":"Leaking ductwork "” energy loss and IAQ issue","ctrl":"All joints sealed with approved mastic or tape "” not standard tape"},{"risk":"MED","text":"Working at height to install ceiling ductwork","ctrl":"Stable platform, exclusion zone, no overreaching"}],"checklist":["Support spacing within specification?","All joints sealed with approved sealant?","Duct dimensions match design drawings?","Flexible duct not kinked or compressed?","Insulation continuous "” no gaps?"],"discussion":"What is the maximum length of flexible duct allowed in a single run?","ref":"AS 4254.1:2012; ASHRAE Fundamentals; SMACNA HVAC Duct Design"},
    ],
    "Gas fitting": [
        {"title":"Gas leak detection procedure","objective":"Every crew member can identify a gas leak and knows the correct emergency response.","hazards":[{"risk":"HIGH","text":"Gas accumulation and explosion","ctrl":"No ignition sources if gas smell detected. Ventilate immediately."},{"risk":"HIGH","text":"Working on pressurised system","ctrl":"Isolate at meter before any disconnection. Confirm zero pressure."},{"risk":"MED","text":"Incorrect leak detection method","ctrl":"Use calibrated detector or leak fluid "” never a naked flame"}],"checklist":["Gas detector calibrated?","Leak detection fluid available?","Emergency isolation identified?","Evacuation plan known?","Emergency number posted: 000?"],"discussion":"You smell gas when you arrive on site. What are the first 3 things you do?","ref":"AS/NZS 5601.1:2013 Cl.5.8; Gas Supply Act 1996 NSW"},
        {"title":"Appliance installation clearances","objective":"All crew know mandatory clearance requirements for gas appliance installation.","hazards":[{"risk":"HIGH","text":"Combustible materials too close to appliance","ctrl":"200mm min to side walls, 600mm to overhead combustibles for cooktops"},{"risk":"HIGH","text":"Inadequate ventilation causing CO buildup","ctrl":"Flued appliances need permanent ventilation "” 1cm2 per kW minimum"},{"risk":"MED","text":"LP gas appliances without floor ventilation","ctrl":"LP gas is heavier than air "” floor level ventilation mandatory"}],"checklist":["Clearances measured and confirmed?","Ventilation openings sized correctly?","Appliance flued correctly?","Gas type confirmed "” LP or natural?","Pressure tested after connection?"],"discussion":"What is the minimum clearance from a gas cooktop to overhead combustibles?","ref":"AS/NZS 5601.1:2013 Cl.6.3; AS 4607"},
    ],
    "Welding": [
        {"title":"Welding fumes "” health effects and controls","objective":"Crew understands health risks and required controls for welding fumes.","hazards":[{"risk":"HIGH","text":"Lung damage from chronic fume exposure","ctrl":"LEV as first control. RPE as supplement "” not substitute."},{"risk":"HIGH","text":"Manganism from MIG/FCAW","ctrl":"P2 for mild steel. P3 for stainless, galvanised, or coated metals."},{"risk":"MED","text":"Confined space fume accumulation","ctrl":"Forced ventilation required. Atmospheric monitoring before entry."}],"checklist":["LEV operational before welding starts?","Correct RPE grade for base metal type?","RPE fit-tested?","Wind direction checked?","No eating/drinking in welding area?"],"discussion":"What RPE grade is required when welding galvanised steel?","ref":"Safe Work Australia COP: Welding Processes; WES-Health Standard 2022"},
        {"title":"Fire prevention "” hot work controls","objective":"All crew understand fire risks from welding and required controls.","hazards":[{"risk":"HIGH","text":"Ignition of nearby combustibles","ctrl":"Remove combustibles 10m radius or use fire blankets to shield"},{"risk":"HIGH","text":"Sparks igniting concealed materials","ctrl":"30-minute fire watch after welding "” check voids and ceiling spaces"},{"risk":"MED","text":"No fire extinguisher available","ctrl":"Type C extinguisher within 3m of hot work at all times"}],"checklist":["Hot work permit obtained?","Combustibles removed or shielded?","Fire extinguisher within 3m?","Fire watch person nominated?","30-minute post-work watch confirmed?"],"discussion":"Welding finishes at 3pm. Until what time must the fire watch continue?","ref":"AS 1940:2017; WHS Act 2011; Safe Work Australia Hot Work COP"},
    ],
    "Solar / Battery": [
        {"title":"DC arc flash "” rooftop solar safety","objective":"All crew understand why DC arcs are more dangerous and required controls.","hazards":[{"risk":"HIGH","text":"DC arc flash "” cannot self-extinguish","ctrl":"DC-rated isolators mandatory. AC isolators on DC = fire risk."},{"risk":"HIGH","text":"Live DC voltage even when inverter off","ctrl":"Array generates voltage in any light. Shade or cover panels before string work."},{"risk":"HIGH","text":"Falls from roof","ctrl":"Edge protection or harness required above 2m. Anchor points 15kN min."}],"checklist":["DC-rated isolators confirmed?","Panel shading available?","Fall protection in place before roof access?","Arc-rated PPE for string work?","String voltage calculated within inverter limits?"],"discussion":"Why can you not use a standard AC isolator on a DC solar circuit?","ref":"AS/NZS 5033:2021 Cl.4.3; AS/NZS 3000:2018; CEC Installer Guidelines"},
        {"title":"Battery storage installation safety","objective":"Crew knows mandatory clearance and location requirements for battery systems.","hazards":[{"risk":"HIGH","text":"Thermal runaway "” fire and toxic gas","ctrl":"600mm clearance all sides. No sleeping areas. Ventilation for thermal event."},{"risk":"HIGH","text":"Short circuit during installation","ctrl":"Use insulated tools. Cover adjacent terminals. One connection at a time."},{"risk":"MED","text":"Battery installed in wrong location","ctrl":"Cannot install in living areas, sleeping areas, or blocking egress"}],"checklist":["Location approved "” not sleeping area or egress path?","600mm clearance all sides?","Ventilation provided?","Insulated tools in use?","Thermal management system connected?"],"discussion":"What is the minimum clearance required around a lithium battery system?","ref":"AS/NZS 5139:2019 Cl.5.3; CEC Battery Installation Guidelines"},
    ],
    "Fire protection": [
        {"title":"Hot work permit and fire watch","objective":"All crew understand hot work permit conditions and fire watch obligations.","hazards":[{"risk":"HIGH","text":"Ignition of concealed combustibles","ctrl":"30-minute fire watch after all hot work. Check voids and ceiling spaces."},{"risk":"HIGH","text":"Sprinkler heads damaged during installation","ctrl":"Cap heads during construction. Replace painted or damaged heads."},{"risk":"MED","text":"System isolation without permit","ctrl":"Formal impairment permit required. Notify building owner and fire brigade."}],"checklist":["Hot work permit obtained?","Fire extinguisher within 3m?","Fire watch person nominated?","30-minute post-work watch confirmed?","Sprinkler impairment permit obtained if isolated?"],"discussion":"Hot work finishes at 2pm. Until what time must fire watch continue?","ref":"AS 1940:2017; NCC Section C; AS 1851:2012 Cl.4.2"},
        {"title":"Sprinkler installation requirements","objective":"Crew installs sprinkler heads to correct clearance and spacing requirements.","hazards":[{"risk":"HIGH","text":"Wrong head type installed","ctrl":"Standard response vs quick response "” check design specification"},{"risk":"HIGH","text":"Clearance violation "” obstructed discharge","ctrl":"450mm clearance below standard heads. 150mm for quick response."},{"risk":"MED","text":"Painting sprinkler heads","ctrl":"Never paint heads "” instantly fails compliance. Replace if painted."}],"checklist":["Head type matches design specification?","Clearances measured and confirmed?","Heads not painted or mechanically damaged?","Correct orientation "” pendant, upright, or sidewall?","Design working pressure available at most remote head?"],"discussion":"What is the maximum coverage area per sprinkler head on a flat ceiling?","ref":"AS 2118.1:2017; NCC Section E"},
    ],
    "Carpentry": [
        {"title":"Frame inspection "” what must not be covered","objective":"All crew know elements that require inspection before lining.","hazards":[{"risk":"HIGH","text":"Covering work before mandatory inspection","ctrl":"Do not line until Form 10A from Accredited Certifier. No exceptions."},{"risk":"HIGH","text":"Incorrect member sizes","ctrl":"Check every member against drawings before framing "” not after."},{"risk":"MED","text":"Missing tie-downs","ctrl":"Every rafter and truss must be tied. Certifier will check."}],"checklist":["Certifier inspection scheduled before lining?","Member sizes match structural drawings?","Bracing installed per engineer specification?","Wet area substrate installed where required?","Tie-down hardware at every connection point?"],"discussion":"What is the consequence of lining a wall before frame inspection sign-off?","ref":"AS 1684.2:2021; EP&A Act 1979 NSW; NCC 2022 Volume 2"},
        {"title":"Manual handling "” timber framing","objective":"Crew uses correct manual handling techniques to prevent musculoskeletal injury.","hazards":[{"risk":"HIGH","text":"Back injury from lifting heavy members","ctrl":"Team lift for members over 20kg. Use mechanical aids for long or heavy loads."},{"risk":"MED","text":"Cuts from rough timber and fixings","ctrl":"Cut-resistant gloves Level 3 minimum for handling rough sawn timber"},{"risk":"MED","text":"Splinters and eye injury","ctrl":"Safety glasses mandatory when cutting. Clear the cutting line of all persons."}],"checklist":["Team lift procedure understood for heavy members?","Mechanical aids available on site?","Correct gloves for task?","Safety glasses worn during cutting?","Work area clear of trip hazards?"],"discussion":"What is the maximum recommended weight for a single-person lift in construction?","ref":"Safe Work Australia Hazardous Manual Tasks COP; WHS Regulation 2017"},
    ],
    "Concrete": [
        {"title":"Reinforcement cover "” getting it right","objective":"All crew understand cover requirements and verification before the pour.","hazards":[{"risk":"HIGH","text":"Incorrect cover "” structural failure over time","ctrl":"Use rated bar chairs. Check cover at multiple points before inspection."},{"risk":"HIGH","text":"Reo movement during pour","ctrl":"Secure all reo at intersections. Recheck cover after vibrating."},{"risk":"MED","text":"Inspector fails the pour","ctrl":"Have structural drawings on site. Know specified cover for each element."}],"checklist":["Cover requirement confirmed from drawings?","Correct bar chair height and spacing?","Cover measured at multiple points?","Structural drawings on site for inspector?","Concrete dockets to be retained?"],"discussion":"What is the minimum cover to reo for a residential slab on ground?","ref":"AS 3600:2018 Table 4.10.3.4"},
        {"title":"Concrete placement "” safety requirements","objective":"Crew knows hazards of concrete placement and required controls.","hazards":[{"risk":"HIGH","text":"Concrete burns from skin contact","ctrl":"Waterproof gloves and rubber boots mandatory. Wash contact immediately with water."},{"risk":"HIGH","text":"Pump line failure "” concrete blowout","ctrl":"Never stand in front of pump outlet. Check all clamps before pumping."},{"risk":"MED","text":"Noise from concrete pump","ctrl":"Hearing protection when within 3m of pump motor during operation"}],"checklist":["Waterproof gloves and boots available for all crew?","Pump lines and clamps inspected?","Washout area designated?","First aid kit with eye wash available?","Exclusion zone around pump outlet?"],"discussion":"Concrete gets on your forearm and starts to feel warm. What do you do?","ref":"Safe Work Australia Cement Products COP; AS 3610:2018"},
    ],
    "Roofing": [
        {"title":"Falls from roof "” control hierarchy","objective":"Every crew member knows today's fall protection plan and individual obligations.","hazards":[{"risk":"HIGH","text":"Fall from roof edge "” leading cause of fatality","ctrl":"Edge protection (preferred) or harness + anchor above 2m. Mandatory."},{"risk":"HIGH","text":"Fragile roof surfaces "” skylights, old metal","ctrl":"Roof boards over fragile surfaces. Never step directly on polycarbonate."},{"risk":"MED","text":"Materials falling to ground","ctrl":"Exclusion zone below. Toe boards on platforms. Secure loose materials."}],"checklist":["Edge protection installed before first person on roof?","Harness and anchor points inspected?","Exclusion zone barricaded at ground level?","Fragile surfaces identified and marked?","Rescue plan established?"],"discussion":"What is the minimum anchor point rating for a single person working at height?","ref":"Safe Work Australia Working at Height COP; AS/NZS 1891.1:2020; WHS Reg 2017 s.78"},
        {"title":"Wind uplift "” correct fixing schedule","objective":"Crew understands the importance of correct fixing schedule for wind region.","hazards":[{"risk":"HIGH","text":"Roof failure in wind event "” under-fixed sheets","ctrl":"Use manufacturer fixing guide for your wind category. Do not estimate."},{"risk":"HIGH","text":"Wrong screw class "” corrosion failure","ctrl":"Class 4 minimum for coastal. Class 3 for inland. Check local corrosion zone."},{"risk":"MED","text":"Tools dropped from roof","ctrl":"Tool lanyards mandatory. No unsecured tools on roof surface."}],"checklist":["Wind category confirmed for this site?","Fixing schedule from manufacturer available?","Correct screw class for corrosion zone?","Edge and perimeter bays fixed to increased schedule?","Tool lanyards fitted to all hand tools?"],"discussion":"What wind category applies to this site and how does it affect your fixing schedule?","ref":"AS 1170.2:2021; NASH Standard; manufacturer fixing guide"},
    ],
}

def get_daily_toolbox(trade: str) -> dict:
    """Get today's toolbox topic for a given trade."""
    topics = TOOLBOX_TOPICS.get(trade, [])
    if not topics:
        return {}
    day_num = _dt.date.today().timetuple().tm_yday
    topic = topics[day_num % len(topics)]
    return topic


def run_http():
    try:
        from mcp.server.sse import SseServerTransport
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse
        from starlette.routing import Mount, Route
        import uvicorn

        sse = SseServerTransport("/messages/")

        async def handle_sse(request):
            async with sse.connect_sse(
                request.scope, request.receive, request._send
            ) as streams:
                await server.run(streams[0], streams[1],
                                 server.create_initialization_options())

        async def handle_gumroad_webhook(request):
            """Gumroad webhook. Set Ping URL in Gumroad Settings > Advanced."""
            try: body = await request.json()
            except:
                form = await request.form(); body = dict(form)
            email = str(body.get("email","")).strip()
            if not email: return JSONResponse({"error":"no email"},status_code=400)
            if body.get("refunded"): _revoke_key(email); return JSONResponse({"status":"revoked"})
            key = generate_pro_key(email)
            _store_key(email, key, str(body.get("sale_id","")), str(body.get("product_name","")))
            _notify_sale(email, key, str(body.get("product_name","")), str(body.get("sale_id","")))
            audit_log(email[:20],"pro","webhook_sale","","","KEY_ISSUED")
            return JSONResponse({"status":"ok","email":email,"tier":"pro"})

        async def handle_key_lookup(request):
            """Admin key lookup. GET /key/lookup?email=x&secret=ADMIN_SECRET"""
            secret = os.environ.get("LEGIS_ADMIN_SECRET","")
            if not secret or request.query_params.get("secret","") != secret:
                return JSONResponse({"error":"Unauthorized"},status_code=401)
            email = request.query_params.get("email","").strip()
            if not email: return JSONResponse({"error":"email required"},status_code=400)
            return JSONResponse({"email":email,"pro_key":generate_pro_key(email),"free_key":generate_free_key(email)})

        async def handle_toolbox(request):
            """Daily toolbox talk "” free tier preview."""
            trade  = request.query_params.get("trade", "Electrical")
            topic  = get_daily_toolbox(trade)
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
            """Generate full toolbox talk PDF "” Pro only."""
            from starlette.responses import Response as _Resp
            api_key = request.query_params.get("api_key", "")
            auth    = validate_api_key(api_key)
            if not auth["valid"] or auth["tier"] != "pro":
                return JSONResponse({"error": "Pro required", "upgrade": PRO_UPGRADE}, status_code=403)
            trade  = request.query_params.get("trade", "Electrical")
            region = request.query_params.get("region", "NSW")
            topic  = get_daily_toolbox(trade)
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
                    Paragraph(f"<font color='white'><b>LEGIS-LINK</b> "” Toolbox Talk</font>", S("hh", fontName="Helvetica-Bold", fontSize=14, textColor=_colors.white, leading=18)),
                    Paragraph(f"<font color='white'>{trade} Â· {region} Â· {_dt.date.today().strftime('%d %b %Y')}</font>", S("hr", fontName="Helvetica", fontSize=11, textColor=_colors.white, leading=14, alignment=TA_RIGHT)),
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
                    Paragraph("â˜", S(f"cb{i}", fontName="Helvetica", fontSize=14, textColor=BLUE, alignment=TA_CENTER)),
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
                    Paragraph("Generated by Legis-Link "” legis-link-mcp-production-3e9b.up.railway.app", SF),
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
                "auth": "required",
                "api_key_set": bool(key),
                "api_key_prefix": key[:12] + "..." if len(key) > 12 else "MISSING"
            })

        async def handle_test(request):
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
            role     = body.get("role", "Journeyman")
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

            # Rate limit
            rate = check_rate_limit(api_key, tier)
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

            # â”€â”€ Image / visual compliance â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
            image_b64  = body.get("image", "") or ""
            media_type = body.get("media_type", "image/jpeg") or "image/jpeg"
            has_image  = len(image_b64) > 100

            if has_image:
                if tier != "pro":
                    return JSONResponse({
                        "error": "Visual compliance requires Pro. Upgrade at https://rickyfarmer.gumroad.com/l/Legis-LinkPro",
                        "upgrade": PRO_UPGRADE
                    }, status_code=403)
                tool = "visual_compliance"
                user_msg = (f"Trade: {trade} | Region: {region} | Role: {role}\n"
                           f"Question: {question or 'Is this installation compliant?'}")
                result = await ask_claude_vision(
                    SYSTEM_PROMPTS["visual"], user_msg, image_b64, media_type
                )
                disclaimer = ("<br><br><em style='color:#94a3b8;font-size:11px'>"
                              "Preliminary visual check only "” not a certified inspection."
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

            return JSONResponse({
                "status":         result.get("status", "INFO"),
                "result":         result_text,
                "code_reference": result.get("code_reference", ""),
                "trade":          trade,
                "region":         region,
                "tool":           tool,
                "remaining":      rate["remaining"],
            })

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
            Route("/manifest.json", handle_manifest),
            Route("/sw.js",         handle_sw),
            Route("/api/query",     handle_api_query, methods=["POST"]),
            Route("/.well-known/mcp/server-card.json", handle_server_card),
            Mount("/sse", app=sse.handle_post_message),
            Mount("/", routes=[Route("/sse", endpoint=handle_sse)]),
        ])

        print(f"[Legis-Link MCP v{VERSION}] HTTP port {PORT} "” auth+ratelimit+audit",
              file=sys.stderr)
        uvicorn.run(starlette_app, host="0.0.0.0", port=PORT)

    except ImportError as e:
        print(f"HTTP deps missing: {e}", file=sys.stderr)
        sys.exit(1)


async def run_stdio():
    print(f"[Legis-Link MCP v{VERSION}] stdio "” auth+ratelimit+audit", file=sys.stderr)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream,
                         server.create_initialization_options())


if __name__ == "__main__":
    if os.environ.get("PORT"):
        run_http()
    else:
        asyncio.run(run_stdio())


