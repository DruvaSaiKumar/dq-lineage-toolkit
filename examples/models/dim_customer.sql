select
    customer_id,
    name,
    email,
    country,
    country in ('US', 'GB', 'DE', 'IN') as is_supported_country
from {{ ref('stg_customers') }}
