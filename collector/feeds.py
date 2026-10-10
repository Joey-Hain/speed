"""
Registry of TfNSW GTFS-realtime vehicle position feeds.

Each feed lists:
  rt       realtime vehicle positions path
  static   candidate static GTFS paths, tried in order (first that works is used)
  mode     train | metro | lightrail | bus | ferry  (used for defaults and the site)
  speed    "reported"  - GPS feeds that send position.speed (m/s per GTFS-RT spec)
           "estimated" - track-circuit feeds (Sydney Trains, L1): speed is estimated
                         from movement between successive track positions
           "auto"      - use reported speed when a fix carries it, else estimate
  interval seconds between polls

Daily request budget on the default Bronze plan is 60,000 requests and 5 req/s.
With the intervals below a full day of polling is about 40,000 requests, leaving
headroom for static GTFS downloads and manual runs. Check the run summary
(status.json) if TfNSW renames or retires a feed; a failing feed is skipped and
reported, it never stops the others.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Feed:
    key: str
    name: str
    mode: str
    rt: str
    static: tuple[str, ...]
    speed: str = "auto"
    interval: int = 60
    segment_m: int = 250
    tolerance_m: int = 120
    speed_scale: float = 1.0  # multiply reported speed by this to get m/s
    hist_kmh: int = 0         # histogram bin width; 0 = 1 km/h for rail modes, 5 otherwise
    max_kmh: int = 0          # faster samples are rejected; 0 = the mode's default
    speed_ceiling_kmh: float = 0  # the feed clamps reported speed here; at it, use movement instead
    min_span_s: int = 0           # shortest span for movement-based speed; 0 = speed.MIN_SPAN_S

    @property
    def hist_width(self) -> int:
        return self.hist_kmh or (1 if self.mode in ("train", "metro", "lightrail") else 5)


def _rb(region: str, label: str) -> Feed:
    return Feed(
        key=f"regionbuses-{region}",
        name=f"Regional buses: {label}",
        mode="bus",
        rt=f"/v1/gtfs/vehiclepos/regionbuses/{region}",
        static=(f"/v1/gtfs/schedule/regionbuses/{region}",),
        speed="auto",
        interval=120,
        segment_m=250,
        tolerance_m=80,
    )


FEEDS: list[Feed] = [
    # TrainLink first: its run numbers are used to drop the Sydney Trains copies
    Feed("nswtrains", "NSW TrainLink", "train",
         "/v1/gtfs/vehiclepos/nswtrains", ("/v1/gtfs/schedule/nswtrains",),
         speed="auto", interval=30, segment_m=1000, tolerance_m=300, max_kmh=165),

    # ---- track-circuit based: speed has to be estimated
    Feed("sydneytrains", "Sydney Trains", "train",
         "/v2/gtfs/vehiclepos/sydneytrains",
         ("/v1/gtfs/schedule/sydneytrains",),  # there is no v2 schedule (404)
         speed="estimated", interval=15, segment_m=250, tolerance_m=200,
         max_kmh=140,  # nothing in the Sydney Trains network runs faster
         # track circuits are a few hundred metres long, so a 45 s span reads
         # 115 km/h track at ~125 in the 85th percentile; 90 s halves that error
         min_span_s=90),
    # L1 carries a real speed, but the feed clamps it at 14 m/s (50.4 km/h):
    # at the ceiling, speed is worked out from movement instead
    Feed("lightrail-innerwest", "Inner West Light Rail (L1)", "lightrail",
         "/v2/gtfs/vehiclepos/lightrail/innerwest",
         ("/v2/gtfs/schedule/lightrail/innerwest", "/v1/gtfs/schedule/lightrail/innerwest"),
         speed="auto", interval=15, segment_m=100, tolerance_m=80, speed_ceiling_kmh=50.4),

    # ---- GPS based
    # Metro: polled often and cut into 100 m segments, because the main use is
    # reading line speed off the 85th-95th percentile profile
    Feed("metro", "Sydney Metro", "metro",
         "/v2/gtfs/vehiclepos/metro", ("/v2/gtfs/schedule/metro",),
         speed="auto", interval=15, segment_m=100, tolerance_m=100),
    Feed("lightrail-cbdandsoutheast", "CBD and South East Light Rail (L2, L3)", "lightrail",
         "/v1/gtfs/vehiclepos/lightrail/cbdandsoutheast",
         ("/v1/gtfs/schedule/lightrail/cbdandsoutheast",),
         speed="auto", interval=30, segment_m=100, tolerance_m=60),
    Feed("lightrail-parramatta", "Parramatta Light Rail (L4)", "lightrail",
         "/v1/gtfs/vehiclepos/lightrail/parramatta",
         ("/v1/gtfs/schedule/lightrail/parramatta",),
         speed="auto", interval=30, segment_m=100, tolerance_m=60),
    Feed("lightrail-newcastle", "Newcastle Light Rail", "lightrail",
         "/v1/gtfs/vehiclepos/lightrail/newcastle",
         ("/v1/gtfs/schedule/lightrail/newcastle",),
         speed="estimated", interval=30, segment_m=100, tolerance_m=60),  # speed field is always 0
    Feed("buses", "Sydney buses", "bus",
         "/v1/gtfs/vehiclepos/buses", ("/v1/gtfs/schedule/buses",),
         speed="auto", interval=60, segment_m=200, tolerance_m=60),
    Feed("ferries-sydneyferries", "Sydney Ferries", "ferry",
         "/v1/gtfs/vehiclepos/ferries/sydneyferries",
         ("/v1/gtfs/schedule/ferries/sydneyferries",),
         speed="auto", interval=60, segment_m=250, tolerance_m=400),
    Feed("ferries-mff", "Manly Fast Ferry", "ferry",
         "/v1/gtfs/vehiclepos/ferries/MFF", ("/v1/gtfs/schedule/ferries/MFF",),
         speed="auto", interval=60, segment_m=250, tolerance_m=400),

    # ---- Transport Connected Bus (regional)
    _rb("centralwestandorana", "Central West and Orana"),
    _rb("centralwestandorana2", "Central West and Orana 2"),
    _rb("farwest", "Far West"),
    _rb("newcastlehunter", "Newcastle and Hunter"),
    _rb("newenglandnorthwest", "New England North West"),
    _rb("northcoast", "North Coast"),
    _rb("northcoast2", "North Coast 2"),
    _rb("riverinamurray", "Riverina Murray"),
    _rb("riverinamurray2", "Riverina Murray 2"),
    _rb("southeasttablelands", "South East and Tablelands"),
    _rb("southeasttablelands2", "South East and Tablelands 2"),
    _rb("sydneysurrounds", "Sydney Surrounds"),
]

BY_KEY = {f.key: f for f in FEEDS}
