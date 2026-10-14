"""
Offline test of delta collection, combining, zip upload and lease publishing.

  python -m tests.test_delta

Runs the synthetic feeds straight into a store (A), then twice more in delta
mode, combines the two deltas, zips and unzips them, and publishes to a local
bare git repo holding A. Totals must come out at exactly 3x A, and the
Actions status.json must survive a local upload.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pandas as pd

from tests import test_synthetic as ts
from collector import collect
from collector.delta import combine_delta, merge_delta, pack, unpack
from collector.publish import publish
import collector.delta as _delta

OUT = Path(__file__).parent / "_out_delta"


def fixtures(cache):
    lat, lon = ts.line_coords()
    stations = [0, 60, 120, 180, 240, 299]
    ts.write_static(cache / "sydneytrains.zip", "TX", lat, lon, stations)
    blat, blon = ts.line_coords(150, 0.05)
    ts.write_static(cache / "buses.zip", "999", blat, blon, [0, 149])
    for f in (ts.TRAIN, ts.BUS):
        object.__setattr__(f, "interval", 0)
    return lambda: ts.FakeApi({"/rt/train": ts.train_messages(lat, lon, stations),
                               "/rt/bus": ts.bus_messages(blat, blon)})


def totals(store):
    out = {}
    for line in ("sydneytrains/TX", "buses/999"):
        df = pd.read_csv(store / line / "speeds.csv")
        pos = pd.read_csv(store / line / "positions.csv")
        out[line] = (int(df["n"].sum()), int(df["n_all"].sum()), int(pos["fixes"].sum()))
    return out


def git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, check=True, text=True, capture_output=True).stdout


def main():
    shutil.rmtree(OUT, ignore_errors=True)
    # deltas from before a fix are refused for its lines; check that, then turn it off
    assert _delta._too_old("sydneytrains|T1", 0)
    assert not _delta._too_old("sydneytrains|T1", 5)
    assert not _delta._too_old("buses|333", 0)
    cache = OUT / "cache"
    api = fixtures(cache)
    A, B = OUT / "A", OUT / "B"

    collect.run([ts.TRAIN, ts.BUS], 0.1, A, cache, set(), api=api())
    base = totals(A)
    print("direct:", base)

    collect.run([ts.TRAIN, ts.BUS], 0.1, B, cache, set(), api=api(), delta=OUT / "d1", source="local")
    collect.run([ts.TRAIN, ts.BUS], 0.1, B, cache, set(), api=api(), delta=OUT / "d2", source="local")
    assert not (B / "sydneytrains" / "TX" / "speeds.csv").exists(), "delta mode must not write totals to the store"
    assert totals(OUT / "d1") == base, "one delta run should equal one direct run"

    combine_delta(OUT / "d1", OUT / "combined")
    combine_delta(OUT / "d2", OUT / "combined")
    meta = json.loads((OUT / "combined" / "meta.json").read_text())
    assert meta["status"]["samples"] == 2 * json.loads((OUT / "d1" / "meta.json").read_text())["status"]["samples"]
    pack(OUT / "combined", OUT / "inbox" / "local-test.zip")
    up = unpack(OUT / "inbox" / "local-test.zip", OUT / "unzipped")
    assert totals(up) == {k: tuple(2 * x for x in v) for k, v in base.items()}

    # merge straight into a copy of A
    C = OUT / "C"
    shutil.copytree(A, C)
    actions_status = json.loads((C / "status.json").read_text())
    merge_delta(up, C)
    assert totals(C) == {k: tuple(3 * x for x in v) for k, v in base.items()}, totals(C)
    assert json.loads((C / "status.json").read_text()) == actions_status, "local upload must not replace status.json"
    assert (C / "status-local.json").exists()
    idx = json.loads((C / "index.json").read_text())
    assert idx["feeds"]["sydneytrains"]["lines"]["TX"]["samples"] == totals(C)["sydneytrains/TX"][0]
    print("merged:", totals(C))

    # publish: fresh remote with no data branch, then on top of it, then after
    # someone else pushed (the lease must refuse the stale base and retry)
    remote = OUT / "remote.git"
    git(OUT, "init", "-q", "--bare", str(remote))
    W = OUT / "work"
    publish(OUT / "d1", W, str(remote), "first")
    publish(OUT / "d2", W, str(remote), "second")
    chk = OUT / "check"
    git(OUT, "clone", "-q", "--branch", "data", str(remote), str(chk))
    assert totals(chk) == {k: tuple(2 * x for x in v) for k, v in base.items()}
    assert git(chk, "rev-list", "--count", "HEAD").strip() == "1", "data branch should stay one commit"

    # another collector publishes from its own checkout, then W (stale) publishes
    publish(OUT / "d1", OUT / "work2", str(remote), "other collector")
    publish(OUT / "d1", W, str(remote), "third")
    shutil.rmtree(chk)
    git(OUT, "clone", "-q", "--branch", "data", str(remote), str(chk))
    assert totals(chk) == {k: tuple(4 * x for x in v) for k, v in base.items()}, totals(chk)
    print("published:", totals(chk))

    # a push that lands between W's fetch and W's push: the lease must refuse
    # W's stale push, and the retry must include both contributions
    import collector.publish as pub
    real, raced = pub.merge_delta, {"done": False}

    def racing_merge(delta, store):
        if not raced["done"]:
            raced["done"] = True
            publish(OUT / "d2", OUT / "work2", str(remote), "racer")
        return real(delta, store)
    pub.merge_delta = racing_merge
    try:
        publish(OUT / "d1", W, str(remote), "raced")
    finally:
        pub.merge_delta = real
    shutil.rmtree(chk)
    git(OUT, "clone", "-q", "--branch", "data", str(remote), str(chk))
    assert totals(chk) == {k: tuple(6 * x for x in v) for k, v in base.items()}, totals(chk)
    print("after race:", totals(chk))
    print("OK")


if __name__ == "__main__":
    main()
