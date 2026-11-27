"""Extract one structured term sheet per counterparty with the Claude API.

Usage (run from the contract-register folder):
    python extract.py plan <Contracts folder> [--extra <folder> ...]  # group files by counterparty -> data/manifest.json
    python extract.py estimate                                         # count tokens and quote the cost (free)
    python extract.py run [--only "Klaviyo,FloQast"] [--limit 5] [--force] [--max-usd 50]

<Contracts folder> is a local copy of SharePoint `Accounts/Contracts` (all areas). Grouping:
  - each sub-folder of an area (Suppliers/Klaviyo, IT and Systems/BambooRose, ...) is a counterparty;
  - each loose file (in the root or an area) is a counterparty, merged into a folder of the same name
    where one exists (e.g. "Cloudinary contract.pdf" joins Suppliers/Cloudinary);
  - folders with the same normalised name in different areas are merged (Suppliers/Bamboo Rose +
    IT and Systems/BambooRose).
--extra adds folders kept elsewhere (AP agreements, distributor agreements, leases, ...): each sub-folder or
loose file in them is a counterparty, merged with a Contracts folder of the same name.
data/grouping.json can override the grouping: {"split": [...], "merge": {"id": [paths]}, "exclude": [...],
"names": {"path": "Display name"}}, with paths as shown in the manifest (starting with the folder name).

Every PDF and Word document for a counterparty goes into one request, so the model reads the MSA,
order forms, SOWs, amendments and side letters together. Online terms the documents incorporate are
fetched with the web fetch tool, saved under data/terms/<id>/ and marked with the date fetched.

Output: data/records/<id>.json. Existing records are skipped unless --force, so a run can be resumed.
Auth: the SDK reads ANTHROPIC_API_KEY (or an `ant auth login` profile).
"""
from __future__ import annotations

import argparse
import base64
import csv
import fnmatch
import datetime as dt
import html
import json
import os
import re
import sys
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import anthropic
from pydantic import ValidationError

from schema import Contract, ContractRecord

MODEL = "claude-opus-5-5"
MAX_REQUEST_BYTES = 28 * 1024 * 1024
MAX_PDF_PAGES = 560  # API limit is 600 PDF pages per request; page counts below are estimates  # API limit is 32 MB per request; leave headroom for base64
HERE = Path(__file__).parent
DATA = HERE / "data"
OUT_DIR = DATA / "records"
TERMS_DIR = DATA / "terms"
MANIFEST = DATA / "manifest.json"
GROUPING = DATA / "grouping.json"

PDF, TEXT = {".pdf"}, {".docx", ".txt", ".md"}
IGNORE = {".ds_store", "thumbs.db", "desktop.ini"}
DEFAULT_EXCLUDE = {"2XU Contract Templates"}

# Opus 5.5 list prices, US$ per million tokens. Used only for the cost printout.
PRICE_IN, PRICE_CACHE_WRITE, PRICE_CACHE_READ, PRICE_OUT = 4.00, 5.00, 0.20, 20.00

