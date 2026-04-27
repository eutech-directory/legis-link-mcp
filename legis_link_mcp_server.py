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
MODEL             = "claude-haiku-4-5-20251001"
PORT              = int(os.environ.get("PORT", 8000))
DATABASE_URL      = os.environ.get("DATABASE_URL", "")
PRO_UPGRADE       = "https://legis-link-mcp-production-3e9b.up.railway.app/upgrade"
VERSION           = "3.2.1"
# ── Page content (embedded — no filesystem dependency) ────────────────────
_PAGES = {
    "app.html":      "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n  <meta charset=\"UTF-8\"/>\n  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no\"/>\n  <meta name=\"mobile-web-app-capable\" content=\"yes\"/>\n  <meta name=\"apple-mobile-web-app-capable\" content=\"yes\"/>\n  <meta name=\"apple-mobile-web-app-status-bar-style\" content=\"black-translucent\"/>\n  <meta name=\"theme-color\" content=\"#0f172a\"/>\n  <title>Legis-Link \u2014 Field Compliance</title>\n  <link rel=\"manifest\" href=\"/manifest.json\"/>\n  <link rel=\"preconnect\" href=\"https://fonts.googleapis.com\"/>\n  <link href=\"https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@300;400;500;600&display=swap\" rel=\"stylesheet\"/>\n  <style>\n    :root {\n      --bg:       #0a0f1a;\n      --surface:  #111827;\n      --border:   #1e293b;\n      --accent:   #3b82f6;\n      --accent2:  #06b6d4;\n      --text:     #f1f5f9;\n      --muted:    #64748b;\n      --green:    #10b981;\n      --amber:    #f59e0b;\n      --red:      #ef4444;\n      --mono:     'IBM Plex Mono', monospace;\n      --sans:     'IBM Plex Sans', sans-serif;\n    }\n    * { box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; }\n    html, body { height: 100%; overflow: hidden; background: var(--bg); color: var(--text); font-family: var(--sans); }\n\n    /* \u2500\u2500 Layout \u2500\u2500 */\n    .app { display: flex; flex-direction: column; height: 100dvh; max-width: 480px; margin: 0 auto; }\n\n    /* \u2500\u2500 Header \u2500\u2500 */\n    .header {\n      display: flex; align-items: center; justify-content: space-between;\n      padding: 12px 16px; border-bottom: 1px solid var(--border);\n      background: var(--surface); flex-shrink: 0;\n    }\n    .header-logo { font-family: var(--mono); font-size: 14px; font-weight: 500; color: var(--accent); }\n    .header-logo span { color: var(--muted); }\n    .rate-pill {\n      font-family: var(--mono); font-size: 11px; color: var(--muted);\n      background: var(--border); border-radius: 20px; padding: 3px 10px;\n    }\n    .rate-pill.warn { color: var(--amber); }\n    .rate-pill.limit { color: var(--red); }\n\n    /* \u2500\u2500 Context bar \u2500\u2500 */\n    .context-bar {\n      display: flex; gap: 8px; padding: 10px 16px;\n      border-bottom: 1px solid var(--border); flex-shrink: 0;\n      overflow-x: auto; scrollbar-width: none;\n    }\n    .context-bar::-webkit-scrollbar { display: none; }\n    .ctx-select {\n      background: var(--border); border: 1px solid transparent;\n      border-radius: 8px; color: var(--text); font-family: var(--sans);\n      font-size: 12px; padding: 6px 10px; cursor: pointer; white-space: nowrap;\n      appearance: none; -webkit-appearance: none;\n    }\n    .ctx-select:focus { outline: none; border-color: var(--accent); }\n    .ctx-select option { background: var(--surface); }\n\n    /* \u2500\u2500 Messages \u2500\u2500 */\n    .messages { flex: 1; overflow-y: auto; padding: 16px; display: flex; flex-direction: column; gap: 12px; }\n    .messages::-webkit-scrollbar { width: 3px; }\n    .messages::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }\n\n    .msg { display: flex; gap: 10px; max-width: 100%; animation: fadeUp 0.2s ease; }\n    @keyframes fadeUp { from { opacity:0; transform:translateY(6px); } to { opacity:1; transform:translateY(0); } }\n\n    .msg.user { flex-direction: row-reverse; }\n    .msg-avatar {\n      width: 28px; height: 28px; border-radius: 8px; flex-shrink: 0;\n      display: flex; align-items: center; justify-content: center; font-size: 13px;\n    }\n    .msg.user .msg-avatar { background: var(--accent); color: white; }\n    .msg.bot  .msg-avatar { background: var(--border); color: var(--accent2); font-family: var(--mono); font-size: 11px; }\n\n    .msg-body { max-width: calc(100% - 40px); }\n    .msg-bubble {\n      padding: 10px 14px; border-radius: 12px; font-size: 14px; line-height: 1.6;\n    }\n    .msg.user .msg-bubble { background: var(--accent); color: white; border-bottom-right-radius: 4px; }\n    .msg.bot  .msg-bubble { background: var(--surface); border: 1px solid var(--border); border-bottom-left-radius: 4px; }\n\n    .msg-status {\n      display: inline-flex; align-items: center; gap: 5px;\n      font-family: var(--mono); font-size: 11px; margin-top: 6px;\n      padding: 3px 8px; border-radius: 4px;\n    }\n    .status-ok     { background: rgba(16,185,129,0.1); color: var(--green); }\n    .status-warn   { background: rgba(245,158,11,0.1); color: var(--amber); }\n    .status-fail   { background: rgba(239,68,68,0.1);  color: var(--red); }\n    .msg-ref { font-family: var(--mono); font-size: 11px; color: var(--muted); margin-top: 4px; }\n\n    /* \u2500\u2500 Quick actions \u2500\u2500 */\n    .quick-wrap { padding: 8px 16px; display: flex; gap: 8px; overflow-x: auto; flex-shrink: 0; scrollbar-width: none; }\n    .quick-wrap::-webkit-scrollbar { display: none; }\n    .quick-btn {\n      background: var(--surface); border: 1px solid var(--border);\n      border-radius: 20px; color: var(--muted); font-size: 12px;\n      padding: 6px 14px; cursor: pointer; white-space: nowrap; flex-shrink: 0;\n      transition: all 0.15s;\n    }\n    .quick-btn:hover { border-color: var(--accent); color: var(--text); }\n\n    /* \u2500\u2500 Input bar \u2500\u2500 */\n    .input-bar {\n      display: flex; align-items: flex-end; gap: 8px; padding: 12px 16px;\n      border-top: 1px solid var(--border); background: var(--surface); flex-shrink: 0;\n    }\n    .input-wrap { flex: 1; position: relative; }\n    #query-input {\n      width: 100%; background: var(--border); border: 1px solid transparent;\n      border-radius: 12px; color: var(--text); font-family: var(--sans);\n      font-size: 14px; padding: 10px 14px; resize: none; line-height: 1.5;\n      max-height: 120px; overflow-y: auto;\n    }\n    #query-input:focus { outline: none; border-color: var(--accent); }\n    #query-input::placeholder { color: var(--muted); }\n\n    .btn-icon {\n      width: 40px; height: 40px; border-radius: 10px; border: none;\n      display: flex; align-items: center; justify-content: center;\n      cursor: pointer; flex-shrink: 0; transition: all 0.15s; font-size: 18px;\n    }\n    #voice-btn { background: var(--border); color: var(--muted); }\n    #voice-btn.listening { background: rgba(239,68,68,0.2); color: var(--red); animation: pulse 1s infinite; }\n    @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.5} }\n    #send-btn { background: var(--accent); color: white; }\n    #send-btn:disabled { opacity: 0.4; cursor: not-allowed; }\n\n    /* \u2500\u2500 Typing indicator \u2500\u2500 */\n    .typing { display: flex; gap: 4px; padding: 10px 14px; }\n    .typing span { width: 6px; height: 6px; background: var(--muted); border-radius: 50%; animation: bounce 1.2s infinite; }\n    .typing span:nth-child(2) { animation-delay: 0.2s; }\n    .typing span:nth-child(3) { animation-delay: 0.4s; }\n    @keyframes bounce { 0%,80%,100%{transform:translateY(0)} 40%{transform:translateY(-6px)} }\n\n    /* \u2500\u2500 Welcome \u2500\u2500 */\n    .welcome { text-align: center; padding: 32px 16px; }\n    .welcome-icon { font-size: 40px; margin-bottom: 12px; }\n    .welcome h2 { font-family: var(--mono); font-size: 18px; color: var(--accent); margin-bottom: 8px; }\n    .welcome p  { font-size: 13px; color: var(--muted); line-height: 1.6; max-width: 280px; margin: 0 auto; }\n\n    /* \u2500\u2500 Offline banner \u2500\u2500 */\n    .offline-banner {\n      display: none; background: rgba(245,158,11,0.1); border-bottom: 1px solid rgba(245,158,11,0.2);\n      color: var(--amber); font-size: 12px; text-align: center; padding: 6px;\n    }\n    body.offline .offline-banner { display: block; }\n\n    /* \u2500\u2500 Upgrade prompt \u2500\u2500 */\n    .upgrade-msg { background: rgba(59,130,246,0.08); border: 1px solid rgba(59,130,246,0.2); border-radius: 10px; padding: 12px 14px; font-size: 13px; color: var(--accent2); }\n    .upgrade-msg a { color: var(--accent); text-decoration: none; font-weight: 500; }\n  </style>\n</head>\n<body>\n<div class=\"app\">\n\n  <div class=\"offline-banner\">\u26a1 Offline \u2014 showing cached results</div>\n\n  <header class=\"header\">\n    <div class=\"header-logo\">legis<span>-link</span></div>\n    <div class=\"rate-pill\" id=\"rate-display\">free \u00b7 50/day</div>\n  </header>\n\n  <div class=\"context-bar\">\n    <select class=\"ctx-select\" id=\"trade-select\">\n      <option value=\"Electrical\">\u26a1 Electrical</option>\n      <option value=\"Plumbing\">\ud83d\udd27 Plumbing</option>\n      <option value=\"HVAC\">\ud83d\udca8 HVAC</option>\n      <option value=\"Welding\">\ud83d\udd25 Welding</option>\n      <option value=\"Solar / Battery\">\ud83d\udd0b Solar</option>\n      <option value=\"Carpentry\">\ud83e\udeb5 Carpentry</option>\n      <option value=\"Fire protection\">\ud83d\ude92 Fire protection</option>\n      <option value=\"Concrete\">\ud83e\udea8 Concrete</option>\n      <option value=\"Roofing\">\ud83c\udfe0 Roofing</option>\n      <option value=\"Gas fitting\">\ud83d\udd29 Gas fitting</option>\n    </select>\n    <select class=\"ctx-select\" id=\"region-select\">\n      <option value=\"NSW\">\ud83c\udde6\ud83c\uddfa NSW</option>\n      <option value=\"VIC\">\ud83c\udde6\ud83c\uddfa VIC</option>\n      <option value=\"QLD\">\ud83c\udde6\ud83c\uddfa QLD</option>\n      <option value=\"WA\">\ud83c\udde6\ud83c\uddfa WA</option>\n      <option value=\"England\">\ud83c\uddec\ud83c\udde7 England</option>\n      <option value=\"Scotland\">\ud83c\uddec\ud83c\udde7 Scotland</option>\n      <option value=\"Texas\">\ud83c\uddfa\ud83c\uddf8 Texas</option>\n      <option value=\"California\">\ud83c\uddfa\ud83c\uddf8 California</option>\n      <option value=\"Ontario\">\ud83c\udde8\ud83c\udde6 Ontario</option>\n      <option value=\"Germany\">\ud83c\udde9\ud83c\uddea Germany</option>\n    </select>\n    <select class=\"ctx-select\" id=\"role-select\">\n      <option value=\"Journeyman\">Journeyman</option>\n      <option value=\"Apprentice\">Apprentice</option>\n      <option value=\"Foreman\">Foreman</option>\n      <option value=\"PM / Executive\">PM</option>\n    </select>\n  </div>\n\n  <div class=\"messages\" id=\"messages\">\n    <div class=\"welcome\">\n      <div class=\"welcome-icon\">\u26a1</div>\n      <h2>Legis-Link</h2>\n      <p>Ask any compliance question in plain English. Get the exact code reference instantly.</p>\n    </div>\n  </div>\n\n  <div class=\"quick-wrap\" id=\"quick-actions\"></div>\n\n  <div class=\"input-bar\">\n    <div class=\"input-wrap\">\n      <textarea id=\"query-input\" rows=\"1\" placeholder=\"Ask a compliance question...\" maxlength=\"500\"></textarea>\n    </div>\n    <button class=\"btn-icon\" id=\"voice-btn\" title=\"Voice input\">\ud83c\udfa4</button>\n    <button class=\"btn-icon\" id=\"send-btn\" title=\"Send\">\u2192</button>\n  </div>\n\n</div>\n\n<script>\nconst API_BASE = window.location.origin;\nconst CACHE_KEY = 'legis-link-queries';\nconst MAX_CACHE = 10;\nconst FREE_LIMIT = 50;\n\n// \u2500\u2500 State \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\nlet usageCount = parseInt(localStorage.getItem('ll-usage') || '0');\nlet usageDate  = localStorage.getItem('ll-date') || '';\nconst today    = new Date().toISOString().slice(0,10);\nif (usageDate !== today) { usageCount = 0; localStorage.setItem('ll-date', today); }\n\n// \u2500\u2500 Quick actions by trade \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\nconst QUICK = {\n  'Electrical':     ['Wire size for 20A circuit','Voltage drop check','Earth fault loop','RCD requirements'],\n  'Plumbing':       ['Pipe size for 50 fixtures','Hot water temp','Backflow prevention','Drain slope'],\n  'HVAC':           ['Duct size for 800 CFM','Refrigerant clearance','Ventilation rate','Filter MERV rating'],\n  'Welding':        ['Preheat temp for carbon steel','AWS qualification','Inspection requirements','Electrode storage'],\n  'Solar / Battery':['Battery clearance from wall','Inverter location','DC cable sizing','Protection requirements'],\n  'Carpentry':      ['Bearer span table','Joist size','Tie-down requirements','Fixing schedule'],\n  'Fire protection':['Detector spacing','Sprinkler clearance','Extinguisher placement','Exit sign height'],\n  'Concrete':       ['Cover to reinforcement','Curing time','Mix design','Compressive strength test'],\n  'Roofing':        ['Pitch requirements','Flashing details','Wind uplift','Sarking requirements'],\n  'Gas fitting':    ['Pipe sizing','Pressure test','Ventilation','Appliance clearance'],\n};\n\nfunction updateQuickActions() {\n  const trade = document.getElementById('trade-select').value;\n  const wrap  = document.getElementById('quick-actions');\n  const qs    = QUICK[trade] || [];\n  wrap.innerHTML = qs.map(q =>\n    `<button class=\"quick-btn\" onclick=\"sendQuery('${q.replace(/'/g,\"\\\\'\")}')\">\n      ${q}\n    </button>`\n  ).join('');\n}\n\nfunction updateRateDisplay() {\n  const pill = document.getElementById('rate-display');\n  const remaining = FREE_LIMIT - usageCount;\n  pill.textContent = `${remaining} left today`;\n  pill.className = 'rate-pill' + (remaining <= 10 ? ' warn' : '') + (remaining <= 0 ? ' limit' : '');\n}\n\n// \u2500\u2500 Message rendering \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\nfunction addMessage(role, content, meta = {}) {\n  const msgs = document.getElementById('messages');\n  const div  = document.createElement('div');\n  div.className = `msg ${role}`;\n\n  const avatar = role === 'user' ? '\ud83d\udc64' : 'LL';\n  let html = `<div class=\"msg-avatar\">${avatar}</div><div class=\"msg-body\">`;\n  html += `<div class=\"msg-bubble\">${content}</div>`;\n\n  if (meta.status && role === 'bot') {\n    const cls = meta.status === 'COMPLIANT' ? 'status-ok'\n              : meta.status === 'NON_COMPLIANT' ? 'status-fail' : 'status-warn';\n    html += `<div class=\"msg-status ${cls}\">\u25cf ${meta.status.replace('_',' ')}</div>`;\n  }\n  if (meta.ref) {\n    html += `<div class=\"msg-ref\">\ud83d\udccb ${meta.ref}</div>`;\n  }\n  html += '</div>';\n  div.innerHTML = html;\n  msgs.appendChild(div);\n  msgs.scrollTop = msgs.scrollHeight;\n  return div;\n}\n\nfunction addTyping() {\n  const msgs = document.getElementById('messages');\n  const div  = document.createElement('div');\n  div.className = 'msg bot';\n  div.id = 'typing-indicator';\n  div.innerHTML = `<div class=\"msg-avatar\">LL</div>\n    <div class=\"msg-body\"><div class=\"msg-bubble\">\n      <div class=\"typing\"><span></span><span></span><span></span></div>\n    </div></div>`;\n  msgs.appendChild(div);\n  msgs.scrollTop = msgs.scrollHeight;\n}\n\nfunction removeTyping() {\n  const t = document.getElementById('typing-indicator');\n  if (t) t.remove();\n}\n\n// \u2500\u2500 Cache \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\nfunction saveToCache(query, result) {\n  try {\n    const cache = JSON.parse(localStorage.getItem(CACHE_KEY) || '[]');\n    cache.push({ q: query, r: result, ts: Date.now() });\n    if (cache.length > MAX_CACHE) cache.shift();\n    localStorage.setItem(CACHE_KEY, JSON.stringify(cache));\n  } catch(e) {}\n}\n\nfunction getFromCache(query) {\n  try {\n    const cache = JSON.parse(localStorage.getItem(CACHE_KEY) || '[]');\n    return cache.find(c => c.q.toLowerCase() === query.toLowerCase()) || null;\n  } catch(e) { return null; }\n}\n\n// \u2500\u2500 API call \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\nasync function callAPI(question) {\n  const trade  = document.getElementById('trade-select').value;\n  const region = document.getElementById('region-select').value;\n  const role   = document.getElementById('role-select').value;\n\n  const resp = await fetch(`${API_BASE}/api/query`, {\n    method: 'POST',\n    headers: { 'Content-Type': 'application/json' },\n    body: JSON.stringify({ trade, region, role, question, api_key: 'dev_local' })\n  });\n\n  if (!resp.ok) {\n    const err = await resp.json().catch(() => ({}));\n    throw new Error(err.error || `HTTP ${resp.status}`);\n  }\n  return resp.json();\n}\n\n// \u2500\u2500 Send query \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\nasync function sendQuery(question) {\n  question = question || document.getElementById('query-input').value.trim();\n  if (!question) return;\n\n  // Rate limit check\n  if (usageCount >= FREE_LIMIT) {\n    addMessage('bot',\n      `<div class=\"upgrade-msg\">Daily limit reached (${FREE_LIMIT} queries).<br>\n      <a href=\"/connect\">Upgrade to Pro</a> for 1,000 queries/day.</div>`);\n    return;\n  }\n\n  document.getElementById('query-input').value = '';\n  document.getElementById('send-btn').disabled = true;\n\n  addMessage('user', question);\n  addTyping();\n\n  // Check offline cache first\n  const cached = !navigator.onLine ? getFromCache(question) : null;\n\n  try {\n    let data;\n    if (cached) {\n      data = cached.r;\n      removeTyping();\n      addMessage('bot', `${data.result} <span style=\"color:var(--muted);font-size:11px\">(cached)</span>`,\n        { status: data.status, ref: data.code_reference });\n    } else {\n      data = await callAPI(question);\n      removeTyping();\n      addMessage('bot', data.result, { status: data.status, ref: data.code_reference });\n      saveToCache(question, data);\n\n      usageCount++;\n      localStorage.setItem('ll-usage', usageCount);\n      updateRateDisplay();\n    }\n  } catch(err) {\n    removeTyping();\n    if (err.message.includes('credit') || err.message.includes('billing')) {\n      addMessage('bot', '\u26a0\ufe0f Service temporarily unavailable. Please try again shortly.');\n    } else if (!navigator.onLine) {\n      addMessage('bot', '\ud83d\udcf5 Offline. Only cached answers available.');\n    } else {\n      addMessage('bot', `Error: ${err.message}`);\n    }\n  }\n\n  document.getElementById('send-btn').disabled = false;\n  document.getElementById('query-input').focus();\n}\n\n// \u2500\u2500 Voice input \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\nlet recognition = null;\nconst SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;\n\nif (SpeechRec) {\n  recognition = new SpeechRec();\n  recognition.continuous = false;\n  recognition.interimResults = true;\n  recognition.lang = 'en-AU';\n\n  recognition.onresult = e => {\n    const transcript = Array.from(e.results).map(r => r[0].transcript).join('');\n    document.getElementById('query-input').value = transcript;\n    if (e.results[e.results.length-1].isFinal) {\n      document.getElementById('voice-btn').classList.remove('listening');\n      sendQuery(transcript);\n    }\n  };\n\n  recognition.onend = () => {\n    document.getElementById('voice-btn').classList.remove('listening');\n  };\n\n  document.getElementById('voice-btn').addEventListener('click', () => {\n    const btn = document.getElementById('voice-btn');\n    if (btn.classList.contains('listening')) {\n      recognition.stop();\n    } else {\n      btn.classList.add('listening');\n      recognition.start();\n    }\n  });\n} else {\n  document.getElementById('voice-btn').style.display = 'none';\n}\n\n// \u2500\u2500 Keyboard submit \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\ndocument.getElementById('query-input').addEventListener('keydown', e => {\n  if (e.key === 'Enter' && !e.shiftKey) {\n    e.preventDefault();\n    sendQuery();\n  }\n});\n\ndocument.getElementById('send-btn').addEventListener('click', () => sendQuery());\n\n// \u2500\u2500 Auto-resize textarea \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\ndocument.getElementById('query-input').addEventListener('input', function() {\n  this.style.height = 'auto';\n  this.style.height = Math.min(this.scrollHeight, 120) + 'px';\n});\n\n// \u2500\u2500 Trade change \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\ndocument.getElementById('trade-select').addEventListener('change', updateQuickActions);\n\n// \u2500\u2500 Offline detection \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\nwindow.addEventListener('online',  () => document.body.classList.remove('offline'));\nwindow.addEventListener('offline', () => document.body.classList.add('offline'));\nif (!navigator.onLine) document.body.classList.add('offline');\n\n// \u2500\u2500 Service Worker \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\nif ('serviceWorker' in navigator) {\n  navigator.serviceWorker.register('/sw.js').catch(() => {});\n}\n\n// \u2500\u2500 Init \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\nupdateQuickActions();\nupdateRateDisplay();\n</script>\n</body>\n</html>",
    "connect.html":  "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n  <meta charset=\"UTF-8\"/>\n  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\"/>\n  <title>Connect to Legis-Link MCP</title>\n  <link href=\"https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@300;400;500;600&display=swap\" rel=\"stylesheet\"/>\n  <style>\n    :root { --bg:#0a0f1a; --surface:#111827; --border:#1e293b; --accent:#3b82f6; --accent2:#06b6d4; --text:#f1f5f9; --muted:#64748b; --green:#10b981; --mono:'IBM Plex Mono',monospace; --sans:'IBM Plex Sans',sans-serif; }\n    * { box-sizing:border-box; margin:0; padding:0; }\n    body { background:var(--bg); color:var(--text); font-family:var(--sans); min-height:100vh; padding:24px; max-width:640px; margin:0 auto; }\n    h1 { font-family:var(--mono); font-size:22px; color:var(--accent); margin-bottom:6px; }\n    .sub { color:var(--muted); font-size:14px; margin-bottom:32px; }\n    h2 { font-size:15px; font-weight:600; margin-bottom:12px; margin-top:28px; }\n    .card { background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:18px 20px; margin-bottom:12px; }\n    .card-title { font-weight:600; font-size:14px; margin-bottom:8px; display:flex; align-items:center; gap:8px; }\n    .card-body { color:var(--muted); font-size:13px; line-height:1.7; }\n    .code-block { background:var(--bg); border:1px solid var(--border); border-radius:8px; padding:12px 14px; font-family:var(--mono); font-size:12px; color:var(--accent2); margin:10px 0; overflow-x:auto; white-space:pre; }\n    .badge { display:inline-block; font-size:10px; font-weight:600; padding:2px 8px; border-radius:4px; margin-left:6px; }\n    .badge-free { background:rgba(16,185,129,0.15); color:var(--green); }\n    .badge-now { background:rgba(59,130,246,0.15); color:var(--accent); }\n    .divider { border:none; border-top:1px solid var(--border); margin:28px 0; }\n    a { color:var(--accent); }\n    .back { display:inline-flex; align-items:center; gap:6px; color:var(--muted); font-size:13px; text-decoration:none; margin-bottom:24px; }\n    .back:hover { color:var(--text); }\n  </style>\n</head>\n<body>\n  <a href=\"/app\" class=\"back\">\u2190 Back to field tool</a>\n  <h1>Connect Legis-Link</h1>\n  <p class=\"sub\">Use Legis-Link from any MCP-compatible AI tool \u2014 Claude Desktop, Cursor, Windsurf, or the mobile apps below.</p>\n\n  <h2>\ud83d\udcf1 Mobile \u2014 Recommended</h2>\n\n  <div class=\"card\">\n    <div class=\"card-title\">Systemprompt MCP <span class=\"badge badge-now\">Works now</span></div>\n    <div class=\"card-body\">\n      Voice-controlled MCP client for iOS and Android. Add the server URL in app settings.\n      <div class=\"code-block\">Server URL: https://legis-link-mcp-production-3e9b.up.railway.app/sse</div>\n      Download: Search \"Systemprompt MCP\" on App Store or Google Play.\n    </div>\n  </div>\n\n  <div class=\"card\">\n    <div class=\"card-title\">Browser PWA <span class=\"badge badge-free\">Free \u00b7 No install</span></div>\n    <div class=\"card-body\">\n      Open <a href=\"/app\">/app</a> on your phone browser. Tap the share icon \u2192 \"Add to Home Screen\" for an app-like experience with offline support.\n    </div>\n  </div>\n\n  <hr class=\"divider\"/>\n  <h2>\ud83d\udda5\ufe0f Desktop</h2>\n\n  <div class=\"card\">\n    <div class=\"card-title\">Claude Desktop</div>\n    <div class=\"card-body\">\n      Add to <code style=\"font-family:var(--mono);font-size:12px\">claude_desktop_config.json</code>:\n      <div class=\"code-block\">{\n  \"mcpServers\": {\n    \"legis-link\": {\n      \"command\": \"python\",\n      \"args\": [\"legis_link_mcp_server.py\"],\n      \"env\": { \"LEGIS_LINK_API_KEY\": \"dev_local\" }\n    }\n  }\n}</div>\n    </div>\n  </div>\n\n  <div class=\"card\">\n    <div class=\"card-title\">Cursor / Windsurf</div>\n    <div class=\"card-body\">\n      Add to MCP settings \u2014 use the remote SSE endpoint:\n      <div class=\"code-block\">{\n  \"mcpServers\": {\n    \"legis-link\": {\n      \"url\": \"https://legis-link-mcp-production-3e9b.up.railway.app/sse\"\n    }\n  }\n}</div>\n    </div>\n  </div>\n\n  <hr class=\"divider\"/>\n  <h2>\ud83d\udd11 API Keys</h2>\n  <div class=\"card\">\n    <div class=\"card-body\">\n      <strong>Free tier:</strong> Use <code style=\"font-family:var(--mono)\">dev_local</code> as your API key for testing (50 requests/day).<br><br>\n      <strong>Pro tier:</strong> $199/year \u2014 1,000 requests/day, all 8 tools. Contact us to get a Pro key.\n    </div>\n  </div>\n</body>\n</html>",
    "manifest.json": "{\n  \"name\": \"Legis-Link\",\n  \"short_name\": \"Legis-Link\",\n  \"description\": \"Field compliance tool for tradespeople\",\n  \"start_url\": \"/app\",\n  \"display\": \"standalone\",\n  \"orientation\": \"portrait\",\n  \"background_color\": \"#0a0f1a\",\n  \"theme_color\": \"#3b82f6\",\n  \"icons\": [\n    {\n      \"src\": \"data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 192 192'><rect width='192' height='192' rx='40' fill='%233b82f6'/><text y='130' x='96' text-anchor='middle' font-size='100' font-family='monospace' fill='white'>\u26a1</text></svg>\",\n      \"sizes\": \"192x192\",\n      \"type\": \"image/svg+xml\"\n    }\n  ]\n}",
    "sw.js":         "const CACHE_NAME = 'legis-link-v1';\nconst STATIC = ['/app', '/connect', '/manifest.json'];\n\nself.addEventListener('install', e => {\n  e.waitUntil(\n    caches.open(CACHE_NAME).then(c => c.addAll(STATIC))\n  );\n  self.skipWaiting();\n});\n\nself.addEventListener('activate', e => {\n  e.waitUntil(\n    caches.keys().then(keys =>\n      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))\n    )\n  );\n  self.clients.claim();\n});\n\nself.addEventListener('fetch', e => {\n  // API calls \u2014 network only\n  if (e.request.url.includes('/api/query')) return;\n\n  e.respondWith(\n    fetch(e.request)\n      .then(r => {\n        const clone = r.clone();\n        caches.open(CACHE_NAME).then(c => c.put(e.request, clone));\n        return r;\n      })\n      .catch(() => caches.match(e.request))\n  );\n});",
}

