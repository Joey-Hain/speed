// Where the collector publishes data: the `data` branch of Joey-Hain/speed.
// raw.githubusercontent.com allows cross-origin reads, so the site can live on
// Cloudflare Pages and still read it directly.
window.SPEED_CONFIG = {
  dataBase: "https://raw.githubusercontent.com/Joey-Hain/speed/data/",
  repoUrl: "https://github.com/Joey-Hain/speed",
  minSamples: 3,            // fewer samples than this draws dashed (mean)
  minSamplesPercentile: 10, // same, for median and percentiles
};
