"""Extract structured contract records with the Claude API.

Usage:
    python extract.py <contracts_root> [--only Klaviyo,FloQast] [--limit 5] [--force]

<contracts_root> is a local copy of SharePoint `Accounts/Contracts/<area>` (e.g. the
OneDrive-synced `Suppliers` folder). Each sub-folder is treated as one counterparty and
all of its PDFs are sent together in one request, so amendments and renewals are read
alongside the original order. Loose PDFs in the root are treated as one contract each.

Output: data/records/<id>.json (one ContractRecord per counterparty). Existing records are
skipped unless --force, so the run can be resumed.

Auth: the SDK reads ANTHROPIC_API_KEY (or an `ant auth login` profile).
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import anthropic
from pydantic import ValidationError

from schema import Contract, ContractRecord

MODEL = "claude-opus-5-5"
MAX_REQUEST_BYTES = 28 * 1024 * 1024  # API limit is 32 MB per request; leave headroom for base64
OUT_DIR = Path(__file__).parent / "data" / "records"

SYSTEM = """You are a contracts analyst for 2XU, an Australian performance apparel company.
You read supplier, customer and partner agreements and produce one register record per
counterparty for the finance team's contract register.

How to read the bundle:
- The documents may include an original order, later amendments, renewals and drafts.
  Work out which document governs TODAY and report the CURRENT term, not the first one.
  Later signed amendments override earlier documents unless they say otherwise.
- Treat a document as signed only when its signature blocks are completed or an e-signature
  certificate shows completion. Unsigned versions and drafts are listed but do not govern.
- Report dates exactly as written, converted to YYYY-MM-DD. Do not compute notice deadlines;
  report the notice period in days and the code will do the arithmetic.
- If renewal or notice terms live in online terms that are not in the bundle, set
  renewal_type to "unclear" when the bundle itself does not settle it, and say so in
  open_questions.
- annual_value is recurring fees for 12 months excluding tax, in the contract currency.
  For usage-based or estimated fees, give the documented estimate and say it is an estimate
  in fee_summary.
- risks are things someone at 2XU should act on: auto-renewal at list price, a renewal or
  extension notice deadline, conflicting clauses between documents, a current term that has
  already ended with no newer order on file, a signatory or notice contact who may have left,
  lock-ins, hardware buyouts, uncapped price rises. Be specific and short.
- exit_routes lists every way 2XU can get out, with clause references: letting a
  non-renewing term lapse, early-exit windows, termination for convenience, exit on a price
  or terms change, termination for the vendor's uncured breach or insolvency, force
  majeure. Mark no_notice_period true only where 2XU can leave without serving a notice
  period (e.g. a term that simply ends, or immediate termination after a cure period).
  State exit costs (buyouts, remaining fees, third-party cancellation fees).
- Every term date, renewal rule, notice period, termination clause and fee must have an
  evidence quote copied verbatim from the documents.
