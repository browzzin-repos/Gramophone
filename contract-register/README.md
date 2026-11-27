# 2XU Contract Register

This pipeline reads the signed contracts in SharePoint `Accounts/Contracts`, uses the Claude API to pull each one into a structured record, works out every renewal and notice date, and renders a dashboard.

```
SharePoint folder (OneDrive sync)
  -> extract.py  Claude API (claude-opus-5-5, structured output against schema.py)
  -> data/records/<counterparty>.json  one record per counterparty, with verbatim evidence quotes
  -> build.py  date arithmetic, status, AUD roll-up
  -> out/index.html + out/contracts.json
```

## Run it

```bash
pip install anthropic pydantic
export ANTHROPIC_API_KEY=...                       # or: ant auth login

# Sync "Accounts/Contracts/Suppliers" with OneDrive, then:
python extract.py "~/2XU/Company - Documents/Accounts/Contracts/Suppliers" --limit 5
python build.py
open out/index.html
```

- **One request per counterparty folder.** All the PDFs in a folder (original order, amendments, renewals) go into the same request, so the model can work out which document governs today. Loose PDFs count as one contract each.
- **Resumable.** Existing records are skipped. Use `--force` to re-extract and `--only Klaviyo,FloQast` to target specific folders.
- **Dates are computed in code.** The model reports term dates and notice periods; `build.py` calculates the cancel-by, extend-by and renegotiate-by dates, so the arithmetic is deterministic.
- **Evidence.** Every term date, renewal rule, notice period and fee carries a verbatim quote, with file name and page, that you can check against the PDF.
- **Fallbacks.** Server-side refusal fallbacks are switched on. If a safety classifier declines a request, it is re-run on a fallback model.

## Not in git

`data/` and `out/` are git-ignored because they contain commercial terms. Keep them in SharePoint or another access-controlled location.

## Cost and time

Expect roughly 5–40 pages per counterparty. At Opus 5.5 pricing a typical supplier folder costs well under US$1. For the full back catalogue, the Message Batches API halves the cost.

## Status values

| Status | Meaning |
|---|---|
| Act now | A notice, extension or exit deadline is within 45 days |
| Term ended | The current term's end date has passed and no newer signed document is on file |
| Needs review | The renewal mechanism is unclear (the documents conflict, or renewal is set by online terms not on file) |
| Due soon | The next deadline is within 4 months |
| Active | Nothing due in the next 4 months |

## Outputs

| File | Use |
|---|---|
| `out/index.html` | Dashboard: overview, actions, calendar, contract detail |
| `out/contracts.json` | The register with computed dates, actions, record checks |
| `out/ap_contract_terms.json` | For the AP tool: per AP21 supplier code, the expected invoice amount, tolerance, payment terms, validity dates and hold reasons |
| `out/contract-deadlines.ics` | Calendar file of every upcoming deadline, with 30-day and 7-day reminders |

## Owners and manual follow-ups

`data/overrides.json` holds the contract owner and any manual actions per contract:
`{"slimstock": {"owner": "Name", "owner_status": "confirmed", "actions": [{"title": "...", "detail": "...", "due": "2026-11-30"}]}}`

## AP integration

`supabase_schema.sql` defines `contracts.contract`, `contracts.deadline`, `contracts.action` and the view
`contracts.ap_expected`. It has not been applied. The AP tool joins each invoice to `ap_expected` on the AP21
supplier code and flags: amount outside tolerance, invoice date outside the term, payment terms that differ,
or any hold reason.
