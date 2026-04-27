import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), 'data', 'scraper.db')

def get_conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS serp_results (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            query     TEXT NOT NULL,
            position  INTEGER NOT NULL,
            url       TEXT NOT NULL,
            title     TEXT,
            snippet   TEXT,
            scraped_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(query, position)
        );

        CREATE TABLE IF NOT EXISTS product_pages (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            url        TEXT UNIQUE NOT NULL,
            page_title TEXT,
            raw_html   TEXT,
            scraped_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS classifications (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            url         TEXT UNIQUE NOT NULL,
            metal_type  TEXT,   -- solid_gold | vermeil | gold_plated | gold_filled | sterling_silver | unknown
            karat       TEXT,   -- 18k | 14k | 10k | unknown
            base_metal  TEXT,   -- sterling_silver | brass | copper | unknown
            price_usd   REAL,
            evidence    TEXT,   -- exact quote from page that determined classification
            confidence  TEXT,   -- high | medium | low
            classified_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS deltas (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            url          TEXT NOT NULL,
            field        TEXT NOT NULL,  -- metal_type | karat | price_usd
            old_value    TEXT,
            new_value    TEXT,
            detected_at  DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_serp_query ON serp_results(query);
        CREATE INDEX IF NOT EXISTS idx_class_url  ON classifications(url);
    ''')
    conn.commit()
    conn.close()
    print(f'DB initialised at {DB_PATH}')

if __name__ == '__main__':
    init_db()
