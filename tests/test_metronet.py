"""
MetroNet beacon areas (north of Wyee, Bell to Lithgow): the Sydney Trains feed
reports a train at the last beacon (a station) it passed, with that beacon's
time, until it passes the next one.

  python -m tests.test_metronet

One train runs through 6 stations at 80 km/h with 30 s dwells, reported only
by beacons read on arrival. Expect station-to-station averages in the
with-stops totals, nothing in the running totals (every hop has a stop in it),
and no bogus dwells from the frozen position.
"""

from __future__ import annotations

import shutil

import numpy as np
import pandas as pd
from google.transit import gtfs_realtime_pb2

from tests import test_synthetic as ts
from collector import collect

OUT = ts.Path(__file__).parent / "_out_metronet"


def beacon_messages(lat, lon, stations_idx, polls=240, dt=15, kmh=80, dwell=30):
    cd = ts.cum_dist(lat, lon)
    st_s = cd[stations_idx]
    v = kmh / 3.6
    arrive, clock = [0.0], 0.0
    for a, b in zip(st_s[:-1], st_s[1:]):
        clock += 30 if len(arrive) > 1 else 0
        clock += (b - a) / v
        arrive.append(clock)
    msgs = []
    for k in range(polls):
        t = k * dt
        m = gtfs_realtime_pb2.FeedMessage()
        m.header.gtfs_realtime_version = "2.0"
        m.header.timestamp = ts.T0 + t
        i = max(j for j, x in enumerate(arrive) if x <= t)
        la, lo = ts.at_distance(lat, lon, cd, st_s[i])
        e = m.entity.add()
        e.id = "A1"
        e.vehicle.trip.trip_id = "T_UP"
        e.vehicle.trip.route_id = "R1"
        e.vehicle.vehicle.id = "A1"
        e.vehicle.stop_id = f"MetroNet.{600 + i}"
        e.vehicle.position.latitude = float(la)
        e.vehicle.position.longitude = float(lo)
        e.vehicle.timestamp = int(ts.T0 + arrive[i])
        msgs.append(m.SerializeToString())
    gaps = np.diff(st_s)
    expect = [g / ((g / v) + (dwell if n else 0)) * 3.6 for n, g in enumerate(gaps)]
    return msgs, expect


def main():
    shutil.rmtree(OUT, ignore_errors=True)
    cache, store = OUT / "cache", OUT / "store"
    lat, lon = ts.line_coords()
    stations = [0, 60, 120, 180, 240, 299]
    ts.write_static(cache / "sydneytrains.zip", "TX", lat, lon, stations)
    msgs, expect = beacon_messages(lat, lon, stations)
    object.__setattr__(ts.TRAIN, "interval", 0)
    status = collect.run([ts.TRAIN], 0.1, store, cache, set(), api=ts.FakeApi({"/rt/train": msgs}))
    s = status["feeds"]["sydneytrains"]
    print({k: s.get(k) for k in ("fixes_metronet", "samples_beacon", "samples_estimated", "fixes_track_circuit")})
    df = pd.read_csv(store / "sydneytrains" / "TX" / "speeds.csv")
    assert df["n"].sum() == 0, "every hop includes a stop, so no running speed"
    assert s["samples_beacon"] == len(stations) - 1, s
    got = df["sum_all"].sum() / df["n_all"].sum()
    want = float(np.mean(expect))
    print(f"with-stops mean {got:.1f} km/h, expected about {want:.1f}")
    assert abs(got - want) < 3
    assert s.get("samples_estimated", 0) == 0, "frozen beacon positions must not reach the track-circuit estimator"
    print("OK")


if __name__ == "__main__":
    main()
