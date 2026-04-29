"""
extraction/etsy_extractor.py

Fetches individual Etsy listing pages via Bright Data, extracts structured metadata
(title, price, shop, materials, tags, reviews, favorites), then classifies metal type
using the existing Claude Haiku classifier.
"""

import os
import re
import sys
import json
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from db import get_conn
from extraction.classifier import _classify_text, _fallback

BRIGHTDATA_API_KEY = os.environ.get('BRIGHTDATA_API_KEY', '')
BRIGHTDATA_ZONE    = os.environ.get('BRIGHTDATA_ZONE', 'web_unlocker1')
BRIGHTDATA_API_URL = 'https://api.brightdata.com/request'


def _fetch_html(url: str, retries: int = 1) -> str:
    if not BRIGHTDATA_API_KEY:
        return ''
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
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode('utf-8', errors='replace')
        except Exception as e:
            print(f'    fetch attempt {attempt}/{retries}: {e}')
            if attempt < retries:
                time.sleep(3 * attempt)
    return ''


def _extract_listing_metadata(html: str) -> dict:
    meta = {
        'title':       None,
        'price_usd':   None,
        'shop':        None,
        'materials':   [],
        'tags':        [],
        'reviews':     None,
        'favorites':   None,
        'description': None,
    }

    # Strategy 1: JSON-LD Product schema
    jsonld_blocks = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.IGNORECASE | re.DOTALL
    )
    for block in jsonld_blocks:
        try:
            obj = json.loads(block.strip())
            items = obj if isinstance(obj, list) else [obj]
            for item in items:
                if item.get('@type') in ('Product', 'IndividualProduct'):
                    meta['title'] = meta['title'] or item.get('name')
                    meta['description'] = meta['description'] or (item.get('description') or '')[:500]

                    offers = item.get('offers', {})
                    if isinstance(offers, list) and offers:
                        offers = offers[0]
                    if isinstance(offers, dict) and not meta['price_usd']:
                        raw = offers.get('price') or offers.get('lowPrice', '')
                        try:
                            meta['price_usd'] = float(raw) if raw else None
                        except (ValueError, TypeError):
                            pass

                    brand = item.get('brand', {})
                    if isinstance(brand, dict):
                        meta['shop'] = meta['shop'] or brand.get('name')
                    elif isinstance(brand, str):
                        meta['shop'] = meta['shop'] or brand

                    seller = item.get('seller', {})
                    if isinstance(seller, dict):
                        meta['shop'] = meta['shop'] or seller.get('name')
        except Exception:
            continue

    # Strategy 2: Etsy's inline JS data — materials, tags, favorites
    tags_m = re.search(r'"tags"\s*:\s*(\[[^\]]{0,1000}\])', html)
    if tags_m:
        try:
            meta['tags'] = json.loads(tags_m.group(1))
        except Exception:
            pass

    materials_m = re.search(r'"materials"\s*:\s*(\[[^\]]{0,500}\])', html)
    if materials_m:
        try:
            meta['materials'] = json.loads(materials_m.group(1))
        except Exception:
            pass

    fav_m = re.search(r'"num_favorers"\s*:\s*(\d+)', html)
    if fav_m:
        meta['favorites'] = int(fav_m.group(1))

    reviews_m = re.search(r'"num_ratings"\s*:\s*(\d+)', html)
    if reviews_m:
        meta['reviews'] = int(reviews_m.group(1))

    # Fallback: title from <title> tag
    if not meta['title']:
        title_m = re.search(r'<title[^>]*>(.*?)</title>', html, re.IGNORECASE | re.DOTALL)
        if title_m:
            meta['title'] = title_m.group(1).strip().split('|')[0].strip()

    # Fallback: price from visible text
    if not meta['price_usd']:
        price_m = re.search(r'\$\s*(\d+(?:\.\d{2})?)', html)
        if price_m:
            try:
                meta['price_usd'] = float(price_m.group(1))
            except ValueError:
                pass

    return meta


def _build_classification_text(meta: dict) -> str:
    parts = []
    if meta.get('title'):
        parts.append(f"Title: {meta['title']}")
    if meta.get('description'):
        parts.append(f"Description: {meta['description']}")
    if meta.get('materials'):
        parts.append(f"Materials: {', '.join(str(m) for m in meta['materials'])}")
    if meta.get('tags'):
        parts.append(f"Tags: {', '.join(str(t) for t in meta['tags'])}")
    return ' | '.join(parts)[:8000]


def _save_listing(url: str, meta: dict, classification: dict):
    conn = get_conn()
    listing_id_m = re.search(r'/listing/(\d+)/', url)
    listing_id = listing_id_m.group(1) if listing_id_m else None

    conn.execute(
        """
        INSERT OR REPLACE INTO etsy_listings
            (listing_id, url, title, price_usd, shop, materials, tags,
             reviews, favorites, metal_type, karat, confidence)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            listing_id,
            url,
            meta.get('title'),
            meta.get('price_usd') or classification.get('price_usd'),
            meta.get('shop'),
            json.dumps(meta.get('materials', [])),
            json.dumps(meta.get('tags', [])),
            meta.get('reviews'),
            meta.get('favorites'),
            classification.get('metal_type', 'unknown'),
            classification.get('karat', 'unknown'),
            classification.get('confidence', 'low'),
        ),
    )
    conn.commit()
    conn.close()


def extract_and_save_etsy_listing(url: str) -> dict:
    html = _fetch_html(url)
    if not html or len(html) < 500:
        result = _fallback(f'empty page ({len(html)} chars)')
        _save_listing(url, {}, result)
        return result

    meta = _extract_listing_metadata(html)
    text = _build_classification_text(meta)

    if not text.strip():
        result = _fallback('could not extract text')
        _save_listing(url, meta, result)
        return result

    try:
        classification = _classify_text(text)
    except Exception as e:
        print(f'    Haiku classification failed: {e}')
        classification = _fallback(str(e)[:80])

    _save_listing(url, meta, classification)
    return classification


def process_etsy_listings(limit: int = 50):
    from crawlers.etsy_serp_crawler import get_unscraped_etsy_listing_urls

    urls = get_unscraped_etsy_listing_urls()[:limit]
    print(f'Processing {len(urls)} Etsy listings...')

    for url in urls:
        print(f'  -> {url[:70]}')
        result = extract_and_save_etsy_listing(url)
        metal = result.get('metal_type', '?')
        karat = result.get('karat', '?')
        conf  = result.get('confidence', '?')
        print(f'     [{metal} | {karat} | conf:{conf}]')
        time.sleep(0.5)


if __name__ == '__main__':
    from db import init_db
    init_db()
    process_etsy_listings(limit=5)
