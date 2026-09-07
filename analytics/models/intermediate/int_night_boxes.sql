-- Dinner (svc = 2) on a 30-minute grid: one row per box of each observation version,
-- flagged open when any slot snapped down to the half-hour lands in it. Mirrors
-- resy_busyness.scoring.score_night; analytics/crosscheck.py compares the two.
with boxes as (
    select distinct
        w.state_id,
        w.venue_id,
        w.service_day,
        w.party_size,
        g.box_min
    from {{ ref('int_windows') }} as w,
        unnest(generate_series(w.start_min, w.end_min, 30)) as g (box_min)
    where w.svc = 2
),

opened as (
    select distinct state_id, box_min_30 as box_min
    from {{ ref('int_slots') }}
)

select
    b.state_id,
    b.venue_id,
    b.service_day,
    b.party_size,
    b.box_min,
    lpad((b.box_min // 60)::varchar, 2, '0') || ':' || lpad((b.box_min % 60)::varchar, 2, '0') as box_time,
    o.box_min is not null as is_open
from boxes as b
left join opened as o on o.state_id = b.state_id and o.box_min = b.box_min
