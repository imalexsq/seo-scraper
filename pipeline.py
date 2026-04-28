"""
seo-scraper pipeline.py
Run: python3 pipeline.py --keywords data/keywords.json

Keywords file format:
[
  {
    "product_handle": "18k-turquoise-bracelet",
    "product_title": "18k Gold Turquoise Beads Bracelet",
    "our_url": "https://www.ellacreationsjewelry.com/products/18k-turquoise-bracelet",
    "views_90d": 640,
    "queries": ["18k solid gold turquoise bracelet", ...]
  },
  ...
]
"""

import asyncio
import argparse
import json
import os
import sys
from urllib.parse import urlparse

from db import init_db, get_conn
from crawlers.serp_crawler import scrape_serp, save_serp_results
from extraction.classifier import classify_unprocessed
from monitoring.delta import detect_deltas

PROXY_SERP    = os.environ.get('PROXY_SERP', '')
PROXY_PRODUCT = os.environ.get('PROXY_PRODUCT', '')

# Our own domains — used to detect when we appear in SERP results
OUR_DOMAINS = {'ellacreationsjewelry.com', 'yasminnabulsi.com', 'www.ellacreationsjewelry.com'}


def get_domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().replace('www.', '')
    except Exception:
        return ''


def load_keywords(path: str) -> tuple[list[str], list[dict]]:
    """Returns (flat_queries, full_product_items)."""
    with open(path) as f:
        data = json.load(f)
    queries = []
    seen = set()
    for item in data:
        for q in item.get('queries', []):
            if q not in seen:
                seen.add(q)
                queries.append(q)
    return queries, data


def seed_our_products(items: list[dict]):
    """Upsert our_products table from keywords.json items."""
    conn = get_conn()
    for item in items:
        handle = item.get('product_handle', '')
        title  = item.get('product_title', item.get('product_handle', ''))
        our_url = item.get('our_url', '')
        views   = item.get('views_90d', 0)
        queries_json = json.dumps(item.get('queries', []))
        conn.execute('''
            INSERT INTO our_products (handle, title, our_url, views_90d, queries_json)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(handle) DO UPDATE SET
                title=excluded.title,
                our_url=excluded.our_url,
                views_90d=excluded.views_90d,
                queries_json=excluded.queries_json
        ''', (handle, title, our_url, views, queries_json))
    conn.commit()
    conn.close()
    print(f'  Seeded {len(items)} products into our_products table')


def build_query_to_product_map(conn) -> dict:
    """Build {query_string: our_product row} mapping from our_products table."""
    rows = conn.execute('SELECT * FROM our_products').fetchall()
    mapping = {}
    for row in rows:
        queries = json.loads(row['queries_json'])
        for q in queries:
            mapping[q] = dict(row)
    return mapping


def upsert_competitor(conn, domain: str) -> int:
    """Upsert competitor by domain, return competitor_id."""
    conn.execute('''
        INSERT INTO competitors (domain)
        VALUES (?)
        ON CONFLICT(domain) DO UPDATE SET updated_at=CURRENT_TIMESTAMP
    ''', (domain,))
    conn.commit()
    row = conn.execute('SELECT id FROM competitors WHERE domain=?', (domain,)).fetchone()
    return row['id']


def upsert_competitor_product(conn, competitor_id: int, url: str) -> int:
    """Upsert competitor_products, return product_id."""
    conn.execute('''
        INSERT INTO competitor_products (competitor_id, url)
        VALUES (?, ?)
        ON CONFLICT(url) DO NOTHING
    ''', (competitor_id, url))
    conn.commit()
    row = conn.execute('SELECT id FROM competitor_products WHERE url=?', (url,)).fetchone()
    return row['id']


