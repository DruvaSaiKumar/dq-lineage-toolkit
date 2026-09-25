-- Deliberately exposes PII as a straight copy, so the tag report has something to flag.
select
    customer_id,
    email,
    upper(name) as name_upper
from {{ ref('dim_customer') }}
where is_supported_country
