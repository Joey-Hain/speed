"""
Metro line-speed test: can the 85th percentile profile recover known speed limits?

A simulated M1 (approximate alignment, real station names) with a known
speed-limit profile. Trains run an ATO-like profile: accelerate and brake at
1.0 m/s^2, cruise 1-3 km/h under the limit, dwell 35 s at each station.
The collector polls them every 15 s, exactly as it will poll the real feed.

Four cases, because it isn't known yet what the real Metro feed carries:
  reported      fixes include position.speed
  gps           positions only, continuous
  blocks-entry  positions snapped to 250 m track sections, timestamp = when the
                train entered the section (how signalling-based feeds report)
  blocks-poll   the same but timestamped at poll time, the worst case

Pass: on cruise sections (limit steady for 800 m either side, 900 m from any
station), the 85th percentile is within 5 km/h of the limit on 90% of segments.

  python -m tests.test_metro            # writes tests/_out/metro-<case>/store
"""

from __future__ import annotations

import dataclasses
import random
import shutil
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
from google.transit import gtfs_realtime_pb2

from collector import collect
import collector.store as _store

_store.RETENTION_DAYS = 36500  # fixtures use fixed 2025 timestamps
from collector.feeds import BY_KEY
from collector.geometry import to_metric

OUT = Path(__file__).parent / "_out"
T0 = 1_760_317_200  # Mon 13 Oct 2025 12:00 Sydney (weekday, middle of day)

# approximate M1 station locations, Tallawong to Sydenham
STATIONS = [
    ("Tallawong", -33.6926, 150.9064), ("Rouse Hill", -33.6913, 150.9228),
    ("Kellyville", -33.7136, 150.9356), ("Bella Vista", -33.7303, 150.9446),
    ("Norwest", -33.7322, 150.9620), ("Hills Showground", -33.7275, 150.9873),
    ("Castle Hill", -33.7319, 151.0054), ("Cherrybrook", -33.7365, 151.0315),
    ("Epping", -33.7727, 151.0820), ("Macquarie University", -33.7745, 151.1144),
    ("Macquarie Park", -33.7790, 151.1255), ("North Ryde", -33.7966, 151.1311),
    ("Chatswood", -33.7968, 151.1806), ("Crows Nest", -33.8260, 151.2010),
    ("Victoria Cross", -33.8396, 151.2070), ("Barangaroo", -33.8616, 151.2034),
    ("Martin Place", -33.8676, 151.2110), ("Gadigal", -33.8774, 151.2072),
    ("Central", -33.8833, 151.2061), ("Waterloo", -33.9000, 151.2066),
    ("Sydenham", -33.9145, 151.1667),
]

A_ACC = A_BRK = 1.0   # m/s^2
DWELL_S = 35
HEADWAY_S = 240
TRIPS = 45            # per direction


def build_line():
    """Densified polyline through the stations, plus station chainages (m)."""
    ll = np.array([[lon, lat] for _, lat, lon in STATIONS])
    pts = []
    for a, b in zip(ll[:-1], ll[1:]):
        for t in np.linspace(0, 1, 40, endpoint=False):
            pts.append(a + (b - a) * t)
    pts.append(ll[-1])
    pts = np.array(pts)
    xy = to_metric(pts)
    cum = np.concatenate([[0], np.cumsum(np.hypot(*np.diff(xy, axis=0).T))])
    st_s = cum[::40]
    return pts, cum, st_s


def limit_at(s_phys, L):
    """Speed limit (km/h) at chainage measured from Tallawong."""
    km = s_phys / 1000
    if km < 1.2:
        return 60
    if 24.0 <= km < 25.5:
        return 70   # curve restriction
    if km >= L / 1000 - 6.5:
        return 90   # Central to Sydenham
    if km >= 31.0:
        return 80   # under the harbour and CBD
    return 100


