"""Thin TfNSW Open Data API client with throttling."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import requests

API_BASE = "https://api.transport.nsw.gov.au"


class ApiError(Exception):
    pass


class QuotaExceeded(ApiError):
    """Daily quota or key problem: stop polling everything."""


class Api:
    def __init__(self, key: str | None = None, min_gap: float = 0.25):
        if key is None:
            try:
                from dotenv import load_dotenv
                load_dotenv()
            except ImportError:
                pass
            key = os.getenv("TFNSW_API_KEY", "").strip()
        if not key:
            raise SystemExit("TFNSW_API_KEY is not set (.env locally, repository secret on GitHub).")
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"apikey {key}"
        self.min_gap = min_gap  # TfNSW allows 5 req/s; stay under it
        self._last = 0.0
        self._lock = threading.Lock()
        self.requests = 0

    def _throttle(self):
        with self._lock:
            wait = self._last + self.min_gap - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            self.requests += 1

    def _check(self, r: requests.Response, path: str):
        if r.ok:
            return
        detail = r.headers.get("X-Error-Detail", "") or r.text[:200]
        if r.status_code in (401, 403) and ("Quota" in detail or "Rate" in detail
                                            or r.status_code == 401):
            if "Rate" in detail:
                raise ApiError(f"{path}: throttled ({detail})")
            raise QuotaExceeded(f"{path}: {r.status_code} {detail}")
        raise ApiError(f"{path}: HTTP {r.status_code} {detail}".strip())

    def get(self, path: str, timeout: float = 60) -> bytes:
        self._throttle()
        try:
            r = self.session.get(API_BASE + path, timeout=timeout)
        except requests.RequestException as e:
            raise ApiError(f"{path}: {e}") from e
        self._check(r, path)
        return r.content

    def download(self, path: str, dest: Path, timeout: float = 600) -> None:
        self._throttle()
        try:
            with self.session.get(API_BASE + path, timeout=timeout, stream=True) as r:
                self._check(r, path)
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
        except requests.RequestException as e:
            raise ApiError(f"{path}: {e}") from e
