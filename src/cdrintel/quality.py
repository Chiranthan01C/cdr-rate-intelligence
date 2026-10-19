"""Per-holder data quality scorecard.

The CDR conformance regime checks that a data holder *responds*, not that what
it publishes is complete or correct. That gap is documented in Basiq's 2022
Treasury submission and it is the reason this scorecard exists: it is the
first thing in the project that says something no one else has measured.

Each check is deliberately cheap and auditable. A score that cannot be traced
back to named products is not worth publishing.
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

    def as_row(self) -> dict:
        return {
            "brand_id": self.brand_id,
            "brand_name": self.brand_name,
            "products": self.products,
            "rate_rows": self.rate_rows,
            "products_without_rates": self.products_without_rates,
            "rates_missing_value": self.rates_missing_value,
            "rates_repaired": self.rates_repaired,
            "lending_missing_comparison": self.lending_missing_comparison,
            "tiered_rate_rows": self.tiered_rate_rows,
            "completeness": round(self.completeness, 4),
            "conformance": round(self.conformance, 4),
        }


def score_holder(
    brand_id: str,
    brand_name: str,
    rows: list[NormalisedRate],
    *,
    product_ids_seen: set[str] | None = None,
) -> HolderScore:
    """Score one holder from its normalised rate rows.

    ``product_ids_seen`` lets the caller pass the full product list, so
    products that published no rates at all are counted. Without it we can
    only see products that produced at least one row, which would flatter
    exactly the holders that deserve it least.
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
    )


def rate_type_census(rows: list[NormalisedRate]) -> Counter:
    """How the whole market populates rate_type.

    Useful on its own: the spread of DISCOUNT vs INTRODUCTORY vs BUNDLE_*
    across holders is a readable picture of how inconsistently the enum is
    actually used in production.
    """
    return Counter(f"{r.rate_kind}:{r.rate_type}" for r in rows)
