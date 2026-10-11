"""Collector version. Bump it whenever a change alters the numbers collected,
and raise delta.MIN_VERSION for the feeds/lines it affects, so uploads from
older copies of the collector can't bring the old numbers back. The local
collector also refuses to run when GitHub has a newer version.

  1  (implicit, no version recorded) everything up to 2026-10-11
  3  backwards-glitch fix, L1 ceiling from the last hop, 90 s Sydney Trains spans
  4  dwell timed in feed time (feed lag made passing trains look stopped),
     misplaced airport-line circuit skipped
"""
VERSION = 4
