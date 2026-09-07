-- A superseded version carries a valid_to; a live one does not.
select state_id, is_current, valid_to
from {{ ref('stg_venue_day_state') }}
where (is_current and valid_to is not null) or (not is_current and valid_to is null)
