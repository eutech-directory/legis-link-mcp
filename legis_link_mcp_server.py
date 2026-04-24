"""
Legis-Link MCP Server v3.0 — Claude-direct, HTTP + stdio dual mode
===================================================================
v3 replaces the broken Legis-Link backend with direct Claude API calls.
All 8 tools are fully functional with no external dependencies.

FREE TIER (3 tools):
  check_compliance          — compliance questions with code references
  get_code_reference        — look up specific standards/sections
  list_supported_regions    — see what's covered for a trade

PRO TIER — $199/year (5 tools):
  calculate_technical_spec  — cable sizing, pipe sizing, HVAC loads, voltage drop
  generate_safety_checklist — PPE + hazard checklist with regulatory citations
  generate_rams             — full Risk Assessment & Method Statement
  verify_material_compliance — COMPLIANT/NON_COMPLIANT/REQUIRES_VERIFICATION
  get_inspection_requirements — who inspects, what docs, which regulation

Run locally (stdio):   python legis_link_mcp_server.py
Deploy to Railway:     automatic HTTP mode via PORT env var

Install: pip install mcp httpx uvicorn starlette
"""

import asyncio
import json
import os
import re
import sys
import httpx
from typing import Any

try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp import types
except ImportError:
    print("Install MCP SDK: pip install mcp", file=sys.stderr)
    sys.exit(1)

# ── Config ─────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_URL  = "https://api.anthropic.com/v1/messages"
MODEL          = "claude-haiku-4-5-20251001"
PORT           = int(os.environ.get("PORT", 8000))
PRO_UPGRADE    = "https://legis-link-mcp-production-3e9b.up.railway.app/app?upgrade=pro"

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

VALID_ROLES    = ["Apprentice", "Journeyman", "Foreman", "PM / Executive"]
FREE_TOOLS     = {"check_compliance", "get_code_reference", "list_supported_regions"}
PRO_TOOLS      = {
    "calculate_technical_spec", "generate_safety_checklist",
    "generate_rams", "verify_material_compliance", "get_inspection_requirements"
}

# ── System prompts per tool type ───────────────────────────────────────────
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
    "serverInfo": {"name": "Legis-Link", "version": "3.0.0"},
    "authentication": {"required": False},
    "tools": [
        {"name": "check_compliance",
         "description": "Answer construction trade compliance questions with code references. Covers electrical, plumbing, HVAC, welding, carpentry, fire protection, concrete, roofing, gas fitting, solar/battery across AU, US, CA, UK, EU.",
         "inputSchema": {"type": "object", "properties": {
             "trade": {"type": "string", "enum": VALID_TRADES},
             "region": {"type": "string"},
             "question": {"type": "string"},
             "role": {"type": "string", "enum": VALID_ROLES, "default": "Journeyman"}
         }, "required": ["trade", "region", "question"]}},
        {"name": "get_code_reference",
         "description": "Look up specific trade code sections and standards.",
         "inputSchema": {"type": "object", "properties": {
             "trade": {"type": "string", "enum": VALID_TRADES},
             "region": {"type": "string"},
             "topic": {"type": "string"}
         }, "required": ["trade", "region", "topic"]}},
        {"name": "list_supported_regions",
         "description": "List all supported regions for a given trade.",
         "inputSchema": {"type": "object", "properties": {
             "trade": {"type": "string", "enum": VALID_TRADES}
         }, "required": ["trade"]}},
        {"name": "calculate_technical_spec",
         "description": "[PRO] Calculate cable sizing, pipe sizing, HVAC loads, voltage drop. Returns result + code reference. Replaces Elec-Mate, Plumbing Formulator.",
         "inputSchema": {"type": "object", "properties": {
             "trade": {"type": "string", "enum": VALID_TRADES},
             "region": {"type": "string"},
             "calculation": {"type": "string", "description": "E.g. 'cable size for 20A circuit, 25m run, clipped direct'"},
             "role": {"type": "string", "enum": VALID_ROLES, "default": "Journeyman"}
         }, "required": ["trade", "region", "calculation"]}},
        {"name": "generate_safety_checklist",
         "description": "[PRO] Generate trade-specific safety checklist with PPE, hazard controls, permits, and regulatory citations.",
         "inputSchema": {"type": "object", "properties": {
             "trade": {"type": "string", "enum": VALID_TRADES},
             "region": {"type": "string"},
             "task": {"type": "string", "description": "E.g. 'working at height on roof', 'live electrical testing'"},
             "role": {"type": "string", "enum": VALID_ROLES, "default": "Journeyman"}
         }, "required": ["trade", "region", "task"]}},
        {"name": "generate_rams",
         "description": "[PRO] Generate a Risk Assessment and Method Statement (RAMS/JHA). Reduces 30-60 min manual writing to seconds.",
         "inputSchema": {"type": "object", "properties": {
             "trade": {"type": "string", "enum": VALID_TRADES},
             "region": {"type": "string"},
             "task": {"type": "string", "description": "E.g. 'install 3-phase distribution board in commercial building'"},
             "company_name": {"type": "string"},
             "site_address": {"type": "string"},
             "role": {"type": "string", "enum": VALID_ROLES, "default": "Foreman"}
         }, "required": ["trade", "region", "task"]}},
        {"name": "verify_material_compliance",
         "description": "[PRO] Check material spec against local code. Returns COMPLIANT/NON_COMPLIANT/REQUIRES_VERIFICATION before ordering.",
         "inputSchema": {"type": "object", "properties": {
             "trade": {"type": "string", "enum": VALID_TRADES},
             "region": {"type": "string"},
             "material": {"type": "string", "description": "E.g. '2.5mm2 TPS copper cable for 20A final sub-circuit'"},
             "use_case": {"type": "string", "description": "E.g. 'clipped to wall in residential building'"},
             "role": {"type": "string", "enum": VALID_ROLES, "default": "Journeyman"}
         }, "required": ["trade", "region", "material"]}},
        {"name": "get_inspection_requirements",
         "description": "[PRO] Get mandatory inspection and certification requirements. Covers EICR, EIC, gas safety certs, solar approvals by region.",
         "inputSchema": {"type": "object", "properties": {
             "trade": {"type": "string", "enum": VALID_TRADES},
             "region": {"type": "string"},
             "installation": {"type": "string", "description": "E.g. 'new consumer unit replacement', 'solar PV 6.6kW with battery'"},
             "role": {"type": "string", "enum": VALID_ROLES, "default": "Journeyman"}
         }, "required": ["trade", "region", "installation"]}},
    ],
    "resources": [],
    "prompts": []
}

