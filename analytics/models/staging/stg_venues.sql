-- Current version of every venue with its JSON attributes unpacked.
with src as (
    select * from {{ source('resy_store', 'venues') }}
    where is_current = 1
)

select
    venue_id,
    json_extract_string(attrs_json, '$.name') as name,
    json_extract_string(attrs_json, '$.neighborhood') as neighborhood,
    json_extract_string(attrs_json, '$.cuisine') as cuisine,
    json_extract_string(attrs_json, '$.price') as price,
    json_extract_string(attrs_json, '$.phone') as phone,
    json_extract_string(attrs_json, '$.locality') as locality,
    json_extract_string(attrs_json, '$.location_code') as location_code,
    json_extract_string(attrs_json, '$.url') as url,
    json_extract(attrs_json, '$.lat')::double as lat,
    json_extract(attrs_json, '$.lng')::double as lng,
    json_extract(attrs_json, '$.max_party_size')::integer as max_party_size,
    hash as attrs_hash,
    valid_from::timestamptz as valid_from,
    first_run_id,
    last_seen_run_id
from src
