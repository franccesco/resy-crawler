# resy-crawler

Lists San Francisco restaurants on Resy using the web client's undocumented API
(`POST https://api.resy.com/3/venuesearch/search`).

## How it works

- Auth is the public API key that resy.com's JavaScript bundle ships with,
  sent as `Authorization: ResyAPI api_key="..."` plus `X-Origin: https://resy.com`.
  Override with `RESY_API_KEY` if Resy rotates it.
- The server caps `per_page` at 75 regardless of what you ask for.
- Three ways to scope to SF, chosen with `--strategy`:
  - `location` (default): `venue_filter.location_code = "sf"` (149 venues at last check)
  - `bbox`: `geo.bounding_box = [south, west, north, east]` over SF city limits
  - `radius`: `geo.{latitude, longitude, radius}` in meters from downtown
  Each returns a slightly different set, so a full list would union all three.

## Polite defaults

One request, at most 50 venues. Pagination is opt-in.

```sh
python3 resy_crawler.py                   # 1 request, up to 50 venues
python3 resy_crawler.py --max-venues 20   # smaller sample
python3 resy_crawler.py --max-pages 2 --delay 3   # opt into pagination
```

Output: `data/sf_venues.csv` and `data/sf_venues.json`, plus a table on stdout.

## Columns

resy_id, name, neighborhood, cuisine, price ($–$$$$), rating_avg, rating_count,
phone, lat, lng, locality, location_code, max_party_size, global_dining_access,
collections, url_slug, url, source_strategy.
