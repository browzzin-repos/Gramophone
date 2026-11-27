"""Contract record schema shared by the extractor (Claude API) and the dashboard builder.

The model only extracts what the documents say. Every derived date (notice deadline,
renewal date, status) is computed in code in build.py, so the arithmetic is deterministic
and auditable.
"""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

# The API allows at most 16 nullable/union fields in a structured-output schema, so free-text
# fields are plain strings in the extraction schema; extract.py turns "" back into None.
EMPTY = "Empty string if the documents do not say."

Category = Literal[
    "IT & Systems",
    "Ecommerce & Marketing",
    "Finance",
    "Logistics & 3PL",
    "Wholesale & Distribution",
    "Sponsorship & Athletes",
    "Professional Services",
    "Property & Facilities",
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


class DocumentRef(BaseModel):
    file_name: str
    doc_type: Literal["master_agreement", "order_form", "sow", "amendment", "renewal", "proposal", "terms", "other"]
    document_date: Optional[str] = Field(None, description="YYYY-MM-DD")
    signed: bool = Field(description="True only if a completed signature/date block or e-sign certificate is present")


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
    cost_on_exit: str = Field(description="Fees, buyouts or refunds that apply. " + EMPTY)
    source: str = Field(description="Clause reference and document, e.g. 'MSA cl 12.4'")


class Contract(BaseModel):
    counterparty: str = Field(description="Short trading name, e.g. 'Klaviyo'")
    counterparty_legal_entity: str = Field(description=EMPTY)
    twoxu_entity: str = Field(description="Contracting 2XU entity, e.g. '2XU Pty Ltd'")
    category: Category
    service_description: str = Field(description="One sentence: what 2XU gets")
    documents: List[DocumentRef]
    signed_for_2xu_by: str = Field(description="Name and title of the 2XU signatory. " + EMPTY)
    twoxu_contact: str = Field(description="Named 2XU contact / notice recipient on the document. " + EMPTY)

    effective_date: Optional[str] = Field(None, description="YYYY-MM-DD")
    term_start: Optional[str] = Field(None, description="YYYY-MM-DD, start of the CURRENT term")
    term_end: Optional[str] = Field(None, description="YYYY-MM-DD, last day of the CURRENT term")
    initial_term_months: Optional[int] = None
    renewal_type: RenewalType
    renewal_term_months: Optional[int] = None
    notice_period_days: Optional[int] = Field(None, description="Days of written notice needed before term end to cancel (auto_renew) or to exercise an extension (option_to_extend)")
    notice_clause_summary: str = Field(description=EMPTY)
    early_exit_deadline: Optional[str] = Field(None, description="YYYY-MM-DD last day of any special exit window, e.g. a 60-day cancel-for-any-reason right")
    early_exit_summary: str = Field(description=EMPTY)

    currency: Optional[Literal["AUD", "USD", "NZD", "EUR", "GBP", "JPY", "CNY", "Other"]] = None
    annual_value: Optional[float] = Field(None, description="Recurring fees for 12 months ex tax, in `currency`. Estimate from monthly x 12 if needed")
    one_off_fees: Optional[float] = None
    fee_summary: str
    price_change_on_renewal: str = Field(description=EMPTY)
    payment_terms: str = Field(description=EMPTY)
    billing_frequency: str = Field(description=EMPTY)

    termination_for_convenience: str = Field(description=EMPTY)
    lock_in_or_exit_costs: str = Field(description=EMPTY)
    incorporated_online_terms: str = Field(description="URL or name of external terms the contract incorporates. " + EMPTY)
    exit_routes: List[ExitRoute] = Field(description="Every way 2XU can get out, including letting a term lapse; cite clauses. Include routes in incorporated online terms only if their text is in the bundle")

    risks: List[Risk] = Field(description="Things finance/legal should act on: auto-renew traps, conflicting clauses, departed signatories, missing signed copies, uncapped increases")
    open_questions: List[str] = Field(description="What the documents do not settle and a human must confirm")
    confidence: Literal["high", "medium", "low"]
    evidence: List[Evidence] = Field(description="Quotes backing term dates, renewal, notice, fees")


class ContractRecord(Contract):
    """Stored record: extraction plus provenance. Free-text fields are None when not stated."""
    counterparty_legal_entity: Optional[str] = None
    signed_for_2xu_by: Optional[str] = None
    twoxu_contact: Optional[str] = None
    notice_clause_summary: Optional[str] = None
    early_exit_summary: Optional[str] = None
    price_change_on_renewal: Optional[str] = None
    payment_terms: Optional[str] = None
    billing_frequency: Optional[str] = None
    termination_for_convenience: Optional[str] = None
    lock_in_or_exit_costs: Optional[str] = None
    incorporated_online_terms: Optional[str] = None
    id: str
    source_folder: str
    ap21_supplier_code: Optional[str] = None
    ap21_supplier_name: Optional[str] = None
    extracted_by: str
    extracted_at: str
