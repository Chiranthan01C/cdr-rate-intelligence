import json
import pathlib

from cdrintel.crawl import unwrap_detail
from cdrintel.normalise import normalise_product
from cdrintel.register import parse_holders

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text())


class TestUnwrapDetail:
    def test_standard_envelope(self):
        product = load("product_conformant.json")
        assert unwrap_detail({"data": product, "links": {}})["productId"] == "savings-001"

    def test_bare_record_is_tolerated(self):
        product = load("product_conformant.json")
        assert unwrap_detail(product)["productId"] == "savings-001"

    def test_summary_envelope_is_not_a_detail(self):
        """The list endpoint's shape must never be mistaken for a detail record.

        This is the bug that made the first live crawl return zero rate rows.
        """
        listing = {"data": {"products": [{"productId": "savings-001", "name": "X"}]}}
        assert unwrap_detail(listing) is None

    def test_garbage_returns_none(self):
        assert unwrap_detail({}) is None
        assert unwrap_detail({"data": None}) is None
        assert unwrap_detail("not a dict") is None


class TestDetailYieldsRates:
    def test_detail_record_produces_rate_rows(self):
        """Guards the invariant the live run violated: details must yield rows."""
        detail = unwrap_detail({"data": load("product_conformant.json")})
        rows = normalise_product(detail, brand_id="b1", brand_name="Alex Bank")
        assert len(rows) == 3
        assert any(r.rate_pct is not None for r in rows)

    def test_summary_record_yields_nothing(self):
        """A summary has no rates - proving the two endpoints are not interchangeable."""
        summary = {"productId": "savings-001", "name": "Everyday Saver",
                   "productCategory": "TRANS_AND_SAVINGS_ACCOUNTS"}
        assert normalise_product(summary, brand_id="b1", brand_name="Alex Bank") == []


class TestRegisterNoise:
    def test_energy_brands_are_filtered_before_the_reachability_check(self, caplog):
        """Energy holders without a base URI are expected, not a data quality finding."""
        payload = {"data": [
            {"brandName": "Ampol Energy", "industries": ["energy"],
             "legalEntity": {"legalEntityName": "Ampol"}},
            {"dataHolderBrandId": "b1", "brandName": "Alex Bank", "industries": ["banking"],
             "publicBaseUri": "https://public.cdr.alex.com.au",
             "legalEntity": {"legalEntityName": "Alex Bank Pty Ltd"}},
        ]}
        holders = parse_holders(payload, industry="banking")
        assert [h.brand_name for h in holders] == ["Alex Bank"]
        assert "Ampol" not in caplog.text

    def test_banking_brand_without_base_uri_is_reported(self, caplog):
        payload = {"data": [
            {"brandName": "Traditional Credit Union", "industries": ["banking"],
             "legalEntity": {"legalEntityName": "TCU"}},
        ]}
        assert parse_holders(payload, industry="banking") == []
        assert "Traditional Credit Union" in caplog.text
