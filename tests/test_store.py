import datetime as dt
from decimal import Decimal

from cdrintel.normalise import normalise_product
from cdrintel.store import connect, detect_moves, snapshot_dates, write_snapshot


def product(rate: str, name="Everyday Saver", pid="savings-001"):
    return {
        "productId": pid,
        "name": name,
        "productCategory": "TRANS_AND_SAVINGS_ACCOUNTS",
        "depositRates": [{"depositRateType": "VARIABLE", "rate": rate}],
    }


def snapshot(conn, rate, when, *, pid="savings-001", name="Everyday Saver"):
    rows = normalise_product(
        product(rate, name=name, pid=pid),
        brand_id="b1",
        brand_name="Alex Bank",
        captured_at=when,
    )
    write_snapshot(conn, rows)


T1 = "2026-10-01T00:00:00+00:00"
T2 = "2026-11-01T00:00:00+00:00"


def test_detects_a_cut_in_basis_points():
    conn = connect(":memory:")
    snapshot(conn, "0.0450", T1)
    snapshot(conn, "0.0410", T2)

    moves = detect_moves(conn, T1, T2)
    assert len(moves) == 1
    assert moves[0].basis_points == -40
    assert moves[0].previous_pct == Decimal("4.50")


def test_unchanged_rate_is_not_a_move():
    conn = connect(":memory:")
    snapshot(conn, "0.0450", T1)
    snapshot(conn, "0.0450", T2)
    assert detect_moves(conn, T1, T2) == []


def test_new_product_is_not_reported_as_a_move():
    conn = connect(":memory:")
    snapshot(conn, "0.0450", T1)
    snapshot(conn, "0.0450", T2)
    snapshot(conn, "0.0600", T2, pid="savings-002", name="New Bonus Saver")

    moves = detect_moves(conn, T1, T2)
    assert moves == [], "a product that did not exist last month has not repriced"


def test_min_bps_filters_noise():
    conn = connect(":memory:")
    snapshot(conn, "0.045000", T1)
    snapshot(conn, "0.045040", T2)
    assert detect_moves(conn, T1, T2, min_bps=1) == []


def test_snapshot_dates_are_ordered():
    conn = connect(":memory:")
    snapshot(conn, "0.0450", T2)
    snapshot(conn, "0.0450", T1)
    assert snapshot_dates(conn) == [T1, T2]


def test_store_is_append_only_across_runs():
    conn = connect(":memory:")
    snapshot(conn, "0.0450", T1)
    snapshot(conn, "0.0410", T2)
    total = conn.execute("SELECT COUNT(*) FROM rate_snapshot").fetchone()[0]
    assert total == 2, "a new snapshot must never overwrite the previous one"
