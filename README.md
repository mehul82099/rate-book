# rate-book

A zero-dependency Python 3.9+ CLI for small businesses to record the courier
rate cards they have negotiated, compare couriers on an all-in cost for a
shipment, and audit courier invoices against the card they were supposed to
be billed on.

## The itch

From Razorpay's "Fix My Itch" list (https://razorpay.com/m/fix-my-itch/),
Logistics category: **"Why can't businesses compare courier rates easily?"**
(itch score 70). Verified against the live page on 2026-11-25. The list is a
curated set of problems people submitted; appearing on it is not proof of
market demand.

## What it does

- Record your couriers and the rate card each one quoted you: base weight
  slab and price, per-slab increments, fuel surcharge %, COD fixed fee + %,
  and GST %.
- `compare` ranks every courier with a card on the full all-in cost for a
  weight (optionally with a COD amount): freight + fuel + COD + GST, so a
  cheaper-looking base rate does not hide an expensive surcharge.
- `audit` checks an actual charged invoice against the recorded card and
  reports exact / over-billed / under-billed with the difference.
- `audits`, `summary` and `export` give you the running audit log, totals,
  and a CSV for your accountant.

Amounts are stored as integer paise and percentages as basis points, so
there is no floating-point drift in the money math.

## Landscape (checked 2026-11-25)

Courier aggregators like Shiprocket, Pickrr and Delhivery already compare
*their own* network rates and book shipments, and invoice-audit services
(ShipJasoos, Netray, deltarate, auditmy3pl and similar) audit freight bills
as a paid service. rate-book is neither: it is a small offline record of the
cards *you* negotiated and the invoices *you* received, with no account, no
per-shipment fee and no data leaving your machine. If you want live booking
or network rates, use an aggregator.

## Install and run

Needs Python 3.9 or newer. Nothing else.

```console
$ python3 rate_book.py courier SWIFTAIR
added courier SWIFTAIR
$ python3 rate_book.py courier CHEAPEX
added courier CHEAPEX
$ python3 rate_book.py rate set SWIFTAIR --base-weight 500 --base-price 65 --slab 500 --slab-price 40 --fuel 15 --cod-fixed 35 --cod-pct 2 --gst 18
recorded rate card for SWIFTAIR: first 500g at 65.00, then 40.00 per 500g, fuel 15%, COD 35.00 + 2%, GST 18% (effective 2026-11-25)
$ python3 rate_book.py rate set CHEAPEX --base-weight 500 --base-price 55 --slab 500 --slab-price 45 --fuel 10 --cod-pct 1 --gst 18
recorded rate card for CHEAPEX: first 500g at 55.00, then 45.00 per 500g, fuel 10%, COD 0.00 + 1%, GST 18% (effective 2026-11-25)
$ python3 rate_book.py rate show SWIFTAIR
SWIFTAIR rate card (effective 2026-11-25):
  first 500 g: 65.00
  each additional 500 g (or part): 40.00
  fuel surcharge: 15.00% of freight
  COD: 35.00 + 2.00% of COD amount
  GST: 18.00% on freight + fuel + COD
$ python3 rate_book.py compare 1200 --cod 1000
1200 g, COD 1,000.00 - all-in cost by courier:
  1. CHEAPEX                freight 145.00 + fuel 14.50 + COD 10.00 + GST 30.51 = 200.01
  2. SWIFTAIR               freight 145.00 + fuel 21.75 + COD 55.00 + GST 39.92 = 261.67
$ python3 rate_book.py audit CHEAPEX 2026-11-24 1200 200.01 --cod 1000
audit CHEAPEX on 2026-11-24 (1200 g, COD 1,000.00): charged 200.01, expected 200.01 - matches the rate card exactly
$ python3 rate_book.py audit SWIFTAIR 2026-11-24 1200 275 --cod 1000
audit SWIFTAIR on 2026-11-24 (1200 g, COD 1,000.00): charged 275.00, expected 261.67 - OVER-BILLED by 13.33 vs the card
$ python3 rate_book.py audits
  CHEAPEX                2026-11-24    1200 g  charged       200.01  expected       200.01  ok
  SWIFTAIR               2026-11-24    1200 g  charged       275.00  expected       261.67  OVER +13.33
$ python3 rate_book.py summary
couriers: 2 (2 with rate cards), invoices audited: 2 (over-billed 1, under-billed 0)
$ python3 rate_book.py export /tmp/audits.csv
wrote /tmp/audits.csv
```

`rate set` again for the same courier replaces the card (use it when a
courier revises rates). Data lives in a JSON file (`--db PATH`, default
`rate_book.json` in the current directory).

## Commands

```
courier NAME [--contact TEXT]        add a courier
rate set NAME --base-weight G --base-price AMT --slab G --slab-price AMT
                                     [--fuel PCT] [--cod-fixed AMT] [--cod-pct PCT]
                                     [--gst PCT] [--effective YYYY-MM-DD]
rate show NAME                       print one courier's card
compare WEIGHT_G [--cod AMT]         rank couriers on all-in cost
audit NAME DATE WEIGHT_G CHARGED [--cod AMT]
                                     check an invoice against the card
audits                               list the audit log
summary                              courier and audit totals
export FILE.csv                      export the audit log
```

Amounts are in rupees with up to 2 decimals; weights are integer grams.

## Limits

- One card per courier, single zone: no origin/destination or pincode zone
  matrix, no dimensional/volumetric weight, no reverse or RTO charges.
  Cards with zones do not fit; you would need one record per zone yourself.
- It records and checks the card you typed in. It does not fetch live
  courier rates and does not advise which courier to contract.
- Fuel, COD and GST are each rounded to the nearest paisa per component;
  a courier that rounds differently can differ by a paisa or two.
- Durability is limited to the tested behavior: saves are an atomic
  file replace (write to temp file, then rename). The audit log is not an
  accounting system of record.

## Tests

```
python3 -m unittest test_rate_book -v
```

30 tests cover parsing, slab ceiling math, fuel/COD/GST integer math,
compare ranking, audit over/under/exact, card replacement, storage
round-trip and atomic save, CLI flows and error exit codes. CI runs them on
ubuntu-24.04 and windows-latest against Python 3.9 and 3.13.

## License

MIT (c) 2026 Mehul Jhabak. See LICENSE.
