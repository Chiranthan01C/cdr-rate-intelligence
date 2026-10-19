import json
import pathlib
from decimal import Decimal

import pytest

from cdrintel.normalise import interpret_rate, normalise_product
from cdrintel.quality import score_holder
from cdrintel.register import parse_holders

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text())


class TestInterpretRate:
    def test_decimal_fraction_becomes_percentage(self):
        assert interpret_rate("0.0425") == (Decimal("4.25"), False)

    def test_percentage_published_against_spec_is_repaired(self):
        value, repaired = interpret_rate("5.89")
        assert value == Decimal("5.89")
        assert repaired is True, "must flag that we second-guessed the holder"

    def test_boundary_below_threshold_is_treated_as_decimal(self):
        value, repaired = interpret_rate("0.30")
        assert value == Decimal("30.00")
        assert repaired is False

    @pytest.mark.parametrize("raw", [None, "", "not-a-number", "-0.01", "9999"])
    def test_unusable_values_drop_to_none(self, raw):
        assert interpret_rate(raw)[0] is None


class TestNormaliseProduct:
    def test_tiered_rate_yields_one_row_per_tier(self):
        rows = normalise_product(
            load("product_conformant.json"), brand_id="b1", brand_name="Alex Bank"
        )
        # 1 untiered VARIABLE + 2 BONUS tiers
        assert len(rows) == 3
        bonus = [r for r in rows if r.rate_type == "BONUS"]
        assert {r.tier_name for r in bonus} == {"Up to $50,000", "Above $50,000"}
        assert all(r.rate_pct == Decimal("3.40") for r in bonus)

    def test_untiered_rate_has_empty_tier_fields(self):
        rows = normalise_product(
            load("product_conformant.json"), brand_id="b1", brand_name="Alex Bank"
        )
        variable = next(r for r in rows if r.rate_type == "VARIABLE")
        assert variable.tier_name is None and variable.tier_min is None
        assert variable.rate_pct == Decimal("0.85")

    def test_lending_rates_carry_purpose_and_comparison(self):
        rows = normalise_product(
            load("product_percent_bug.json"), brand_id="b2", brand_name="Percent Bank"
        )
        var = next(r for r in rows if r.rate_type == "VARIABLE")
        assert var.rate_kind == "lending"
        assert var.loan_purpose == "OWNER_OCCUPIED"
        assert var.comparison_rate_pct == Decimal("5.94")
        assert var.rate_repaired is True

    def test_empty_rate_string_survives_as_none(self):
        rows = normalise_product(
            load("product_percent_bug.json"), brand_id="b2", brand_name="Percent Bank"
        )
        penalty = next(r for r in rows if r.rate_type == "PENALTY")
        assert penalty.rate_pct is None


class TestRegister:
    def test_filters_to_requested_industry(self):
        holders = parse_holders(load("register_sample.json"), industry="banking")
        assert [h.brand_name for h in holders] == ["Alex Bank"]

    def test_malformed_record_is_skipped_not_raised(self):
        holders = parse_holders(load("register_sample.json"), industry="")
        assert len(holders) == 2  # the one without publicBaseUri is dropped

    def test_products_url_follows_the_mandated_path(self):
        holder = parse_holders(load("register_sample.json"))[0]
        assert holder.products_url == (
            "https://public.cdr.alex.com.au/cds-au/v1/banking/products"
        )


class TestQuality:
    def test_conformance_penalises_repaired_rates(self):
        rows = normalise_product(
            load("product_percent_bug.json"), brand_id="b2", brand_name="Percent Bank"
        )
        score = score_holder("b2", "Percent Bank", rows)
        assert score.rates_repaired == 2  # VARIABLE and FIXED, not the empty PENALTY
        assert score.conformance < 0.5

    def test_products_with_no_rates_are_counted_when_supplied(self):
        rows = normalise_product(
            load("product_conformant.json"), brand_id="b1", brand_name="Alex Bank"
        )
        score = score_holder(
            "b1", "Alex Bank", rows, product_ids_seen={"savings-001", "silent-002"}
        )
        assert score.products == 2
        assert score.products_without_rates == 1

    def test_empty_holder_scores_zero_without_dividing_by_zero(self):
        score = score_holder("b3", "Silent Bank", [], product_ids_seen=set())
        assert score.completeness == 0.0 and score.conformance == 0.0
