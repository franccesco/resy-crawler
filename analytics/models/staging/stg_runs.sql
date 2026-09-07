-- One row per run, with the days it covered derived the way pipeline.run_days does:
-- nights start the day after the run started (Pacific) and span params.days; the
-- verification day is verify_offset_days after the start.
with src as (
    select * from {{ source('resy_store', 'runs') }}
),

typed as (
    select
        id as run_id,
        status,
        started_at::timestamptz as started_at,
        finished_at::timestamptz as finished_at,
        requests_total,
        requests_done,
        venues_seen,
        states_new,
        states_unchanged,
        error,
        params_json,
        json_extract_string(params_json, '$.source') as source,
        json_extract(params_json, '$.days')::integer as nights,
        json_extract(params_json, '$.verify_offset_days')::integer as verify_offset_days,
        from_json(json_extract(params_json, '$.party_sizes'), '["INTEGER"]') as party_sizes,
        timezone('America/Los_Angeles', started_at::timestamptz)::date as started_on_pacific
    from src
)

select
    *,
    started_on_pacific + interval 1 day as first_night,
    started_on_pacific + to_days(nights) as last_night,
    started_on_pacific + to_days(verify_offset_days) as verify_day
from typed
