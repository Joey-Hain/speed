"""
The data store (checked out from the repo's `data` branch).

  <store>/index.json                       every line with data, for the site
  <store>/status.json                      last run: per-feed health
  <store>/<feed>/<line>/geometry.json      frozen branches + stations
  <store>/<feed>/<line>/speeds.csv         running totals per segment
  <store>/<feed>/<line>/positions.csv      fixes per segment, and how many were
                                           track-circuit positions

speeds.csv columns:
  dir       GTFS direction_id
  branch    branch index within that direction (see geometry.json)
  seg       segment index along the branch (segment_m metres each)
  week      Monday of the week the samples are from (local time). Weeks that
            ended more than RETENTION_DAYS ago are dropped, so the totals
            always cover roughly the last 30 days.
  day       wd = weekday, we = weekend (local time)
  band      night 00-06, am 06-10, mid 10-15, pm 15-19, eve 19-24
  svc       all = trip stops at every regular station on its stretch of the branch,
            lim = skips a few, exp = skips geometry.EXPRESS_SKIPS or more
            (Sydney Trains only; everything else is "all")
  n, sum, sumsq, hist
            running speed: stopped time left out (GPS fixes under 1 km/h,
            or the dwell before a track-section move)
  n_all, sum_all, sumsq_all, hist_all
            the same with stopped time included
  hist      speed histogram, "bin:count;bin:count", bin width hist_kmh from
            geometry.json (1 km/h for rail, 5 otherwise), for percentiles

Within a week, totals only ever get added to, so any number of runs merge cleanly.
mean = sum / n,  sd = sqrt(sumsq / n - mean^2)
"""

from __future__ import annotations

import csv
import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .feeds import BY_KEY

COLS = ["dir", "branch", "seg", "week", "day", "band", "svc",
        "n", "sum", "sumsq", "hist", "n_all", "sum_all", "sumsq_all", "hist_all"]
RETENTION_DAYS = 30
TZ = ZoneInfo("Australia/Sydney")


def oldest_week_kept(today: date | None = None) -> str:
    """Monday of the oldest week still inside the retention window."""
    today = today or datetime.now(TZ).date()
    cutoff = today - timedelta(days=RETENTION_DAYS)
    # keep a week while any of its days is inside the window
    return (cutoff - timedelta(days=cutoff.weekday())).isoformat()


def _this_week() -> str:
    t = datetime.now(TZ).date()
    return (t - timedelta(days=t.weekday())).isoformat()


def _hist_parse(txt: str) -> dict[int, int]:
    out = {}
    for part in (txt or "").split(";"):
        if part:
            b, c = part.split(":")
            out[int(b)] = int(c)
    return out


def _hist_text(h: dict[int, int]) -> str:
    return ";".join(f"{b}:{h[b]}" for b in sorted(h))


def read_speed_totals(p: Path) -> dict[tuple, list]:
    """speeds.csv -> {key: [n, sum, sumsq, hist, n_all, sum_all, sumsq_all, hist_all]},
    dropping weeks outside the retention window."""
    totals: dict[tuple, list] = {}
    keep_from = oldest_week_kept()
    if p.exists():
        with p.open() as f:
            for r in csv.DictReader(f):
                week = r.get("week") or _this_week()  # rows from before weekly buckets
                if week < keep_from:
                    continue
                k = (r["dir"], int(r["branch"]), int(r["seg"]), week, r["day"], r["band"],
                     r.get("svc") or "all")
                totals[k] = [int(r["n"]), float(r["sum"]), float(r["sumsq"]),
                             _hist_parse(r.get("hist", "")),
                             int(r.get("n_all") or 0), float(r.get("sum_all") or 0),
                             float(r.get("sumsq_all") or 0), _hist_parse(r.get("hist_all", ""))]
    return totals


def _add_totals(totals: dict, k: tuple, add: list):
    t = totals.setdefault(k, [0, 0.0, 0.0, {}, 0, 0.0, 0.0, {}])
    for i in (0, 1, 2, 4, 5, 6):
        t[i] += add[i]
    for i in (3, 7):
        for b, n in add[i].items():
            t[i][b] = t[i].get(b, 0) + n


def write_speed_totals(p: Path, totals: dict) -> int:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with tmp.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(COLS)
        for k in sorted(totals):
            n, s, ss, h, na, sa, ssa, ha = totals[k]
            w.writerow([*k, n, round(s, 1), round(ss, 1), _hist_text(h),
                        na, round(sa, 1), round(ssa, 1), _hist_text(ha)])
    tmp.replace(p)
    return int(sum(t[0] for t in totals.values()))


