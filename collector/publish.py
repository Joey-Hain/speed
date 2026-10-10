"""
Publish a delta to the data branch without overwriting anyone else's pushes.

  python -m collector.publish --delta delta --dir store --remote <git url>

Fetch the latest data branch into --dir, add the delta, amend the branch's
single commit, and push with --force-with-lease against the commit fetched.
If another collector pushed in between, the push is refused; fetch and try
again.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

from .delta import merge_delta


def _git(cwd: Path, *args, check=True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, check=check, text=True, capture_output=True)


def publish(delta: Path, workdir: Path, remote: str, message: str, tries: int = 6) -> dict:
    workdir.mkdir(parents=True, exist_ok=True)
    if not (workdir / ".git").exists():
        _git(workdir, "init", "-q", "-b", "data")
    if not _git(workdir, "config", "user.email", check=False).stdout.strip():
        _git(workdir, "config", "user.name", "speed collector")
        _git(workdir, "config", "user.email", "speed-collector@users.noreply.github.com")
    for attempt in range(1, tries + 1):
        got = _git(workdir, "fetch", "-q", "--depth", "1", remote, "data", check=False)
        if got.returncode == 0:
            _git(workdir, "reset", "-q", "--hard", "FETCH_HEAD")
            _git(workdir, "clean", "-qfdx")
            base = _git(workdir, "rev-parse", "HEAD").stdout.strip()
        else:
            base = ""  # no data branch yet
        status = merge_delta(delta, workdir)
        _git(workdir, "add", "-A")
        if base:
            _git(workdir, "commit", "-q", "--amend", "--reset-author", "-m", message)
            lease = f"--force-with-lease=data:{base}"
        else:
            _git(workdir, "commit", "-q", "-m", message)
            lease = "--force-with-lease=data:"
        pushed = _git(workdir, "push", "-q", lease, remote, "HEAD:data", check=False)
        if pushed.returncode == 0:
            print(f"published on attempt {attempt}")
            return status
        print(f"push refused (someone else published); retrying: {pushed.stderr.strip()[:200]}")
        time.sleep(3 * attempt)
    raise SystemExit("could not publish after several attempts")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--delta", required=True)
    p.add_argument("--dir", required=True, help="working checkout of the data branch")
    p.add_argument("--remote", required=True)
    p.add_argument("--message", default="Data update " + time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime()))
    a = p.parse_args()
    publish(Path(a.delta), Path(a.dir), a.remote, a.message)


if __name__ == "__main__":
    main()
