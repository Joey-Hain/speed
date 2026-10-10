"""
Line geometry: reference branches per direction, frozen once written.

A line (route short name) can have several shapes per direction: branches
(T1 to Emu Plains vs Richmond), short workings, deviations. We pick branches
greedily: the most-used shape first, then any shape that runs mostly away from
the ones already picked. Every fix is projected onto every branch it lies near,
so shared trunk sections feed all branches and each branch's distance-speed
profile is complete. The map draws each piece of track only once.

Once geometry.json exists for a line it is reused as-is, so segment numbers in
speeds.csv stay valid even when TfNSW reissues shape ids. Delete a line's
folder (or run with --rebuild LINE) to rebuild it after a real route change.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path

import numpy as np
import shapely
from pyproj import Transformer
from shapely.geometry import LineString

from .gtfs_static import StaticGtfs

METRIC_CRS = "EPSG:7856"  # GDA2020 / MGA zone 56; fine across NSW for distances
_FWD = Transformer.from_crs("EPSG:4326", METRIC_CRS, always_xy=True)

MAX_BRANCHES = 6
STATION_MODES = {"train", "metro", "lightrail", "ferry"}


def to_metric(lonlat: np.ndarray) -> np.ndarray:
    x, y = _FWD.transform(lonlat[:, 0], lonlat[:, 1])
    return np.column_stack([x, y])


class Branch:
    def __init__(self, data: dict, segment_m: int):
        self.id = data["id"]
        self.lonlat = np.asarray(data["lonlat"], dtype=float)
        self.line = LineString(to_metric(self.lonlat))
        self.length = self.line.length
        self.n_seg = int(np.ceil(self.length / segment_m)) or 1


class LineGeometry:
    def __init__(self, data: dict):
        self.data = data
        self.segment_m = data["segment_m"]
        self.hist_kmh = data.get("hist_kmh", 5)
        self.directions: dict[str, list[Branch]] = {
            d: [Branch(b, self.segment_m) for b in v["branches"]]
            for d, v in data["directions"].items()
        }

    def project(self, direction: str, x: float, y: float, tolerance: float):
        """[(branch_id, s)] for every branch of `direction` within tolerance."""
        branches = self.directions.get(direction)
        if not branches:
            return []
        p = shapely.Point(x, y)
        out = []
        for b in branches:
            if b.line.distance(p) <= tolerance:
                out.append((b.id, float(b.line.project(p))))
        return out

    def n_seg(self, direction: str, branch_id: int) -> int:
        return self.directions[direction][branch_id].n_seg


# --------------------------------------------------------------------- build

def _pick_branches(shape_ids: list[str], shapes: dict, tol: float) -> list[str]:
    chosen, chosen_lines = [], []
    for sid in shape_ids:
        if sid not in shapes or len(shapes[sid]) < 2:
            continue
        line = LineString(to_metric(shapes[sid]))
        if not chosen:
            chosen.append(sid)
            chosen_lines.append(line)
            continue
        # sample every ~100 m and see how much of it is new track
        n = max(int(line.length // 100), 2)
        pts = shapely.points([line.interpolate(i / (n - 1), normalized=True).coords[0]
                              for i in range(n)])
        near = np.zeros(n, dtype=bool)
        for cl in chosen_lines:
            near |= shapely.distance(cl, pts) <= tol
        new_len = (~near).mean() * line.length
        if (~near).mean() > 0.2 and new_len > 500:
            chosen.append(sid)
            chosen_lines.append(line)
        if len(chosen) >= MAX_BRANCHES:
            break
    return chosen


NOT_IN_SERVICE = r"(?i)\b(?:empty train|not in service|out of service|non[ _-]?revenue|depot)\b"


def build_line(static: StaticGtfs, line: str) -> dict | None:
    feed = static.feed
    trips = static.trips[(static.trips.line == line) & (static.trips.shape_id != "")]
    # empty runs shouldn't pick the branches or name the directions
    in_service = trips[~trips.trip_headsign.str.contains(NOT_IN_SERVICE, regex=True)]
    if not in_service.empty:
        trips = in_service
    if trips.empty:
        return None
    shapes = static.shapes()
    meta = static.line_meta(line)
    tol = max(feed.tolerance_m, 50)

    directions = {}
    station_trips = {}
    for d, dt in trips.groupby("direction_id"):
        ranked = [s for s, _ in Counter(dt.shape_id).most_common()]
        picked = _pick_branches(ranked, shapes, tol)
        if not picked:
            continue
        headsign = Counter(h for h in dt.trip_headsign if h).most_common(1)
        branches, metric_lines = [], []
        for i, sid in enumerate(picked):
            # ~2 m simplification keeps geometry.json small without moving the track
            ll = np.asarray(LineString(shapes[sid]).simplify(0.00002).coords)
            ml = LineString(to_metric(ll))
            n_seg = int(np.ceil(ml.length / feed.segment_m)) or 1
            # draw a segment on the map unless an earlier branch already covers it
            mids = shapely.points([ml.interpolate(min((k + 0.5) * feed.segment_m, ml.length)).coords[0]
                                   for k in range(n_seg)])
            covered = np.zeros(n_seg, dtype=bool)
            for prev in metric_lines:
                covered |= shapely.distance(prev, mids) <= tol
            metric_lines.append(ml)
            branches.append({
                "id": i,
                "shape_id": sid,
                "trips": int((dt.shape_id == sid).sum()),
                "length_m": round(ml.length, 1),
                "lonlat": [[round(float(a), 6), round(float(b), 6)] for a, b in ll],
                "draw": "".join("0" if c else "1" for c in covered),
                "stations": [],
            })
            station_trips[(d, i)] = list(dt.loc[dt.shape_id == sid, "trip_id"].iloc[:25])
        directions[d] = {
            "headsign": headsign[0][0] if headsign else "",
            "branches": branches,
        }
    if not directions:
        return None

    if feed.mode in STATION_MODES:
        _add_stations(static, directions, station_trips, tol)

    for d in directions.values():
        if not d["headsign"]:
            last = d["branches"][0]["stations"]
            d["headsign"] = last[-1]["name"] if last else ""

    return {
        "feed": feed.key,
        "line": line,
        "mode": feed.mode,
        "name": meta["name"],
        "colour": meta["colour"],
        "segment_m": feed.segment_m,
        "hist_kmh": feed.hist_width,
        "built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "static_source": static.source_path,
        "directions": directions,
    }


def _add_stations(static, directions, station_trips, tol):
    # stations come from the trip on each shape that stops most often, so a
    # short working (Central to Wyong on a Newcastle shape) can't truncate them
    stop_lists = static.stops_for_trips({t for ts in station_trips.values() for t in ts})
    stops = static.stops()
    for (d, i), trip_ids in station_trips.items():
        best = max(trip_ids, key=lambda t: len(stop_lists.get(t, [])))
        ids = [s for s in stop_lists.get(best, []) if s in stops.index]
        if not ids:
            continue
        st = stops.loc[ids]
        b = directions[d]["branches"][i]
        line = LineString(to_metric(np.asarray(b["lonlat"])))
        xy = to_metric(st[["lon", "lat"]].to_numpy(dtype=float))
        pts = shapely.points(xy)
        s = shapely.line_locate_point(line, pts)
        off = shapely.distance(line, pts)
        seen = set()
        for name, si, oi, lat, lon in zip(st.station, s, off, st.lat, st.lon):
            if oi <= tol * 2 and name not in seen:
                seen.add(name)
                b["stations"].append({"name": name, "s": round(float(si), 1),
                                      "lat": round(float(lat), 6), "lon": round(float(lon), 6)})


# ---------------------------------------------------------------- persistence

def geometry_path(store: Path, feed_key: str, line: str) -> Path:
    return store / feed_key / line / "geometry.json"


def load_or_build(store: Path, static: StaticGtfs, line: str, rebuild: bool = False):
    p = geometry_path(store, static.feed.key, line)
    if p.exists() and not rebuild:
        return LineGeometry(json.loads(p.read_text()))
    data = build_line(static, line)
    if data is None:
        return None
    if rebuild:  # old segment numbers no longer apply
        (p.parent / "speeds.csv").unlink(missing_ok=True)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, separators=(",", ":")))
    return LineGeometry(data)