SYSTEM = """You are a contracts analyst for 2XU, an Australian performance apparel company.
You read every document filed for one counterparty (supplier, customer, distributor, agent,
athlete, sponsorship partner, landlord, financier or contractor) and produce one term sheet for
the finance team's contract register. Someone who reads only your record must understand what
2XU signed: term, renewal, how and when either side can get out, notice, what 2XU pays or
receives and when, what can change without 2XU agreeing, and the legal terms that matter.

How to read the bundle:
- It may contain master agreements, order forms, SOWs, amendments, variations, side letters,
  renewals, schedules, drafts, quotes and letters, possibly from several folders (each document
  title shows the folder it came from). Work out which documents govern TODAY and how they fit
  together (an SOW under an MSA, a side letter varying an order form). Apply the order-of-
  precedence clause where there is one and report it in document_precedence.
- Later signed documents override earlier ones unless they say otherwise. Report the CURRENT
  term, not the first one. Mark each document governing, superseded, draft or reference.
- Treat a document as signed only when its signature blocks are completed or an e-signature
  certificate shows completion. Unsigned versions and drafts are listed but do not govern.
- List in missing_documents anything referred to but not in the bundle (an MSA, schedule,
  price list, earlier order).
- Online terms: when a governing document incorporates terms published at a URL (terms of
  service, MSA, general terms), fetch them with the web_fetch tool (only URLs that appear in the
  documents) and use them for termination, renewal, notice, liability, changes and payment terms
  the signed documents leave out. Record each in online_terms_used with today's date as
  retrieved_on. These are the CURRENT published terms, which may differ from the version in
  force when 2XU signed: say so in open_questions where it matters. If a fetch fails, record the
  URL with retrieved_on null and treat those terms as unknown.
- Report dates exactly as written, converted to YYYY-MM-DD. Do not compute notice deadlines;
  report notice periods in days and the code will do the arithmetic.
- If renewal or notice terms are not settled by what you have (including fetched online terms),
  set renewal_type to "unclear" and say why in open_questions.
- annual_value is recurring fees for 12 months excluding tax, in the contract currency, for the
  money that flows in money_direction. For usage-based or estimated fees, give the documented
  estimate and say it is an estimate in fee_summary.
- payment_days is the number of days from invoice to due date (0 for due on receipt or in
  advance). billing_frequency is how often invoices are issued.
- unilateral_change_rights: anything the other party can change without 2XU signing (prices,
  indexation, service scope, the online terms themselves), the notice it must give, and whether
  2XU may exit as a result.
- exit_routes lists every way 2XU can get out, with clause references: letting a non-renewing
  term lapse, early-exit windows, termination for convenience, exit on a price or terms change,
  termination for the other party's uncured breach or insolvency, force majeure. Mark
  no_notice_period true only where 2XU can leave without serving a notice period. State exit
  costs. termination_by_counterparty covers when the other party can terminate or suspend.
- risks are things someone at 2XU should act on: auto-renewal at list price, a notice deadline,
  conflicting documents, a term that has ended with no newer document, a signatory or notice
  contact who may have left, lock-ins, buyouts, uncapped price rises, one-sided change rights,
  unlimited 2XU liability, missing signed copies. Be specific and short.
- Every term date, renewal rule, notice period, termination right, payment term, price-change
  right and liability cap must have an evidence quote copied verbatim from a document (or a
  fetched online page, using its URL as file_name).
- If something is not in the documents, leave it null. Never guess."""


# ---------------------------------------------------------------- grouping

def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


STOP = {"pty", "ltd", "limited", "inc", "llc", "group", "the", "and", "contract", "contracts", "agreement",
        "agreements", "signed", "final", "subscription", "subscriptions", "australia", "2xu", "co", "of"}
GENERIC_FIRST = STOP | set("""draft sow msa copy new renewal order form contract quote proposal executed clean v1 v2 v3 fy23 fy24
    fy25 fy26 fy27 distribution distributor non exclusive reseller master service services letter terms trading partnership
    sponsorship complete completed with docusign intercompany further estimate invoice lease deed extension amendment
    addendum variation schedule mou loi nda mnda mutual confidentiality goods vendor customer supply supplier licence
    license software support global uniform agency wholesale for and per""".split())


def norm(name: str) -> str:
    words = re.findall(r"[a-z0-9]+", name.lower().replace("&", " and "))
    key = "".join(w for w in words if w not in STOP)
    return key or "".join(words)


def first_token(stem: str) -> str:
    for w in re.findall(r"[a-z0-9]+", stem.lower()):
        if len(w) >= 3 and w not in GENERIC_FIRST and not w.isdigit() and not re.fullmatch(r"(19|20)\d\d\d*", w):
            return w
    return ""


NOISE = GENERIC_FIRST | {"mh", "hm", "edited", "pty", "ltd", "v", "final", "signed", "executed", "copy", "doc", "docx",
                         "pdf", "version", "updated", "or", "all", "only", "to", "re", "x", "fy", "xu"}


def glob_match(pattern: str, path: str) -> bool:
    """'*' matches within one folder level, '**' across levels."""
    rx = re.escape(pattern).replace(r"\*\*", "\0").replace(r"\*", "[^/]*").replace("\0", ".*")
    return re.fullmatch(rx, path) is not None