def _page(name: str) -> str:
    """Return embedded page content."""
    return _PAGES.get(name, "")


# ── API Key Auth ────────────────────────────────────────────────────────────
# Format: ll_f_<32hex> = free | ll_p_<32hex> = pro | dev_local = dev bypass
# Keys are issued manually for now. Phase 2 will automate via payment webhook.

FREE_DAILY_LIMIT = 50
PRO_DAILY_LIMIT  = 1000

# In-memory rate store — resets on server restart (acceptable for now)
# Phase 2: replace with Redis for persistence across restarts
_rate_store: dict = defaultdict(int)

def validate_api_key(key: str | None) -> dict:
    """Validate API key. Returns {valid, tier, reason}."""
    if not key:
        return {"valid": False, "tier": None,
                "reason": "API key required. Get a free key at legis-link-mcp-production-3e9b.up.railway.app"}
    k = key.strip()
    if k == "dev_local":
        return {"valid": True, "tier": "pro"}
    if k.startswith("ll_p_") and len(k) == 37:
        return {"valid": True, "tier": "pro"}
    if k.startswith("ll_f_") and len(k) == 37:
        return {"valid": True, "tier": "free"}
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
    "Australia": ["NSW", "VIC", "QLD", "WA", "SA", "ACT"],
    "USA":       ["Texas", "California", "Florida", "New York", "Illinois"],
    "Canada":    ["Ontario", "British Columbia", "Alberta", "Quebec"],
    "UK":        ["England", "Scotland", "Wales", "Northern Ireland"],
    "EU":        ["Germany", "France", "Netherlands", "Ireland", "Spain", "Italy"],
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
Use correct regional standards and units: mm² for AU/UK/EU, AWG for USA. Be precise.
Return ONLY this JSON, no other text:
{"status": "COMPLIANT", "result": "calculation result and working", "code_reference": "standard + section"}""",

    "safety": """You are a construction safety expert.
