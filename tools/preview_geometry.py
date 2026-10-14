"""
Build line geometry from the live static timetables without touching the data,
and summarise it: branches, stations with the share of passing trips that stop,
and how trips classify as express / all stations.

  python tools/preview_geometry.py --feeds sydneytrains,nswtrains --out preview
"""
import argparse, json, sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from collector import geometry as geo
from collector.api import Api
from collector.feeds import BY_KEY
from collector.gtfs_static import load_static

p = argparse.ArgumentParser()
p.add_argument("--feeds", default="sydneytrains,nswtrains")
p.add_argument("--lines", default="")
p.add_argument("--out", default="preview")
p.add_argument("--cache", default="gtfs_cache")
a = p.parse_args()
api = Api()
out = Path(a.out)
want = {x for x in a.lines.split(",") if x}
for fk in a.feeds.split(","):
    feed = BY_KEY[fk]
    st = load_static(feed, api, Path(a.cache))
    lines = sorted(set(st.trips.line))
    for line in lines:
        if want and line not in want:
            continue
        if fk == "nswtrains" and line.isdigit():
            continue  # coaches
        try:
            g = (geo.build_updown if feed.mode in geo.UPDOWN_MODES else geo.build_line)(st, line)
        except Exception as e:
            print(f"{fk} {line}: FAILED {type(e).__name__}: {e}")
            continue
        if g is None:
            continue
        (out / fk / line).mkdir(parents=True, exist_ok=True)
        (out / fk / line / "geometry.json").write_text(json.dumps(g, separators=(",", ":")))
        print(f"\n## {fk} {line}  {g['name']}")
        for d, v in g["directions"].items():
            if d != "down" and "down" in g["directions"]:
                continue
            for b in v["branches"]:
                sts = b["stations"]
                print(f"  {d} b{b['id']}: {sts[0]['name'] if sts else '?'} -> {sts[-1]['name'] if sts else '?'}"
                      f"  {b['length_m']/1000:.1f} km, {b['trips']} trips")
                odd = [f"{x['name']} {x.get('share', 1):.2f}" for x in sts if x.get("share", 1) < 0.95]
                if odd:
                    print("     part-served:", ", ".join(odd))
        # how trips classify on the busiest branch (down), as the collector would
        if g.get("layout") == "updown":
            stations = g["directions"]["down"]["branches"][0]["stations"]
            names = [x["name"] for x in stations if x.get("share", 1) >= geo.REGULAR_SHARE]
            verdict = Counter()
            for tid in st.trips[st.trips.line == line].trip_id[:3000]:
                served = st.served_station_names(tid) or set()
                hit = [i for i, n in enumerate(names) if n in served]
                if len(hit) >= 2:
                    verdict["exp" if any(n not in served for n in names[hit[0]:hit[-1] + 1]) else "all"] += 1
            print(f"  trips on main branch: {dict(verdict)}")
