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

## What the live runs found

**The version ceiling was the whole story.** A first full-market crawl reported that 87 of 115 holders published no usable rates, including ANZ, Westpac, Bendigo and BOQ. That reading was wrong, and the error was mine. The [endpoint versioning schedule](https://consumerdatastandardsaustralia.github.io/standards/includes/endpoint-version-schedule/) puts Get Products at V5 and Get Product Detail at V7, with every earlier version past its retirement date as of October 2026. This crawler was offering v4 and below, so holders that retired old versions on schedule correctly answered 406 to everything on the table. The banks were compliant; the crawler was two years stale.

That is worth stating plainly because a rate tracker that silently mistakes its own staleness for a market-wide outage is worse than no tracker. The crawler now records which version each holder actually accepted, so the same mistake shows up as data rather than as a conclusion.

**Findings that survive the correction:**

| Holder | Finding |
| --- | --- |
| Traditional Credit Union | Registered as a banking data holder with no public base URI at all — nothing to crawl |
| Wise | Still serving Get Products v3, past its May 2026 retirement date |
| Aussie Home Loans | Registered base URI returns 404, not 406 — a dead endpoint rather than a version mismatch |
| PayPal Australia | Three products served cleanly, one rate row between them |

## Design decisions worth arguing with

- **Failures are data, not exceptions.** One holder returning 503 must not sink a market-wide crawl. Each run records which holders failed and why in `crawl_run.failures_json`. A snapshot covering 112 of 117 holders with a named gap is useful; a traceback at holder 40 is not.
- **Concurrency is capped at 6.** These are real production endpoints at real banks serving a public obligation. The crawler identifies itself and stays slow on purpose.
- **Version negotiation walks down from the current standard, not from memory.** The ceilings are V5 for the list endpoint and V7 for detail, taken from the published versioning schedule, and the walk reaches one version past retirement because laggards exist. Both numbers move; treat them as a dependency, not a constant.
- **A circuit breaker stops the walk becoming a flood.** AMP refused all five versions for all nine of its products: 45 requests into a production estate to learn one fact. Three consecutive failures now skip a holder's remaining products.
- **SQLite, not Parquet.** The dataset is tens of thousands of rows per snapshot. Reaching for a columnar format here would be resume-driven engineering.

## What I would do differently

- The percent-vs-decimal threshold is a heuristic with a magic number in it. The honest fix is per-holder convention detection — infer a holder's convention from the distribution of all its rates, then apply it consistently — rather than judging each value alone.
- Product detail carries fees, eligibility and constraints as well as rates. Only the rates are normalised so far; fees are fetched but not yet parsed.
- The version ceilings are hardcoded. They should be read from the versioning schedule, or at minimum fail loudly when a majority of holders reject every version offered — which is the signal that the crawler has gone stale, and which this project learned the hard way.
- Detail fetches ignore each product's `lastUpdated`. Skipping unchanged products and carrying their previous rows forward would cut a full crawl by most of its requests, which matters when the schedule is weekly and the target is someone else's production estate.
- There is no backfill. The history starts the day the first snapshot runs, which is an argument for starting it today rather than when the rest is finished.

## Status

43 tests, no network required. Run live against all 115 banking brands: 62 holders reachable and 1,422 products listed under the old (incorrect) version ceilings. The corrected ceilings have not yet been run at full scale, so current market coverage is unmeasured.

## Data source and licence

Data comes from the [CDR Register](https://www.cdr.gov.au/for-providers/how-find-data-holders-product-data-request-service) and each holder's public Product Reference Data endpoint. These are unauthenticated public APIs; no consumer data is accessed, stored, or involved at any point. The code is MIT licensed; the published bank data remains each institution's own.