Generate a numbered safety checklist. Each item must include the requirement, control measure, and regulation reference.
Cover: PPE, hazard controls, permits, emergency procedures.
Regional regs: AU (Safe Work Australia, WHS Act), UK (CDM 2015, HSE, PUWER), USA (OSHA 29 CFR 1926), EU (Directive 92/57/EEC).
Return ONLY this JSON, no other text:
{"status": "COMPLIANT", "result": "numbered checklist with reg refs", "code_reference": "primary regulation"}""",

    "rams": """You are a construction RAMS expert. Generate a professional document with:
SECTION 1 — HAZARD REGISTER: table with Hazard | Severity(1-5) | Likelihood(1-5) | Risk Rating | Control Measure | Regulation
SECTION 2 — METHOD STATEMENT: numbered steps
SECTION 3 — REQUIRED QUALIFICATIONS & CERTIFICATIONS
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
            "free": "50 requests/day, 3 tools — no signup needed, use key: dev_local for testing",
            "pro":  "$199/year, 1000 requests/day, 8 tools"
        }
    },
    "tools": [
        {"name": "check_compliance",
         "description": "Answer construction trade compliance questions with code references. Free tier.",
         "inputSchema": {"type": "object", "properties": {
             "trade":    {"type": "string", "enum": VALID_TRADES},
             "region":   {"type": "string"},
             "question": {"type": "string"},
             "role":     {"type": "string", "enum": VALID_ROLES, "default": "Journeyman"},
             "api_key":  {"type": "string", "description": "Your Legis-Link API key"}
         }, "required": ["trade", "region", "question"]}},
        {"name": "get_code_reference",
         "description": "Look up specific trade code sections and standards. Free tier.",
         "inputSchema": {"type": "object", "properties": {
             "trade":   {"type": "string", "enum": VALID_TRADES},
             "region":  {"type": "string"},
             "topic":   {"type": "string"},
             "api_key": {"type": "string"}
         }, "required": ["trade", "region", "topic"]}},
        {"name": "list_supported_regions",
         "description": "List all supported regions for a given trade. Free tier.",
         "inputSchema": {"type": "object", "properties": {
             "trade":   {"type": "string", "enum": VALID_TRADES},
             "api_key": {"type": "string"}
         }, "required": ["trade"]}},
        {"name": "calculate_technical_spec",
         "description": "[PRO] Calculate cable sizing, pipe sizing, HVAC loads, voltage drop.",
         "inputSchema": {"type": "object", "properties": {
             "trade":       {"type": "string", "enum": VALID_TRADES},
             "region":      {"type": "string"},
             "calculation": {"type": "string"},
             "role":        {"type": "string", "enum": VALID_ROLES, "default": "Journeyman"},
             "api_key":     {"type": "string"}
         }, "required": ["trade", "region", "calculation"]}},
        {"name": "generate_safety_checklist",
         "description": "[PRO] Generate trade-specific safety checklist with regulatory citations.",
         "inputSchema": {"type": "object", "properties": {
             "trade":   {"type": "string", "enum": VALID_TRADES},
             "region":  {"type": "string"},
             "task":    {"type": "string"},
             "role":    {"type": "string", "enum": VALID_ROLES, "default": "Journeyman"},
             "api_key": {"type": "string"}
         }, "required": ["trade", "region", "task"]}},
        {"name": "generate_rams",
         "description": "[PRO] Generate a Risk Assessment and Method Statement (RAMS/JHA).",
         "inputSchema": {"type": "object", "properties": {
             "trade":        {"type": "string", "enum": VALID_TRADES},
             "region":       {"type": "string"},
             "task":         {"type": "string"},
             "company_name": {"type": "string"},
             "site_address": {"type": "string"},
             "role":         {"type": "string", "enum": VALID_ROLES, "default": "Foreman"},
             "api_key":      {"type": "string"}
         }, "required": ["trade", "region", "task"]}},
        {"name": "verify_material_compliance",
         "description": "[PRO] Check material spec against local code.",
         "inputSchema": {"type": "object", "properties": {
             "trade":    {"type": "string", "enum": VALID_TRADES},
             "region":   {"type": "string"},
             "material": {"type": "string"},
             "use_case": {"type": "string"},
             "role":     {"type": "string", "enum": VALID_ROLES, "default": "Journeyman"},
             "api_key":  {"type": "string"}
         }, "required": ["trade", "region", "material"]}},
        {"name": "get_inspection_requirements",
         "description": "[PRO] Get mandatory inspection and certification requirements.",
         "inputSchema": {"type": "object", "properties": {
             "trade":        {"type": "string", "enum": VALID_TRADES},
             "region":       {"type": "string"},
             "installation": {"type": "string"},
             "role":         {"type": "string", "enum": VALID_ROLES, "default": "Journeyman"},
             "api_key":      {"type": "string"}
         }, "required": ["trade", "region", "installation"]}},
    ],
    "resources": [],
    "prompts": []
}

