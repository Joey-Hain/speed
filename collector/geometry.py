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

# Trains (Sydney Trains, NSW TrainLink) use an up/down layout instead of GTFS
# direction_id: every branch runs outward from Sydney (Central), or from the
# point nearest Central for lines that don't reach it, and a vehicle is "down"
# when moving away and "up" when moving towards. Through routes (T1 Emu Plains
# to Berowra) are split at Central, short workings contained in a longer
# branch (Grafton inside Brisbane) don't become branches, and km are measured
# from Central like railway kilometrage.
UPDOWN_MODES = {"train"}
UPDOWN_ACTIVE = True
GEOMETRY_VERSION = 7   # 2: up/down layout; 3: weighted by days run; 4: BRANCH_RULES; 5: shapes cut to served stops;
                       # 6: stations from trains running the branch, exact cuts, wrong shapes ignored, hub split;
                       # 7: limited stops, SCO rules, join_at
CENTRAL_LONLAT = (151.2065, -33.8832)
CENTRAL_NEAR_M = 400   # a shape passing this close to Central is split there
MIN_NEW_TRACK_M = 1000  # a candidate branch must add this much track not already covered
MIN_BRANCH_SHARE = 0.1   # ...and be run by at least this share of the line's trips
INTERIOR_SPLIT_M = 5000  # see _pieces

# Hand corrections for routes the timetable runs regularly but which aren't
# wanted as branches (from Joey, 2026-10-14). Keyed by "feed:line".
#   exclude_via: drop candidate branches passing these stations
#   truncate:    (via, at) - a branch passing `via` ends at station `at`
#   begin_at:    branches passing this station are cut to begin there; or
#                [(via, station)] for branches passing `via`
#   join_at:     a branch ending at this station carries on into the one starting there
#   start_at:    branches start (km 0, the "up" end) at the end nearer this station
#   minor:       stations that never count as regular stops (see REGULAR_SHARE)
BRANCH_RULES = {
    "sydneytrains:T8": {"exclude_via": ["Sydenham"]},      # via Sydenham ends with the 15/11 timetable
    # Blacktown and Central trains are special workings; the shuttle's down
    # end is Olympic Park (railway convention, though it's nearer Central)
    "sydneytrains:T7": {"exclude_via": ["Blacktown", "Redfern"], "start_at": "Lidcombe"},
    "sydneytrains:T5": {"exclude_via": ["Penrith", "Doonside"]},  # nothing west of Blacktown
    "sydneytrains:T9": {"truncate": [("Strathfield", "Hornsby")]},  # via Strathfield only to Hornsby
    "sydneytrains:SHL": {"begin_at": "Campbelltown"},  # the few Central trains run via East Hills
    # Bondi Junction trains end with the 15/11 timetable; Port Kembla trains
    # start no further north than Thirroul; Kiama - Bomaderry is one line
    "sydneytrains:SCO": {"exclude_via": ["Edgecliff"], "begin_at": [("Lysaghts", "Thirroul")],
                         "join_at": ["Kiama"]},
    # minor: stations skipping which doesn't make a train an express
    "sydneytrains:T1": {"minor": ["Lidcombe", "Auburn", "Clyde", "Granville", "Harris Park"]},  # T1 runs the main lines to Parramatta
    "sydneytrains:BMT": {"minor": ["Zig Zag"]},       # request stop
    "sydneytrains:CCN": {"minor": ["Wondabyne"]},     # request stop
    "nswtrains:North_Coast_NSW": {"minor": ["Eungai"]},  # request stop
}
RULE_NEAR_M = 300
REGULAR_SHARE = 0.3    # a station where this share of passing trips stop is a regular stop;
                       # skipping one makes a trip limited stops or express
SHOW_SHARE = 0.1       # stations served by fewer passing trips aren't listed
EXPRESS_SKIPS = 8      # a train skipping this many regular stations is an express, fewer a
                       # limited stops (BRANCH_RULES express_skips overrides it per line)


def service_type(skipped: int, express_skips: int = EXPRESS_SKIPS) -> str:
    return "all" if skipped <= 0 else "lim" if skipped < express_skips else "exp"


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
        self.updown = data.get("layout") == "updown"
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


