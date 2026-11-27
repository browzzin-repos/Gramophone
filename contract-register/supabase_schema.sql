-- Contract register tables for 2XU Supabase. NOT applied yet: review, then run as a migration.
-- Purpose: one shared source for the contract dashboard and the AP tool.
-- The AP tool joins invoices to contracts.ap_expected on the AP21 supplier code.
create schema if not exists contracts;

create table if not exists contracts.contract (
  id                  text primary key,              -- slug, e.g. 'planet6'
  counterparty        text not null,
  legal_entity        text,
  ap21_supplier_code  text,                          -- trs.supplier.supplier_code
  category            text,
  owner               text,
  owner_confirmed     boolean default false,
  status              text,                          -- Act now / Term ended / Needs review / Due soon / Active
  renewal_type        text,
  term_start          date,
  term_end            date,
  notice_period_days  int,
  currency            text,
  annual_value        numeric,
  annual_value_aud    numeric,
  billing_frequency   text,
  payment_terms       text,
  record              jsonb not null,                -- full extracted record incl. evidence quotes
  extracted_by        text,
  extracted_at        timestamptz,
  updated_at          timestamptz default now()
);

create table if not exists contracts.deadline (
  contract_id text references contracts.contract(id) on delete cascade,
  kind        text,                                  -- Cancel by / Extend by / Free exit closes / Term ends ...
  due         date,
  why         text,
  primary key (contract_id, kind, due)
);

create table if not exists contracts.action (
  id          bigint generated always as identity primary key,
  contract_id text references contracts.contract(id) on delete cascade,
  title       text not null,
  detail      text,
  due         date,
  owner       text,
  source      text,                                  -- deadline / risk / manual
  done_at     timestamptz
);

-- What AP should see on an invoice. One row per contract with a supplier code.
create or replace view contracts.ap_expected as
select c.ap21_supplier_code,
       c.id as contract_id,
       c.counterparty,
       c.status,
       c.currency,
       (c.record -> 'computed' -> 'ap' ->> 'expected_invoice')::numeric as expected_invoice,
       (c.record -> 'computed' -> 'ap' ->> 'tolerance_pct')::int       as tolerance_pct,
       c.billing_frequency,
       c.payment_terms,
       c.term_start as valid_from,
       c.term_end   as valid_to,
       c.record -> 'computed' -> 'ap' -> 'holds' as holds
from contracts.contract c
where c.ap21_supplier_code is not null;

alter table contracts.contract enable row level security;
alter table contracts.deadline enable row level security;
alter table contracts.action   enable row level security;
