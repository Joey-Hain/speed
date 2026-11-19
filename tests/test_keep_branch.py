"""
Kept-branch test (BRANCH_RULES keep_via): a rare service's track is added to a
frozen up/down geometry without disturbing the branches already there.

  python -m tests.test_keep_branch

A line with a Central - Far trunk gets a branch to Spur that leaves the trunk
partway and is timetabled only from Mid. Checks that
  * the appended branch is carried back along the trunk so km count from Central
  * old branches (track, stations, draw flags) are untouched, and the new one
    draws only its own track
  * a delta with the grown geometry merges onto a store with the old one, and a
    delta collected on the old geometry merges onto a grown store, with every
    total kept (merge_delta and combine_delta, both orders)
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np
import pandas as pd

import collector.geometry as geo
import collector.store as st
from collector.delta import _compatible, _extends, combine_delta, merge_delta
from collector.feeds import BY_KEY

st.RETENTION_DAYS = 36500
OUT = Path(__file__).parent / "_out_keep_branch"
FEED = BY_KEY["nswtrains"]
LINE = "Southern_NSW"
SEG = FEED.segment_m


def track(points):
    return [[round(float(a), 6), round(float(b), 6)] for a, b in points]


# trunk west from Central for ~60 km; the spur leaves at ~30 km and heads south
TRUNK = track([(151.2065 - 0.01 * i, -33.8832) for i in range(61)])
SPUR_FROM_MID = track([(151.2065 - 0.01 * i, -33.8832) for i in range(30, 33)] +
                      [(151.2065 - 0.32, -33.8832 - 0.01 * j) for j in range(1, 31)])


def branch(i, ll, stations):
    ml = geo.LineString(geo.to_metric(np.asarray(ll, dtype=float)))
    L = ml.length
    sts = []
    for name, k in stations:
        s = float(geo.shapely.line_locate_point(ml, geo.shapely.Point(geo.to_metric(np.asarray([ll[k]]))[0])))
        sts.append({"name": name, "s": round(s, 1), "lon": ll[k][0], "lat": ll[k][1],
                    "share": 1.0, "regular": True})
    return {"id": i, "shape_id": f"s{i}", "trips": 10, "length_m": round(L, 1), "lonlat": ll,
            "draw": "1" * (int(np.ceil(L / SEG)) or 1), "stations": sts}


def updown(down):
    up = []
    for b in down:
        L = b["length_m"]
        up.append({**b, "lonlat": b["lonlat"][::-1], "stations": sorted(
            ({**x, "s": round(L - x["s"], 1)} for x in b["stations"]), key=lambda x: x["s"])})
    return {"feed": FEED.key, "line": LINE, "mode": "train", "layout": "updown",
            "geometry_version": geo.GEOMETRY_VERSION, "name": "Test", "colour": "000000",
            "segment_m": SEG, "hist_kmh": 1, "built": "x", "stations_version": geo.STATIONS_VERSION,
            "directions": {"down": {"headsign": "Down", "branches": down},
                           "up": {"headsign": "Up", "branches": up}}}


class FakeStatic:
    feed = FEED

    def __init__(self, stations):
        self._st = pd.DataFrame(
            [{"stop_id": n, "station": n, "lon": ll[0], "lat": ll[1]} for n, ll in stations.items()]
        ).set_index("stop_id")

    def stops(self):
        return self._st


def write_line(root: Path, g: dict, rows: list[tuple]):
    d = root / FEED.key / LINE
    d.mkdir(parents=True, exist_ok=True)
    (d / "geometry.json").write_text(json.dumps(g))
    totals = {}
    for dr, b, seg in rows:
        totals[(dr, b, seg, "2026-11-16", "wd", "mid", "all")] = [3, 300.0, 30000.0, {100: 3}, 4, 300.0, 30000.0, {0: 1, 100: 3}]
    st.write_speed_totals(d / "speeds.csv", totals)


def make_delta(root: Path, g: dict, rows):
    shutil.rmtree(root, ignore_errors=True)
    write_line(root, g, rows)
    (root / "meta.json").write_text(json.dumps({
        "source": "local", "version": 7, "status": {}, "metas": {}, "valid": {},
        "lines": [f"{FEED.key}|{LINE}"], "rebuilt": [], "extended": []}))


def n_total(root: Path):
    return sum(t[0] for t in st.read_speed_totals(root / FEED.key / LINE / "speeds.csv").values())


def main():
    shutil.rmtree(OUT, ignore_errors=True)
    stations = {"Central": TRUNK[0], "Mid": TRUNK[30], "Far": TRUNK[60], "Spur": SPUR_FROM_MID[-1]}
    static = FakeStatic(stations)
    old = updown([branch(0, TRUNK, [("Central", 0), ("Mid", 30), ("Far", 60)])])
    # what build_updown gives once keep_via lets the spur through: timetabled from Mid
    built = updown([branch(0, TRUNK, [("Central", 0), ("Mid", 30), ("Far", 60)]),
                    branch(1, SPUR_FROM_MID, [("Mid", 0), ("Spur", len(SPUR_FROM_MID) - 1)])])
    geo.BRANCH_RULES[f"{FEED.key}:{LINE}"] = {"keep_via": ["Spur"]}
    try:
        assert geo.missing_kept(static, old, LINE) == ["Spur"]
        grown = geo.append_branches(old, built, ["Spur"], static)
        assert geo.missing_kept(static, grown, LINE) == []
    finally:
        del geo.BRANCH_RULES[f"{FEED.key}:{LINE}"]

    for d in ("down", "up"):
        ob, gb = old["directions"][d]["branches"], grown["directions"][d]["branches"]
        assert len(gb) == 2 and gb[0] == ob[0], "existing branch must be untouched"
        assert gb[1]["id"] == 1
    spur = grown["directions"]["down"]["branches"][1]
    names = [x["name"] for x in spur["stations"]]
    assert names == ["Central", "Mid", "Spur"], names
    assert spur["stations"][0]["s"] == 0.0 and abs(spur["stations"][1]["s"] - old["directions"]["down"]["branches"][0]["stations"][1]["s"]) < 50
    trunk_len = old["directions"]["down"]["branches"][0]["length_m"]
    assert spur["length_m"] > trunk_len / 2 + 30000, "spur should start at Central"
    drawn = spur["draw"].count("1") * SEG
    assert 25000 < drawn < 40000, f"spur should draw only its own ~33 km, drew {drawn}"
    up_spur = grown["directions"]["up"]["branches"][1]
    assert up_spur["stations"][0]["name"] == "Spur" and up_spur["lonlat"] == spur["lonlat"][::-1]
    assert _extends(grown, old) and not _extends(old, grown) and not _compatible(grown, old)
    assert not _extends(old, old)
    print("append ok:", round(spur["length_m"] / 1000, 1), "km,", len(spur["stations"]), "stations")

    old_rows = [("down", 0, 5), ("up", 0, 7)]
    new_rows = [("down", 0, 5), ("down", 1, 400), ("up", 1, 3)]

    # store on the old geometry, delta on the grown one
    S1 = OUT / "store1"
    write_line(S1, old, old_rows)
    make_delta(OUT / "d_new", grown, new_rows)
    merge_delta(OUT / "d_new", S1)
    assert json.loads((S1 / FEED.key / LINE / "geometry.json").read_text()) == grown, "store should take the grown geometry"
    assert n_total(S1) == 3 * (len(old_rows) + len(new_rows))

    # store on the grown geometry, delta collected on the old one (an old local upload)
    S2 = OUT / "store2"
    write_line(S2, grown, new_rows)
    make_delta(OUT / "d_old", old, old_rows)
    status = merge_delta(OUT / "d_old", S2)
    assert not status.get("skipped_incompatible_lines"), status
    assert json.loads((S2 / FEED.key / LINE / "geometry.json").read_text()) == grown
    assert n_total(S2) == 3 * (len(old_rows) + len(new_rows))

    # combining local deltas either way round keeps everything and the grown geometry
    for first, second in (("d_old", "d_new"), ("d_new", "d_old")):
        make_delta(OUT / "d_old", old, old_rows)
        make_delta(OUT / "d_new", grown, new_rows)
        C = OUT / f"combined_{first}"
        combine_delta(OUT / first, C)
        combine_delta(OUT / second, C)
        assert json.loads((C / FEED.key / LINE / "geometry.json").read_text()) == grown
        assert n_total(C) == 3 * (len(old_rows) + len(new_rows)), (first, n_total(C))

    shutil.rmtree(OUT, ignore_errors=True)
    print("keep_via test passed")


if __name__ == "__main__":
    main()
