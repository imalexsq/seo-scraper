# SEO Scraper MCP — Connection Guide

Server: `https://scraper.ellacreationsjewelry.com/mcp`
Auth:   OAuth 2.0 with PKCE (Claude Desktop runs the flow automatically)

---

## ONE-TIME SETUP: Cloudflare DNS

In the Cloudflare dashboard for `ellacreationsjewelry.com`:

1. DNS → Add record
2. Type: **A**
3. Name: **scraper**
4. IPv4: **65.109.136.20**
5. Proxy status: **Proxied** (orange cloud) ← essential for HTTPS

This routes `https://scraper.ellacreationsjewelry.com` → Hetzner port 80 with free SSL.

---

## Connect via Cowork / Claude Desktop

Add to MCP settings (no API key needed — OAuth runs automatically):

```json
{
  "seo_scraper": {
    "type": "url",
    "url": "https://scraper.ellacreationsjewelry.com/mcp"
  }
}
```

On first connect, Claude Desktop will open a browser window for the OAuth flow.
The flow **auto-approves** — the browser redirects immediately with no login page.
You'll only see this once; the token is stored and reused.

---

## Connect via Claude Code (`~/.claude/claude_code_config.json`)

```json
{
  "mcpServers": {
    "seo_scraper": {
      "url": "https://scraper.ellacreationsjewelry.com/mcp"
    }
  }
}
```

---

## OAuth endpoints (auto-discovered by clients)

| Endpoint | URL |
|----------|-----|
| Discovery | `https://scraper.ellacreationsjewelry.com/.well-known/oauth-authorization-server` |
| Authorize | `https://scraper.ellacreationsjewelry.com/authorize` |
| Token     | `https://scraper.ellacreationsjewelry.com/token` |
| Register  | `https://scraper.ellacreationsjewelry.com/register` |
| Revoke    | `https://scraper.ellacreationsjewelry.com/revoke` |

---

## Available Tools

### Google / General

| Tool | Description |
|------|-------------|
| `scraper_get_stats` | DB overview: row counts, last run, keywords covered, metal breakdown |
| `scraper_get_competitor_intel` | Competitor list by keyword: metal type, karat, price, evidence |
| `scraper_get_metal_breakdown` | % solid gold vs plated vs vermeil in SERP for given keywords |
| `scraper_list_serp_results` | Raw Google results for a specific query |
| `scraper_get_product_classification` | Full classification for a specific competitor URL |
| `scraper_get_serp_history` | Historical SERP positions for a keyword over time |
| `scraper_get_competitor_products` | All tracked URLs for a competitor domain |
| `scraper_get_price_history` | Price history for a specific competitor product |
| `scraper_get_delta_changes` | Week-over-week price/metal_type changes detected |
| `scraper_run_pipeline` | Trigger a fresh scrape run (non-blocking, runs in background) |

### Etsy

| Tool | Description |
|------|-------------|
| `scraper_list_etsy_serp` | Ranked Etsy listings for a query — price, shop, reviews, star_seller, metal classification |
| `scraper_get_etsy_competitor_intel` | Full Etsy competitor view — rank + shop + materials/tags + metal type + karat |

---

## Systemd management (SSH to server)

```bash
ssh root@65.109.136.20
systemctl status seo-scraper-mcp    # check status
systemctl restart seo-scraper-mcp   # restart
journalctl -u seo-scraper-mcp -f    # live logs
```

## Environment (on Hetzner, in /root/seo-scraper/.env)

```
MCP_PORT=80                                              # Cloudflare proxies 443→80
MCP_PUBLIC_URL=https://scraper.ellacreationsjewelry.com  # issued in OAuth metadata
MCP_OAUTH_DB=/root/seo-scraper/data/oauth.db             # token storage (optional, has default)
```

## Legacy Bearer token (Cowork / Claude Code only — NOT Claude Desktop)

If you need the old unauthenticated URL for testing from curl/Cowork directly:

```
http://65.109.136.20:80/.well-known/oauth-authorization-server
```

Note: the HTTP IP:port URL is no longer suitable for Claude Desktop (requires HTTPS + OAuth).
