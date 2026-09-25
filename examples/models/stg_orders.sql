{{ config(materialized='table') }}
select
    order_id,
    customer_id,
    amount,
    coalesce(discount, 0) as discount,
    upper(trim(status)) as status,
    order_ts
from {{ source('raw', 'orders') }}