def save_serp_snapshots(results: list[dict], query_product_map: dict):
    """
    For each query in results, create a serp_snapshot with our position,
    upsert competitor domains + products, and store serp_snapshot_results.
    """
    conn = get_conn()

    # Group results by query
    by_query = {}
    for r in results:
        by_query.setdefault(r['query'], []).append(r)

    for query, rows in by_query.items():
        # Sort by position
        rows_sorted = sorted(rows, key=lambda x: x['position'])

        # Find our position
        our_position = None
        our_product = query_product_map.get(query)
        our_url = our_product['our_url'] if our_product else None
        our_product_id = our_product['id'] if our_product else None

        if our_url:
            our_domain = get_domain(our_url)
            for r in rows_sorted:
                if get_domain(r['url']) == our_domain or r['url'].rstrip('/') == our_url.rstrip('/'):
                    our_position = r['position']
                    break

        # Insert snapshot header
        cur = conn.execute('''
            INSERT INTO serp_snapshots (our_product_id, query, our_url, our_position, total_results)
            VALUES (?, ?, ?, ?, ?)
        ''', (our_product_id, query, our_url, our_position, len(rows_sorted)))
        snapshot_id = cur.lastrowid
        conn.commit()

        # Insert snapshot results + upsert competitors
        for r in rows_sorted:
            url    = r['url']
            domain = get_domain(url)
            is_ours = 1 if domain in {d.replace('www.', '') for d in OUR_DOMAINS} else 0

            competitor_id = None
            if not is_ours and domain:
                competitor_id = upsert_competitor(conn, domain)
                upsert_competitor_product(conn, competitor_id, url)

            conn.execute('''
                INSERT INTO serp_snapshot_results
                    (snapshot_id, position, url, title, domain, competitor_id, is_ours)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (snapshot_id, r['position'], url, r.get('title', ''), domain, competitor_id, is_ours))

        conn.commit()
        pos_str = str(our_position) if our_position else 'not ranked'
        print(f'  Snapshot saved: "{query[:50]}" — our position: {pos_str}')

    conn.close()


def get_product_urls_from_db() -> list[str]:
    """Get all SERP result URLs not yet in product_pages."""
    conn = get_conn()
    rows = conn.execute('''
        SELECT DISTINCT s.url FROM serp_results s
        LEFT JOIN product_pages p ON s.url = p.url
        WHERE p.url IS NULL
    ''').fetchall()
    conn.close()
    return [r['url'] for r in rows]


async def crawl_product_pages(urls: list[str]):
    """Fetch product page HTML and save to product_pages table."""
    from crawlee.crawlers import PlaywrightCrawler, PlaywrightCrawlingContext
    from crawlee._request import Request
    from crawlee.proxy_configuration import ProxyConfiguration
    from crawlee import ConcurrencySettings

    conn = get_conn()

    proxy_config = None
    if PROXY_PRODUCT:
        proxy_config = ProxyConfiguration(proxy_urls=[PROXY_PRODUCT])

    crawler = PlaywrightCrawler(
        proxy_configuration=proxy_config,
        max_request_retries=3,
        headless=True,
        concurrency_settings=ConcurrencySettings(desired_concurrency=2, max_concurrency=2),
        browser_launch_options={'args': ['--no-sandbox', '--disable-setuid-sandbox']},
    )

    @crawler.router.default_handler
    async def handle_page(ctx: PlaywrightCrawlingContext):
        await ctx.page.wait_for_load_state('domcontentloaded')
        title = await ctx.page.title()
        html  = await ctx.page.content()
        conn.execute('''
            INSERT OR IGNORE INTO product_pages (url, page_title, raw_html)
            VALUES (?, ?, ?)
        ''', (ctx.request.url, title, html[:50000]))
        conn.commit()
        print(f'  Fetched: {title[:60]} [{ctx.request.url[:60]}]')

    requests = [Request.from_url(u) for u in urls]
    await crawler.run(requests)
    conn.close()


def print_summary():
    conn = get_conn()
    serp_count   = conn.execute('SELECT COUNT(*) FROM serp_results').fetchone()[0]
    page_count   = conn.execute('SELECT COUNT(*) FROM product_pages').fetchone()[0]
    class_count  = conn.execute('SELECT COUNT(*) FROM classifications').fetchone()[0]
    solid_count  = conn.execute("SELECT COUNT(*) FROM classifications WHERE metal_type='solid_gold'").fetchone()[0]
    vermeil      = conn.execute("SELECT COUNT(*) FROM classifications WHERE metal_type='vermeil'").fetchone()[0]
    plated       = conn.execute("SELECT COUNT(*) FROM classifications WHERE metal_type IN ('gold_plated','gold_filled')").fetchone()[0]
    snap_count   = conn.execute('SELECT COUNT(*) FROM serp_snapshots').fetchone()[0]
    comp_count   = conn.execute('SELECT COUNT(*) FROM competitors').fetchone()[0]
    prod_snap    = conn.execute('SELECT COUNT(*) FROM product_snapshots').fetchone()[0]
    conn.close()

    print('\n--- Pipeline Summary ---')
    print(f'SERP results:        {serp_count}')
    print(f'SERP snapshots:      {snap_count}')
    print(f'Competitors tracked: {comp_count}')
    print(f'Product pages:       {page_count}')
    print(f'Product snapshots:   {prod_snap}')
    print(f'Classified:          {class_count}')
    print(f'  solid_gold:        {solid_count}')
    print(f'  vermeil:           {vermeil}')
    print(f'  plated/filled:     {plated}')


def main():
    parser = argparse.ArgumentParser(description='SEO Scraper Pipeline')
    parser.add_argument('--keywords', default='data/keywords.json', help='Path to keywords JSON')
    parser.add_argument('--skip-serp',      action='store_true', help='Skip SERP crawl')
    parser.add_argument('--skip-pages',     action='store_true', help='Skip product page crawl')
    parser.add_argument('--skip-classify',  action='store_true', help='Skip classification')
    parser.add_argument('--skip-delta',     action='store_true', help='Skip delta detection')
    parser.add_argument('--classify-limit', type=int, default=50, help='Max pages to classify per run')
    args = parser.parse_args()

    print('Initialising DB...')
    init_db()

    queries, product_items = load_keywords(args.keywords)

    print(f'\nSeeding {len(product_items)} products...')
    seed_our_products(product_items)

    # Reload our_products with IDs from DB (needed for snapshot linkage)
    conn = get_conn()
    query_product_map = build_query_to_product_map(conn)
    conn.close()

    if not args.skip_serp:
        print(f'\nStep 1: SERP crawl ({len(queries)} unique queries)')
        results = scrape_serp(queries)
        save_serp_results(results)

        print('\nStep 1b: Saving SERP snapshots + competitor registry')
        save_serp_snapshots(results, query_product_map)

    if not args.skip_pages:
        print('\nStep 2: Product page crawl')
        urls = get_product_urls_from_db()
        print(f'  {len(urls)} new URLs to fetch')
        if urls:
            asyncio.run(crawl_product_pages(urls))

    if not args.skip_classify:
        print(f'\nStep 3: Classify + save product snapshots (limit={args.classify_limit})')
        classify_unprocessed(limit=args.classify_limit)

    if not args.skip_delta:
        print('\nStep 4: Delta detection')
        detect_deltas()

    print_summary()


if __name__ == '__main__':
    main()
