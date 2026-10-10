"""
Offline end-to-end test: fake static GTFS + fake realtime feeds through the
collector, no API key or network needed.

  python -m tests.test_synthetic            # writes to tests/_out/store

Simulates
  * a track-circuit train line (positions snap to 400 m blocks, station dwells,
    no speed field), checking the block estimator recovers the cruise speed
  * a GPS bus route that reports speed in m/s
"""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
from google.transit import gtfs_realtime_pb2

from collector import collect
import collector.store as _store

_store.RETENTION_DAYS = 36500  # fixtures use fixed 2025 timestamps
from collector.feeds import Feed

OUT = Path(__file__).parent / "_out"
T0 = 1_760_000_000  # a Thursday morning in Sydney

TRAIN = Feed("sydneytrains", "Test trains", "train", "/rt/train", ("/static/train",),
             speed="estimated", interval=15, segment_m=250, tolerance_m=150)
BUS = Feed("buses", "Test buses", "bus", "/rt/bus", ("/static/bus",),
           speed="auto", interval=30, segment_m=200, tolerance_m=60)


def line_coords(n=300, length_deg=0.2):
    s = np.linspace(0, 1, n)
    return -33.87 + 0.02 * np.sin(s * 3), 151.21 - length_deg * s


def write_static(path: Path, short: str, lat, lon, stations_idx):
    path.parent.mkdir(parents=True, exist_ok=True)
    rev_lat, rev_lon = lat[::-1], lon[::-1]
    shapes = pd.concat([
        pd.DataFrame(dict(shape_id="UP", shape_pt_lat=lat, shape_pt_lon=lon,
                          shape_pt_sequence=range(len(lat)))),
        pd.DataFrame(dict(shape_id="DN", shape_pt_lat=rev_lat, shape_pt_lon=rev_lon,
                          shape_pt_sequence=range(len(lat)))),
    ])
    stops = pd.DataFrame(dict(
        stop_id=[f"S{i}" for i in stations_idx],
        stop_name=[f"Station {k} Platform 1" for k in range(len(stations_idx))],
        stop_lat=lat[stations_idx], stop_lon=lon[stations_idx], parent_station=""))
    st = pd.concat([
        pd.DataFrame(dict(trip_id="T_UP", stop_id=[f"S{i}" for i in stations_idx],
                          stop_sequence=range(len(stations_idx)))),
        pd.DataFrame(dict(trip_id="T_DN", stop_id=[f"S{i}" for i in stations_idx[::-1]],
                          stop_sequence=range(len(stations_idx)))),
    ])
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("routes.txt", pd.DataFrame(dict(
            route_id=["R1"], route_short_name=[short], route_long_name=[f"{short} test line"],
            route_type=[2], route_color=["F99D1C"])).to_csv(index=False))
        z.writestr("trips.txt", pd.DataFrame(dict(
            route_id="R1", service_id="x", trip_id=["T_UP", "T_DN"],
            direction_id=["0", "1"], shape_id=["UP", "DN"],
            trip_headsign=["Station 5", "Station 0"])).to_csv(index=False))
        z.writestr("shapes.txt", shapes.to_csv(index=False))
        z.writestr("stops.txt", stops.to_csv(index=False))
        z.writestr("stop_times.txt", st.to_csv(index=False))
    (path.parent / (path.stem + ".source")).write_text("synthetic")


class FakeApi:
    """Serves pre-built feed messages; one per poll."""

    def __init__(self, messages: dict[str, list[bytes]]):
        self.messages = {k: iter(v) for k, v in messages.items()}
        self.requests = 0
        self.done = set()

    def get(self, path):
        self.requests += 1
        try:
            return next(self.messages[path])
        except StopIteration:
            self.done.add(path)
            from collector.api import ApiError, QuotaExceeded
            if self.done == set(self.messages):
                raise QuotaExceeded("fixtures exhausted")  # ends the run
            raise ApiError("no more fixtures")

    def download(self, path, dest):
        raise AssertionError("static should come from cache")


def cum_dist(lat, lon):
    from collector.geometry import to_metric
    xy = to_metric(np.column_stack([lon, lat]))
    return np.concatenate([[0], np.cumsum(np.hypot(*np.diff(xy, axis=0).T))])


def at_distance(lat, lon, cd, s):
    return np.interp(s, cd, lat), np.interp(s, cd, lon)