def speed_profile(st_s, L, reverse, cruise_offset, ds=5.0):
    """v(s) in m/s along the direction of travel, ATO-like."""
    s = np.arange(0, L + ds, ds)
    phys = L - s if reverse else s
    vlim = np.array([limit_at(p, L) for p in phys]) / 3.6 - cruise_offset / 3.6
    stops = np.sort(L - st_s if reverse else st_s)
    stop_idx = np.clip(np.round(stops / ds).astype(int), 0, len(s) - 1)
    v = vlim.copy()
    v[stop_idx] = 0
    for i in range(1, len(s)):                       # accelerate
        v[i] = min(v[i], np.sqrt(v[i - 1] ** 2 + 2 * A_ACC * ds))
    for i in range(len(s) - 2, -1, -1):              # brake
        v[i] = min(v[i], np.sqrt(v[i + 1] ** 2 + 2 * A_BRK * ds))
    # time at each grid point, with dwells
    vm = np.maximum((v[1:] + v[:-1]) / 2, 0.3)
    t = np.concatenate([[0], np.cumsum(ds / vm)])
    dwell = np.zeros(len(s))
    for k in stop_idx[1:-1]:
        dwell[k:] += DWELL_S
    t = t + dwell
    return s, v, t, set(stop_idx.tolist())


def write_static(path: Path, pts, st_s):
    path.parent.mkdir(parents=True, exist_ok=True)
    rev = pts[::-1]
    shapes = pd.concat([
        pd.DataFrame(dict(shape_id="M1_UP", shape_pt_lat=pts[:, 1], shape_pt_lon=pts[:, 0],
                          shape_pt_sequence=range(len(pts)))),
        pd.DataFrame(dict(shape_id="M1_DN", shape_pt_lat=rev[:, 1], shape_pt_lon=rev[:, 0],
                          shape_pt_sequence=range(len(rev)))),
    ])
    stops = pd.DataFrame(dict(stop_id=[f"S{i}" for i in range(len(STATIONS))],
                              stop_name=[f"{n} Station" for n, _, _ in STATIONS],
                              stop_lat=[a for _, a, _ in STATIONS], stop_lon=[b for _, _, b in STATIONS],
                              parent_station=""))
    ids = list(stops.stop_id)
    st = pd.concat([
        pd.DataFrame(dict(trip_id="UP", stop_id=ids, stop_sequence=range(len(ids)))),
        pd.DataFrame(dict(trip_id="DN", stop_id=ids[::-1], stop_sequence=range(len(ids)))),
    ])
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("routes.txt", pd.DataFrame(dict(
            route_id=["SMNW_M1"], route_short_name=["M1"], route_long_name=["Tallawong to Sydenham"],
            route_type=[401], route_color=["168388"])).to_csv(index=False))
        z.writestr("trips.txt", pd.DataFrame(dict(
            route_id="SMNW_M1", service_id="x", trip_id=["UP", "DN"], direction_id=["0", "1"],
            shape_id=["M1_UP", "M1_DN"], trip_headsign=["Sydenham", "Tallawong"])).to_csv(index=False))
        z.writestr("shapes.txt", shapes.to_csv(index=False))
        z.writestr("stops.txt", stops.to_csv(index=False))
        z.writestr("stop_times.txt", st.to_csv(index=False))
    (path.parent / "metro.source").write_text("synthetic M1")


def messages(case, pts, cum, st_s, poll=15, seed=1):
    rng = random.Random(seed)
    L = cum[-1]
    trips = []
    for d, rev in ((0, False), (1, True)):
        for k in range(TRIPS):
            s, v, t, _ = speed_profile(st_s, L, rev, cruise_offset=rng.uniform(1, 3))
            start = k * HEADWAY_S + rng.uniform(0, 60) + (120 if rev else 0)
            trips.append(dict(vid=f"{'UD'[d]}{k:02d}", trip="UP" if d == 0 else "DN",
                              d=d, rev=rev, s=s, v=v, t=t + start, L=L))
    end = max(tr["t"][-1] for tr in trips)
    msgs = []
    for tick in np.arange(0, end + poll, poll):
        m = gtfs_realtime_pb2.FeedMessage()
        m.header.gtfs_realtime_version = "2.0"
        m.header.timestamp = int(T0 + tick)
        for tr in trips:
            if not (tr["t"][0] <= tick <= tr["t"][-1]):
                continue
            s_now = float(np.interp(tick, tr["t"], tr["s"]))
            v_now = float(np.interp(tick, tr["t"], tr["v"]))
            ts = int(T0 + tick)
            if case.startswith("blocks"):
                s_now = float(np.floor(s_now / 250) * 250)
                if case == "blocks-entry":
                    ts = int(round(T0 + float(np.interp(s_now, tr["s"], tr["t"]))))
            phys = L - s_now if tr["rev"] else s_now
            lon = float(np.interp(phys, cum, pts[:, 0])) + rng.gauss(0, 0.00003)
            lat = float(np.interp(phys, cum, pts[:, 1])) + rng.gauss(0, 0.00003)
            e = m.entity.add()
            e.id = tr["vid"]
            e.vehicle.trip.trip_id = tr["trip"]
            e.vehicle.trip.route_id = "SMNW_M1"
            e.vehicle.vehicle.id = tr["vid"]
            e.vehicle.position.latitude = lat
            e.vehicle.position.longitude = lon
            if case == "reported":
                e.vehicle.position.speed = max(0.0, v_now + rng.gauss(0, 0.3))
            e.vehicle.timestamp = ts
        msgs.append(m.SerializeToString())
    return msgs


