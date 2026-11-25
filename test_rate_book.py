#!/usr/bin/env python3
"""Tests for rate_book. Run: python3 -m unittest test_rate_book -v"""

import contextlib
import csv
import io
import json
import os
import tempfile
import unittest

import rate_book as rb


CARD = dict(base_weight_g=500, base_price=6500, slab_g=500,
            slab_price=4000, fuel_bp=1500, cod_fixed=3500, cod_bp=200,
            gst_bp=1800, effective="2026-11-01")


def book_with_card():
    book = rb.Book()
    book.add_courier("SWIFTAIR")
    book.set_card("SWIFTAIR", **CARD)
    return book


class ParseTest(unittest.TestCase):
    def test_parse_amount(self):
        self.assertEqual(rb.parse_amount("65"), 6500)
        self.assertEqual(rb.parse_amount("65.50"), 6550)
        self.assertEqual(rb.parse_amount("0", allow_zero=True), 0)
        for bad in ("0", "abc", "1.234", "1,000", "-5"):
            with self.assertRaises(rb.RateBookError, msg=bad):
                rb.parse_amount(bad)

    def test_format_paise_indian_grouping(self):
        self.assertEqual(rb.format_paise(123456789), "12,34,567.89")
        self.assertEqual(rb.format_paise(6500), "65.00")

    def test_parse_weight(self):
        self.assertEqual(rb.parse_weight("1200"), 1200)
        for bad in ("0", "-2", "1.5", "heavy"):
            with self.assertRaises(rb.RateBookError, msg=bad):
                rb.parse_weight(bad)

    def test_parse_percent_to_basis_points(self):
        self.assertEqual(rb.parse_percent("15"), 1500)
        self.assertEqual(rb.parse_percent("2.5"), 250)
        self.assertEqual(rb.parse_percent("18"), 1800)
        for bad in ("0", "abc", "1.234"):
            with self.assertRaises(rb.RateBookError, msg=bad):
                rb.parse_percent(bad)

    def test_pct_of_rounding(self):
        self.assertEqual(rb.pct_of(10000, 1500), 1500)
        self.assertEqual(rb.pct_of(10001, 1500), 1500)   # 1500.015 -> 1500
        self.assertEqual(rb.pct_of(10003, 1500), 1500)   # 1500.045 -> 1500
        self.assertEqual(rb.pct_of(10007, 1500), 1501)   # 1501.05 -> 1501


class CostMathTest(unittest.TestCase):
    def cost(self, w, cod=0):
        return rb.shipment_cost(dict(CARD), w, cod)

    def test_within_base_weight(self):
        c = self.cost(500)
        self.assertEqual(c["freight"], 6500)          # 65.00
        self.assertEqual(c["fuel"], 975)              # 15% of 65.00
        self.assertEqual(c["cod"], 0)
        self.assertEqual(c["gst"], 1346)              # 18% of 74.75 = 13.455
        self.assertEqual(c["total"], 6500 + 975 + 1346)

    def test_partial_slab_rounds_up(self):
        # 501 g: one extra started slab
        c = self.cost(501)
        self.assertEqual(c["freight"], 10500)         # 65 + 40
        # 1200 g: 700 extra -> 2 slabs
        c2 = self.cost(1200)
        self.assertEqual(c2["freight"], 14500)        # 65 + 2x40

    def test_exact_slab_boundary(self):
        c = self.cost(1000)                            # 500 extra = 1 slab
        self.assertEqual(c["freight"], 10500)

    def test_cod_charges(self):
        c = self.cost(500, cod=200000)                # COD 2,000.00
        self.assertEqual(c["cod"], 3500 + 4000)       # 35 fixed + 2%
        taxable = 6500 + 975 + 7500
        self.assertEqual(c["gst"], rb.pct_of(taxable, 1800))
        self.assertEqual(c["total"], taxable + c["gst"])

    def test_no_cod_no_cod_charge(self):
        c = self.cost(1500)
        self.assertEqual(c["cod"], 0)


