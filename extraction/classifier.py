"""
extraction/classifier.py

Fetches competitor product pages via Bright Data API, strips HTML to plain text,
then calls Claude Haiku to extract metal type, karat, price, title, description,
and materials. Saves to classifications + product_snapshots tables.
"""

import os
import re
import sys
import json
import time
import urllib.request
from html.parser import HTMLParser

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from db import get_conn

import anthropic

ANTHROPIC_KEY      = os.environ.get('ANTHROPIC_API_KEY', '')
BRIGHTDATA_API_KEY = os.environ.get('BRIGHTDATA_API_KEY', '')
BRIGHTDATA_ZONE    = os.environ.get('BRIGHTDATA_ZONE', 'web_unlocker1')
BRIGHTDATA_API_URL = 'https://api.brightdata.com/request'

SYSTEM_PROMPT = (
    "You are a jewelry product data extractor. Analyse the product page text and return "
    "a single JSON object — no explanation, no markdown, just the JSON."
)

EXTRACTION_PROMPT = (
    "Analyse this jewelry product page text and extract all of the following fields:\n\n"
    "1. metal_type: one of solid_gold | vermeil | gold_plated | gold_filled | sterling_silver | unknown\n"
    "   - solid_gold = described as solid 10k/14k/18k/24k gold (NOT plated, NOT vermeil)\n"
    "   - vermeil = gold-plated sterling silver base\n"
    "   - gold_plated = brass/copper base with gold electroplating\n"
    "   - gold_filled = base metal with thick mechanically bonded gold layer\n"
    "   - sterling_silver = silver only, no gold\n"
    "2. karat: 9k | 10k | 14k | 18k | 24k | unknown\n"
    "3. base_metal: sterling_silver | brass | copper | gold | unknown\n"
    "4. price_usd: numeric price in USD (lowest listed), or null\n"
    "5. price_raw: raw price string as shown on page (e.g. '$124.00'), or null\n"
    "6. title: product title as shown on the page (max 120 chars)\n"
    "7. description: concise product description (max 300 chars, your own words)\n"
    "8. materials: comma-separated list of materials/gemstones mentioned (e.g. '18k gold, turquoise, freshwater pearls')\n"
    "9. availability: 'in_stock' | 'out_of_stock' | 'unknown'\n"
    "10. evidence: exact short quote (max 100 chars) from the text that determined metal_type\n"
    "11. confidence: high | medium | low\n\n"
    "Return JSON only:\n"
    '{"metal_type":"...","karat":"...","base_metal":"...","price_usd":null,"price_raw":null,'
    '"title":"...","description":"...","materials":"...","availability":"...","evidence":"...","confidence":"..."}\n\n'
    "PAGE TEXT:\n{page_text}"
)


# ---------------------------------------------------------------------------
# HTML → plain text
# ---------------------------------------------------------------------------

def _html_to_text(html: str) -> str:
    parts = []

    # 1. Page title
    title_m = re.search(r'<title[^>]*>(.*?)</title>', html, re.IGNORECASE | re.DOTALL)
    if title_m:
        parts.append(title_m.group(1).strip())

    # 2. JSON-LD structured data
    jsonld_blocks = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.IGNORECASE | re.DOTALL
    )
    for block in jsonld_blocks:
        try:
            data = json.loads(block.strip())
            def _flatten(obj, prefix=''):
                if isinstance(obj, dict):
                    for k, v in obj.items():
                        _flatten(v, k)
                elif isinstance(obj, list):
                    for item in obj:
                        _flatten(item, prefix)
                else:
                    s = str(obj).strip()
                    if s and len(s) < 200:
                        parts.append(f'{prefix}: {s}' if prefix else s)
            _flatten(data)
        except Exception:
            parts.append(block[:500])

    # 3. Inline product JSON
    meta_m = re.search(r'"product"\s*:\s*(\{[^}]{20,500}\})', html)
    if meta_m:
        parts.append(meta_m.group(1))

    # 4. Fallback — strip tags
    if not parts or len(' '.join(parts)) < 100:
        stripped = re.sub(r'<script[^>]*>.*?</script>', ' ', html, flags=re.DOTALL | re.IGNORECASE)
        stripped = re.sub(r'<style[^>]*>.*?</style>',  ' ', stripped, flags=re.DOTALL | re.IGNORECASE)
        stripped = re.sub(r'<[^>]+>', ' ', stripped)
        stripped = re.sub(r'\s+', ' ', stripped)
        parts.append(stripped[:3000])

    text = ' | '.join(str(p) for p in parts if str(p).strip())
    text = re.sub(r'\s+', ' ', text)
    return text[:8000]


# ---------------------------------------------------------------------------
# Bright Data API fetch
# ---------------------------------------------------------------------------

def _fetch_html(url: str, retries: int = 3) -> str:
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
                time.sleep(2 * attempt)
    return ''


# ---------------------------------------------------------------------------
# Claude Haiku extraction
# ---------------------------------------------------------------------------

