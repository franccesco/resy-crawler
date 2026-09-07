-- Per observation version: dinner boxes, open boxes and taken boxes on a 30-minute grid.
-- Versions with no dinner window have boxes = 0 (the app skips those nights).
-- This mirrors resy_busyness.scoring.score_night and is cross-checked by
-- analytics/crosscheck.py; if the two disagree, fix the SQL.
with agg as (
    select
        state_id,
        count(*) as boxes,
        count(*) filter (where is_open) as open_boxes
    from {{ ref('int_night_boxes') }}
    group by state_id
)

select
    v.state_id,
    v.venue_id,
    v.service_day,
    v.party_size,
    v.valid_from,
    v.valid_to,
    v.is_current,
    v.first_run_id,
    v.last_seen_run_id,
    v.events,
    v.is_tock,
    v.source_name,
    v.reopen_date,
    v.n_windows > 0 as any_window,
    coalesce(a.boxes, 0) as boxes,
    coalesce(a.open_boxes, 0) as open_boxes,
    coalesce(a.boxes, 0) - coalesce(a.open_boxes, 0) as taken,
    case when a.boxes > 0 then (a.boxes - a.open_boxes) / a.boxes end as taken_ratio
from {{ ref('stg_venue_day_state') }} as v
left join agg as a on a.state_id = v.state_id
