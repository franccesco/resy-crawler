#!/usr/bin/env python3
"""List San Francisco restaurants on Resy via api.resy.com/3/venuesearch/search.

Polite by default: one request, capped at --max-venues rows, --delay seconds
between any additional pages. Pagination is opt-in via --max-pages.

Usage:
  python3 resy_crawler.py                       # 1 request, up to 50 venues
  python3 resy_crawler.py --max-venues 20       # smaller sample
  python3 resy_crawler.py --max-pages 3         # opt into pagination (75/page)
  python3 resy_crawler.py --strategy bbox       # location | bbox | radius
"""
import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request

API_BASE = "https://api.resy.com"
SEARCH_PATH = "/3/venuesearch/search"
# Public key shipped in resy.com's web client JS bundle. Override with RESY_API_KEY.
DEFAULT_API_KEY = "VbWk7s3L4KiK5fzlO7JD3Q5EYolJI7n5"
PAGE_SIZE_CAP = 75  # server silently caps per_page at 75

SF = {
    "location_code": "sf",
    "center": {"latitude": 37.7749, "longitude": -122.4194},
    "radius_m": 12000,
    # [south, west, north, east] -- the shape the web client sends for map searches
    "bbox": [37.70, -122.53, 37.84, -122.35],
}
PRICE = {1: "$", 2: "$$", 3: "$$$", 4: "$$$$"}


def headers(api_key):
    return {
        "Authorization": f'ResyAPI api_key="{api_key}"',
        "X-Origin": "https://resy.com",
        "Origin": "https://resy.com",
        "Referer": "https://resy.com/",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
        ),
    }


def search(body, api_key, retries=3):
    req = urllib.request.Request(
        API_BASE + SEARCH_PATH, data=json.dumps(body).encode(), headers=headers(api_key), method="POST"
    )
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < retries - 1:
                wait = 5 * (attempt + 1)
                print(f"HTTP {e.code}; backing off {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            raise SystemExit(f"HTTP {e.code} from Resy: {e.read()[:300]!r}")


def body_for(strategy, page, per_page):
    body = {"types": ["venue"], "query": "", "page": page, "per_page": per_page}
    if strategy == "location":
        body["geo"] = SF["center"]
        body["venue_filter"] = {"location_code": SF["location_code"]}
    elif strategy == "bbox":
        body["geo"] = {"bounding_box": SF["bbox"]}
    elif strategy == "radius":
        body["geo"] = {**SF["center"], "radius": SF["radius_m"]}
    else:
        raise ValueError(strategy)
    return body


def normalize(hit, strategy):
    loc = hit.get("location") or {}
    geo = hit.get("_geoloc") or {}
    rating = hit.get("rating") or {}
    slug = hit.get("url_slug")
    return {
        "resy_id": (hit.get("id") or {}).get("resy"),
        "name": hit.get("name"),
        "neighborhood": hit.get("neighborhood"),
        "cuisine": "; ".join(hit.get("cuisine") or []),
        "price": PRICE.get(hit.get("price_range_id"), ""),
        "rating_avg": round(rating["average"], 2) if rating.get("average") is not None else None,
        "rating_count": rating.get("count"),
        "phone": (hit.get("contact") or {}).get("phone_number"),
        "lat": geo.get("lat"),
        "lng": geo.get("lng"),
        "locality": hit.get("locality"),
        "location_code": loc.get("code"),
        "max_party_size": hit.get("max_party_size"),
        "global_dining_access": bool(hit.get("is_global_dining_access")),
        "collections": "; ".join(c.get("short_name", "") for c in hit.get("collections") or []),
        "url_slug": slug,
        "url": f"https://resy.com/cities/{loc.get('url_slug', 'san-francisco-ca')}/venues/{slug}" if slug else None,
        "source_strategy": strategy,
    }


def is_sf(row):
    return row["location_code"] == "sf" or row["locality"] == "San Francisco"


def crawl(strategy, max_venues, max_pages, delay, api_key):
    per_page = min(PAGE_SIZE_CAP, max_venues)
    venues, total, pages_hit = {}, None, 0
    for page in range(1, max_pages + 1):
        if page > 1:
            time.sleep(delay)
        data = search(body_for(strategy, page, per_page), api_key)
        s = data.get("search") or {}
        total = s.get("nbHits", total)
        hits = s.get("hits") or []
        pages_hit += 1
        for h in hits:
            row = normalize(h, strategy)
            if row["resy_id"] and is_sf(row):
                venues.setdefault(row["resy_id"], row)
            if len(venues) >= max_venues:
                break
        if len(venues) >= max_venues or page >= (s.get("nbPages") or 1) or not hits:
            break
    return list(venues.values()), total, pages_hit


def write_outputs(rows, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    csv_path, json_path = os.path.join(out_dir, "sf_venues.csv"), os.path.join(out_dir, "sf_venues.json")
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    with open(json_path, "w") as f:
        json.dump(rows, f, indent=1, ensure_ascii=False)
    return csv_path, json_path


def print_table(rows, n):
    cols = ["resy_id", "name", "neighborhood", "cuisine", "price", "rating_avg", "rating_count"]
    sample = rows[:n]
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in sample)) for c in cols}
    line = "| " + " | ".join(c.ljust(widths[c]) for c in cols) + " |"
    print(line)
    print("|" + "|".join("-" * (widths[c] + 2) for c in cols) + "|")
    for r in sample:
        print("| " + " | ".join(str(r.get(c, "") if r.get(c) is not None else "").ljust(widths[c]) for c in cols) + " |")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--strategy", choices=["location", "bbox", "radius"], default="location")
    ap.add_argument("--max-venues", type=int, default=50, help="hard cap on rows collected (default 50)")
    ap.add_argument("--max-pages", type=int, default=1, help="max API requests; 1 = no pagination (default)")
    ap.add_argument("--delay", type=float, default=2.0, help="seconds between page requests (default 2)")
    ap.add_argument("--out", default="data", help="output directory (default data/)")
    ap.add_argument("--show", type=int, default=15, help="rows to print (default 15)")
    args = ap.parse_args()

    api_key = os.environ.get("RESY_API_KEY", DEFAULT_API_KEY)
    rows, total, pages_hit = crawl(args.strategy, args.max_venues, args.max_pages, args.delay, api_key)
    if not rows:
        raise SystemExit("No venues returned.")
    rows.sort(key=lambda r: (r["name"] or "").lower())
    csv_path, json_path = write_outputs(rows, args.out)
    print(f"strategy={args.strategy} requests={pages_hit} server_total_hits={total} collected={len(rows)}")
    print(f"wrote {csv_path} and {json_path}\n")
    print_table(rows, args.show)


if __name__ == "__main__":
    main()