def _classify_text(page_text: str) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    prompt = EXTRACTION_PROMPT.replace('{page_text}', page_text)
    msg = client.messages.create(
        model='claude-haiku-4-5-20251001',
        max_tokens=512,
        system=SYSTEM_PROMPT,
        messages=[{'role': 'user', 'content': prompt}],
    )
    raw = msg.content[0].text.strip()
    raw = re.sub(r'^```[a-z]*\n?', '', raw)
    raw = re.sub(r'\n?```$', '', raw)
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Product snapshot helpers
# ---------------------------------------------------------------------------

def _get_competitor_product_id(conn, url: str) -> int | None:
    """Look up competitor_products.id for a URL."""
    row = conn.execute(
        'SELECT id FROM competitor_products WHERE url=?', (url,)
    ).fetchone()
    return row['id'] if row else None


def _get_last_product_snapshot(conn, product_id: int) -> dict | None:
    """Get the most recent product snapshot for a competitor_product."""
    row = conn.execute('''
        SELECT price_usd, description FROM product_snapshots
        WHERE product_id=?
        ORDER BY scraped_at DESC LIMIT 1
    ''', (product_id,)).fetchone()
    return dict(row) if row else None


def save_product_snapshot(conn, url: str, data: dict):
    """Save a product_snapshot row with change detection vs previous snapshot."""
    product_id = _get_competitor_product_id(conn, url)
    if product_id is None:
        return  # Not a tracked competitor URL — skip

    prev = _get_last_product_snapshot(conn, product_id)
    price_changed = 0
    desc_changed  = 0

    if prev:
        new_price = data.get('price_usd')
        old_price = prev.get('price_usd')
        if new_price is not None and old_price is not None and abs(new_price - old_price) > 0.01:
            price_changed = 1

        new_desc = (data.get('description') or '').strip()[:200]
        old_desc = (prev.get('description') or '').strip()[:200]
        if new_desc and old_desc and new_desc != old_desc:
            desc_changed = 1

    conn.execute('''
        INSERT INTO product_snapshots
            (product_id, title, price_raw, price_usd, description,
             metal_type, karat, materials, availability,
             price_changed, description_changed)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        product_id,
        (data.get('title') or '')[:120],
        data.get('price_raw'),
        data.get('price_usd'),
        (data.get('description') or '')[:300],
        data.get('metal_type', 'unknown'),
        data.get('karat', 'unknown'),
        data.get('materials', ''),
        data.get('availability', 'unknown'),
        price_changed,
        desc_changed,
    ))

    # Update last_scraped_at on competitor_products
    conn.execute(
        'UPDATE competitor_products SET last_scraped_at=CURRENT_TIMESTAMP WHERE id=?',
        (product_id,)
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def classify_url(url: str) -> dict:
    html = _fetch_html(url)
    if not html or len(html) < 300:
        return _fallback(f'empty page ({len(html)} chars)')

    page_text = _html_to_text(html)
    if not page_text.strip():
        return _fallback('could not extract text from HTML')

    try:
        result = _classify_text(page_text)
        return result
    except json.JSONDecodeError as e:
        print(f'    JSON parse error: {e}')
        return _fallback('JSON parse error')
    except Exception as e:
        print(f'    Haiku call failed: {e}')
        return _fallback(str(e)[:80])


def _fallback(reason: str = '') -> dict:
    return {
        'metal_type':   'unknown',
        'karat':        'unknown',
        'base_metal':   'unknown',
        'price_usd':    None,
        'price_raw':    None,
        'title':        '',
        'description':  '',
        'materials':    '',
        'availability': 'unknown',
        'evidence':     f'fallback: {reason}'[:120],
        'confidence':   'low',
    }


def classify_unprocessed(limit: int = 50):
    conn = get_conn()
    rows = conn.execute('''
        SELECT p.url FROM product_pages p
        LEFT JOIN classifications c ON p.url = c.url
        WHERE c.url IS NULL
        LIMIT ?
    ''', (limit,)).fetchall()
    conn.close()

    print(f'Classifying {len(rows)} URLs...')
    for row in rows:
        url = row['url']
        print(f'  -> {url[:70]}')
        result = classify_url(url)
        metal = result.get('metal_type', '?')
        karat = result.get('karat', '?')
        conf  = result.get('confidence', '?')
        price = result.get('price_usd')
        title = result.get('title', '')[:50]
        ev    = result.get('evidence', '')[:60]
        print(f'     [{metal} | {karat} | ${price} | conf:{conf}] "{ev}"')
        if title:
            print(f'     Title: {title}')
        save_classification(url, result)
        # Also save product snapshot
        conn = get_conn()
        save_product_snapshot(conn, url, result)
        conn.close()
        time.sleep(0.2)


def save_classification(url: str, data: dict):
    conn = get_conn()
    conn.execute('''
        INSERT OR REPLACE INTO classifications
            (url, metal_type, karat, base_metal, price_usd, evidence, confidence)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (
        url,
        data.get('metal_type', 'unknown'),
        data.get('karat', 'unknown'),
        data.get('base_metal', 'unknown'),
        data.get('price_usd'),
        data.get('evidence', ''),
        data.get('confidence', 'low'),
    ))
    conn.commit()
    conn.close()


if __name__ == '__main__':
    classify_unprocessed(limit=10)
