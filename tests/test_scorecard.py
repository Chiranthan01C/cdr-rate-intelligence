"""The scorecard must never omit a holder for having published nothing."""
import pytest

from cdrintel.crawl import HolderCircuit
from cdrintel.normalise import normalise_product
from cdrintel.quality import score_holder
from cdrintel.store import connect, holders_at, write_holder_snapshot

T = "2026-10-19T10:00:00+00:00"


class TestSilentHolders:
    def test_holder_with_no_rows_still_scores(self):
        """AMP Bank GO served nine products and zero readable details."""
        score = score_holder(
            "amp", "AMP Bank GO", [], product_ids_seen={f"p{i}" for i in range(9)},
            details_fetched=0, detail_failures=9,
        )
        assert score.rate_rows == 0
        assert score.completeness == 0.0 and score.conformance == 0.0
        assert score.note == "lists products, serves no detail"

    def test_products_fetched_but_carrying_no_rates_is_distinguished(self):
        """Wise served its product detail fine; the detail just has no rates."""
        score = score_holder(
            "wise", "Wise", [], product_ids_seen={"WiseMCA"},
            details_fetched=1, detail_failures=0,
        )
        assert score.note == "products carry no rates"

    def test_repaired_rates_are_named_in_the_note(self):
        detail = {
            "productId": "p1", "name": "Loan", "productCategory": "RESIDENTIAL_MORTGAGES",
            "lendingRates": [{"lendingRateType": "VARIABLE", "rate": "5.89"}],
        }
        rows = normalise_product(detail, brand_id="b", brand_name="Percent Bank")
        score = score_holder("b", "Percent Bank", rows, details_fetched=1)
        assert "published as percent" in score.note

    def test_reachability_separates_listed_from_readable(self):
        score = score_holder(
            "amp", "AMP Bank GO", [], product_ids_seen={"a", "b", "c", "d"},
            details_fetched=1, detail_failures=3,
        )
        assert score.reachability == pytest.approx(0.25)


class TestHolderSnapshotRoundTrip:
    def test_silent_holder_survives_the_database(self):
        conn = connect(":memory:")
        write_holder_snapshot(conn, [
            (T, "amp", "AMP Bank GO", 9, 0, 9, None, "5", None),
            (T, "alex", "Alex.Bank", 6, 6, 0, None, "5", "7"),
        ])
        rows = holders_at(conn, T)
        names = {r[1] for r in rows}
        assert names == {"AMP Bank GO", "Alex.Bank"}, "a holder with no rates must persist"

    def test_negotiated_versions_are_recorded(self):
        """Which version a holder serves is a conformance finding in its own right."""
        conn = connect(":memory:")
        write_holder_snapshot(conn, [
            (T, "wise", "Wise", 1, 1, 0, None, "3", "7"),
        ])
        row = holders_at(conn, T)[0]
        assert row[6] == "3", "a holder still on a retired list version must be visible"
        assert row[7] == "7"

    def test_store_created_before_version_columns_is_migrated(self):
        """An existing snapshot history must survive a schema addition."""
        import sqlite3

        conn = sqlite3.connect(":memory:")
        conn.executescript(
            "CREATE TABLE holder_snapshot (captured_at TEXT NOT NULL,"
            " brand_id TEXT NOT NULL, brand_name TEXT NOT NULL,"
            " products_listed INTEGER NOT NULL, details_fetched INTEGER NOT NULL,"
            " detail_failures INTEGER NOT NULL, crawl_error TEXT,"
            " PRIMARY KEY (captured_at, brand_id));"
        )
        conn.execute(
            "INSERT INTO holder_snapshot VALUES (?,?,?,?,?,?,?)",
            (T, "old", "Legacy Bank", 3, 3, 0, None),
        )
        conn.commit()

        from cdrintel.store import _migrate

        with conn:
            _migrate(conn)
        columns = {r[1] for r in conn.execute("PRAGMA table_info(holder_snapshot)")}
        assert {"list_version", "detail_version"} <= columns
        kept = conn.execute("SELECT brand_name FROM holder_snapshot").fetchone()
        assert kept[0] == "Legacy Bank", "migration must not drop existing history"


class TestStaleVersions:
    """The version a holder serves turned out to be the metric that discriminates."""

    def test_current_versions_are_not_flagged(self):
        score = score_holder("b", "CommBank", [], details_fetched=1,
                             list_version="5", detail_version="7")
        assert score.stale_versions == []

    def test_retired_list_version_is_named(self):
        score = score_holder("b", "Woolworths Team Bank", [], details_fetched=1,
                             list_version="3", detail_version="7")
        assert score.stale_versions == ["list v3"]

    def test_both_endpoints_stale_are_both_named(self):
        score = score_holder("b", "Tyro Payments", [], details_fetched=1,
                             list_version="3", detail_version="5")
        assert score.stale_versions == ["list v3", "detail v5"]

    def test_stale_version_reaches_the_note_when_data_is_otherwise_clean(self):
        from cdrintel.normalise import normalise_product
        detail = {"productId": "p", "name": "Saver",
                  "productCategory": "TRANS_AND_SAVINGS_ACCOUNTS",
                  "depositRates": [{"depositRateType": "VARIABLE", "rate": "0.045"}]}
        rows = normalise_product(detail, brand_id="b", brand_name="Auswide Bank Ltd")
        score = score_holder("b", "Auswide Bank Ltd", rows,
                             product_ids_seen={"p"}, details_fetched=1,
                             list_version="4", detail_version="6")
        assert score.conformance == 1.0, "the rates themselves are fine"
        assert score.note == "retired list v4 and detail v6"

    def test_a_missing_version_is_not_guessed_at(self):
        """A holder that never served detail has no detail version to judge."""
        score = score_holder("b", "Thriday", [], details_fetched=0,
                             list_version="5", detail_version=None)
        assert score.stale_versions == []


class TestEmptyCatalogue:
    def test_a_holder_listing_nothing_is_distinguished_from_one_serving_no_detail(self):
        empty = score_holder("h", "HSBC", [], product_ids_seen=set(),
                             details_fetched=0, list_version="5")
        assert empty.note == "responds but lists no products"

        no_detail = score_holder("w", "Woolworths Team Bank", [],
                                 product_ids_seen={"a", "b"}, details_fetched=0,
                                 detail_failures=2, list_version="3")
        assert no_detail.note == "lists products, serves no detail"


class TestCircuitBreaker:
    def test_trips_after_three_consecutive_failures(self):
        c = HolderCircuit(threshold=3)
        assert c.allow("amp")
        for _ in range(3):
            c.record("amp", False)
        assert not c.allow("amp")

    def test_a_success_resets_the_run(self):
        c = HolderCircuit(threshold=3)
        c.record("amp", False)
        c.record("amp", False)
        c.record("amp", True)
        c.record("amp", False)
        assert c.allow("amp"), "two failures either side of a success must not trip it"

    def test_other_holders_are_unaffected(self):
        c = HolderCircuit(threshold=2)
        c.record("amp", False)
        c.record("amp", False)
        assert not c.allow("amp")
        assert c.allow("alex")
