# Legis-Link MCP

Construction trade compliance MCP server. Covers electrical, plumbing, HVAC, welding, roofing, gas fitting, solar and more across Australia, UK, USA, Canada and EU.

## Tools

### Free (no signup needed)
- check_compliance - Compliance questions with exact code references
- get_code_reference - Look up specific standards and sections
- list_supported_regions - See coverage for a trade

### Pro (/year)
- calculate_technical_spec - Cable sizing, pipe sizing, voltage drop
- generate_safety_checklist - PPE and hazard controls with reg citations
- generate_rams - Full Risk Assessment and Method Statement
- erify_material_compliance - COMPLIANT/NON_COMPLIANT before ordering
- get_inspection_requirements - Who inspects, what docs, which regulation

## Installation

Add to Claude Desktop config:

`json
{
  "mcpServers": {
    "legis-link": {
      "command": "python",
      "args": ["legis_link_mcp_server.py"]
    }
  }
}
`

Or install via Smithery:
https://smithery.ai/server/ricky-farmerai/construction-legis-link-mcp

## Coverage

- Australia: AS/NZS 3000, AS/NZS 3008, NCC, state WHS Acts
- UK: BS 7671, CDM 2015, HSE guidance, Gas Safe
- USA: NEC NFPA 70, IBC, OSHA 29 CFR 1926
- Canada: CEC CSA C22.1, NBC
- EU: EN standards, local regulations

## License

MIT
