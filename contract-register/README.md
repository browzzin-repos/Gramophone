# 2XU Contract Register

This pipeline reads every agreement in SharePoint `Accounts/Contracts` (all areas), groups the documents by counterparty, uses the Claude API to turn each counterparty's documents into one term sheet, works out every renewal and notice date, and renders a dashboard.

```
Local copy of Accounts/Contracts (download or OneDrive sync)
  -> extract.py plan      group files by counterparty -> data/manifest.json
  -> extract.py estimate  token count and cost quote (free)
  -> extract.py run       Claude API (claude-opus-5-5): one request per counterparty, online terms fetched
  -> data/records/<id>.json  one term sheet per counterparty, with verbatim evidence quotes
  -> build.py             date arithmetic, status, AUD roll-up
  -> out/index.html + out/contracts.json
```

## Run it

```bash
pip install anthropic pydantic
export ANTHROPIC_API_KEY=...                       # or: ant auth login

python extract.py plan ~/2XU-agreements/Contracts \
    --extra "~/2XU-agreements/Agreements & Contracts" \
    --extra "~/2XU-agreements/Distributors Agreements & Letters of Credit"
python extract.py estimate
python extract.py run --max-usd 50
python build.py
open out/index.html
```

- **One request per counterparty.** Every PDF and Word document for a counterparty goes into one request: MSA, order forms, SOWs, amendments, side letters, renewals. The model works out which documents govern today and how they fit together.
- **Grouping.** Each sub-folder of an area (`Suppliers/Klaviyo`, `IT and Systems/BambooRose`) is a counterparty. Folders with the same name in different areas are merged, and loose files join a folder with the same name. Folders kept outside Contracts (AP agreements, distributor agreements, leases, sponsorships) are added with `--extra`: each sub-folder or loose file in them is a counterparty, merged with a Contracts folder of the same name. Individually downloaded signed copies can go in one extra folder; name a sub-folder after the counterparty when the file name doesn't start with it. `data/grouping.json` overrides the grouping: `{"merge": {"planet6": ["Suppliers/Planet6", "IT and Systems/Redfig"]}, "split": ["Sponsorship/USA & CA"], "exclude": ["Payroll"], "names": {"path": "Display name"}}`. Re-run `plan` after changing it.
- **Online terms.** Where a signed document incorporates terms published at a URL, the model fetches them with the web fetch tool and uses them. Each fetched page is saved under `data/terms/<id>/` with its retrieval date. These are the current published terms, which may differ from the version in force when 2XU signed.
- **Term sheet.** Term, renewal, notice and how to give it, every exit route, termination by the other party, payment days and frequency, minimum commitment, price and unilateral change rights, how to amend, liability cap, indemnities, assignment and change of control, exclusivity, SLAs, IP, data, confidentiality, insurance, governing law, document precedence, and documents referred to but missing.
- **Resumable, with a spend cap.** Existing records are skipped; `--force` re-extracts and `--only "Klaviyo,FloQast"` targets counterparties. `--max-usd` (default 50) stops new counterparties once the run's estimated spend reaches the cap. `--no-web` skips online terms.
- **Dates are computed in code.** The model reports term dates and notice periods; `build.py` calculates the cancel-by, extend-by and renegotiate-by dates, so the arithmetic is deterministic.
- **Evidence.** Every term date, renewal rule, notice period, termination right, payment term, price-change right and liability cap carries a verbatim quote with file name and page.
- **Fallbacks.** Server-side refusal fallbacks are switched on. If a safety classifier declines a request, it is re-run on a fallback model.
- **Formats.** PDF and Word (.docx) are read. Other files (.doc, .xlsx, .msg, images, links) are listed on the record as not read.

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
