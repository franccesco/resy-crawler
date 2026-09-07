-- A version cannot be last seen by a run earlier than the one that opened it.
select state_id, first_run_id, last_seen_run_id
from {{ ref('stg_venue_day_state') }}
where first_run_id > last_seen_run_id
