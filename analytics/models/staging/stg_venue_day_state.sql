-- One row per SCD2 version of a venue-night observation. The JSON is kept for the
-- intermediate models that unnest windows and slots.
select
    id as state_id,
    venue_id,
    service_day::date as service_day,
    party_size,
    hash as state_hash,
    valid_from::timestamptz as valid_from,
    valid_to::timestamptz as valid_to,
    is_current = 1 as is_current,
    first_run_id,
    last_seen_run_id,
    json_extract(state_json, '$.events')::integer as events,
    json_extract(state_json, '$.is_tock')::boolean as is_tock,
    json_extract_string(state_json, '$.source_name') as source_name,
    json_extract_string(state_json, '$.reopen_date') as reopen_date,
    json_array_length(json_extract(state_json, '$.windows')) as n_windows,
    json_array_length(json_extract(state_json, '$.slots')) as n_slots,
    state_json
from {{ source('resy_store', 'venue_day_state') }}
