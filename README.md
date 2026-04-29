# seo-scraper

Competitor intelligence scraper for Yasmin Nabulsi Fine Jewelry. Collects Google SERP + Etsy search results, classifies competitor metal types via Claude Haiku, and exposes everything through an MCP server for querying in Claude.

**Does one thing:** scrape → classify → store → serve. No Shopify writes, no CSV exports, no title generation — that's the `seo-optimizer` skill.

---

## Architecture

```
Google SERP (Bright Data)          Etsy SERP (Bright Data)
        │                                   │
        ▼                                   ▼
 serp_crawler.py                 etsy_serp_crawler.py
 Google top-10 per query         Etsy top-48 per query
        │                                   │
        ▼                                   ▼
 serp_results table             etsy_serp_results table
        │                                   │
        ▼                                   ▼
  Playwright crawl              etsy_extractor.py
  (product pages)               (Bright Data, no browser)
        │                                   │
        ▼                                   ▼
 product_pages table            etsy_listings table
        │                                   │
        └──────────────┬────────────────────┘
                       ▼
              Claude Haiku classifier
              (metal_type, karat, confidence)
                       │
                       ▼
                  scraper.db (SQLite)
                       │
                       ▼
              MCP server (OAuth 2.0)
              scraper.ellacreationsjewelry.com
                       │
                       ▼
              Claude Desktop / Claude Code
```

---

## Running the Pipeline

### Google SERP (existing flow)
```bash
cd /root/seo-scraper
source .venv/bin/activate

# Full run
python pipeline.py --keywords data/keywords.json

# Skip expensive Playwright page crawl (Bright Data only)
python pipeline.py --keywords data/keywords.json --pages-limit 0

# Classify only (existing SERP data)
python pipeline.py --skip-serp --pages-limit 0 --classify-limit 50
```

### Etsy SERP (new flow)
```bash
# Standalone Etsy run
python pipeline.py --skip-serp --pages-limit 0 --skip-classify --skip-delta --etsy

# Full run (Google + Etsy)
python pipeline.py --keywords data/keywords.json --etsy

# Etsy with custom keyword file
python pipeline.py --skip-serp --pages-limit 0 --skip-classify --skip-delta \
  --etsy --etsy-keywords data/etsy_keywords.json --etsy-limit 50
```

### CLI Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--keywords` | `data/keywords.json` | Google keywords file |
| `--skip-serp` | false | Skip Google SERP crawl |
| `--pages-limit` | 0 | Max Playwright page crawls (0 = skip) |
| `--skip-classify` | false | Skip Haiku classification |
| `--skip-delta` | false | Skip delta detection |
| `--classify-limit` | 50 | Max pages to classify per run |
| `--etsy` | false | Enable Etsy SERP + listing crawl |
| `--etsy-keywords` | `data/etsy_keywords.json` | Etsy keywords file |
| `--etsy-limit` | 50 | Max Etsy listings to fetch per run |

---

## Keywords File Format

### `data/keywords.json` (Google)
```json
[
  {
    "product_handle": "18k-turquoise-bracelet",
    "product_title": "18k Gold Turquoise Beads Bracelet",
    "our_url": "https://www.ellacreationsjewelry.com/products/18k-turquoise-bracelet",
    "views_90d": 640,
    "queries": [
      "18k solid gold turquoise bracelet",
      "solid gold turquoise bracelet"
    ]
  }
]
```

### `data/etsy_keywords.json` (Etsy)
```json
[
  {
    "product_handle": "18k-turquoise-bracelet",
    "queries": [
      "18k gold turquoise bracelet",
      "solid gold turquoise bracelet"
    ]
  }
]
```

---

## Database Schema

### Google SERP tables

```sql
serp_results          -- Google top-10 per query (position, url, title)
product_pages         -- raw HTML from competitor product pages
classifications       -- metal_type, karat, price, evidence per URL
deltas                -- field-level changes between runs
our_products          -- our products being tracked
competitors           -- competitor domain registry
competitor_products   -- competitor product URLs
serp_snapshots        -- weekly SERP position snapshots per query
serp_snapshot_results -- individual positions within each snapshot
product_snapshots     -- price + metal history per competitor product
```

### Etsy tables (new)

```sql
-- Etsy search results per query
etsy_serp_results (
    query, position, listing_id, url, title,
    price_usd, shop, reviews, star_seller
)

-- Etsy listing detail + classification
etsy_listings (
    listing_id, url, title, price_usd, shop,
    materials,   -- JSON array
    tags,        -- JSON array
    reviews, favorites,
    metal_type, karat, confidence  -- from Haiku classifier
)
```

---

## How Etsy Scraping Works

### Step 1 — SERP (`crawlers/etsy_serp_crawler.py`)

Fetches `https://www.etsy.com/search?q={query}&explicit=1` via Bright Data Web Unlocker API (same REST endpoint as Google scraping — no proxy port, no browser).

