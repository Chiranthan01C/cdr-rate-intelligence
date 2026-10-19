"""Append-only snapshot storage and rate-move detection.

The value of this project compounds with time: one snapshot is a comparison
table anyone could scrape, but a year of snapshots is a record of how the
Australian market actually repriced, and that does not exist publicly.

So the store is append-only. Snapshots are never updated in place, and a rate
move is derived by comparing consecutive snapshots rather than recorded at
write time -- which means a bug in move detection can be fixed and the whole
history recomputed.
"""

from __future__ import annotations

import dataclasses
import sqlite3
from decimal import Decimal
from pathlib import Path

from .normalise import NormalisedRate

SCHEMA = """
CREATE TABLE IF NOT EXISTS rate_snapshot (
    captured_at        TEXT NOT NULL,
    brand_id           TEXT NOT NULL,
    brand_name         TEXT NOT NULL,
    product_id         TEXT NOT NULL,
    product_name       TEXT NOT NULL,
    product_category   TEXT NOT NULL,
    rate_kind          TEXT NOT NULL,
    rate_type          TEXT NOT NULL,
    rate_pct           TEXT,
    comparison_rate_pct TEXT,
    tier_name          TEXT,
    tier_min           TEXT,
    tier_max           TEXT,
    tier_unit          TEXT,
    additional_value   TEXT,
    repayment_type     TEXT,
    loan_purpose       TEXT,
    rate_repaired      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshot_key
    ON rate_snapshot (brand_id, product_id, rate_kind, rate_type, tier_name, captured_at);
CREATE INDEX IF NOT EXISTS idx_snapshot_time ON rate_snapshot (captured_at);

CREATE TABLE IF NOT EXISTS crawl_run (
    captured_at   TEXT PRIMARY KEY,
    holders_total INTEGER NOT NULL,
    holders_ok    INTEGER NOT NULL,
    failures_json TEXT NOT NULL
);
"""


@dataclasses.dataclass(frozen=True, slots=True)
class RateMove:
    brand_name: str
    product_name: str
    rate_kind: str
    rate_type: str
    tier_name: str | None
    previous_pct: Decimal
    current_pct: Decimal

    @property
    def basis_points(self) -> int:
        return int((self.current_pct - self.previous_pct) * 100)


def connect(path: str | Path) -> sqlite3.Connection:
    """Open the snapshot store, creating its directory if it does not exist.

    The default path is ``data/snapshots.sqlite`` and git does not track empty
    directories, so a fresh clone has no ``data/`` and sqlite would fail with
    an opaque "unable to open database file".
    """
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


def write_snapshot(conn: sqlite3.Connection, rows: list[NormalisedRate]) -> int:
    payload = [
        (
            r.captured_at, r.brand_id, r.brand_name, r.product_id, r.product_name,
            r.product_category, r.rate_kind, r.rate_type,
            None if r.rate_pct is None else str(r.rate_pct),
            None if r.comparison_rate_pct is None else str(r.comparison_rate_pct),
            r.tier_name,
            None if r.tier_min is None else str(r.tier_min),
            None if r.tier_max is None else str(r.tier_max),
            r.tier_unit, r.additional_value, r.repayment_type, r.loan_purpose,
            int(r.rate_repaired),
        )
        for r in rows
    ]
    with conn:
        conn.executemany(
            "INSERT INTO rate_snapshot VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", payload
        )
    return len(payload)


def snapshot_dates(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT captured_at FROM rate_snapshot ORDER BY captured_at"
    )]


def detect_moves(
    conn: sqlite3.Connection, previous: str, current: str, *, min_bps: int = 1
) -> list[RateMove]:
    """Rates that changed between two snapshots.

    The join key is (brand, product, rate kind, rate type, tier). Anything that
    appeared or disappeared between snapshots is deliberately excluded: a new
    product is not a repricing, and conflating the two is how rate trackers end
    up reporting phantom cuts.
    """
    query = """
    SELECT p.brand_name, p.product_name, p.rate_kind, p.rate_type, p.tier_name,
           p.rate_pct, c.rate_pct
      FROM rate_snapshot p
      JOIN rate_snapshot c
        ON  p.brand_id   = c.brand_id
        AND p.product_id = c.product_id
        AND p.rate_kind  = c.rate_kind
        AND p.rate_type  = c.rate_type
        AND IFNULL(p.tier_name, '') = IFNULL(c.tier_name, '')
     WHERE p.captured_at = ? AND c.captured_at = ?
       AND p.rate_pct IS NOT NULL AND c.rate_pct IS NOT NULL
       AND p.rate_pct <> c.rate_pct
    """
    moves = []
    for brand, product, kind, rtype, tier, prev, curr in conn.execute(query, (previous, current)):
        move = RateMove(brand, product, kind, rtype, tier, Decimal(prev), Decimal(curr))
        if abs(move.basis_points) >= min_bps:
            moves.append(move)
    return sorted(moves, key=lambda m: abs(m.basis_points), reverse=True)
