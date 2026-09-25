select
    country,
    count(*) as orders,
    sum(net_amount) as revenue
from {{ ref('fct_orders') }}
group by country
