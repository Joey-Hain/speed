"""Collector version. Bump it whenever a change alters the numbers collected,
and raise delta.MIN_VERSION for the feeds/lines it affects, so uploads from
older copies of the collector can't bring the old numbers back. The local
collector also refuses to run when GitHub has a newer version.

  1  (implicit, no version recorded) everything up to 2026-10-11
  3  backwards-glitch fix, L1 ceiling from the last hop, 90 s Sydney Trains spans
  4  dwell timed in feed time (feed lag made passing trains look stopped),
     misplaced airport-line circuit skipped
  5  trains in an up/down layout from Central, express by regular stops,
     stops only near timetabled stations, stale fixes ignored
  6  regular stops counted only from trains that run that branch (T8 Airport
     trains were all "express" for skipping Redfern), request stops and
     T1's Lidcombe-Harris Park never regular, branch ends cut exactly
  7  limited stops ("lim") between all stations and express, each trip rated
     once; South Coast branches reworked; regional trains unrated
"""
VERSION = 7