- If something is not in the documents, leave it null. Never guess."""


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def collect_bundles(root: Path) -> list[tuple[str, list[Path]]]:
    bundles: list[tuple[str, list[Path]]] = []
    for entry in sorted(root.iterdir()):
        if entry.is_dir():
            pdfs = sorted(p for p in entry.rglob("*.pdf") if p.is_file())
            if pdfs:
                bundles.append((entry.name, pdfs))
        elif entry.suffix.lower() == ".pdf":
            bundles.append((entry.stem, [entry]))
    return bundles


def build_content(name: str, pdfs: list[Path], today: str) -> list[dict]:
    content: list[dict] = []
    total = 0
    skipped: list[str] = []
    # Newest-looking files last so the model reads the history in order.
    for pdf in pdfs:
        data = pdf.read_bytes()
        if total + len(data) * 4 // 3 > MAX_REQUEST_BYTES:
            skipped.append(pdf.name)
            continue
        total += len(data) * 4 // 3
        content.append({
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": base64.b64encode(data).decode()},
            "title": pdf.name,
        })
    note = f" These files were left out because the bundle is too large: {', '.join(skipped)}." if skipped else ""
    content.append({
        "type": "text",
        "text": (
            f"Today is {today}. The documents above are everything filed for counterparty folder "
            f"'{name}'.{note} Produce the register record."
        ),
    })
    return content


# Opus 5.5 list prices, US$ per million tokens (input, output). Used only for the cost printout.
PRICE_IN, PRICE_OUT = 4.00, 20.00

# The record schema is too large for API-side structured outputs ("Schema is too complex"), so the
# schema goes in the prompt and the reply is validated here with pydantic, with one correction round.
SCHEMA_TEXT = json.dumps(Contract.model_json_schema(), separators=(",", ":"))
FORMAT_NOTE = (
    "Reply with only the register record as one JSON object that validates against this JSON Schema. "
    "No prose and no code fences. Use null for anything the documents do not state.\n" + SCHEMA_TEXT
)


def _json_text(response) -> str:
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    return text[text.find("{"): text.rfind("}") + 1]


def extract_one(client: anthropic.Anthropic, name: str, pdfs: list[Path], today: str) -> tuple[ContractRecord, dict]:
    messages = [{"role": "user", "content": build_content(name, pdfs, today) + [{"type": "text", "text": FORMAT_NOTE}]}]
    usage = {"input": 0, "output": 0}
    for attempt in range(2):
        with client.beta.messages.stream(
            model=MODEL,
            max_tokens=32000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",  # re-run on a fallback model if a safety classifier declines
            thinking={"type": "adaptive"},
            output_config={"effort": "high"},
            system=SYSTEM,
            messages=messages,
        ) as stream:
            response = stream.get_final_message()
        u = response.usage
        usage["input"] += u.input_tokens + (u.cache_creation_input_tokens or 0) + (u.cache_read_input_tokens or 0)
        usage["output"] += u.output_tokens
        if response.stop_reason == "refusal":
            raise RuntimeError(f"{name}: request declined ({response.stop_details})")
        if response.stop_reason == "max_tokens":
            raise RuntimeError(f"{name}: reply cut off at max_tokens")
        try:
            contract = Contract.model_validate_json(_json_text(response))
            break
        except ValidationError as e:
            if attempt == 1:
                raise RuntimeError(f"{name}: reply did not match the schema after a correction round: {e}") from e
            messages += [
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": f"That JSON does not validate:\n{e}\nReply with the corrected JSON object only."},
            ]
    usage["usd"] = round(usage["input"] / 1e6 * PRICE_IN + usage["output"] / 1e6 * PRICE_OUT, 4)
    record = ContractRecord(
        **contract.model_dump(),
        id=slug(name),
        source_folder=name,
        extracted_by=f"Claude API ({response.model})",
        extracted_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    )
    return record, usage


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", type=Path)
    ap.add_argument("--only", help="Comma-separated folder names")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    today = dt.date.today().isoformat()
    bundles = collect_bundles(args.root)
    if args.only:
        wanted = {s.strip().lower() for s in args.only.split(",")}
        bundles = [b for b in bundles if b[0].lower() in wanted]
    if not args.force:
        bundles = [b for b in bundles if not (OUT_DIR / f"{slug(b[0])}.json").exists()]
    if args.limit:
        bundles = bundles[: args.limit]
    print(f"{len(bundles)} counterparties to extract")

    # This cloud environment sets ANTHROPIC_BASE_URL for its own use; always call the public API with your key.
    client = anthropic.Anthropic(base_url=os.environ.get("CONTRACTS_API_BASE_URL", "https://api.anthropic.com"))
    failures = 0
    totals = {"input": 0, "output": 0, "usd": 0.0}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(extract_one, client, n, p, today): n for n, p in bundles}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                record, usage = fut.result()
            except anthropic.RateLimitError as e:
                failures += 1
                print(f"  RATE LIMITED  {name}: {e}  (re-run to resume)")
            except anthropic.APIStatusError as e:
                failures += 1
                print(f"  API ERROR {e.status_code}  {name}: {e.message}")
            except anthropic.APIConnectionError as e:
                failures += 1
                print(f"  CONNECTION  {name}: {e}")
            except RuntimeError as e:
                failures += 1
                print(f"  FAILED  {e}")
            else:
                (OUT_DIR / f"{record.id}.json").write_text(json.dumps(record.model_dump(), indent=2))
                for k in ("input", "output", "usd"):
                    totals[k] += usage[k]
                print(f"  ok  {name}  ({record.confidence} confidence, {len(record.risks)} risks)  "
                      f"{usage['input']:,} in / {usage['output']:,} out tokens  US${usage['usd']:.2f}")
    print(f"Total: {totals['input']:,} input + {totals['output']:,} output tokens, about US${totals['usd']:.2f} "
          f"(Opus 5.5 list price; check console.anthropic.com Usage for the billed amount)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