server = Server("legis-link")


# ── Claude API call ────────────────────────────────────────────────────────

async def ask_claude(system_prompt: str, user_message: str) -> dict:
    """Call Claude API and parse JSON response."""
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
                return {"status": "ERROR", "result": f"API error {resp.status_code}",
                        "code_reference": ""}

            data     = resp.json()
            raw_text = data["content"][0]["text"].strip()

            # Strip markdown code fences if present
            raw_text = re.sub(r'^```(?:json)?\s*', '', raw_text)
            raw_text = re.sub(r'\s*```$', '', raw_text)

            try:
                return json.loads(raw_text)
            except json.JSONDecodeError:
                # Extract JSON from response if mixed with text
                match = re.search(r'\{.*\}', raw_text, re.DOTALL)
                if match:
                    return json.loads(match.group())
                return {"status": "INFO", "result": raw_text, "code_reference": ""}

        except httpx.TimeoutException:
            return {"status": "ERROR",
                    "result": "Request timed out. Please try again.",
                    "code_reference": ""}
        except Exception as e:
            return {"status": "ERROR", "result": f"Error: {e}", "code_reference": ""}


def format_response(result: dict, header: str, footer_link: str) -> str:
    """Format Claude response into clean MCP output."""
    status   = result.get("status", "")
    answer   = result.get("result", "")
    code_ref = result.get("code_reference", "")

    text = f"**{header}**\n\n{answer}"
    if code_ref:
        text += f"\n\n*Code reference: {code_ref}*"
    if status in ("NON_COMPLIANT",):
        text += f"\n\n⚠️ **Non-compliant** — see answer above for the correct alternative."
    elif status == "REQUIRES_VERIFICATION":
        text += f"\n\n⚠️ **Requires verification** — confirm with local authority before proceeding."
    text += f"\n\n[{footer_link}]"
    return text


# ── Tool definitions ───────────────────────────────────────────────────────

@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [types.Tool(
        name=t["name"],
        description=t["description"],
        inputSchema=t["inputSchema"]
    ) for t in SERVER_CARD["tools"]]


