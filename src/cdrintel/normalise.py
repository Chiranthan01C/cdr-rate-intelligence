"""Normalise Consumer Data Standards product records into one comparable row.

This module is the point of the project. The Standards mandate a schema, but
they do not enforce how holders populate it, and the regime explicitly does
not test the quality of what is published. So ~117 institutions emit
structurally valid JSON that is not actually comparable until someone does
the work below.

The three traps this handles, in order of how often they bite:

1. RateString is a DECIMAL, not a percentage. 4.25% is "0.0425". A
   meaningful minority of holders publish "4.25". Averaging the raw field
   across holders silently produces garbage.
2. Tiered rates carry no headline. A savings account with a bonus tier has
   several BankingProductRateTier entries and no single "the rate" -- picking
   the first element is wrong roughly as often as it is right.
3. Rate TYPE is the comparison key, not product name. A "VARIABLE" lending
   rate and an "INTRODUCTORY" one on the same product are not alternatives.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
from decimal import Decimal, InvalidOperation

LOG = logging.getLogger(__name__)

# A deposit rate above this, read as a decimal, is implausible in any
# Australian retail market and almost certainly means the holder published
# a percentage. 0.35 would be 35% p.a.
PERCENT_SUSPICION_THRESHOLD = Decimal("0.35")

# Above this, even the percent reading is nonsense and the value is dropped.
ABSURD_THRESHOLD = Decimal("100")


@dataclasses.dataclass(frozen=True, slots=True)
class NormalisedRate:
    brand_id: str
    brand_name: str
    product_id: str
    product_name: str
    product_category: str
    rate_kind: str           # "deposit" or "lending"
    rate_type: str           # VARIABLE, FIXED, INTRODUCTORY, BONUS, ...
    rate_pct: Decimal | None  # annual rate as a percentage, e.g. Decimal("4.25")
    comparison_rate_pct: Decimal | None
    tier_name: str | None
    tier_min: Decimal | None
    tier_max: Decimal | None
    tier_unit: str | None
    additional_value: str | None
    repayment_type: str | None
    loan_purpose: str | None
    rate_repaired: bool      # True if we corrected a percent-as-decimal error
    captured_at: str


def _to_decimal(raw: object) -> Decimal | None:
    if raw is None or raw == "":
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        LOG.debug("uninterpretable numeric: %r", raw)
        return None


def interpret_rate(raw: object) -> tuple[Decimal | None, bool]:
    """Turn a CDS RateString into a percentage, repairing the common error.

    Returns (percentage, was_repaired).

    The Standards say the field is a decimal fraction, so the correct reading
    of "0.0425" is 4.25%. A value whose decimal reading exceeds
    PERCENT_SUSPICION_THRESHOLD is treated as already being a percentage and
    is NOT multiplied -- the flag records that we second-guessed the holder,
    so the quality scorecard can count it and a reader can audit it.
    """
    value = _to_decimal(raw)
    if value is None:
        return None, False

    if value < 0:
        LOG.debug("negative rate dropped: %r", raw)
        return None, False

    if value > ABSURD_THRESHOLD:
        LOG.debug("absurd rate dropped: %r", raw)
        return None, False

    if value > PERCENT_SUSPICION_THRESHOLD:
        # Already a percentage, against the Standards.
        return value, True

    return value * 100, False


def _tier_fields(tier: dict) -> dict:
    return {
        "tier_name": tier.get("name"),
        "tier_min": _to_decimal(tier.get("minimumValue")),
        "tier_max": _to_decimal(tier.get("maximumValue")),
        "tier_unit": tier.get("unitOfMeasure"),
    }


_EMPTY_TIER = {"tier_name": None, "tier_min": None, "tier_max": None, "tier_unit": None}


def normalise_product(
    detail: dict,
    *,
    brand_id: str,
    brand_name: str,
    captured_at: str | None = None,
) -> list[NormalisedRate]:
    """Flatten one product-detail record into one row per rate per tier.

    A tiered product yields several rows. That is deliberate: collapsing a
    tiered savings account to a single number is the thing that makes most
    published rate comparisons wrong.
    """
    captured_at = captured_at or dt.datetime.now(dt.timezone.utc).isoformat()
    product_id = detail.get("productId") or ""
    product_name = detail.get("name") or ""
    category = detail.get("productCategory") or ""

    rows: list[NormalisedRate] = []

    for kind, key in (("deposit", "depositRates"), ("lending", "lendingRates")):
        for rate in detail.get(key) or []:
            rate_pct, repaired = interpret_rate(rate.get("rate"))
            comparison_pct, _ = interpret_rate(rate.get("comparisonRate"))
            rate_type = rate.get(f"{kind}RateType") or "UNKNOWN"

            tiers = rate.get("tiers") or [None]
            for tier in tiers:
                tier_fields = _tier_fields(tier) if tier else dict(_EMPTY_TIER)
                rows.append(
                    NormalisedRate(
                        brand_id=brand_id,
                        brand_name=brand_name,
                        product_id=product_id,
                        product_name=product_name,
                        product_category=category,
                        rate_kind=kind,
                        rate_type=rate_type,
                        rate_pct=rate_pct,
                        comparison_rate_pct=comparison_pct,
                        additional_value=rate.get("additionalValue"),
                        repayment_type=rate.get("repaymentType"),
                        loan_purpose=rate.get("loanPurpose"),
                        rate_repaired=repaired,
                        captured_at=captured_at,
                        **tier_fields,
                    )
                )

    return rows