# ------------------------------------------------------------ up/down layout

def _central_xy() -> np.ndarray:
    return to_metric(np.array([CENTRAL_LONLAT], dtype=float))[0]


def _pieces(lonlat: np.ndarray, hub=None) -> list[np.ndarray]:
    """Split a shape where it passes Central (or at its point nearest Central,
    for lines that don't get within CENTRAL_NEAR_M), and orient every piece to
    start at its end nearer Central. A loop through Central (City Circle)
    becomes a piece of its own."""
    xy = to_metric(lonlat)
    dist = np.hypot(*(xy - _central_xy()).T)
    n = len(dist)
    near = dist < CENTRAL_NEAR_M
    if not near.any() and hub is not None:
        # a line that doesn't reach Central splits where its track comes
        # nearest (T5 at Harris Park), for every trip passing that point
        hd = np.hypot(*(xy - hub).T)
        if hd.min() < CENTRAL_NEAR_M:
            k = int(np.argmin(hd))  # pieces start at the hub
            if 0 < k < n - 1:
                return [p for p in (lonlat[:k + 1][::-1], lonlat[k:]) if len(p) >= 2]
            return [lonlat[::-1] if k == n - 1 else lonlat]
    cuts = []
    if near.any():
        i = 0
        while i < n:
            if near[i]:
                j = i
                while j + 1 < n and near[j + 1]:
                    j += 1
                cuts.append(i + int(np.argmin(dist[i:j + 1])))
                i = j + 1
            else:
                i += 1
    else:
        # a line that doesn't reach Central: split at its point nearest Central
        # only if that's well inside the line (T5 near Harris Park), not just
        # past an end (HUN starts at Newcastle Interchange)
        k = int(np.argmin(dist))
        if dist[k] < min(dist[0], dist[-1]) - INTERIOR_SPLIT_M:
            cuts.append(k)
    bounds = sorted({0, n - 1, *cuts})
    out = []
    for a, b in zip(bounds[:-1], bounds[1:]):
        piece = lonlat[a:b + 1]
        if len(piece) < 2:
            continue
        out.append(piece[::-1] if dist[b] < dist[a] else piece)
    return out


def _draw_flags(lines: list, segment_m: int, tol: float) -> list[str]:
    """Per branch, which segments the map should draw (not already drawn by an
    earlier branch)."""
    out, done = [], []
    for ml in lines:
        n_seg = int(np.ceil(ml.length / segment_m)) or 1
        mids = shapely.points([ml.interpolate(min((k + 0.5) * segment_m, ml.length)).coords[0]
                               for k in range(n_seg)])
        covered = np.zeros(n_seg, dtype=bool)
        for prev in done:
            covered |= shapely.distance(prev, mids) <= tol
        done.append(ml)
        out.append("".join("0" if c else "1" for c in covered))
    return out


