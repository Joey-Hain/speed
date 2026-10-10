(() => {
  "use strict";

  // ------------------------------------------------------------------ config
  const CFG = window.SPEED_CONFIG || {};
  const params = new URLSearchParams(location.search);
  let DATA = params.get("data") || CFG.dataBase || "data/";
  if (DATA.includes("/OWNER/")) DATA = "data/"; // not configured yet: local copy
  if (!DATA.endsWith("/")) DATA += "/";
  const MIN_N = CFG.minSamples || 3;                // for means
  const MIN_N_PCT = CFG.minSamplesPercentile || 10; // percentiles need more samples
  // TfNSW mode colours, used when a line has no route_color in GTFS
  const MODE_COLOUR = { metro: "#168388", train: "#F6891F", lightrail: "#EE343F", bus: "#00B5EF", ferry: "#5AB031" };

  const MODE_LABEL = { train: "Trains", metro: "Metro", lightrail: "Light rail", bus: "Buses", ferry: "Ferries" };
  const MODE_ORDER = ["metro", "train", "lightrail", "bus", "ferry"];
  const STAT_LABEL = { mean: "mean", p50: "median", p85: "85th percentile", p95: "95th percentile" };

  // map ramp built from the line's own colour: pale tint = slow, line colour, darker = fast
  const hexRgb = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
  const rgbHex = (c) => "#" + c.map((v) => Math.round(Math.max(0, Math.min(255, v))).toString(16).padStart(2, "0")).join("");
  const mix = (a, b, t) => a.map((v, i) => v + (b[i] - v) * t);
  function lineColour(g) {
    const c = (g.colour || "").replace("#", "");
    return /^[0-9a-f]{6}$/i.test(c) ? "#" + c.toUpperCase() : MODE_COLOUR[g.mode] || "#111111";
  }
  function rampFor(hex) {
    const c = hexRgb(hex), w = [255, 255, 255], k = [0, 0, 0];
    return [mix(c, w, 0.85), mix(c, w, 0.6), mix(c, w, 0.3), c, mix(c, k, 0.25), mix(c, k, 0.5)].map(rgbHex);
  }

  const $ = (id) => document.getElementById(id);
  const el = {
    mode: $("mode"), feed: $("feed"), line: $("line"), lines: $("lines"), dir: $("dir"),
    branch: $("branch"), day: $("day"), band: $("band"), stat: $("stat"), stops: $("stops"),
    chart: $("chart"), legend: $("legend"), updated: $("updated"), note: $("note"),
  };
  const state = { index: null, feed: null, line: null, geom: null, rows: null, cache: new Map() };

  // ------------------------------------------------------------------ data
  async function fetchData(path, kind) {
    const r = await fetch(DATA + path, { cache: "no-cache" });
    if (!r.ok) throw new Error(`${path}: ${r.status}`);
    return kind === "json" ? r.json() : r.text();
  }
  function parseHist(txt) {
    const h = [];
    if (txt) for (const part of txt.split(";")) {
      const [b, c] = part.split(":");
      h.push([+b, +c]);
    }
    return h;
  }
  function parseCSV(text) {
    const lines = text.trim().split(/\r?\n/);
    const head = lines.shift().split(",");
    const num = new Set(["branch", "seg", "n", "sum", "sumsq", "n_all", "sum_all", "sumsq_all"]);
    return lines.map((l) => {
      const v = l.split(",");
      const o = {};
      head.forEach((h, i) => (o[h] = num.has(h) ? +(v[i] || 0) : v[i] || ""));
      o.hist = parseHist(o.hist);
      o.hist_all = parseHist(o.hist_all);
      o.hasAll = o.n_all > 0;
      return o;
    });
  }

  // positions.csv: per segment, how many fixes came from track circuits
  function parsePositions(text) {
    const out = new Map();
    if (!text || !text.trim()) return out;
    const lines = text.trim().split(/\r?\n/);
    const head = lines.shift().split(",");
    const iD = head.indexOf("dir"), iB = head.indexOf("branch"), iS = head.indexOf("seg");
    const iF = head.indexOf("fixes"), iT = head.indexOf("tc");
    if (iT < 0) return out;
    for (const l of lines) {
      const v = l.split(",");
      out.set(`${v[iD]}|${v[iB]}|${v[iS]}`, [+v[iF], +v[iT]]);
    }
    return out;
  }
  // segments of a branch whose positions are mostly track-circuit based
  function tcSegments(dir, bi) {
    const tc = new Set();
    let seen = 0;
    if (!state.pos) return { tc, seen };
    for (const [k, [n, t]] of state.pos) {
      const [d, b, sgm] = k.split("|");
      if (d !== dir || +b !== bi || n < 3) continue;
      seen++;
      if (t / n >= 0.5) tc.add(+sgm);
    }
    return { tc, seen };
  }

  // great-circle metres
  function hav(a, b) {
    const R = 6371008.8, r = Math.PI / 180;
    const dLat = (b[1] - a[1]) * r, dLon = (b[0] - a[0]) * r;
    const s = Math.sin(dLat / 2) ** 2 + Math.cos(a[1] * r) * Math.cos(b[1] * r) * Math.sin(dLon / 2) ** 2;
    return 2 * R * Math.asin(Math.sqrt(s));
  }
  // cumulative distance, scaled to the collector's stored length so segment cuts line up
  function cumulative(lonlat, lengthM) {
    const c = [0];
    for (let i = 1; i < lonlat.length; i++) c.push(c[i - 1] + hav(lonlat[i - 1], lonlat[i]));
    const k = lengthM && c[c.length - 1] ? lengthM / c[c.length - 1] : 1;
    return c.map((x) => x * k);
  }
  function slice(lonlat, cum, a, b) {
    const at = (s) => {
      let i = 1;
      while (i < cum.length - 1 && cum[i] < s) i++;
      const t = cum[i] === cum[i - 1] ? 0 : (s - cum[i - 1]) / (cum[i] - cum[i - 1]);
      const p = lonlat[i - 1], q = lonlat[i];
      return [p[1] + (q[1] - p[1]) * t, p[0] + (q[0] - p[0]) * t];
    };
    const out = [at(a)];
    for (let i = 0; i < cum.length; i++) if (cum[i] > a && cum[i] < b) out.push([lonlat[i][1], lonlat[i][0]]);
    out.push(at(b));
    return out;
  }

  // ------------------------------------------------------------------ statistics
  function aggregate(dir, day, band, stops) {
    const out = new Map(); // branch -> Map(seg -> cell)
    for (const r of state.rows) {
      if (r.dir !== dir) continue;
      if (day !== "all" && r.day !== day) continue;
      if (band !== "all" && r.band !== band) continue;
      const useAll = stops && r.hasAll;
      const n = useAll ? r.n_all : r.n;
      if (!n) continue;
      if (!out.has(r.branch)) out.set(r.branch, new Map());
      const m = out.get(r.branch);
      const c = m.get(r.seg) || { n: 0, sum: 0, sumsq: 0, hist: new Map() };
      c.n += n;
      c.sum += useAll ? r.sum_all : r.sum;
      c.sumsq += useAll ? r.sumsq_all : r.sumsq;
      for (const [b, k] of useAll ? r.hist_all : r.hist) c.hist.set(b, (c.hist.get(b) || 0) + k);
      m.set(r.seg, c);
    }
    for (const m of out.values()) for (const c of m.values()) {
      c.mean = c.sum / c.n;
      c.sd = Math.sqrt(Math.max(0, c.sumsq / c.n - c.mean * c.mean));
      c.bins = [...c.hist.entries()].sort((a, b) => a[0] - b[0]);
      c.hn = c.bins.reduce((t, [, k]) => t + k, 0);
    }
    return out;
  }
  // percentile from the 5 km/h histogram, interpolating inside the bin
  function pct(c, q) {
    const HIST_KMH = state.geom.hist_kmh || 5;
    if (!c.hn) return c.mean;
    const target = q * c.hn;
    let acc = 0;
    for (const [b, k] of c.bins) {
      if (acc + k >= target) return (b + (target - acc) / k) * HIST_KMH;
      acc += k;
    }
    return (c.bins[c.bins.length - 1][0] + 1) * HIST_KMH;
  }
  function value(c, stat) {
    if (stat === "mean") return c.mean;
    return pct(c, { p50: 0.5, p85: 0.85, p95: 0.95 }[stat]);
  }
  function spread(c, stat) {
    // mean: plus/minus one sd; percentiles: the middle half (25th to 75th)
    return stat === "mean" || !c.hn
      ? [Math.max(0, c.mean - c.sd), c.mean + c.sd]
      : [pct(c, 0.25), pct(c, 0.75)];
  }
  const enough = (c, stat) => c.n >= (stat === "mean" ? MIN_N : MIN_N_PCT);

  function colourScale(vmax, ramp) {
    const stops = ramp.map(hexRgb);
    return (v) => {
      const t = Math.max(0, Math.min(1, v / vmax)) * (stops.length - 1);
      const i = Math.min(Math.floor(t), stops.length - 2), f = t - i;
      return `rgb(${stops[i].map((x, k) => Math.round(x + (stops[i + 1][k] - x) * f)).join(",")})`;
    };
  }
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  // ------------------------------------------------------------------ map
  let map, mapLayer;
  function initMap() {
    map = L.map("map", { zoomSnap: 0.25, scrollWheelZoom: false });
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19, attribution: "&copy; OpenStreetMap contributors",
    }).addTo(map);
    map.setView([-33.87, 151.2], 10);
    mapLayer = L.layerGroup().addTo(map);
    map.on("focus", () => map.scrollWheelZoom.enable());
    map.on("blur", () => map.scrollWheelZoom.disable());
  }

  // ------------------------------------------------------------------ chart
  function drawChart(branch, cells, stat, vmax, colour, tcSegs) {
    const host = el.chart;
    host.style.setProperty("--line", colour);
    host.innerHTML = "";
    const W = Math.max(320, host.clientWidth), H = W < 600 ? 300 : 380;
    const hasStations = (branch.stations || []).length > 0;
    const M = { t: hasStations ? 64 : 22, r: 12, b: 36, l: 44 };
    const L_km = branch.length_m / 1000;
    const segKm = state.geom.segment_m / 1000;
    const nSeg = Math.ceil(L_km / segKm);
    const x = (km) => M.l + (km / L_km) * (W - M.l - M.r);
    const y = (v) => H - M.b - (Math.min(v, vmax) / vmax) * (H - M.t - M.b);
    const NS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(NS, "svg");
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    const add = (parent, tag, attrs, text) => {
      const n = document.createElementNS(NS, tag);
      for (const k in attrs) n.setAttribute(k, attrs[k]);
      if (text != null) n.textContent = text;
      parent.appendChild(n);
      return n;
    };

    const grid = add(svg, "g", { class: "grid" });
    const axis = add(svg, "g", { class: "axis" });
    const yStep = vmax <= 50 ? 10 : vmax <= 150 ? 20 : 40;
    for (let v = 0; v <= vmax; v += yStep) {
      add(grid, "line", { x1: M.l, x2: W - M.r, y1: y(v), y2: y(v) });
      add(axis, "text", { x: M.l - 6, y: y(v) + 4, "text-anchor": "end" }, v);
    }
    const xTarget = Math.max(5, Math.floor((W - M.l - M.r) / 70));
    const xStep = [0.5, 1, 2, 5, 10, 20, 50, 100, 200].find((s) => L_km / s <= xTarget) || 500;
    for (let k = 0; k <= L_km + 1e-9; k += xStep) {
      add(axis, "text", { x: x(k), y: H - M.b + 18, "text-anchor": "middle" }, +k.toFixed(1));
    }
    add(axis, "text", { x: W - M.r, y: H - 4, "text-anchor": "end", class: "label" }, "km");
    add(axis, "text", { x: 2, y: hasStations ? M.t - 10 : 10, "text-anchor": "start", class: "label" }, "km/h");

    const stG = add(svg, "g", { class: "station" });
    let lastX = -1e9;
    for (const st of branch.stations || []) {
      const sx = x(st.s / 1000);
      add(stG, "line", { x1: sx, x2: sx, y1: M.t - 4, y2: H - M.b });
      if (sx - lastX >= 13) {
        add(stG, "text", { x: sx + 3, y: M.t - 8, transform: `rotate(-45 ${sx + 3} ${M.t - 8})` }, st.name);
        lastX = sx;
      }
    }

    const clipId = "clip-" + Math.random().toString(36).slice(2);
    add(add(add(svg, "defs", {}), "clipPath", { id: clipId }), "rect",
      { x: M.l, y: M.t, width: W - M.l - M.r, height: H - M.t - M.b });
    const plot = add(svg, "g", { "clip-path": `url(#${clipId})` });

    // a flat step per segment, so speed limits read as level runs
    const x0 = (s) => x(s * segKm), x1 = (s) => x(Math.min((s + 1) * segKm, L_km));
    // shade track-circuit sections when only part of the line is tracked that way
    if (tcSegs && tcSegs.size) {
      for (const sgm of tcSegs) {
        add(plot, "rect", { class: "tc", x: x0(sgm), y: M.t, width: Math.max(1, x1(sgm) - x0(sgm)), height: H - M.t - M.b });
      }
    }
    const segs = [...cells.keys()].sort((a, b) => a - b);
    const runs = [];
    let cur = [];
    for (const s of segs) {
      if (cur.length && s !== cur[cur.length - 1] + 1) { runs.push(cur); cur = []; }
      cur.push(s);
    }
    if (cur.length) runs.push(cur);

    for (const run of runs) {
      const top = [], bot = [];
      for (const s of run) {
        const [lo, hi] = spread(cells.get(s), stat);
        top.push(`${x0(s)},${y(hi)}`, `${x1(s)},${y(hi)}`);
        bot.unshift(`${x1(s)},${y(lo)}`, `${x0(s)},${y(lo)}`);
      }
      add(plot, "path", { class: "iqr", d: "M" + top.concat(bot).join("L") + "Z" });
    }
    for (const run of runs) {
      run.forEach((s, i) => {
        const c = cells.get(s), v = value(c, stat);
        const ok = enough(c, stat);
        add(plot, "line", { class: ok ? "stat" : "stat-thin", x1: x0(s), x2: x1(s), y1: y(v), y2: y(v) });
        if (i > 0) {
          const p = cells.get(run[i - 1]);
          add(plot, "line", { class: ok && enough(p, stat) ? "stat" : "stat-thin",
            x1: x0(s), x2: x0(s), y1: y(value(p, stat)), y2: y(v) });
        }
      });
    }

    // hover: highlight one segment
    const cross = add(svg, "g", { class: "cross", visibility: "hidden" });
    const colRect = add(cross, "rect", { y: M.t, height: H - M.t - M.b });
    const hit = add(svg, "rect", { x: M.l, y: M.t, width: W - M.l - M.r, height: H - M.t - M.b, fill: "transparent" });
    const tip = document.createElement("div");
    tip.className = "tip";
    tip.hidden = true;
    host.appendChild(svg);
    host.appendChild(tip);

    const near = (km) => {
      let best = null;
      for (const st of branch.stations || []) {
        const d = Math.abs(st.s / 1000 - km);
        if (!best || d < best.d) best = { d, name: st.name };
      }
      return best && best.d < 1 ? best.name : null;
    };
    const move = (ev) => {
      const rect = svg.getBoundingClientRect();
      const px = ((ev.clientX - rect.left) / rect.width) * W;
      const km = Math.max(0, Math.min(L_km - 1e-9, ((px - M.l) / (W - M.l - M.r)) * L_km));
      const s = Math.min(Math.floor(km / segKm), nSeg - 1);
      const c = cells.get(s);
      cross.setAttribute("visibility", "visible");
      colRect.setAttribute("x", x0(s));
      colRect.setAttribute("width", Math.max(2, x1(s) - x0(s)));
      const a = (s * segKm).toFixed(2), b = Math.min((s + 1) * segKm, L_km).toFixed(2);
      const st = near(km);
      let html = `<span class="muted">${a} to ${b} km${st ? ", " + escapeHtml(st) : ""}</span>`;
      if (c) {
        const [lo, hi] = spread(c, stat);
        html = `<b>${Math.round(value(c, stat))} km/h</b> ${STAT_LABEL[stat]}<br>` +
          `<span class="muted">${stat === "mean" ? "sd" : "middle half"} ${Math.round(lo)} to ${Math.round(hi)}, ` +
          `${c.n.toLocaleString()} samples</span><br>` + html;
      } else html = "<b>No data</b><br>" + html;
      tip.innerHTML = html;
      tip.hidden = false;
      const hr = host.getBoundingClientRect();
      tip.style.left = Math.max(0, Math.min(ev.clientX - hr.left + 12, hr.width - tip.offsetWidth - 4)) + "px";
      tip.style.top = Math.max(0, ev.clientY - hr.top - tip.offsetHeight - 12) + "px";
    };
    hit.addEventListener("pointermove", move);
    hit.addEventListener("pointerdown", move);
    hit.addEventListener("pointerleave", () => { cross.setAttribute("visibility", "hidden"); tip.hidden = true; });
  }

  // ------------------------------------------------------------------ render
  function branchLabel(b, i) {
    const st = b.stations || [];
    const km = (b.length_m / 1000).toFixed(1) + " km";
    const name = i === 0 ? "Main" : "Branch " + i;
    return st.length >= 2 ? `${name}: ${st[0].name} to ${st[st.length - 1].name} (${km})` : `${name} (${km})`;
  }

  function render() {
    const g = state.geom;
    if (!g || !state.rows) return;
    const dir = el.dir.value, stat = el.stat.value;
    const d = g.directions[dir];
    const bi = +el.branch.value || 0;
    const branch = d.branches[bi];
    const agg = aggregate(dir, el.day.value, el.band.value, el.stops.checked);
    const cells = agg.get(bi) || new Map();

    // one scale for chart and map
    // scale runs from 0 to the fastest segment shown + 10 km/h, so nothing clips
    const all = [...agg.values()].flatMap((m) => [...m.values()]);
    const vmax = Math.max(20, Math.ceil(Math.max(0, ...all.map((c) => value(c, stat))) + 10));
    const lc = lineColour(g);
    const ramp = rampFor(lc);
    const colour = colourScale(vmax, ramp);

    const { tc, seen } = tcSegments(dir, bi);
    const tcShare = seen ? tc.size / seen : 0;
    const partial = tcShare > 0.05 && tcShare < 0.9;
    if (tcShare >= 0.9) {
      el.note.textContent = "Tracked by Sydney Trains track circuits: speeds are estimated from movement between track sections, not measured on the train.";
      el.note.hidden = false;
    } else if (partial) {
      el.note.textContent = "Shaded sections are tracked by Sydney Trains track circuits: speeds there are estimated from movement between track sections, not measured on the train.";
      el.note.hidden = false;
    } else {
      el.note.hidden = true;
    }
    if (cells.size) drawChart(branch, cells, stat, vmax, lc, partial ? tc : null);
    else el.chart.innerHTML = `<div class="empty-chart">No data yet for this selection</div>`;

    mapLayer.clearLayers();
    const bounds = [];
    const segM = g.segment_m;
    d.branches.forEach((b, i) => {
      const cum = cumulative(b.lonlat, b.length_m);
      const m = agg.get(i) || new Map();
      for (let s = 0; s < b.draw.length; s++) {
        if (b.draw[s] !== "1") continue;
        const a = s * segM, z = Math.min((s + 1) * segM, b.length_m);
        const pts = slice(b.lonlat, cum, a, z);
        bounds.push(...pts);
        const c = m.get(s);
        const ok = c && enough(c, stat);
        const style = ok
          ? { color: colour(value(c, stat)), weight: i === bi ? 7 : 5, opacity: 1, lineCap: "round", lineJoin: "round" }
          : { color: "#aaa", weight: 3, dashArray: "4 5", opacity: 0.9 };
        const tipTxt = `${(a / 1000).toFixed(2)} to ${(z / 1000).toFixed(2)} km: ` +
          (c ? `${Math.round(value(c, stat))} km/h ${STAT_LABEL[stat]} (${c.n} samples)` : "no data");
        L.polyline(pts, style).bindTooltip(tipTxt, { sticky: true }).addTo(mapLayer);
      }
    });
    for (const st of branch.stations || []) {
      L.circleMarker([st.lat, st.lon], { radius: 4, color: "#111", weight: 1.5, fillColor: "#fff", fillOpacity: 1 })
        .bindTooltip(`${st.name} (${(st.s / 1000).toFixed(1)} km)`).addTo(mapLayer);
    }
    if (bounds.length) map.fitBounds(bounds, { padding: [20, 20] });

    el.legend.innerHTML = `<span>0</span><span class="bar" style="background:linear-gradient(90deg,${ramp.join(",")})"></span>` +
      `<span>${vmax} km/h</span><span class="nodata"></span><span>too few samples</span>`;
    syncUrl();
  }

  // ------------------------------------------------------------------ selection
  // Lines are grouped for the Network menu. Most modes group by feed; trains
  // are regrouped into Sydney Trains / Intercity / Regional across both train
  // feeds, and NSW TrainLink coaches move under buses.
  const INTERCITY = new Set(["BMT", "CCN", "HUN", "SCO", "SHL"]);
  const TRAIN_GROUPS = { sydney: "Sydney Trains", intercity: "Intercity trains", regional: "Regional trains" };
  const TRAIN_FEEDS = new Set(["sydneytrains", "nswtrains"]);

  function classify(feedKey, f, line, l) {
    if (TRAIN_FEEDS.has(feedKey)) {
      const net = l.network || (INTERCITY.has(line) ? "intercity" : /^T\d/.test(line) ? "sydney" : "regional");
      if (net === "coach") return ["bus", "train-coach", "NSW TrainLink coaches"];
      return ["train", "train-" + net, TRAIN_GROUPS[net] || TRAIN_GROUPS.regional];
    }
    return [f.mode, feedKey, f.name];
  }

  function buildCatalog() {
    const cat = [];
    for (const [feedKey, f] of Object.entries(state.index.feeds)) {
      for (const [line, l] of Object.entries(f.lines || {})) {
        const [mode, group, groupLabel] = classify(feedKey, f, line, l);
        cat.push({ mode, group, groupLabel, feed: feedKey, line, name: l.name || "", samples: l.samples || 0 });
      }
    }
    // the same line code can come from two feeds (HUN, SHL): label those apart
    const seen = new Map();
    for (const e of cat) {
      const k = e.group + "|" + e.line;
      seen.set(k, (seen.get(k) || 0) + 1);
    }
    for (const e of cat) {
      const dup = seen.get(e.group + "|" + e.line) > 1;
      e.key = dup ? `${e.line} (${state.index.feeds[e.feed].name})` : e.line;
    }
    state.catalog = cat;
  }

  function groupsForMode(mode) {
    const groups = new Map();
    for (const e of state.catalog) if (e.mode === mode && !groups.has(e.group)) groups.set(e.group, e.groupLabel);
    const order = ["train-sydney", "train-intercity", "train-regional"];
    return [...groups.entries()].sort((a, b) => {
      const ia = order.indexOf(a[0]), ib = order.indexOf(b[0]);
      if (ia >= 0 || ib >= 0) return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
      return a[1].localeCompare(b[1]);
    });
  }
  function fillModes() {
    el.mode.innerHTML = MODE_ORDER.filter((m) => groupsForMode(m).length)
      .map((m) => `<option value="${m}">${MODE_LABEL[m]}</option>`).join("");
  }
  function fillFeeds() {
    el.feed.innerHTML = groupsForMode(el.mode.value)
      .map(([k, label]) => `<option value="${k}">${escapeHtml(label)}</option>`).join("");
  }
  function entriesOfGroup() {
    return state.catalog.filter((e) => e.group === el.feed.value)
      .sort((a, b) => a.key.localeCompare(b.key, "en", { numeric: true }));
  }
  function fillLines() {
    el.lines.innerHTML = entriesOfGroup()
      .map((e) => `<option value="${escapeHtml(e.key)}">${escapeHtml(e.name)}</option>`).join("");
  }
  function firstLine() {
    const ls = entriesOfGroup().sort((a, b) => b.samples - a.samples);
    return ls.length ? ls[0] : null;
  }
  const entryByKey = (key) => entriesOfGroup().find((e) => e.key === key);

  async function selectLine(entry) {
    if (!entry) return;
    const { feed, line: key } = entry;
    el.line.value = entry.key;
    state.feed = feed; state.line = key;
    const ck = feed + "/" + key;
    try {
      if (!state.cache.has(ck)) {
        const base = `${feed}/${encodeURIComponent(key)}/`;
        const [geom, csv, pos] = await Promise.all([
          fetchData(base + "geometry.json", "json"), fetchData(base + "speeds.csv"),
          fetchData(base + "positions.csv").catch(() => ""),
        ]);
        state.cache.set(ck, { geom, rows: parseCSV(csv), pos: parsePositions(pos) });
      }
    } catch (e) {
      el.chart.innerHTML = `<div class="empty-chart">Couldn't load ${escapeHtml(key)}</div>`;
      return;
    }
    if (state.feed !== feed || state.line !== key) return;
    const { geom, rows, pos } = state.cache.get(ck);
    state.geom = geom; state.rows = rows; state.pos = pos;
    el.dir.innerHTML = Object.entries(geom.directions)
      .map(([d, v]) => `<option value="${d}">to ${escapeHtml(v.headsign || "direction " + d)}</option>`).join("");
    const want = params.get("d");
    if (want && geom.directions[want]) { el.dir.value = want; params.delete("d"); }
    fillBranches();
    render();
  }
  function fillBranches() {
    const d = state.geom.directions[el.dir.value];
    el.branch.innerHTML = d.branches.map((b, i) => `<option value="${i}">${escapeHtml(branchLabel(b, i))}</option>`).join("");
    el.branch.disabled = d.branches.length < 2;
    const want = params.get("b");
    if (want && d.branches[+want]) { el.branch.value = want; params.delete("b"); }
  }
  function syncUrl() {
    const p = new URLSearchParams();
    if (params.get("data")) p.set("data", params.get("data"));
    p.set("f", state.feed); p.set("l", state.line); p.set("d", el.dir.value);
    if (+el.branch.value) p.set("b", el.branch.value);
    if (el.day.value !== "all") p.set("day", el.day.value);
    if (el.band.value !== "all") p.set("band", el.band.value);
    if (el.stat.value !== "p85") p.set("stat", el.stat.value);
    if (el.stops.checked) p.set("stops", "1");
    history.replaceState(null, "", "?" + p.toString());
  }

  // ------------------------------------------------------------------ boot
  async function boot() {
    initMap();
    try {
      state.index = await fetchData("index.json", "json");
    } catch (e) {
      el.chart.innerHTML = `<div class="empty-chart">No data published yet</div>`;
      return;
    }
    if (state.index.updated) {
      el.updated.textContent = " | Updated " + new Date(state.index.updated).toLocaleString("en-AU", {
        timeZone: "Australia/Sydney", hour12: false, day: "numeric", month: "short",
        year: "numeric", hour: "2-digit", minute: "2-digit" });
    }
    buildCatalog();
    fillModes();
    const start = state.catalog.find((e) => e.feed === params.get("f") && e.line === params.get("l"));
    if (start) el.mode.value = start.mode;
    fillFeeds();
    if (start) el.feed.value = start.group;
    fillLines();
    for (const k of ["day", "band", "stat"]) if (params.get(k)) el[k].value = params.get(k);
    el.stops.checked = params.get("stops") === "1";

    el.mode.addEventListener("change", () => { fillFeeds(); fillLines(); selectLine(firstLine()); });
    el.feed.addEventListener("change", () => { fillLines(); selectLine(firstLine()); });
    el.line.addEventListener("change", () => selectLine(entryByKey(el.line.value.trim())));
    el.line.addEventListener("input", () => {
      const e = entryByKey(el.line.value.trim());
      if (e) selectLine(e);
    });
    el.dir.addEventListener("change", () => { fillBranches(); render(); });
    for (const e of [el.branch, el.day, el.band, el.stat, el.stops]) e.addEventListener("change", render);
    let rt;
    addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(render, 150); });

    selectLine(start || firstLine());
  }

  boot();
})();
