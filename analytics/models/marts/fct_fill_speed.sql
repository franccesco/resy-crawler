-- How each venue-night's dinner availability moved between consecutive snapshots.
-- One row per version after the first: boxes that appeared (cancellations or
-- releases) and boxes that disappeared (bookings), with the time between versions.
-- This is the raw material for the app's "windows" feature and for fill-speed curves.
with versions as (
    select
        state_id,
        venue_id,
        service_day,
        party_size,
        valid_from,
        boxes,
        open_boxes,
        lag(state_id) over w as prev_state_id,
        lag(valid_from) over w as prev_valid_from,
        lag(open_boxes) over w as prev_open_boxes
    from {{ ref('fct_night_boxes') }}
    window w as (partition by venue_id, service_day, party_size order by valid_from)
),

open_sets as (
    select state_id, list(box_time order by box_time) as open_times
    from {{ ref('int_night_boxes') }}
    where is_open
    group by state_id
)

select
    v.state_id,
    v.prev_state_id,
    v.venue_id,
    v.service_day,
    v.party_size,
    v.prev_valid_from as observed_before,
    v.valid_from as observed_at,
    date_diff('minute', v.prev_valid_from, v.valid_from) as minutes_between,
    v.boxes,
    v.prev_open_boxes as open_before,
    v.open_boxes as open_after,
    v.open_boxes - v.prev_open_boxes as open_delta,
    list_filter(coalesce(cur.open_times, []), x -> not list_contains(coalesce(prev.open_times, []), x)) as appeared,
    list_filter(coalesce(prev.open_times, []), x -> not list_contains(coalesce(cur.open_times, []), x)) as disappeared
from versions as v
left join open_sets as cur on cur.state_id = v.state_id
left join open_sets as prev on prev.state_id = v.prev_state_id
where v.prev_state_id is not null
