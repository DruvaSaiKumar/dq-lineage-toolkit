-- Cancelled and negative-amount orders are excluded; orders whose customer is unknown are kept
-- (left join) so revenue is not silently lost, and show up with a NULL country.
with valid as (
    select *
    from {{ ref('stg_orders') }}
    where status <> 'CANCELLED' and amount >= 0
)

select
    o.order_id,
    o.customer_id,
    c.country,
    o.amount,
    o.discount,
    o.amount - o.discount as net_amount,
    o.order_ts
from valid as o
left join {{ ref('dim_customer') }} as c on o.customer_id = c.customer_id
