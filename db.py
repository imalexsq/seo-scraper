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
        -- ---------------------------------------------------------------
        -- Original tables (unchanged)
        -- ---------------------------------------------------------------
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
            metal_type  TEXT,
            karat       TEXT,
            base_metal  TEXT,
            price_usd   REAL,
            evidence    TEXT,
            confidence  TEXT,
            classified_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS deltas (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            url          TEXT NOT NULL,
            field        TEXT NOT NULL,
            old_value    TEXT,
            new_value    TEXT,
            detected_at  DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_serp_query ON serp_results(query);
        CREATE INDEX IF NOT EXISTS idx_class_url  ON classifications(url);

        -- ---------------------------------------------------------------
        -- Competitor intelligence tables (new)
        -- ---------------------------------------------------------------

        -- Our own products being tracked (seeded from keywords.json + GA4)
        CREATE TABLE IF NOT EXISTS our_products (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            handle        TEXT UNIQUE NOT NULL,
            title         TEXT NOT NULL,
            our_url       TEXT NOT NULL,
            views_90d     INTEGER DEFAULT 0,
            queries_json  TEXT NOT NULL,  -- JSON array of search queries
            created_at    DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        -- Competitor brand/domain registry (one row per domain)
        CREATE TABLE IF NOT EXISTS competitors (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            domain        TEXT UNIQUE NOT NULL,
            name          TEXT,
            first_seen_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at    DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        -- Competitor product URLs (one row per unique URL)
        CREATE TABLE IF NOT EXISTS competitor_products (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            competitor_id   INTEGER NOT NULL REFERENCES competitors(id),
            url             TEXT UNIQUE NOT NULL,
            first_seen_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
            last_scraped_at DATETIME
        );

        -- Weekly SERP snapshot header (one per query per run)
        CREATE TABLE IF NOT EXISTS serp_snapshots (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            our_product_id   INTEGER REFERENCES our_products(id),
            query            TEXT NOT NULL,
            our_url          TEXT,
            our_position     INTEGER,  -- NULL if we don't appear in top 10
            total_results    INTEGER DEFAULT 0,
            scraped_at       DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        -- Individual positions within each snapshot
        CREATE TABLE IF NOT EXISTS serp_snapshot_results (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_id     INTEGER NOT NULL REFERENCES serp_snapshots(id),
            position        INTEGER NOT NULL,
            url             TEXT NOT NULL,
            title           TEXT,
            domain          TEXT,
            competitor_id   INTEGER REFERENCES competitors(id),
            is_ours         INTEGER DEFAULT 0
        );

        -- Weekly product detail snapshots (price + description history)
        CREATE TABLE IF NOT EXISTS product_snapshots (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id           INTEGER NOT NULL REFERENCES competitor_products(id),
            scraped_at           DATETIME DEFAULT CURRENT_TIMESTAMP,
            title                TEXT,
            price_raw            TEXT,
            price_usd            REAL,
            description          TEXT,
            metal_type           TEXT,
            karat                TEXT,
            materials            TEXT,
            availability         TEXT,
            price_changed        INTEGER DEFAULT 0,
            description_changed  INTEGER DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_serp_snapshots_query     ON serp_snapshots(query);
        CREATE INDEX IF NOT EXISTS idx_serp_snapshots_product   ON serp_snapshots(our_product_id);
        CREATE INDEX IF NOT EXISTS idx_product_snapshots_prod   ON product_snapshots(product_id);
        CREATE INDEX IF NOT EXISTS idx_competitor_products_comp ON competitor_products(competitor_id);
    ''')
    conn.commit()
    conn.close()
    print(f'DB initialised at {DB_PATH}')

if __name__ == '__main__':
    init_db()
