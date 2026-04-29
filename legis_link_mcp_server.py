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
OPENAI_API_KEY    = os.environ.get("OPENAI_API_KEY", "")
OPENAI_URL        = "https://api.openai.com/v1/chat/completions"
OPENAI_MODEL      = "gpt-4o-mini"
PRO_UPGRADE       = "https://legis-link-mcp-production-3e9b.up.railway.app/upgrade"
VERSION           = "3.2.1"
# ── Page content (embedded — no filesystem dependency) ────────────────────
_PAGES = {
    "app.html":      "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n  <meta charset=\"UTF-8\"/>\n  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no\"/>\n  <meta name=\"mobile-web-app-capable\" content=\"yes\"/>\n  <meta name=\"apple-mobile-web-app-capable\" content=\"yes\"/>\n  <meta name=\"theme-color\" content=\"#0f172a\"/>\n  <title>Legis-Link</title>\n  <link rel=\"manifest\" href=\"/manifest.json\"/>\n  <link href=\"https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&display=swap\" rel=\"stylesheet\"/>\n  <style>\n    :root{\n      --bg:#0a0f1a;--surface:#111827;--surface2:#1a2235;\n      --border:#1e293b;--accent:#3b82f6;--accent2:#06b6d4;\n      --text:#f1f5f9;--muted:#64748b;--muted2:#94a3b8;\n      --green:#10b981;--amber:#f59e0b;--red:#ef4444;\n      --mono:\"IBM Plex Mono\",monospace;--sans:\"IBM Plex Sans\",sans-serif;\n      --r:10px;--sb:64px;\n    }\n    *{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}\n    html,body{height:100%;overflow:hidden;background:var(--bg);color:var(--text);font-family:var(--sans)}\n    .app{display:flex;height:100dvh}\n\n    /* Sidebar */\n    .sb{width:var(--sb);flex-shrink:0;background:var(--surface);border-right:1px solid var(--border);display:flex;flex-direction:column;overflow-y:auto;scrollbar-width:none}\n    .sb::-webkit-scrollbar{display:none}\n    .sb-logo{font-family:var(--mono);font-size:12px;color:var(--accent);text-align:center;padding:10px 0 8px;border-bottom:1px solid var(--border);flex-shrink:0}\n    .tb{display:flex;flex-direction:column;align-items:center;gap:2px;padding:8px 4px;margin:2px 4px;border-radius:8px;cursor:pointer;border:1px solid transparent;background:none;color:var(--muted);font-family:var(--sans);transition:all 0.15s;flex-shrink:0}\n    .tb:active,.tb.on{background:rgba(59,130,246,0.12);border-color:rgba(59,130,246,0.25);color:var(--accent)}\n    .ti{font-size:20px;line-height:1}\n    .tn{font-size:9px;font-weight:500;text-align:center;line-height:1.2;margin-top:1px}\n\n    /* Main */\n    .main{flex:1;display:flex;flex-direction:column;min-width:0;overflow:hidden}\n\n    /* Header */\n    .hdr{background:var(--surface);border-bottom:1px solid var(--border);padding:8px 10px;flex-shrink:0}\n    .hdr-r1{display:flex;align-items:center;justify-content:space-between;margin-bottom:6px}\n    .trade-lbl{font-family:var(--mono);font-size:13px;color:var(--accent);font-weight:500}\n    .rate-pill{font-family:var(--mono);font-size:10px;padding:3px 8px;border-radius:20px;background:var(--border);color:var(--muted2);white-space:nowrap;flex-shrink:0}\n    .rate-pill.warn{color:var(--amber)}\n    .rate-pill.limit{color:var(--red)}\n    .hdr-r2{display:flex;gap:6px}\n    .csel{flex:1;min-width:0;background:var(--surface2);border:1px solid var(--border);border-radius:7px;color:var(--text);font-family:var(--sans);font-size:12px;padding:6px 8px;cursor:pointer;appearance:none;-webkit-appearance:none;outline:none}\n    .csel:focus{border-color:var(--accent)}\n    .csel option{background:var(--surface)}\n\n    /* Tool selector */\n    .tools-bar{padding:6px 10px;display:flex;gap:5px;overflow-x:auto;flex-shrink:0;border-bottom:1px solid var(--border);scrollbar-width:none;background:var(--bg)}\n    .tools-bar::-webkit-scrollbar{display:none}\n    .tool-btn{\n      display:flex;align-items:center;gap:5px;\n      padding:5px 10px;border-radius:7px;border:1px solid var(--border);\n      background:var(--surface);color:var(--muted2);font-size:11px;\n      font-family:var(--sans);cursor:pointer;white-space:nowrap;flex-shrink:0;\n      transition:all 0.15s;\n    }\n    .tool-btn:active,.tool-btn.active{background:rgba(59,130,246,0.12);border-color:rgba(59,130,246,0.35);color:var(--accent)}\n    .tool-btn.pro-tool{border-color:rgba(6,182,212,0.25);color:var(--accent2)}\n    .tool-btn.pro-tool.active{background:rgba(6,182,212,0.12);border-color:var(--accent2);color:var(--accent2)}\n    .tool-dot{width:6px;height:6px;border-radius:50%;background:currentColor;flex-shrink:0}\n    .pro-badge{font-size:8px;background:rgba(6,182,212,0.2);color:var(--accent2);padding:1px 4px;border-radius:3px;font-weight:600}\n\n    /* Offline */\n    .offbar{display:none;background:rgba(245,158,11,0.08);border-bottom:1px solid rgba(245,158,11,0.15);color:var(--amber);font-size:11px;text-align:center;padding:5px;flex-shrink:0}\n    body.offline .offbar{display:block}\n\n    /* Messages */\n    .msgs{flex:1;overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:12px}\n    .msgs::-webkit-scrollbar{width:3px}\n    .msgs::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}\n\n    .welcome{display:flex;flex-direction:column;align-items:center;justify-content:center;flex:1;text-align:center;padding:20px;gap:10px;min-height:140px}\n    .wlogo{width:46px;height:46px;background:linear-gradient(135deg,var(--accent),var(--accent2));border-radius:13px;display:flex;align-items:center;justify-content:center;font-family:var(--mono);font-size:18px;font-weight:600;color:white}\n    .welcome h2{font-family:var(--mono);font-size:16px;font-weight:500}\n    .welcome p{font-size:12px;color:var(--muted2);line-height:1.6;max-width:230px}\n\n    .msg{display:flex;gap:8px;animation:up 0.2s ease}\n    @keyframes up{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:translateY(0)}}\n    .msg.user{flex-direction:row-reverse}\n    .av{width:27px;height:27px;border-radius:8px;flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:600;font-family:var(--mono)}\n    .msg.user .av{background:var(--accent);color:white}\n    .msg.bot  .av{background:var(--surface2);border:1px solid var(--border);color:var(--accent2)}\n    .mb{max-width:calc(100% - 37px);display:flex;flex-direction:column;gap:4px}\n    .bbl{padding:9px 12px;border-radius:var(--r);font-size:13px;line-height:1.65}\n    .msg.user .bbl{background:linear-gradient(135deg,var(--accent),#2563eb);color:white;border-bottom-right-radius:3px}\n    .msg.bot  .bbl{background:var(--surface2);border:1px solid var(--border);border-bottom-left-radius:3px}\n    .tool-tag{display:inline-flex;align-items:center;gap:4px;font-family:var(--mono);font-size:9px;padding:2px 7px;border-radius:4px;width:fit-content;background:var(--border);color:var(--muted);margin-bottom:2px}\n    .stag{display:inline-flex;align-items:center;gap:4px;font-family:var(--mono);font-size:10px;padding:2px 8px;border-radius:4px;width:fit-content}\n    .ok{background:rgba(16,185,129,0.12);color:var(--green)}\n    .wn{background:rgba(245,158,11,0.12);color:var(--amber)}\n    .fl{background:rgba(239,68,68,0.12);color:var(--red)}\n    .crf{font-family:var(--mono);font-size:10px;color:var(--muted)}\n\n    .typing{display:flex;gap:4px;align-items:center;padding:2px 0}\n    .typing span{width:5px;height:5px;background:var(--muted);border-radius:50%;animation:dot 1.2s infinite}\n    .typing span:nth-child(2){animation-delay:0.2s}\n    .typing span:nth-child(3){animation-delay:0.4s}\n    @keyframes dot{0%,80%,100%{transform:scale(1);opacity:0.5}40%{transform:scale(1.3);opacity:1}}\n\n    /* Quick bar */\n    .qbar{padding:6px 10px;display:flex;gap:6px;overflow-x:auto;flex-shrink:0;scrollbar-width:none}\n    .qbar::-webkit-scrollbar{display:none}\n    .qb{background:var(--surface2);border:1px solid var(--border);border-radius:16px;color:var(--muted2);font-size:11px;padding:5px 11px;cursor:pointer;white-space:nowrap;flex-shrink:0;font-family:var(--sans);transition:all 0.15s}\n    .qb:active{border-color:var(--accent);color:var(--text)}\n\n    /* Input */\n    .inp{padding:8px 10px 12px;border-top:1px solid var(--border);background:var(--surface);flex-shrink:0}\n    .inpr{display:flex;gap:6px;align-items:flex-end}\n    textarea#q{flex:1;background:var(--surface2);border:1px solid var(--border);border-radius:var(--r);color:var(--text);font-family:var(--sans);font-size:13px;padding:9px 11px;resize:none;line-height:1.5;max-height:80px;outline:none;transition:border-color 0.15s}\n    textarea#q:focus{border-color:var(--accent)}\n    textarea#q::placeholder{color:var(--muted)}\n    .ibtn{width:38px;height:38px;border-radius:9px;border:none;display:flex;align-items:center;justify-content:center;cursor:pointer;flex-shrink:0;transition:all 0.15s}\n    #mic{background:var(--surface2);border:1px solid var(--border);color:var(--muted2);font-size:13px;font-family:var(--sans)}\n    #mic.on{background:rgba(239,68,68,0.15);border-color:var(--red);color:var(--red);animation:pulse 1s infinite}\n    #send{background:var(--accent);color:white}\n    #send:disabled{opacity:0.4;cursor:not-allowed}\n    @keyframes pulse{0%,100%{opacity:1}50%{opacity:0.5}}\n    .upbox{background:rgba(59,130,246,0.07);border:1px solid rgba(59,130,246,0.2);border-radius:var(--r);padding:10px 12px;font-size:13px;color:var(--accent2);line-height:1.7}\n    .upbox a{color:var(--accent);font-weight:600;text-decoration:none}\n    .upbox strong{color:var(--text)}\n  </style>\n</head>\n<body>\n<div class=\"app\">\n  <div class=\"sb\" id=\"sb\">\n    <div class=\"sb-logo\">LL</div>\n  </div>\n  <div class=\"main\">\n    <div class=\"offbar\">Offline - cached answers only</div>\n    <header class=\"hdr\">\n      <div class=\"hdr-r1\">\n        <div class=\"trade-lbl\" id=\"tlbl\">Electrical</div>\n        <div class=\"rate-pill\" id=\"rate\">50 left</div>\n      </div>\n      <div class=\"hdr-r2\">\n        <select class=\"csel\" id=\"region\">\n          <option value=\"NSW\">AU - NSW</option>\n          <option value=\"VIC\">AU - VIC</option>\n          <option value=\"QLD\">AU - QLD</option>\n          <option value=\"WA\">AU - WA</option>\n          <option value=\"England\">UK - England</option>\n          <option value=\"Scotland\">UK - Scotland</option>\n          <option value=\"Wales\">UK - Wales</option>\n          <option value=\"Texas\">US - Texas</option>\n          <option value=\"California\">US - California</option>\n          <option value=\"Ontario\">CA - Ontario</option>\n          <option value=\"Germany\">EU - Germany</option>\n          <option value=\"France\">EU - France</option>\n        </select>\n        <select class=\"csel\" id=\"role\" style=\"max-width:105px\">\n          <option value=\"Journeyman\">Journeyman</option>\n          <option value=\"Apprentice\">Apprentice</option>\n          <option value=\"Foreman\">Foreman</option>\n          <option value=\"PM / Executive\">PM</option>\n        </select>\n      </div>\n    </header>\n\n    <!-- Tool selector -->\n    <div class=\"tools-bar\" id=\"tools-bar\">\n      <button class=\"tool-btn active\" data-tool=\"check_compliance\">\n        <span class=\"tool-dot\"></span>Compliance\n      </button>\n      <button class=\"tool-btn pro-tool\" data-tool=\"calculate_technical_spec\">\n        <span class=\"tool-dot\"></span>Calc<span class=\"pro-badge\">PRO</span>\n      </button>\n      <button class=\"tool-btn pro-tool\" data-tool=\"generate_safety_checklist\">\n        <span class=\"tool-dot\"></span>Safety<span class=\"pro-badge\">PRO</span>\n      </button>\n      <button class=\"tool-btn pro-tool\" data-tool=\"generate_rams\">\n        <span class=\"tool-dot\"></span>RAMS<span class=\"pro-badge\">PRO</span>\n      </button>\n      <button class=\"tool-btn pro-tool\" data-tool=\"verify_material_compliance\">\n        <span class=\"tool-dot\"></span>Material<span class=\"pro-badge\">PRO</span>\n      </button>\n      <button class=\"tool-btn pro-tool\" data-tool=\"get_inspection_requirements\">\n        <span class=\"tool-dot\"></span>Inspection<span class=\"pro-badge\">PRO</span>\n      </button>\n    </div>\n\n    <div class=\"msgs\" id=\"msgs\">\n      <div class=\"welcome\" id=\"welcome\">\n        <div class=\"wlogo\">LL</div>\n        <h2>Legis-Link</h2>\n        <p>Select a tool above or ask any compliance question. Pro tools auto-detected from your question.</p>\n      </div>\n    </div>\n    <div class=\"qbar\" id=\"qbar\"></div>\n    <div class=\"inp\">\n      <div class=\"inpr\">\n        <textarea id=\"q\" rows=\"1\" placeholder=\"Ask a compliance question...\"></textarea>\n        <button class=\"ibtn\" id=\"mic\">mic</button>\n        <button class=\"ibtn\" id=\"send\">\n          <svg width=\"15\" height=\"15\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.5\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><line x1=\"22\" y1=\"2\" x2=\"11\" y2=\"13\"/><polygon points=\"22 2 15 22 11 13 2 9 22 2\"/></svg>\n        </button>\n      </div>\n    </div>\n  </div>\n</div>\n<script>\nconst TRADES = [\n  {id:\"Electrical\",     icon:\"&#9889;\", short:\"Elec\"},\n  {id:\"Plumbing\",       icon:\"&#128295;\",short:\"Plumb\"},\n  {id:\"HVAC\",           icon:\"&#10052;\", short:\"HVAC\"},\n  {id:\"Gas fitting\",    icon:\"&#128293;\",short:\"Gas\"},\n  {id:\"Welding\",        icon:\"&#128293;\",short:\"Weld\"},\n  {id:\"Solar / Battery\",icon:\"&#9728;\",  short:\"Solar\"},\n  {id:\"Fire protection\",icon:\"&#128680;\",short:\"Fire\"},\n  {id:\"Carpentry\",      icon:\"&#128296;\",short:\"Carp\"},\n  {id:\"Concrete\",       icon:\"&#9632;\",  short:\"Conc\"},\n  {id:\"Roofing\",        icon:\"&#8963;\",  short:\"Roof\"},\n];\n\nconst QUICK = {\n  \"Electrical\":      [\"Wire size 20A\",\"Voltage drop\",\"Earth fault loop\",\"RCD requirements\",\"Breaker sizing\"],\n  \"Plumbing\":        [\"Pipe size 50 fixtures\",\"Hot water temp\",\"Backflow prevention\",\"Drain slope\"],\n  \"HVAC\":            [\"Duct size 800 CFM\",\"Ventilation rate\",\"Refrigerant clearance\",\"MERV rating\"],\n  \"Gas fitting\":     [\"Pipe sizing\",\"Pressure test\",\"Ventilation calc\",\"Appliance clearance\"],\n  \"Welding\":         [\"Preheat temp\",\"AWS qualification\",\"Inspection requirements\",\"Electrode storage\"],\n  \"Solar / Battery\": [\"Battery clearance\",\"Inverter location\",\"DC cable sizing\",\"Isolator requirements\"],\n  \"Fire protection\": [\"Detector spacing\",\"Sprinkler clearance\",\"Extinguisher placement\",\"Exit sign height\"],\n  \"Carpentry\":       [\"Bearer span\",\"Joist sizing\",\"Tie-down requirements\",\"Fixing schedule\"],\n  \"Concrete\":        [\"Cover to reo\",\"Curing time\",\"Mix design\",\"Compressive strength\"],\n  \"Roofing\":         [\"Min pitch\",\"Flashing detail\",\"Wind uplift\",\"Sarking requirements\"],\n};\n\nconst TOOL_LABELS = {\n  \"check_compliance\":           \"Compliance\",\n  \"calculate_technical_spec\":   \"Calc\",\n  \"generate_safety_checklist\":  \"Safety\",\n  \"generate_rams\":              \"RAMS\",\n  \"verify_material_compliance\": \"Material\",\n  \"get_inspection_requirements\":\"Inspection\",\n};\n\nconst FREE_LIMIT = 50;\nconst CACHE_KEY  = \"ll-cache\";\nlet activeTrade  = \"Electrical\";\nlet activeTool   = \"check_compliance\"; // default, overridden by auto-detect\nlet manualTool   = false; // true when user explicitly picked a tool\n\nlet used = parseInt(localStorage.getItem(\"ll-used\") || \"0\");\nconst savedDate = localStorage.getItem(\"ll-date\") || \"\";\nconst today = new Date().toISOString().slice(0,10);\nif (savedDate !== today) { used = 0; localStorage.setItem(\"ll-date\", today); }\n\n// \u2500\u2500 Auto-detect tool from question \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\nfunction detectTool(q) {\n  const ql = q.toLowerCase();\n  if (/cable size|wire size|pipe size|duct size|voltage drop|current rating|load calc|sizing|ampacity|flow rate|heat load|cooling load|what size|calculate/.test(ql))\n    return \"calculate_technical_spec\";\n  if (/rams|risk assessment|method statement|jha|job hazard|swms|safe work method/.test(ql))\n    return \"generate_rams\";\n  if (/safety checklist|ppe|hazard|toolbox|induction|safety check|what safety|safe to/.test(ql))\n    return \"generate_safety_checklist\";\n  if (/material|compliant|approved|can i use|is this cable|is this pipe|product|brand|specification|meets code/.test(ql))\n    return \"verify_material_compliance\";\n  if (/inspection|sign off|certificate|hold point|certifier|who inspects|test and tag|commissioning|handover/.test(ql))\n    return \"get_inspection_requirements\";\n  return \"check_compliance\";\n}\n\nfunction setActiveTool(toolId, fromUser=false) {\n  activeTool = toolId;\n  manualTool = fromUser;\n  document.querySelectorAll(\".tool-btn\").forEach(b => {\n    b.classList.toggle(\"active\", b.dataset.tool === toolId);\n  });\n}\n\n// \u2500\u2500 Build sidebar \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\nconst sb = document.getElementById(\"sb\");\nTRADES.forEach((t,i) => {\n  const btn = document.createElement(\"button\");\n  btn.className = \"tb\" + (i===0?\" on\":\"\");\n  btn.dataset.trade = t.id;\n  btn.innerHTML = `<span class=\"ti\">${t.icon}</span><span class=\"tn\">${t.short}</span>`;\n  btn.addEventListener(\"click\", function() {\n    document.querySelectorAll(\".tb\").forEach(b => b.classList.remove(\"on\"));\n    this.classList.add(\"on\");\n    activeTrade = this.dataset.trade;\n    document.getElementById(\"tlbl\").textContent = activeTrade;\n    renderQuick();\n  });\n  sb.appendChild(btn);\n});\n\n// \u2500\u2500 Tool button clicks \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\ndocument.querySelectorAll(\".tool-btn\").forEach(btn => {\n  btn.addEventListener(\"click\", function() {\n    setActiveTool(this.dataset.tool, true);\n    // Update placeholder to hint at what to ask\n    const hints = {\n      \"check_compliance\":           \"Ask any compliance question...\",\n      \"calculate_technical_spec\":   \"e.g. Cable size for 32A circuit in conduit, 25m run...\",\n      \"generate_safety_checklist\":  \"e.g. Safety checklist for working at height...\",\n      \"generate_rams\":              \"e.g. RAMS for electrical switchboard installation...\",\n      \"verify_material_compliance\": \"e.g. Is 6mm2 TPS cable approved for this use...\",\n      \"get_inspection_requirements\":\"e.g. Inspection requirements for timber frame NSW...\",\n    };\n    document.getElementById(\"q\").placeholder = hints[this.dataset.tool] || \"Ask a compliance question...\";\n    document.getElementById(\"q\").focus();\n  });\n});\n\nfunction renderQuick() {\n  const bar = document.getElementById(\"qbar\");\n  const qs  = QUICK[activeTrade] || [];\n  bar.innerHTML = \"\";\n  qs.forEach(q => {\n    const btn = document.createElement(\"button\");\n    btn.className = \"qb\";\n    btn.textContent = q;\n    btn.addEventListener(\"click\", function() { ask(this.textContent); });\n    bar.appendChild(btn);\n  });\n}\n\nfunction updateRate() {\n  const el  = document.getElementById(\"rate\");\n  const rem = FREE_LIMIT - used;\n  el.textContent = rem + \" left\";\n  el.className = \"rate-pill\" + (rem<=10?\" warn\":\"\") + (rem<=0?\" limit\":\"\");\n}\n\nfunction addMsg(role, content, meta={}) {\n  const w = document.getElementById(\"welcome\"); if(w) w.remove();\n  const msgs = document.getElementById(\"msgs\");\n  const d = document.createElement(\"div\");\n  d.className = \"msg \" + role;\n  const av = role===\"user\" ? \"U\" : \"LL\";\n  let h = `<div class=\"av\">${av}</div><div class=\"mb\">`;\n  if (meta.tool && role===\"bot\") {\n    h += `<div class=\"tool-tag\">${TOOL_LABELS[meta.tool]||meta.tool}</div>`;\n  }\n  h += `<div class=\"bbl\">${content}</div>`;\n  if (meta.status && role===\"bot\") {\n    const cls = meta.status===\"COMPLIANT\"?\"ok\":meta.status===\"NON_COMPLIANT\"?\"fl\":\"wn\";\n    h += `<div class=\"stag ${cls}\">&#9679; ${meta.status.replace(/_/g,\" \")}</div>`;\n  }\n  if (meta.ref) h += `<div class=\"crf\">Ref: ${meta.ref}</div>`;\n  h += \"</div>\";\n  d.innerHTML = h;\n  msgs.appendChild(d);\n  msgs.scrollTop = msgs.scrollHeight;\n}\n\nfunction addTyping() {\n  const msgs = document.getElementById(\"msgs\");\n  const d = document.createElement(\"div\");\n  d.className = \"msg bot\"; d.id = \"typing\";\n  d.innerHTML = `<div class=\"av\">LL</div><div class=\"mb\"><div class=\"bbl\"><div class=\"typing\"><span></span><span></span><span></span></div></div></div>`;\n  msgs.appendChild(d); msgs.scrollTop = msgs.scrollHeight;\n}\nfunction rmTyping() { const t=document.getElementById(\"typing\"); if(t) t.remove(); }\n\nfunction getCache(q) {\n  try { return (JSON.parse(localStorage.getItem(CACHE_KEY)||\"[]\")).find(c=>c.q.toLowerCase()===q.toLowerCase())||null; }\n  catch(e) { return null; }\n}\nfunction saveCache(q,r) {\n  try {\n    const c = JSON.parse(localStorage.getItem(CACHE_KEY)||\"[]\");\n    c.push({q,r}); if(c.length>10) c.shift();\n    localStorage.setItem(CACHE_KEY, JSON.stringify(c));\n  } catch(e) {}\n}\n\nasync function ask(question) {\n  question = (question || document.getElementById(\"q\").value).trim();\n  if (!question) return;\n\n  if (used >= FREE_LIMIT) {\n    addMsg(\"bot\", `<div class=\"upbox\">\n      <strong>You have used all ${FREE_LIMIT} free queries today.</strong><br><br>\n      Legis-Link Pro gives you:<br>\n      &#10003; 1,000 queries/day<br>\n      &#10003; All 8 tools: cable sizing, RAMS, safety checklists, material compliance, inspections<br>\n      &#10003; All regions: AU, UK, US, Canada, EU<br><br>\n      <a href=\"https://rickyfarmer.gumroad.com/l/Legis-LinkPro\" target=\"_blank\">Get Pro Access &mdash; $199/year &rarr;</a><br>\n      <span style=\"font-size:10px;color:var(--muted)\">Free queries reset at midnight UTC each day.</span>\n    </div>`);\n    return;\n  }\n\n  // Auto-detect tool unless user manually selected\n  const tool = manualTool ? activeTool : detectTool(question);\n  setActiveTool(tool, false);\n\n  document.getElementById(\"q\").value = \"\";\n  document.getElementById(\"q\").style.height = \"auto\";\n  document.getElementById(\"send\").disabled = true;\n  addMsg(\"user\", question);\n  addTyping();\n\n  const cached = !navigator.onLine ? getCache(question) : null;\n  try {\n    if (cached) {\n      rmTyping();\n      addMsg(\"bot\",\n        cached.r.result + ' <span style=\"font-size:10px;color:var(--muted)\">(cached)</span>',\n        {status:cached.r.status, ref:cached.r.code_reference, tool});\n    } else {\n      const res = await fetch(\"/api/query\", {\n        method: \"POST\",\n        headers: {\"Content-Type\":\"application/json\"},\n        body: JSON.stringify({\n          trade:   activeTrade,\n          region:  document.getElementById(\"region\").value,\n          role:    document.getElementById(\"role\").value,\n          question,\n          tool,\n          api_key: \"dev_local\"\n        })\n      });\n      const data = await res.json();\n      rmTyping();\n      if (!res.ok) {\n        addMsg(\"bot\", data.error || \"Server error. Please try again.\");\n      } else {\n        addMsg(\"bot\", data.result, {status:data.status, ref:data.code_reference, tool});\n        saveCache(question, data);\n        used++; localStorage.setItem(\"ll-used\", used); updateRate();\n      }\n    }\n  } catch(e) {\n    rmTyping();\n    addMsg(\"bot\", navigator.onLine ? \"Connection error. Try again.\" : \"Offline. Cached answers only.\");\n  }\n  document.getElementById(\"send\").disabled = false;\n  // Reset to auto-detect after each question\n  manualTool = false;\n}\n\n// Voice\nconst SR = window.SpeechRecognition || window.webkitSpeechRecognition;\nconst mic = document.getElementById(\"mic\");\nif (SR) {\n  const rec = new SR();\n  rec.continuous = false; rec.interimResults = true; rec.lang = \"en-AU\";\n  rec.onresult = e => {\n    const t = Array.from(e.results).map(r=>r[0].transcript).join(\"\");\n    document.getElementById(\"q\").value = t;\n    if (e.results[e.results.length-1].isFinal) { mic.classList.remove(\"on\"); ask(t); }\n  };\n  rec.onend = () => mic.classList.remove(\"on\");\n  mic.addEventListener(\"click\", () => {\n    if (mic.classList.contains(\"on\")) rec.stop();\n    else { mic.classList.add(\"on\"); rec.start(); }\n  });\n} else { mic.style.display = \"none\"; }\n\ndocument.getElementById(\"q\").addEventListener(\"keydown\", e => {\n  if (e.key===\"Enter\" && !e.shiftKey) { e.preventDefault(); ask(); }\n});\ndocument.getElementById(\"q\").addEventListener(\"input\", function() {\n  this.style.height = \"auto\";\n  this.style.height = Math.min(this.scrollHeight, 80) + \"px\";\n});\ndocument.getElementById(\"send\").addEventListener(\"click\", () => ask());\n\nwindow.addEventListener(\"online\",  () => document.body.classList.remove(\"offline\"));\nwindow.addEventListener(\"offline\", () => document.body.classList.add(\"offline\"));\nif (!navigator.onLine) document.body.classList.add(\"offline\");\nif (\"serviceWorker\" in navigator) navigator.serviceWorker.register(\"/sw.js\").catch(()=>{});\n\nrenderQuick();\nupdateRate();\n</script>\n</body>\n</html>",
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
            "free": "50 requests/day, 3 tools — use api_key: dev_local for testing",
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
                "role":    {"type": "string", "enum": VALID_ROLES, "default": "Journeyman",
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
            }
            prompt_key = prompt_map.get(tool, "compliance")
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
