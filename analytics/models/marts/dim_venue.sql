-- One row per venue: current attributes plus the latest run's rating and its
-- S–F tier using the same cuts as resy_busyness.scoring.rating_tier.
with latest_run as (
    select max(run_id) as run_id from {{ ref('stg_runs') }} where status = 'done'
),

ratings as (
    select r.venue_id, r.rating_avg, r.rating_count
    from {{ ref('stg_venue_ratings') }} as r
    join latest_run as l on l.run_id = r.run_id
)

select
    v.venue_id,
    v.name,
    v.neighborhood,
    v.cuisine,
    v.price,
    v.phone,
    v.locality,
    v.location_code,
    v.url,
    v.lat,
    v.lng,
    v.max_party_size,
    r.rating_avg,
    r.rating_count,
    case
        when r.rating_avg is null or coalesce(r.rating_count, 0) < 20 then null
        when r.rating_avg >= 4.85 then 'S'
        when r.rating_avg >= 4.70 then 'A'
        when r.rating_avg >= 4.55 then 'B'
        when r.rating_avg >= 4.40 then 'C'
        when r.rating_avg >= 4.00 then 'D'
        else 'F'
    end as rating_tier
from {{ ref('stg_venues') }} as v
left join ratings as r on r.venue_id = v.venue_id