def name_tokens(name: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+[a-z0-9]*", name.lower()) if len(w) >= 2 and w not in NOISE}


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def wanted_file(p: Path) -> bool:
    n = p.name
    return p.is_file() and not n.startswith((".", "~$")) and n.lower() not in IGNORE


def disp(root: Path, p: Path) -> str:
    """Path shown to the model and in the manifest: '<root folder name>/<path inside it>'."""
    return root.name if p == root else f"{root.name}/{p.relative_to(root).as_posix()}"


def plan(roots: list[tuple[Path, str]]) -> list[dict]:
    """Group files into counterparties. roots: (folder, mode) where mode 'areas' means the folder is laid out like
    Accounts/Contracts (area/counterparty/...) and 'flat' means each sub-folder or loose file is a counterparty."""
    cfg = json.loads(GROUPING.read_text()) if GROUPING.exists() else {}
    exclude = DEFAULT_EXCLUDE | set(cfg.get("exclude", []))
    split = set(cfg.get("split", []))
    names = cfg.get("names", {})
    skip = lambda p, d: (p.name.startswith((".", "__MACOSX")) or d in exclude or p.name in exclude
                         or any(glob_match(pat, d) for pat in exclude if "*" in pat))
    units: list[dict] = []  # {name, area, paths (display), src [(root, abs)], kind}

    def unit(root, p, area, kind):
        d = disp(root, p)
        units.append({"name": names.get(d, p.name if kind == "dir" else p.stem), "area": area, "paths": [d],
                      "src": [(str(root), str(p))], "kind": kind})

    def add_children(root: Path, folder: Path, area: str):
        for child in sorted(folder.iterdir()):
            d = disp(root, child)
            if skip(child, d):
                continue
            if child.is_dir():
                if d in split:
                    add_children(root, child, area)
                else:
                    unit(root, child, area, "dir")
            elif wanted_file(child):
                unit(root, child, area, "file")

    for root, mode in roots:
        if mode == "flat":
            add_children(root, root, root.name)
            continue
        for top in sorted(root.iterdir()):
            d = disp(root, top)
            if skip(top, d):
                continue
            if top.is_dir():
                add_children(root, top, top.name)
            elif wanted_file(top):
                unit(root, top, "(root)", "file")

    # Explicit merges first, then folders with the same normalised name, then loose files whose name starts with
    # a folder's name or shares a distinctive first word with another loose file.
    groups: dict[str, dict] = {}
    path_to_gid = {p: gid for gid, paths in cfg.get("merge", {}).items() for p in paths}

    def join(gid, u, label=None):
        g = groups.setdefault(gid, {"name": label or u["name"], "areas": [], "paths": [], "src": []})
        g["areas"].append(u["area"])
        g["paths"] += u["paths"]
        g["src"] += u["src"]
        if u["area"] == "Suppliers" and u["kind"] == "dir":
            g["name"] = u["name"]

    rest = []
    for u in units:
        gid = path_to_gid.get(u["paths"][0]) or (norm(u["name"]) if u["kind"] == "dir" else None)
        if gid:
            join(gid, u)
        else:
            rest.append(u)
    # Loose files: join a folder whose name the file name starts with (e.g. "Cloudinary contract.pdf" ->
    # Suppliers/Cloudinary); otherwise cluster with other loose files only when their distinctive words largely
    # match (versions and copies of the same document), so different athletes or contractors stay apart.
    clusters: list[dict] = []
    for u in rest:
        key = norm(u["name"])
        match = next((gid for gid in groups if len(gid) >= 5 and not gid.startswith("file:") and gid not in GENERIC_FIRST
                      and key.startswith(gid)), None)
        if match:
            join(match, u)
            continue
        toks = name_tokens(u["name"])
        best = max(clusters, key=lambda c: jaccard(toks, c["toks"]), default=None)
        if best and toks and jaccard(toks, best["toks"]) >= 0.6:
            best["units"].append(u)
            best["toks"] |= toks
        else:
            clusters.append({"toks": set(toks), "units": [u]})
    for i, c in enumerate(clusters):
        label = min((u["name"] for u in c["units"]), key=len)
        for u in c["units"]:
            join(f"file:{i}", u, label)

    manifest = []
    for g in groups.values():
        files, skipped = collect_files(g["src"], exclude)
        manifest.append({
            "id": slug(g["name"]), "name": g["name"], "areas": sorted(set(g["areas"])), "paths": g["paths"],
            "files": files, "bytes": sum(Path(f["path"]).stat().st_size for f in files), "skipped": skipped,
        })
    seen: dict[str, int] = {}
    for m in sorted(manifest, key=lambda m: m["name"].lower()):
        n = seen.get(m["id"], 0)
        seen[m["id"]] = n + 1
        if n:
            m["id"] = f"{m['id']}-{n + 1}"
    return sorted(manifest, key=lambda m: m["name"].lower())


def collect_files(src: list[tuple[str, str]], exclude: set | None = None) -> tuple[list[dict], list[str]]:
    """Readable files under each source path. Exclude patterns (data/grouping.json) apply to files and sub-folders too."""
    pats = [p for p in (exclude or ()) if "*" in p]
    files, skipped, seen = [], [], set()
    for root, base in src:
        root, base = Path(root), Path(base)
        for f in ([base] if base.is_file() else sorted(base.rglob("*"))):
            if not wanted_file(f) or "__MACOSX" in f.parts:
                continue
            d = disp(root, f)
            if any(glob_match(p, d) for p in pats) or any(part in (exclude or ()) for part in f.relative_to(root).parts):
                continue
            key = (f.name.lower(), f.stat().st_size)
            if key in seen:
                continue
            seen.add(key)
            if f.suffix.lower() in PDF | TEXT:
                files.append({"path": str(f), "title": disp(root, f)})
            else:
                skipped.append(disp(root, f))
    return files, skipped


# ---------------------------------------------------------------- activity (is the agreement still live?)

ACTIVE_DAYS = 548  # 18 months
CODE_RE = re.compile(r"(?<![A-Z0-9])[0-9][A-Z0-9]{6,8}(?![A-Z0-9])")


def load_activity() -> list[dict]:
    """Rows from data/activity_*.csv: suppliers paid (Medius) and customers invoiced (TRS)."""
    rows = []
    for f in sorted(DATA.glob("activity_*.csv")):
        with f.open() as fh:
            for r in csv.DictReader(fh):
                name = r.get("name") or ""
                rows.append({"name": name, "codes": set((r.get("codes") or "").upper().split()), "last_date": r.get("last_date") or "",
                             "amount_12m": float(r.get("amount_12m") or 0), "source": r.get("source") or f.stem,
                             "key": norm(name), "tok": first_token(name)})
    return rows


def match_activity(entry: dict, activity: list[dict]) -> dict | None:
    """Match by AP21 code only when the names also share a word (supplier and customer codes can collide),
    otherwise by name: exact first, then the longest name that one starts with the other (5+ letters)."""
    text = " ".join(entry["paths"] + [f["title"] for f in entry["files"]]).upper()
    codes = set(CODE_RE.findall(text))
    key, words = norm(entry["name"]), name_tokens(entry["name"] + " " + " ".join(entry["paths"]))
    hits = [a for a in activity if codes & a["codes"] and words & name_tokens(a["name"])]
    if not hits:
        hits = [a for a in activity if a["key"] == key]
    if not hits:
        pref = [a for a in activity if a["key"] and min(len(a["key"]), len(key)) >= 5
                and (a["key"].startswith(key) or key.startswith(a["key"]))]
        if pref:
            longest = max(len(a["key"]) for a in pref)
            hits = [a for a in pref if len(a["key"]) == longest]
    if not hits:
        return None
    best = max(hits, key=lambda a: a["last_date"])
    return {"matched": best["name"], "source": best["source"], "last_date": best["last_date"],
            "amount_12m": round(sum(a["amount_12m"] for a in hits)), "accounts": len(hits)}


def triage(manifest: list[dict]) -> None:
    """Mark each counterparty active / recent / dormant / unknown."""
    activity = load_activity()
    cfg = json.loads(GROUPING.read_text()) if GROUPING.exists() else {}
    keep, drop = cfg.get("keep", []), cfg.get("drop", [])
    have_suppliers = (DATA / "activity_suppliers.csv").exists()
    cutoff = (dt.date.today() - dt.timedelta(days=ACTIVE_DAYS)).isoformat()
    recent_year = re.compile(rf"20({'|'.join(str(y)[2:] for y in range(dt.date.today().year - 2, dt.date.today().year + 2))})")
    for m in manifest:
        act = match_activity(m, activity) if activity else None
        m["activity"] = act
        # Dates in file or folder names, ignoring the download folder's own name (e.g. "OneDrive_1_27-11-2026").
        recent_doc = any(recent_year.search(f["title"].split("/", 1)[-1]) for f in m["files"])
        if act and act["last_date"] >= cutoff:
            m["status"] = "active"
        elif recent_doc:
            m["status"] = "recent"  # signed or dated in the last ~2 years, even if no money has moved
        elif act or (activity and have_suppliers):
            m["status"] = "dormant"
        else:
            m["status"] = "unknown"  # no activity data to judge by
        if any(glob_match(p, path) for p in drop for path in m["paths"]):
            m["status"] = "dormant"
        elif m["status"] in ("dormant", "unknown") and any(glob_match(p, path) for p in keep for path in m["paths"]):
            m["status"] = "kept"  # in an area that is always in scope (data/grouping.json "keep")


def load_manifest() -> list[dict]:
    if not MANIFEST.exists():
        raise SystemExit("No data/manifest.json yet. Run: python extract.py plan <Contracts folder> [--extra <folder> ...]")
    return json.loads(MANIFEST.read_text())


# ---------------------------------------------------------------- request content

def docx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf8", "ignore")
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:tab/>", "\t", xml)
    return html.unescape(re.sub(r"<[^>]+>", "", xml)).strip()


def pdf_pages(path: Path) -> int:
    """Page count: pypdf if installed, else count page objects, else ~60 KB per page."""
    try:
        import pypdf  # optional
        return len(pypdf.PdfReader(str(path)).pages)
    except Exception:
        pass
    data = path.read_bytes()
    n = len(re.findall(rb"/Type\s*/Page(?![a-zA-Z])", data))
    return n or max(1, len(data) // 60_000)


def build_content(entry: dict, today: str) -> list[dict]:
    files = [(Path(f["path"]), f["title"]) for f in entry["files"]]
    # Fit the request: signed/executed PDFs and newer files first; present the chosen ones oldest first.
    rank = lambda ft: (not re.search(r"sign|execut|complet|final", ft[0].name, re.I), ft[0].suffix.lower() != ".pdf", -ft[0].stat().st_mtime)
    chosen, left_out, total, pages = [], [], 0, 0
    for f, title in sorted(files, key=rank):
        is_pdf = f.suffix.lower() in PDF
        size = f.stat().st_size * 4 // 3 if is_pdf else f.stat().st_size // 4
        n = pdf_pages(f) if is_pdf else 0
        if total + size > MAX_REQUEST_BYTES or pages + n > MAX_PDF_PAGES:
            left_out.append(title)
            continue
        total += size
        pages += n
        chosen.append((f, title))
    content: list[dict] = []
    for f, title in sorted(chosen, key=lambda ft: ft[0].stat().st_mtime):
        try:
            if f.suffix.lower() in PDF:
                data = f.read_bytes()
                if b"/Encrypt" in data[-200_000:] or b"/Encrypt" in data[:200_000]:  # password-protected: the API rejects it
                    left_out.append(f"{title} (password-protected)")
                    continue
                content.append({"type": "document", "title": title, "source": {
                    "type": "base64", "media_type": "application/pdf", "data": base64.b64encode(data).decode()}})
            else:
                text = docx_text(f) if f.suffix.lower() == ".docx" else f.read_text(errors="ignore")
                if text.strip():
                    content.append({"type": "document", "title": title, "source": {
                        "type": "text", "media_type": "text/plain", "data": text}})
        except (zipfile.BadZipFile, KeyError, OSError) as e:
            left_out.append(f"{title} (unreadable: {e.__class__.__name__})")
    notes = []
    if left_out:
        notes.append(f"Left out because the bundle is too large or unreadable: {', '.join(left_out)}.")
    if entry.get("skipped"):
        notes.append("Also filed but not readable here (other formats): " + ", ".join(entry["skipped"]) + ".")
    content.append({"type": "text", "text": (
        f"Today is {today}. The documents above are everything filed for counterparty '{entry['name']}' "
        f"(folders: {', '.join(entry['paths'])}). {' '.join(notes)} Produce the register record.")})
    return content


SCHEMA_TEXT = json.dumps(Contract.model_json_schema(), separators=(",", ":"))
FORMAT_NOTE = (
    "When you have everything you need, reply with only the register record as one JSON object that "
    "validates against this JSON Schema. No prose and no code fences. Use null for anything not stated.\n"
    + SCHEMA_TEXT
)
WEB_FETCH = {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 8, "max_content_tokens": 60000}


def _json_text(response) -> str:
    blocks = list(response.content)
    last_tool = max((i for i, b in enumerate(blocks) if b.type != "text" and b.type != "thinking"), default=-1)
    text = "".join(b.text for b in blocks[last_tool + 1:] if b.type == "text").strip()
    if "{" not in text:
        text = "".join(b.text for b in blocks if b.type == "text").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    return text[text.find("{"): text.rfind("}") + 1]


def _cost(u) -> dict:
    cw, cr = u.cache_creation_input_tokens or 0, u.cache_read_input_tokens or 0
    usd = (u.input_tokens * PRICE_IN + cw * PRICE_CACHE_WRITE + cr * PRICE_CACHE_READ + u.output_tokens * PRICE_OUT) / 1e6
    return {"input": u.input_tokens + cw + cr, "output": u.output_tokens, "usd": usd}


def _save_fetches(blocks, rid: str, store: list[dict]) -> None:
    for b in blocks:
        if b.type != "web_fetch_tool_result" or getattr(b.content, "type", "") != "web_fetch_result":
            continue
        res = b.content
        src = res.content.source
        TERMS_DIR.joinpath(rid).mkdir(parents=True, exist_ok=True)
        stem = f"{len(store) + 1:02d}-{slug(res.url)[:70]}"
        if getattr(src, "type", "") == "base64":
            path = TERMS_DIR / rid / f"{stem}.pdf"
            path.write_bytes(base64.b64decode(src.data))
        else:
            path = TERMS_DIR / rid / f"{stem}.txt"
            path.write_text(f"URL: {res.url}\nRetrieved: {res.retrieved_at}\n\n{getattr(src, 'data', '')}")
        store.append({"url": res.url, "retrieved_at": res.retrieved_at, "saved_as": "terms/" + path.relative_to(TERMS_DIR).as_posix()})


def extract_one(client: anthropic.Anthropic, entry: dict, today: str, web: bool) -> tuple[ContractRecord, dict]:
    content = build_content(entry, today)
    content.append({"type": "text", "text": FORMAT_NOTE, "cache_control": {"type": "ephemeral"}})
    messages = [{"role": "user", "content": content}]
    usage = {"input": 0, "output": 0, "usd": 0.0}
    fetched: list[dict] = []
    contract, corrections, rounds = None, 0, 0
    while contract is None:
        rounds += 1
        if rounds > 8:
            raise RuntimeError(f"{entry['name']}: no record after {rounds - 1} rounds")
        with client.beta.messages.stream(
            model=MODEL,
            max_tokens=32000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",  # re-run on a fallback model if a safety classifier declines
            thinking={"type": "adaptive"},
            output_config={"effort": "high"},
            system=SYSTEM,
            **({"tools": [WEB_FETCH]} if web else {}),
            messages=messages,
        ) as stream:
            response = stream.get_final_message()
        for k, v in _cost(response.usage).items():
            usage[k] += v
        _save_fetches(response.content, entry["id"], fetched)
        if response.stop_reason == "refusal":
            raise RuntimeError(f"{entry['name']}: request declined ({response.stop_details})")
        if response.stop_reason == "max_tokens":
            raise RuntimeError(f"{entry['name']}: reply cut off at max_tokens")
        if messages[-1]["role"] == "assistant":  # continuing a paused turn: extend it rather than add a second one
            messages[-1] = {"role": "assistant", "content": list(messages[-1]["content"]) + list(response.content)}
        else:
            messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason == "pause_turn":  # server tool still working; send the turn back to continue
            continue
        try:
            contract = Contract.model_validate_json(_json_text(response))
        except ValidationError as e:
            corrections += 1
            if corrections > 1:
                raise RuntimeError(f"{entry['name']}: reply did not match the schema after a correction round: {e}") from e
            messages.append({"role": "user", "content": f"That JSON does not validate:\n{e}\nReply with the corrected JSON object only."})
    usage["usd"] = round(usage["usd"], 4)
    record = ContractRecord(
        **contract.model_dump(),
        id=entry["id"],
        source_folder=entry["paths"][0],
        area=", ".join(entry["areas"]),
        source_paths=entry["paths"],
        skipped_files=entry.get("skipped", []),
        online_terms_fetched=fetched,
        activity=entry.get("activity"),
        activity_status=entry.get("status"),
        extracted_by=f"Claude API ({response.model})",
        extracted_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    )
    return record, usage


# ---------------------------------------------------------------- commands

class BudgetReached(Exception):
    pass


def select(manifest: list[dict], args) -> list[dict]:
    todo = [m for m in manifest if m["files"]]
    if not getattr(args, "include_dormant", False) and not args.only:
        todo = [m for m in todo if m.get("status") != "dormant"]
    if args.only:
        wanted = {s.strip().lower() for s in args.only.split(",")}
        todo = [m for m in todo if m["name"].lower() in wanted or m["id"] in wanted]
    if not getattr(args, "force", False):
        todo = [m for m in todo if not (OUT_DIR / f"{m['id']}.json").exists()]
    if args.limit:
        todo = todo[: args.limit]
    return todo


def client_() -> anthropic.Anthropic:
    # Cloud sessions set ANTHROPIC_BASE_URL for their own use; always call the public API with your key.
    client = anthropic.Anthropic(base_url=os.environ.get("CONTRACTS_API_BASE_URL", "https://api.anthropic.com"))
    try:  # one free call to check the key before doing anything else
        client.messages.count_tokens(model=MODEL, messages=[{"role": "user", "content": "key check"}])
    except anthropic.AuthenticationError:
        key = os.environ.get("ANTHROPIC_API_KEY", "")
        hint = (" It contains a line break or space: reload it with "
                'export ANTHROPIC_API_KEY="$(pbpaste | tr -d \'\\r\\n \')"' if re.search(r"\s", key) else
                " The key may have been deleted or mistyped: create a new one at console.anthropic.com > API Keys.")
        raise SystemExit("The API rejected your key (invalid x-api-key)." + (hint if key else " ANTHROPIC_API_KEY is not set."))
    return client


def cmd_plan(args) -> int:
    roots = [(args.root, "areas")] + [(e, "flat") for e in args.extra]
    for r, _ in roots:
        if not r.is_dir():
            print(f"Folder not found: {r}")
            return 2
    DATA.mkdir(parents=True, exist_ok=True)
    m = plan(roots)
    triage(m)
    MANIFEST.write_text(json.dumps(m, indent=2))
    with (DATA / "triage.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["status", "counterparty", "id", "last_activity", "amount_12m", "matched_to", "source", "files", "folders"])
        for x in sorted(m, key=lambda x: (x["status"], x["name"].lower())):
            a = x.get("activity") or {}
            w.writerow([x["status"], x["name"], x["id"], a.get("last_date", ""), a.get("amount_12m", ""), a.get("matched", ""),
                        a.get("source", ""), len(x["files"]), " + ".join(x["paths"])])
    multi = [x for x in m if len(x["paths"]) > 1]
    empty = [x for x in m if not x["files"]]
    print(f"{len(m)} counterparties, {sum(len(x['files']) for x in m)} readable files "
          f"({sum(x['bytes'] for x in m) / 1e6:.0f} MB), {sum(len(x['skipped']) for x in m)} other files listed but not read.")
    print(f"\nMerged from several places ({len(multi)}):")
    for x in multi:
        print(f"  {x['name']}: {' + '.join(x['paths'])}")
    if empty:
        print(f"\nNo readable files ({len(empty)}): " + ", ".join(x["name"] for x in empty))
    exts: dict[str, int] = {}
    for x in m:
        for s in x["skipped"]:
            e = Path(s).suffix.lower() or "(none)"
            exts[e] = exts.get(e, 0) + 1
    if exts:
        print("\nNot read (by type): " + ", ".join(f"{k} {v}" for k, v in sorted(exts.items(), key=lambda kv: -kv[1])))
    counts: dict[str, int] = {}
    for x in m:
        counts[x["status"]] = counts.get(x["status"], 0) + 1
    print("\nStill live? " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) +
          " (active = paid or invoiced in the last 18 months; recent = documents dated in the last 2 years; "
          "kept = always in scope per data/grouping.json; dormant = none of these, skipped unless --include-dormant).")
    if not (DATA / "activity_suppliers.csv").exists():
        print("No supplier activity yet: run python activity.py <invoices.json> first, or suppliers show as unknown.")
    print(f"\nWrote {MANIFEST.relative_to(HERE)} and data/triage.csv. Review them, then run: python extract.py estimate")
    return 0


TOKENS_PER_PAGE = 2500  # PDF text plus page image; Anthropic's guide gives 1,500-3,000 per page


def quick_estimate(todo: list[dict]) -> int:
    """Offline estimate from page counts within each request's limits. Fast, no upload."""
    rows = []
    for e in todo:
        files = [Path(f["path"]) for f in e["files"]]
        rank = lambda f: (not re.search(r"sign|execut|complet|final", f.name, re.I), f.suffix.lower() != ".pdf", -f.stat().st_mtime)
        pages, size, text_tokens = 0, 0, 0
        for f in sorted(files, key=rank):
            if f.suffix.lower() in PDF:
                n, b = pdf_pages(f), f.stat().st_size * 4 // 3
                if pages + n > MAX_PDF_PAGES or size + b > MAX_REQUEST_BYTES:
                    continue
                pages, size = pages + n, size + b
            else:
                text_tokens += f.stat().st_size // 8  # Word files: mostly XML; text is a fraction of the size
        rows.append((e["name"], pages * TOKENS_PER_PAGE + text_tokens + 12000, pages))
    n, tokens = len(rows), sum(r[1] for r in rows)
    for name, t, pg in sorted(rows, key=lambda r: -r[1])[:10]:
        print(f"  {name}: ~{t:,} tokens ({pg} PDF pages)")
    low = (tokens * 0.6 * PRICE_IN + n * 8000 * PRICE_OUT) / 1e6
    high = (tokens * 1.2 * PRICE_IN + n * (16000 * PRICE_OUT + 40000 * PRICE_IN)) / 1e6
    print(f"\n{n} counterparties, about {sum(r[2] for r in rows):,} PDF pages and ~{tokens:,} input tokens. "
          f"Estimated cost US${low:.0f}-{high:.0f} (quick estimate from page counts; run without --quick for an exact token count).")
    return 0


def cmd_estimate(args) -> int:
    manifest = load_manifest()
    todo = select(manifest, args)
    if args.quick:
        return quick_estimate(todo)
    client, today = client_(), dt.date.today().isoformat()
    counts: dict[str, int] = {}

    def count(entry):
        content = build_content(entry, today) + [{"type": "text", "text": FORMAT_NOTE}]
        return client.messages.count_tokens(model=MODEL, system=SYSTEM, messages=[{"role": "user", "content": content}]).input_tokens

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(count, e): e for e in todo}
        for fut in as_completed(futs):
            e = futs[fut]
            try:
                counts[e["name"]] = fut.result()
            except anthropic.APIError as err:
                print(f"  could not count {e['name']}: {err}")
    n, tokens = len(counts), sum(counts.values())
    low = (tokens * PRICE_IN + n * 8000 * PRICE_OUT) / 1e6
    high = (tokens * PRICE_IN * 1.2 + n * (16000 * PRICE_OUT + 40000 * PRICE_IN)) / 1e6
    for name, t in sorted(counts.items(), key=lambda kv: -kv[1])[:10]:
        print(f"  {name}: {t:,} tokens")
    print(f"\n{n} counterparties, {tokens:,} input tokens. Estimated cost US${low:.0f}-{high:.0f} "
          f"(higher end allows for fetched online terms and longer answers).")
    return 0


def cmd_run(args) -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest()
    todo = select(manifest, args)
    print(f"{len(todo)} counterparties to extract")
    client, today = client_(), dt.date.today().isoformat()
    failures, skipped = 0, 0
    totals = {"input": 0, "output": 0, "usd": 0.0}
    lock = threading.Lock()

    def run(entry):
        # Checked when a worker picks up the next counterparty; ones already running still finish.
        with lock:
            if totals["usd"] >= args.max_usd:
                raise BudgetReached
        record, usage = extract_one(client, entry, today, not args.no_web)
        with lock:
            for k in totals:
                totals[k] += usage[k]
        return record, usage

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run, e): e["name"] for e in todo}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                record, usage = fut.result()
            except BudgetReached:
                skipped += 1
                continue
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
                web = f", {len(record.online_terms_fetched)} online terms" if record.online_terms_fetched else ""
                print(f"  ok  {name}  ({record.confidence} confidence, {len(record.documents)} docs{web})  "
                      f"{usage['input']:,} in / {usage['output']:,} out tokens  US${usage['usd']:.2f}")
    print(f"Total: {totals['input']:,} input + {totals['output']:,} output tokens, about US${totals['usd']:.2f} "
          f"(Opus 5.5 list price; check console.anthropic.com Usage for the billed amount)")
    if skipped:
        print(f"Spend cap of US${args.max_usd:.2f} reached: {skipped} counterparties not started. "
              f"Re-run the same command (with a higher --max-usd if needed) to continue.")
    return 1 if failures or skipped else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["plan", "estimate", "run"])
    ap.add_argument("root", nargs="?", type=lambda s: Path(s).expanduser(),
                    help="plan only: local copy of Accounts/Contracts (laid out as area/counterparty)")
    ap.add_argument("--extra", action="append", default=[], type=lambda s: Path(s).expanduser(),
                    help="plan only: another folder where each sub-folder or loose file is one counterparty (repeatable)")
    ap.add_argument("--only", help="Comma-separated counterparty names or ids")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--force", action="store_true", help="Re-extract counterparties that already have a record")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--max-usd", type=float, default=50.0,
                    help="Stop starting new counterparties once estimated spend in this run reaches this (default 50)")
    ap.add_argument("--no-web", action="store_true", help="Do not fetch online terms")
    ap.add_argument("--quick", action="store_true", help="estimate only: count pages locally instead of asking the API (fast)")
    ap.add_argument("--include-dormant", action="store_true", help="Also extract counterparties with no recent activity or documents")
    args = ap.parse_args()
    if args.command == "plan" and args.root is None:
        print("plan needs the Contracts folder, e.g. python extract.py plan ~/Downloads/contracts/Contracts")
        return 2
    return {"plan": cmd_plan, "estimate": cmd_estimate, "run": cmd_run}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
