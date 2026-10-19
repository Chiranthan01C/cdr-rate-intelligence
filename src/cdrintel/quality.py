"""Per-holder data quality scorecard.

The CDR conformance regime checks that a data holder *responds*, not that what
it publishes is complete or correct. That gap is documented in Basiq's 2022
Treasury submission and it is the reason this scorecard exists: it is the
first thing in the project that measures something nobody else is measuring.

One rule shapes the design. A holder that publishes NOTHING must still appear
in the scorecard, scoring zero. Scoring only the holders that produced rows
quietly deletes the worst offenders from the report -- which is the opposite
of what a data quality report is for. AMP Bank GO and Wise both hit this in
the first live run: both were crawled, both yielded no usable rates, and both
vanished from the output.
"""

from __future__ import annotations

import dataclasses
from collections import Counter

from .normalise import NormalisedRate


@dataclasses.dataclass(frozen=True, slots=True)
class HolderScore:
    brand_id: str
    brand_name: str
    products: int
    rate_rows: int
    products_without_rates: int
    rates_missing_value: int
    rates_repaired: int
    lending_missing_comparison: int
    tiered_rate_rows: int
    details_fetched: int = 0
    detail_failures: int = 0
    crawl_error: str | None = None

    @property
    def completeness(self) -> float:
        """Share of rate rows that carry a usable number. 0.0 if nothing published."""
        if not self.rate_rows:
            return 0.0
        return 1 - (self.rates_missing_value / self.rate_rows)

    @property
    def conformance(self) -> float:
        """Share of rate rows we did NOT have to repair.

        A repair means the holder published a percentage where the Standards
        require a decimal. It is the clearest signal of a holder that shipped
        the mandated endpoint without reading the spec.
        """
        if not self.rate_rows:
            return 0.0
        return 1 - (self.rates_repaired / self.rate_rows)

    @property
    def reachability(self) -> float:
        """Share of listed products whose detail record could actually be read."""
        if not self.products:
            return 0.0
        return self.details_fetched / self.products

    @property
    def note(self) -> str:
        """One short phrase naming this holder's worst problem, or empty."""
        if self.crawl_error:
            return "crawl failed"
        if self.products and not self.details_fetched:
            return "no product detail served"
        if self.detail_failures:
            return f"{self.detail_failures} details unreachable"
        if self.products and not self.rate_rows:
            return "products carry no rates"
        if self.rates_repaired:
            return f"{self.rates_repaired} rates published as percent"
        return ""

    def as_row(self) -> dict:
        return {
            "brand_id": self.brand_id,
            "brand_name": self.brand_name,
            "products": self.products,
            "details_fetched": self.details_fetched,
            "detail_failures": self.detail_failures,
            "rate_rows": self.rate_rows,
            "products_without_rates": self.products_without_rates,
            "rates_missing_value": self.rates_missing_value,
            "rates_repaired": self.rates_repaired,
            "lending_missing_comparison": self.lending_missing_comparison,
            "tiered_rate_rows": self.tiered_rate_rows,
            "completeness": round(self.completeness, 4),
            "conformance": round(self.conformance, 4),
            "reachability": round(self.reachability, 4),
            "note": self.note,
        }


def score_holder(
    brand_id: str,
    brand_name: str,
    rows: list[NormalisedRate],
    *,
    product_ids_seen: set[str] | None = None,
    details_fetched: int = 0,
    detail_failures: int = 0,
    crawl_error: str | None = None,
) -> HolderScore:
    """Score one holder from its normalised rate rows plus its crawl outcome.

    ``product_ids_seen`` should be every product the holder LISTED, so that
    products which published no rates are counted. Without it we can only see
    products that produced at least one row, which flatters exactly the
    holders that deserve it least.
    """
    with_rates = {row.product_id for row in rows}
    all_products = product_ids_seen if product_ids_seen is not None else with_rates

    lending_rows = [r for r in rows if r.rate_kind == "lending"]

    return HolderScore(
        brand_id=brand_id,
        brand_name=brand_name,
        products=len(all_products),
        rate_rows=len(rows),
        products_without_rates=len(all_products - with_rates),
        rates_missing_value=sum(1 for r in rows if r.rate_pct is None),
        rates_repaired=sum(1 for r in rows if r.rate_repaired),
        lending_missing_comparison=sum(
            1 for r in lending_rows if r.comparison_rate_pct is None
        ),
        tiered_rate_rows=sum(1 for r in rows if r.tier_name is not None),
        details_fetched=details_fetched,
        detail_failures=detail_failures,
        crawl_error=crawl_error,
    )


def rate_type_census(rows: list[NormalisedRate]) -> Counter:
    """How the whole market populates rate_type.

    The spread of DISCOUNT vs INTRODUCTORY vs BUNDLE_* across holders is a
    readable picture of how inconsistently the enum is used in production.
    """
    return Counter(f"{r.rate_kind}:{r.rate_type}" for r in rows)
