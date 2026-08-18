/* Classes comparison: three synced panes over the SAME golden page,
   each overlaying one source's boxes — ground truth, bl-warm, or the
   legacy ensemble (per-pane dropdown). Class = color; GT boxes draw
   thicker/translucent; class+conf tags beside each box (toggleable,
   "labels" button / l key). Notes persist server-side
   (data/notes.json); UI prefs in localStorage. */

(function () {
  "use strict";

  var DATA = JSON.parse(document.getElementById("data").textContent);
  var SVG_NS = "http://www.w3.org/2000/svg";
  var STORE_KEY = "blwarm_comparison_classes_v2";
  var PANES = ["a", "b", "c"];

  var COLORS = {};
  var CONTAINER = {};
  DATA.taxonomy.forEach(function (t) {
    COLORS[t.name] = t.color;
    if (t.container) CONTAINER[t.name] = true;
  });
  var NOTES = DATA.notes || {};

  var state = {
    reporter: "", flagFilter: "", hiddenCls: {}, conf: 0.2,
    zoomVal: 0.3, selected: null, showBoxes: true, showTags: true,
    showRedaction: true, showContainers: true,
    paneSrc: {a: "gt", b: "bl_warm_raw", c: "ensemble"}
  };
  try {
    var saved = JSON.parse(localStorage.getItem(STORE_KEY));
    if (saved) state = Object.assign(state, saved);
  } catch (e) { /* defaults */ }
  PANES.forEach(function (p) {
    if (DATA.sources.indexOf(state.paneSrc[p]) === -1) {
      state.paneSrc = {a: "gt", b: "bl_warm_raw", c: "ensemble"};
    }
  });

  var zoom = state.zoomVal;
  var current = null;

  var rail = document.getElementById("rail");
  var railCount = document.getElementById("rail-count");
  var fReporter = document.getElementById("f-reporter");
  var fFlag = document.getElementById("f-flag");
  var confSlider = document.getElementById("conf-slider");
  var confVal = document.getElementById("conf-val");
  var noteInput = document.getElementById("page-note");
  var flagInput = document.getElementById("page-flag");
  var noteState = document.getElementById("note-state");
  var zoomLevel = document.getElementById("zoom-level");
  var boxesBtn = document.getElementById("boxes-btn");
  var tagsBtn = document.getElementById("tags-btn");
  var redactionBtn = document.getElementById("redaction-btn");
  var containerBtn = document.getElementById("container-btn");

  function el(id) { return document.getElementById(id); }

  function persist() {
    try {
      state.zoomVal = zoom;
      localStorage.setItem(STORE_KEY, JSON.stringify(state));
    } catch (e) { /* non-fatal */ }
  }

  function visible() {
    return DATA.sample.filter(function (p) {
      if (state.reporter && p.split !== state.reporter) return false;
      var n = NOTES[p.stem];
      if (state.flagFilter === "flagged" && !(n && n.flag)) return false;
      if (state.flagFilter === "noted" && !(n && n.note)) return false;
      return true;
    });
  }

  function renderRail() {
    var items = visible();
    rail.textContent = "";
    items.forEach(function (p) {
      var li = document.createElement("li");
      li.dataset.stem = p.stem;
      if (current && current.stem === p.stem) li.className = "active";
      var stem = document.createElement("span");
      stem.className = "stem";
      stem.textContent = p.stem;
      li.appendChild(stem);
      var n = NOTES[p.stem];
      if (n && (n.flag || n.note)) {
        var mark = document.createElement("span");
        mark.className = "nb";
        mark.textContent = n.flag ? "⚑" : "✎";
        li.appendChild(mark);
      }
      li.addEventListener("click", function () { select(p.stem); });
      rail.appendChild(li);
    });
    railCount.textContent = items.length + " of " +
      DATA.sample.length + " pages";
  }

  function drawBoxes(svg, src) {
    svg.textContent = "";
    if (!src || !state.showBoxes || !current) return;
    svg.setAttribute("viewBox",
      "0 0 " + current.width + " " + current.height);
    var golden = src.kind === "golden";
    (src.boxes || []).forEach(function (b) {
      if (!golden && b.conf < state.conf) return;
      if (state.hiddenCls[b.cls]) return;
      if (CONTAINER[b.cls] ? !state.showContainers
                           : !state.showRedaction) return;
      var color = COLORS[b.cls] || "#888";
      var rect = document.createElementNS(SVG_NS, "rect");
      rect.setAttribute("x", b.x);
      rect.setAttribute("y", b.y);
      rect.setAttribute("width", b.w);
      rect.setAttribute("height", b.h);
      rect.setAttribute("fill", "none");
      rect.setAttribute("stroke", color);
      rect.setAttribute("stroke-width", (golden ? 4 : 2.2) / zoom);
      if (golden) rect.setAttribute("stroke-opacity", "0.55");
      if (CONTAINER[b.cls]) {
        rect.setAttribute("stroke-dasharray",
          (10 / zoom) + " " + (6 / zoom));
      }
      var title = document.createElementNS(SVG_NS, "title");
      title.textContent = b.cls + (golden ? "" : " · " + b.conf);
      rect.appendChild(title);
      svg.appendChild(rect);

      if (!state.showTags) return;
      var fh = 11 / zoom;
      var tag = document.createElementNS(SVG_NS, "text");
      tag.setAttribute("x", b.x + 3 / zoom);
      tag.setAttribute("y", Math.max(fh, b.y - 3 / zoom));
      tag.setAttribute("paint-order", "stroke");
      tag.setAttribute("stroke", "#fff");
      tag.setAttribute("stroke-width", 3 / zoom);
      tag.style.font = "600 " + fh + "px ui-sans-serif, system-ui," +
        " sans-serif";
      tag.style.fill = color;
      tag.style.pointerEvents = "none";
      tag.textContent = b.cls +
        (golden ? "" : " " + Number(b.conf).toFixed(2));
      svg.appendChild(tag);
    });
  }

  function renderPane(p) {
    if (!current) return;
    var img = el("img-" + p);
    img.src = "/classes_pages/" + encodeURIComponent(current.file);
    img.style.width = (current.width * zoom) + "px";
    img.style.height = (current.height * zoom) + "px";
    drawBoxes(el("svg-" + p), current.sources[state.paneSrc[p]]);
  }

  function renderPanes() { PANES.forEach(renderPane); }

  function select(stem) {
    var p = null;
    for (var i = 0; i < DATA.sample.length; i++) {
      if (DATA.sample[i].stem === stem) { p = DATA.sample[i]; break; }
    }
    if (!p) return;
    current = p;
    state.selected = stem;
    persist();
    el("stage-title").textContent = p.stem;
    el("stage-meta").textContent = p.split;
    var n = NOTES[stem] || {};
    noteInput.value = n.note || "";
    flagInput.checked = !!n.flag;
    noteState.textContent = "";
    var lis = rail.querySelectorAll("li");
    for (var j = 0; j < lis.length; j++) {
      lis[j].className = lis[j].dataset.stem === stem ? "active" : "";
    }
    renderPanes();
  }

  function fitZoom() {
    if (!current) return;
    var stage = document.querySelector(".e2e-stage");
    zoom = Math.min(2, (stage.clientWidth - 24) / current.width);
  }

  function setZoom(z) {
    zoom = Math.max(0.1, Math.min(3, z));
    persist();
    zoomLevel.textContent = Math.round(zoom * 100) + "%";
    renderPanes();
  }

  var noteTimer = null;
  function saveNote() {
    var stem = current && current.stem;
    if (!stem) return;
    var payload = {note: noteInput.value, flag: flagInput.checked};
    NOTES[stem] = payload;
    noteState.textContent = "saving…";
    fetch("/api/note/" + encodeURIComponent(stem), {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload)
    }).then(function (r) {
      noteState.textContent = r.ok ? "saved ✓" : "save failed";
      renderRail();
    }).catch(function () {
      noteState.textContent = "save failed (network)";
    });
  }
  noteInput.addEventListener("input", function () {
    noteState.textContent = "…";
    clearTimeout(noteTimer);
    noteTimer = setTimeout(saveNote, 700);
  });
  flagInput.addEventListener("change", saveNote);

  document.querySelectorAll("#class-chips .chip").forEach(function (chip) {
    var cls = chip.dataset.cls;
    chip.setAttribute("aria-pressed",
      state.hiddenCls[cls] ? "false" : "true");
    chip.addEventListener("click", function (ev) {
      if (ev.altKey) {
        var isSolo = !state.hiddenCls[cls] &&
          Object.keys(state.hiddenCls).length ===
          DATA.taxonomy.length - 1;
        state.hiddenCls = {};
        if (!isSolo) {
          DATA.taxonomy.forEach(function (t) {
            if (t.name !== cls) state.hiddenCls[t.name] = true;
          });
        }
      } else if (state.hiddenCls[cls]) {
        delete state.hiddenCls[cls];
      } else {
        state.hiddenCls[cls] = true;
      }
      document.querySelectorAll("#class-chips .chip")
        .forEach(function (c) {
          c.setAttribute("aria-pressed",
            state.hiddenCls[c.dataset.cls] ? "false" : "true");
        });
      persist();
      renderPanes();
    });
  });

  confSlider.value = state.conf;
  confVal.textContent = Number(state.conf).toFixed(2);
  confSlider.addEventListener("input", function () {
    state.conf = parseFloat(confSlider.value);
    confVal.textContent = state.conf.toFixed(2);
    persist();
    renderPanes();
  });

  fReporter.value = state.reporter;
  fReporter.addEventListener("change", function () {
    state.reporter = fReporter.value;
    persist();
    renderRail();
  });
  fFlag.value = state.flagFilter;
  fFlag.addEventListener("change", function () {
    state.flagFilter = fFlag.value;
    persist();
    renderRail();
  });

  function applyBoxesBtn() {
    boxesBtn.classList.toggle("seg-on", state.showBoxes);
    tagsBtn.classList.toggle("seg-on", state.showTags);
    redactionBtn.classList.toggle("seg-on", state.showRedaction);
    containerBtn.classList.toggle("seg-on", state.showContainers);
  }
  function toggleFlag(key) {
    state[key] = !state[key];
    persist();
    applyBoxesBtn();
    renderPanes();
  }
  boxesBtn.addEventListener("click", function () {
    toggleFlag("showBoxes");
  });
  tagsBtn.addEventListener("click", function () {
    toggleFlag("showTags");
  });
  redactionBtn.addEventListener("click", function () {
    toggleFlag("showRedaction");
  });
  containerBtn.addEventListener("click", function () {
    toggleFlag("showContainers");
  });

  PANES.forEach(function (p) {
    var sel = el("pane-" + p + "-src");
    sel.value = state.paneSrc[p];
    sel.addEventListener("change", function () {
      state.paneSrc[p] = sel.value;
      persist();
      renderPane(p);
    });
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

  el("zoom-in").addEventListener("click",
    function () { setZoom(zoom * 1.25); });
  el("zoom-out").addEventListener("click",
    function () { setZoom(zoom / 1.25); });
  el("zoom-fit").addEventListener("click",
    function () { fitZoom(); setZoom(zoom); });

  document.addEventListener("keydown", function (ev) {
    if (ev.target.tagName === "INPUT" || ev.target.tagName === "SELECT") {
      return;
    }
    if (ev.key === "[" || ev.key === "]") {
      var items = visible();
      if (!items.length || !current) return;
      var idx = items.findIndex(function (p) {
        return p.stem === current.stem;
      });
      idx += ev.key === "]" ? 1 : -1;
      idx = Math.max(0, Math.min(items.length - 1, idx));
      select(items[idx].stem);
      var active = rail.querySelector("li.active");
      if (active) active.scrollIntoView({block: "nearest"});
    } else if (ev.key === "+" || ev.key === "=") {
      setZoom(zoom * 1.25);
    } else if (ev.key === "-") {
      setZoom(zoom / 1.25);
    } else if (ev.key === "b") {
      toggleFlag("showBoxes");
    } else if (ev.key === "l") {
      toggleFlag("showTags");
    } else if (ev.key === "r") {
      toggleFlag("showRedaction");
    } else if (ev.key === "c") {
      toggleFlag("showContainers");
    } else if (ev.key === "f") {
      flagInput.checked = !flagInput.checked;
      saveNote();
    }
  });

  renderRail();
  applyBoxesBtn();
  zoomLevel.textContent = Math.round(zoom * 100) + "%";
  var initial = state.selected;
  var ok = DATA.sample.some(function (p) { return p.stem === initial; });
  if (!initial || !ok) {
    var vis = visible();
    initial = vis.length ? vis[0].stem : null;
  }
  if (initial) select(initial);
})();
