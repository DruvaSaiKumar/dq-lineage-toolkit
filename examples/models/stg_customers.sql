select
    customer_id,
    trim(name) as name,
    lower(trim(email)) as email,
    upper(trim(country)) as country,
    signup_ts
from {{ source('raw', 'customers') }}
