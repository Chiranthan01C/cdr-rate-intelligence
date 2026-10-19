"""Command line entry point.

    python -m cdrintel crawl      # discover, fetch, normalise, store a snapshot
    python -m cdrintel score      # per-holder data quality scorecard
    python -m cdrintel moves      # rate changes between the last two snapshots
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from . import crawl as crawl_mod
from . import store as store_mod
from .normalise import normalise_product
from .quality import score_holder
from .register import fetch_holders

DEFAULT_DB = "data/snapshots.sqlite"


def cmd_crawl(args) -> int:
    holders = fetch_holders("banking")
    print(f"register: {len(holders)} banking brands", file=sys.stderr)
    if args.limit:
        holders = holders[: args.limit]

    results, captured_at = asyncio.run(crawl_mod.crawl(holders))

    rows = []
    for result in results:
        for product in result.details:
            rows.extend(
                normalise_product(
                    product,
                    brand_id=result.holder.brand_id,
                    brand_name=result.holder.brand_name,
                    captured_at=captured_at,
                )
            )

    conn = store_mod.connect(args.db)
    written = store_mod.write_snapshot(conn, rows)
    store_mod.write_holder_snapshot(
        conn,
        [
            (
                captured_at,
                r.holder.brand_id,
                r.holder.brand_name,
                len(r.summaries),
                len(r.details),
                r.detail_failures,
                r.error,
            )
            for r in results
        ],
    )
    failures = {r.holder.brand_name: r.error for r in results if not r.ok}
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO crawl_run VALUES (?,?,?,?)",
            (captured_at, len(results), len(results) - len(failures), json.dumps(failures)),
        )

    products = sum(len(r.details) for r in results)
    print(
        f"snapshot {captured_at}: {written} rate rows from {products} products "
        f"across {len(results) - len(failures)} holders"
    )
    if written == 0 and products:
        print(
            "WARNING: products fetched but no rates parsed - the schema assumption is wrong",
            file=sys.stderr,
        )
    if failures:
        print(f"{len(failures)} holders failed; see crawl_run.failures_json", file=sys.stderr)
    detail_fails = sum(r.detail_failures for r in results)
    if detail_fails:
        print(f"{detail_fails} individual product details could not be fetched", file=sys.stderr)
    return 0


def cmd_score(args) -> int:
    from decimal import Decimal

    from .normalise import NormalisedRate

    conn = store_mod.connect(args.db)
    dates = store_mod.snapshot_dates(conn)
    if not dates:
        print("no snapshots yet - run `crawl` first", file=sys.stderr)
        return 1

    latest = dates[-1]
    rows_by_brand: dict[str, list] = {}
    cursor = conn.execute(
        "SELECT brand_id, brand_name, product_id, rate_kind, rate_pct,"
        " comparison_rate_pct, tier_name, rate_repaired"
        " FROM rate_snapshot WHERE captured_at = ?",
        (latest,),
    )
    for bid, bname, pid, kind, rate, comp, tier, repaired in cursor:
        rows_by_brand.setdefault(bid, []).append(
            NormalisedRate(
                brand_id=bid, brand_name=bname, product_id=pid, product_name="",
                product_category="", rate_kind=kind, rate_type="",
                rate_pct=None if rate is None else Decimal(rate),
                comparison_rate_pct=None if comp is None else Decimal(comp),
                tier_name=tier, tier_min=None, tier_max=None, tier_unit=None,
                additional_value=None, repayment_type=None, loan_purpose=None,
                rate_repaired=bool(repaired), captured_at=latest,
            )
        )

    # Drive the report off every holder CRAWLED, not off the holders that
    # produced rows. A holder serving nothing is the most interesting row in
    # a data quality report and must never be omitted for having no data.
    holders = store_mod.holders_at(conn, latest)
    if not holders:
        print(
            "this snapshot predates per-holder recording - re-run `crawl`",
            file=sys.stderr,
        )
        return 1

    scored = []
    for bid, bname, listed, fetched, dfails, err in holders:
        rows = rows_by_brand.get(bid, [])
        scored.append((
            listed,
            score_holder(
                bid, bname, rows,
                product_ids_seen={r.product_id for r in rows} if rows else set(),
                details_fetched=fetched,
                detail_failures=dfails,
                crawl_error=err,
            ),
        ))

    # Worst first: silent holders at the top, then by conformance.
    scored.sort(key=lambda pair: (pair[1].rate_rows > 0, pair[1].conformance,
                                  pair[1].completeness))

    print(f"{'brand':30} {'prod':>5} {'rates':>6} {'compl':>7} {'conf':>6}  note")
    for listed, score in scored:
        print(
            f"{score.brand_name[:30]:30} {listed:5d} {score.rate_rows:6d} "
            f"{score.completeness:7.1%} {score.conformance:6.1%}  {score.note}"
        )
    scores = [score for _listed, score in scored]
    silent = [s_ for s_ in scores if s_.rate_rows == 0]
    if silent:
        print(
            f"\n{len(silent)} of {len(scores)} holders published no usable rates.",
            file=sys.stderr,
        )
    return 0


def cmd_moves(args) -> int:
    conn = store_mod.connect(args.db)
    dates = store_mod.snapshot_dates(conn)
    if len(dates) < 2:
        print("need at least two snapshots to compare", file=sys.stderr)
        return 1

    moves = store_mod.detect_moves(conn, dates[-2], dates[-1], min_bps=args.min_bps)
    print(f"{len(moves)} rate moves between {dates[-2][:10]} and {dates[-1][:10]}\n")
    for move in moves[: args.top]:
        arrow = "up" if move.basis_points > 0 else "down"
        print(
            f"{move.basis_points:+5d}bp {arrow:4} {move.brand_name[:24]:24} "
            f"{move.product_name[:34]:34} {move.rate_type}"
        )
    return 0


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(prog="cdrintel")
    parser.add_argument("--db", default=DEFAULT_DB)
    sub = parser.add_subparsers(dest="command", required=True)

    p_crawl = sub.add_parser("crawl", help="fetch and store a snapshot")
    p_crawl.add_argument("--limit", type=int, default=0, help="only the first N holders")
    p_crawl.set_defaults(func=cmd_crawl)

    sub.add_parser("score", help="data quality scorecard").set_defaults(func=cmd_score)

    p_moves = sub.add_parser("moves", help="rate changes since the previous snapshot")
    p_moves.add_argument("--min-bps", type=int, default=1)
    p_moves.add_argument("--top", type=int, default=40)
    p_moves.set_defaults(func=cmd_moves)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
