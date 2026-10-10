"""
Dump raw GTFS-realtime vehicle entities for inspection.

  python tools/diagnose.py --feed nswtrains --polls 3 --match "rail"
  python tools/diagnose.py --feed sydneytrains --match "Bomaderry|Kiama|ST\\d"

--match is a regex tested against each entity's text; "rail" keeps entities whose
route is a train in static GTFS (route_type 2 or 1xx).
"""
import argparse, re, sys, time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from google.protobuf import text_format
from google.transit import gtfs_realtime_pb2
from collector.api import Api
from collector.feeds import BY_KEY
from collector.gtfs_static import load_static, _is_rail

p = argparse.ArgumentParser()
p.add_argument("--feed", required=True)
p.add_argument("--polls", type=int, default=2)
p.add_argument("--match", default="")
p.add_argument("--limit", type=int, default=60)
p.add_argument("--rt", default="", help="override the realtime path, e.g. /v2/gtfs/vehiclepos/nswtrains")
a = p.parse_args()
feed = BY_KEY[a.feed]
api = Api()
st = load_static(feed, api, Path("gtfs_cache"))
print("realtime path:", a.rt or feed.rt)
rtype = st.routes["route_type"].to_dict() if "route_type" in st.routes else {}
shown = 0
speeds = defaultdict(list)
stopkinds = defaultdict(Counter)   # route -> kind of stop_id
nonloc = defaultdict(list)         # route -> examples of non-Loc stop_ids with position
for i in range(a.polls):
    m = gtfs_realtime_pb2.FeedMessage()
    m.ParseFromString(api.get(a.rt or feed.rt))
    for e in m.entity:
        if not e.HasField("vehicle"):
            continue
        v = e.vehicle
        rid = v.trip.route_id or (st.trip_info.get(v.trip.trip_id, ("",))[0])
        is_rail = _is_rail(rtype.get(rid, ""))
        txt = text_format.MessageToString(e, as_one_line=True)
        if a.match == "rail":
            if not is_rail:
                continue
        elif a.match and not re.search(a.match, txt):
            continue
        key = f"{rid} rail={is_rail} line={st.route_line.get(rid, '?')}"
        sid = v.stop_id
        kind = "Loc" if sid.endswith(" Loc") else ("empty" if not sid else "other")
        stopkinds[key][kind] += 1
        if kind != "Loc" and len(nonloc[key]) < 40:
            nonloc[key].append(f"{v.vehicle.label or v.vehicle.id} stop_id={sid!r} status={v.current_status} "
                               f"{v.position.latitude:.5f},{v.position.longitude:.5f} ts={v.timestamp}")
        speeds[key].append(round(v.position.speed * 3.6, 1) if v.position.HasField("speed") else None)
        if shown < a.limit:
            print(txt[:900]); shown += 1
    if i < a.polls - 1:
        time.sleep(20)
print("\nstop_id kinds by route:")
for k, c in sorted(stopkinds.items()):
    print(" ", k, dict(c))
print("\nnon-Loc positions by route:")
for k, v in sorted(nonloc.items()):
    print(" ", k)
    for x in v:
        print("     ", x)
print("\nspeed (km/h) by route:")
for k, v in sorted(speeds.items()):
    print(" ", k, v[:12])
