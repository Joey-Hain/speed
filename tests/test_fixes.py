"""
Checks for three fixes:

  python -m tests.test_fixes

  * loop shapes (City Circle): a first fix where the shape passes twice is
    held back on rail lines (it could be either pass), but not on buses
  * a feed that clamps reported speed (L1 at 50.4 km/h): at the ceiling,
    speed comes from movement, so a 70 km/h tram reads ~70 not 50
  * the estimator honours a per-feed minimum span
"""

from __future__ import annotations

import shutil

import numpy as np
import pandas as pd
from google.transit import gtfs_realtime_pb2

from tests import test_synthetic as ts
from collector import collect
from collector.feeds import Feed
from collector.geometry import LineGeometry, to_metric
from collector.speed import SpeedEstimator

OUT = ts.Path(__file__).parent / "_out_fixes"


def loop_check():
    th = np.linspace(0, 2 * np.pi * 1.05, 400)  # round the loop and a bit past the start
    lonlat = np.column_stack([151.2 + 0.01 * np.cos(th), -33.88 + 0.01 * np.sin(th)])
    data = {"segment_m": 250, "mode": "train",
            "directions": {"0": {"branches": [{"id": 0, "lonlat": lonlat.tolist()}]}}}
    g = LineGeometry(data)
    x, y = to_metric(lonlat[:1])[0]
    assert g.project("0", x, y, 150) == [], "ambiguous first fix on a loop should wait"
    x2, y2 = to_metric(lonlat[100:101])[0]
    assert len(g.project("0", x2, y2, 150)) == 1, "a fix away from the overlap places normally"
    L = g.directions["0"][0].length
    (b, s), = g.project("0", x, y, 150, {0: L - 300})
    assert s > L - 1500, f"with a hint near the end it stays at the end ({s:.0f} of {L:.0f})"
    data["mode"] = "bus"
    assert len(LineGeometry(data).project("0", x, y, 150)) == 1, "buses aren't held back"
    print("loop: ok")


def ceiling_check():
    cache, store = OUT / "cache", OUT / "store"
    lat, lon = ts.line_coords()
    ts.write_static(cache / "tram.zip", "LX", lat, lon, [0, 299])
    feed = Feed("tram", "Test tram", "lightrail", "/rt/tram", ("/static/tram",),
                speed="auto", interval=0, segment_m=100, tolerance_m=80, speed_ceiling_kmh=50.4)
    cd = ts.cum_dist(lat, lon)
    v = 70 / 3.6
    msgs = []
    for k in range(int(cd[-1] / v / 15)):
        t = k * 15
        la, lo = ts.at_distance(lat, lon, cd, min(t * v, cd[-1]))
        m = gtfs_realtime_pb2.FeedMessage()
        m.header.gtfs_realtime_version = "2.0"
        m.header.timestamp = ts.T0 + t
        e = m.entity.add()
        e.id = "A"
        e.vehicle.trip.trip_id = "T_UP"
        e.vehicle.trip.route_id = "R1"
        e.vehicle.vehicle.id = "A"
        e.vehicle.position.latitude = float(la)
        e.vehicle.position.longitude = float(lo)
        e.vehicle.position.speed = min(v, 14.0)  # the feed's clamp
        e.vehicle.timestamp = ts.T0 + t
        msgs.append(m.SerializeToString())
    status = collect.run([feed], 0.1, store, cache, set(), api=ts.FakeApi({"/rt/tram": msgs}))
    s = status["feeds"]["tram"]
    df = pd.read_csv(store / "tram" / "LX" / "speeds.csv")
    mean = df["sum"].sum() / df["n"].sum()
    print(f"ceiling: {s.get('fixes_at_speed_ceiling')} fixes at the ceiling, running mean {mean:.1f} km/h (true 70)")
    assert s.get("fixes_at_speed_ceiling", 0) > 10
    assert abs(mean - 70) < 4, "speeds at the ceiling should come from movement"


def span_check():
    for span in (45, 90):
        est = SpeedEstimator(span)
        out = []
        for k in range(20):
            out += est.update("v", "t", "0", {0: k * 15 * 20.0}, k * 15, k * 15)
        gaps = {round((z - a) / 20.0) for _, a, z, *_ in out}
        assert gaps == {span // 15 * 15 + (15 if span % 15 else 0)} or gaps == {span}, (span, gaps)
    print("min span: ok")


def backwards_check():
    # the airport line case: Central (stopped) -> 6454 -> 6917 -> 6279 (misplaced
    # circuit) -> 7455, at ~15-40 s steps. Before the fix the glitch became a
    # 'stop' and 6279 -> 7455 in 33 s was recorded at 128 km/h
    est = SpeedEstimator(90)
    seq = [(5982, 0), (5982, 15), (5982, 45), (5982, 75), (6454, 75 + 74), (6917, 75 + 112),
           (6279, 75 + 130), (7455, 75 + 163), (8000, 75 + 190), (8600, 75 + 215), (9200, 75 + 240)]
    out = []
    for s, t in seq:
        out += est.update("v", "t", "0", {0: float(s)}, t, t)
    fast = [(a, z, m, ma) for _, a, z, m, ma in out if max(m or 0, ma or 0) * 3.6 > 100]
    assert not fast, f"backwards glitch produced {fast}"
    # same place, train skips straight from Central to the misplaced circuit, with
    # the feed running ~10-30 s behind the vehicle timestamps (as it does). The
    # old dwell check compared feed time with the circuit entry time, so 6279
    # looked like a stop and 6279 -> 7455 became a 128 km/h departure
    est = SpeedEstimator(90)
    seq = [(5982, 870, 941), (5982, 870, 961), (5982, 870, 1001), (6279, 1043, 1051), (6279, 1043, 1066),
           (6279, 1043, 1086), (7455, 1076, 1096), (8000, 1100, 1111), (8600, 1125, 1131), (9200, 1150, 1161)]
    out2 = []
    for s_, t, obs in seq:
        out2 += est.update("v", "t", "0", {0: float(s_)}, t, obs)
    fast = [(a, z, m, ma) for _, a, z, m, ma in out2 if max(m or 0, ma or 0) * 3.6 > 100]
    assert not fast, f"feed lag made a false stop: {fast}"
    print("backwards glitch: ok", [(round(a), round(z), m and round(m * 3.6), ma and round(ma * 3.6)) for _, a, z, m, ma in out])


def main():
    shutil.rmtree(OUT, ignore_errors=True)
    loop_check()
    ceiling_check()
    span_check()
    backwards_check()
    print("OK")


if __name__ == "__main__":
    main()
