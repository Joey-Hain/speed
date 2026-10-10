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
  day       wd = weekday, we = weekend (local time)
  band      night 00-06, am 06-10, mid 10-15, pm 15-19, eve 19-24
  n, sum, sumsq, hist
            running speed: stopped time left out (GPS fixes under 1 km/h,
            or the dwell before a track-section move)
  n_all, sum_all, sumsq_all, hist_all
            the same with stopped time included
  hist      speed histogram, "bin:count;bin:count", bin width hist_kmh from
            geometry.json (1 km/h for rail, 5 otherwise), for percentiles

Totals only ever get added to, so any number of runs merge cleanly.
mean = sum / n,  sd = sqrt(sumsq / n - mean^2)
"""

from __future__ import annotations

import csv
import json
import time
from pathlib import Path

from .feeds import BY_KEY

COLS = ["dir", "branch", "seg", "day", "band",
        "n", "sum", "sumsq", "hist", "n_all", "sum_all", "sumsq_all", "hist_all"]


def _hist_parse(txt: str) -> dict[int, int]:
    out = {}
    for part in (txt or "").split(";"):
        if part:
            b, c = part.split(":")
            out[int(b)] = int(c)
    return out


def _hist_text(h: dict[int, int]) -> str:
    return ";".join(f"{b}:{h[b]}" for b in sorted(h))


def merge_line(store: Path, feed: str, line: str, cells: dict) -> int:
    """Add this run's Cells into the line's speeds.csv. Returns total running samples."""
    p = store / feed / line / "speeds.csv"
    totals: dict[tuple, list] = {}
    if p.exists():
        with p.open() as f:
            for r in csv.DictReader(f):
                k = (r["dir"], int(r["branch"]), int(r["seg"]), r["day"], r["band"])
                totals[k] = [int(r["n"]), float(r["sum"]), float(r["sumsq"]),
                             _hist_parse(r.get("hist", "")),
                             int(r.get("n_all") or 0), float(r.get("sum_all") or 0),
                             float(r.get("sumsq_all") or 0), _hist_parse(r.get("hist_all", ""))]
    for k, c in cells.items():
        t = totals.setdefault(k, [0, 0.0, 0.0, {}, 0, 0.0, 0.0, {}])
        t[0] += c.n; t[1] += c.sum; t[2] += c.sumsq
        t[4] += c.n_all; t[5] += c.sum_all; t[6] += c.sumsq_all
        for src, dst in ((c.hist, t[3]), (c.hist_all, t[7])):
            for b, n in src.items():
                dst[b] = dst.get(b, 0) + n
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


def merge_positions(store: Path, feed: str, line: str, cells: dict) -> tuple[int, int]:
    """positions.csv: dir,branch,seg,fixes,tc. 'tc' counts fixes positioned by
    track circuits rather than GPS (Sydney Trains' stop_id "... Loc").
    Returns line totals (fixes, tc)."""
    p = store / feed / line / "positions.csv"
    totals: dict[tuple, list[int]] = {}
    if p.exists():
        with p.open() as f:
            for r in csv.DictReader(f):
                totals[(r["dir"], int(r["branch"]), int(r["seg"]))] = [int(r["fixes"]), int(r.get("tc") or 0)]
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


def write_status(store: Path, status: dict):
    status["finished"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    (store / "status.json").write_text(json.dumps(status, indent=1, sort_keys=True))
