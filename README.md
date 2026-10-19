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

**Rates are not where you first look for them.** `GET /banking/products` returns summaries with no rates at all; every rate lives behind `GET /banking/products/{id}`, one request per product. There is no bulk endpoint, so a full market snapshot is a few thousand requests. The first live run of this project fetched only summaries and produced zero rate rows from five healthy holders — the pipeline ran perfectly and did nothing. Concurrency is now capped globally and per host, and `tests/test_crawl.py` holds the regression that stops a summary ever being mistaken for a detail record again.

**A holder that publishes nothing must still appear in the report.** The first live scorecard listed Alex.Bank, MyState and PayPal and silently omitted AMP Bank GO and Wise — the two holders with the worst data. Grouping by the rows that exist deletes every holder that produced none, which is exactly backwards for a data quality report. Scoring is now driven by the `holder_snapshot` table, which records every holder crawled whether or not it served anything, and the report sorts silent holders to the top.

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

## What the first live run found

Five holders, 37 products listed, 28 details readable, 215 rate rows.

| Holder | Listed | Readable | Finding |
| --- | --- | --- | --- |
| MyState Bank | 16 | 16 | 164 rate rows, fully conformant |
| Alex.Bank | 6 | 6 | 50 rate rows, fully conformant |
| AMP Bank GO | 9 | 0 | Refused every version of `GetProductDetail` on all nine products while serving the product list normally |
| PayPal Australia | 3 | 3 | Products served, one rate row between them |
| Wise | 1 | 1 | Serves v3 and rejects v4 on the list endpoint; the product carries no rates |
| Traditional Credit Union | — | — | Registered as a banking data holder with no public base URI at all |

The AMP result is the kind of thing this project exists to surface: conformance testing confirms a holder responds, and AMP responds — to the endpoint that carries no rates, while refusing the one that does.

## Design decisions worth arguing with

- **Failures are data, not exceptions.** One holder returning 503 must not sink a market-wide crawl. Each run records which holders failed and why in `crawl_run.failures_json`. A snapshot covering 112 of 117 holders with a named gap is useful; a traceback at holder 40 is not.
- **Concurrency is capped at 6.** These are real production endpoints at real banks serving a public obligation. The crawler identifies itself and stays slow on purpose.
- **Version negotiation walks down, not up.** Holders disagree on which version they support and answer 406 when you guess wrong, so the crawler tries from the top down — v4 for the list endpoint, v5 for detail. Wise confirms the need: it rejects v4 and serves v3.
- **A circuit breaker stops the walk becoming a flood.** AMP refused all five versions for all nine of its products: 45 requests into a production estate to learn one fact. Three consecutive failures now skip a holder's remaining products.
- **SQLite, not Parquet.** The dataset is tens of thousands of rows per snapshot. Reaching for a columnar format here would be resume-driven engineering.

## What I would do differently

- The percent-vs-decimal threshold is a heuristic with a magic number in it. The honest fix is per-holder convention detection — infer a holder's convention from the distribution of all its rates, then apply it consistently — rather than judging each value alone.
- Product detail carries fees, eligibility and constraints as well as rates. Only the rates are normalised so far; fees are fetched but not yet parsed.
- Detail fetches ignore each product's `lastUpdated`. Skipping unchanged products and carrying their previous rows forward would cut a full crawl by most of its requests, which matters when the schedule is weekly and the target is someone else's production estate.
- There is no backfill. The history starts the day the first snapshot runs, which is an argument for starting it today rather than when the rest is finished.

## Status

41 tests, no network required. Verified live against five holders end to end: Register discovery, list and detail fetch, normalisation and scoring all produce real output. Not yet run against the full 115-brand market, so the crawl's behaviour at scale and its total runtime are unmeasured.

## Data source and licence

Data comes from the [CDR Register](https://www.cdr.gov.au/for-providers/how-find-data-holders-product-data-request-service) and each holder's public Product Reference Data endpoint. These are unauthenticated public APIs; no consumer data is accessed, stored, or involved at any point. The code is MIT licensed; the published bank data remains each institution's own.
