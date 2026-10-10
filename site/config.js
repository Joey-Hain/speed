// Where the collector publishes data: the `data` branch of Joey-Hain/speed.
// raw.githubusercontent.com allows cross-origin reads, so the site can live on
// Cloudflare Pages and still read it directly.
window.SPEED_CONFIG = {
  dataBase: "https://raw.githubusercontent.com/Joey-Hain/speed/data/",
  repoUrl: "https://github.com/Joey-Hain/speed",
  minSamples: 3,            // fewer samples than this draws dashed (mean)
  minSamplesPercentile: 10, // same, for median and percentiles

  // Where Sydney Trains-feed positions come from, from the Sydney end:
  // [source, up to station]; the last entry runs to the end of the line.
  //   tc     = track circuits (signalling)
  //   beacon = MetroNet radio beacons at stations (outside the track-circuit
  //            reporting area): average speed between stations
  //   gps    = NSW TrainLink's GPS copy of the train
  // Unlisted Sydney Trains lines are track circuit throughout; other feeds
  // (Metro, light rail, buses, regional trains) are GPS.
  positionSource: {
    BMT: [["tc", "Bell"], ["beacon", "Lithgow"], ["gps"]],
    CCN: [["tc", "Wyee"], ["beacon"]],
    SCO: [["tc", "Kiama"], ["gps"]],
    SHL: [["gps"]],
    HUN: [["gps"]],
  },
};
