"""
Take a merged upload's totals back out of the data branch, for chosen lines.

  python tools/subtract_upload.py --zip local.zip --lines "sydneytrains|*" "lightrail-innerwest|L1"

Totals are sums, so subtracting the same upload's numbers removes exactly
what it added. Uses the same fetch / change / push-with-lease loop as
publishing, so it can run while collectors are publishing.
"""
import argparse, fnmatch, json, subprocess, sys, tempfile, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from collector import store as st
from collector.delta import unpack
from collector.geometry import LineGeometry

REMOTE_DEFAULT = "https://github.com/Joey-Hain/speed.git"


def git(cwd, *a, check=True):
    return subprocess.run(["git", *a], cwd=cwd, check=check, text=True, capture_output=True)


def subtract(store: Path, delta: Path, patterns) -> dict:
    meta = json.loads((delta / "meta.json").read_text())
    touched, geoms, shares = {}, {}, {}
    for key in meta["lines"]:
        if not any(fnmatch.fnmatch(key, p) for p in patterns):
            continue
        feed, line = key.split("|", 1)
        sp = store / feed / line / "speeds.csv"
        if sp.exists() and (delta / feed / line / "speeds.csv").exists():
            totals = st.read_speed_totals(sp)
            for k, sub in st.read_speed_totals(delta / feed / line / "speeds.csv").items():
                if k not in totals:
                    continue
                t = totals[k]
                for i in (0, 1, 2, 4, 5, 6):
                    t[i] = max(0, t[i] - sub[i])
                for i in (3, 7):
                    for b, n in sub[i].items():
                        left = t[i].get(b, 0) - n
                        if left > 0:
                            t[i][b] = left
                        else:
                            t[i].pop(b, None)
                if t[0] == 0:
                    t[1] = t[2] = 0.0
                if t[4] == 0:
                    t[5] = t[6] = 0.0
                if t[0] == 0 and t[4] == 0:
                    del totals[k]
            touched[(feed, line)] = st.write_speed_totals(sp, totals)
        pp = store / feed / line / "positions.csv"
        dpos = st.read_positions(delta / feed / line / "positions.csv")
        if pp.exists() and dpos:
            pos = st.read_positions(pp)
            for k, (n, tc) in dpos.items():
                if k in pos:
                    pos[k] = [max(0, pos[k][0] - n), max(0, pos[k][1] - tc)]
            import csv
            with pp.open("w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["dir", "branch", "seg", "fixes", "tc"])
                for k in sorted(pos):
                    if pos[k][0] > 0:
                        w.writerow([*k, *pos[k]])
            shares[(feed, line)] = (sum(v[0] for v in pos.values()), sum(v[1] for v in pos.values()))
        gp = store / feed / line / "geometry.json"
        if gp.exists():
            geoms[(feed, line)] = LineGeometry(json.loads(gp.read_text()))
        touched.setdefault((feed, line), sum(t[0] for t in st.read_speed_totals(sp).values()) if sp.exists() else 0)
    st.update_index(store, touched, geoms, None, shares)
    return touched


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--zip", required=True)
    p.add_argument("--lines", nargs="+", required=True, help='patterns like "sydneytrains|*"')
    p.add_argument("--remote", default=REMOTE_DEFAULT)
    p.add_argument("--dir", default=None)
    a = p.parse_args()
    work = Path(tempfile.mkdtemp())
    delta = unpack(Path(a.zip), work / "delta")
    wd = Path(a.dir) if a.dir else work / "store"
    wd.mkdir(parents=True, exist_ok=True)
    if not (wd / ".git").exists():
        git(wd, "init", "-q", "-b", "data")
    for attempt in range(1, 7):
        git(wd, "fetch", "-q", "--depth", "1", a.remote, "data")
        git(wd, "reset", "-q", "--hard", "FETCH_HEAD")
        git(wd, "clean", "-qfdx")
        base = git(wd, "rev-parse", "HEAD").stdout.strip()
        touched = subtract(wd, delta, a.lines)
        git(wd, "add", "-A")
        git(wd, "commit", "-q", "--amend", "--reset-author", "-m",
            "Remove " + Path(a.zip).name + " for " + ", ".join(a.lines))
        if git(wd, "push", "-q", f"--force-with-lease=data:{base}", a.remote, "HEAD:data", check=False).returncode == 0:
            print(f"removed from {len(touched)} lines on attempt {attempt}")
            return
        time.sleep(3 * attempt)
    raise SystemExit("could not push")


if __name__ == "__main__":
    main()
