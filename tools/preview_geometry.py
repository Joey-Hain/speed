"""
Build line geometry from the live static timetables without touching the data,
and summarise it: branches, stations with the share of passing trips that stop,
and every stopping pattern with how the collector would classify it.

  python tools/preview_geometry.py --feeds sydneytrains,nswtrains --out preview
  python tools/preview_geometry.py --cache <dir with the zips> --max-age 999   # offline

Patterns are per branch (down direction): trips that stop at two or more of
the branch's regular stations, grouped by where they join and leave the branch
and which regular stations they skip, weighted by days run in the next four
weeks. A pattern that skips a regular station is recorded as express there.
"""
import argparse, json, sys
from collections import defaultdict
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
p.add_argument("--max-age", type=float, default=3, help="days before the cached timetable is re-downloaded")
a = p.parse_args()
api = Api()
out = Path(a.out)
want = {x for x in a.lines.split(",") if x}


def patterns(st, line, g):
    """Per branch: {(first, last, skipped regular stations): [trip-days, trips]}
    over the trips that run along it, as the collector classifies them there."""
    import numpy as np
    from shapely.geometry import LineString
    branches = g["directions"]["down"]["branches"]
    trips = st.trips[(st.trips.line == line) & (st.trips.shape_id != "")]
    trips = trips[~trips.trip_headsign.str.contains(geo.NOT_IN_SERVICE, regex=True)]
    lines = [LineString(geo.to_metric(np.asarray(b["lonlat"]))) for b in branches]
    runs = geo._branch_runs(st, trips, lines, max(st.feed.tolerance_m, 50))
    out, placed = [], set()
    for b, (best, rs) in zip(branches, runs):
        names = [x["name"] for x in b["stations"] if x.get("regular")]
        pats = defaultdict(lambda: [0, 0])
        for t, served, lo, hi, w in rs:
            hit = [k for k, n in enumerate(names) if n in served]
            if len(hit) < 2:
                continue
            span = names[hit[0]:hit[-1] + 1]
            e = pats[(span[0], span[-1], tuple(n for n in span if n not in served))]
            e[0] += w
            e[1] += 1
            placed.add(t)
        out.append(pats)
    days = st.trip_days()
    unplaced = sum(days.get(t, 0) for t in trips.trip_id if t not in placed)
    total = sum(days.get(t, 0) for t in trips.trip_id) or 1
    return out, unplaced / total


for fk in a.feeds.split(","):
    feed = BY_KEY[fk]
    st = load_static(feed, api, Path(a.cache), max_age_days=a.max_age)
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
            print(f"{fk} {line}: no geometry")
            continue
        (out / fk / line).mkdir(parents=True, exist_ok=True)
        (out / fk / line / "geometry.json").write_text(json.dumps(g, separators=(",", ":")))
        print(f"\n## {fk} {line}  {g['name']}")
        if g.get("layout") != "updown":
            continue
        per_branch, unplaced = patterns(st, line, g)
        print(f"  trip-days on no branch: {unplaced:.0%}")
        for b, pats in zip(g["directions"]["down"]["branches"], per_branch):
            sts = b["stations"]
            print(f"  b{b['id']} {sts[0]['name'] if sts else '?'} -> {sts[-1]['name'] if sts else '?'}"
                  f"  {b['length_m'] / 1000:.1f} km, {b['trips']} trip-days")
            print("    stations: " + ", ".join(
                n["name"] + ("" if n.get("share", 1) >= 0.95 else f" {n['share']:.2f}")
                + ("" if n.get("regular") else " (minor)") for n in sts))
            btot = sum(v[0] for v in pats.values()) or 1
            exp = sum(v[0] for k, v in pats.items() if k[2]) / btot
            print(f"    express {exp:.0%}")
            for (s0, s1, skipped), (w, n) in sorted(pats.items(), key=lambda kv: -kv[1][0])[:12]:
                if w / btot < 0.01:
                    break
                skip = ("exp skips " + ", ".join(skipped)) if skipped else "all stops"
                print(f"    {w / btot:5.1%} {s0} - {s1}: {skip}")