Parse strategy (priority order):
1. JSON-LD `@type: ItemList` → `ListItem` entries (Etsy embeds this for SEO)
2. Regex on `data-listing-id` attributes + adjacent price/title elements

Extracts: listing_id, url, title, price_usd, star_seller flag. Up to 48 results per query.

### Step 2 — Listing extraction (`extraction/etsy_extractor.py`)

Fetches each individual listing URL via Bright Data. Extracts metadata:
1. JSON-LD `@type: Product` → name, description, offers.price, brand (shop)
2. Inline JS JSON → `"materials":[...]`, `"tags":[...]`, `"num_favorers"`, `"num_ratings"`
3. Fallback regex for price and title

Feeds `title + description + materials + tags` into the existing Claude Haiku classifier (`_classify_text` from `classifier.py`). Saves metal_type, karat, confidence to `etsy_listings`.

### Parsing robustness

Etsy regularly changes its HTML structure. The extractor uses layered fallbacks:
- JSON-LD first (most stable — Etsy generates this for Google indexing)
- Inline JS JSON second (semi-stable key names)
- Regex fallback last

If parsing fails, listing is saved with `metal_type = unknown`, `confidence = low`.

---

## MCP Server

Live at `https://scraper.ellacreationsjewelry.com/mcp`
Auth: OAuth 2.0 with PKCE (auto-approves, single-user)

### Google tools

| Tool | Use |
|------|-----|
| `scraper_get_stats` | DB overview: row counts, last run, keywords, metal breakdown |
| `scraper_get_competitor_intel` | Ranked competitors by keyword — metal type, karat, price, evidence |
| `scraper_get_metal_breakdown` | % solid gold vs plated vs vermeil across keywords |
| `scraper_list_serp_results` | Raw Google results for a specific query |
| `scraper_get_product_classification` | Full classification for a competitor URL |
| `scraper_get_serp_history` | Historical SERP positions for a keyword over time |
| `scraper_get_competitor_products` | All tracked URLs for a competitor domain |
| `scraper_get_price_history` | Price history for a specific competitor product |
| `scraper_get_delta_changes` | Week-over-week metal/price changes |
| `scraper_run_pipeline` | Trigger a fresh scrape (non-blocking background job) |

### Etsy tools

| Tool | Use |
|------|-----|
| `scraper_list_etsy_serp` | Ranked Etsy listings for a query — price, shop, reviews, star_seller |
| `scraper_get_etsy_competitor_intel` | Full competitor view — SERP rank + metal classification + materials/tags |

---

## File Structure

```
seo-scraper/
├── crawlers/
│   ├── serp_crawler.py          Google SERP via Bright Data REST API
│   └── etsy_serp_crawler.py     Etsy SERP via Bright Data REST API
│
├── extraction/
│   ├── classifier.py            Bright Data page fetch + Claude Haiku classification
│   └── etsy_extractor.py        Etsy listing fetch + metadata extraction + classification
│
├── monitoring/
│   └── delta.py                 Week-over-week change detection
│
├── data/
│   ├── keywords.json            Google search queries (gitignored)
│   ├── etsy_keywords.json       Etsy search queries (gitignored)
│   └── scraper.db               SQLite database (gitignored)
│
├── db.py                        Schema init + connection helper
├── pipeline.py                  Main entrypoint — orchestrates all steps
├── mcp_server.py                FastMCP server with OAuth 2.0
├── README.md                    This file
├── MCP_CONNECT.md               MCP connection + OAuth setup guide
└── PLAN.md                      Original architecture design doc
```

---

## Infrastructure

| Item | Detail |
|------|--------|
| **Server** | Hetzner CX22, `65.109.136.20` |
| **DNS** | Cloudflare A record: `scraper` → `65.109.136.20` (proxied, free SSL) |
| **Service** | `systemctl status seo-scraper-mcp` |
| **Cron** | Every Monday 3am UTC — full pipeline run |
| **DB location** | `/root/seo-scraper/data/scraper.db` |
| **Repo** | `github.com/imalexsq/seo-scraper` |

### SSH management
```bash
ssh root@65.109.136.20

# Service
systemctl status seo-scraper-mcp
systemctl restart seo-scraper-mcp
journalctl -u seo-scraper-mcp -f

# Manual pipeline run
cd /root/seo-scraper && source .venv/bin/activate
python pipeline.py --skip-serp --pages-limit 0 --skip-classify --skip-delta --etsy

# Check DB
sqlite3 data/scraper.db "SELECT metal_type, COUNT(*) FROM etsy_listings GROUP BY metal_type;"
```

---

## Environment Variables (on Hetzner, `/root/seo-scraper/.env`)

```bash
BRIGHTDATA_API_KEY=...    # Bright Data Web Unlocker bearer token
BRIGHTDATA_ZONE=...       # Zone name (default: web_unlocker1)
ANTHROPIC_API_KEY=...     # Claude API key (Haiku classification)
MCP_PORT=80
MCP_PUBLIC_URL=https://scraper.ellacreationsjewelry.com
```
