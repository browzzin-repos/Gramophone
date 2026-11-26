-- AP21 supplier codes for the contract register (2XU Supabase, schema trs, synced daily from TRS).
-- Run in Supabase, then save the matches to data/ap21_suppliers.json as {record_id: {"code": ..., "name": ...}}.
-- build.py merges that file into each record. Check matches by hand: names are fuzzy (e.g. "Planet" also
-- matches Planet ISP and Planet Innovation, which are different companies).
select supplier_id, supplier_code, supplier_name, country, dpo_classification
from trs.supplier
where supplier_name ~* '(triple ?whale|planet ?6|klaviyo|floqast|style ?arcade|slim ?stock)'
order by supplier_name;
