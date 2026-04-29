"""
crawlers/etsy_serp_crawler.py

Fetches Etsy search results via Bright Data Web Unlocker API.
Parses listing cards to extract URL, title, price, shop, reviews, star-seller status.
"""

import os
import sys
import json
import re
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from db import get_conn

BRIGHTDATA_API_KEY = os.environ.get('BRIGHTDATA_API_KEY', '')
BRIGHTDATA_ZONE    = os.environ.get('BRIGHTDATA_ZONE', 'web_unlocker1')
BRIGHTDATA_API_URL = 'https://api.brightdata.com/request'

ETSY_SEARCH_URL = 'https://www.etsy.com/search?q={query}&explicit=1'


def _fetch_via_api(url: str, retries: int = 3) -> str:
    payload = json.dumps({
        'zone':   BRIGHTDATA_ZONE,
        'url':    url,
        'format': 'raw',
    }).encode()
    headers = {
        'Content-Type':  'application/json',
        'Authorization': f'Bearer {BRIGHTDATA_API_KEY}',
    }
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(
                BRIGHTDATA_API_URL, data=payload, headers=headers, method='POST'
            )
            with urllib.request.urlopen(req, timeout=45) as resp:
                return resp.read().decode('utf-8', errors='replace')
        except Exception as e:
            print(f'  API fetch attempt {attempt}/{retries} failed: {e}')
            if attempt < retries:
                time.sleep(3 * attempt)
    return ''


def _extract_listing_id(url: str) -> str:
    m = re.search(r'/listing/(\d+)/', url)
    return m.group(1) if m else ''


def _parse_etsy_serp_html(html: str, query: str) -> list:
    results = []

    # Strategy 1: JSON-LD ItemList (Etsy embeds this for SEO)
    jsonld_blocks = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.IGNORECASE | re.DOTALL
    )
    for block in jsonld_blocks:
        try:
            data = json.loads(block.strip())
            items = []
            if isinstance(data, dict) and data.get('@type') == 'ItemList':
                items = data.get('itemListElement', [])
            elif isinstance(data, list):
                for d in data:
                    if isinstance(d, dict) and d.get('@type') == 'ItemList':
                        items = d.get('itemListElement', [])
                        break

            for item in items:
                thing = item.get('item', item)
                url = thing.get('url', '') or item.get('url', '')
                if not url or 'etsy.com' not in url:
                    continue
                title = thing.get('name', '') or item.get('name', '')
                position = item.get('position', len(results) + 1)

                offers = thing.get('offers', {})
                if isinstance(offers, list) and offers:
                    offers = offers[0]
                price = None
                if isinstance(offers, dict):
                    raw_price = offers.get('price') or offers.get('lowPrice', '')
                    try:
                        price = float(raw_price) if raw_price else None
                    except (ValueError, TypeError):
                        price = None

                listing_id = _extract_listing_id(url)
                results.append({
                    'query':       query,
                    'position':    int(position),
                    'listing_id':  listing_id,
                    'url':         url.split('?')[0],
                    'title':       title,
                    'price_usd':   price,
                    'shop':        None,
                    'reviews':     None,
                    'star_seller': False,
                })
        except Exception:
            continue

    if results:
        seen = set()
        unique = []
        for r in results:
            key = r['listing_id'] or r['url']
            if key not in seen:
                seen.add(key)
                unique.append(r)
        return unique[:48]

    # Strategy 2: regex on data-listing-id attributes
    listing_ids = re.findall(r'data-listing-id=["\'](\d+)["\']', html)
    seen = set()
    for lid in listing_ids:
        if lid in seen:
            continue
        seen.add(lid)

        idx = html.find(f'data-listing-id="{lid}"')
        if idx == -1:
            idx = html.find(f"data-listing-id='{lid}'")
        snippet = html[max(0, idx - 200):idx + 1000] if idx != -1 else ''

        url_m = re.search(r'href=["\']([^"\']*listing/' + lid + r'[^"\']*)["\']', snippet)
        url = ''
        if url_m:
            url = url_m.group(1)
            if url.startswith('/'):
                url = 'https://www.etsy.com' + url
        if not url:
            url = f'https://www.etsy.com/listing/{lid}/'

        title_m = re.search(r'<h[23][^>]*>\s*([^<]{10,200})\s*</h[23]>', snippet)
        title = title_m.group(1).strip() if title_m else ''

        price_m = re.search(r'\$\s*(\d+(?:\.\d{2})?)', snippet)
        price = float(price_m.group(1)) if price_m else None

        star_seller = 'star-seller' in snippet.lower() or 'star_seller' in snippet.lower()

        results.append({
            'query':       query,
            'position':    len(results) + 1,
            'listing_id':  lid,
            'url':         url.split('?')[0],
            'title':       title,
            'price_usd':   price,
            'shop':        None,
            'reviews':     None,
            'star_seller': star_seller,
        })

    return results[:48]


def scrape_etsy_serp(queries: list) -> list:
    if not BRIGHTDATA_API_KEY:
        raise ValueError('BRIGHTDATA_API_KEY not set')

    all_results = []
    for query in queries:
        etsy_url = ETSY_SEARCH_URL.replace('{query}', urllib.parse.quote_plus(query))
        print(f'  Fetching Etsy SERP: {query[:60]}')
        html = _fetch_via_api(etsy_url)
        if not html:
            print(f'    -> empty response, skipping')
            continue

        items = _parse_etsy_serp_html(html, query)
        for i, item in enumerate(items):
            if item['position'] == 0:
                item['position'] = i + 1
        all_results.extend(items)
        print(f'    -> {len(items)} listings')
        time.sleep(1.5)

    return all_results


def save_etsy_serp_results(results: list):
    conn = get_conn()
    saved = 0
    for r in results:
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO etsy_serp_results
                    (query, position, listing_id, url, title, price_usd, shop, reviews, star_seller)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    r['query'], r['position'], r.get('listing_id', ''),
                    r['url'], r.get('title', ''), r.get('price_usd'),
                    r.get('shop'), r.get('reviews'),
                    1 if r.get('star_seller') else 0,
                ),
            )
            saved += 1
        except Exception as e:
            print(f'  DB error: {e}')
    conn.commit()
    conn.close()
    print(f'Saved {saved} Etsy SERP results to DB')


def get_unscraped_etsy_listing_urls() -> list:
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT DISTINCT s.url FROM etsy_serp_results s
        LEFT JOIN etsy_listings l ON s.url = l.url
        WHERE l.url IS NULL
        """
    ).fetchall()
    conn.close()
    return [r['url'] for r in rows]


if __name__ == '__main__':
    from db import init_db
    init_db()
    test_queries = [
        '18k gold turquoise bracelet',
        'solid gold name necklace',
    ]
    results = scrape_etsy_serp(test_queries)
    save_etsy_serp_results(results)
    print('\nTop results:')
    for r in results[:6]:
        print(f'  [{r["position"]}] {r["title"][:55]}')
        print(f'       {r["url"][:70]}')
