-- One row per open slot inside each observation version, with the slot snapped down
-- to its 30-minute box the same way resy_busyness.scoring.score_night does.
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
        sl.t as slot_time,
        sl.type as seating_type
    from state as s,
        unnest(
            from_json(json_extract(s.state_json, '$.slots'), '[{"t":"VARCHAR","type":"VARCHAR"}]')
        ) as t (sl)
)

select
    *,
    split_part(slot_time, ':', 1)::integer * 60 + split_part(slot_time, ':', 2)::integer as slot_min,
    (split_part(slot_time, ':', 1)::integer * 60 + split_part(slot_time, ':', 2)::integer) // 30 * 30 as box_min_30
from unnested
