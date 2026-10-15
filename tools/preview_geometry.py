"""
Build line geometry from the live static timetables without touching the data,
and summarise it: branches, stations with the share of passing trips that stop,
and every stopping pattern with how the collector would classify it.

  python tools/preview_geometry.py --feeds sydneytrains,nswtrains --out preview

Patterns are per branch (down direction): trips that stop at two or more of
the branch's regular stations, grouped by where they join and leave the branch
and which regular stations they skip, weighted by days run in the next four
weeks. The verdict is what the collector records (express if the trip skips a
regular station on every branch it runs).
"""
import argparse, json, sys
from collections import Counter, defaultdict
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


def regular(b):
    return [x["name"] for x in b["stations"] if x.get("share", 1) >= geo.REGULAR_SHARE]


def patterns(st, line, g):
    branches = g["directions"]["down"]["branches"]
    regs = [regular(b) for b in branches]
    days = st.trip_days()
    trips = st.trips[st.trips.line == line]
    per_branch = [defaultdict(lambda: [0, 0, None]) for _ in branches]  # key -> [days, trips, verdict]
    totals = Counter()
    for tid in trips.trip_id:
        w = days.get(tid, 0)
        if not w:
            continue
        served = st.served_station_names(tid) or set()
        found = []
        for i, names in enumerate(regs):
            hit = [k for k, n in enumerate(names) if n in served]
            if len(hit) < 2:
                continue
            span = names[hit[0]:hit[-1] + 1]
            skipped = tuple(n for n in span if n not in served)
            found.append((i, span[0], span[-1], skipped))
        if not found:
            totals["unplaced"] += w
            continue
        # the collector only compares against branches the fix lies on; a trip
        # all-stations on any branch it uses counts as all-stations there
        verdict = "all" if any(not f[3] for f in found) else "exp"
        totals[verdict] += w
        for i, s0, s1, skipped in found:
            e = per_branch[i][(s0, s1, skipped)]
            e[0] += w
            e[1] += 1
            e[2] = "all" if not skipped else ("exp" if verdict == "exp" else "all*")
    return per_branch, totals


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
        per_branch, totals = patterns(st, line, g)
        tot = sum(totals.values()) or 1
        print(f"  trip-days: " + ", ".join(f"{k} {v / tot:.0%}" for k, v in totals.most_common()))
        for b, pats in zip(g["directions"]["down"]["branches"], per_branch):
            sts = b["stations"]
            print(f"  b{b['id']} {sts[0]['name'] if sts else '?'} -> {sts[-1]['name'] if sts else '?'}"
                  f"  {b['length_m'] / 1000:.1f} km, {b['trips']} trip-days")
            print("    stations: " + ", ".join(
                n["name"] + ("" if n.get("share", 1) >= 0.95 else f" {n['share']:.2f}") for n in sts))
            btot = sum(v[0] for v in pats.values()) or 1
            for (s0, s1, skipped), (w, n, verdict) in sorted(pats.items(), key=lambda kv: -kv[1][0])[:12]:
                if w / btot < 0.01:
                    break
                skip = ("skips " + ", ".join(skipped)) if skipped else "all stops"
                print(f"    {w / btot:5.1%} {verdict:4} {s0} - {s1}: {skip}")
