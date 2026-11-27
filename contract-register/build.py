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
import re
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

    # Start and length known but no end date written: derive it.
    if not end and start and rec.get("initial_term_months"):
        end = add_months(start, rec["initial_term_months"]) - dt.timedelta(days=1)
        notes.append("Term end computed from the start date and initial term (not stated in a document).")

    # An auto-renewing contract whose term has passed has (presumably) rolled over.
    if rtype == "auto_renew" and end and end < today and rec.get("renewal_term_months"):
        while end < today:
            start = end + dt.timedelta(days=1)
            end = add_months(start, rec["renewal_term_months"]) - dt.timedelta(days=1)
        notes.append("Term rolled forward by the auto-renewal clause (computed, not from a document).")
    # If this term's cancel-by date has already passed, the next decision is for the following term.
    if rtype == "auto_renew" and end and notice is not None and rec.get("renewal_term_months"):
        while end - dt.timedelta(days=notice) < today:
            start = end + dt.timedelta(days=1)
            end = add_months(start, rec["renewal_term_months"]) - dt.timedelta(days=1)
            if "Cancel-by date for this term has passed; showing the next term." not in notes:
                notes.append("Cancel-by date for this term has passed; showing the next term.")

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


ACTION_TITLE = {
    "Cancel by": "Decide: cancel or let it renew",
    "Extend by": "Decide: extend or let it lapse",
    "Renegotiate by": "Start the renewal negotiation",
    "Free exit closes": "Decide: keep it or use the free exit",
    "Term ends": "Plan for the term ending",
    "Confirm notice period": "Confirm the notice period",
    "Confirm renewal terms": "Confirm the renewal terms",
}
PERIODS = {"month": 12, "quarter": 4, "semi": 2, "annual": 1, "year": 1}  # order matters: "semi" before "annual"


def actions(rec: dict, manual: list[dict], today: dt.date) -> list[dict]:
    """Action inbox: dated decisions from deadlines, high risks, and manual follow-ups."""
    out = []
    upcoming = [x for x in rec["computed"]["deadlines"] if x["days"] >= 0]
    for dl in upcoming:
        if dl["days"] > 180 or (dl["kind"] == "Term ends" and len(upcoming) > 1):
            continue
        out.append({"title": ACTION_TITLE.get(dl["kind"], dl["kind"]), "detail": dl["why"], "due": dl["date"],
                    "days": dl["days"], "severity": "high" if dl["days"] <= 45 else "medium", "source": "deadline"})
    # High risks become undated actions, unless a dated or manual action already covers them.
    dated = [dt.date.fromisoformat(x["due"]).strftime("%-d %b %Y") for x in out]
    covered = " ".join(a["detail"] for a in manual)
    for r in rec.get("risks", []):
        issue = r["issue"]
        if r["severity"] != "high" or any(x in issue for x in dated):
            continue
        if any(tok in covered for tok in issue.split() if len(tok) > 8 and tok[0].isdigit()):
            continue
        title, _, rest = issue.partition(". ")
        out.append({"title": title.rstrip("."), "detail": rest, "due": None, "days": None,
                    "severity": "high", "source": "risk"})
    for a in manual:
        due = d(a.get("due"))
        out.append({"title": a["title"], "detail": a.get("detail", ""), "due": a.get("due"),
                    "days": (due - today).days if due else None, "severity": "high", "source": "manual"})
    return out


def health(rec: dict) -> dict:
    """Record completeness: what a contract needs before finance can rely on it."""
    c = rec["computed"]
    checks = [
        ("Signed copy on file", any(x["signed"] for x in rec["documents"])),
        ("Renewal terms clear", rec["renewal_type"] != "unclear"),
        ("Current term in date", c["status"] != "Term ended"),
        ("Notice or end date known", bool(c["deadlines"]) or (rec["renewal_type"] == "evergreen" and rec.get("notice_period_days") is not None)),
        ("Exit routes captured", bool(rec.get("exit_routes"))),
        ("AP21 supplier linked", bool(rec.get("ap21_supplier_code"))),
        ("Owner confirmed", bool(rec.get("owner")) and rec.get("owner_status") == "confirmed"),
    ]
    return {"score": sum(ok for _, ok in checks), "total": len(checks), "checks": [{"label": l, "ok": ok} for l, ok in checks]}


def ap_terms(rec: dict) -> dict:
    """What AP should expect on an invoice from this supplier, and when to hold it."""
    c = rec["computed"]
    bf = (rec.get("billing_frequency") or "").lower()
    n = next((v for k, v in PERIODS.items() if k in bf), None)
    expected = round(rec["annual_value"] / n, 2) if n and rec.get("annual_value") else None
    variable = any(w in rec["fee_summary"].lower() for w in ("estimat", "consumption", "usage", "overage"))
    holds = []
    if c["status"] == "Term ended":
        holds.append(f"Term ended {d(c['current_term_end']).strftime('%-d %b %Y')}: no contract on file covers later invoices")
    if rec["renewal_type"] == "unclear" and c["status"] != "Term ended":
        holds.append("No signed terms on file: confirm before approving increases")
    if not rec.get("ap21_supplier_code"):
        holds.append("No AP21 supplier record")
    return {
        "expected_invoice": expected, "currency": rec.get("currency"), "billing_frequency": rec.get("billing_frequency"),
        "tolerance_pct": 20 if variable else 5, "variable": variable, "payment_terms": rec.get("payment_terms"),
        "valid_from": c["current_term_start"], "valid_to": c["current_term_end"], "holds": holds,
    }


STOP = {"pty", "ltd", "limited", "inc", "llc", "group", "the", "and", "co", "of", "gmbh", "sa", "ab", "plc"}