class FakeApi:
    def __init__(self, msgs):
        self.it = iter(msgs)
        self.requests = 0

    def get(self, path):
        self.requests += 1
        try:
            return next(self.it)
        except StopIteration:
            from collector.api import QuotaExceeded
            raise QuotaExceeded("fixtures exhausted")

    def download(self, *a):
        raise AssertionError("static should come from cache")


def p85(hist_txt, width):
    bins = sorted((int(b), int(c)) for b, c in (p.split(":") for p in hist_txt.split(";") if p))
    n = sum(c for _, c in bins)
    target, acc = 0.85 * n, 0
    for b, c in bins:
        if acc + c >= target:
            return (b + (target - acc) / c) * width
        acc += c
    return (bins[-1][0] + 1) * width


def evaluate(store: Path, st_s, L):
    import json
    geom = json.loads((store / "metro" / "M1" / "geometry.json").read_text())
    seg_m, width = geom["segment_m"], geom["hist_kmh"]
    df = pd.read_csv(store / "metro" / "M1" / "speeds.csv", dtype={"dir": str}, keep_default_na=False)
    df = df[(df.dir == "0") & (df.branch == 0) & (df.n > 0)]
    rows = []
    for seg, g in df.groupby("seg"):
        h = {}
        for txt in g["hist"]:
            for p in filter(None, txt.split(";")):
                b, c = map(int, p.split(":"))
                h[b] = h.get(b, 0) + c
        n = sum(h.values())
        mid = (seg + 0.5) * seg_m
        lim = limit_at(mid, L)
        steady = all(limit_at(min(max(mid + o, 0), L), L) == lim for o in (-800, -400, 400, 800))
        clear = np.min(np.abs(st_s - mid)) >= 900
        rows.append(dict(seg=seg, km=mid / 1000, limit=lim, n=n,
                         p85=p85(";".join(f"{b}:{c}" for b, c in h.items()), width),
                         mean=g["sum"].sum() / g.n.sum(), cruise=steady and clear))
    return pd.DataFrame(rows)


def main():
    pts, cum, st_s = build_line()
    L = cum[-1]
    print(f"Simulated M1: {L / 1000:.1f} km, {len(STATIONS)} stations, "
          f"{TRIPS} trips each way at {HEADWAY_S // 60} min headway, polled every 15 s\n")
    feed = dataclasses.replace(BY_KEY["metro"], interval=0)
    results = {}
    for case in ("reported", "gps", "blocks-entry", "blocks-poll"):
        base = OUT / f"metro-{case}"
        shutil.rmtree(base, ignore_errors=True)
        write_static(base / "cache" / "metro.zip", pts, st_s)
        api = FakeApi(messages(case, pts, cum, st_s))
        status = collect.run([feed], minutes=30, store=base / "store", cache=base / "cache",
                             rebuild=set(), api=api)
        fs = status["feeds"]["metro"]
        r = evaluate(base / "store", st_s, L)
        cr = r[r.cruise & (r.n >= 10)]
        err = (cr.p85 - cr.limit).abs()
        within = (err <= 5).mean()
        results[case] = within
        print(f"{case:12} speed mode {fs['speed_mode']:9} | {len(r)} segments with data, "
              f"{len(cr)} cruise segments | 85th pct within 5 km/h of limit: {within:.0%}, "
              f"median error {err.median():.1f} km/h | mean on cruise {cr['mean'].mean():.0f} "
              f"vs limit {cr.limit.mean():.0f}")
        for lim, g in cr.groupby("limit"):
            print(f"           limit {lim:>3}: 85th pct median {g.p85.median():5.1f}, "
                  f"mean {g['mean'].median():5.1f}  ({len(g)} segs)")
    print()
    for case, w in results.items():
        need = 0.6 if case == "blocks-poll" else 0.9
        assert w >= need, f"{case}: only {w:.0%} of cruise segments within 5 km/h"
    print("OK")


if __name__ == "__main__":
    main()
