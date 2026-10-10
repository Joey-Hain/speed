// Where the collector publishes data: the `data` branch of Joey-Hain/speed.
// raw.githubusercontent.com allows cross-origin reads, so the site can live on
// Cloudflare Pages and still read it directly.
window.SPEED_CONFIG = {
  dataBase: "https://raw.githubusercontent.com/Joey-Hain/speed/data/",
  repoUrl: "https://github.com/Joey-Hain/speed",
  minSamples: 3,            // fewer samples than this draws dashed (mean)
  minSamplesPercentile: 10, // same, for median and percentiles

  // Where speeds come from Sydney Trains track circuits rather than GPS.
  // Every Sydney Trains-feed line is track circuit end to end unless listed:
  //   "Station" = track circuit from the Sydney end up to that station, GPS beyond
  //   "none"    = all GPS (NSW TrainLink's copy of the train is used)
  // Other feeds (Metro, light rail, buses, NSW TrainLink regional) are all GPS.
  trackCircuit: {
    BMT: "Lithgow",   // Bathurst Bullet GPS beyond
    SCO: "Kiama",     // Kiama-Bomaderry diesel shuttle GPS
    CCN: "Wyee",      // no track-circuit positions north of here
    SHL: "none",
    HUN: "none",
  },
};
