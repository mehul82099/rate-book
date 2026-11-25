#!/usr/bin/env python3
"""rate-book: zero-dependency courier rate comparison and invoice-audit CLI.

Couriers quote differently: base slabs, per-slab increments, fuel
surcharges, COD fees, GST. Comparing two rate cards for one shipment is
manual arithmetic every time. rate-book keeps each courier's card, then
answers: for this parcel, what does each courier really cost all-in,
ranked cheapest first - and when the invoice arrives, does the charged
amount match the card?

Python 3.9+, standard library only. One JSON file, offline, no accounts.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import tempfile
from datetime import date

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "rate_book.json")


class RateBookError(Exception):
    """User-facing error (bad input, unknown courier, ...)."""


def today_iso():
    return date.today().isoformat()


def parse_date(text):
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise RateBookError("bad date %r (expected YYYY-MM-DD)" % text)


def parse_amount(text, allow_zero=False):
    """Rupees string -> integer paise; max 2 decimals."""
    text = (text or "").strip()
    if not re.fullmatch(r"\d+(\.\d{1,2})?", text):
        raise RateBookError(
            "bad amount %r (e.g. 65 or 65.50, paise beyond 2 decimals "
            "not accepted)" % text)
    rupees, _, frac = text.partition(".")
    frac = (frac + "00")[:2]
    paise = int(rupees) * 100 + int(frac)
    if paise < 0 or (paise == 0 and not allow_zero):
        raise RateBookError("amount must be positive")
    return paise


def format_paise(paise):
    sign = "-" if paise < 0 else ""
    paise = abs(paise)
    s = str(paise // 100)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts + [tail])
    return "%s%s.%02d" % (sign, s, paise % 100)


def parse_weight(text):
    """Grams as a whole number, at least 1."""
    try:
        g = int(text)
    except (TypeError, ValueError):
        raise RateBookError("bad weight %r (expected whole grams, e.g. 1200)"
                            % (text,))
    if g <= 0:
        raise RateBookError("weight must be at least 1 gram")
    return g


def parse_percent(text):
    """Percent string -> basis points integer: '15' -> 1500, '2.5' -> 250."""
    text = (text or "").strip()
    if not re.fullmatch(r"\d+(\.\d{1,2})?", text):
        raise RateBookError("bad percent %r (e.g. 15 or 2.5)" % text)
    whole, _, frac = text.partition(".")
    bp = int(whole) * 100 + int((frac + "00")[:2])
    if bp <= 0:
        raise RateBookError("percent must be positive")
    return bp


def pct_of(amount_paise, bp):
    """bp percent of a paise amount, rounded to the nearest paisa."""
    return (amount_paise * bp + 5000) // 10000


def ceil_div(a, b):
    return -(-a // b)


def shipment_cost(card, weight_g, cod_amount=0):
    """All-in cost in paise for a shipment on one courier's card.

    freight: base price covers the first base_weight_g, then one slab
    price per started slab_g beyond it.
    fuel:    fuel_pct of freight.
    cod:     cod_fixed + cod_pct of the COD amount (only when cod_amount
             is positive).
    gst:     gst_pct of (freight + fuel + cod charge).
    Returns a breakdown dict of integer paise.
    """
    freight = card["base_price"]
    extra = weight_g - card["base_weight_g"]
    if extra > 0:
        freight += ceil_div(extra, card["slab_g"]) * card["slab_price"]
    fuel = pct_of(freight, card["fuel_bp"])
    cod_charge = 0
    if cod_amount > 0:
        cod_charge = card["cod_fixed"] + pct_of(cod_amount, card["cod_bp"])
    taxable = freight + fuel + cod_charge
    gst = pct_of(taxable, card["gst_bp"])
    return {"freight": freight, "fuel": fuel, "cod": cod_charge,
            "gst": gst, "total": taxable + gst}


class Book:
    def __init__(self):
        self.couriers = {}  # name -> {name, contact, card, created}
        self.audits = []    # list of audit dicts

    @classmethod
    def load(cls, path):
        book = cls()
        if not os.path.exists(path):
            return book
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            raise RateBookError("cannot read %s: %s" % (path, exc))
        book.couriers = data.get("couriers", {})
        book.audits = data.get("audits", [])
        return book

    def save(self, path):
        data = {"couriers": self.couriers, "audits": self.audits}
        fd, tmp_name = tempfile.mkstemp(
            dir=os.path.dirname(os.path.abspath(path)), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
                fh.write("\n")
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def add_courier(self, name, contact=""):
        name = (name or "").strip()
        if not name:
            raise RateBookError("courier name cannot be empty")
        if name in self.couriers:
            raise RateBookError("courier %r already exists" % name)
        self.couriers[name] = {"name": name, "contact": contact,
                               "card": None, "created": today_iso()}
        return self.couriers[name]

    def get_courier(self, name):
        try:
            return self.couriers[name]
        except KeyError:
            raise RateBookError("unknown courier %r (add it first)"
                                % (name,))

    def set_card(self, name, base_weight_g, base_price, slab_g, slab_price,
                 fuel_bp, cod_fixed, cod_bp, gst_bp, effective=None):
        courier = self.get_courier(name)
        card = {"base_weight_g": base_weight_g, "base_price": base_price,
                "slab_g": slab_g, "slab_price": slab_price,
                "fuel_bp": fuel_bp, "cod_fixed": cod_fixed,
                "cod_bp": cod_bp, "gst_bp": gst_bp,
                "effective": effective or today_iso()}
        courier["card"] = card
        return card

    def get_card(self, name):
        courier = self.get_courier(name)
        if not courier["card"]:
            raise RateBookError(
                "no rate card recorded for %s yet (use: rate set)" % name)
        return courier["card"]

    def compare(self, weight_g, cod_amount=0):
        rows = []
        for name in sorted(self.couriers):
            card = self.couriers[name]["card"]
            if not card:
                continue
            cost = shipment_cost(card, weight_g, cod_amount)
            rows.append((name, cost))
        rows.sort(key=lambda row: (row[1]["total"], row[0]))
        return rows

    def record_audit(self, name, when, weight_g, charged, cod_amount=0):
        card = self.get_card(name)
        expected = shipment_cost(card, weight_g, cod_amount)
        audit = {"courier": name, "date": when, "weight_g": weight_g,
                 "cod_amount": cod_amount, "charged": charged,
                 "expected": expected["total"],
                 "diff": charged - expected["total"]}
        self.audits.append(audit)
        return audit

    def summary(self):
        with_cards = sum(1 for c in self.couriers.values() if c["card"])
        over = sum(1 for a in self.audits if a["diff"] > 0)
        under = sum(1 for a in self.audits if a["diff"] < 0)
        return {"couriers": len(self.couriers), "with_cards": with_cards,
                "audits": len(self.audits), "over": over, "under": under}

    def export_csv(self, out_path):
        fields = ["courier", "date", "weight_g", "cod_amount", "charged",
                  "expected", "diff"]
        with open(out_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(fields)
            for a in self.audits:
                writer.writerow([
                    a["courier"], a["date"], a["weight_g"],
                    format_paise(a["cod_amount"]),
                    format_paise(a["charged"]),
                    format_paise(a["expected"]),
                    format_paise(a["diff"])])


def cmd_courier_add(book, args):
    book.add_courier(args.name, contact=args.contact or "")
    print("added courier %s" % args.name)


def cmd_rate_set(book, args):
    existed = bool(book.get_courier(args.name)["card"])
    card = book.set_card(
        args.name, parse_weight(args.base_weight),
        parse_amount(args.base_price), parse_weight(args.slab),
        parse_amount(args.slab_price), parse_percent(args.fuel),
        parse_amount(args.cod_fixed, allow_zero=True),
        parse_percent(args.cod_pct), parse_percent(args.gst),
        effective=args.effective)
    print("%s rate card for %s: first %dg at %s, then %s per %dg, fuel "
          "%s%%, COD %s + %s%%, GST %s%% (effective %s)" % (
              "replaced" if existed else "recorded", args.name,
              card["base_weight_g"], format_paise(card["base_price"]),
              format_paise(card["slab_price"]), card["slab_g"],
              args.fuel, format_paise(card["cod_fixed"]), args.cod_pct,
              args.gst, card["effective"]))


def cmd_rate_show(book, args):
    card = book.get_card(args.name)
    print("%s rate card (effective %s):" % (args.name, card["effective"]))
    print("  first %d g: %s" % (card["base_weight_g"],
                                format_paise(card["base_price"])))
    print("  each additional %d g (or part): %s"
          % (card["slab_g"], format_paise(card["slab_price"])))
    print("  fuel surcharge: %.2f%% of freight" % (card["fuel_bp"] / 100))
    print("  COD: %s + %.2f%% of COD amount"
          % (format_paise(card["cod_fixed"]), card["cod_bp"] / 100))
    print("  GST: %.2f%% on freight + fuel + COD" % (card["gst_bp"] / 100))


def _print_cost_line(prefix, cost):
    print("%sfreight %s + fuel %s + COD %s + GST %s = %s" % (
        prefix, format_paise(cost["freight"]), format_paise(cost["fuel"]),
        format_paise(cost["cod"]), format_paise(cost["gst"]),
        format_paise(cost["total"])))


def cmd_compare(book, args):
    weight = parse_weight(args.weight)
    cod = parse_amount(args.cod, allow_zero=True)
    rows = book.compare(weight, cod)
    if not rows:
        print("no rate cards recorded yet")
        return
    label = "%d g%s - all-in cost by courier:" % (
        weight, ", COD %s" % format_paise(cod) if cod else "")
    print(label)
    rank = 1
    for name, cost in rows:
        _print_cost_line("  %d. %-22s " % (rank, name), cost)
        rank += 1


def cmd_audit(book, args):
    weight = parse_weight(args.weight)
    cod = parse_amount(args.cod, allow_zero=True)
    charged = parse_amount(args.charged)
    audit = book.record_audit(args.name, args.date, weight, charged, cod)
    diff = audit["diff"]
    if diff == 0:
        verdict = "matches the rate card exactly"
    elif diff > 0:
        verdict = "OVER-BILLED by %s vs the card" % format_paise(diff)
    else:
        verdict = "under-billed by %s vs the card" % format_paise(-diff)
    print("audit %s on %s (%d g%s): charged %s, expected %s - %s" % (
        args.name, args.date, weight,
        ", COD %s" % format_paise(cod) if cod else "",
        format_paise(charged), format_paise(audit["expected"]), verdict))


def cmd_audits(book, args):
    audits = book.audits
    if args.name:
        book.get_courier(args.name)
        audits = [a for a in audits if a["courier"] == args.name]
    if not audits:
        print("no audits recorded yet")
        return
    for a in audits:
        diff = a["diff"]
        mark = "ok" if diff == 0 else (
            "OVER +%s" % format_paise(diff) if diff > 0
            else "under -%s" % format_paise(-diff))
        print("  %-22s %s  %6d g  charged %12s  expected %12s  %s" % (
            a["courier"], a["date"], a["weight_g"],
            format_paise(a["charged"]), format_paise(a["expected"]), mark))


def cmd_export(book, args):
    book.export_csv(args.path)
    print("wrote %s" % args.path)


def cmd_summary(book, args):
    s = book.summary()
    print("couriers: %d (%d with rate cards), invoices audited: %d "
          "(over-billed %d, under-billed %d)"
          % (s["couriers"], s["with_cards"], s["audits"], s["over"],
             s["under"]))


def build_parser():
    parser = argparse.ArgumentParser(
        prog="rate-book",
        description="Courier rate-card comparison and invoice audit: the "
                    "true all-in cost of a shipment, and whether the "
                    "invoice matches the card.")
    parser.add_argument("--db", default=DEFAULT_DB,
                        help="JSON data file (default: %(default)s)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("courier", help="courier add")
    p.add_argument("name")
    p.add_argument("--contact", default=None)
    p.set_defaults(func=cmd_courier_add)

    p = sub.add_parser("rate", help="rate card set/show")
    rate_sub = p.add_subparsers(dest="rate_cmd", required=True)
    ps = rate_sub.add_parser("set", help="record or replace a rate card")
    ps.add_argument("name")
    ps.add_argument("--base-weight", required=True,
                    help="grams covered by the base price, e.g. 500")
    ps.add_argument("--base-price", required=True,
                    help="price for the base weight, in rupees")
    ps.add_argument("--slab", required=True,
                    help="slab size in grams beyond base weight, e.g. 500")
    ps.add_argument("--slab-price", required=True,
                    help="price per started slab, in rupees")
    ps.add_argument("--fuel", required=True,
                    help="fuel surcharge percent of freight, e.g. 15")
    ps.add_argument("--cod-fixed", default="0",
                    help="fixed COD fee in rupees (default 0)")
    ps.add_argument("--cod-pct", required=True,
                    help="COD percent of the COD amount, e.g. 2")
    ps.add_argument("--gst", required=True, help="GST percent, e.g. 18")
    ps.add_argument("--effective", default=None,
                    help="effective date YYYY-MM-DD (default today)")
    ps.set_defaults(func=cmd_rate_set)
    pw = rate_sub.add_parser("show", help="show one courier's rate card")
    pw.add_argument("name")
    pw.set_defaults(func=cmd_rate_show)

    p = sub.add_parser("compare", help="rank couriers for a shipment")
    p.add_argument("weight", help="parcel weight in grams")
    p.add_argument("--cod", default="0",
                   help="COD amount to collect, in rupees")
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("audit", help="check a courier invoice against the card")
    p.add_argument("name")
    p.add_argument("date", help="invoice date, YYYY-MM-DD")
    p.add_argument("weight", help="shipped weight in grams")
    p.add_argument("charged", help="total the courier charged, in rupees")
    p.add_argument("--cod", default="0", help="COD amount, in rupees")
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("audits", help="list recorded audits")
    p.add_argument("name", nargs="?", default=None)
    p.set_defaults(func=cmd_audits)

    p = sub.add_parser("export", help="export audits to CSV")
    p.add_argument("path")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("summary", help="one-line totals")
    p.set_defaults(func=cmd_summary)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        book = Book.load(args.db)
        args.func(book, args)
        book.save(args.db)
    except RateBookError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
