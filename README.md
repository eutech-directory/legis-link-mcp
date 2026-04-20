# Legis-Link MCP Server

Construction trade compliance AI for Claude, Cursor, and any MCP-compatible client.

## What it does

Answers trade compliance questions with exact code references across 10 trades and 20+ regions.

**Trades:** Electrical · Plumbing · HVAC · Welding · Carpentry · Fire protection · Concrete · Roofing · Gas fitting · Solar/Battery

**Regions:** Australia (NSW, VIC, QLD, WA, SA, ACT) · USA (TX, CA, FL, NY, IL) · Canada (ON, BC, AB, QC) · UK (England, Scotland, Wales, NI) · EU (Germany, France, Netherlands, Ireland, Spain, Italy)

## Tools

### `check_compliance`
Answer any trade compliance question with code-cited response.
```json
{
  "trade": "Electrical",
  "region": "NSW",
  "question": "minimum wire gauge for 20A circuit",
  "role": "Journeyman"
}
```
Returns: answer + exact code reference (e.g. AS/NZS 3008.1.2)

### `get_code_reference`
Look up specific code sections and standards.
```json
{
  "trade": "Electrical",
  "region": "NSW", 
  "topic": "wire sizing"
}
```

### `list_supported_regions`
List all supported regions for a given trade.
```json
{
  "trade": "Plumbing"
}
```

## Install

### Claude Desktop
Add to `~/.claude/claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "legis-link": {
      "command": "python",
      "args": ["/path/to/legis_link_mcp_server.py"]
    }
  }
}
```

### Requirements
```bash
pip install mcp httpx
```

## Live demo
[legis-link-mcp-production.up.railway.app/app](https://legis-link-mcp-production.up.railway.app/app)

## Free + Pro
- Free tier: unlimited compliance questions
- Pro ($199/year): audit logs, compliance certificates, team dashboard
