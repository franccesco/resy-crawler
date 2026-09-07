# Methodology: the busyness score

**Question.** For a party of two, how much of each San Francisco restaurant's dinner
book on Resy is already gone?

**Answer shape.** One number per restaurant between 0 and 1. 0 means every dinner
half-hour is still bookable; 1 means nothing is left on any night. The table is
sorted by this number, descending, with every input that produced it visible.

## Source

Everything comes from one undocumented endpoint the resy.com web client uses,
`POST https://api.resy.com/3/venuesearch/search`, authenticated with the public API
key the client ships in its JavaScript bundle. We ask for San Francisco venues
(`venue_filter.location_code = "sf"`) with `availability: true` and a
`slot_filter` of `{day, party_size: 2}`. For every venue the response carries:

- `availability.notify_options`: the service windows in which the restaurant seats
  a party of that size that day, one per service type (dinner is type 2), as a
  first and last seating time in 30-minute steps.
- `availability.slots`: the tables still bookable, as start times at 15-minute
  granularity, each tagged with a seating area.
- `availability.events`: ticketed events that day.
- Flags used for exclusions: `is_tock_inventory`, `source.name`, `reopen.date`.

No other data source is needed. Review counts are also returned and shown as a
size hint, but they are not part of the score.

## One night

1. **Boxes.** Build the half-hour grid from the first to the last dinner seating in
   `notify_options` (inclusive). Rintaro on a Saturday, 17:00 to 22:00, is 11 boxes.
2. **Open boxes.** Snap every slot start time down to its half-hour box. A box is
   open if any slot lands in it. Snapping matters: a restaurant that offers 7:00,
   7:15, 7:30 and 7:45 would otherwise look twice as open as one offering 7:00 and
   7:30.
3. **Nightly ratio** = (boxes − open boxes) ÷ boxes.

A night with no dinner window for two is skipped, not counted as full. The
restaurant may be closed that day, or seat two only at lunch.

## The week

Nights are tomorrow through seven days out, Pacific time. The score is the plain
mean of the nightly ratios over nights that had a dinner window. Seven days gives
one of each weekday and stays inside the horizon in which nearly every restaurant
has released its tables. Holidays and special dates will skew individual weeks;
the run window is a parameter so it can be widened later.

## Who is not scored

The exercise asks that empty never pass for full. Each rule maps to a field:

| Reason | Signal | Outcome |
| --- | --- | --- |
| Closed (temporary or permanent) | `reopen.date` in the future | excluded |
| Books on another platform | `is_tock_inventory` true, or `source.name` set | excluded |
| Events only | events in the week, never a dinner window for two | excluded |
| No dinner service for two | no dinner window on any night (lunch-only, walk-in, or seats two nowhere) | excluded |
| Fewer than 2 nights with a window | too little to average | excluded |
| Zero open boxes every night | one extra look 21 days out. Tables there: genuinely sold out, scored 1.00 with that evidence. Still nothing: Resy holds no real two-top inventory for this venue | scored or excluded |

Excluded venues are listed with the reason and the evidence, and they are held out
of the ranked set entirely.

## Normalization

The nightly ratio is already a proportion of the restaurant's own dinner window, so
a 14-seat counter and a 200-seat hall are compared on the same 0 to 1 scale of
"how much of what you offer is gone". No percentile ranking or tiering is applied
on top; the plain ratio was chosen for transparency. The inputs (taken and total
boxes per night, open times on hover) are shown next to every score.

## Snapshots and change tracking

The book changes constantly, so every run is a snapshot with a timestamp, and the
store is slowly-changing-dimension type 2. Resy has no "changed since" endpoint,
so each run re-reads the full state (two pages per day, about 16 requests with a
two-second gap). What is stored is only change: a venue's descriptive attributes
and each (venue, night) availability state get a new row only when their hash
differs from the current row; otherwise the current row's `last_seen_run_id` is
bumped. Ratings, which move every run, are kept per run outside the SCD2 hash so
they do not churn versions. `GET /api/venues/{id}/history` exposes the versions.

## Limitations and judgment calls

- The seating window comes from the "notify me" range. If a restaurant's real last
  seating differs from that range the box count is off by one at the edges.
- A far-out check at 21 days assumes inventory is released at least three weeks
  ahead. A restaurant that releases only 14 days ahead and is sold out all week
  would be excluded as "no inventory" rather than scored 1.00.
- Some venues carry Resy's San Francisco location code but sit in Berkeley or
  Oakland (Chez Panisse, Belotti). They are kept because the exercise is "on Resy
  in San Francisco" as Resy defines it; a city-limits filter is one line if wanted.
- Dinner only. Lunch and brunch windows are read and stored but not scored.
- One snapshot says nothing about fill speed. Daily runs against the same store
  would, since each night's state history is kept.