@server.call_tool()
async def call_tool(name: str,
                    arguments: dict[str, Any]) -> list[types.TextContent]:

    trade  = arguments.get("trade", "")
    region = arguments.get("region", "")
    role   = arguments.get("role", "Journeyman")

    # ── FREE TOOLS ─────────────────────────────────────────────────────────

    if name == "check_compliance":
        question = arguments.get("question", "")
        user_msg = f"Trade: {trade} | Region: {region} | Role: {role}\nQuestion: {question}"
        result   = await ask_claude(SYSTEM_PROMPTS["compliance"], user_msg)
        text     = format_response(result,
                    f"{trade} Compliance — {region}",
                    f"Legis-Link full tool: {PRO_UPGRADE.split('?')[0]}")
        return [types.TextContent(type="text", text=text)]

    if name == "get_code_reference":
        topic    = arguments.get("topic", "")
        user_msg = f"Trade: {trade} | Region: {region}\nLook up the specific code reference and section number for: {topic}"
        result   = await ask_claude(SYSTEM_PROMPTS["compliance"], user_msg)
        text     = format_response(result,
                    f"Code Reference: {topic} — {trade} / {region}",
                    f"Legis-Link full tool: {PRO_UPGRADE.split('?')[0]}")
        return [types.TextContent(type="text", text=text)]

    if name == "list_supported_regions":
        lines = [f"**Supported regions for {trade}:**\n"]
        for country, regions in VALID_REGIONS.items():
            lines.append(f"**{country}:** {', '.join(regions)}")
        lines.append(f"\nUpgrade to Pro for calculations, RAMS, and safety checklists: {PRO_UPGRADE}")
        return [types.TextContent(type="text", text="\n".join(lines))]

    # ── PRO TOOLS ──────────────────────────────────────────────────────────

    if name == "calculate_technical_spec":
        calculation = arguments.get("calculation", "")
        user_msg = (
            f"Trade: {trade} | Region: {region} | Role: {role}\n"
            f"Calculate: {calculation}\n"
            f"Show the numerical result, formula used, and exact code reference."
        )
        result = await ask_claude(SYSTEM_PROMPTS["calculation"], user_msg)
        text   = format_response(result,
                    f"Technical Calculation — {trade} / {region}",
                    f"Full calculations + audit log: {PRO_UPGRADE}")
        return [types.TextContent(type="text", text=text)]

    if name == "generate_safety_checklist":
        task     = arguments.get("task", "")
        user_msg = (
            f"Trade: {trade} | Region: {region} | Role: {role}\n"
            f"Generate a safety checklist for: {task}"
        )
        result = await ask_claude(SYSTEM_PROMPTS["safety"], user_msg)
        text   = format_response(result,
                    f"Safety Checklist — {task} | {trade} / {region}",
                    f"Generate PDF checklist: {PRO_UPGRADE}")
        return [types.TextContent(type="text", text=text)]

    if name == "generate_rams":
        task         = arguments.get("task", "")
        company_name = arguments.get("company_name", "")
        site_address = arguments.get("site_address", "")
        header_info  = ""
        if company_name:
            header_info += f"Company: {company_name}. "
        if site_address:
            header_info += f"Site: {site_address}. "
        user_msg = (
            f"Trade: {trade} | Region: {region} | Role: {role}\n"
            f"{header_info}"
            f"Generate a RAMS for: {task}"
        )
        result = await ask_claude(SYSTEM_PROMPTS["rams"], user_msg)
        doc_title = f"RAMS — {task} | {trade} / {region}"
        if company_name:
            doc_title += f" | {company_name}"
        text = format_response(result, doc_title,
                    f"Download as PDF: {PRO_UPGRADE}")
        return [types.TextContent(type="text", text=text)]

    if name == "verify_material_compliance":
        material = arguments.get("material", "")
        use_case = arguments.get("use_case", "standard installation")
        user_msg = (
            f"Trade: {trade} | Region: {region} | Role: {role}\n"
            f"Material: {material}\n"
            f"Use case: {use_case}\n"
            f"Is this material compliant with local code?"
        )
        result = await ask_claude(SYSTEM_PROMPTS["material"], user_msg)
        text   = format_response(result,
                    f"Material Compliance — {material} | {trade} / {region}",
                    f"Full compliance audit: {PRO_UPGRADE}")
        return [types.TextContent(type="text", text=text)]

    if name == "get_inspection_requirements":
        installation = arguments.get("installation", "")
        user_msg = (
            f"Trade: {trade} | Region: {region} | Role: {role}\n"
            f"What are the mandatory inspection and certification requirements for: {installation}?"
        )
        result = await ask_claude(SYSTEM_PROMPTS["inspection"], user_msg)
        text   = format_response(result,
                    f"Inspection Requirements — {installation} | {trade} / {region}",
                    f"Track inspections + certificates: {PRO_UPGRADE}")
        return [types.TextContent(type="text", text=text)]

    return [types.TextContent(type="text", text=f"Unknown tool: {name}")]


# ── HTTP server (Railway) ──────────────────────────────────────────────────

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

        async def handle_server_card(request):
            return JSONResponse(SERVER_CARD)

        async def handle_health(request):
            key = ANTHROPIC_API_KEY
            return JSONResponse({
                "status": "ok", "service": "legis-link-mcp",
                "version": "3.0.0", "engine": "claude-direct",
                "tools": {"free": 3, "pro": 5, "total": 8},
                "api_key_set": bool(key),
                "api_key_prefix": key[:12] + "..." if len(key) > 12 else "MISSING"
            })

        starlette_app = Starlette(routes=[
            Route("/.well-known/mcp/server-card.json", handle_server_card),
            Route("/health", handle_health),
            Mount("/sse", app=sse.handle_post_message),
            Mount("/", routes=[Route("/sse", endpoint=handle_sse)]),
        ])

        print(f"[Legis-Link MCP v3.0] HTTP port {PORT} — Claude-direct, 8 tools",
              file=sys.stderr)
        uvicorn.run(starlette_app, host="0.0.0.0", port=PORT)

    except ImportError as e:
        print(f"HTTP deps missing: {e}", file=sys.stderr)
        sys.exit(1)


async def run_stdio():
    print("[Legis-Link MCP v3.0] stdio — Claude-direct, 8 tools", file=sys.stderr)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream,
                         server.create_initialization_options())


if __name__ == "__main__":
    if os.environ.get("PORT"):
        run_http()
    else:
        asyncio.run(run_stdio())
