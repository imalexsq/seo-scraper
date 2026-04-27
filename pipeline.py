"""
seo-scraper pipeline.py
Run: python3 pipeline.py --keywords data/keywords.json

Keywords file format:
[
  {"product_handle": "18k-turquoise-bracelet", "queries": ["18k solid gold turquoise bracelet", ...]},
  ...
]
"""

import asyncio
import argparse
import json
import os
import sys

from db import init_db, get_conn
from crawlers.serp_crawler import scrape_serp, save_serp_results
from extraction.classifier import classify_unprocessed

PROXY_SERP    = os.environ.get('PROXY_SERP', '')     # Bright Data residential
PROXY_PRODUCT = os.environ.get('PROXY_PRODUCT', '')  # IPRoyal residential


def load_keywords(path: str) -> list[str]:
    with open(path) as f:
        data = json.load(f)
    queries = []
    for item in data:
        queries.extend(item.get('queries', []))
    # deduplicate while preserving order
    seen = set()
    unique = []
    for q in queries:
        if q not in seen:
            seen.add(q)
            unique.append(q)
    return unique


def get_product_urls_from_db() -> list[str]:
    """Get all SERP result URLs not yet in product_pages."""
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT DISTINCT s.url FROM serp_results s
        LEFT JOIN product_pages p ON s.url = p.url
        WHERE p.url IS NULL
        """
    ).fetchall()
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
        # Store trimmed HTML (first 50k chars is enough for extraction)
        html = await ctx.page.content()
        conn.execute(
            """
            INSERT OR IGNORE INTO product_pages (url, page_title, raw_html)
            VALUES (?, ?, ?)
            """,
            (ctx.request.url, title, html[:50000]),
        )
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
    conn.close()
    print('\n--- Pipeline Summary ---')
    print(f'SERP results:    {serp_count}')
    print(f'Product pages:   {page_count}')
    print(f'Classified:      {class_count}')
    print(f'  solid_gold:    {solid_count}')
    print(f'  vermeil:       {vermeil}')
    print(f'  plated/filled: {plated}')


def main():
    parser = argparse.ArgumentParser(description='SEO Scraper Pipeline')
    parser.add_argument('--keywords', default='data/keywords.json', help='Path to keywords JSON')
    parser.add_argument('--skip-serp',    action='store_true', help='Skip SERP crawl (use existing DB)')
    parser.add_argument('--skip-pages',   action='store_true', help='Skip product page crawl')
    parser.add_argument('--skip-classify',action='store_true', help='Skip classification')
    parser.add_argument('--classify-limit', type=int, default=50, help='Max pages to classify per run')
    args = parser.parse_args()

    print('Initialising DB...')
    init_db()

    if not args.skip_serp:
        print(f'\nStep 1: SERP crawl from {args.keywords}')
        queries = load_keywords(args.keywords)
        print(f'  {len(queries)} unique queries')
        results = asyncio.run(scrape_serp(queries, proxy_url=PROXY_SERP))
        save_serp_results(results)

    if not args.skip_pages:
        print('\nStep 2: Product page crawl')
        urls = get_product_urls_from_db()
        print(f'  {len(urls)} new URLs to fetch')
        if urls:
            asyncio.run(crawl_product_pages(urls))

    if not args.skip_classify:
        print(f'\nStep 3: Classify (limit={args.classify_limit})')
        classify_unprocessed(limit=args.classify_limit)

    print_summary()


if __name__ == '__main__':
    main()
