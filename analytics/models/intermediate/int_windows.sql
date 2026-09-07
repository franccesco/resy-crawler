-- One row per service window inside each observation version.
with state as (
    select state_id, venue_id, service_day, party_size, state_json
    from {{ ref('stg_venue_day_state') }}
),

unnested as (
    select
        s.state_id,
        s.venue_id,
        s.service_day,
        s.party_size,
        w.svc,
        w.start as window_start,
        w."end" as window_end,
        w.step
    from state as s,
        unnest(
            from_json(
                json_extract(s.state_json, '$.windows'),
                '[{"svc":"INTEGER","start":"VARCHAR","end":"VARCHAR","step":"INTEGER"}]'
            )
        ) as t (w)
)

select
    *,
    case svc
        when 1 then 'brunch' when 2 then 'dinner' when 3 then 'lunch' when 5 then 'breakfast'
    end as service,
    split_part(window_start, ':', 1)::integer * 60 + split_part(window_start, ':', 2)::integer as start_min,
    split_part(window_end, ':', 1)::integer * 60 + split_part(window_end, ':', 2)::integer as end_min
from unnested