def merge_line(store: Path, feed: str, line: str, cells: dict) -> int:
    """Add this run's Cells into the line's speeds.csv. Returns total running samples."""
    p = store / feed / line / "speeds.csv"
    totals = read_speed_totals(p)
    keep_from = oldest_week_kept()
    for k, c in cells.items():
        if k[3] < keep_from:
            continue
        _add_totals(totals, k, [c.n, c.sum, c.sumsq, c.hist, c.n_all, c.sum_all, c.sumsq_all, c.hist_all])
    return write_speed_totals(p, totals)


def merge_speed_file(store: Path, feed: str, line: str, delta_file: Path) -> int:
    """Add another collector's speeds.csv (a delta) into the store's."""
    p = store / feed / line / "speeds.csv"
    totals = read_speed_totals(p)
    for k, add in read_speed_totals(delta_file).items():
        _add_totals(totals, k, add)
    return write_speed_totals(p, totals)


def remap_line(line_dir: Path, plan: dict) -> None:
    """Move a line's totals onto rebuilt branches: plan is {dir: {old branch:
    (new branch, segment shift)}} (geometry.remap_plan). Totals on branches the
    plan doesn't cover are dropped; cells landing together are added up."""
    sp = line_dir / "speeds.csv"
    if sp.exists():
        out: dict[tuple, list] = {}
        for k, t in read_speed_totals(sp).items():
            m = plan.get(k[0], {}).get(k[1])
            if m is not None and k[2] + m[1] >= 0:
                _add_totals(out, (k[0], m[0], k[2] + m[1], *k[3:]), t)
        write_speed_totals(sp, out)
    pp = line_dir / "positions.csv"
    if pp.exists():
        pos: dict[tuple, list[int]] = {}
        for k, (n, tc) in read_positions(pp).items():
            m = plan.get(k[0], {}).get(k[1])
            if m is not None and k[2] + m[1] >= 0:
                t = pos.setdefault((k[0], m[0], k[2] + m[1]), [0, 0])
                t[0] += n
                t[1] += tc
        with pp.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["dir", "branch", "seg", "fixes", "tc"])
            for k in sorted(pos):
                w.writerow([*k, *pos[k]])


def read_positions(p: Path) -> dict[tuple, list[int]]:
    totals: dict[tuple, list[int]] = {}
    if p.exists():
        with p.open() as f:
            for r in csv.DictReader(f):
                totals[(r["dir"], int(r["branch"]), int(r["seg"]))] = [int(r["fixes"]), int(r.get("tc") or 0)]
    return totals


def merge_positions(store: Path, feed: str, line: str, cells: dict) -> tuple[int, int]:
    """positions.csv: dir,branch,seg,fixes,tc. 'tc' counts fixes positioned by
    track circuits rather than GPS (Sydney Trains' stop_id "... Loc").
    Returns line totals (fixes, tc)."""
    p = store / feed / line / "positions.csv"
    totals = read_positions(p)
    for k, (n, sh) in cells.items():
        t = totals.setdefault(k, [0, 0])
        t[0] += n
        t[1] += sh
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["dir", "branch", "seg", "fixes", "tc"])
        for k in sorted(totals):
            w.writerow([*k, *totals[k]])
    return sum(t[0] for t in totals.values()), sum(t[1] for t in totals.values())


def prune_expired(store: Path, skip: set[tuple[str, str]]) -> dict[tuple[str, str], int]:
    """Drop expired weeks from lines this run didn't touch (touched lines were
    pruned while merging). Returns new running-sample totals for lines changed."""
    keep_from = oldest_week_kept()
    changed = {}
    for p in store.glob("*/*/speeds.csv"):
        key = (p.parent.parent.name, p.parent.name)
        if key in skip:
            continue
        with p.open() as f:
            rows = list(csv.DictReader(f))
        kept = [r for r in rows if (r.get("week") or keep_from) >= keep_from]
        if len(kept) == len(rows):
            continue
        with p.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=COLS, extrasaction="ignore")
            w.writeheader()
            w.writerows(kept)
        changed[key] = sum(int(r["n"]) for r in kept)
    return changed


INTERCITY = {"BMT", "CCN", "HUN", "SCO", "SHL"}
TRAIN_FEEDS = {"sydneytrains", "nswtrains"}


def train_network(feed: str, line: str, meta: dict | None) -> str | None:
    """sydney / intercity / regional / coach for train feeds, from GTFS route_desc
    ("Sydney Trains Network", "Intercity Trains Network", "Regional Trains and
    Coaches Network") and route_type, falling back to line codes."""
    if feed not in TRAIN_FEEDS:
        return None
    meta = meta or {}
    desc = (meta.get("desc") or "").lower()
    try:
        rt = int(meta.get("route_type") or 0)
    except ValueError:
        rt = 0
    if rt == 3 or 200 <= rt < 300 or 700 <= rt < 800:
        return "coach"
    if "intercity" in desc or line in INTERCITY:
        return "intercity"
    if "sydney trains" in desc or (line[:1] == "T" and line[1:2].isdigit()):
        return "sydney"
    return "regional"


