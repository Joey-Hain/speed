"""Static GTFS download, caching and lookup tables for one feed."""

from __future__ import annotations

import io
import re
import time
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

from .api import Api, ApiError
from .feeds import Feed


def line_key(short_name: str, long_name: str, route_id: str) -> str:
    raw = (short_name or long_name or route_id or "").strip()
    return re.sub(r"[^A-Za-z0-9_-]+", "_", raw).strip("_") or "unknown"


class StaticGtfs:
    """Lookup tables for a feed's static GTFS. Shapes and stop_times load lazily."""

    def __init__(self, feed: Feed, zip_path: Path, source_path: str):
        self.feed = feed
        self.zip_path = zip_path
        self.source_path = source_path
        self._shapes: dict[str, np.ndarray] | None = None
        self._stops: pd.DataFrame | None = None

        routes = self._read("routes.txt")
        for c in ("route_short_name", "route_long_name", "route_color", "route_desc"):
            if c not in routes:
                routes[c] = ""
        routes = routes.fillna("")
        routes["line"] = [line_key(s, l, r) for s, l, r in
                          zip(routes.route_short_name, routes.route_long_name, routes.route_id)]
        self.routes = routes.set_index("route_id")
        self.route_line: dict[str, str] = self.routes["line"].to_dict()

        trips = self._read("trips.txt", ["trip_id", "route_id", "direction_id",
                                         "shape_id", "trip_headsign"])
        for c in ("direction_id", "shape_id", "trip_headsign"):
            if c not in trips:
                trips[c] = ""
        trips = trips.fillna("")
        trips.loc[trips.direction_id == "", "direction_id"] = "0"
        trips["line"] = trips["route_id"].map(self.route_line).fillna("")
        self.trips = trips
        self.trip_info: dict[str, tuple[str, str]] = dict(
            zip(trips.trip_id, zip(trips.route_id, trips.direction_id)))

    # ------------------------------------------------------------------ io
    def _read(self, name: str, cols: list[str] | None = None, **kw) -> pd.DataFrame:
        with zipfile.ZipFile(self.zip_path) as z:
            names = {n.split("/")[-1]: n for n in z.namelist()}
            if name not in names:
                return pd.DataFrame(columns=cols or [])
            with z.open(names[name]) as f:
                use = (lambda c: c in cols) if cols else None
                return pd.read_csv(io.TextIOWrapper(f, "utf-8-sig"), usecols=use,
                                   dtype=str, keep_default_na=False, **kw)

    # -------------------------------------------------------------- lookups
    def line_meta(self, line: str) -> dict:
        r = self.routes[self.routes.line == line]
        if r.empty:
            return {"name": line, "colour": "", "route_ids": [], "route_type": "", "desc": ""}
        first = r.iloc[0]
        return {
            "name": first.route_long_name or first.route_short_name or line,
            "colour": first.route_color,
            "route_ids": list(r.index),
            "route_type": first.get("route_type", ""),
            "desc": first.route_desc,
        }

    def shapes(self) -> dict[str, np.ndarray]:
        """shape_id -> (n, 2) array of lon, lat in sequence order."""
        if self._shapes is None:
            df = self._read("shapes.txt", ["shape_id", "shape_pt_lat", "shape_pt_lon",
                                           "shape_pt_sequence"])
            df["seq"] = pd.to_numeric(df.shape_pt_sequence, errors="coerce")
            df["lat"] = pd.to_numeric(df.shape_pt_lat, errors="coerce")
            df["lon"] = pd.to_numeric(df.shape_pt_lon, errors="coerce")
            df = df.dropna(subset=["seq", "lat", "lon"]).sort_values(["shape_id", "seq"])
            self._shapes = {sid: g[["lon", "lat"]].to_numpy()
                            for sid, g in df.groupby("shape_id", sort=False)}
        return self._shapes

    def stops(self) -> pd.DataFrame:
        if self._stops is None:
            s = self._read("stops.txt", ["stop_id", "stop_name", "stop_lat", "stop_lon",
                                         "parent_station"])
            if "parent_station" not in s:
                s["parent_station"] = ""
            s = s.set_index("stop_id")
            names = s["stop_name"].to_dict()
            s["station"] = [names.get(p, n) if p else n
                            for p, n in zip(s.parent_station, s.stop_name)]
            s["station"] = s["station"].str.replace(
                r"\s*(,\s*)?(Platform|Wharf|Stand|Side)\s*\w*$", "", regex=True
            ).str.replace(r"\s+Station$", "", regex=True).str.replace(
                r"\s+Light Rail$", "", regex=True)
            s["lat"] = pd.to_numeric(s.stop_lat, errors="coerce")
            s["lon"] = pd.to_numeric(s.stop_lon, errors="coerce")
            self._stops = s
        return self._stops

    def stops_for_trips(self, trip_ids: set[str]) -> dict[str, list[str]]:
        """trip_id -> ordered stop_ids where the trip actually stops.

        Sydney Trains timetables also list every station a train runs through,
        with pickup_type and drop_off_type both 1 (no pickup, no drop-off);
        those are left out so expresses only show the stations they serve.
        """
        out: dict[str, list[tuple[int, str]]] = {}
        with zipfile.ZipFile(self.zip_path) as z:
            names = {n.split("/")[-1]: n for n in z.namelist()}
            if "stop_times.txt" not in names:
                return {}
            with z.open(names["stop_times.txt"]) as f:
                cols = ["trip_id", "stop_id", "stop_sequence", "pickup_type", "drop_off_type"]
                for chunk in pd.read_csv(io.TextIOWrapper(f, "utf-8-sig"),
                                         usecols=lambda c: c in cols, dtype=str,
                                         keep_default_na=False, chunksize=1_000_000):
                    hit = chunk[chunk.trip_id.isin(trip_ids)]
                    if "pickup_type" in hit and "drop_off_type" in hit:
                        hit = hit[~((hit.pickup_type == "1") & (hit.drop_off_type == "1"))]
                    for t, s, q in zip(hit.trip_id, hit.stop_id, hit.stop_sequence):
                        out.setdefault(t, []).append((int(q), s))
        return {t: [s for _, s in sorted(v)] for t, v in out.items()}


def load_static(feed: Feed, api: Api, cache_dir: Path, max_age_days: float = 3) -> StaticGtfs:
    cache_dir.mkdir(parents=True, exist_ok=True)
    zp = cache_dir / f"{feed.key}.zip"
    src_file = cache_dir / f"{feed.key}.source"
    fresh = zp.exists() and time.time() - zp.stat().st_mtime < max_age_days * 86400
    notes = []
    if not fresh:
        last_err = None
        for path in feed.static:
            try:
                tmp = zp.with_suffix(".part")
                api.download(path, tmp)
                if not zipfile.is_zipfile(tmp):
                    raise ApiError(f"{path} did not return a zip")
                tmp.replace(zp)
                src_file.write_text(path)
                break
            except ApiError as e:
                last_err = e
                notes.append(f"{path}: {e}")
        else:
            if not zp.exists():
                raise ApiError(f"no static GTFS for {feed.key}: {last_err}")
            # keep using the stale copy rather than failing
    source = src_file.read_text().strip() if src_file.exists() else feed.static[0]
    st = StaticGtfs(feed, zp, source)
    st.notes = notes
    return st
