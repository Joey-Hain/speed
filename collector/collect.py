"""
Poll every TfNSW vehicle position feed for a while, then merge the speed
samples into the store.

  python -m collector.collect --minutes 50 --store store

On GitHub Actions this runs hourly for ~50 minutes, so polling is close to
continuous even when a scheduled start is late or skipped.
"""

from __future__ import annotations

import argparse
import heapq
import signal
import time
import traceback
from collections import Counter, deque
from pathlib import Path

from google.transit import gtfs_realtime_pb2
from pyproj import Transformer

from . import geometry as geo
from .api import Api, ApiError, QuotaExceeded
from .feeds import BY_KEY, FEEDS, Feed
from .gtfs_static import load_static
from .speed import MAX_KMH, MOVE_EPS_M, Accumulator, SpeedEstimator
from .store import (merge_line, merge_positions, prune_expired, prune_obsolete,
                    train_network, update_index, write_status)

_TO_METRIC = Transformer.from_crs("EPSG:4326", geo.METRIC_CRS, always_xy=True)
REFRESH_STATIC = False  # --refresh-static: re-download static GTFS this run
MIN_REPORTED_KMH = 1.0  # below this a fix counts as stopped (only in the with-stops totals)


class FeedRunner:
    def __init__(self, feed: Feed, api: Api, store: Path, cache: Path,
                 acc: Accumulator, rebuild: set[str]):
        self.feed, self.api, self.store, self.cache = feed, api, store, cache
        self.acc, self.rebuild = acc, rebuild
        self.static = None
        self.geoms: dict[str, geo.LineGeometry | None] = {}
        self.est = SpeedEstimator()
        self.last_ts: dict[str, int] = {}
        self.dir_guess: dict[str, tuple[float, str]] = {}
        self.speed_mode = feed.speed
        self.disabled = False
        self.fail_streak = 0
        self.unmatched_routes: Counter = Counter()
        self.nt_examples: list[dict] = []
        # positioning diagnostics: GPS fixes rarely land on exactly the same
        # coordinates as another vehicle's; track-circuit positions do
        self.coord_owner: dict[tuple, set] = {}
        self.prev_coord: dict[str, tuple] = {}
        self.speed_samples: list[float] = []
        self.speed_ok: set[str] = set()
        self.gps_runs: dict[str, int] = {}
        self.excluded: dict[str, bool] = {}
        self.raw_samples: list[str] = []
        self.stats = Counter()
        self.errors: deque[str] = deque(maxlen=20)

    # ------------------------------------------------------------------ setup
    def ensure_static(self) -> bool:
        if self.static is None and not self.disabled:
            try:
                self.static = load_static(self.feed, self.api, self.cache,
                                          max_age_days=0 if REFRESH_STATIC else 3)
            except ApiError as e:
                self.disabled = True
                self.errors.append(f"static: {e}")
        return self.static is not None

    def geometry(self, line: str):
        if line not in self.geoms:
            try:
                self.geoms[line] = geo.load_or_build(
                    self.store, self.static, line,
                    rebuild=f"{self.feed.key}:{line}" in self.rebuild)
            except Exception as e:  # a bad shape shouldn't stop the feed
                self.geoms[line] = None
                self.errors.append(f"geometry {line}: {e}")
        return self.geoms[line]

    # ------------------------------------------------------------------- poll
    def poll(self):
        if not self.ensure_static():
            return
        raw = self.api.get(self.feed.rt)
        msg = gtfs_realtime_pb2.FeedMessage()
        msg.ParseFromString(raw)
        self.stats["polls_ok"] += 1
        self.process(msg)

    def process(self, msg):
        f, st = self.feed, self.static
        header_ts = int(msg.header.timestamp or time.time())
        for ent in msg.entity:
            if not ent.HasField("vehicle"):
                continue
            v = ent.vehicle
            if not v.HasField("position"):
                continue
            self.stats["fixes"] += 1
            tid, rid = v.trip.trip_id, v.trip.route_id
            if not rid and tid in st.trip_info:
                rid = st.trip_info[tid][0]
            line = st.route_line.get(rid)
            if line is None and tid in st.trip_info:
                line = st.route_line.get(st.trip_info[tid][0])
            if not line:
                self.stats["unmatched"] += 1
                if (rid or tid) and not tid.startswith("NonTimetabled"):
                    self.unmatched_routes[rid or f"trip:{tid}"] += 1
                elif tid.startswith("NonTimetabled"):
                    self.stats["non_timetabled"] += 1
                    if len(self.nt_examples) < 6:
                        self.nt_examples.append({
                            "trip": tid, "route": rid, "label": v.vehicle.label,
                            "id": v.vehicle.id, "start": v.trip.start_time,
                            "sched": v.trip.schedule_relationship})
                continue
            # the same train in both feeds shares its run number, the first part of
            # the trip_id (KN49.1414... in Sydney Trains, KN49.101026... in
            # TrainLink); TrainLink's GPS copy wins over the track-circuit one
            run_no = tid.split(".", 1)[0]
            if f.key == "nswtrains" and run_no:
                self.gps_runs[run_no] = header_ts
            elif f.key == "sydneytrains" and run_no and header_ts - self.gps_runs.get(run_no, -10**9) <= 900:
                self.stats["skipped_gps_duplicate"] += 1
                continue
            if self.is_excluded(line):
                self.stats["skipped_duplicate_regional"] += 1
                continue
            g = self.geometry(line)
            if g is None:
                self.stats["no_geometry"] += 1
                continue

            ts = int(v.timestamp or header_ts)
            vid = v.vehicle.id or v.vehicle.label or tid or ent.id
            repeat = self.last_ts.get(vid) == ts  # unchanged fix repeated by the feed
            self.last_ts[vid] = ts
            x, y = _TO_METRIC.transform(v.position.longitude, v.position.latitude)

            if v.trip.HasField("direction_id"):
                d = str(v.trip.direction_id)
            elif tid in st.trip_info:
                d = st.trip_info[tid][1]
            else:
                d = self.infer_direction(vid, g, x, y)
                if d is None:
                    continue

            pos = dict(g.project(d, x, y, f.tolerance_m))
            if not pos:
                self.stats["off_line"] += 1
                continue

            has_speed = v.position.HasField("speed")
            key = (round(v.position.latitude, 6), round(v.position.longitude, 6))
            owners = self.coord_owner.setdefault(key, set())
            shared = bool(owners - {vid})
            if len(owners) < 8:
                owners.add(vid)
            still = self.prev_coord.get(vid) == key and not repeat
            self.prev_coord[vid] = key
            self.stats["fixes_on_line"] += 1
            if shared:
                self.stats["fixes_shared_position"] += 1
            if has_speed:
                kmh_r = v.position.speed * 3.6
                if len(self.speed_samples) < 20000 and not repeat:
                    self.speed_samples.append(kmh_r)
                if still:
                    self.stats["still_with_speed_field"] += 1
                    if kmh_r > 5:
                        self.stats["still_but_speed_over_5"] += 1
            if len(self.raw_samples) < 3 and not repeat:
                from google.protobuf import text_format
                self.raw_samples.append(text_format.MessageToString(ent, as_one_line=True)[:1500])
            # Sydney Trains puts the track-circuit location in stop_id
            # ("Ashfield.Burwood 5 Loc"); those positions aren't GPS
            tc = v.stop_id.endswith(" Loc")
            if tc:
                self.stats["fixes_track_circuit"] += 1
            for b, sb in pos.items():
                seg = min(int(sb // f.segment_m), g.n_seg(d, b) - 1)
                self.acc.add_position(f.key, line, d, b, seg, tc)
            if has_speed:
                self.stats["with_speed"] += 1
                if v.position.speed > 0:
                    self.stats["speed_nonzero"] += 1
            # per vehicle: trust its speed field once it has shown a real (non-zero)
            # value; a vehicle that only ever sends 0 is estimated from movement
            if has_speed and v.position.speed > 0.5:
                self.speed_ok.add(vid)
            use_reported = self.speed_mode == "reported" or (
                self.speed_mode == "auto" and has_speed and vid in self.speed_ok)

            vmax = MAX_KMH.get(f.mode, 150)
            if use_reported:
                if repeat:
                    continue
                kmh = max(0.0, v.position.speed * f.speed_scale * 3.6)
                if kmh <= vmax:
                    moving = kmh if kmh >= MIN_REPORTED_KMH else None
                    for b, s in pos.items():
                        seg = min(int(s // f.segment_m), g.n_seg(d, b) - 1)
                        self.acc.add(f.key, line, d, b, seg, ts, moving, kmh, g.hist_kmh)
                    self.stats["samples_reported"] += 1
            else:
                for b, a, z, mps, mps_all in self.est.update(vid, tid, d, pos, ts, header_ts):
                    kmh = mps * 3.6 if mps is not None else None
                    if (kmh or 0) > vmax:
                        self.stats["rejected_fast"] += 1
                        continue
                    kmh_all = mps_all * 3.6 if mps_all is not None else None
                    last = g.n_seg(d, b) - 1
                    for seg in range(int(a // f.segment_m), min(int(z // f.segment_m), last) + 1):
                        self.acc.add(f.key, line, d, b, seg, ts, kmh, kmh_all, g.hist_kmh)
                    self.stats["samples_estimated"] += 1

        self.check_auto()
        self.est.forget_older_than(header_ts)

    def infer_direction(self, vid, g, x, y):
        dirs = list(g.directions)
        if len(dirs) == 1:
            return dirs[0]
        pos = g.project(dirs[0], x, y, self.feed.tolerance_m)
        if not pos:
            return None
        s = pos[0][1]
        prev = self.dir_guess.get(vid)
        guess = prev[1] if prev else None
        if prev and abs(s - prev[0]) >= MOVE_EPS_M:
            guess = dirs[0] if s > prev[0] else dirs[1]
        self.dir_guess[vid] = (s, guess)
        return guess

    def check_auto(self):
        """Per-vehicle now (see speed_ok); kept so status shows what happened."""
        if self.speed_mode == "auto":
            self.stats["vehicles_with_real_speed"] = len(self.speed_ok)

    def is_excluded(self, line: str) -> bool:
        """Regional trains in the Sydney Trains feed are track-circuit copies of
        trains NSW TrainLink reports by GPS; only the TrainLink ones are used."""
        if line not in self.excluded:
            ex = False
            if self.feed.key == "sydneytrains":
                ex = train_network("sydneytrains", line, self.static.line_meta(line)) == "regional"
            self.excluded[line] = ex
        return self.excluded[line]

    def valid_lines(self) -> set[str]:
        lines = set(self.static.route_line.values())
        return {l for l in lines if not self.is_excluded(l)}

    def summary(self) -> dict:
        s = dict(self.stats)
        s["speed_mode"] = self.speed_mode
        s["static_source"] = self.static.source_path if self.static else None
        s["lines_seen"] = sum(1 for g in self.geoms.values() if g is not None)
        missing = sorted(k for k, g in self.geoms.items() if g is None)
        if missing:
            s["lines_without_geometry"] = missing[:20]
        if self.static is not None and getattr(self.static, "notes", None):
            s["static_notes"] = self.static.notes
        if self.nt_examples:
            s["non_timetabled_examples"] = self.nt_examples
        if self.unmatched_routes:
            s["unmatched_examples"] = [r for r, _ in self.unmatched_routes.most_common(8)]
        if self.errors:
            s["errors"] = list(self.errors)[-5:]
        if s.get("fixes_on_line"):
            s["track_circuit_share"] = round(s.get("fixes_track_circuit", 0) / s["fixes_on_line"], 3)
            s["shared_position_share"] = round(s.get("fixes_shared_position", 0) / s["fixes_on_line"], 3)
        if self.speed_samples:
            v = sorted(self.speed_samples)
            q = lambda p: round(v[min(len(v) - 1, int(p * len(v)))], 1)
            s["reported_kmh"] = {"n": len(v), "zero_share": round(sum(1 for x in v if x < 0.5) / len(v), 3),
                                 "p10": q(0.1), "p50": q(0.5), "p90": q(0.9), "max": round(v[-1], 1),
                                 "distinct": len(set(round(x, 1) for x in v))}
        if self.raw_samples:
            s["raw_samples"] = self.raw_samples
        return s


def run(feeds: list[Feed], minutes: float, store: Path, cache: Path,
        rebuild: set[str], api: Api | None = None, once: bool = False) -> dict:
    api = api or Api()
    store.mkdir(parents=True, exist_ok=True)
    acc = Accumulator()
    runners = [FeedRunner(f, api, store, cache, acc, rebuild) for f in feeds]
    gps_runs: dict[str, int] = {}  # run number -> last time NSW TrainLink reported it
    for r in runners:
        r.gps_runs = gps_runs
    deadline = time.time() + minutes * 60
    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))
    fatal = None

    # lines asked to be rebuilt are rebuilt now, even if no vehicle shows up this run
    for item in sorted(rebuild):
        fk, _, line = item.partition(":")
        r = next((r for r in runners if r.feed.key == fk), None)
        if r and line and r.ensure_static():
            g = r.geometry(line)
            if g is not None:
                acc.data.setdefault((fk, line), {})

    # stagger first polls so feeds don't all fire together
    queue = [(time.time() + i * 0.5, i) for i in range(len(runners))]
    heapq.heapify(queue)
    while queue and not stop["flag"]:
        due, i = heapq.heappop(queue)
        if due > deadline:
            break
        time.sleep(max(0.0, due - time.time()))
        r = runners[i]
        try:
            r.poll()
            r.fail_streak = 0
        except QuotaExceeded as e:
            fatal = str(e)
            print(f"stopping: {e}")
            break
        except ApiError as e:
            r.stats["polls_failed"] += 1
            r.fail_streak += 1
            r.errors.append(str(e))
        except Exception as e:
            r.stats["polls_failed"] += 1
            r.fail_streak += 1
            r.errors.append(f"{type(e).__name__}: {e}")
            traceback.print_exc()
        if r.disabled or once:
            continue
        # back off a feed that keeps failing (endpoint down or retired)
        wait = r.feed.interval if r.fail_streak < 3 else min(600, r.feed.interval * 2 ** r.fail_streak)
        heapq.heappush(queue, (max(time.time(), due + wait), i))

    # ---- merge
    touched = {}
    geoms = {}
    metas = {}
    for feed, line in acc.positions:
        acc.data.setdefault((feed, line), {})
    shares = {}
    for (feed, line), cells in acc.data.items():
        touched[(feed, line)] = merge_line(store, feed, line, cells)
        if (feed, line) in acc.positions:
            shares[(feed, line)] = merge_positions(store, feed, line, acc.positions[(feed, line)])
        runner = next(r for r in runners if r.feed.key == feed)
        geoms[(feed, line)] = runner.geoms.get(line)
        if runner.static is not None:
            metas[(feed, line)] = runner.static.line_meta(line)
    for key, total in prune_expired(store, set(touched)).items():
        touched[key] = total
    update_index(store, touched, geoms, metas, shares)
    removed = prune_obsolete(store, {r.feed.key: r.valid_lines() for r in runners if r.static is not None})
    status = {
        "minutes": minutes,
        "requests": api.requests,
        "samples": acc.samples,
        "fatal": fatal,
        "removed_lines": removed,
        "feeds": {r.feed.key: r.summary() for r in runners},
    }
    write_status(store, status)
    return status


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--minutes", type=float, default=50)
    p.add_argument("--store", default="store", help="checkout of the data branch")
    p.add_argument("--cache", default="gtfs_cache", help="static GTFS cache directory")
    p.add_argument("--feeds", help="comma-separated feed keys (default: all)")
    p.add_argument("--rebuild", action="append", default=[],
                   help="feed:line (comma-separated for several) whose geometry should be "
                        "rebuilt; drops their totals")
    p.add_argument("--once", action="store_true", help="poll each feed once, for testing")
    p.add_argument("--refresh-static", action="store_true", help="re-download static GTFS now")
    args = p.parse_args()
    global REFRESH_STATIC
    REFRESH_STATIC = args.refresh_static

    feeds = FEEDS if not args.feeds else [BY_KEY[k.strip()] for k in args.feeds.split(",")]
    rebuild = {x.strip() for r in args.rebuild for x in r.split(",") if x.strip()}
    status = run(feeds, args.minutes, Path(args.store), Path(args.cache),
                 rebuild, once=args.once)

    print(f"\n{status['samples']} samples from {status['requests']} requests")
    for k, s in status["feeds"].items():
        ok, bad = s.get("polls_ok", 0), s.get("polls_failed", 0)
        smp = s.get("samples_reported", 0) + s.get("samples_estimated", 0)
        note = f"  ({s['errors'][-1]})" if s.get("errors") and not ok else ""
        print(f"  {k:32} polls {ok:>4} ok {bad:>3} failed  lines {s['lines_seen']:>4}  "
              f"samples {smp:>7}  {s['speed_mode']}{note}")


if __name__ == "__main__":
    main()
