"""Compute renewal/notice dates for every extracted record and render the dashboard.

Usage:
    python build.py [--today YYYY-MM-DD] [--out out/]

Reads data/records/*.json, writes:
    out/contracts.json   - records plus computed fields (the register)
    out/index.html       - the dashboard, with the register embedded
"""
from __future__ import annotations

import argparse
import calendar
import datetime as dt
import json
from pathlib import Path

HERE = Path(__file__).parent

# Indicative rates for the AUD roll-up only. Update monthly from finance's rate table.
FX_TO_AUD = {"AUD": 1.0, "USD": 1.53, "NZD": 0.91, "EUR": 1.66, "GBP": 1.95, "JPY": 0.0102, "CNY": 0.21}

# Internal lead time before a term end when nothing in the contract forces a date.
RENEGOTIATE_LEAD_DAYS = 60


def d(s: str | None) -> dt.date | None:
    return dt.date.fromisoformat(s) if s else None


def add_months(day: dt.date, months: int) -> dt.date:
    m = day.month - 1 + months
    y, m = day.year + m // 12, m % 12 + 1
    return dt.date(y, m, min(day.day, calendar.monthrange(y, m)[1]))


def derive(rec: dict, today: dt.date) -> dict:
    end = d(rec.get("term_end"))
    start = d(rec.get("term_start"))
    rtype = rec["renewal_type"]
    notice = rec.get("notice_period_days")
    notes: list[str] = []

    # An auto-renewing contract whose term has passed has (presumably) rolled over.
    if rtype == "auto_renew" and end and end < today and rec.get("renewal_term_months"):
        while end < today:
            start = end + dt.timedelta(days=1)
            end = add_months(start, rec["renewal_term_months"]) - dt.timedelta(days=1)
        notes.append("Term rolled forward by the auto-renewal clause (computed, not from a document).")

    deadlines: list[dict] = []
    if end:
        if rtype == "auto_renew":
            if notice is not None:
                deadlines.append({"kind": "Cancel by", "date": end - dt.timedelta(days=notice),
                                  "why": f"{notice} days' written notice before it renews on {(end + dt.timedelta(days=1)).strftime('%-d %b %Y')}"})
            else:
                deadlines.append({"kind": "Confirm notice period", "date": end,
                                  "why": "Auto-renews; notice period not in the documents"})
        elif rtype == "option_to_extend":
            if notice is not None:
                deadlines.append({"kind": "Extend by", "date": end - dt.timedelta(days=notice),
                                  "why": f"{notice} days' notice to exercise the extension option"})
            deadlines.append({"kind": "Term ends", "date": end, "why": "Services stop unless extended"})
        elif rtype == "mutual_agreement":
            deadlines.append({"kind": "Renegotiate by", "date": end - dt.timedelta(days=RENEGOTIATE_LEAD_DAYS),
                              "why": f"Renews only by written agreement; {RENEGOTIATE_LEAD_DAYS}-day internal lead time"})
            deadlines.append({"kind": "Term ends", "date": end, "why": "No automatic renewal"})
        elif rtype in ("fixed_term", "unclear"):
            deadlines.append({"kind": "Term ends" if rtype == "fixed_term" else "Confirm renewal terms",
                              "date": end, "why": "End of current term"})
    ee = d(rec.get("early_exit_deadline"))
    if ee:
        deadlines.append({"kind": "Free exit closes", "date": ee, "why": rec.get("early_exit_summary") or "Early exit window"})

    upcoming = sorted((x for x in deadlines if x["date"] >= today), key=lambda x: x["date"])
    nxt = upcoming[0] if upcoming else None
    lapsed = bool(end and end < today)

    if lapsed:
        status = "Term ended"
    elif rtype == "unclear":
        status = "Needs review"
    elif nxt and (nxt["date"] - today).days <= 45:
        status = "Act now"
    elif nxt and (nxt["date"] - today).days <= 120:
        status = "Due soon"
    else:
        status = "Active"

    aud = None
    if rec.get("annual_value") is not None and rec.get("currency") in FX_TO_AUD:
        aud = round(rec["annual_value"] * FX_TO_AUD[rec["currency"]])

    return {
        "current_term_start": start.isoformat() if start else None,
        "current_term_end": end.isoformat() if end else None,
        "renewal_date": (end + dt.timedelta(days=1)).isoformat() if end and rtype in ("auto_renew", "option_to_extend") else None,
        "deadlines": [{**x, "date": x["date"].isoformat(), "days": (x["date"] - today).days} for x in sorted(deadlines, key=lambda x: x["date"])],
        "next_deadline": {**nxt, "date": nxt["date"].isoformat(), "days": (nxt["date"] - today).days} if nxt else None,
        "status": status,
        "annual_value_aud": aud,
        "computed_notes": notes,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--today", default=dt.date.today().isoformat())
    ap.add_argument("--records", type=Path, default=HERE / "data" / "records")
    ap.add_argument("--out", type=Path, default=HERE / "out")
    args = ap.parse_args()
    today = dt.date.fromisoformat(args.today)

    records = []
    for f in sorted(args.records.glob("*.json")):
        rec = json.loads(f.read_text())
        rec["computed"] = derive(rec, today)
        records.append(rec)

    order = {"Term ended": 0, "Act now": 1, "Needs review": 2, "Due soon": 3, "Active": 4}
    records.sort(key=lambda r: (order[r["computed"]["status"]],
                                r["computed"]["next_deadline"]["days"] if r["computed"]["next_deadline"] else 10**6))

    payload = {"generated": today.isoformat(), "fx_to_aud": FX_TO_AUD, "contracts": records}
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "contracts.json").write_text(json.dumps(payload, indent=2))

    template = (HERE / "template.html").read_text()
    data = json.dumps(payload).replace("</", "<\\/")
    (args.out / "index.html").write_text(template.replace("/*__REGISTER_DATA__*/null", data))
    print(f"{len(records)} contracts -> {args.out / 'index.html'}")


if __name__ == "__main__":
    main()
