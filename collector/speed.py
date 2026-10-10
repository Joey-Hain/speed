"""
Turning vehicle fixes into speed samples on line segments.

Two sources:

reported   GPS feeds send position.speed (m/s). Each fix is one sample, added to
           the segment the vehicle is in.

estimated  Sydney Trains and the L1 report where a train is from the signalling
           system: the position jumps from one track section to the next
           instead of moving smoothly. Speed is measured over running spans:

             * a span runs from one observed position to the first position at
               least MIN_SPAN_S later; its speed is distance / time and it is
               added to every segment it covers. Longer spans keep the error
               from section-sized position steps small enough for percentiles.
             * a span ends where the train stops (standing longer than DWELL_S),
               timed to when it arrived, so dwell time never sits inside one
             * after a stop, running speed restarts from the next position seen,
               because where in its stopping section the train stood, and when
               exactly it left, aren't known. That departure hop only counts in
               the with-stops totals, timed from arrival.

           The same method works on GPS fixes without a speed field, so "auto"
           feeds fall back to it per fix.

Samples are accumulated by (direction, branch, segment, day type, time band).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Australia/Sydney")

DWELL_S = 40        # stationary longer than this counts as a stop
MAX_GAP_S = 600     # ignore blocks that took longer than this
MOVE_EPS_M = 20     # smaller jumps are treated as standing still
MIN_SPAN_S = 45     # shortest running span a speed is measured over
A_MAX = 1.5         # m/s^2: no train, tram or bus pulls away faster than this
DEPART_SLACK = 1.25 # allowance for position/timing error on the departure hop
DEPART_MAX_M = 2000 # the first position after leaving a stop is within a poll or a track
                    # circuit of it; a longer hop means the stop was placed wrongly
MAX_KMH = {"train": 170, "metro": 120, "lightrail": 90, "bus": 110, "ferry": 80}

BANDS = [  # (name, start hour inclusive, end hour exclusive), local time
    ("night", 0, 6), ("am", 6, 10), ("mid", 10, 15), ("pm", 15, 19), ("eve", 19, 24),
]


def band_of(ts: int) -> tuple[str, str, str]:
    """(week, day type, time band) in Sydney time; week is the Monday's date."""
    t = datetime.fromtimestamp(ts, TZ)
    week = (t.date() - timedelta(days=t.weekday())).isoformat()
    day = "we" if t.weekday() >= 5 else "wd"
    for name, a, b in BANDS:
        if a <= t.hour < b:
            return week, day, name
    return week, day, "night"




