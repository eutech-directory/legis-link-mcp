<p align="center">
  <img src="https://legis-link.com/icon-192.png" width="96" height="96" alt="Legis-Link"/>
</p>

<h1 align="center">Legis-Link</h1>

<p align="center"><b>Construction compliance answers for the trades, inside Claude, Cursor, VS Code or any phone.</b><br/>
Australia · United Kingdom · United States · Canada · European Union</p>

<p align="center">
  <a href="https://legis-link.com/app">Try it free</a> ·
  <a href="#connect-to-claude-or-any-mcp-client">Connect via MCP</a> ·
  <a href="https://rickyfarmer.gumroad.com/l/Legis-LinkPro">Get Pro</a>
</p>

<!-- mcp-name: com.legis-link/legis-link -->

---

Ask a compliance question in plain English, such as *"Is 2.5 mm² TPS OK on a 20 A circuit in a wall cavity in NSW?"*.
Legis-Link gives you a verdict (**Compliant / Non-compliant / Needs checking**), the standard and clause it rests on,
and the conditions that would change the answer.

- **Answers the normal case first.** It doesn't mark something non-compliant because of an unstated worst case.
- **Cites the edition in force:** AS/NZS 3000:2018, BS 7671:2018+A4:2026, AS/NZS 3500:2025, AS/NZS 5601.1:2022.
  For the NEC and US plumbing codes it names the edition each state has adopted instead of guessing.
- **Checked licensing data:** who may legally do the work, the licence class, the apprentice route and the
  contractor licence, for **110 trade-and-region combinations** across 22 jurisdictions.
- **Tested:** a 21-question known-answer set (electrical, plumbing, gas; AU, UK, US) runs against the live server
  after every change. Current score: **21 / 21**.

## Tools

All tools are **read-only**: they look things up and write text. They never change, send or delete anything.

| Tool | Tier | What it does |
|------|------|--------------|
| **Check compliance** | Free | Verdict + standard, clause and conditions |
| **Look up a code or standard** | Free | The clause or table that applies, in plain English |
| **List supported regions** | Free | Jurisdictions covered for a trade |
| **Calculate a technical spec** | Pro | Cable size, voltage drop, pipe size, heat load |
| **Generate a safety checklist** | Pro | PPE and hazard controls, each tied to its regulation |
| **Generate RAMS** | Pro | Risk assessment and method statement in about a minute |
| **Verify material compliance** | Pro | Will this product pass, before you order it |
| **Get inspection requirements** | Pro | Hold points, who signs off, which certificate |

The [web app](https://legis-link.com/app) also has a daily toolbox talk per trade (a PDF for Pro users) and a photo check.

## Coverage

| Area | Regions | Checked licensing data |
|------|---------|------------------------|
| Australia | all 8 states and territories | all 8 |
| United Kingdom | England, Scotland, Wales, Northern Ireland | all 4 |
| United States | all 50 states + DC | California, Texas, Florida, New York |
| Canada | 10 provinces | Ontario, British Columbia, Alberta |
| European Union | 14 countries | Ireland, Germany, France |

**Trades:** Electrical, Plumbing, Gas fitting, HVAC/refrigeration, Carpentry, plus Welding, Solar/Battery,
Fire protection, Concrete and Roofing.
Regions without checked data still get an answer, marked to be checked against the local code.

## Connect to Claude or any MCP client

**Server URL (streamable HTTP):** `https://legis-link.com/mcp`. No sign-in is needed for the free tier.

**Claude (web, desktop, mobile):** Settings → Connectors → **Add custom connector**. Name: `Legis-Link`. URL: `https://legis-link.com/mcp`.
Pro: `https://legis-link.com/mcp?key=YOUR_PRO_KEY`

**Cursor / Windsurf / VS Code**
```json
{
  "mcpServers": {
    "legis-link": {
      "url": "https://legis-link.com/mcp",
      "headers": { "X-API-Key": "YOUR_PRO_KEY (optional)" }
    }
  }
}
```

**Claude Desktop (config file)**
```json
{
  "mcpServers": {
    "legis-link": { "command": "npx", "args": ["-y", "mcp-remote", "https://legis-link.com/mcp"] }
  }
}
```

A Pro key (the license key from your Gumroad receipt) can be sent as the `X-API-Key` header, as `Authorization: Bearer <key>`, or as `?key=<key>` on the URL.
Older SSE-only clients can use `https://legis-link.com/sse`.

### Try asking
```
Is 2.5mm2 twin and earth OK for a 32A ring final circuit in England?
Can an unregistered handyman replace the gas hob in a rented flat in England?
What is the maximum hot water temperature at a new bathroom basin in NSW?
Who can legally do gas work in Ontario?
Generate a RAMS for a switchboard upgrade in a house in Victoria
```

## Pricing

| | Free | Pro |
|---|---|---|
| Price | $0 | $199 / year |
| Questions per day | 50 | 1,000 |
| Tools | 3 | 8 |
| Toolbox talk PDFs | | ✓ |

**Get Pro:** https://rickyfarmer.gumroad.com/l/Legis-LinkPro. Your Pro key is the license key on your Gumroad receipt, and it works straight away.

## API

```
GET  https://legis-link.com/health
POST https://legis-link.com/api/query    {"trade": "Electrical", "region": "NSW", "question": "...", "api_key": ""}
GET  https://legis-link.com/toolbox?trade=Electrical&region=NSW
GET  https://legis-link.com/.well-known/mcp/server-card.json
```

## Self-host

Python 3.11+ and an Anthropic API key.
```bash
git clone https://github.com/eutech-directory/legis-link-mcp.git
cd legis-link-mcp
pip install -r requirements.txt
ANTHROPIC_API_KEY=... python legis_link_mcp_server.py
```
Optional: `DATABASE_URL` (PostgreSQL) for usage limits and keys; `LEGIS_KEY_SECRET` to issue Pro keys.

## Links

- **App:** https://legis-link.com/app
- **Official MCP Registry:** `com.legis-link/legis-link`
- **Glama:** https://glama.ai/mcp/servers/eutech-directory/legis-link-mcp

---

*Legis-Link gives preliminary compliance references to help qualified people work faster. Always confirm against
the published standard and your local authority before installing, and use a licensed person where the law
requires one.*