def train_messages(lat, lon, stations_idx, polls=240, dt=15, cruise_kmh=80, block_m=400):
    """Two trains, one each way. Position reported only at block boundaries."""
    cd = cum_dist(lat, lon)
    L = cd[-1]
    st_s = cd[stations_idx]
    v = cruise_kmh / 3.6
    msgs = []

    def kinematics(t):
        """distance along line at time t: cruise between stations, 30 s dwell at each."""
        s, clock = 0.0, 0.0
        for a, b in zip(st_s[:-1], st_s[1:]):
            run = (b - a) / v
            if t < clock + run:
                return a + (t - clock) * v
            clock += run
            if t < clock + 30:
                return b
            clock += 30
        return L

    first_seen = {}
    for k in range(polls):
        t = k * dt
        m = gtfs_realtime_pb2.FeedMessage()
        m.header.gtfs_realtime_version = "2.0"
        m.header.timestamp = T0 + t
        for vid, trip, d, rev in [("A1", "T_UP", 0, False), ("B2", "T_DN", 1, True)]:
            s_true = kinematics(t)
            s_block = np.floor(s_true / block_m) * block_m  # track circuit snap
            key = (vid, s_block)
            first_seen.setdefault(key, T0 + t)
            s_report = L - s_block if rev else s_block
            la, lo = at_distance(lat, lon, cd, s_report)
            e = m.entity.add()
            e.id = vid
            e.vehicle.trip.trip_id = trip
            e.vehicle.trip.route_id = "R1"
            e.vehicle.vehicle.id = vid
            e.vehicle.position.latitude = float(la)
            e.vehicle.position.longitude = float(lo)
            e.vehicle.timestamp = first_seen[key]  # timestamp = circuit entry
        msgs.append(m.SerializeToString())
    return msgs


def bus_messages(lat, lon, polls=60, dt=30, kmh=35):
    cd = cum_dist(lat, lon)
    msgs = []
    for k in range(polls):
        t = k * dt
        m = gtfs_realtime_pb2.FeedMessage()
        m.header.gtfs_realtime_version = "2.0"
        m.header.timestamp = T0 + t
        s = min(t * kmh / 3.6, cd[-1])
        la, lo = at_distance(lat, lon, cd, s)
        e = m.entity.add()
        e.id = "bus1"
        e.vehicle.trip.trip_id = "T_UP"
        e.vehicle.vehicle.id = "bus1"
        e.vehicle.position.latitude = float(la) + 0.00005  # ~5 m GPS noise
        e.vehicle.position.longitude = float(lo)
        e.vehicle.position.speed = kmh / 3.6 + (0.5 if k % 2 else -0.5)
        e.vehicle.timestamp = T0 + t
        msgs.append(m.SerializeToString())
    return msgs


def main():
    shutil.rmtree(OUT, ignore_errors=True)
    cache, store = OUT / "cache", OUT / "store"
    lat, lon = line_coords()
    stations = [0, 60, 120, 180, 240, 299]
    write_static(cache / "sydneytrains.zip", "TX", lat, lon, stations)
    blat, blon = line_coords(150, 0.05)
    write_static(cache / "buses.zip", "999", blat, blon, [0, 149])

    api = FakeApi({"/rt/train": train_messages(lat, lon, stations),
                   "/rt/bus": bus_messages(blat, blon)})
    for f in (TRAIN, BUS):
        object.__setattr__(f, "interval", 0)  # replay as fast as possible

    status = collect.run([TRAIN, BUS], minutes=0.1, store=store, cache=cache,
                         rebuild=set(), api=api)
    print({k: {kk: vv for kk, vv in v.items() if kk != "unmatched_examples"}
           for k, v in status["feeds"].items()})

    tr = pd.read_csv(store / "sydneytrains" / "TX" / "speeds.csv")
    tr["mean"] = tr["sum"] / tr["n"]
    overall = tr["sum"].sum() / tr["n"].sum()
    print(f"train: {len(tr)} cells, sample-weighted mean {overall:.1f} km/h (true cruise 80)")
    assert 70 < overall < 90, "block estimator should recover ~80 km/h"
    assert set(tr["dir"].astype(str)) == {"0", "1"}
    with_stops = tr["sum_all"].sum() / tr["n_all"].sum()
    print(f"train with stops: {with_stops:.1f} km/h (30 s dwells pull it down)")
    assert with_stops < overall, "dwell time should lower the with-stops average"
    dwell_cells = (tr["sum_all"] / tr["n_all"]) < 0.8 * (tr["sum"] / tr["n"])
    assert dwell_cells.any(), "blocks starting at a station stop should slow right down with stops"
    assert tr.loc[tr["n"] > 0, "hist"].str.len().gt(0).all()

    bu = pd.read_csv(store / "buses" / "999" / "speeds.csv")
    bmean = bu["sum"].sum() / bu["n"].sum()
    print(f"bus: {len(bu)} cells, mean {bmean:.1f} km/h (true 35)")
    assert abs(bmean - 35) < 2

    assert (store / "index.json").exists() and (store / "status.json").exists()
    print("OK")


if __name__ == "__main__":
    main()
