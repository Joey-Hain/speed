// AdSense slot below the map. Does nothing until SLOT is filled in, and only loads
// once a speed graph is on the page, so it never shows beside an empty screen.
(function () {
  var CLIENT = "ca-pub-2609523563751769";
  var SLOT = "";
  if (!SLOT) return;

  var chart = document.getElementById("chart");
  var box = document.getElementById("ad-bottom");
  if (!chart || !box) return;
  var loaded = false;

  function update() {
    var hasGraph = !!chart.querySelector("svg");
    box.hidden = !hasGraph;
    if (!hasGraph || loaded) return;
    loaded = true;
    var label = document.createElement("div");
    label.className = "ad-label";
    label.textContent = "Advertisements";
    var ins = document.createElement("ins");
    ins.className = "adsbygoogle";
    ins.style.display = "block";
    ins.setAttribute("data-ad-client", CLIENT);
    ins.setAttribute("data-ad-slot", SLOT);
    ins.setAttribute("data-ad-format", "auto");
    ins.setAttribute("data-full-width-responsive", "true");
    box.appendChild(label);
    box.appendChild(ins);
    var s = document.createElement("script");
    s.async = true;
    s.crossOrigin = "anonymous";
    s.src = "https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=" + CLIENT;
    document.head.appendChild(s);
    (window.adsbygoogle = window.adsbygoogle || []).push({});
  }

  new MutationObserver(update).observe(chart, { childList: true });
  update();
})();
