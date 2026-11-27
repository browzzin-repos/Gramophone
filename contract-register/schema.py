"""Contract record schema shared by the extractor (Claude API) and the dashboard builder.

The model only extracts what the documents say. Every derived date (notice deadline,
renewal date, status) is computed in code in build.py, so the arithmetic is deterministic
and auditable.

One record = one counterparty, built from every document filed for it: master agreements,
order forms, SOWs, amendments, side letters, renewals and the online terms they incorporate.
"""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

Category = Literal[
    "IT & Systems",
    "Ecommerce & Marketing",
    "Finance",
    "Logistics & 3PL",
    "Wholesale & Distribution",
    "Sponsorship & Athletes",
    "Professional Services",
    "Property & Facilities",
    "Contractors & HR",
    "NDA & Confidentiality",
    "Legal & IP",
    "Other",
]

RenewalType = Literal[
    "auto_renew",            # rolls over unless someone gives notice
    "option_to_extend",      # 2XU must actively exercise an extension
    "mutual_agreement",      # ends unless both parties sign something new
    "evergreen",             # runs until terminated (no end date)
    "fixed_term",            # ends, no renewal mechanism
    "unclear",               # documents conflict or defer to terms not on file
]

DocType = Literal[
    "master_agreement", "order_form", "sow", "amendment", "variation", "side_letter", "renewal",
    "proposal", "terms", "online_terms", "schedule", "nda", "settlement", "invoice_or_quote", "other",
]


class DocumentRef(BaseModel):
    file_name: str
    doc_type: DocType
    document_date: Optional[str] = Field(None, description="YYYY-MM-DD")
    signed: bool = Field(description="True only if a completed signature/date block or e-sign certificate is present")
    status: Literal["governing", "superseded", "draft", "reference"] = Field(
        "governing", description="governing = forms part of the current contract; superseded = replaced by a later signed document; "
                                 "draft = unsigned or not finalised; reference = background only (quote, invoice, letter)")
    location: Optional[str] = Field(None, description="Folder the file came from, as given in its title")
    summary: Optional[str] = Field(None, description="One line: what this document does (e.g. 'Adds 5 users from 1 Mar 2026')")


class Evidence(BaseModel):
    field: str = Field(description="Which record field this quote supports, e.g. 'notice_period_days'")
    quote: str = Field(description="Verbatim text from the document, max ~40 words")
    file_name: str
    page: Optional[int] = None


class Risk(BaseModel):
    severity: Literal["high", "medium", "low"]
    issue: str


class ExitRoute(BaseModel):
    route: Literal["let_expire", "early_exit_window", "convenience", "price_or_terms_change", "vendor_breach",
                   "vendor_insolvency", "force_majeure", "warranty_failure", "mutual_agreement", "other"]
    notice: str = Field(description="Notice 2XU must give, e.g. 'None', '30 days before term end', 'Immediate after 30-day cure period'")
    no_notice_period: bool = Field(description="True if 2XU can exit without serving a notice period (e.g. letting a non-renewing term end, immediate termination after an uncured breach)")
    conditions: str = Field(description="What must be true for this route to work")
    cost_on_exit: Optional[str] = Field(None, description="Fees, buyouts or refunds that apply")
    source: str = Field(description="Clause reference and document, e.g. 'MSA cl 12.4'")


class OnlineTerms(BaseModel):
    url: str
    title: Optional[str] = None
    retrieved_on: Optional[str] = Field(None, description="YYYY-MM-DD the page was fetched; None if it could not be fetched")
    used_for: str = Field(description="Which terms came from this page, e.g. 'termination, liability cap, price changes'")


