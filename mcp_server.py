"""
seo-scraper MCP server — OAuth 2.0 + HTTPS (Cloudflare-proxied)

Public URL: https://scraper.ellacreationsjewelry.com/mcp
Auth:       OAuth 2.0 with PKCE — Claude Desktop compatible
Transport:  streamable-http, stateless

Setup steps (one-time):
  1. In Cloudflare DNS: add A record  scraper → 65.109.136.20  (Proxied / orange cloud)
  2. On Hetzner systemd unit: set MCP_PORT=80, MCP_PUBLIC_URL=https://scraper.ellacreationsjewelry.com
  3. systemctl daemon-reload && systemctl restart seo-scraper-mcp

Claude Desktop config  (~/.claude/claude_code_config.json or Cowork MCP settings):
  {
    "seo_scraper": {
      "type": "url",
      "url": "https://scraper.ellacreationsjewelry.com/mcp"
    }
  }
  No headers needed — Claude Desktop will run the OAuth flow automatically.

Environment variables:
  MCP_PUBLIC_URL   Public HTTPS base URL  (required for OAuth to work)
  MCP_PORT         Port to listen on (default 80, served behind Cloudflare proxy)
  SCRAPER_DB       Path to scraper SQLite DB (default /root/seo-scraper/data/scraper.db)
  MCP_OAUTH_DB     Path to OAuth SQLite DB  (default /root/seo-scraper/data/oauth.db)
  PIPELINE_PY      Path to pipeline.py
  VENV_PYTHON      Path to venv python3
"""

import json
import os
import secrets
import sqlite3
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional, Union
from urllib.parse import urlencode

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
)
from mcp.server.auth.settings import (
    AuthSettings,
    ClientRegistrationOptions,
    RevocationOptions,
)
from mcp.server.fastmcp import FastMCP, Context
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DB_PATH     = Path(os.environ.get("SCRAPER_DB",  "/root/seo-scraper/data/scraper.db"))
OAUTH_DB    = Path(os.environ.get("MCP_OAUTH_DB", "/root/seo-scraper/data/oauth.db"))
PIPELINE_PY = Path(os.environ.get("PIPELINE_PY", "/root/seo-scraper/pipeline.py"))
VENV_PYTHON = Path(os.environ.get("VENV_PYTHON", "/root/seo-scraper/.venv/bin/python3"))
PORT        = int(os.environ.get("MCP_PORT", "80"))
PUBLIC_URL  = os.environ.get("MCP_PUBLIC_URL", "https://scraper.ellacreationsjewelry.com")

# Strip trailing slash
PUBLIC_URL = PUBLIC_URL.rstrip("/")


# ---------------------------------------------------------------------------
# OAuth provider — SQLite-backed, auto-approve (private single-user server)
# ---------------------------------------------------------------------------

class SingleUserOAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    """
    OAuth 2.0 Authorization Server backed by SQLite.

    Auto-approves every authorization request — this is a private server
    accessible only to authorised users (Cloudflare can add IP allow-list
    for extra security if needed).

    Supports:
      - Dynamic client registration (RFC 7591)
      - Authorization code flow with PKCE S256 (RFC 7636)
      - Refresh token rotation
      - Token revocation (RFC 7009)
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._init_db()

    # ---- DB setup ----------------------------------------------------------

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS oauth_clients (
                    client_id   TEXT PRIMARY KEY,
                    client_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oauth_codes (
                    code                          TEXT PRIMARY KEY,
                    client_id                     TEXT NOT NULL,
                    scopes                        TEXT NOT NULL,
                    expires_at                    REAL NOT NULL,
                    code_challenge                TEXT NOT NULL,
                    redirect_uri                  TEXT NOT NULL,
                    redirect_uri_provided_explicitly INTEGER NOT NULL,
                    used                          INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS oauth_access_tokens (
                    token      TEXT PRIMARY KEY,
                    client_id  TEXT NOT NULL,
                    scopes     TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oauth_refresh_tokens (
                    token      TEXT PRIMARY KEY,
                    client_id  TEXT NOT NULL,
                    scopes     TEXT NOT NULL,
                    revoked    INTEGER NOT NULL DEFAULT 0
                );
            """)

    def _db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # ---- Client registration -----------------------------------------------

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        """Store a newly registered client (client_id assigned by the framework)."""
        with self._db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO oauth_clients (client_id, client_json) VALUES (?, ?)",
                (client_info.client_id, client_info.model_dump_json()),
            )

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        with self._db() as conn:
            row = conn.execute(
                "SELECT client_json FROM oauth_clients WHERE client_id = ?",
                (client_id,),
            ).fetchone()
        if not row:
            return None
        return OAuthClientInformationFull.model_validate_json(row["client_json"])

    # ---- Authorization (auto-approve) --------------------------------------

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
    ) -> str:
        """
        Auto-approve: immediately generate an authorization code and return
        the redirect URL to the client's callback URI.

        No user interaction required — this is a private server.
        """
        code = secrets.token_urlsafe(32)
        expires_at = time.time() + 600  # 10-minute window to exchange

        with self._db() as conn:
            conn.execute(
                """
                INSERT INTO oauth_codes
                    (code, client_id, scopes, expires_at, code_challenge,
                     redirect_uri, redirect_uri_provided_explicitly)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    code,
                    client.client_id,
                    json.dumps(params.scopes or []),
                    expires_at,
                    params.code_challenge,
                    str(params.redirect_uri),
                    1 if params.redirect_uri_provided_explicitly else 0,
                ),
            )

        # Build redirect URL: send code back to the client's registered callback
        qs = {"code": code}
        if params.state:
            qs["state"] = params.state

        base = str(params.redirect_uri)
        sep  = "&" if "?" in base else "?"
        return f"{base}{sep}{urlencode(qs)}"

    # ---- Authorization code ------------------------------------------------

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> AuthorizationCode | None:
        with self._db() as conn:
            row = conn.execute(
                """
                SELECT * FROM oauth_codes
                WHERE code = ? AND client_id = ? AND used = 0
                """,
                (authorization_code, client.client_id),
            ).fetchone()

        if not row:
            return None

        return AuthorizationCode(
            code=row["code"],
            client_id=row["client_id"],
            scopes=json.loads(row["scopes"]),
            expires_at=row["expires_at"],
            code_challenge=row["code_challenge"],
            redirect_uri=row["redirect_uri"],
            redirect_uri_provided_explicitly=bool(row["redirect_uri_provided_explicitly"]),
        )

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        """
        Issue access + refresh tokens.
        PKCE verification is done by the framework before this is called.
        """
        # Mark code as used (prevent replay)
        with self._db() as conn:
            conn.execute(
                "UPDATE oauth_codes SET used = 1 WHERE code = ?",
                (authorization_code.code,),
            )

        access_token  = secrets.token_urlsafe(48)
        refresh_token = secrets.token_urlsafe(48)
        scopes_json   = json.dumps(authorization_code.scopes)

        with self._db() as conn:
            conn.execute(
                "INSERT INTO oauth_access_tokens  (token, client_id, scopes) VALUES (?, ?, ?)",
                (access_token, client.client_id, scopes_json),
            )
            conn.execute(
                "INSERT INTO oauth_refresh_tokens (token, client_id, scopes) VALUES (?, ?, ?)",
                (refresh_token, client.client_id, scopes_json),
            )

        return OAuthToken(
            access_token=access_token,
            token_type="Bearer",
            refresh_token=refresh_token,
            scope=" ".join(authorization_code.scopes),
        )

    # ---- Access token verification ------------------------------------------

    async def load_access_token(self, token: str) -> AccessToken | None:
        with self._db() as conn:
            row = conn.execute(
                "SELECT client_id, scopes FROM oauth_access_tokens WHERE token = ?",
                (token,),
            ).fetchone()

        if not row:
            return None

        return AccessToken(
            token=token,
            client_id=row["client_id"],
            scopes=json.loads(row["scopes"]),
        )

    # ---- Refresh token ------------------------------------------------------

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> RefreshToken | None:
        with self._db() as conn:
            row = conn.execute(
                """
                SELECT client_id, scopes FROM oauth_refresh_tokens
                WHERE token = ? AND client_id = ? AND revoked = 0
                """,
                (refresh_token, client.client_id),
            ).fetchone()

        if not row:
            return None

        return RefreshToken(
            token=refresh_token,
            client_id=row["client_id"],
            scopes=json.loads(row["scopes"]),
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        """Issue a new access token. Keep the same refresh token."""
        effective_scopes = scopes or refresh_token.scopes
        new_access_token = secrets.token_urlsafe(48)

        with self._db() as conn:
            conn.execute(
                "INSERT INTO oauth_access_tokens (token, client_id, scopes) VALUES (?, ?, ?)",
                (new_access_token, client.client_id, json.dumps(effective_scopes)),
            )

        return OAuthToken(
            access_token=new_access_token,
            token_type="Bearer",
            refresh_token=refresh_token.token,   # same refresh token
            scope=" ".join(effective_scopes),
        )

    # ---- Revocation ---------------------------------------------------------

    async def revoke_token(
        self,
        token: Union[AccessToken, RefreshToken],
    ) -> None:
        with self._db() as conn:
            if isinstance(token, RefreshToken):
                conn.execute(
                    "UPDATE oauth_refresh_tokens SET revoked = 1 WHERE token = ?",
                    (token.token,),
                )
            else:
                conn.execute(
                    "DELETE FROM oauth_access_tokens WHERE token = ?",
                    (token.token,),
                )


# ---------------------------------------------------------------------------
# Server init
# ---------------------------------------------------------------------------

oauth_provider = SingleUserOAuthProvider(OAUTH_DB)

auth_settings = AuthSettings(
    issuer_url=AnyHttpUrl(PUBLIC_URL),
    resource_server_url=AnyHttpUrl(PUBLIC_URL),
    client_registration_options=ClientRegistrationOptions(
        enabled=True,
        valid_scopes=["mcp"],
        default_scopes=["mcp"],
    ),
    revocation_options=RevocationOptions(enabled=True),
)


@asynccontextmanager
async def lifespan(server):
    if not DB_PATH.exists():
        raise RuntimeError(f"Scraper DB not found at {DB_PATH}. Run pipeline.py first.")
    yield


mcp = FastMCP(
    "seo_scraper_mcp",
    lifespan=lifespan,
    host="0.0.0.0",
    port=PORT,
    stateless_http=True,
    auth_server_provider=oauth_provider,
    auth=auth_settings,
)


# ---------------------------------------------------------------------------
# DB helpers (scraper DB — separate from OAuth DB)
# ---------------------------------------------------------------------------

def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _scalar(sql: str, params: tuple = ()) -> int | None:
    with _db() as conn:
        row = conn.execute(sql, params).fetchone()
        return row[0] if row else None


# ---------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------

class KeywordsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    keywords: list[str] = Field(
        ...,
        description="List of search queries to look up (e.g. ['18k solid gold turquoise bracelet', 'october birthstone ring'])",
        min_length=1,
        max_length=10,
    )
    limit: Optional[int] = Field(
        default=15,
        description="Max competitor results to return per set of keywords (1–50)",
        ge=1, le=50,
    )


class UrlInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    url: str = Field(
        ...,
        description="Full product page URL to look up (e.g. 'https://cateandchloe.com/products/...')",
        min_length=10,
    )


class QueryInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    query: str = Field(
        ...,
        description="Exact SERP search query to look up (e.g. '18k solid gold turquoise bracelet')",
        min_length=2,
    )
    limit: Optional[int] = Field(default=20, ge=1, le=100)
    offset: Optional[int] = Field(default=0, ge=0)


class RunPipelineInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    keywords: Optional[list[str]] = Field(
        default=None,
        description="Keywords to scrape. If omitted, uses the existing keywords.json on the server.",
        max_length=20,
    )
    classify_limit: Optional[int] = Field(
        default=50,
        description="Max product pages to classify in this run (1–200)",
        ge=1, le=200,
    )


# ---------------------------------------------------------------------------
# Tool: get_stats
# ---------------------------------------------------------------------------

@mcp.tool(
    name="scraper_get_stats",
    annotations={
        "title": "Get Scraper DB Stats",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def scraper_get_stats() -> str:
    """Get overall statistics from the seo-scraper database.

    Returns row counts, last classification timestamp, and metal type breakdown.
    Use this first to understand what data is available before running queries.

    Returns:
        str: JSON with db_path, row counts, last_classified_at, metal_breakdown, keywords_covered
    """
    with _db() as conn:
        serp_n     = conn.execute("SELECT COUNT(*) FROM serp_results").fetchone()[0]
        pages_n    = conn.execute("SELECT COUNT(*) FROM product_pages").fetchone()[0]
        class_n    = conn.execute("SELECT COUNT(*) FROM classifications").fetchone()[0]
        last_ts    = conn.execute(
            "SELECT MAX(classified_at) FROM classifications"
        ).fetchone()[0]

        metal_rows = conn.execute(
            "SELECT metal_type, COUNT(*) as n FROM classifications GROUP BY metal_type ORDER BY n DESC"
        ).fetchall()

        queries    = conn.execute(
            "SELECT DISTINCT query FROM serp_results ORDER BY query"
        ).fetchall()

    breakdown = {r["metal_type"]: r["n"] for r in metal_rows}
    keywords  = [r["query"] for r in queries]

    result = {
        "db_path":         str(DB_PATH),
        "serp_results":    serp_n,
        "product_pages":   pages_n,
        "classifications": class_n,
        "last_classified_at": last_ts,
        "metal_breakdown": breakdown,
        "keywords_covered": keywords,
        "keywords_count":  len(keywords),
    }
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Tool: get_competitor_intel
# ---------------------------------------------------------------------------

@mcp.tool(
    name="scraper_get_competitor_intel",
    annotations={
        "title": "Get Competitor Intelligence",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def scraper_get_competitor_intel(params: KeywordsInput) -> str:
    """Get ranked competitor data for a set of search keywords.

    For each keyword, returns competitors found in Google SERP results with their
    classified metal type, karat, price, and evidence text. Use this to understand
    what you're competing against for specific product categories.

    Args:
        params (KeywordsInput):
            - keywords (list[str]): Search queries to look up
            - limit (int): Max results to return (default 15)

    Returns:
        str: JSON with:
            - keywords_searched: list of queried keywords
            - keywords_found: keywords that had data
            - keywords_missing: keywords with no data (run pipeline to add them)
            - competitors: list of {position, url, page_title, query,
                           metal_type, karat, base_metal, price_usd, evidence, confidence}
            - metal_summary: {solid_gold: N, vermeil: N, gold_plated: N, ...}
            - insight: one-line competitive insight string
    """
    keywords = params.keywords
    placeholders = ",".join("?" * len(keywords))

    with _db() as conn:
        rows = conn.execute(f"""
            SELECT
                s.query, s.position, s.url, s.title AS serp_title,
                p.page_title,
                c.metal_type, c.karat, c.base_metal, c.price_usd,
                c.evidence, c.confidence
            FROM serp_results s
            LEFT JOIN product_pages p ON s.url = p.url
            LEFT JOIN classifications c ON s.url = c.url
            WHERE s.query IN ({placeholders})
            ORDER BY s.query, s.position ASC
            LIMIT ?
        """, (*keywords, params.limit)).fetchall()

        covered = {r["query"] for r in rows}

    missing  = [k for k in keywords if k not in covered]
    found    = [k for k in keywords if k in covered]

    competitors = []
    from collections import Counter
    metal_counter: Counter = Counter()

    for r in rows:
        metal = r["metal_type"] or "unknown"
        metal_counter[metal] += 1
        competitors.append({
            "query":      r["query"],
            "position":   r["position"],
            "url":        r["url"],
            "page_title": r["page_title"] or r["serp_title"] or "",
            "metal_type": metal,
            "karat":      r["karat"] or "unknown",
            "base_metal": r["base_metal"] or "unknown",
            "price_usd":  r["price_usd"],
            "evidence":   (r["evidence"] or "")[:120],
            "confidence": r["confidence"] or "unknown",
        })

    solid   = metal_counter.get("solid_gold", 0)
    plated  = metal_counter.get("gold_plated", 0) + metal_counter.get("gold_filled", 0)
    vermeil = metal_counter.get("vermeil", 0)
    total   = len(competitors)

    if total == 0:
        insight = "No competitor data found for these keywords. Run scraper_run_pipeline to collect data."
    elif solid == 0:
        insight = f"Zero solid gold competitors in top results. Lead hard on '18k Solid Gold' — {plated} plated and {vermeil} vermeil rank above you."
    elif solid <= total * 0.25:
        insight = f"Only {solid}/{total} solid gold competitors. Strong differentiation opportunity on material quality."
    else:
        insight = f"{solid}/{total} competitors are solid gold — differentiate on designer credentials, handcrafted, Nevada-made."

    result = {
        "keywords_searched": keywords,
        "keywords_found":    found,
        "keywords_missing":  missing,
        "total_results":     len(competitors),
        "competitors":       competitors,
        "metal_summary":     dict(metal_counter.most_common()),
        "insight":           insight,
    }
    if missing:
        result["hint"] = f"Run scraper_run_pipeline with keywords={missing} to collect data for missing keywords."

    return json.dumps(result, indent=2, default=str)


# ---------------------------------------------------------------------------
# Tool: get_metal_breakdown
# ---------------------------------------------------------------------------

@mcp.tool(
    name="scraper_get_metal_breakdown",
    annotations={
        "title": "Get SERP Metal Breakdown",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def scraper_get_metal_breakdown(params: KeywordsInput) -> str:
    """Get a percentage breakdown of metal types ranking in Google for given keywords.

    Useful for quickly assessing how dominated a keyword is by plated/vermeil vs solid gold.
    Helps prioritise which product pages to optimise and what differentiation angle to use.

    Args:
        params (KeywordsInput):
            - keywords (list[str]): Queries to analyse
            - limit (int): Max competitor results to include (default 15)

    Returns:
        str: JSON with percentages, raw counts, per-query breakdown, and a plain-English summary
    """
    placeholders = ",".join("?" * len(params.keywords))

    with _db() as conn:
        rows = conn.execute(f"""
            SELECT s.query, c.metal_type, c.karat, c.price_usd
            FROM serp_results s
            LEFT JOIN classifications c ON s.url = c.url
            WHERE s.query IN ({placeholders})
            ORDER BY s.query, s.position
            LIMIT ?
        """, (*params.keywords, params.limit)).fetchall()

    from collections import Counter, defaultdict
    overall: Counter = Counter()
    per_query: dict = defaultdict(Counter)

    for r in rows:
        metal = r["metal_type"] or "unknown"
        overall[metal] += 1
        per_query[r["query"]][metal] += 1

    total = sum(overall.values())
    pct = {k: round(v / total * 100, 1) for k, v in overall.most_common()} if total else {}

    solid_pct   = pct.get("solid_gold", 0)
    plated_pct  = pct.get("gold_plated", 0) + pct.get("gold_filled", 0)
    vermeil_pct = pct.get("vermeil", 0)

    if total == 0:
        summary = "No data for these keywords yet."
    elif solid_pct == 0:
        summary = "0% solid gold in top results across all keywords. Massive opening — every competitor is plated or vermeil."
    elif solid_pct < 25:
        summary = f"Only {solid_pct}% solid gold. {plated_pct + vermeil_pct}% is plated/vermeil. Strong differentiation opportunity."
    elif solid_pct < 50:
        summary = f"{solid_pct}% solid gold competitors. Still meaningful differentiation available — focus on price-quality story."
    else:
        summary = f"{solid_pct}% solid gold competitors. Market is competitive on material. Differentiate on craft, credentials, price-per-karat."

    per_query_clean = {
        q: {m: c for m, c in cnt.most_common()}
        for q, cnt in per_query.items()
    }

    result = {
        "keywords": params.keywords,
        "total_results_analysed": total,
        "overall_percentages": pct,
        "overall_counts": dict(overall.most_common()),
        "per_query_breakdown": per_query_clean,
        "summary": summary,
    }
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Tool: list_serp_results
# ---------------------------------------------------------------------------

@mcp.tool(
    name="scraper_list_serp_results",
    annotations={
        "title": "List SERP Results",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def scraper_list_serp_results(params: QueryInput) -> str:
    """List raw Google SERP results for a specific search query.

    Returns all crawled results for the query including position, URL, title, and
    whether the page has been classified yet. Use this to see exactly what Google
    is returning for a keyword before diving into metal type analysis.

    Args:
        params (QueryInput):
            - query (str): The exact search query
            - limit (int): Max results (default 20)
            - offset (int): Pagination offset (default 0)

    Returns:
        str: JSON with query, total, results list (position, url, title, classified, metal_type)
    """
    with _db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM serp_results WHERE query = ?", (params.query,)
        ).fetchone()[0]

        rows = conn.execute("""
            SELECT s.position, s.url, s.title, s.snippet,
                   c.metal_type, c.karat, c.confidence
            FROM serp_results s
            LEFT JOIN classifications c ON s.url = c.url
            WHERE s.query = ?
            ORDER BY s.position ASC
            LIMIT ? OFFSET ?
        """, (params.query, params.limit, params.offset)).fetchall()

    if total == 0:
        similar = []
        with _db() as conn:
            similar = [
                r["query"] for r in conn.execute(
                    "SELECT DISTINCT query FROM serp_results WHERE query LIKE ? LIMIT 5",
                    (f"%{params.query.split()[0]}%",)
                ).fetchall()
            ]
        return json.dumps({
            "error": f"No SERP results found for '{params.query}'",
            "similar_queries": similar,
            "hint": "Use scraper_get_stats to see all available queries.",
        })

    results = []
    for r in rows:
        results.append({
            "position":   r["position"],
            "url":        r["url"],
            "title":      r["title"] or "",
            "snippet":    (r["snippet"] or "")[:200],
            "classified": r["metal_type"] is not None,
            "metal_type": r["metal_type"] or "not classified",
            "karat":      r["karat"] or "",
            "confidence": r["confidence"] or "",
        })

    return json.dumps({
        "query":    params.query,
        "total":    total,
        "count":    len(results),
        "offset":   params.offset,
        "has_more": total > params.offset + len(results),
        "results":  results,
    }, indent=2)


# ---------------------------------------------------------------------------
# Tool: get_product_classification
# ---------------------------------------------------------------------------

@mcp.tool(
    name="scraper_get_product_classification",
    annotations={
        "title": "Get Product Classification",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def scraper_get_product_classification(params: UrlInput) -> str:
    """Get the metal type classification for a specific competitor product URL.

    Returns the full classification record including metal_type, karat, base_metal,
    price, evidence quote, and confidence level. Also shows which SERP queries
    this URL appeared in and at what position.

    Args:
        params (UrlInput):
            - url (str): Full product page URL

    Returns:
        str: JSON with classification details and SERP context, or error if not found
    """
    with _db() as conn:
        cls_row = conn.execute("""
            SELECT c.metal_type, c.karat, c.base_metal, c.price_usd,
                   c.evidence, c.confidence, c.classified_at,
                   p.page_title
            FROM classifications c
            LEFT JOIN product_pages p ON c.url = p.url
            WHERE c.url = ?
        """, (params.url,)).fetchone()

        serp_rows = conn.execute("""
            SELECT query, position, title
            FROM serp_results
            WHERE url = ?
            ORDER BY position ASC
        """, (params.url,)).fetchall()

    if not cls_row:
        with _db() as conn:
            serp_only = conn.execute(
                "SELECT query, position FROM serp_results WHERE url = ? LIMIT 5",
                (params.url,)
            ).fetchall()

        if serp_only:
            return json.dumps({
                "url": params.url,
                "status": "in_serp_not_classified",
                "serp_appearances": [dict(r) for r in serp_only],
                "hint": "This URL appeared in SERP results but hasn't been classified yet. Run scraper_run_pipeline to classify it.",
            })

        return json.dumps({
            "url": params.url,
            "status": "not_found",
            "hint": "URL not in database. It may not have appeared in SERP results for any tracked keywords.",
        })

    return json.dumps({
        "url":            params.url,
        "status":         "classified",
        "page_title":     cls_row["page_title"] or "",
        "metal_type":     cls_row["metal_type"],
        "karat":          cls_row["karat"],
        "base_metal":     cls_row["base_metal"],
        "price_usd":      cls_row["price_usd"],
        "evidence":       cls_row["evidence"],
        "confidence":     cls_row["confidence"],
        "classified_at":  cls_row["classified_at"],
        "serp_appearances": [
            {"query": r["query"], "position": r["position"], "title": r["title"]}
            for r in serp_rows
        ],
    }, indent=2, default=str)


# ---------------------------------------------------------------------------
# Tool: get_delta_changes
# ---------------------------------------------------------------------------

@mcp.tool(
    name="scraper_get_delta_changes",
    annotations={
        "title": "Get Competitor Changes",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def scraper_get_delta_changes() -> str:
    """Get all tracked changes in competitor metal types and prices over time.

    Shows any competitor that changed metal_type, karat, or price_usd between
    scraper runs. Useful for monitoring competitor reformulations or price updates.

    Returns:
        str: JSON with list of {url, field, old_value, new_value, detected_at}
             or a message if no changes detected yet.
    """
    with _db() as conn:
        try:
            rows = conn.execute("""
                SELECT d.url, d.field, d.old_value, d.new_value, d.detected_at,
                       p.page_title
                FROM deltas d
                LEFT JOIN product_pages p ON d.url = p.url
                ORDER BY d.detected_at DESC
                LIMIT 100
            """).fetchall()
        except sqlite3.OperationalError:
            return json.dumps({
                "status": "no_delta_table",
                "message": "Delta tracking not yet set up. Run pipeline.py at least twice to begin tracking changes.",
            })

    if not rows:
        return json.dumps({
            "status": "no_changes",
            "message": "No competitor changes detected yet. Deltas are tracked after each weekly scraper run.",
        })

    changes = []
    for r in rows:
        changes.append({
            "url":         r["url"],
            "page_title":  r["page_title"] or "",
            "field":       r["field"],
            "old_value":   r["old_value"],
            "new_value":   r["new_value"],
            "detected_at": r["detected_at"],
        })

    return json.dumps({
        "total_changes": len(changes),
        "changes":       changes,
    }, indent=2, default=str)


# ---------------------------------------------------------------------------
# Tool: run_pipeline
# ---------------------------------------------------------------------------

_pipeline_lock    = threading.Lock()
_pipeline_running = False


@mcp.tool(
    name="scraper_run_pipeline",
    annotations={
        "title": "Run Scraper Pipeline",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def scraper_run_pipeline(params: RunPipelineInput) -> str:
    """Trigger a fresh competitor scrape run on the Hetzner server.

    Runs the seo-scraper pipeline in the background: fetches Google SERP results
    for given keywords, crawls product pages, and classifies metal types.
    This is non-blocking — it returns immediately and the run happens asynchronously.

    Only one pipeline run can be active at a time. Check scraper_get_stats after
    a few minutes to see updated results.

    Args:
        params (RunPipelineInput):
            - keywords (list[str]|None): Keywords to scrape. Omit to use existing keywords.json
            - classify_limit (int): Max pages to classify (default 50)

    Returns:
        str: JSON with status ('started', 'already_running', 'error') and details
    """
    global _pipeline_running

    if not PIPELINE_PY.exists():
        return json.dumps({
            "status": "error",
            "message": f"pipeline.py not found at {PIPELINE_PY}",
        })

    if not _pipeline_lock.acquire(blocking=False):
        return json.dumps({
            "status": "already_running",
            "message": "A pipeline run is already in progress. Check scraper_get_stats in a few minutes.",
        })

    env_path = Path("/root/seo-scraper/.env")
    if not env_path.exists():
        _pipeline_lock.release()
        return json.dumps({"status": "error", "message": ".env file not found on server"})

    keywords_arg = []
    if params.keywords:
        tmp_keywords = Path("/root/seo-scraper/data/keywords_tmp.json")
        keywords_json = []
        for kw in params.keywords:
            keywords_json.append({"product": "custom", "queries": [kw]})
        tmp_keywords.write_text(json.dumps(keywords_json, indent=2))
        keywords_arg = ["--keywords", str(tmp_keywords)]

    cmd = [
        str(VENV_PYTHON), str(PIPELINE_PY),
        "--classify-limit", str(params.classify_limit),
    ] + keywords_arg

    def _run():
        global _pipeline_running
        try:
            env = os.environ.copy()
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    env[k.strip()] = v.strip()
            subprocess.run(
                cmd,
                cwd="/root/seo-scraper",
                env=env,
                capture_output=True,
                timeout=600,
            )
        except Exception:
            pass
        finally:
            _pipeline_lock.release()

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    started_at = datetime.utcnow().isoformat() + "Z"
    return json.dumps({
        "status":     "started",
        "started_at": started_at,
        "command":    " ".join(cmd),
        "keywords":   params.keywords or "using existing keywords.json",
        "message":    "Pipeline running in background. Check scraper_get_stats in 2–5 minutes for updated results.",
    }, indent=2)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(f"Starting seo_scraper_mcp on port {PORT}")
    print(f"Public URL: {PUBLIC_URL}")
    print(f"Scraper DB: {DB_PATH}")
    print(f"OAuth DB:   {OAUTH_DB}")
    print(f"OAuth: enabled (auto-approve, PKCE required)")
    mcp.run(transport="streamable-http")
