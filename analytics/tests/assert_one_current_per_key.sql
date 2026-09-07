-- SCD2 invariant: at most one live version per (venue, service day, party size).
select venue_id, service_day, party_size, count(*) as live_rows
from {{ ref('stg_venue_day_state') }}
where is_current
group by venue_id, service_day, party_size
having count(*) > 1