def update_index(store: Path, touched: dict[tuple[str, str], int], geometries: dict,
                 metas: dict | None = None, shares: dict | None = None):
    p = store / "index.json"
    idx = json.loads(p.read_text()) if p.exists() else {"feeds": {}}
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for (feed, line), total in touched.items():
        f = BY_KEY.get(feed)
        fe = idx["feeds"].setdefault(feed, {
            "name": f.name if f else feed, "mode": f.mode if f else "", "lines": {}})
        if f:
            fe["name"], fe["mode"] = f.name, f.mode
        g = geometries.get((feed, line))
        entry = fe["lines"].setdefault(line, {})
        net = train_network(feed, line, (metas or {}).get((feed, line)))
        if net:
            entry["network"] = net
        fx, sh = (shares or {}).get((feed, line), (0, 0))
        if fx:
            entry["track_circuit"] = round(sh / fx, 3)
        entry["samples"] = total
        entry["updated"] = now
        if g is not None:
            entry["name"] = g.data.get("name", line)
            entry["colour"] = g.data.get("colour", "")
            entry["directions"] = {d: v.get("headsign", "") for d, v in g.data["directions"].items()}
    idx["updated"] = now
    p.write_text(json.dumps(idx, indent=1, sort_keys=True))


def prune_obsolete(store: Path, valid: dict[str, set[str]]) -> list[str]:
    """Remove lines that no longer exist under their key (renamed, regrouped or
    excluded), for feeds whose static GTFS was loaded this run."""
    import shutil
    p = store / "index.json"
    if not p.exists():
        return []
    idx = json.loads(p.read_text())
    removed = []
    for feed, lines in valid.items():
        fe = idx.get("feeds", {}).get(feed)
        if not fe:
            continue
        for line in list(fe.get("lines", {})):
            if line not in lines:
                del fe["lines"][line]
                shutil.rmtree(store / feed / line, ignore_errors=True)
                removed.append(f"{feed}:{line}")
    if removed:
        p.write_text(json.dumps(idx, indent=1, sort_keys=True))
    return removed


# One-off data repairs, each applied once per store and recorded in migrations.json
MIGRATIONS = {
    # City Circle loop let fixes jump between passes (660 km/h on T8); clear the
    # totals collected before the fix. Geometry is kept, so segments still line up.
    "clear-city-circle-2026-10-10": [("sydneytrains", "T2"), ("sydneytrains", "T3"), ("sydneytrains", "T8")],
    # restart Sydney Trains totals with the 140 km/h network cap, express/all-
    # stations tagging and TrainLink's intercity GPS merged in
    "reset-sydneytrains-2026-10-10b": [("sydneytrains", "*")],
    # platform fixes now count as track-circuit too; recount the shares
    "recount-track-circuit-2026-10-10": [("sydneytrains", "*", ("positions.csv",))],
    # Sydney Trains: 90 s minimum spans, City Circle loop starts placed
    # correctly, departure hops checked against acceleration. L1: speeds above
    # the feed's 50.4 km/h ceiling now come from movement. Positions are kept.
    "spans-90s-loop-start-l1-ceiling-2026-10-10": [("sydneytrains", "*", ("speeds.csv",)),
                                                   ("lightrail-innerwest", "L1", ("speeds.csv",))],
    # a single misplaced track circuit (airport line SR1.3BER) was read as a
    # stop, then the next fix as a 120+ km/h departure; L1 ceiling now uses
    # the hop since the previous fix
    "backwards-glitch-l1-hop-2026-10-11": [("sydneytrains", "*", ("speeds.csv",)),
                                           ("lightrail-innerwest", "L1", ("speeds.csv",))],
    # feed lag made trains passing through a track section look stopped, which
    # turned running spans into with-stops 'departures' (and 120+ km/h after
    # a misplaced circuit near Central)
    "dwell-feed-time-2026-10-11": [("sydneytrains", "*", ("speeds.csv",))],
}


def apply_migrations(store: Path) -> list[str]:
    p = store / "migrations.json"
    done = set(json.loads(p.read_text())) if p.exists() else set()
    applied = []
    for name, lines in MIGRATIONS.items():
        if name in done:
            continue
        for feed, line, *files in lines:
            files = files[0] if files else ("speeds.csv", "positions.csv")
            dirs = [d for d in (store / feed).glob("*") if d.is_dir()] if line == "*" else [store / feed / line]
            for d in dirs:
                for f in files:
                    (d / f).unlink(missing_ok=True)
        done.add(name)
        applied.append(name)
    if applied:
        p.write_text(json.dumps(sorted(done)))
    return applied


def write_status(store: Path, status: dict):
    status["finished"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    (store / "status.json").write_text(json.dumps(status, indent=1, sort_keys=True))