class Contract(BaseModel):
    counterparty: str = Field(description="Short trading name, e.g. 'Klaviyo'")
    counterparty_legal_entity: Optional[str] = None
    counterparty_role: Literal["supplier", "customer", "distributor_or_agent", "athlete_or_talent", "sponsorship_partner",
                               "landlord", "financier", "contractor", "other"] = "supplier"
    money_direction: Literal["2XU pays", "2XU receives", "both", "none"] = "2XU pays"
    twoxu_entity: str = Field(description="Contracting 2XU entity, e.g. '2XU Pty Ltd'")
    category: Category
    service_description: str = Field(description="One sentence: what the agreement covers")
    documents: List[DocumentRef]
    document_precedence: Optional[str] = Field(None, description="Which document wins if they conflict, per the order-of-precedence clause")
    missing_documents: List[str] = Field(default_factory=list, description="Documents referred to but not in the bundle (e.g. 'MSA dated 1 Jul 2024', 'Schedule 2 price list')")
    signed_for_2xu_by: Optional[str] = Field(None, description="Name and title of the 2XU signatory")
    twoxu_contact: Optional[str] = Field(None, description="Named 2XU contact / notice recipient on the document")

    # Term and renewal
    effective_date: Optional[str] = Field(None, description="YYYY-MM-DD")
    term_start: Optional[str] = Field(None, description="YYYY-MM-DD, start of the CURRENT term")
    term_end: Optional[str] = Field(None, description="YYYY-MM-DD, last day of the CURRENT term")
    initial_term_months: Optional[int] = None
    renewal_type: RenewalType
    renewal_term_months: Optional[int] = None
    notice_period_days: Optional[int] = Field(None, description="Days of written notice needed before term end to cancel (auto_renew) or to exercise an extension (option_to_extend)")
    notice_clause_summary: Optional[str] = None
    notice_method: Optional[str] = Field(None, description="How notices must be given and to whom (e.g. 'In writing to legal@vendor.com')")
    early_exit_deadline: Optional[str] = Field(None, description="YYYY-MM-DD last day of any special exit window, e.g. a 60-day cancel-for-any-reason right")
    early_exit_summary: Optional[str] = None

    # Money
    currency: Optional[Literal["AUD", "USD", "NZD", "EUR", "GBP", "JPY", "CNY", "Other"]] = None
    annual_value: Optional[float] = Field(None, description="Recurring fees for 12 months ex tax, in `currency`. Estimate from monthly x 12 if needed")
    one_off_fees: Optional[float] = None
    fee_summary: str
    minimum_commitment: Optional[str] = Field(None, description="Minimum spend, volume or term 2XU is committed to")
    payment_days: Optional[int] = Field(None, description="Days from invoice until payment is due (0 = due on receipt / in advance)")
    payment_terms: Optional[str] = None
    billing_frequency: Optional[str] = None
    late_payment: Optional[str] = Field(None, description="Interest, suspension rights or fees for late payment")
    price_change_on_renewal: Optional[str] = None

    # Changes
    unilateral_change_rights: Optional[str] = Field(None, description="What the other party can change without 2XU signing (price, terms, service, online terms), on what notice, and whether 2XU can exit as a result")
    variation_clause: Optional[str] = Field(None, description="How the contract may be amended (e.g. 'only in writing signed by both')")

    # Exit
    termination_for_convenience: Optional[str] = None
    termination_by_counterparty: Optional[str] = Field(None, description="When the other party can terminate or suspend")
    lock_in_or_exit_costs: Optional[str] = None
    post_termination: Optional[str] = Field(None, description="Data return, transition assistance, surviving obligations, refunds")
    incorporated_online_terms: Optional[str] = Field(None, description="URL or name of external terms the contract incorporates")
    online_terms_used: List[OnlineTerms] = Field(default_factory=list, description="Online terms fetched and relied on for this record")
    exit_routes: List[ExitRoute] = Field(description="Every way 2XU can get out, including letting a term lapse; cite clauses")

    # Legal
    liability_cap: Optional[str] = Field(None, description="Cap on each party's liability and main exclusions")
    indemnities: Optional[str] = Field(None, description="Who indemnifies whom, for what")
    assignment_and_change_of_control: Optional[str] = None
    exclusivity_and_restrictions: Optional[str] = Field(None, description="Exclusivity, non-compete, non-solicit, territory, minimum purchase")
    service_levels: Optional[str] = Field(None, description="SLAs, service credits, support hours")
    ip_ownership: Optional[str] = None
    data_and_privacy: Optional[str] = None
    confidentiality: Optional[str] = None
    insurance: Optional[str] = None
    governing_law: Optional[str] = Field(None, description="Governing law and dispute forum, e.g. 'Victoria, Australia; courts of Victoria'")
    other_key_terms: List[str] = Field(default_factory=list, description="Anything else a finance or legal reader would want to know")

    risks: List[Risk] = Field(description="Things finance/legal should act on: auto-renew traps, conflicting clauses, departed signatories, missing signed copies, uncapped increases")
    open_questions: List[str] = Field(description="What the documents do not settle and a human must confirm")
    confidence: Literal["high", "medium", "low"]
    evidence: List[Evidence] = Field(description="Quotes backing term dates, renewal, notice, termination, payment, fees, price changes and liability")


class FetchedTerms(BaseModel):
    url: str
    retrieved_at: Optional[str] = None
    saved_as: str


class ContractRecord(Contract):
    """Stored record: extraction plus provenance."""
    id: str
    source_folder: str
    area: Optional[str] = None
    source_paths: List[str] = Field(default_factory=list)
    skipped_files: List[str] = Field(default_factory=list)
    online_terms_fetched: List[FetchedTerms] = Field(default_factory=list)
    ap21_supplier_code: Optional[str] = None
    ap21_supplier_name: Optional[str] = None
    extracted_by: str
    extracted_at: str