def norm(name: str) -> str:
    return "".join(w for w in re.findall(r"[a-z0-9]+", (name or "").lower().replace("&", " and ")) if w not in STOP)


def supersede(records: list[dict]) -> None:
    """Several records for one counterparty (yearly athlete deals, an old SOW and its replacement): the most recent is
    live; an older one is marked Superseded when its term has ended or it starts and ends no later than the live one.
    Parallel agreements that run past the live one stay as they are."""
    groups: dict[str, list[dict]] = {}
    for r in records:
        groups.setdefault(norm(r["counterparty"]), []).append(r)
    for rs in groups.values():
        if len(rs) < 2:
            continue
        start = lambda r: r["computed"]["current_term_start"] or r.get("effective_date") or ""
        end = lambda r: r["computed"]["current_term_end"] or ""
        primary = max(rs, key=lambda r: (start(r), end(r), any(d["signed"] for d in r["documents"]), len(r["documents"])))
        for r in rs:
            if r is primary:
                continue
            if r["computed"]["status"] == "Term ended" or (start(r) <= start(primary) and end(r) <= end(primary)):
                c = r["computed"]
                c["status"] = "Superseded"
                c["superseded_by"] = primary["id"]
                c["actions"] = []
                c["computed_notes"].append(f"Superseded by the later record for {primary['counterparty']} ({primary['id']}).")


def write_ics(records: list[dict], path: Path) -> None:
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//2XU//Contract Register//EN", "X-WR-CALNAME:2XU contract deadlines"]
    for rec in records:
        for dl in rec["computed"]["deadlines"]:
            if dl["days"] < 0:
                continue
            day = dl["date"].replace("-", "")
            nxt = (dt.date.fromisoformat(dl["date"]) + dt.timedelta(days=1)).strftime("%Y%m%d")
            owner = rec.get("owner") or "Unassigned"
            lines += ["BEGIN:VEVENT", f"UID:{rec['id']}-{day}-{dl['kind'].replace(' ', '')}@contract-register",
                      f"DTSTAMP:{day}T000000Z", f"DTSTART;VALUE=DATE:{day}", f"DTEND;VALUE=DATE:{nxt}",
                      f"SUMMARY:{rec['counterparty']}: {dl['kind']}",
                      f"DESCRIPTION:{dl['why']}. Owner: {owner}.".replace(",", "\\,"),
                      "BEGIN:VALARM", "ACTION:DISPLAY", "DESCRIPTION:Contract deadline in 30 days", "TRIGGER:-P30D", "END:VALARM",
                      "BEGIN:VALARM", "ACTION:DISPLAY", "DESCRIPTION:Contract deadline in 7 days", "TRIGGER:-P7D", "END:VALARM",
                      "END:VEVENT"]
    lines.append("END:VCALENDAR")
    path.write_text("\r\n".join(lines) + "\r\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--today", default=dt.date.today().isoformat())
    ap.add_argument("--records", type=Path, default=HERE / "data" / "records")
    ap.add_argument("--out", type=Path, default=HERE / "out")
    args = ap.parse_args()
    today = dt.date.fromisoformat(args.today)

    codes_file = args.records.parent / "ap21_suppliers.json"
    codes = json.loads(codes_file.read_text()) if codes_file.exists() else {}

    ov_file = args.records.parent / "overrides.json"
    overrides = json.loads(ov_file.read_text()) if ov_file.exists() else {}

    records = []
    for f in sorted(args.records.glob("*.json")):
        rec = json.loads(f.read_text())
        if rec["id"] in codes:
            rec["ap21_supplier_code"] = codes[rec["id"]].get("code")
            rec["ap21_supplier_name"] = codes[rec["id"]].get("name")
        ov = overrides.get(rec["id"], {})
        rec["owner"], rec["owner_status"] = ov.get("owner"), ov.get("owner_status", "proposed")
        rec["computed"] = derive(rec, today)
        rec["computed"]["actions"] = actions(rec, ov.get("actions", []), today)
        rec["computed"]["health"] = health(rec)
        rec["computed"]["ap"] = ap_terms(rec)
        records.append(rec)

    supersede(records)
    # Contracts that ended more than a year ago are history: keep the record, drop its actions.
    year_ago = (today - dt.timedelta(days=365)).isoformat()
    for r in records:
        c = r["computed"]
        c["current"] = c["status"] != "Superseded" and not (c["status"] == "Term ended" and (c["current_term_end"] or "") < year_ago)
        if not c["current"]:
            c["actions"] = [a for a in c["actions"] if a["source"] == "manual"]
    order = {"Term ended": 0, "Act now": 1, "Needs review": 2, "Due soon": 3, "Active": 4, "Superseded": 5}
    records.sort(key=lambda r: (order[r["computed"]["status"]],
                                r["computed"]["next_deadline"]["days"] if r["computed"]["next_deadline"] else 10**6))

    payload = {"generated": today.isoformat(), "fx_to_aud": FX_TO_AUD, "contracts": records}
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "contracts.json").write_text(json.dumps(payload, indent=2))

    # Lookup for the AP tool: one row per supplier code, what an invoice should look like.
    ap = [{"ap21_supplier_code": r.get("ap21_supplier_code"), "supplier": r["counterparty"], "contract_id": r["id"],
           "status": r["computed"]["status"], **r["computed"]["ap"]} for r in records]
    (args.out / "ap_contract_terms.json").write_text(json.dumps(ap, indent=2))
    write_ics(records, args.out / "contract-deadlines.ics")

    template = (HERE / "template.html").read_text()
    data = json.dumps(payload).replace("</", "<\\/")
    (args.out / "index.html").write_text(template.replace("/*__REGISTER_DATA__*/null", data))
    print(f"{len(records)} contracts -> {args.out / 'index.html'}")


if __name__ == "__main__":
    main()
