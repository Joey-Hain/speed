"""
Deltas: what one collector run saw, kept apart from the shared store.

Several collectors can run at once (GitHub Actions and a local machine). Each
writes only its own run's totals to a delta folder; publishing fetches the
latest data branch, adds the delta on top and pushes only if nobody pushed in
between (otherwise it re-fetches and tries again). Totals are additive, so
contributions from any number of collectors combine cleanly.

delta/
  meta.json                       status, line metadata, valid lines per feed
  <feed>/<line>/geometry.json     the geometry the delta's segments refer to
  <feed>/<line>/speeds.csv        this run's totals only
  <feed>/<line>/positions.csv
"""

from __future__ import annotations

import json
import shutil
import time
import zipfile
from pathlib import Path

from . import store as st
from .version import VERSION


def write_delta(delta: Path, acc, runners, geometry_store: Path, status: dict,
                rebuilt: set[str] | None = None, source: str = "actions"):
    shutil.rmtree(delta, ignore_errors=True)
    delta.mkdir(parents=True)
    metas, lines = {}, []
    keys = set(acc.data) | set(acc.positions)
    for feed, line in keys:
        src = geometry_store / feed / line / "geometry.json"
        if not src.exists():
            continue
        d = delta / feed / line
        d.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, d / "geometry.json")
        if (feed, line) in acc.data:
            st.merge_line(delta, feed, line, acc.data[(feed, line)])
        if (feed, line) in acc.positions:
            st.merge_positions(delta, feed, line, acc.positions[(feed, line)])
        lines.append(f"{feed}|{line}")
    for r in runners:
        if r.static is None:
            continue
        for line in r.geoms:
            metas[f"{r.feed.key}|{line}"] = r.static.line_meta(line)
        # aliased lines are stored under the other feed; carry their metadata too
        if getattr(r, "alias", None) is not None and r.alias.static is not None:
            for line in r.alias.geoms:
                metas.setdefault(f"{r.alias.feed.key}|{line}", r.alias.static.line_meta(line))
    valid = {r.feed.key: sorted(r.valid_lines()) for r in runners if r.static is not None}
    (delta / "meta.json").write_text(json.dumps(
        {"source": source, "version": VERSION, "status": status, "metas": metas, "valid": valid, "lines": lines,
         "rebuilt": sorted(k.replace(":", "|", 1) for k in (rebuilt or ()))}, default=str))


def _compatible(a: dict, b: dict) -> bool:
    """Same directions and branches (so segment numbers mean the same thing)."""
    if a.get("segment_m") != b.get("segment_m") or set(a["directions"]) != set(b["directions"]):
        return False
    for d in a["directions"]:
        ba, bb = a["directions"][d]["branches"], b["directions"][d]["branches"]
        if len(ba) != len(bb):
            return False
        for x, y in zip(ba, bb):
            if abs(x["length_m"] - y["length_m"]) > 1:
                return False
    return True


# Deltas from an older collector are dropped for the lines a later fix
# affects, so an old local copy can't bring the bad numbers back.
# feed or "feed|line" -> lowest collector version accepted (see version.py).
MIN_VERSION = {
    "sydneytrains": 3,
    "lightrail-innerwest|L1": 3,
}


def _too_old(key: str, version: int) -> bool:
    feed = key.split("|", 1)[0]
    need = MIN_VERSION.get(key) or MIN_VERSION.get(feed) or 0
    return version < need


