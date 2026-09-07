-- Per (run, venue): the busyness score as the app computes it for dinner, party of
-- two, 30-minute grid: the mean over nights with a dinner window of taken / boxes.
-- Versions are picked as of each finished run (first_run_id <= run_id <= last_seen_run_id).
-- Exclusion rules (closed, other platform, events only, no inventory) are not applied
-- here; the flags needed for them are carried so a consumer can.
with runs as (
    select run_id, first_night, last_night, verify_day
    from {{ ref('stg_runs') }}
    where status = 'done'
),

nights as (
    select
        r.run_id,
        b.venue_id,
        b.party_size,
        b.service_day,
        b.boxes,
        b.open_boxes,
        b.taken,
        b.taken_ratio,
        b.events,
        b.is_tock,
        b.source_name,
        b.reopen_date,
        b.any_window
    from runs as r
    join {{ ref('fct_night_boxes') }} as b
        on b.first_run_id <= r.run_id
        and b.last_seen_run_id >= r.run_id
        and b.service_day between r.first_night and r.last_night
),

far as (
    select r.run_id, b.venue_id, b.party_size, b.open_boxes as far_open_boxes
    from runs as r
    join {{ ref('fct_night_boxes') }} as b
        on b.first_run_id <= r.run_id
        and b.last_seen_run_id >= r.run_id
        and b.service_day = r.verify_day
)

select
    n.run_id,
    n.venue_id,
    n.party_size,
    count(*) as nights_observed,
    count(*) filter (where n.boxes > 0) as days_scored,
    sum(n.taken) as taken_total,
    sum(n.boxes) as boxes_total,
    round(avg(n.taken_ratio), 4) as score,
    bool_and(n.open_boxes = 0) filter (where n.boxes > 0) as sold_out_all_week,
    max(f.far_open_boxes) as far_open_boxes,
    sum(n.events) as events,
    bool_or(n.is_tock) as is_tock,
    max(n.source_name) as source_name,
    max(n.reopen_date) as reopen_date,
    bool_or(n.any_window) as any_window
from nights as n
left join far as f
    on f.run_id = n.run_id and f.venue_id = n.venue_id and f.party_size = n.party_size
group by n.run_id, n.venue_id, n.party_size
