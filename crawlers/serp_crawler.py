"""
crawlers/serp_crawler.py

Fetches Google SERP results via Bright Data Web Unlocker API.
No IP whitelisting required — uses the REST API endpoint, not the proxy port.

For product page crawling (requires JS), we use Playwright + proxy port (33335).
"""

import os
import sys
import json
import time
import urllib.request
import urllib.parse
from html.parser import HTMLParser

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from db import get_conn

BRIGHTDATA_API_KEY = os.environ.get('BRIGHTDATA_API_KEY', '')
BRIGHTDATA_ZONE    = os.environ.get('BRIGHTDATA_ZONE', 'web_unlocker1')
BRIGHTDATA_API_URL = 'https://api.brightdata.com/request'

GOOGLE_URL = 'https://www.google.com/search?q={query}&num=10&hl=en&gl=us'


# ---------------------------------------------------------------------------
# Minimal HTML parser — extracts organic result links + titles from SERP HTML
# ---------------------------------------------------------------------------

class SerpParser(HTMLParser):
    """Extract organic result (link, title, snippet) tuples from Google HTML."""

    def __init__(self):
        super().__init__()
        self.results = []
        self._in_h3 = False
        self._current_h3 = ''
        self._current_href = None
        self._depth = 0
        # Stack-based tracking of <a> tags wrapping <h3>
        self._a_stack = []   # list of hrefs for open <a> tags
        self._h3_open = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'a':
            href = attrs.get('href', '')
            self._a_stack.append(href if href.startswith('http') else None)
        if tag == 'h3':
            self._h3_open = True
            self._current_h3 = ''
            # The href is the innermost <a> ancestor that has one
            self._current_href = next(
                (h for h in reversed(self._a_stack) if h), None
            )

    def handle_endtag(self, tag):
        if tag == 'a' and self._a_stack:
            self._a_stack.pop()
        if tag == 'h3' and self._h3_open:
            self._h3_open = False
            if self._current_href and self._current_h3.strip():
                self.results.append({
                    'url':   self._current_href,
                    'title': self._current_h3.strip(),
                })

    def handle_data(self, data):
        if self._h3_open:
            self._current_h3 += data


def _parse_serp_html(html: str) -> list:
    parser = SerpParser()
    parser.feed(html)
    # Deduplicate by URL, keep order
    seen = set()
    unique = []
    for r in parser.results:
        if r['url'] not in seen:
            seen.add(r['url'])
            unique.append(r)
    return unique[:10]


# ---------------------------------------------------------------------------
# Bright Data API fetch
# ---------------------------------------------------------------------------

def _fetch_via_api(url: str, retries: int = 3) -> str:
    """Fetch a URL through Bright Data Web Unlocker API. Returns HTML string."""
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
                BRIGHTDATA_API_URL,
                data=payload,
                headers=headers,
                method='POST',
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode('utf-8', errors='replace')
        except Exception as e:
            print(f'  API fetch attempt {attempt}/{retries} failed: {e}')
            if attempt < retries:
                time.sleep(2 * attempt)
    return ''


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def scrape_serp(queries: list, api_key: str = None) -> list:
    """
    Fetch Google SERP results for a list of queries via Bright Data API.
    Returns list of dicts: {query, position, url, title, snippet}.
    """
    if api_key:
        global BRIGHTDATA_API_KEY
        BRIGHTDATA_API_KEY = api_key

    if not BRIGHTDATA_API_KEY:
        raise ValueError('BRIGHTDATA_API_KEY not set — export it or pass api_key=')

    all_results = []
    for query in queries:
        google_url = GOOGLE_URL.replace('{query}', urllib.parse.quote_plus(query))
        print(f'  Fetching SERP: {query[:60]}')
        html = _fetch_via_api(google_url)
        if not html:
            print(f'    -> empty response, skipping')
            continue

        items = _parse_serp_html(html)
        for i, item in enumerate(items, start=1):
            all_results.append({
                'query':    query,
                'position': i,
                'url':      item['url'],
                'title':    item['title'],
                'snippet':  '',   # snippet parsing optional — title + URL enough for classifier
            })
        print(f'    -> {len(items)} results')
        time.sleep(0.5)   # gentle pacing between queries

    return all_results


def save_serp_results(results: list):
    conn = get_conn()
    saved = 0
    for r in results:
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO serp_results (query, position, url, title, snippet)
                VALUES (?, ?, ?, ?, ?)
                """,
                (r['query'], r['position'], r['url'], r['title'], r['snippet']),
            )
            saved += 1
        except Exception as e:
            print(f'  DB error: {e}')
    conn.commit()
    conn.close()
    print(f'Saved {saved} SERP results to DB')


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    from db import init_db
    init_db()

    test_queries = [
        '18k solid gold turquoise bracelet',
        'october birthstone necklace 18k gold',
        'arabic name necklace solid gold',
    ]

    results = scrape_serp(test_queries)
    save_serp_results(results)

    print('\nTop results:')
    for r in results[:9]:
        print(f'  [{r["position"]}] {r["title"][:55]}')
        print(f'       {r["url"][:70]}')
