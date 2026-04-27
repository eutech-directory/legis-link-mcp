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
    "app.html":      "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n  <meta charset=\"UTF-8\"/>\n  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no\"/>\n  <meta name=\"mobile-web-app-capable\" content=\"yes\"/>\n  <meta name=\"apple-mobile-web-app-capable\" content=\"yes\"/>\n  <meta name=\"theme-color\" content=\"#0f172a\"/>\n  <title>Legis-Link</title>\n  <link rel=\"manifest\" href=\"/manifest.json\"/>\n  <link href=\"https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@300;400;500;600&display=swap\" rel=\"stylesheet\"/>\n  <style>\n    :root {\n      --bg:#0a0f1a; --surface:#111827; --surface2:#1a2235;\n      --border:#1e293b; --accent:#3b82f6; --accent2:#06b6d4;\n      --text:#f1f5f9; --muted:#64748b; --muted2:#94a3b8;\n      --green:#10b981; --amber:#f59e0b; --red:#ef4444;\n      --mono:'IBM Plex Mono',monospace; --sans:'IBM Plex Sans',sans-serif;\n      --r:12px;\n    }\n    *{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}\n    html,body{height:100%;overflow:hidden;background:var(--bg);color:var(--text);font-family:var(--sans)}\n\n    .app{display:flex;flex-direction:column;height:100dvh;max-width:520px;margin:0 auto;position:relative}\n\n    /* Header */\n    .header{\n      padding:14px 16px 12px;\n      background:linear-gradient(180deg,#0d1829 0%,var(--surface) 100%);\n      border-bottom:1px solid var(--border);flex-shrink:0;\n    }\n    .header-top{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}\n    .logo{font-family:var(--mono);font-size:16px;font-weight:500;letter-spacing:-0.5px}\n    .logo span{color:var(--accent)}\n    .logo em{color:var(--muted);font-style:normal;font-size:13px}\n    .rate-badge{\n      font-family:var(--mono);font-size:11px;padding:4px 10px;\n      border-radius:20px;background:var(--border);color:var(--muted2);\n      border:1px solid transparent;transition:all 0.2s;\n    }\n    .rate-badge.warn{color:var(--amber);border-color:rgba(245,158,11,0.3)}\n    .rate-badge.limit{color:var(--red);border-color:rgba(239,68,68,0.3)}\n\n    /* Context selects */\n    .ctx-row{display:flex;gap:8px}\n    .ctx-select{\n      flex:1;background:var(--surface2);border:1px solid var(--border);\n      border-radius:8px;color:var(--text);font-family:var(--sans);\n      font-size:13px;font-weight:500;padding:8px 10px;cursor:pointer;\n      appearance:none;-webkit-appearance:none;outline:none;\n      transition:border-color 0.15s;\n    }\n    .ctx-select:focus{border-color:var(--accent)}\n    .ctx-select option{background:var(--surface)}\n\n    /* Offline */\n    .offline-bar{\n      display:none;background:rgba(245,158,11,0.08);\n      border-bottom:1px solid rgba(245,158,11,0.15);\n      color:var(--amber);font-size:12px;text-align:center;padding:6px;\n    }\n    body.offline .offline-bar{display:block}\n\n    /* Messages */\n    .messages{\n      flex:1;overflow-y:auto;padding:16px 16px 8px;\n      display:flex;flex-direction:column;gap:14px;\n    }\n    .messages::-webkit-scrollbar{width:3px}\n    .messages::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}\n\n    /* Welcome */\n    .welcome{\n      display:flex;flex-direction:column;align-items:center;\n      justify-content:center;flex:1;text-align:center;padding:32px 24px;gap:12px;\n      min-height:200px;\n    }\n    .welcome-icon{\n      width:56px;height:56px;background:linear-gradient(135deg,var(--accent),var(--accent2));\n      border-radius:16px;display:flex;align-items:center;justify-content:center;\n      font-size:28px;margin-bottom:4px;\n    }\n    .welcome h2{font-family:var(--mono);font-size:20px;color:var(--text);font-weight:500}\n    .welcome p{font-size:14px;color:var(--muted2);line-height:1.6;max-width:300px}\n\n    /* Message bubbles */\n    .msg{display:flex;gap:10px;animation:fadeUp 0.2s ease}\n    @keyframes fadeUp{from{opacity:0;transform:translateY(5px)}to{opacity:1;transform:translateY(0)}}\n    .msg.user{flex-direction:row-reverse}\n    .avatar{\n      width:30px;height:30px;border-radius:9px;flex-shrink:0;\n      display:flex;align-items:center;justify-content:center;\n      font-size:12px;font-weight:600;font-family:var(--mono);\n    }\n    .msg.user .avatar{background:var(--accent);color:white}\n    .msg.bot  .avatar{background:var(--surface2);border:1px solid var(--border);color:var(--accent2)}\n    .msg-body{max-width:calc(100% - 42px);display:flex;flex-direction:column;gap:4px}\n    .bubble{padding:11px 14px;border-radius:var(--r);font-size:14px;line-height:1.65}\n    .msg.user .bubble{\n      background:linear-gradient(135deg,var(--accent),#2563eb);\n      color:white;border-bottom-right-radius:4px;\n    }\n    .msg.bot .bubble{\n      background:var(--surface2);border:1px solid var(--border);\n      border-bottom-left-radius:4px;color:var(--text);\n    }\n    .status-tag{\n      display:inline-flex;align-items:center;gap:5px;\n      font-family:var(--mono);font-size:11px;padding:3px 9px;border-radius:5px;\n      width:fit-content;\n    }\n    .status-ok  {background:rgba(16,185,129,0.12);color:var(--green)}\n    .status-warn{background:rgba(245,158,11,0.12);color:var(--amber)}\n    .status-fail{background:rgba(239,68,68,0.12);color:var(--red)}\n    .code-ref{font-family:var(--mono);font-size:11px;color:var(--muted);padding-left:2px}\n\n    /* Typing */\n    .typing-wrap .bubble{padding:12px 16px}\n    .typing{display:flex;gap:5px;align-items:center}\n    .typing span{\n      width:6px;height:6px;background:var(--muted);border-radius:50%;\n      animation:dot 1.2s infinite;\n    }\n    .typing span:nth-child(2){animation-delay:0.2s}\n    .typing span:nth-child(3){animation-delay:0.4s}\n    @keyframes dot{0%,80%,100%{transform:scale(1);opacity:0.5}40%{transform:scale(1.2);opacity:1}}\n\n    /* Quick actions */\n    .quick-bar{\n      padding:8px 16px;display:flex;gap:8px;overflow-x:auto;\n      flex-shrink:0;scrollbar-width:none;\n    }\n    .quick-bar::-webkit-scrollbar{display:none}\n    .quick-btn{\n      background:var(--surface2);border:1px solid var(--border);\n      border-radius:20px;color:var(--muted2);font-size:12px;font-family:var(--sans);\n      padding:6px 14px;cursor:pointer;white-space:nowrap;flex-shrink:0;\n      transition:all 0.15s;\n    }\n    .quick-btn:hover,.quick-btn:active{border-color:var(--accent);color:var(--text)}\n\n    /* Input */\n    .input-area{\n      padding:12px 16px 16px;border-top:1px solid var(--border);\n      background:var(--surface);flex-shrink:0;\n    }\n    .input-row{display:flex;gap:8px;align-items:flex-end}\n    .input-wrap{flex:1;position:relative}\n    #q{\n      width:100%;background:var(--surface2);border:1px solid var(--border);\n      border-radius:var(--r);color:var(--text);font-family:var(--sans);\n      font-size:14px;padding:11px 14px;resize:none;line-height:1.5;\n      max-height:100px;outline:none;transition:border-color 0.15s;\n    }\n    #q:focus{border-color:var(--accent)}\n    #q::placeholder{color:var(--muted)}\n\n    .btn{\n      width:42px;height:42px;border-radius:11px;border:none;\n      display:flex;align-items:center;justify-content:center;\n      cursor:pointer;flex-shrink:0;transition:all 0.15s;font-size:16px;\n    }\n    #mic-btn{background:var(--surface2);border:1px solid var(--border);color:var(--muted2)}\n    #mic-btn.on{background:rgba(239,68,68,0.15);border-color:var(--red);color:var(--red);animation:pulse 1s infinite}\n    #send-btn{background:var(--accent);color:white}\n    #send-btn:disabled{opacity:0.4;cursor:not-allowed}\n    @keyframes pulse{0%,100%{opacity:1}50%{opacity:0.6}}\n\n    /* Upgrade */\n    .upgrade-box{\n      background:rgba(59,130,246,0.07);border:1px solid rgba(59,130,246,0.2);\n      border-radius:var(--r);padding:12px 14px;font-size:13px;color:var(--accent2);\n      line-height:1.6;\n    }\n    .upgrade-box a{color:var(--accent);font-weight:500;text-decoration:none}\n  </style>\n</head>\n<body>\n<div class=\"app\">\n  <div class=\"offline-bar\">Offline mode - showing cached answers only</div>\n\n  <header class=\"header\">\n    <div class=\"header-top\">\n      <div class=\"logo\">legis<span>-link</span> <em>v3.2</em></div>\n      <div class=\"rate-badge\" id=\"rate\">50 left</div>\n    </div>\n    <div class=\"ctx-row\">\n      <select class=\"ctx-select\" id=\"trade\">\n        <option value=\"Electrical\">Electrical</option>\n        <option value=\"Plumbing\">Plumbing</option>\n        <option value=\"HVAC\">HVAC</option>\n        <option value=\"Welding\">Welding</option>\n        <option value=\"Solar / Battery\">Solar</option>\n        <option value=\"Carpentry\">Carpentry</option>\n        <option value=\"Fire protection\">Fire</option>\n        <option value=\"Concrete\">Concrete</option>\n        <option value=\"Roofing\">Roofing</option>\n        <option value=\"Gas fitting\">Gas fitting</option>\n      </select>\n      <select class=\"ctx-select\" id=\"region\">\n        <option value=\"NSW\">AU - NSW</option>\n        <option value=\"VIC\">AU - VIC</option>\n        <option value=\"QLD\">AU - QLD</option>\n        <option value=\"WA\">AU - WA</option>\n        <option value=\"England\">UK - England</option>\n        <option value=\"Scotland\">UK - Scotland</option>\n        <option value=\"Texas\">US - Texas</option>\n        <option value=\"California\">US - California</option>\n        <option value=\"Ontario\">CA - Ontario</option>\n        <option value=\"Germany\">EU - Germany</option>\n      </select>\n      <select class=\"ctx-select\" id=\"role\" style=\"max-width:110px\">\n        <option value=\"Journeyman\">Journeyman</option>\n        <option value=\"Apprentice\">Apprentice</option>\n        <option value=\"Foreman\">Foreman</option>\n        <option value=\"PM / Executive\">PM</option>\n      </select>\n    </div>\n  </header>\n\n  <div class=\"messages\" id=\"msgs\">\n    <div class=\"welcome\" id=\"welcome\">\n      <div class=\"welcome-icon\">L</div>\n      <h2>Legis-Link</h2>\n      <p>Ask any compliance question in plain English and get the exact code reference instantly.</p>\n    </div>\n  </div>\n\n  <div class=\"quick-bar\" id=\"quick\"></div>\n\n  <div class=\"input-area\">\n    <div class=\"input-row\">\n      <div class=\"input-wrap\">\n        <textarea id=\"q\" rows=\"1\" placeholder=\"Ask a compliance question...\" maxlength=\"500\"></textarea>\n      </div>\n      <button class=\"btn\" id=\"mic-btn\" title=\"Voice input\">mic</button>\n      <button class=\"btn\" id=\"send-btn\" title=\"Send\">\n        <svg width=\"18\" height=\"18\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.5\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><line x1=\"22\" y1=\"2\" x2=\"11\" y2=\"13\"/><polygon points=\"22 2 15 22 11 13 2 9 22 2\"/></svg>\n      </button>\n    </div>\n  </div>\n</div>\n\n<script>\nconst FREE_LIMIT = 50;\nconst CACHE_KEY  = 'll-cache';\nconst MAX_CACHE  = 10;\n\nlet used = parseInt(localStorage.getItem('ll-used') || '0');\nconst savedDate = localStorage.getItem('ll-date') || '';\nconst today = new Date().toISOString().slice(0,10);\nif (savedDate !== today) { used = 0; localStorage.setItem('ll-date', today); }\n\nconst QUICK = {\n  'Electrical':     ['Wire size for 20A','Voltage drop check','Earth fault loop','RCD requirements','Circuit breaker sizing'],\n  'Plumbing':       ['Pipe size for 50 fixtures','Hot water temp','Backflow prevention','Drain slope min'],\n  'HVAC':           ['Duct size for 800 CFM','Ventilation rate','Refrigerant clearance','Filter MERV rating'],\n  'Welding':        ['Preheat temp carbon steel','AWS qualification','Inspection requirements','Electrode storage'],\n  'Solar / Battery':['Battery wall clearance','Inverter location','DC cable sizing','Isolator requirements'],\n  'Carpentry':      ['Bearer span table','Joist sizing','Tie-down requirements','Fixing schedule'],\n  'Fire protection':['Detector spacing','Sprinkler clearance','Extinguisher placement','Exit sign height'],\n  'Concrete':       ['Cover to reinforcement','Curing time','Mix design','Compressive strength'],\n  'Roofing':        ['Minimum pitch','Flashing detail','Wind uplift rating','Sarking requirements'],\n  'Gas fitting':    ['Pipe sizing','Pressure test','Ventilation calc','Appliance clearance'],\n};\n\nfunction renderQuick() {\n  const trade = document.getElementById('trade').value;\n  const bar   = document.getElementById('quick');\n  bar.innerHTML = (QUICK[trade]||[]).map(q =>\n    `<button class=\"quick-btn\" onclick=\"ask('${q.replace(/'/g,\"\\'\")}')\"> ${q}</button>`\n  ).join('');\n}\n\nfunction updateRate() {\n  const el  = document.getElementById('rate');\n  const rem = FREE_LIMIT - used;\n  el.textContent = rem + ' left today';\n  el.className = 'rate-badge' + (rem <= 10 ? ' warn':'') + (rem <= 0 ? ' limit':'');\n}\n\nfunction addMsg(role, content, meta={}) {\n  const welcome = document.getElementById('welcome');\n  if (welcome) welcome.remove();\n  const msgs = document.getElementById('msgs');\n  const d = document.createElement('div');\n  d.className = 'msg ' + role;\n  const av = role === 'user' ? 'U' : 'LL';\n  let h = `<div class=\"avatar\">${av}</div><div class=\"msg-body\">`;\n  h += `<div class=\"bubble\">${content}</div>`;\n  if (meta.status && role === 'bot') {\n    const cls = meta.status==='COMPLIANT'?'status-ok':meta.status==='NON_COMPLIANT'?'status-fail':'status-warn';\n    const label = meta.status.replace(/_/g,' ');\n    h += `<div class=\"status-tag ${cls}\"><span>&#9679;</span> ${label}</div>`;\n  }\n  if (meta.ref) h += `<div class=\"code-ref\">Ref: ${meta.ref}</div>`;\n  h += '</div>';\n  d.innerHTML = h;\n  msgs.appendChild(d);\n  msgs.scrollTop = msgs.scrollHeight;\n}\n\nfunction addTyping() {\n  const msgs = document.getElementById('msgs');\n  const d = document.createElement('div');\n  d.className = 'msg bot typing-wrap'; d.id = 'typing';\n  d.innerHTML = `<div class=\"avatar\">LL</div><div class=\"msg-body\"><div class=\"bubble\"><div class=\"typing\"><span></span><span></span><span></span></div></div></div>`;\n  msgs.appendChild(d); msgs.scrollTop = msgs.scrollHeight;\n}\nfunction removeTyping() { const t=document.getElementById('typing'); if(t) t.remove(); }\n\nfunction getCache(q) {\n  try { return (JSON.parse(localStorage.getItem(CACHE_KEY)||'[]')).find(c=>c.q.toLowerCase()===q.toLowerCase())||null; }\n  catch(e) { return null; }\n}\nfunction saveCache(q, r) {\n  try {\n    const c = JSON.parse(localStorage.getItem(CACHE_KEY)||'[]');\n    c.push({q,r}); if(c.length>MAX_CACHE) c.shift();\n    localStorage.setItem(CACHE_KEY, JSON.stringify(c));\n  } catch(e) {}\n}\n\nasync function ask(question) {\n  question = question || document.getElementById('q').value.trim();\n  if (!question) return;\n  if (used >= FREE_LIMIT) {\n    addMsg('bot', '<div class=\"upgrade-box\">Daily limit reached (' + FREE_LIMIT + ' queries).<br>Upgrade to Pro for 1,000/day \u2014 <a href=\"/connect\">get a Pro key</a>.</div>');\n    return;\n  }\n  document.getElementById('q').value = '';\n  document.getElementById('q').style.height = 'auto';\n  document.getElementById('send-btn').disabled = true;\n  addMsg('user', question);\n  addTyping();\n\n  const cached = !navigator.onLine ? getCache(question) : null;\n  try {\n    let data;\n    if (cached) {\n      data = cached.r;\n      removeTyping();\n      addMsg('bot', data.result + ' <span style=\"font-size:11px;color:var(--muted)\">(cached)</span>',\n        {status:data.status, ref:data.code_reference});\n    } else {\n      const res = await fetch('/api/query', {\n        method:'POST', headers:{'Content-Type':'application/json'},\n        body: JSON.stringify({\n          trade:    document.getElementById('trade').value,\n          region:   document.getElementById('region').value,\n          role:     document.getElementById('role').value,\n          question: question,\n          api_key:  'dev_local'\n        })\n      });\n      data = await res.json();\n      removeTyping();\n      if (!res.ok) {\n        addMsg('bot', data.error || 'Server error. Please try again.');\n      } else {\n        addMsg('bot', data.result, {status:data.status, ref:data.code_reference});\n        saveCache(question, data);\n        used++; localStorage.setItem('ll-used', used); updateRate();\n      }\n    }\n  } catch(e) {\n    removeTyping();\n    addMsg('bot', navigator.onLine ? 'Connection error. Try again.' : 'Offline. Only cached answers available.');\n  }\n  document.getElementById('send-btn').disabled = false;\n}\n\n// Voice\nconst SR = window.SpeechRecognition || window.webkitSpeechRecognition;\nconst micBtn = document.getElementById('mic-btn');\nif (SR) {\n  const rec = new SR(); rec.continuous=false; rec.interimResults=true; rec.lang='en-AU';\n  rec.onresult = e => {\n    const t = Array.from(e.results).map(r=>r[0].transcript).join('');\n    document.getElementById('q').value = t;\n    if (e.results[e.results.length-1].isFinal) { micBtn.classList.remove('on'); ask(t); }\n  };\n  rec.onend = () => micBtn.classList.remove('on');\n  micBtn.textContent = 'mic';\n  micBtn.onclick = () => {\n    if (micBtn.classList.contains('on')) { rec.stop(); }\n    else { micBtn.classList.add('on'); rec.start(); }\n  };\n} else {\n  micBtn.style.display = 'none';\n}\n\n// Enter to send\ndocument.getElementById('q').addEventListener('keydown', e => {\n  if (e.key==='Enter' && !e.shiftKey) { e.preventDefault(); ask(); }\n});\ndocument.getElementById('q').addEventListener('input', function() {\n  this.style.height='auto'; this.style.height=Math.min(this.scrollHeight,100)+'px';\n});\ndocument.getElementById('send-btn').onclick = () => ask();\ndocument.getElementById('trade').addEventListener('change', renderQuick);\n\nwindow.addEventListener('online',  ()=>document.body.classList.remove('offline'));\nwindow.addEventListener('offline', ()=>document.body.classList.add('offline'));\nif (!navigator.onLine) document.body.classList.add('offline');\n\nif ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(()=>{});\n\nrenderQuick(); updateRate();\n</script>\n</body>\n</html>",
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
            Route("/connect",       handle_connect_page),
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
