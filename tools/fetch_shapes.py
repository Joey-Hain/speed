"""
Fetch real route shapes for the speed tests.

Run from the repo root (it reads TFNSW_API_KEY from the repo's .env):

    python tools/fetch_shapes.py

It downloads the static GTFS for Metro, Sydney Trains, all light rail, Sydney
Ferries and buses, keeps only routes, trips, shapes, stops and one trip's stop
list per shape, and writes shapes_bundle.zip (a few MB). Attach that file in
the chat. Buses are trimmed to a handful of routes because the full bus
timetable is large.
"""

import io
import os
import sys
import zipfile
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

KEY = os.getenv("TFNSW_API_KEY", "").strip()
if not KEY:
    sys.exit("TFNSW_API_KEY not set (put it in .env at the repo root)")

API = "https://api.transport.nsw.gov.au"
FEEDS = {  # name: (candidate static paths, route short names to keep or None for all)
    "metro": (["/v2/gtfs/schedule/metro"], None),
    "sydneytrains": (["/v2/gtfs/schedule/sydneytrains", "/v1/gtfs/schedule/sydneytrains"], None),
    "lightrail-innerwest": (["/v2/gtfs/schedule/lightrail/innerwest", "/v1/gtfs/schedule/lightrail/innerwest"], None),
    "lightrail-cbdandsoutheast": (["/v1/gtfs/schedule/lightrail/cbdandsoutheast"], None),
    "lightrail-parramatta": (["/v1/gtfs/schedule/lightrail/parramatta"], None),
    "ferries-sydneyferries": (["/v1/gtfs/schedule/ferries/sydneyferries"], None),
    "buses": (["/v1/gtfs/schedule/buses"], {"333", "370", "M30", "B1", "610X", "T80", "199"}),
}
KEEP = ["agency.txt", "routes.txt", "trips.txt", "shapes.txt", "stops.txt", "stop_times.txt"]


def read_csv(z, name):
    import csv
    names = {n.split("/")[-1]: n for n in z.namelist()}
    if name not in names:
        return [], []
    with z.open(names[name]) as f:
        r = csv.reader(io.TextIOWrapper(f, "utf-8-sig"))
        head = next(r)
        return head, list(r)


def write_csv(out, path, head, rows):
    import csv
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(head)
    w.writerows(rows)
    out.writestr(path, buf.getvalue())


def trim(feed, data, keep_lines, out):
    z = zipfile.ZipFile(io.BytesIO(data))
    rh, routes = read_csv(z, "routes.txt")
    ri = {h: i for i, h in enumerate(rh)}
    if keep_lines:
        routes = [r for r in routes if r[ri["route_short_name"]] in keep_lines]
    route_ids = {r[ri["route_id"]] for r in routes}
    th, trips = read_csv(z, "trips.txt")
    ti = {h: i for i, h in enumerate(th)}
    trips = [t for t in trips if t[ti["route_id"]] in route_ids]
    shape_ids = {t[ti["shape_id"]] for t in trips if "shape_id" in ti}
    one_trip = {}
    for t in trips:
        one_trip.setdefault(t[ti["shape_id"]], t[ti["trip_id"]])
    sample_trips = set(one_trip.values())
    sh, shapes = read_csv(z, "shapes.txt")
    si = {h: i for i, h in enumerate(sh)}
    shapes = [s for s in shapes if s[si["shape_id"]] in shape_ids]
    sth, st = [], []
    names = {n.split("/")[-1]: n for n in z.namelist()}
    import csv
    with z.open(names["stop_times.txt"]) as f:
        r = csv.reader(io.TextIOWrapper(f, "utf-8-sig"))
        sth = next(r)
        k = sth.index("trip_id")
        st = [row for row in r if row[k] in sample_trips]
    stop_ids = {row[sth.index("stop_id")] for row in st}
    sph, stops = read_csv(z, "stops.txt")
    pi = {h: i for i, h in enumerate(sph)}
    parents = {s[pi["parent_station"]] for s in stops if s[pi["stop_id"]] in stop_ids and "parent_station" in pi}
    stops = [s for s in stops if s[pi["stop_id"]] in stop_ids or s[pi["stop_id"]] in parents]
    for name, head, rows in [("routes.txt", rh, routes), ("trips.txt", th, trips), ("shapes.txt", sh, shapes),
                             ("stops.txt", sph, stops), ("stop_times.txt", sth, st)]:
        write_csv(out, f"{feed}/{name}", head, rows)
    ah, agency = read_csv(z, "agency.txt")
    if ah:
        write_csv(out, f"{feed}/agency.txt", ah, agency)
    print(f"  {feed}: {len(routes)} routes, {len(shape_ids)} shapes, {len(trips)} trips")


def main():
    s = requests.Session()
    s.headers["Authorization"] = f"apikey {KEY}"
    with zipfile.ZipFile("shapes_bundle.zip", "w", zipfile.ZIP_DEFLATED) as out:
        for feed, (paths, keep) in FEEDS.items():
            for p in paths:
                print(f"Downloading {feed} ({p}) ...", flush=True)
                try:
                    r = s.get(API + p, timeout=600)
                    r.raise_for_status()
                    trim(feed, r.content, keep, out)
                    out.writestr(f"{feed}/source.txt", p)
                    break
                except Exception as e:
                    print(f"  failed: {e}")
    print(f"\nWrote shapes_bundle.zip ({Path('shapes_bundle.zip').stat().st_size / 1e6:.1f} MB). Attach it in the chat.")


if __name__ == "__main__":
    main()
