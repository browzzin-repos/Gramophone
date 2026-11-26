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
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import anthropic

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
- Every term date, renewal rule, notice period and fee must have an evidence quote copied
  verbatim from the documents.
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


def extract_one(client: anthropic.Anthropic, name: str, pdfs: list[Path], today: str) -> ContractRecord:
    response = client.beta.messages.parse(
        model=MODEL,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",  # re-run on a fallback model if a safety classifier declines
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
        system=SYSTEM,
        messages=[{"role": "user", "content": build_content(name, pdfs, today)}],
        output_format=Contract,
    )
    if response.stop_reason == "refusal":
        raise RuntimeError(f"{name}: request declined ({response.stop_details})")
    if response.stop_reason == "max_tokens" or response.parsed_output is None:
        raise RuntimeError(f"{name}: no complete record returned (stop_reason={response.stop_reason})")
    contract: Contract = response.parsed_output
    return ContractRecord(
        **contract.model_dump(),
        id=slug(name),
        source_folder=name,
        extracted_by=f"Claude API ({response.model})",
        extracted_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    )


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

    client = anthropic.Anthropic()
    failures = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(extract_one, client, n, p, today): n for n, p in bundles}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                record = fut.result()
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
                print(f"  ok  {name}  ({record.confidence} confidence, {len(record.risks)} risks)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
