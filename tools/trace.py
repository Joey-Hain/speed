"""
Trace where suspicious speeds on a line come from.

  python tools/trace.py --line T8 --dir 0 --from-km 6 --to-km 7.5 --over 85 --minutes 25

Runs the real Sydney Trains collector code against the live feed (geometry
from --store, the data branch) and, whenever a speed above --over lands in
the given stretch, prints the vehicle's recent fixes and the span that was
added. Nothing is written to the data.
"""
import argparse, sys, time
from collections import defaultdict, deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from google.transit import gtfs_realtime_pb2
from collector import collect
from collector.api import Api
from collector.feeds import BY_KEY
from collector.speed import Accumulator

p = argparse.ArgumentParser()
p.add_argument("--feed", default="sydneytrains")
p.add_argument("--line", required=True)
p.add_argument("--dir", default="0")
p.add_argument("--from-km", type=float, required=True)
p.add_argument("--to-km", type=float, required=True)
p.add_argument("--over", type=float, default=85)
p.add_argument("--minutes", type=float, default=25)
p.add_argument("--store", default="store")
p.add_argument("--cache", default="gtfs_cache")
a = p.parse_args()

feed = BY_KEY[a.feed]
api = Api()
acc = Accumulator()
r = collect.FeedRunner(feed, api, Path(a.store), Path(a.cache), acc, set())
r.gps_runs = {}
hist = defaultdict(lambda: deque(maxlen=10))
stop_of, cur = {}, {"vid": None}
seen = set()

real_update = r.est.update
def update(vid, tid, d, pos, ts, obs):
    hist[vid].append((obs, ts, tid, d, {b: round(s) for b, s in pos.items()}, stop_of.get(vid, "")))
    cur["vid"] = vid
    return real_update(vid, tid, d, pos, ts, obs)
r.est.update = update

real_add = acc.add
def add(fk, line, d, b, seg, ts, kmh_run, kmh_all, width=5, svc="all"):
    seg_m = feed.segment_m
    hit = (line == a.line and d == a.dir and a.from_km * 1000 <= seg * seg_m <= a.to_km * 1000
           and max(kmh_run or 0, kmh_all or 0) > a.over)
    if hit and (cur["vid"], ts, b) not in seen:
        seen.add((cur["vid"], ts, b))
        print(f"\n== {line} dir {d} branch {b} seg {seg} ({seg*seg_m/1000:.2f} km): "
              f"running {kmh_run and round(kmh_run)} with stops {kmh_all and round(kmh_all)}  vehicle {cur['vid']}")
        for obs, t, tid, dd, pos, sid in hist[cur["vid"]]:
            print(f"   header {obs} ts {t} ({t-obs:+d}s) trip {tid[:30]} dir {dd} pos {pos} stop {sid}")
        sys.stdout.flush()
    return real_add(fk, line, d, b, seg, ts, kmh_run, kmh_all, width, svc)
acc.add = add

real_process = r.process
def process(msg):
    for e in msg.entity:
        if e.HasField("vehicle"):
            v = e.vehicle
            stop_of[v.vehicle.id or v.vehicle.label or v.trip.trip_id or e.id] = v.stop_id
    return real_process(msg)
r.process = process

end = time.time() + a.minutes * 60
polls = 0
while time.time() < end:
    t0 = time.time()
    try:
        r.poll()
        polls += 1
    except Exception as e:
        print("poll failed:", e)
    time.sleep(max(0, feed.interval - (time.time() - t0)))
print(f"\n{polls} polls, {len(seen)} suspicious spans")
