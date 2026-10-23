"""
Route extension test: a metro line that gets longer is rebuilt and its old
totals carried over, not dropped.

  python -m tests.test_extend

Builds a short line A-C, gives it totals, then a timetable whose trips from
today run A-E. Checks that
  * a one-day diversion over new track doesn't trigger a rebuild
  * the extension does, the store's totals keep their sum, and segments move
    by the length of the new track in the direction that now starts further out
  * a delta flagged "extended" is merged onto an old store the same way
"""

from __future__ import annotations

import json
import shutil
import zipfile
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

import collector.geometry as geo
import collector.store as st
from collector.delta import merge_delta
from collector.feeds import BY_KEY
from collector.gtfs_static import StaticGtfs

st.RETENTION_DAYS = 36500
OUT = Path(__file__).parent / "_out_extend"
FEED = BY_KEY["metro"]
TODAY = date(2026, 10, 30)


def coords(n_stations):
    """Stations every ~1.5 km heading east; points every ~100 m."""
    lon0, lat = 151.0, -33.9
    lons = lon0 + np.arange(0, (n_stations - 1) * 15 + 1) * 0.00108
    return lons, np.full(len(lons), lat)


def write_static(path: Path, n_stations: int, extra_day_only: bool = False):
    """Line from station 0 eastwards. Regular trips run n_stations; with
    extra_day_only the regular trips stop at 3 stations and a one-day diversion
    runs to n_stations."""
    lons, lats = coords(n_stations)
    reg_n = 3 if extra_day_only else n_stations
    shapes, trips, st_rows, stops, cal, cd = [], [], [], [], [], []
    for k in range(n_stations):
        stops.append(dict(stop_id=f"S{k}", stop_name=f"Stop {k}", stop_lat=lats[k * 15],
                          stop_lon=lons[k * 15], parent_station=""))

    def add(shape, n, svc, count):
        pts = list(range(0, (n - 1) * 15 + 1))
        for direction, seq in (("1", pts), ("0", pts[::-1])):
            sid = f"{shape}{direction}"
            shapes.extend(dict(shape_id=sid, shape_pt_lat=lats[i], shape_pt_lon=lons[i], shape_pt_sequence=j)
                          for j, i in enumerate(seq))
            order = list(range(n)) if direction == "1" else list(range(n))[::-1]
            for t in range(count):
                tid = f"{sid}_{svc}_{t}"
                trips.append(dict(route_id="M1", service_id=svc, trip_id=tid, direction_id=direction,
                                  shape_id=sid, trip_headsign=f"Stop {order[-1]}"))
                st_rows.extend(dict(trip_id=tid, stop_id=f"S{s}", stop_sequence=q) for q, s in enumerate(order))

    add("REG", reg_n, "daily", 20)
    cal.append(dict(service_id="daily", monday=1, tuesday=1, wednesday=1, thursday=1, friday=1, saturday=1,
                    sunday=1, start_date=(TODAY - timedelta(5)).strftime("%Y%m%d"),
                    end_date=(TODAY + timedelta(60)).strftime("%Y%m%d")))
    if extra_day_only:
        add("DIV", n_stations, "oneday", 20)
        cd.append(dict(service_id="oneday", date=TODAY.strftime("%Y%m%d"), exception_type=1))
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("routes.txt", pd.DataFrame([dict(route_id="M1", route_short_name="M1",
                   route_long_name="Test metro", route_type=401, route_color="168388")]).to_csv(index=False))
        z.writestr("trips.txt", pd.DataFrame(trips).to_csv(index=False))
        z.writestr("shapes.txt", pd.DataFrame(shapes).to_csv(index=False))
        z.writestr("stops.txt", pd.DataFrame(stops).to_csv(index=False))
        z.writestr("stop_times.txt", pd.DataFrame(st_rows).to_csv(index=False))
        z.writestr("calendar.txt", pd.DataFrame(cal).to_csv(index=False))
        z.writestr("calendar_dates.txt", pd.DataFrame(cd, columns=["service_id", "date", "exception_type"]).to_csv(index=False))
    return StaticGtfs(FEED, path, "test")


