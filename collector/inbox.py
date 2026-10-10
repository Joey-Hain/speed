"""
Merge collector deltas uploaded as zips (from tools/local_collect.py) into the
data branch.

    python -m collector.inbox --dir store --remote <git url> inbox/*.zip

All the zips are combined into one delta and published in one go, using the
same fetch / add / push-with-lease loop as the Actions collector, so nothing
another collector pushed meanwhile is lost.
"""

from __future__ import annotations

import argparse
import shutil
import tempfile
from pathlib import Path

from .delta import combine_delta, unpack
from .publish import publish


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("zips", nargs="+")
    p.add_argument("--dir", required=True, help="working checkout of the data branch")
    p.add_argument("--remote", required=True)
    p.add_argument("--used", help="write the zips that were merged to this file, one per line")
    a = p.parse_args()

    work = Path(tempfile.mkdtemp(prefix="inbox-"))
    combined, used = work / "combined", []
    for z in a.zips:
        try:
            d = unpack(Path(z), work / "one")
        except Exception as e:  # a broken upload shouldn't block the others
            print(f"skipping {z}: {e}")
            continue
        combine_delta(d, combined)
        used.append(z)
        print(f"added {z}")
    if not used:
        raise SystemExit("no usable deltas")
    names = ", ".join(Path(z).name for z in used)
    status = publish(combined, Path(a.dir), a.remote, f"Merge local data: {names}")
    print(f"merged {len(used)} upload(s): {status.get('samples')} samples, "
          f"{status.get('minutes')} min of polling")
    if status.get("skipped_incompatible_lines"):
        print("skipped (geometry differs from the data branch):", status["skipped_incompatible_lines"])
    if a.used:
        Path(a.used).write_text("".join(z + "\n" for z in used))
    shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
