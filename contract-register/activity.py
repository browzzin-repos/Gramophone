"""Supplier activity from the Medius invoice history, for deciding which agreements still matter.

Usage:
    python activity.py "<path to invoices.json>"

invoices.json is the Medius export prepared for the OneTeam AP tool
(OneDrive: Claude Backup/Documents-Claude/AP Approval Tool/prepared/invoices.json).
Writes data/activity_suppliers.csv: one row per supplier with invoice count, total, last invoice date and
the total for the last 12 months. `extract.py plan` reads it (with data/activity_customers.csv) to mark each
counterparty active or dormant. Field names are detected, so small changes to the export do not matter.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import re
import sys
from pathlib import Path

OUT = Path(__file__).parent / "data" / "activity_suppliers.csv"


def records(data):
    """Find the list of invoice records, wherever it sits in the JSON."""
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data
    if isinstance(data, dict):
        for v in data.values():
            r = records(v)
            if r:
                return r
    return []


def flat(d: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(flat(v, key + "."))
        else:
            out[key] = v
    return out


def pick(keys, *patterns):
    for pat in patterns:
        for k in keys:
            if re.search(pat, k, re.I):
                return k
    return None


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    src = Path(sys.argv[1]).expanduser()
    rows = [flat(r) for r in records(json.loads(src.read_text()))]
    if not rows:
        print("No invoice records found in that file.")
        return 1
    keys = list({k for r in rows[:200] for k in r})
    name_k = pick(keys, r"^supplier\.name$", r"supplier.?name", r"vendor.?name", r"^supplier$", r"^name$")
    code_k = pick(keys, r"^supplier\.code$", r"supplier.?code", r"vendor.?code", r"supplier.?id")
    date_k = pick(keys, r"^invoice.?date$", r"invoice.?date", r"doc.*date", r"^date$")
    amt_k = pick(keys, r"^total$", r"total.?(incl|gross|amount)", r"^gross", r"total", r"^amount$", r"amount")
    cur_k = pick(keys, r"^currency$", r"currency")
    print(f"{len(rows):,} invoices. Using fields: supplier={name_k}, code={code_k}, date={date_k}, amount={amt_k}, currency={cur_k}")
    if not (name_k or code_k) or not date_k:
        print("Could not find the supplier or date fields. Field names in the file:\n  " + ", ".join(sorted(keys)))
        return 1
    cutoff = (dt.date.today() - dt.timedelta(days=365)).isoformat()
    agg: dict[str, dict] = {}
    for r in rows:
        key = str(r.get(code_k) or r.get(name_k) or "").strip()
        if not key:
            continue
        a = agg.setdefault(key, {"name": r.get(name_k) or key, "codes": r.get(code_k) or "", "n": 0, "total": 0.0,
                                 "last": "", "total_12m": 0.0, "currency": set()})
        d = str(r.get(date_k) or "")[:10]
        try:
            amt = float(str(r.get(amt_k) or 0).replace(",", "")) if amt_k else 0.0
        except ValueError:
            amt = 0.0
        a["n"] += 1
        a["total"] += amt
        a["last"] = max(a["last"], d)
        if d >= cutoff:
            a["total_12m"] += amt
        if cur_k and r.get(cur_k):
            a["currency"].add(str(r[cur_k]))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["name", "codes", "last_date", "amount_12m", "source", "invoices", "amount_total", "currency"])
        for a in sorted(agg.values(), key=lambda a: -a["total_12m"]):
            w.writerow([a["name"], a["codes"], a["last"], round(a["total_12m"]), "Medius invoices", a["n"], round(a["total"]),
                        " ".join(sorted(a["currency"]))])
    print(f"{len(agg)} suppliers -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