class Cell:
    """Totals for one segment/day/band. 'run' leaves stopped time out, 'all' keeps it."""
    __slots__ = ("n", "sum", "sumsq", "hist", "n_all", "sum_all", "sumsq_all", "hist_all")

    def __init__(self):
        self.n = self.n_all = 0
        self.sum = self.sumsq = self.sum_all = self.sumsq_all = 0.0
        self.hist: dict[int, int] = {}
        self.hist_all: dict[int, int] = {}

    def add(self, kmh: float, run: bool, width: int):
        b = int(kmh // width)
        if run:
            self.n += 1
            self.sum += kmh
            self.sumsq += kmh * kmh
            self.hist[b] = self.hist.get(b, 0) + 1
        else:
            self.n_all += 1
            self.sum_all += kmh
            self.sumsq_all += kmh * kmh
            self.hist_all[b] = self.hist_all.get(b, 0) + 1


class Accumulator:
    """(feed, line) -> {(dir, branch, seg, week, day, band, svc): Cell}"""

    def __init__(self):
        self.data: dict[tuple[str, str], dict[tuple, Cell]] = {}
        self.positions: dict[tuple[str, str], dict[tuple, list[int]]] = {}
        self.samples = 0

    def add_position(self, feed: str, line: str, d: str, branch: int, seg: int, tc: bool):
        """Count a fix on a segment, and whether its position came from track circuits."""
        c = self.positions.setdefault((feed, line), {}).setdefault((d, branch, seg), [0, 0])
        c[0] += 1
        c[1] += int(tc)

    def add(self, feed: str, line: str, d: str, branch: int, seg: int, ts: int,
            kmh_run: float | None, kmh_all: float | None, width: int = 5, svc: str = "all"):
        """kmh_run: running speed (None if stopped); kmh_all: speed with stops included.
        width: histogram bin width in km/h (from the line's geometry)."""
        week, day, band = band_of(ts)
        cell = self.data.setdefault((feed, line), {}).setdefault(
            (d, branch, seg, week, day, band, svc), Cell())
        if kmh_run is not None:
            cell.add(kmh_run, True, width)
            self.samples += 1
        if kmh_all is not None:
            cell.add(kmh_all, False, width)


@dataclass
class VehicleState:
    trip: str
    direction: str
    anchor: dict[int, float] | None  # where the current running span started
    t_anchor: int | None              # when it left there (None: still standing)
    t_anchor_all: int                 # when it arrived there (dwell included)
    pos: dict[int, float]             # current position (branch -> s)
    t_first: int                      # first seen at current position
    t_seen: int                       # last seen at current position


class SpeedEstimator:
    """Speed over running spans of at least MIN_SPAN_S.

    Single 15 s hops are too coarse when positions come in track-section steps:
    a train at 100 km/h shows 250 m or 500 m hops, i.e. 60 or 120 km/h. That
    averages out in a mean but wrecks percentiles. Measuring over longer spans
    shrinks the error, and a span always ends where the train stops, so station
    dwells never sit inside one.
    """

    def __init__(self, min_span: float = MIN_SPAN_S):
        self.vehicles: dict[str, VehicleState] = {}
        self.min_span = min_span

    def update(self, vid: str, trip: str, direction: str, pos: dict[int, float],
               ts: int, obs: int):
        """Feed one fix (ts = vehicle timestamp, obs = feed header time).

        Returns [(branch, s_from, s_to, mps_running, mps_with_stops)] for spans
        completed. Either speed can be None: running speed for the hop out of a
        stop, with-stops speed after a very long stand such as a layover.
        """
        st = self.vehicles.get(vid)
        if st is None or st.trip != trip or st.direction != direction:
            t = max(ts, obs)
            self.vehicles[vid] = VehicleState(trip, direction, dict(pos), None, ts, dict(pos), ts, t)
            return []
        common = [b for b in pos if b in st.pos]
        if not common:
            t = max(ts, obs)
            self.vehicles[vid] = VehicleState(trip, direction, dict(pos), None, ts, dict(pos), ts, t)
            return []
        main = min(common)
        ds = pos[main] - st.pos[main]
        out = []

        if abs(ds) < MOVE_EPS_M:  # standing (or no new position)
            st.t_seen = max(st.t_seen, obs, ts)
            if st.t_seen - st.t_first >= DWELL_S and st.t_anchor is not None:
                # it has stopped: close the running span at the arrival time
                out = self._span(st, st.pos, st.t_first)
                st.anchor, st.t_anchor, st.t_anchor_all = dict(st.pos), None, st.t_first
            return out
        if ts <= st.t_first or ds < 0:
            if ds < 0:  # went backwards: position glitch, start again here
                t = max(ts, obs)
                self.vehicles[vid] = VehicleState(trip, direction, dict(pos), None, ts, dict(pos), ts, t)
            return []

        if st.t_anchor is None:
            # departing after a stop. Where in its stopping block the train stood,
            # and exactly when it left, aren't known (a block feed only says which
            # section it's in; a poll only says it was still there), so running
            # speed restarts from this new position. The departure hop only goes
            # into the with-stops totals, timed from arrival.
            dt_all = ts - min(st.t_anchor_all, st.t_first)
            if st.anchor is not None and 0 < dt_all <= MAX_GAP_S:
                for b in pos:
                    d = pos[b] - st.pos[b] if b in st.pos else 0
                    if d < MOVE_EPS_M or d > DEPART_MAX_M:
                        continue
                    # starting from rest, the average over d can't exceed
                    # sqrt(A_MAX * d / 2); faster means the start position was wrong
                    if d / dt_all > DEPART_SLACK * (A_MAX * d / 2) ** 0.5:
                        continue
                    out.append((b, st.pos[b], pos[b], None, d / dt_all))
            st.anchor, st.t_anchor, st.t_anchor_all = dict(pos), ts, ts
            st.pos, st.t_first, st.t_seen = dict(pos), ts, max(ts, obs)
            return out
        st.pos, st.t_first, st.t_seen = dict(pos), ts, max(ts, obs)
        if ts - st.t_anchor >= self.min_span:
            out = self._span(st, pos, ts)
            st.anchor, st.t_anchor, st.t_anchor_all = dict(pos), ts, ts
        return out

    @staticmethod
    def _span(st: VehicleState, end: dict[int, float], t_end: int):
        dt = t_end - st.t_anchor
        dt_all = t_end - st.t_anchor_all
        out = []
        if st.anchor is None or not 0 < dt <= MAX_GAP_S:
            return out
        for b in end:
            if b not in st.anchor:
                continue
            a, z = st.anchor[b], end[b]
            if z - a >= MOVE_EPS_M:
                out.append((b, a, z, (z - a) / dt,
                            (z - a) / dt_all if 0 < dt_all <= MAX_GAP_S else None))
        return out

    def forget_older_than(self, now: int):
        for vid in [v for v, s in self.vehicles.items() if now - s.t_seen > MAX_GAP_S]:
            del self.vehicles[vid]
