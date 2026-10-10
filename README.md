# tfnsw-speed

Average running speed along every NSW public transport line, built from
Transport for NSW realtime vehicle positions. A GitHub Actions job polls every
feed around the clock and adds what it sees to per-line totals; the site at
speed.joeyhain.org reads those totals and draws a distance-speed profile and a
speed map for any line.

## How it fits together

```
GitHub Actions (hourly, polls ~50 min)          Cloudflare Pages
  collector/  --->  `data` branch (CSV + JSON)  <---  site/ (static, reads data
                    single commit, replaced            branch via raw GitHub)
                    every run
```

* **collector/** polls 22 vehicle position feeds: Sydney Trains, Metro, NSW
  TrainLink, all four light rail networks, Sydney buses, Sydney Ferries, Manly
  Fast Ferry and twelve regional bus feeds (see `collector/feeds.py`).
* **Speed source per feed**
  * GPS feeds report `position.speed`. Each fix is one sample. Stationary
    fixes are skipped so profiles show running speed.
  * Sydney Trains and the L1 report positions from the signalling system, so
    speed is **estimated** over running spans of at least 45 s, which keeps the
    error from section-sized position steps small. Spans end where a train
    stops, and running speed restarts at the next position after it leaves, so
    dwell time is never counted as slow running.
  * "auto" feeds use reported speed when present and fall back to the estimate.
    A feed whose speed field is always zero is switched to the estimate.
* **Line geometry** (branches per direction, stations) is built from static
  GTFS the first time a line is seen and then frozen in `geometry.json`, so
  segment numbers stay valid when TfNSW reissues shape ids. Each fix is
  projected onto every branch it lies near, so a shared trunk feeds every
  branch's profile.
* **Storage** is running totals, not raw fixes: `n`, `sum`, `sumsq` per
  direction, branch, segment, weekday/weekend and time band. It stays small
  forever and any number of runs merge by addition. The `data` branch is
  force-pushed as one commit each run, so the repo never grows from history.

Data layout on the `data` branch:

```
index.json                      every line with data (the site's menu)
status.json                     last run: per-feed polls, errors, samples
<feed>/<line>/geometry.json     branches, stations, segment length
<feed>/<line>/speeds.csv        totals per segment (columns below)
```

`speeds.csv` has `dir, branch, seg, day, band` then two sets of totals:
`n, sum, sumsq, hist` for running speed (stopped time left out) and
`n_all, sum_all, sumsq_all, hist_all` with stopped time included, which is
what the site's "Include stops" box switches between. `hist` is a 5 km/h
histogram (`bin:count;...`, bin 12 = 60-65 km/h) that the site turns into
medians and 85th/95th percentiles. mean = sum / n, sd = sqrt(sumsq / n - mean^2).
Time bands (Sydney time): night 00-06, am 06-10, mid 10-15, pm 15-19, eve 19-24.

## Reading line speed

Pick the line, set Statistic to 85th percentile and leave "Include stops"
unticked. Each segment is drawn as a flat step in the line's colour, so a
speed limit shows as a level run; the shading is the middle half of observed
speeds. The map gradient runs from 0 to the fastest segment + 10 km/h. The mean
is dragged down by accelerating and braking near stations; a high percentile of
running speed sits close to what drivers or ATO run to. Weekday middle of day
is usually the cleanest band.

Rail speed histograms use 1 km/h bins (5 km/h for buses and ferries), so
percentiles aren't rounded. Metro is polled every 15 s into 100 m segments.

`python -m tests.test_metro` checks this on a simulated M1 (approximate
alignment, real station names) with known limits of 60/70/80/90/100 km/h,
ATO-like running (1.0 m/s^2, 1-3 km/h under the limit, 35 s dwells) and 15 s
polling. On cruise sections the 85th percentile lands within 5 km/h of the
limit on:

| What the feed carries | Within 5 km/h | Median error |
|---|---|---|
| Reported speed | 100% | 0.7 km/h |
| GPS positions only | 95% | 1.3 km/h |
| 250 m track sections, timestamped on entry | 93% | 1.2 km/h |
| 250 m track sections, timestamped at poll (worst case) | 84% | 0.7 km/h |

In the worst case the span speeds are noisier, so the 85th percentile can read
up to ~10 km/h high on short sections; use the median there. The first run's
summary (`status.json`) shows which mode the Metro feed ends up in.

## Setup

1. **Create a public GitHub repo** and push these files to `main`. It must be
   public: public repos get unlimited Actions minutes; a private one would use
   up its 2,000 free minutes in under two days.
2. **Add the API key**: repo Settings > Secrets and variables > Actions > New
   repository secret, name `TFNSW_API_KEY`. Get a key at
   opendata.transport.nsw.gov.au and make sure the application has the GTFS
   realtime and timetable APIs enabled.
3. **Site data source**: `site/config.js` points at this repo's `data`
   branch (`Joey-Hain/speed`). Change it only if the repo moves.
4. **First run**: Actions tab > Collect speeds > Run workflow, set minutes to
   `5`. When it finishes, the run summary lists every feed with polls, lines
   and samples. Any feed that failed shows its error there and in
   `status.json`; fix its path in `collector/feeds.py` if TfNSW has renamed it.
   After that the hourly schedule takes over.
5. **Cloudflare Pages**: Create project > Connect to Git > this repo.
   * Production branch: `main`
   * Build command: none. Build output directory: `site`
   * Settings > Builds > Build watch paths: include `site/*`, so data updates
     don't trigger rebuilds (the site fetches data live from GitHub).
   * Settings > Builds > Branch control: preview deployments **None** (or
     exclude `data`), otherwise every hourly data push builds a preview and
     you'll hit the 500 builds a month limit.
   * Custom domains > `speed.joeyhain.org`. Cloudflare adds the CNAME itself
     since joeyhain.org is already on Cloudflare.

## Running locally

```
pip install -r requirements.txt
cp .env.example .env            # add your key
python -m collector.collect --minutes 10 --store store --feeds sydneytrains,metro
python -m tests.test_synthetic  # offline test, no key needed
python -m tests.test_metro      # line-speed recovery on a simulated M1
python tools/fetch_shapes.py    # real route shapes for testing, writes shapes_bundle.zip (git-ignored)
```

To view the site against local data: `ln -s ../store site/data`, then
`python -m http.server -d site` and open http://localhost:8000/?data=data/
(`?data=<url>` overrides where the site reads from).

## Things to know

* **API quota.** The default plan allows 60,000 requests a day at 5 per
  second. The intervals in `feeds.py` come to about 38,000 a day. If you add
  feeds or shorten intervals, keep under the quota or ask TfNSW for a higher
  plan. On a quota error the run stops polling and still saves what it has.
* **Rebuilding a line** after a real route change: run the workflow with
  `rebuild` set to e.g. `buses:333`. That line's totals restart from zero.
* **Scheduled workflows pause after 60 days without repo activity.** The
  hourly data pushes normally count; if GitHub ever emails that it disabled
  the schedule, re-enable it in the Actions tab.
* **Estimated speeds are section averages.** Short slowdowns inside a track
  section are smoothed out, and a section a train starts from after a stop
  reads a little high, because the departure time is only known to within one
  poll (15 s).
* **Data licence.** TfNSW Open Data is CC BY 4.0; the site credits it in the
  footer.
