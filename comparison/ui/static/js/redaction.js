/* Full-volume redaction comparison: three synced panes per pdf page —
   unredacted processing copy · bl-warm redaction · legacy 3-model
   redaction. Volume dropdown, page arrows/input/slider, a jump to the
   next golden (hand-annotated) page, and an optional overlay of each
   model's redaction-class detections on its own redacted pane
   ("redaction classes" button / b). Prefs in localStorage. */

(function () {
  "use strict";

  var DATA = JSON.parse(document.getElementById("data").textContent);
  var SVG_NS = "http://www.w3.org/2000/svg";
  var STORE_KEY = "blwarm_comparison_redaction_v5";
  var VOLS = Object.keys(DATA.volumes);
  var PAGE_W = 1200;
  var BOX_VERSIONS = ["new", "legacy"];

  var state = {vol: VOLS[0], page: 0, zoomVal: 0.45, showBoxes: false};
  try {
    var saved = JSON.parse(localStorage.getItem(STORE_KEY));
    if (saved) state = Object.assign(state, saved);
  } catch (e) { /* defaults */ }
  if (VOLS.indexOf(state.vol) === -1) state.vol = VOLS[0];

  var zoom = state.zoomVal;

  var volSel = document.getElementById("vol-select");
  var pageInput = document.getElementById("page-input");
  var pageSlider = document.getElementById("page-slider");
  var pageTotal = document.getElementById("page-total");
  var goldenMark = document.getElementById("golden-mark");
  var zoomLevel = document.getElementById("zoom-level");
  var boxesBtn = document.getElementById("boxes-btn");

  function el(id) { return document.getElementById(id); }

  function persist() {
    try {
      state.zoomVal = zoom;
      localStorage.setItem(STORE_KEY, JSON.stringify(state));
    } catch (e) { /* non-fatal */ }
  }

  function volInfo() { return DATA.volumes[state.vol]; }

  var boxCache = {};

  function loadBoxes() {
    var vol = state.vol;
    if (boxCache[vol]) { drawOverlays(); return; }
    boxCache[vol] = {};
    BOX_VERSIONS.forEach(function (v) {
      fetch("/redaction_boxes/" + encodeURIComponent(vol) + "/" + v)
        .then(function (r) { return r.ok ? r.json() : {}; })
        .then(function (d) {
          boxCache[vol][v] = d;
          drawOverlays();
        })
        .catch(function () { boxCache[vol][v] = {}; });
    });
  }

  function drawOverlay(v) {
    var svg = el("svg-" + v);
    if (!svg) return;
    svg.textContent = "";
    if (!state.showBoxes) return;
    var vb = boxCache[state.vol];
    if (!vb || !vb[v]) return;
    var img = el("img-" + v);
    var w = img.naturalWidth || PAGE_W;
    var h = img.naturalHeight || Math.round(PAGE_W * 11 / 8.5);
    svg.setAttribute("viewBox", "0 0 " + w + " " + h);
    (vb[v][String(state.page)] || []).forEach(function (b) {
      // b = [label, conf, x, y, w, h] in the 1200w render space
      var rect = document.createElementNS(SVG_NS, "rect");
      rect.setAttribute("x", b[2]);
      rect.setAttribute("y", b[3]);
      rect.setAttribute("width", b[4]);
      rect.setAttribute("height", b[5]);
      rect.setAttribute("fill", "none");
      rect.setAttribute("stroke", DATA.label_colors[b[0]] || "#888");
      rect.setAttribute("stroke-width", 2.2 / zoom);
      var title = document.createElementNS(SVG_NS, "title");
      title.textContent = b[0] + " · " + b[1];
      rect.appendChild(title);
      svg.appendChild(rect);

      var color = DATA.label_colors[b[0]] || "#888";
      var fh = 11 / zoom;
      var tag = document.createElementNS(SVG_NS, "text");
      tag.setAttribute("x", b[2] + 3 / zoom);
      tag.setAttribute("y", Math.max(fh, b[3] - 3 / zoom));
      tag.setAttribute("paint-order", "stroke");
      tag.setAttribute("stroke", "#fff");
      tag.setAttribute("stroke-width", 3 / zoom);
      tag.style.font = "600 " + fh + "px ui-sans-serif, system-ui," +
        " sans-serif";
      tag.style.fill = color;
      tag.style.pointerEvents = "none";
      tag.textContent = b[0].toLowerCase() + " " +
        Number(b[1]).toFixed(2);
      svg.appendChild(tag);
    });
  }

  function drawOverlays() { BOX_VERSIONS.forEach(drawOverlay); }

  function applyBoxesBtn() {
    boxesBtn.classList.toggle("seg-on", state.showBoxes);
  }

  function render() {
    var n = volInfo().pages;
    state.page = Math.max(0, Math.min(n - 1, state.page));
    var name = "p" + String(state.page).padStart(4, "0") + ".jpg";
    DATA.versions.forEach(function (v) {
      var img = el("img-" + v);
      img.src = "/redaction_pages/" + encodeURIComponent(state.vol) +
        "/" + v + "/" + name;
      img.style.width = (PAGE_W * zoom) + "px";
    });
    pageInput.value = state.page + 1;
    pageInput.max = n;
    pageSlider.max = n;
    pageSlider.value = state.page + 1;
    pageTotal.textContent = "/ " + n;
    var g = volInfo().golden_pages[String(state.page)];
    goldenMark.textContent = g ? "✦ golden: " + g : "";
    zoomLevel.textContent = Math.round(zoom * 100) + "%";
    if (state.showBoxes) loadBoxes(); else drawOverlays();
    persist();
  }

  function setPage(p) {
    state.page = p;
    render();
  }

  volSel.value = state.vol;
  volSel.addEventListener("change", function () {
    state.vol = volSel.value;
    state.page = 0;
    render();
  });
  pageInput.addEventListener("change", function () {
    setPage((parseInt(pageInput.value, 10) || 1) - 1);
  });
  pageSlider.addEventListener("input", function () {
    setPage(parseInt(pageSlider.value, 10) - 1);
  });
  el("page-prev").addEventListener("click", function () {
    setPage(state.page - 1);
  });
  el("page-next").addEventListener("click", function () {
    setPage(state.page + 1);
  });
  el("golden-next").addEventListener("click", nextGolden);
  boxesBtn.addEventListener("click", toggleBoxes);

  function toggleBoxes() {
    state.showBoxes = !state.showBoxes;
    applyBoxesBtn();
    if (state.showBoxes) loadBoxes(); else drawOverlays();
    persist();
  }

  BOX_VERSIONS.forEach(function (v) {
    var img = el("img-" + v);
    if (img) {
      img.addEventListener("load", function () { drawOverlay(v); });
    }
  });

  function nextGolden() {
    var pages = Object.keys(volInfo().golden_pages)
      .map(Number).sort(function (a, b) { return a - b; });
    var nxt = pages.find(function (p) { return p > state.page; });
    setPage(nxt !== undefined ? nxt : (pages[0] || state.page));
  }

  function setZoom(z) {
    zoom = Math.max(0.1, Math.min(2, z));
    render();
  }
  el("zoom-in").addEventListener("click",
    function () { setZoom(zoom * 1.25); });
  el("zoom-out").addEventListener("click",
    function () { setZoom(zoom / 1.25); });
  el("zoom-fit").addEventListener("click", function () {
    var stage = document.querySelector(".e2e-stage");
    setZoom((stage.clientWidth - 24) / PAGE_W);
  });

  var syncing = false;
  document.querySelectorAll(".e2e-stage").forEach(function (s) {
    s.addEventListener("scroll", function () {
      if (syncing) return;
      syncing = true;
      document.querySelectorAll(".e2e-stage").forEach(function (o) {
        if (o !== s) {
          o.scrollTop = s.scrollTop;
          o.scrollLeft = s.scrollLeft;
        }
      });
      syncing = false;
    });
  });

  document.addEventListener("keydown", function (ev) {
    if (ev.target.tagName === "INPUT" || ev.target.tagName === "SELECT") {
      return;
    }
    if (ev.key === "[") setPage(state.page - 1);
    else if (ev.key === "]") setPage(state.page + 1);
    else if (ev.key === "}") nextGolden();
    else if (ev.key === "b") toggleBoxes();
    else if (ev.key === "+" || ev.key === "=") setZoom(zoom * 1.25);
    else if (ev.key === "-") setZoom(zoom / 1.25);
  });

  applyBoxesBtn();
  render();
})();