def _branch_runs(static, trips, lines: list, tol: float):
    """For each (down-oriented) branch: the stations near it {name: (s, offset,
    lat, lon)} and the trips that run along it [(trip_id, stop names, lo, hi,
    days run)], lo..hi being the stretch between its first and last stop on the
    branch. A trip only counts on a branch if its own track follows the branch
    over that stretch: a T8 via Sydenham stops at Central and Wolli Creek but
    doesn't run the Airport line between them."""
    stop_lists = static.all_stop_lists()
    stops = static.stops()
    ids = {s for t in trips.trip_id for s in stop_lists.get(t, ())
           if s in stops.index and not str(s).endswith(" Loc")}
    if not ids:
        return [({}, []) for _ in lines]
    st = stops.loc[sorted(ids)]
    st = st[~st.station.astype(str).str.endswith(" Loc")].dropna(subset=["lat", "lon"])
    station_of = st["station"].to_dict()
    pts = shapely.points(to_metric(st[["lon", "lat"]].to_numpy(dtype=float)))
    groups = {}
    for key, (ll, members) in _trip_tracks(static, trips).items():
        if ll is None:
            continue  # unknown track: can't tell which branch it runs
        tl = LineString(to_metric(ll))
        for t, sl, w in members:
            names = {station_of[x] for x in sl if x in station_of}
            if len(names) >= 2:
                groups.setdefault(key, (tl, []))[1].append((t, names, w))
    out = []
    for ml in lines:
        s_along = shapely.line_locate_point(ml, pts)
        off = shapely.distance(ml, pts)
        best: dict[str, tuple] = {}
        for name, si, oi, lat, lon in zip(st.station, s_along, off, st.lat, st.lon):
            if oi <= tol * 2 and (name not in best or oi < best[name][1]):
                best[name] = (float(si), float(oi), float(lat), float(lon))
        pos = {n: v[0] for n, v in best.items()}
        runs = []
        for tl, members in groups.values():
            for t, names, w in members:
                here = [pos[n] for n in names if n in pos]
                if len(here) < 2:
                    continue
                lo, hi = min(here), max(here)
                k = max(2, min(40, int((hi - lo) // 250)))
                probe = shapely.points([ml.interpolate(lo + (hi - lo) * i / (k - 1)).coords[0]
                                        for i in range(k)])
                if (shapely.distance(tl, probe) <= tol).mean() < 0.9:
                    continue  # runs a different way between these stops
                runs.append((t, names, lo, hi, w))
        out.append((best, runs))
    return out


def _updown_stations(static, trips, lines: list, tol: float, minor=()) -> list[list[dict]]:
    """Stations along each (down-oriented) branch with the share of the trips
    running past them that stop there. 'regular' stations are the ones a trip
    has to stop at all of to count as all-stations; `minor` stations (request
    stops, stations the line's trains skip whatever their pattern) never are."""
    out = []
    for best, runs in _branch_runs(static, trips, lines, tol):
        passing, stopping = Counter(), Counter()
        for t, names, lo, hi, w in runs:
            for n, v in best.items():
                if lo <= v[0] <= hi:
                    passing[n] += w
                    if n in names:
                        stopping[n] += w
        stations = []
        for n, (si, oi, lat, lon) in best.items():
            share = stopping[n] / passing[n] if passing[n] else 0.0
            if share >= SHOW_SHARE:
                stations.append({"name": n, "s": round(si, 1), "lat": round(lat, 6), "lon": round(lon, 6),
                                 "share": round(share, 3),
                                 "regular": share >= REGULAR_SHARE and n not in minor})
        out.append(sorted(stations, key=lambda x: x["s"]))
    return out


def trip_skips(static, line: str, data: dict) -> dict:
    """{trip_id: regular stations it skips}, judged on the branch that sees
    most of its run among the branches its track actually follows, so a
    trip's rating is the same wherever it is (an express is an express on the
    City Circle too)."""
    branches = data["directions"]["down"]["branches"]
    trips = static.trips[(static.trips.line == line) & (static.trips.shape_id != "")]
    in_service = trips[~trips.trip_headsign.str.contains(NOT_IN_SERVICE, regex=True)]
    if not in_service.empty:
        trips = in_service
    lines = [LineString(to_metric(np.asarray(b["lonlat"]))) for b in branches]
    best = {}
    for b, (_, runs) in zip(branches, _branch_runs(static, trips, lines, max(static.feed.tolerance_m, 50))):
        names = [x["name"] for x in b["stations"] if x.get("regular", x.get("share", 1.0) >= REGULAR_SHARE)]
        for t, served, lo, hi, w in runs:
            hit = [k for k, n in enumerate(names) if n in served]
            if len(hit) >= 2:
                span = names[hit[0]:hit[-1] + 1]
                best[t] = min(best.get(t, (0, 0)), (-len(span), sum(n not in served for n in span)))
    return {t: v[1] for t, v in best.items()}


def _trim_to_stops(lonlat: np.ndarray, stops, first, last, near_m: float = 400, pad_m: float = 200):
    """The part of a shape from its trip's first stop to its last (plus a little).
    The first stop is matched to the earliest pass of the shape near it and the
    last to the latest, so loops through the same station trim correctly."""
    if first is None or last is None or first not in stops.index or last not in stops.index:
        return lonlat
    xy = to_metric(lonlat)
    cum = np.concatenate([[0], np.cumsum(np.hypot(*np.diff(xy, axis=0).T))])
    def pass_s(stop, earliest):
        r = stops.loc[stop]
        if getattr(r, "ndim", 1) > 1:  # duplicate stop ids: take the first
            r = r.iloc[0]
        if not np.isfinite(float(r["lon"])) or not np.isfinite(float(r["lat"])):
            return None
        sx, sy = to_metric(np.array([[float(r["lon"]), float(r["lat"])]]))[0]
        d = np.hypot(xy[:, 0] - sx, xy[:, 1] - sy)
        idx = np.where(d <= near_m)[0]
        if not len(idx):
            return None
        return cum[idx[0] if earliest else idx[-1]]
    try:
        a, b = pass_s(first, True), pass_s(last, False)
    except (KeyError, TypeError, ValueError):
        return lonlat
    if a is None or b is None or b - a < MIN_NEW_TRACK_M:
        return lonlat
    return _cut(lonlat, cum, a - pad_m, b + pad_m)


def _cut(lonlat: np.ndarray, cum: np.ndarray, s0: float, s1: float) -> np.ndarray:
    """The part of a polyline between distances s0 and s1 along it, with the
    ends interpolated (shape points can be kilometres apart on straight track,
    so cutting at a vertex would overshoot)."""
    s0, s1 = max(0.0, s0), min(float(cum[-1]), s1)
    if s1 - s0 <= 0:
        return lonlat
    def at(s):
        k = min(max(int(np.searchsorted(cum, s)), 1), len(cum) - 1)
        seg = cum[k] - cum[k - 1]
        f = 0.0 if seg <= 0 else (s - cum[k - 1]) / seg
        return lonlat[k - 1] + f * (lonlat[k] - lonlat[k - 1])
    inner = lonlat[(cum > s0) & (cum < s1)]
    return np.vstack([at(s0), inner, at(s1)])


def _station_points(static, name: str):
    st = static.stops()
    hit = st[st.station == name].dropna(subset=["lat", "lon"])
    if hit.empty:
        return None
    return shapely.points(to_metric(hit[["lon", "lat"]].to_numpy(dtype=float)))


def _apply_rules(static, cands, rules):
    out = []
    excl = [p for p in (_station_points(static, n) for n in rules.get("exclude_via", [])) if p is not None]
    cuts = [(_station_points(static, v), _station_points(static, a)) for v, a in rules.get("truncate", [])]
    # begin_at: "station", or [(via, station)] for branches passing `via` only
    b = rules.get("begin_at", [])
    begins = [("any", b)] if isinstance(b, str) else [(_station_points(static, v), a) for v, a in b]
    for c, length, sid, ll, ml in cands:
        if any(shapely.distance(ml, pts).min() <= RULE_NEAR_M for pts in excl):
            continue
        for via, at in cuts:
            if via is None or at is None or shapely.distance(ml, via).min() > RULE_NEAR_M:
                continue
            if shapely.distance(ml, at).min() <= RULE_NEAR_M:
                end = float(shapely.line_locate_point(ml, at).max()) + 200
                if end < ml.length:
                    xy = to_metric(ll)
                    cum = np.concatenate([[0], np.cumsum(np.hypot(*np.diff(xy, axis=0).T))])
                    ll = _cut(ll, cum, 0, end)
                    ml = LineString(to_metric(ll))
                    length = ml.length
        for via, name in begins:
            at = _station_points(static, name)
            if not isinstance(via, str) and (via is None or shapely.distance(ml, via).min() > RULE_NEAR_M):
                continue
            if at is not None and shapely.distance(ml, at).min() <= RULE_NEAR_M:
                begin = float(shapely.line_locate_point(ml, at).min()) - 200
                if begin > 0:
                    xy = to_metric(ll)
                    cum = np.concatenate([[0], np.cumsum(np.hypot(*np.diff(xy, axis=0).T))])
                    ll = _cut(ll, cum, begin, cum[-1])
                    ml = LineString(to_metric(ll))
                    length = ml.length
        if rules.get("start_at"):
            at = _station_points(static, rules["start_at"])
            if at is not None:
                a0, a1 = shapely.Point(ml.coords[0]), shapely.Point(ml.coords[-1])
                if shapely.distance(a1, at).min() < shapely.distance(a0, at).min():
                    ll = ll[::-1]
                    ml = LineString(to_metric(ll))
        if length >= MIN_NEW_TRACK_M:
            out.append((c, length, sid, ll, ml))
    return out


def _join(static, chosen, trips_on, stations):
    """Run a branch on through a branch that starts where it ends (the Kiama -
    Bomaderry shuttle carries on from the South Coast line)."""
    for name in stations:
        at = _station_points(static, name)
        if at is None:
            continue
        ends = [i for i, ch in enumerate(chosen)
                if shapely.distance(shapely.Point(ch[4].coords[-1]), at).min() <= RULE_NEAR_M + 200]
        starts = [i for i, ch in enumerate(chosen)
                  if shapely.distance(shapely.Point(ch[4].coords[0]), at).min() <= RULE_NEAR_M + 200]
        if not ends or not starts or set(ends) & set(starts):
            continue
        a, b = ends[0], starts[0]
        ll = np.vstack([chosen[a][3], chosen[b][3][1:]])
        ml = LineString(to_metric(ll))
        chosen[a] = (chosen[a][0], ml.length, chosen[a][2], ll, ml)
        trips_on[a] = max(trips_on[a], trips_on[b])
        chosen = [ch for i, ch in enumerate(chosen) if i != b]
        trips_on = [t for i, t in enumerate(trips_on) if i != b]
    return chosen, trips_on


BAD_SHAPE_M = 1000  # a trip whose stops are further than this from its shape has the wrong shape


def _trip_tracks(static, trips, weights=None) -> dict:
    """Trips grouped by the track they run: (shape_id, first stop, last stop)
    -> (the shape cut to those stops as lon/lat, or None if there's no shape or
    it's the wrong one, [(trip_id, passenger stop ids, weight)]). Some trips
    carry another route's shape (CCN via Strathfield on the via-Gordon shape);
    their track is unknown rather than wrong."""
    stop_lists = static.all_stop_lists()
    stops = static.stops()
    shapes = static.shapes()
    if weights is None:
        days = static.trip_days()
        weights = [days.get(t, 0) for t in trips.trip_id]
        if not any(weights):
            weights = [1] * len(trips)
    groups: dict[tuple, list] = {}
    for t, sid, w in zip(trips.trip_id, trips.shape_id, weights):
        if w <= 0:
            continue
        sl = [x for x in stop_lists.get(t, ()) if x in stops.index and not str(x).endswith(" Loc")]
        if len(sl) < 2:
            continue
        groups.setdefault((sid, sl[0], sl[-1]), []).append((t, sl, w))
    out = {}
    for (sid, first, last), members in groups.items():
        ll = None
        if sid in shapes and len(shapes[sid]) >= 2:
            ll = _trim_to_stops(np.asarray(shapes[sid], dtype=float), stops, first, last)
            ids = sorted({x for _, sl, _ in members for x in sl})
            st = stops.loc[ids].dropna(subset=["lat", "lon"])
            if len(st):
                pts = shapely.points(to_metric(st[["lon", "lat"]].to_numpy(dtype=float)))
                full = LineString(to_metric(np.asarray(shapes[sid], dtype=float)))
                if shapely.distance(full, pts).max() > BAD_SHAPE_M:
                    ll = None
        out[(sid, first, last)] = (ll, members)
    return out


def _hub(lonlats: list):
    """Where a line that doesn't reach Central splits into up and down: the
    point of its track nearest Central. None for lines through Central."""
    best = None
    c = _central_xy()
    for ll in lonlats:
        xy = to_metric(ll)
        d = np.hypot(*(xy - c).T)
        k = int(np.argmin(d))
        if d[k] < CENTRAL_NEAR_M:
            return None
        if best is None or d[k] < best[0]:
            best = (d[k], xy[k])
    return None if best is None else best[1]


def build_updown(static: StaticGtfs, line: str) -> dict | None:
    feed = static.feed
    trips = static.trips[(static.trips.line == line) & (static.trips.shape_id != "")]
    in_service = trips[~trips.trip_headsign.str.contains(NOT_IN_SERVICE, regex=True)]
    if not in_service.empty:
        trips = in_service
    if trips.empty:
        return None
    shapes = static.shapes()
    meta = static.line_meta(line)
    tol = max(feed.tolerance_m, 50)
    # weight trips by how many days they run in the coming four weeks, so
    # special workings (event trains, trackwork patterns) can't make branches
    days = static.trip_days()
    weights = [days.get(t, 0) for t in trips.trip_id]
    if sum(weights) == 0:  # no usable calendar: count every trip once
        weights = [1] * len(trips)
    # a trip's shape can run well past its first and last stop (every T7
    # shuttle uses a shape that carries on to Blacktown), so cut each shape
    # to the stretch its trips actually serve
    tracks = _trip_tracks(static, trips, weights)
    counts = Counter()
    for (sid, first, last), (ll, members) in tracks.items():
        if ll is not None:
            counts[sid] += sum(w for t, sl, w in members)
    counts = Counter({k: v for k, v in counts.items() if v > 0})
    total = sum(counts.values())
    hub = _hub([ll for ll, m in tracks.values() if ll is not None])
    cands = []  # (trip-days, length, shape_id, lonlat, metric line)
    for (sid, first, last), (lonlat, members) in tracks.items():
        if lonlat is None:
            continue
        c = sum(w for t, sl, w in members)
        for piece in _pieces(lonlat, hub):
            ll = np.asarray(LineString(piece).simplify(0.00002).coords)
            ml = LineString(to_metric(ll))
            if ml.length >= MIN_NEW_TRACK_M:
                cands.append((c, ml.length, sid, ll, ml))
    rules = BRANCH_RULES.get(f"{feed.key}:{line}", {})
    if rules:
        cands = _apply_rules(static, cands, rules)
    if not cands:
        return None
    # how many trips run (most of) each piece, whatever their exact shape
    shape_lines = {sid: LineString(to_metric(np.asarray(shapes[sid], dtype=float)))
                   for sid in counts if sid in shapes and len(shapes[sid]) >= 2}
    runs = []
    for c, length, sid, ll, ml in cands:
        pts = shapely.points([ml.interpolate(i / 19, normalized=True).coords[0] for i in range(20)])
        runs.append(sum(counts[s2] for s2, other in shape_lines.items()
                        if (shapely.distance(other, pts) <= tol).mean() >= 0.9))
    cands = [(r, length, sid, ll, ml) for r, (c, length, sid, ll, ml) in zip(runs, cands)]
    # pieces run by a fair share of trips, longest first, so a short working
    # is always met after the branch that contains it
    busy = [x for x in cands if x[0] >= max(2, MIN_BRANCH_SHARE * total)] or cands
    busy.sort(key=lambda x: -x[1])
    chosen = []
    for c, length, sid, ll, ml in busy:
        if chosen:
            k = max(int(length // 100), 2)
            pts = shapely.points([ml.interpolate(i / (k - 1), normalized=True).coords[0] for i in range(k)])
            near = np.zeros(k, dtype=bool)
            for ch in chosen:
                near |= shapely.distance(ch[4], pts) <= tol
            if (~near).mean() * length < MIN_NEW_TRACK_M:
                continue
        chosen.append((c, length, sid, ll, ml))
        if len(chosen) >= MAX_BRANCHES:
            break
    # busiest first, so the default branch is the one most trains use
    trips_on = []
    for c, length, sid, ll, ml in chosen:
        k = max(int(length // 250), 2)
        pts = shapely.points([ml.interpolate(i / (k - 1), normalized=True).coords[0] for i in range(k)])
        n = 0
        for sid2, c2 in counts.items():
            if sid2 in shapes and len(shapes[sid2]) >= 2:
                other = LineString(to_metric(np.asarray(shapes[sid2], dtype=float)))
                if (shapely.distance(other, pts) <= tol).mean() > 0.5:
                    n += c2
        trips_on.append(n)
    chosen, trips_on = _join(static, chosen, trips_on, rules.get("join_at", ()))
    order = sorted(range(len(chosen)), key=lambda i: -trips_on[i])
    chosen = [chosen[i] for i in order]
    trips_on = [trips_on[i] for i in order]

    down_lines = [ch[4] for ch in chosen]
    up_ll = [ch[3][::-1] for ch in chosen]
    up_lines = [LineString(to_metric(ll)) for ll in up_ll]
    stations = (_updown_stations(static, trips, down_lines, tol, rules.get("minor", ()))
                if feed.mode in STATION_MODES else [[] for _ in chosen])
    directions = {}
    for d, lls, lines in (("up", up_ll, up_lines), ("down", [ch[3] for ch in chosen], down_lines)):
        draws = _draw_flags(lines, feed.segment_m, tol)
        branches = []
        for i, (ll, ml) in enumerate(zip(lls, lines)):
            L = ml.length
            sts = stations[i] if d == "down" else sorted(
                ({**x, "s": round(L - x["s"], 1)} for x in stations[i]), key=lambda x: x["s"])
            branches.append({
                "id": i,
                "shape_id": chosen[i][2],
                "trips": int(trips_on[i]),
                "length_m": round(L, 1),
                "lonlat": [[round(float(a), 6), round(float(b), 6)] for a, b in ll],
                "draw": draws[i],
                "stations": sts,
            })
        directions[d] = {"headsign": "Up" if d == "up" else "Down", "branches": branches}

    return {
        "feed": feed.key,
        "line": line,
        "mode": feed.mode,
        "layout": "updown",
        "geometry_version": GEOMETRY_VERSION,
        "name": meta["name"],
        "colour": meta["colour"],
        "segment_m": feed.segment_m,
        "hist_kmh": feed.hist_width,
        "built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "stations_version": STATIONS_VERSION,
        "static_source": static.source_path,
        "directions": directions,
    }


def refresh_stations(static: StaticGtfs, data: dict):
    """Recompute a frozen geometry's stations in place. Stations don't affect
    segment numbering, so the line's collected totals stay valid."""
    if data.get("layout") == "updown":
        trips = static.trips[static.trips.line == data["line"]]
        lines = [LineString(to_metric(np.asarray(b["lonlat"]))) for b in data["directions"]["down"]["branches"]]
        trips = trips[trips.shape_id != ""]
        in_service = trips[~trips.trip_headsign.str.contains(NOT_IN_SERVICE, regex=True)]
        if not in_service.empty:
            trips = in_service
        minor = BRANCH_RULES.get(f"{static.feed.key}:{data['line']}", {}).get("minor", ())
        sts = _updown_stations(static, trips, lines, max(static.feed.tolerance_m, 50), minor)
        for i, b in enumerate(data["directions"]["down"]["branches"]):
            b["stations"] = sts[i]
        for i, b in enumerate(data["directions"]["up"]["branches"]):
            L = b["length_m"]
            b["stations"] = sorted(({**x, "s": round(L - x["s"], 1)} for x in sts[i]), key=lambda x: x["s"])
        data["stations_version"] = STATIONS_VERSION
        return
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
        if UPDOWN_ACTIVE and static.feed.mode in UPDOWN_MODES and data.get("geometry_version", 1) < GEOMETRY_VERSION:
            rebuild = True  # moving to the up/down layout: segment numbers change
    if p.exists() and not rebuild:
        if data.get("stations_version", 1) < STATIONS_VERSION and static.feed.mode in STATION_MODES:
            refresh_stations(static, data)
            p.write_text(json.dumps(data, separators=(",", ":")))
        return LineGeometry(data)
    data = (build_updown if UPDOWN_ACTIVE and static.feed.mode in UPDOWN_MODES else build_line)(static, line)
    if data is None:
        return None
    if rebuild:  # old segment numbers no longer apply
        (p.parent / "speeds.csv").unlink(missing_ok=True)
        (p.parent / "positions.csv").unlink(missing_ok=True)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, separators=(",", ":")))
    return LineGeometry(data)
