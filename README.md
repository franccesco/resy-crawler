# resy-busyness

How booked is each San Francisco restaurant on Resy for a party of two. A FastAPI
service plus a small UI: start a run, watch the requests, read the ranked table.
The scoring is written up in [METHODOLOGY.md](METHODOLOGY.md).

## Setup

```sh
cp .env.example .env      # paste the key into RESY_API_KEY=
direnv allow              # optional: .envrc auto-loads .env
uv sync
```

To find the key: open resy.com, fetch the `modules/app.*.js` bundle it references,
and search for `apiKey:`. It is the public key the web client itself uses.

## Run

```sh
uv run uvicorn resy_busyness.api:app --reload   # UI at http://127.0.0.1:8000, OpenAPI at /docs
uv run resy-run --days 7 --party 2              # or run once from the CLI
```

A run is two requests per day per party size for seven nights plus one verification
day three weeks out, with two seconds between requests: about 16 requests for one
party size. The API process schedules a run every hour (`RUN_INTERVAL_MINUTES`,
0 to disable; `RUN_ON_START=true` to run immediately at boot). Equivalent crontab:

```
0 * * * * cd /path/to/resy-crawler && uv run resy-run --party 2 >> data/cron.log 2>&1
```

## API

| Method | Path | What |
| --- | --- | --- |
| POST | `/api/runs` | Start a run. Body is `RunParams` (party_sizes, days, verify_offset_days, start_day). 409 if one is running. |
| GET | `/api/runs`, `/api/runs/{id}` | Progress, counters, and the request log. |
| GET | `/api/scheduler` | Interval, next run, active run. |
| GET | `/api/results?run_id=&party_size=2&service=dinner&grid=30&min_nights=2&view=&q=` | Ranked table, scored at read time from the stored observations. Defaults to the latest finished run. |
| GET | `/api/results.csv?…` | Same as CSV; the first line records the scoring parameters. |
| GET | `/api/windows?min_score=0.7&party_size=2&service=dinner&grid=30` | Exclusive restaurants with open boxes, and which boxes appeared since the previous snapshot. |
| GET | `/api/venues/{id}/history?party_size=` | SCD2 version history of a venue's nightly observations. |

## Storage

SQLite at `data/resy.sqlite` with slowly-changing-dimension type 2 tables for
venue attributes and per-night raw observations (all service windows, all open slot
times). Only changed observations open new rows; a row is valid for run R when
`first_run_id <= R <= last_seen_run_id`. Scoring reads these at request time, so
service, grid and party size are query parameters. `data/resy_v1_dinner_only.sqlite`
is the first-generation store whose rows hold only dinner results.

## Layout

- `resy_busyness/` package: `config` (env), `models` (pydantic), `resy_client`, `scoring` (observe at ingestion, score at read), `db` (SCD2), `pipeline` (the run), `scheduler` (hourly), `api` (FastAPI).
- `static/` UI on the Modernist stylesheet from the design mockup.
- `resy_crawler.py` the original one-file sample crawler, kept for reference.
- `data/` outputs: the SQLite store and CSV exports.

## How the Resy API is used

- Auth is the public API key that resy.com's JavaScript bundle ships with,
  sent as `Authorization: ResyAPI api_key="..."` plus `X-Origin: https://resy.com`.
  The key is read from the `RESY_API_KEY` environment variable, never hardcoded.

## Setup

```sh
cp .env.example .env      # then paste the key into RESY_API_KEY=
direnv allow              # optional: .envrc auto-loads .env via direnv
```

Without direnv the script loads `.env` itself. To find the key: open resy.com,
fetch the `modules/app.*.js` bundle it references, and search for `apiKey:`.
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
