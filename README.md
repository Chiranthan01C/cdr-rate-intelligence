# CDR Rate Intelligence

**A longitudinal record of how Australian banking product terms actually change — built from data that is public, mandated, and that nobody is keeping.**

Every Authorised Deposit-taking Institution in Australia is required by the Consumer Data Right to publish its full product catalogue — rates, fees, eligibility, constraints — through an unauthenticated API conforming to a single standard. Roughly 117 banking brands do this today.

Nobody stores it over time. The data exists for exactly as long as a bank leaves it up, and then the previous value is gone. This project snapshots all of it on a schedule, normalises it into something genuinely comparable, and derives two things that do not otherwise exist publicly:

1. **Rate movements** — which institution repriced what, by how many basis points, and when.
2. **A data quality scorecard per holder** — because the CDR conformance regime tests that a bank *responds*, not that what it publishes is correct.

## The problem this is actually solving

The interesting part is not fetching 117 URLs. It is that structurally valid JSON from 117 independently-maintained implementations is not comparable data.

**Rates are published as decimals, except when they aren't.** The Consumer Data Standards define `RateString` as a decimal fraction: 4.25% is `"0.0425"`. A meaningful minority of holders publish `"4.25"`. Naively averaging the raw field across the market produces a number that is wrong by two orders of magnitude for those holders, and nothing in the response indicates which convention you are reading.

`normalise.interpret_rate` resolves this by plausibility — a deposit rate whose decimal reading exceeds 35% p.a. is almost certainly a percentage — and **flags every value it had to second-guess**. Those flags feed the conformance score, so the correction is auditable rather than silent. A rate tracker that quietly repairs its inputs is a rate tracker you cannot check.

**Tiered rates have no headline number.** A bonus saver has several `BankingProductRateTier` entries and no single "the rate". Taking the first element is wrong about as often as it is right, so the normaliser emits one row per rate per tier and refuses to invent a headline.

**New products are not repricings.** `store.detect_moves` joins consecutive snapshots on `(brand, product, rate kind, rate type, tier)` and deliberately drops anything that appeared or disappeared. Conflating a launched product with a rate cut is the standard way these trackers report phantom movements.

## Running it

```bash
pip install -e ".[dev]"

python -m cdrintel crawl --limit 5   # start small; these are production bank endpoints
python -m cdrintel crawl             # full market snapshot
python -m cdrintel score             # per-holder data quality, worst first
python -m cdrintel moves --min-bps 5 # repricings since the last snapshot
```

The value compounds. One snapshot is a comparison table. Twelve months of snapshots is a record of how the Australian market repriced, which is why the store is append-only and moves are derived at read time — a bug in move detection can be fixed and the entire history recomputed.

## Design decisions worth arguing with

- **Failures are data, not exceptions.** One holder returning 503 must not sink a market-wide crawl. Each run records which holders failed and why in `crawl_run.failures_json`. A snapshot covering 112 of 117 holders with a named gap is useful; a traceback at holder 40 is not.
- **Concurrency is capped at 6.** These are real production endpoints at real banks serving a public obligation. The crawler identifies itself and stays slow on purpose.
- **Version negotiation walks down, not up.** Holders disagree on which `GetProducts` version they support and answer 406 when you guess wrong, so the crawler tries v4 through v1 rather than assuming.
- **SQLite, not Parquet.** The dataset is tens of thousands of rows per snapshot. Reaching for a columnar format here would be resume-driven engineering.

## What I would do differently

- The percent-vs-decimal threshold is a heuristic with a magic number in it. The honest fix is per-holder convention detection — infer a holder's convention from the distribution of all its rates, then apply it consistently — rather than judging each value alone.
- Product *detail* (`GET /banking/products/{id}`) carries fees, eligibility and constraints. This version only reads the summary list, so the fee side of the comparison is not built yet.
- There is no backfill. The history starts the day the first snapshot runs, which is an argument for starting it today rather than when the rest is finished.

## Status

Core pipeline complete and tested offline (24 tests, no network required). Live crawl not yet validated end-to-end against the Register.

## Data source and licence

Data comes from the [CDR Register](https://www.cdr.gov.au/for-providers/how-find-data-holders-product-data-request-service) and each holder's public Product Reference Data endpoint. These are unauthenticated public APIs; no consumer data is accessed, stored, or involved at any point. The code is MIT licensed; the published bank data remains each institution's own.
