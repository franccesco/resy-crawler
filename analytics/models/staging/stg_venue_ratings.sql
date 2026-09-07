select
    run_id,
    venue_id,
    avg as rating_avg,
    count as rating_count
from {{ source('resy_store', 'venue_ratings') }}