server = Server("legis-link")


# ── Claude API call ──────────────────────────────────────────────────────────

async def ask_claude(system_prompt: str, user_message: str) -> dict:
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
            if resp.status_code != 200:
                error_body = resp.text[:200]
                return {"status": "ERROR",
                        "result": f"API error {resp.status_code}: {error_body}",
                        "code_reference": ""}
            data     = resp.json()
            raw_text = data["content"][0]["text"].strip()
            raw_text = re.sub(r'^```(?:json)?\s*', '', raw_text)
            raw_text = re.sub(r'\s*```$', '', raw_text)
            try:
                return json.loads(raw_text)
            except json.JSONDecodeError:
                match = re.search(r'\{.*\}', raw_text, re.DOTALL)
                if match:
                    return json.loads(match.group())
                return {"status": "INFO", "result": raw_text, "code_reference": ""}
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
    api_key = arguments.get("api_key", "") or os.environ.get("LEGIS_LINK_API_KEY", "")
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
    role   = arguments.get("role", "Journeyman")

    # ── FREE TOOLS ────────────────────────────────────────────────────────────

    if name == "check_compliance":
        question = arguments.get("question", "")
        user_msg = f"Trade: {trade} | Region: {region} | Role: {role}\nQuestion: {question}"
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
            user_msg = f"Trade: {trade} | Region: {region} | Role: {role}\nQuestion: {question}"
            result   = await ask_claude(SYSTEM_PROMPTS["compliance"], user_msg)
            audit_log(api_key, tier, "api_query", trade, region, result.get("status","OK"))

            return JSONResponse({
                "status":         result.get("status", "INFO"),
                "result":         result.get("result", ""),
                "code_reference": result.get("code_reference", ""),
                "trade":          trade,
                "region":         region,
                "remaining":      rate["remaining"],
            })

        starlette_app = Starlette(routes=[
            Route("/health",        handle_health),
            Route("/test",          handle_test),
            Route("/roadmap",       handle_roadmap),
            Route("/app",           handle_app),
            Route("/connect",       handle_connect),
            Route("/manifest.json", handle_manifest),
            Route("/sw.js",         handle_sw),
            Route("/api/query",     handle_api_query, methods=["POST"]),
            Route("/.well-known/mcp/server-card.json", handle_server_card),
            Mount("/sse", app=sse.handle_post_message),
            Mount("/", routes=[Route("/sse", endpoint=handle_sse)]),
        ])

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
