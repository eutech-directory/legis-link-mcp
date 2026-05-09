# Legis-Link MCP — Construction Compliance AI

[![npm version](https://img.shields.io/npm/v/legis-link-mcp.svg)](https://www.npmjs.com/package/legis-link-mcp)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![Glama Score](https://glama.ai/mcp/servers/eutech-directory/legis-link-mcp/badges/score.svg)](https://glama.ai/mcp/servers/eutech-directory/legis-link-mcp)

**Instant construction compliance answers on mobile. Free. No install. Any phone.**

Ask any compliance question in plain English. Get the exact standard, clause, and table reference back in seconds.

## Live Demo

**Try it now:** https://legis-link-mcp-production-3e9b.up.railway.app/app

No account required. 50 free queries/day.

---

## 9 Tools

### Free Tier (50 queries/day, no account)
| Tool | What it does |
|------|-------------|
| **Compliance check** | COMPLIANT / NON-COMPLIANT verdict with exact clause |
| **Code reference** | Look up any standard, clause, or table |
| **Toolbox talks** | Daily trade-specific safety briefing (10 trades, 70 topics) |

### Pro Tier ($199/year — 1,000 queries/day)
| Tool | What it does |
|------|-------------|
| **Technical calculations** | Cable sizing, pipe sizing, voltage drop, heat load |
| **Safety checklist** | PPE + hazard controls with regulation per item |
| **RAMS generator** | Full Risk Assessment + Method Statement in 60 seconds |
| **Material compliance** | COMPLIANT / NON-COMPLIANT before ordering |
| **Inspection requirements** | Hold points, sign-off authority, form numbers |
| **Visual compliance** | Upload a site photo — AI compliance assessment (unique in market) |

---

## Coverage — 87 Regions, 10 Trades

| Region | Coverage | Standards |
|--------|----------|-----------|
| Australia | 8 states + territories | AS/NZS 3000:2018, NCC 2022, WHS Acts |
| United States | All 50 states + DC | NEC NFPA 70, IBC, OSHA 29 CFR 1926 |
| Canada | 10 provinces | CEC CSA C22.1, NBC, NPC |
| United Kingdom | England, Scotland, Wales, N.Ireland | BS 7671:2018, CDM 2015, Gas Safe |
| European Union | 14 countries | EN standards, IEC 60364 |

**10 Trades:** Electrical, Plumbing, HVAC, Gas fitting, Welding, Solar/Battery, Fire protection, Carpentry, Concrete, Roofing

---

## MCP Integration

Use Legis-Link from Claude Desktop, Cursor, Windsurf, or any MCP-compatible AI tool.

### Claude Desktop
Add to `claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "legis-link": {
      "url": "https://legis-link-mcp-production-3e9b.up.railway.app/sse"
    }
  }
}
```

### Cursor / Windsurf
```json
{
  "mcpServers": {
    "legis-link": {
      "url": "https://legis-link-mcp-production-3e9b.up.railway.app/sse"
    }
  }
}
```

### Example queries via MCP
```
"What cable size for 45A, 30m run, 240V single phase, NSW?"
"Generate a RAMS for electrical switchboard installation in NSW"
"Is 6mm2 XLPE cable compliant for underground installation in NSW?"
"What are the inspection hold points for a timber frame in NSW?"
"Wire size for 60A circuit, 50 feet run, 240V, California?"
```

---

## Self-Host

### Requirements
- Python 3.11+
- Anthropic API key

### Install
```bash
git clone https://github.com/eutech-directory/legis-link-mcp.git
cd legis-link-mcp
pip install -r requirements.txt
```

### Configure
```bash
cp legis_link.env.example legis_link.env
# Edit legis_link.env with your API keys
```

### Run
```bash
python legis_link_mcp_server.py
```

Server runs on `http://localhost:8080`

---

## API

### Health check
```
GET /health
```

### Compliance query
```
POST /api/query
{
  "trade": "Electrical",
  "region": "NSW",
  "question": "Wire size for 45A load, 30m run?",
  "api_key": ""
}
```

### Daily toolbox talk
```
GET /toolbox?trade=Electrical
```

### Toolbox PDF (Pro)
```
GET /toolbox/pdf?trade=Electrical&region=NSW&api_key=ll_p_xxx
```

---

## Pricing

| Tier | Price | Queries/day | Tools |
|------|-------|-------------|-------|
| Free | $0 | 50 | 3 |
| Pro | $199/year | 1,000 | 9 |

**Get Pro:** https://rickyfarmer.gumroad.com/l/Legis-LinkPro

---

## Links

- **Live app:** https://legis-link-mcp-production-3e9b.up.railway.app/app
- **GitHub:** https://github.com/eutech-directory/legis-link-mcp
- **Smithery:** https://smithery.ai/server/ricky-farmerai/construction-legis-link-mcp
- **Glama:** https://glama.ai/mcp/servers/eutech-directory/legis-link-mcp

---

*Results are preliminary compliance references. Always verify against the full published standard before proceeding with any installation.*
