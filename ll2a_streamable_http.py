#!/usr/bin/env python3
"""
Legis-Link LL-2a: streamable HTTP at /mcp (v3.4.0).

  1. New MCP address https://legis-link-mcp-production-3e9b.up.railway.app/mcp using streamable HTTP,
     the transport Claude connectors and new MCP clients expect. /sse keeps working for older clients.
     Claude's "Add custom connector" check now passes with no sign-in.
  2. Pro users can connect with their key in the URL (?key=ll_p_...), an X-API-Key header or
     Authorization: Bearer - no need to type it in every question.
  3. Connect page, server card and README show the new address and correct setup for Claude,
     Claude Desktop (mcp-remote), Cursor, Windsurf and VS Code.

Run AFTER LL-1, in the legis-link-mcp folder:
    python ll2a_streamable_http.py
Atomic: nothing is written if any part does not match.
"""
import ast
import datetime
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MAIN = os.environ.get("LL_MAIN") or os.path.join(HERE, "legis_link_mcp_server.py")
README = os.path.join(os.path.dirname(MAIN), "README.md")
MARK = "LL-2a: streamable HTTP at /mcp"

SWAPS = [('            Mount("/messages/", app=sse.handle_post_message),\n', '            Mount("/messages/", app=sse.handle_post_message),\n            Route("/mcp", endpoint=_LLMCP(), methods=["GET", "POST", "DELETE"]),\n', 1), ('        starlette_app = Starlette(routes=[\n', '        # ── LL-2a: streamable HTTP at /mcp (the transport new MCP clients and Claude connectors expect) ──\n        from contextlib import asynccontextmanager as _acm\n        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager as _SHSM\n        _ll_shsm = _SHSM(app=server, event_store=None, json_response=False, stateless=True)\n\n        class _LLMCP:\n            """ASGI app for /mcp. Picks up the visitor\'s IP and an optional Pro key from ?key=, X-API-Key or Bearer."""\n            async def __call__(self, scope, receive, send):\n                from starlette.requests import Request as _Rq\n                rq = _Rq(scope, receive)\n                _LL_CLIENT.set(_client_ip(rq))\n                _LL_KEY.set(_request_key(rq))\n                await _ll_shsm.handle_request(scope, receive, send)\n\n        @_acm\n        async def _ll_lifespan(app):\n            async with _ll_shsm.run():\n                yield\n\n        starlette_app = Starlette(routes=[\n', 1), ('            Mount("/", routes=[Route("/sse", endpoint=handle_sse)]),\n        ])\n', '            Mount("/", routes=[Route("/sse", endpoint=handle_sse)]),\n        ], lifespan=_ll_lifespan)\n', 1), ('            _LL_CLIENT.set(_client_ip(request))\n            async with sse.connect_sse(', '            _LL_CLIENT.set(_client_ip(request))\n            _LL_KEY.set(_request_key(request))\n            async with sse.connect_sse(', 1), ('def validate_api_key(', '_LL_KEY = _cv.ContextVar("ll_key", default="")\n\n\ndef _request_key(request) -> str:\n    """Optional Pro key sent with the connection: ?key=..., X-API-Key header or Authorization: Bearer ll_..."""\n    k = (request.query_params.get("key", "") or request.headers.get("x-api-key", "")).strip()\n    if not k:\n        a = request.headers.get("authorization", "")\n        if a.lower().startswith("bearer ") and a[7:].strip().startswith("ll_"):\n            k = a[7:].strip()\n    return k[:80]\n\n\ndef validate_api_key(', 1), ('    api_key = arguments.get("api_key", "") or os.environ.get("LEGIS_LINK_API_KEY", "")\n', '    api_key = arguments.get("api_key", "") or _LL_KEY.get() or os.environ.get("LEGIS_LINK_API_KEY", "")\n', 1), ('    "serverInfo": {"name": "Legis-Link", "version": VERSION},\n', '    "serverInfo": {"name": "Legis-Link", "version": VERSION},\n    "transport": {"type": "streamable-http", "url": "https://legis-link-mcp-production-3e9b.up.railway.app/mcp", "sse_url": "https://legis-link-mcp-production-3e9b.up.railway.app/sse"},\n', 1), ('"header": "X-API-Key",', '"header": "X-API-Key (optional; or ?key= in the URL, or Authorization: Bearer)",', 1), ('<h2>\\ud83d\\udcf1 Mobile \\u2014 Recommended</h2>', '<h2>🤖 Claude, ChatGPT and other AI apps</h2>\\n\\n  <div class=\\"card\\">\\n    <div class=\\"card-title\\">Claude (web, desktop, mobile) <span class=\\"badge badge-now\\">Recommended</span></div>\\n    <div class=\\"card-body\\">\\n      Settings → Connectors → Add custom connector. Name: Legis-Link. URL:\\n      <div class=\\"code-block\\">https://legis-link-mcp-production-3e9b.up.railway.app/mcp</div>\\n      No sign-in needed (free tier, 50 questions a day). Pro users add their key to the end of the URL:\\n      <div class=\\"code-block\\">https://legis-link-mcp-production-3e9b.up.railway.app/mcp?key=ll_p_your_key</div>\\n    </div>\\n  </div>\\n\\n  <div class=\\"card\\">\\n    <div class=\\"card-title\\">Cursor, Windsurf, VS Code and other MCP clients</div>\\n    <div class=\\"card-body\\">\\n      Use the streamable HTTP address:\\n      <div class=\\"code-block\\">{ \\"mcpServers\\": { \\"legis-link\\": { \\"url\\": \\"https://legis-link-mcp-production-3e9b.up.railway.app/mcp\\" } } }</div>\\n      Older clients that only support SSE can still use https://legis-link-mcp-production-3e9b.up.railway.app/sse\\n    </div>\\n  </div>\\n\\n  <h2>📱 Mobile — Recommended</h2>', 1), ('VERSION           = "3.3.0"', 'VERSION           = "3.4.0"', 1)]
README_MCP = '## MCP Integration\n\nUse Legis-Link from Claude, ChatGPT, Cursor, Windsurf, VS Code or any MCP-compatible AI tool. No sign-in is needed for the free tier.\n\n**Server URL (streamable HTTP):** `https://legis-link-mcp-production-3e9b.up.railway.app/mcp`\n**Older clients (SSE only):** `https://legis-link-mcp-production-3e9b.up.railway.app/sse`\n\n### Claude (web, desktop and mobile)\nSettings → Connectors → **Add custom connector**. Name `Legis-Link`, URL:\n```\nhttps://legis-link-mcp-production-3e9b.up.railway.app/mcp\n```\nPro users add their key: `https://legis-link-mcp-production-3e9b.up.railway.app/mcp?key=ll_p_your_key`\n\n### Claude Desktop (config file)\n`claude_desktop_config.json` (Settings → Developer → Edit Config):\n```json\n{\n  "mcpServers": {\n    "legis-link": {\n      "command": "npx",\n      "args": ["-y", "mcp-remote", "https://legis-link-mcp-production-3e9b.up.railway.app/mcp"]\n    }\n  }\n}\n```\n\n### Cursor / Windsurf / VS Code\n```json\n{\n  "mcpServers": {\n    "legis-link": {\n      "url": "https://legis-link-mcp-production-3e9b.up.railway.app/mcp"\n    }\n  }\n}\n```\nPro key: send it as the `X-API-Key` header, as `Authorization: Bearer ll_p_...`, or add `?key=ll_p_...` to the URL.\n\n### Example queries via MCP\n```\n"What cable size for 45A, 30m run, 240V single phase, NSW?"\n"Generate a RAMS for electrical switchboard installation in NSW"\n"Is 6mm2 XLPE cable compliant for underground installation in NSW?"\n"What are the inspection hold points for a timber frame in NSW?"\n"Wire size for 60A circuit, 50 feet run, 240V, California?"\n```\n\n---\n\n'


