"""
delta_alert.py — Weekly competitor delta digest posted to Telegram.

Run automatically after each pipeline.py execution (Step 4b).
Compares the most recent scrape run against the previous run and
posts a digest to the OpenClaw Telegram bot only if changes exist.

Configuration (via environment variables or .env):
    TELEGRAM_BOT_TOKEN  — Telegram bot token (from @BotFather)
    TELEGRAM_CHAT_ID    — Chat/channel ID to post to

    Both must be set for alerts to fire. If either is missing the module
    logs a warning and exits silently so it never blocks the main pipeline.
"""

import os
import sys
import json
import urllib.request
import urllib.error
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import get_conn


# ── Config ───────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

# Alert thresholds
PRICE_CHANGE_PCT   = 0.15   # 15% price movement triggers alert
POSITION_SHIFT     = 3      # ±3 position change triggers alert
TOP_N              = 5      # "top N" for entrant/dropout tracking


# ── Telegram sender ───────────────────────────────────────────────────────────
def send_telegram(message: str) -> bool:
    """POST message to Telegram Bot API. Returns True on success."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[delta_alert] TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set — skipping alert.")
        return False

    url     = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = json.dumps({
        "chat_id":    TELEGRAM_CHAT_ID,
        "text":       message,
        "parse_mode": "HTML",
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
            if result.get("ok"):
                print("[delta_alert] Telegram message sent successfully.")
                return True
            else:
                print(f"[delta_alert] Telegram API error: {result}")
                return False
    except urllib.error.URLError as exc:
        print(f"[delta_alert] Network error posting to Telegram: {exc}")
        return False
    except Exception as exc:
        print(f"[delta_alert] Unexpected error posting to Telegram: {exc}")
        return False


# ── Data queries ──────────────────────────────────────────────────────────────
def get_two_most_recent_run_dates(conn):
    """Return (current_date_str, previous_date_str) for the two most recent scrape runs.
    Returns (None, None) if fewer than two distinct run dates exist."""
    rows = conn.execute("""
        SELECT DATE(scraped_at) AS run_date
        FROM serp_snapshots
        GROUP BY DATE(scraped_at)
        ORDER BY run_date DESC
        LIMIT 2
    """).fetchall()

    if len(rows) < 2:
        return rows[0]["run_date"] if rows else None, None
    return rows[0]["run_date"], rows[1]["run_date"]


def get_top_n_domains_for_run(conn, run_date: str, top_n: int = TOP_N) -> dict:
    """
    Returns {query: set_of_domains_in_top_N} for all snapshots on run_date.
    """
    rows = conn.execute("""
        SELECT ss.query, ssr.domain
        FROM serp_snapshots ss
        JOIN serp_snapshot_results ssr ON ssr.snapshot_id = ss.id
        WHERE DATE(ss.scraped_at) = ?
          AND ssr.is_ours = 0
          AND ssr.position <= ?
          AND ssr.domain IS NOT NULL
          AND ssr.domain != ''
    """, (run_date, top_n)).fetchall()

    result: dict = {}
    for row in rows:
        q = row["query"]
        result.setdefault(q, set()).add(row["domain"])
    return result


def get_our_positions_for_run(conn, run_date: str) -> dict:
    """Returns {query: our_position_or_None} for all snapshots on run_date."""
    rows = conn.execute("""
        SELECT query, our_position
        FROM serp_snapshots
        WHERE DATE(scraped_at) = ?
    """, (run_date,)).fetchall()
    return {row["query"]: row["our_position"] for row in rows}


def get_price_changes(conn, current_date: str, previous_date: str) -> list[dict]:
    """
    Detect competitor product price changes between the two most recent snapshot dates.
    Returns list of dicts with url, old_price, new_price, pct_change.
    """
    # Get latest product snapshot per product for each run date
    rows = conn.execute("""
        SELECT
            cp.url,
            curr.price_usd  AS curr_price,
            prev.price_usd  AS prev_price
        FROM competitor_products cp
        JOIN (
            SELECT product_id, price_usd
            FROM product_snapshots
            WHERE DATE(scraped_at) = ?
              AND price_usd IS NOT NULL AND price_usd > 0
        ) curr ON curr.product_id = cp.id
        JOIN (
            SELECT product_id, price_usd
            FROM product_snapshots
            WHERE DATE(scraped_at) = ?
              AND price_usd IS NOT NULL AND price_usd > 0
        ) prev ON prev.product_id = cp.id
        WHERE ABS(curr.price_usd - prev.price_usd) / prev.price_usd > ?
    """, (current_date, previous_date, PRICE_CHANGE_PCT)).fetchall()

    changes = []
    for row in rows:
        pct = (row["curr_price"] - row["prev_price"]) / row["prev_price"] * 100
        changes.append({
            "url":       row["url"],
            "old_price": row["prev_price"],
            "new_price": row["curr_price"],
            "pct":       pct,
        })
    return changes


# ── Delta detection ───────────────────────────────────────────────────────────
def compute_deltas() -> dict | None:
    """
    Compare the two most recent scrape runs.
    Returns a dict with lists of changes, or None if not enough data.
    """
    try:
        conn = get_conn()
    except Exception as exc:
        print(f"[delta_alert] DB connection failed: {exc}")
        return None

    try:
        current_date, previous_date = get_two_most_recent_run_dates(conn)

        if not previous_date:
            print(f"[delta_alert] Only one run date found ({current_date}). Need two runs to diff. Skipping.")
            conn.close()
            return None

        print(f"[delta_alert] Diffing {previous_date} → {current_date}")

        # Top-N domain sets per query
        curr_top = get_top_n_domains_for_run(conn, current_date)
        prev_top = get_top_n_domains_for_run(conn, previous_date)

        entrants:  list[dict] = []   # new into top-N
        dropouts:  list[dict] = []   # fell out of top-N
        pos_shifts: list[dict] = []  # our position changed ≥ POSITION_SHIFT

        all_queries = set(curr_top.keys()) | set(prev_top.keys())
        for query in sorted(all_queries):
            curr_domains = curr_top.get(query, set())
            prev_domains = prev_top.get(query, set())

            new_in  = curr_domains - prev_domains
            dropped = prev_domains - curr_domains

            for d in new_in:
                entrants.append({"query": query, "domain": d})
            for d in dropped:
                dropouts.append({"query": query, "domain": d})

        # Our position shifts
        curr_positions = get_our_positions_for_run(conn, current_date)
        prev_positions = get_our_positions_for_run(conn, previous_date)

        for query in sorted(set(curr_positions) | set(prev_positions)):
            curr_pos = curr_positions.get(query)
            prev_pos = prev_positions.get(query)
            if curr_pos is None and prev_pos is None:
                continue
            if curr_pos is None or prev_pos is None:
                # Appeared or disappeared from results entirely
                direction = "entered results" if prev_pos is None else "dropped from results"
                pos_shifts.append({
                    "query":    query,
                    "prev_pos": prev_pos,
                    "curr_pos": curr_pos,
                    "direction": direction,
                })
            elif abs(curr_pos - prev_pos) >= POSITION_SHIFT:
                direction = "improved" if curr_pos < prev_pos else "dropped"
                pos_shifts.append({
                    "query":    query,
                    "prev_pos": prev_pos,
                    "curr_pos": curr_pos,
                    "direction": direction,
                })

        # Price changes
        price_changes = get_price_changes(conn, current_date, previous_date)

        conn.close()
        return {
            "current_date":  current_date,
            "previous_date": previous_date,
            "entrants":      entrants,
            "dropouts":      dropouts,
            "pos_shifts":    pos_shifts,
            "price_changes": price_changes,
        }

    except Exception as exc:
        print(f"[delta_alert] Error computing deltas: {exc}")
        try:
            conn.close()
        except Exception:
            pass
        return None


# ── Message formatter ─────────────────────────────────────────────────────────
def _trunc(s: str, n: int = 60) -> str:
    return s if len(s) <= n else s[:n - 1] + "…"


def format_message(deltas: dict) -> str | None:
    """
    Build Telegram message. Returns None if nothing to report (silent run).
    """
    entrants      = deltas["entrants"]
    dropouts      = deltas["dropouts"]
    pos_shifts    = deltas["pos_shifts"]
    price_changes = deltas["price_changes"]
    current_date  = deltas["current_date"]
    previous_date = deltas["previous_date"]

    total_changes = len(entrants) + len(dropouts) + len(pos_shifts) + len(price_changes)
    if total_changes == 0:
        return None  # Nothing to report — stay silent

    lines = [
        f"<b>📊 Weekly Competitor Intel — {current_date}</b>",
        f"<i>vs. {previous_date}</i>",
        "",
    ]

    if entrants:
        lines.append(f"<b>🆕 New top-{TOP_N} entrants ({len(entrants)})</b>")
        for e in entrants[:10]:  # cap at 10 per section
            lines.append(f"  • <code>{e['domain']}</code> on: {_trunc(e['query'], 50)}")
        if len(entrants) > 10:
            lines.append(f"  …and {len(entrants) - 10} more")
        lines.append("")

    if dropouts:
        lines.append(f"<b>👋 Dropped out of top-{TOP_N} ({len(dropouts)})</b>")
        for d in dropouts[:10]:
            lines.append(f"  • <code>{d['domain']}</code> on: {_trunc(d['query'], 50)}")
        if len(dropouts) > 10:
            lines.append(f"  …and {len(dropouts) - 10} more")
        lines.append("")

    if price_changes:
        lines.append(f"<b>💰 Price changes &gt;{int(PRICE_CHANGE_PCT*100)}% ({len(price_changes)})</b>")
        for p in sorted(price_changes, key=lambda x: abs(x["pct"]), reverse=True)[:10]:
            arrow = "⬆️" if p["pct"] > 0 else "⬇️"
            lines.append(
                f"  {arrow} {_trunc(p['url'], 45)} "
                f"${p['old_price']:.0f}→${p['new_price']:.0f} ({p['pct']:+.0f}%)"
            )
        if len(price_changes) > 10:
            lines.append(f"  …and {len(price_changes) - 10} more")
        lines.append("")

    if pos_shifts:
        lines.append(f"<b>📈 Our position shifts ≥{POSITION_SHIFT} ({len(pos_shifts)})</b>")
        for s in pos_shifts[:10]:
            prev = str(s["prev_pos"]) if s["prev_pos"] else "—"
            curr = str(s["curr_pos"]) if s["curr_pos"] else "—"
            dir_icon = "🟢" if s["direction"] in ("improved", "entered results") else "🔴"
            lines.append(f"  {dir_icon} #{prev}→#{curr} — {_trunc(s['query'], 45)}")
        if len(pos_shifts) > 10:
            lines.append(f"  …and {len(pos_shifts) - 10} more")
        lines.append("")

    lines.append(f"<i>Powered by OpenClaw · ellacreationsjewelry.com</i>")
    return "\n".join(lines)


# ── Main entry ────────────────────────────────────────────────────────────────
def run_delta_alert() -> bool:
    """
    Compute deltas, format, and send. Safe to call from pipeline.py.
    Returns True if a message was sent, False otherwise.
    Never raises — catches all exceptions.
    """
    try:
        deltas = compute_deltas()
        if deltas is None:
            return False

        message = format_message(deltas)
        if message is None:
            print("[delta_alert] No changes detected — silent run.")
            return False

        print(f"[delta_alert] Posting digest ({len(message)} chars) to Telegram…")
        return send_telegram(message)

    except Exception as exc:
        print(f"[delta_alert] Unhandled exception — skipping alert: {exc}")
        return False


if __name__ == "__main__":
    success = run_delta_alert()
    sys.exit(0 if success else 1)