def merge_delta(delta: Path, store: Path) -> dict:
    from .geometry import LineGeometry
    meta = json.loads((delta / "meta.json").read_text())
    status = meta["status"]
    status["migrations"] = st.apply_migrations(store)
    touched, geoms, shares, skipped = {}, {}, {}, []
    rebuilt = set(meta.get("rebuilt", []))
    too_old = []
    for key in meta["lines"]:
        if _too_old(key, int(meta.get("version") or 0)):
            too_old.append(key)
            continue
        feed, line = key.split("|", 1)
        dg = json.loads((delta / feed / line / "geometry.json").read_text())
        sp = store / feed / line / "geometry.json"
        if key in rebuilt:  # new geometry on purpose: old segment numbers are void
            for f in ("speeds.csv", "positions.csv"):
                (sp.parent / f).unlink(missing_ok=True)
            sp.unlink(missing_ok=True)
        if sp.exists():
            sg = json.loads(sp.read_text())
            if not _compatible(dg, sg):
                skipped.append(key)  # built differently elsewhere; don't mix segments
                continue
            if dg.get("stations_version", 1) > sg.get("stations_version", 1):
                sp.write_text(json.dumps(dg, separators=(",", ":")))
                sg = dg
        else:
            sp.parent.mkdir(parents=True, exist_ok=True)
            sp.write_text(json.dumps(dg, separators=(",", ":")))
            sg = dg
        geoms[(feed, line)] = LineGeometry(sg)
        if (delta / feed / line / "speeds.csv").exists():
            touched[(feed, line)] = st.merge_speed_file(store, feed, line, delta / feed / line / "speeds.csv")
        else:
            touched[(feed, line)] = sum(t[0] for t in st.read_speed_totals(store / feed / line / "speeds.csv").values())
        dpos = st.read_positions(delta / feed / line / "positions.csv")
        if dpos:
            shares[(feed, line)] = st.merge_positions(store, feed, line, dpos)
    for k, total in st.prune_expired(store, set(touched)).items():
        touched[k] = total
    metas = {tuple(k.split("|", 1)): v for k, v in meta["metas"].items()}
    st.update_index(store, touched, geoms, metas, shares)
    status["removed_lines"] = st.prune_obsolete(store, {k: set(v) for k, v in meta["valid"].items()})
    if skipped:
        status["skipped_incompatible_lines"] = skipped
    if too_old:
        status["skipped_collected_before_fix"] = too_old
    if meta.get("source", "actions") == "actions":
        st.write_status(store, status)
    else:  # keep status.json about the Actions collector; note local uploads apart
        status["finished"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        (store / "status-local.json").write_text(json.dumps(status, indent=1, sort_keys=True, default=str))
    return status


def combine_delta(src: Path, dst: Path):
    """Add delta `src` into delta `dst` (both stay deltas: totals only)."""
    sm = json.loads((src / "meta.json").read_text())
    if not (dst / "meta.json").exists():
        shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(src, dst)
        return
    dm = json.loads((dst / "meta.json").read_text())
    lines = set(dm["lines"])
    for key in sm["lines"]:
        feed, line = key.split("|", 1)
        s_dir, d_dir = src / feed / line, dst / feed / line
        sg = json.loads((s_dir / "geometry.json").read_text())
        if (d_dir / "geometry.json").exists():
            dg = json.loads((d_dir / "geometry.json").read_text())
            if not _compatible(sg, dg):  # rebuilt in between: the newer one wins
                shutil.rmtree(d_dir)
        if not (d_dir / "geometry.json").exists():
            d_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy(s_dir / "geometry.json", d_dir / "geometry.json")
        if (s_dir / "speeds.csv").exists():
            st.merge_speed_file(dst, feed, line, s_dir / "speeds.csv")
        pos = st.read_positions(s_dir / "positions.csv")
        if pos:
            st.merge_positions(dst, feed, line, pos)
        lines.add(key)
    a, b = dm["status"], sm["status"]
    status = dict(b)
    for k in ("minutes", "requests", "samples"):
        status[k] = (a.get(k) or 0) + (b.get(k) or 0)
    status["started"] = a.get("started") or b.get("started")
    dm.update(status=status, lines=sorted(lines), valid=sm["valid"] or dm["valid"],
              rebuilt=sorted(set(dm.get("rebuilt", [])) | set(sm.get("rebuilt", []))))
    dm["metas"].update(sm["metas"])
    dm["version"] = min(int(dm.get("version") or 0), int(sm.get("version") or 0))
    (dst / "meta.json").write_text(json.dumps(dm, default=str))


def pack(delta: Path, zip_path: Path):
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = zip_path.with_suffix(".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(delta.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(delta).as_posix())
    tmp.replace(zip_path)


def unpack(zip_path: Path, dest: Path) -> Path:
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():  # no absolute paths or ".." escapes
            if name.startswith("/") or ".." in Path(name).parts:
                raise ValueError(f"{zip_path}: bad path {name}")
        z.extractall(dest)
    if not (dest / "meta.json").exists():
        raise ValueError(f"{zip_path}: not a collector delta (no meta.json)")
    return dest
