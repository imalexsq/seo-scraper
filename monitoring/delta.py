"""
monitoring/delta.py

Detects changes between the current classifications and the previous run.
Writes new rows to the deltas table when metal_type, karat, or price_usd changes.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from db import get_conn


TRACKED_FIELDS = ['metal_type', 'karat', 'price_usd']


def detect_deltas():
    """Compare current classifications against previous snapshot and log changes."""
    conn = get_conn()

    # Get all classifications with their previous state via self-join on deltas
    rows = conn.execute(
        """
        SELECT c.url, c.metal_type, c.karat, c.price_usd,
               c.classified_at
        FROM classifications c
        ORDER BY c.classified_at DESC
        """
    ).fetchall()

    new_deltas = 0
    for row in rows:
        url = row['url']
        for field in TRACKED_FIELDS:
            current_val = str(row[field]) if row[field] is not None else None

            # Get last recorded value for this field
            last = conn.execute(
                """
                SELECT new_value FROM deltas
                WHERE url = ? AND field = ?
                ORDER BY detected_at DESC LIMIT 1
                """,
                (url, field),
            ).fetchone()

            if last is None:
                # First time seeing this URL — record as baseline, no delta
                conn.execute(
                    "INSERT INTO deltas (url, field, old_value, new_value) VALUES (?, ?, ?, ?)",
                    (url, field, None, current_val),
                )
                new_deltas += 1
            elif last['new_value'] != current_val:
                # Value changed — record delta
                conn.execute(
                    "INSERT INTO deltas (url, field, old_value, new_value) VALUES (?, ?, ?, ?)",
                    (url, field, last['new_value'], current_val),
                )
                new_deltas += 1
                print(f'  CHANGE [{field}] {url[:60]}')
                print(f'         {last["new_value"]} -> {current_val}')

    conn.commit()
    conn.close()
    print(f'Delta check complete. {new_deltas} new delta records.')
    return new_deltas


if __name__ == '__main__':
    detect_deltas()
