"""
Collect from your own computer to build up data faster than GitHub Actions
alone. No git needed.

    python tools/local_collect.py                  # run until Ctrl+C
    python tools/local_collect.py --hours 3        # stop after 3 hours
    python tools/local_collect.py --feeds metro    # only some feeds

Needs `pip install -r requirements.txt` and TFNSW_API_KEY in .env next to
README.md. The first run downloads the static timetables (a few hundred MB,
mostly buses) into .local/gtfs_cache.

It writes inbox/local-<date>-<time>.zip, updated every --save-every minutes,
so stopping (or a crash) loses at most the last few minutes. When you're done,
upload that zip to the inbox/ folder of the repo on GitHub (open inbox/, then
Add file > Upload files). The "Merge local data" workflow adds it to the data
branch and removes it from inbox/.

Ctrl+C once finishes the current poll and saves; Ctrl+C twice quits at once.

The collector uses the line geometry already on the data branch (downloaded at
start) so its segments line up with the shared totals.

API quota: TfNSW allows 60,000 requests a day per API key. The Actions
collector uses about 42,000 a day and this uses about 1,750 an hour, so with
the same key as Actions keep it under about 9 hours a day (--max-requests
stops it at 15,000 by default). A second key from the Open Data portal (create
another application) avoids sharing the quota.
"""

from __future__ import annotations

import argparse
import io
import os
import shutil
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

import requests  # noqa: E402

from collector import collect as col  # noqa: E402
from collector.api import Api  # noqa: E402
from collector.delta import combine_delta, pack  # noqa: E402
from collector.feeds import BY_KEY, FEEDS  # noqa: E402

LOCAL = ROOT / ".local"
DATA_ZIP = "https://codeload.github.com/Joey-Hain/speed/zip/refs/heads/data"


def fetch_geometry(store: Path, url: str):
    """Every line's geometry.json from the data branch (one zip download)."""
    print("downloading line geometry from the data branch ...", flush=True)
    try:
        r = requests.get(url, timeout=300)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"  couldn't download it ({e}); building geometry from the timetable instead.\n"
              f"  Lines that already exist online may then not merge.")
        return
    shutil.rmtree(store, ignore_errors=True)
    store.mkdir(parents=True)
    n = 0
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        for name in z.namelist():
            parts = Path(name).parts  # speed-data/<feed>/<line>/geometry.json
            if len(parts) == 4 and parts[3] == "geometry.json" and ".." not in parts:
                dest = store / parts[1] / parts[2] / "geometry.json"
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(z.read(name))
                n += 1
    print(f"  {n} lines")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--hours", type=float, default=0, help="stop after this long (default: until Ctrl+C)")
    p.add_argument("--feeds", help="comma-separated feed keys (default: all)")
    p.add_argument("--save-every", type=float, default=30, help="minutes between saves (default 30)")
    p.add_argument("--max-requests", type=int, default=15000,
                   help="stop after this many API requests (default 15000, ~8.5 h)")
    p.add_argument("--out", help="zip to write (default inbox/local-<date>-<time>.zip)")
    p.add_argument("--data-url", default=DATA_ZIP, help=argparse.SUPPRESS)
    a = p.parse_args()

    if not os.getenv("TFNSW_API_KEY", "").strip():
        sys.exit(f"TFNSW_API_KEY not set; put it in {ROOT / '.env'}")
    feeds = FEEDS if not a.feeds else [BY_KEY[k.strip()] for k in a.feeds.split(",")]
    store, cache = LOCAL / "store", LOCAL / "gtfs_cache"
    cycle_delta, combined = LOCAL / "cycle", LOCAL / "combined"
    out = Path(a.out) if a.out else ROOT / "inbox" / f"local-{time.strftime('%Y%m%d-%H%M')}.zip"

    fetch_geometry(store, a.data_url)
    shutil.rmtree(combined, ignore_errors=True)
    api = Api()  # one client for every cycle, so the request count is the session total
    end = time.time() + a.hours * 3600 if a.hours else None
    cycle = 0
    while True:
        minutes = a.save_every
        if end is not None:
            minutes = min(minutes, (end - time.time()) / 60)
            if minutes < 0.5:
                break
        cycle += 1
        print(f"\n[{time.strftime('%H:%M')}] polling {len(feeds)} feeds for {minutes:.0f} min "
              f"(save {cycle})", flush=True)
        status = col.run(feeds, minutes, store, cache, set(), api=api,
                         delta=cycle_delta, source="local")
        combine_delta(cycle_delta, combined)
        pack(combined, out)
        print(f"[{time.strftime('%H:%M')}] saved {out.relative_to(ROOT) if out.is_relative_to(ROOT) else out}"
              f" ({out.stat().st_size / 1e6:.1f} MB) - {status['samples']} samples this save, "
              f"{api.requests} requests so far", flush=True)
        if status.get("fatal"):
            print(f"stopped: {status['fatal']}")
            break
        if status.get("stopped"):
            break
        if api.requests >= a.max_requests:
            print(f"reached --max-requests {a.max_requests}; stopping to leave quota for GitHub Actions")
            break
    if out.exists():
        print(f"\nDone. Upload {out} to the inbox/ folder on GitHub.")
    else:
        print("\nNothing collected.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nquit; anything already saved is still in the inbox zip")