def fake_totals(line_dir: Path, geom: dict):
    rows, pos = [], []
    for d, v in geom["directions"].items():
        n = int(v["branches"][0]["length_m"] // 100)
        for seg in range(n):
            rows.append(dict(dir=d, branch=0, seg=seg, week="2026-10-26", day="wd", band="am", svc="all",
                             n=10, sum=600.0 + seg, sumsq=36000.0, hist="60:10", n_all=12, sum_all=600.0,
                             sumsq_all=36000.0, hist_all="0:2;60:10"))
            pos.append(dict(dir=d, branch=0, seg=seg, fixes=5, tc=0))
    pd.DataFrame(rows, columns=st.COLS).to_csv(line_dir / "speeds.csv", index=False)
    pd.DataFrame(pos).to_csv(line_dir / "positions.csv", index=False)


def main():
    shutil.rmtree(OUT, ignore_errors=True)
    short = write_static(OUT / "short.zip", 3)
    old = geo.build_line(short, "M1")
    assert old and all(len(v["branches"]) == 1 for v in old["directions"].values())

    # a one-day diversion over new track is not an extension
    div = write_static(OUT / "div.zip", 5, extra_day_only=True)
    assert not geo.extension_needed(div, old, "M1", TODAY), "one-day diversion triggered a rebuild"

    longer = write_static(OUT / "long.zip", 5)
    assert geo.extension_needed(longer, old, "M1", TODAY), "extension not detected"
    new = geo.build_line(longer, "M1")
    plan = geo.remap_plan(old, new, 100)
    added = (new["directions"]["1"]["branches"][0]["length_m"] - old["directions"]["1"]["branches"][0]["length_m"]) / 100
    assert plan["1"][0] == (0, 0), plan          # starts at stop 0 both times
    assert abs(plan["0"][0][1] - added) <= 1, plan  # now starts 3 km further east

    # store side
    line_dir = OUT / "store" / "metro" / "M1"
    line_dir.mkdir(parents=True)
    (line_dir / "geometry.json").write_text(json.dumps(old))
    fake_totals(line_dir, old)
    n0 = pd.read_csv(line_dir / "speeds.csv").n.sum()
    seg0 = pd.read_csv(line_dir / "speeds.csv").query("dir == 0").seg.min()
    st.remap_line(line_dir, plan)
    sp = pd.read_csv(line_dir / "speeds.csv")
    assert sp.n.sum() == n0, "totals lost"
    assert sp.query("dir == 0").seg.min() == seg0 + plan["0"][0][1]
    assert sp.query("dir == 1").seg.min() == 0

    # delta side: an "extended" delta merged onto a store still on the old geometry
    store2 = OUT / "store2"
    (store2 / "metro" / "M1").mkdir(parents=True)
    (store2 / "metro" / "M1" / "geometry.json").write_text(json.dumps(old))
    fake_totals(store2 / "metro" / "M1", old)
    delta = OUT / "delta"
    (delta / "metro" / "M1").mkdir(parents=True)
    (delta / "metro" / "M1" / "geometry.json").write_text(json.dumps(new))
    fake_totals(delta / "metro" / "M1", new)
    (delta / "meta.json").write_text(json.dumps({"source": "actions", "version": 7, "status": {}, "metas": {},
                                                 "valid": {"metro": ["M1"]}, "lines": ["metro|M1"],
                                                 "rebuilt": [], "extended": ["metro|M1"]}))
    nd = pd.read_csv(delta / "metro" / "M1" / "speeds.csv").n.sum()
    merge_delta(delta, store2)
    merged = pd.read_csv(store2 / "metro" / "M1" / "speeds.csv")
    assert merged.n.sum() == n0 + nd, (merged.n.sum(), n0, nd)
    g2 = json.loads((store2 / "metro" / "M1" / "geometry.json").read_text())
    assert abs(g2["directions"]["0"]["branches"][0]["length_m"] - new["directions"]["0"]["branches"][0]["length_m"]) < 1
    print("extension test passed: plan", plan)


if __name__ == "__main__":
    main()