def abort(msg):
    print("ABORTED - no changes written.")
    print(msg)
    sys.exit(1)


def main():
    if not os.path.exists(MAIN):
        abort("Not found: " + MAIN + "\nPut this script in the legis-link-mcp folder.")
    with open(MAIN, "r", encoding="utf-8-sig", newline="") as f:
        src = f.read()
    if MARK in src:
        abort("LL-2a is already applied.")
    if "LL-1: keys are signed and checked" not in src:
        abort("Please apply ll1_security_fix.py first.")
    nl = "\r\n" if "\r\n" in src else "\n"
    out = src.replace(nl, "\n")
    for old, new, n in SWAPS:
        if out.count(old) != n:
            abort("Text not found exactly " + str(n) + " time(s):\n  " + old[:110])
        out = out.replace(old, new)
    try:
        ast.parse(out)
    except SyntaxError as e:
        abort("Patched file failed Python syntax check: " + str(e))
    rd = None
    if os.path.exists(README):
        with open(README, "r", encoding="utf-8-sig", newline="") as f:
            rd = f.read()
        rnl = "\r\n" if "\r\n" in rd else "\n"
        r = rd.replace(rnl, "\n")
        i = r.find("## MCP Integration")
        j = r.find("## Self-Host", i)
        if i < 0 or j < 0:
            abort("README sections not found (MCP Integration / Self-Host).")
        rd = (r[:i] + README_MCP + r[j:]).replace("\n", rnl)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    for p in (MAIN, README):
        if os.path.exists(p):
            shutil.copy2(p, p + ".bak_" + stamp)
    with open(MAIN, "w", encoding="utf-8", newline="") as f:
        f.write(out.replace("\n", nl))
    if rd is not None:
        with open(README, "w", encoding="utf-8", newline="") as f:
            f.write(rd)
    print("OK - LL-2a applied (v3.4.0).")
    print("  done: streamable HTTP at /mcp (Claude connectors, Cursor, VS Code); /sse still works")
    print("  done: Pro key via ?key=, X-API-Key or Bearer on the connection")
    print("  done: connect page, server card and README updated")
    print("Backups saved with .bak_" + stamp)


if __name__ == "__main__":
    main()
