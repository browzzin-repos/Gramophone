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