class CourierCardTest(unittest.TestCase):
    def test_add_courier_and_card(self):
        book = rb.Book()
        book.add_courier("SWIFTAIR", contact="ops@swift.example")
        card = book.set_card("SWIFTAIR", **CARD)
        self.assertEqual(book.get_card("SWIFTAIR")["slab_price"], 4000)
        self.assertEqual(card["effective"], "2026-11-01")

    def test_duplicate_courier_rejected(self):
        book = book_with_card()
        with self.assertRaises(rb.RateBookError):
            book.add_courier("SWIFTAIR")

    def test_card_replace_allowed(self):
        book = book_with_card()
        new = dict(CARD, base_price=7000, effective="2026-11-20")
        book.set_card("SWIFTAIR", **new)
        self.assertEqual(book.get_card("SWIFTAIR")["base_price"], 7000)
        self.assertEqual(book.get_card("SWIFTAIR")["effective"],
                         "2026-11-20")

    def test_card_on_unknown_courier_rejected(self):
        with self.assertRaises(rb.RateBookError):
            rb.Book().set_card("GHOST", **CARD)

    def test_missing_card_error(self):
        book = rb.Book()
        book.add_courier("SWIFTAIR")
        with self.assertRaises(rb.RateBookError):
            book.get_card("SWIFTAIR")


class CompareTest(unittest.TestCase):
    def setUp(self):
        self.book = book_with_card()
        self.book.add_courier("CHEAPEX")
        self.book.set_card("CHEAPEX", base_weight_g=500, base_price=5500,
                           slab_g=500, slab_price=4500, fuel_bp=1000,
                           cod_fixed=0, cod_bp=100, gst_bp=1800)
        self.book.add_courier("NO CARD COURIER")

    def test_compare_ranks_and_skips_cardless(self):
        rows = self.book.compare(500)
        names = [name for name, _ in rows]
        self.assertEqual(names, ["CHEAPEX", "SWIFTAIR"])
        self.assertNotIn("NO CARD COURIER", names)
        # CHEAPEX: 55 + 5.5 fuel = 60.5 taxable; GST 10.89 -> 71.39
        self.assertEqual(rows[0][1]["total"], 6050 + 1089)
        # SWIFTAIR: 65 + 9.75 = 74.75; GST 13.46(rounded) -> 88.21
        self.assertEqual(rows[1][1]["total"], 7475 + 1346)

    def test_compare_with_cod(self):
        rows = self.book.compare(1200, cod_amount=100000)
        by_name = dict(rows)
        sw = by_name["SWIFTAIR"]
        self.assertEqual(sw["freight"], 14500)
        self.assertEqual(sw["cod"], 3500 + 2000)
        self.assertTrue(all(cost["total"] > 0 for _, cost in rows))

    def test_ranking_at_higher_weight(self):
        # At 1200 g freight ties at 145.00 (SWIFTAIR 65+2x40, CHEAPEX
        # 55+2x45); CHEAPEX stays first on its lower fuel surcharge.
        rows = self.book.compare(1200)
        self.assertEqual(rows[0][0], "CHEAPEX")
        sw = dict(rows)["SWIFTAIR"]
        self.assertEqual(sw["freight"], 14500)
        self.assertEqual(sw["fuel"], 2175)


class AuditTest(unittest.TestCase):
    def setUp(self):
        self.book = book_with_card()

    def test_exact_match(self):
        expected = rb.shipment_cost(dict(CARD), 500)["total"]
        a = self.book.record_audit("SWIFTAIR", "2026-11-10", 500, expected)
        self.assertEqual(a["diff"], 0)

    def test_over_and_under(self):
        expected = rb.shipment_cost(dict(CARD), 500)["total"]
        over = self.book.record_audit("SWIFTAIR", "2026-11-10", 500,
                                      expected + 250)
        self.assertEqual(over["diff"], 250)
        under = self.book.record_audit("SWIFTAIR", "2026-11-11", 500,
                                       expected - 100)
        self.assertEqual(under["diff"], -100)

    def test_audit_needs_card(self):
        book = rb.Book()
        book.add_courier("SWIFTAIR")
        with self.assertRaises(rb.RateBookError):
            book.record_audit("SWIFTAIR", "2026-11-10", 500, 100)

    def test_summary_counts(self):
        expected = rb.shipment_cost(dict(CARD), 500)["total"]
        self.book.record_audit("SWIFTAIR", "2026-11-10", 500, expected + 1)
        self.book.record_audit("SWIFTAIR", "2026-11-11", 500, expected - 1)
        self.book.record_audit("SWIFTAIR", "2026-11-12", 500, expected)
        s = self.book.summary()
        self.assertEqual(s["audits"], 3)
        self.assertEqual(s["over"], 1)
        self.assertEqual(s["under"], 1)
        self.assertEqual(s["with_cards"], 1)


class StorageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "book.json")

    def test_roundtrip(self):
        book = book_with_card()
        book.record_audit("SWIFTAIR", "2026-11-10", 700, 9999)
        book.save(self.path)
        loaded = rb.Book.load(self.path)
        self.assertEqual(loaded.get_card("SWIFTAIR")["fuel_bp"], 1500)
        self.assertEqual(len(loaded.audits), 1)
        self.assertEqual(loaded.audits[0]["charged"], 9999)

    def test_load_missing_file_gives_empty_book(self):
        book = rb.Book.load(os.path.join(self.tmp.name, "nope.json"))
        self.assertEqual(len(book.couriers), 0)

    def test_corrupt_file_raises_user_error(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{ nope")
        with self.assertRaises(rb.RateBookError):
            rb.Book.load(self.path)

    def test_save_is_atomic_replace(self):
        book = book_with_card()
        book.save(self.path)
        self.assertTrue(os.path.exists(self.path))
        leftovers = [n for n in os.listdir(self.tmp.name) if n != "book.json"]
        self.assertEqual(leftovers, [])
        with open(self.path, encoding="utf-8") as fh:
            json.load(fh)


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "book.json")

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = rb.main(["--db", self.db] + list(argv))
        return code, out.getvalue()

    def seed(self):
        self.run_cli("courier", "SWIFTAIR")
        self.run_cli("courier", "CHEAPEX")
        self.run_cli("rate", "set", "SWIFTAIR", "--base-weight", "500",
                     "--base-price", "65", "--slab", "500",
                     "--slab-price", "40", "--fuel", "15",
                     "--cod-fixed", "35", "--cod-pct", "2", "--gst", "18")
        self.run_cli("rate", "set", "CHEAPEX", "--base-weight", "500",
                     "--base-price", "55", "--slab", "500",
                     "--slab-price", "45", "--fuel", "10",
                     "--cod-pct", "1", "--gst", "18")

    def test_full_flow_and_outputs(self):
        self.seed()
        code, out = self.run_cli("rate", "show", "SWIFTAIR")
        self.assertEqual(code, 0)
        self.assertIn("first 500 g: 65.00", out)
        self.assertIn("fuel surcharge: 15.00%", out)

        code, out = self.run_cli("compare", "500")
        self.assertEqual(code, 0)
        self.assertIn("1. CHEAPEX", out)
        self.assertIn("2. SWIFTAIR", out)
        self.assertIn("= 71.39", out)   # CHEAPEX total
        self.assertIn("= 88.21", out)   # SWIFTAIR total

        code, out = self.run_cli("compare", "1200", "--cod", "1000")
        self.assertEqual(code, 0)
        self.assertIn("COD 1,000.00", out)

        # audit exact SWIFTAIR 500g total: 88.21
        code, out = self.run_cli("audit", "SWIFTAIR", "2026-11-24", "500",
                                 "88.21")
        self.assertEqual(code, 0)
        self.assertIn("matches the rate card exactly", out)

        code, out = self.run_cli("audit", "SWIFTAIR", "2026-11-24", "500",
                                 "95")
        self.assertIn("OVER-BILLED by 6.79", out)

        code, out = self.run_cli("audits")
        self.assertIn("ok", out)
        self.assertIn("OVER +6.79", out)

        code, out = self.run_cli("summary")
        self.assertIn("couriers: 2 (2 with rate cards)", out)
        self.assertIn("invoices audited: 2 (over-billed 1, under-billed 0)",
                      out)

    def test_cli_error_exit_code(self):
        self.seed()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = rb.main(["--db", self.db, "compare", "500",
                            "--cod", "-5"])
        self.assertEqual(code, 2)
        self.assertIn("bad amount", err.getvalue())

    def test_cli_audit_without_card_exit_code(self):
        self.run_cli("courier", "LONECOURIER")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = rb.main(["--db", self.db, "audit", "LONECOURIER",
                            "2026-11-24", "500", "100"])
        self.assertEqual(code, 2)
        self.assertIn("no rate card recorded", err.getvalue())

    def test_cli_export(self):
        self.seed()
        self.run_cli("audit", "SWIFTAIR", "2026-11-24", "500", "88.21")
        self.run_cli("audit", "SWIFTAIR", "2026-11-24", "500", "95")
        csv_path = os.path.join(self.tmp.name, "out.csv")
        code, out = self.run_cli("export", csv_path)
        self.assertEqual(code, 0)
        with open(csv_path, newline="", encoding="utf-8") as fh:
            rows = list(csv.reader(fh))
        self.assertEqual(rows[0], ["courier", "date", "weight_g",
                                   "cod_amount", "charged", "expected",
                                   "diff"])
        self.assertEqual(rows[1][4], "88.21")
        self.assertEqual(rows[1][6], "0.00")
        self.assertEqual(rows[2][6], "6.79")


if __name__ == "__main__":
    unittest.main()
