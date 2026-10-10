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
from shapely.ops import substring

from .gtfs_static import StaticGtfs

METRIC_CRS = "EPSG:7856"  # GDA2020 / MGA zone 56; fine across NSW for distances
_FWD = Transformer.from_crs("EPSG:4326", METRIC_CRS, always_xy=True)

MAX_BRANCHES = 6
HINT_BACK_M = 1500    # projection window around a vehicle's previous position
HINT_AHEAD_M = 8000
AMBIGUOUS_GAP_M = 1500  # other passes of the shape closer along the line than this don't count
STATIONS_VERSION = 4  # 2: served stops only (no pass-through stations or track-circuit points)
                      # 3: every station the line serves along the branch, not one trip's
                      # 4: (refresh only)
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
        # only rail shapes are checked for passing the same spot twice: bus
        # routes that run out and back along one street would never place
        self.rail = data.get("mode") in ("train", "metro", "lightrail")
        self.directions: dict[str, list[Branch]] = {
            d: [Branch(b, self.segment_m) for b in v["branches"]]
            for d, v in data["directions"].items()
        }

    def project(self, direction: str, x: float, y: float, tolerance: float,
                hints: dict[int, float] | None = None):
        """[(branch_id, s)] for every branch of `direction` within tolerance.

        A shape can pass the same track twice (the City Circle loop on T2, T3
        and T8), where the nearest point may be on the wrong pass. With a hint
        (the vehicle's previous s on that branch) the fix is placed within a
        window around it, so the position can't jump to the other pass.
        """
        branches = self.directions.get(direction)
        if not branches:
            return []
        p = shapely.Point(x, y)
        out = []
        for b in branches:
            if b.line.distance(p) > tolerance:
                continue
            s_global = float(b.line.project(p))
            h = (hints or {}).get(b.id)
            if h is None and self.rail and self._ambiguous(b, p, s_global, tolerance):
                # no previous position to go by, and the shape passes here twice
                # (a train starting at Central on a City Circle shape could be at
                # either end of the loop): wait for a fix that isn't ambiguous
                continue
            if h is not None and abs(s_global - h) > HINT_BACK_M:
                a, z = max(0.0, h - HINT_BACK_M), min(b.length, h + HINT_AHEAD_M)
                if z - a > 1:
                    win = substring(b.line, a, z)
                    if win.distance(p) <= tolerance:
                        s_global = a + float(win.project(p))
            out.append((b.id, s_global))
        return out

    @staticmethod
    def _ambiguous(b, p, s: float, tolerance: float) -> bool:
        """Another part of the branch, well away along the line, is also within
        tolerance of the point."""
        for a, z in ((0.0, s - AMBIGUOUS_GAP_M), (s + AMBIGUOUS_GAP_M, b.length)):
            if z - a > 1 and substring(b.line, a, z).distance(p) <= tolerance:
                return True
        return False

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
        dir_trips = {d: list(dt.trip_id) for d, dt in trips.groupby("direction_id")}
        _add_stations(static, directions, station_trips, tol, dir_trips)

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
        "stations_version": STATIONS_VERSION,
        "static_source": static.source_path,
        "directions": directions,
    }


def _add_stations(static, directions, station_trips, tol, dir_trips=None):
    """Stations on each branch: every station the line serves along it.

    Starts from the trip on the branch's shape that stops most often (so a
    short working can't truncate the list), then adds any station another trip
    of the line in that direction serves right on the branch. One trip alone
    can be an express (T7 event trains), which would hide stations and make
    real expresses look like all-stations trains."""
    stop_lists = static.all_stop_lists()
    stops = static.stops()
    for (d, i), trip_ids in station_trips.items():
        b = directions[d]["branches"][i]
        line = LineString(to_metric(np.asarray(b["lonlat"])))
        best = max(trip_ids, key=lambda t: len(stop_lists.get(t, [])))
        own = [s for s in stop_lists.get(best, []) if s in stops.index]
        others = {s for t in (dir_trips or {}).get(d, ()) for s in stop_lists.get(t, ())
                  if s in stops.index} - set(own)
        found = {}
        for ids, max_off in ((own, tol * 2), (sorted(others), tol)):
            if not ids:
                continue
            st = stops.loc[ids]
            pts = shapely.points(to_metric(st[["lon", "lat"]].to_numpy(dtype=float)))
            s_along = shapely.line_locate_point(line, pts)
            off = shapely.distance(line, pts)
            for name, si, oi, lat, lon, sid in zip(st.station, s_along, off, st.lat, st.lon, st.index):
                # track-circuit location points ("... Loc") aren't stations
                if str(name).endswith(" Loc") or str(sid).endswith(" Loc"):
                    continue
                if oi <= max_off and name not in found:
                    found[name] = {"name": name, "s": round(float(si), 1),
                                   "lat": round(float(lat), 6), "lon": round(float(lon), 6)}
        b["stations"] = sorted(found.values(), key=lambda x: x["s"])


def refresh_stations(static: StaticGtfs, data: dict):
    """Recompute a frozen geometry's stations in place. Stations don't affect
    segment numbering, so the line's collected totals stay valid."""
    trips = static.trips[static.trips.line == data["line"]]
    in_service = trips[~trips.trip_headsign.str.contains(NOT_IN_SERVICE, regex=True)]
    if not in_service.empty:
        trips = in_service
    station_trips = {}
    for d, v in data["directions"].items():
        for i, b in enumerate(v["branches"]):
            b["stations"] = []
            ids = list(trips.loc[(trips.direction_id == d) & (trips.shape_id == b.get("shape_id")), "trip_id"].iloc[:25])
            if ids:
                station_trips[(d, i)] = ids
    if station_trips:
        dir_trips = {d: list(dt.trip_id) for d, dt in trips.groupby("direction_id")}
        _add_stations(static, data["directions"], station_trips, max(static.feed.tolerance_m, 50), dir_trips)
    data["stations_version"] = STATIONS_VERSION


# ---------------------------------------------------------------- persistence

def geometry_path(store: Path, feed_key: str, line: str) -> Path:
    return store / feed_key / line / "geometry.json"


def load_or_build(store: Path, static: StaticGtfs, line: str, rebuild: bool = False):
    p = geometry_path(store, static.feed.key, line)
    if p.exists() and not rebuild:
        data = json.loads(p.read_text())
        if data.get("stations_version", 1) < STATIONS_VERSION and static.feed.mode in STATION_MODES:
            refresh_stations(static, data)
            p.write_text(json.dumps(data, separators=(",", ":")))
        return LineGeometry(data)
    data = build_line(static, line)
    if data is None:
        return None
    if rebuild:  # old segment numbers no longer apply
        (p.parent / "speeds.csv").unlink(missing_ok=True)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, separators=(",", ":")))
    return LineGeometry(data)
