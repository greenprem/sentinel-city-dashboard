/* City Land Monitor front end: date-wise analysis, change view, localities and a timeline slider. */
(() => {
  const $ = (id) => document.getElementById(id);
  const CLASS_ORDER = ["dense_veg", "moderate_veg", "sparse_veg", "built", "bare", "water"];
  const fmt = (v, d = 1) => (v === null || v === undefined || Number.isNaN(v)) ? "–" : Number(v).toFixed(d);
  const pct = (v) => `${fmt(v, 1)}%`;
  const signed = (v, d = 1, unit = "") => (v === null || v === undefined || Number.isNaN(v)) ? "–" :
    `${v > 0 ? "▲ +" : v < 0 ? "▼ −" : ""}${Math.abs(v).toFixed(d)}${unit}`;
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const getJSON = (u) => fetch(u).then((r) => { if (!r.ok) throw new Error(`${r.status} ${u}`); return r.json(); });

  const state = {
    slug: null, series: null, runs: [], changes: {}, pairId: null,
    t: 0, layerKey: "landcover", overlays: {}, changeOverlay: null,
    zoneSet: null, zoneData: {}, selection: null, localities: [], boundary: null, zoneLayer: null,
  };

  // ------------------------------------------------------------------ map
  const map = L.map("map", { zoomControl: true, preferCanvas: true }).setView([28.61, 77.21], 10);
  const bases = {
    "Streets (OpenStreetMap)": L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
      { maxZoom: 19, attribution: "© OpenStreetMap contributors" }),
    "Satellite (Esri, live basemap)": L.tileLayer(
      "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
      { maxZoom: 19, attribution: "Imagery © Esri, Maxar, Earthstar Geographics" }),
    "None": L.layerGroup(),
  };
  bases["Streets (OpenStreetMap)"].addTo(map);
  L.control.layers(bases, null, { position: "topleft" }).addTo(map);
  L.control.scale({ imperial: false, position: "bottomleft" }).addTo(map);
  map.attributionControl.addAttribution("Contains modified Copernicus Sentinel data");
  map.createPane("zones").style.zIndex = 450;
  window.cityMap = map;  // handy for debugging and scripted screenshots
  if (window.matchMedia("(max-width: 900px)").matches) $("layer-panel").open = false;

  // ------------------------------------------------------------------ date chips
  const iso = (d) => d.toISOString().slice(0, 10);
  const todayIso = iso(new Date());
  function setDates(list) {
    $("dates").innerHTML = "";
    list.forEach(addDateChip);
    const add = document.createElement("button");
    add.type = "button"; add.className = "chip-add"; add.textContent = "+ Add date";
    add.addEventListener("click", () => {
      const vals = readDates();
      const last = vals.length ? new Date(vals[vals.length - 1]) : new Date();
      const d = new Date(last.getTime() - 365 * 864e5);
      $("dates").insertBefore(makeChip(iso(d)), add);
      updateChipButtons();
    });
    $("dates").appendChild(add);
    updateChipButtons();
  }
  function makeChip(v) {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.innerHTML = `<input type="date" min="2017-01-01" max="${todayIso}" value="${v}" required><button type="button" aria-label="Remove date">×</button>`;
    chip.querySelector("button").addEventListener("click", () => { chip.remove(); updateChipButtons(); });
    return chip;
  }
  function addDateChip(v) { $("dates").appendChild(makeChip(v)); }
  function readDates() { return [...$("dates").querySelectorAll("input")].map((i) => i.value).filter(Boolean).sort(); }
  function updateChipButtons() {
    const chips = $("dates").querySelectorAll(".chip");
    chips.forEach((c) => { c.querySelector("button").disabled = chips.length <= 1; });
    $("dates").querySelector(".chip-add").disabled = chips.length >= 12;
  }
  setDates([iso(new Date(Date.now() - 365 * 864e5)), todayIso]);

  // ------------------------------------------------------------------ tabs
  function showTab(name) {
    document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x.dataset.tab === name));
    document.querySelectorAll(".tabpane").forEach((p) => p.classList.toggle("active", p.id === `tab-${name}`));
  }
  document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));

  // ------------------------------------------------------------------ saved analyses
  async function loadSaved(select) {
    const idx = await getJSON("/api/runs");
    const sel = $("saved");
    sel.innerHTML = "";
    const items = [];
    Object.entries(idx.cities).forEach(([slug, c]) => {
      (c.series || []).forEach((s) => items.push({ value: `series|${slug}|${s.id}`, sort: s.modified,
        text: `${c.name.split(",")[0]} · ${s.dates.join(", ")} · ${s.resolution_m} m` }));
      c.runs.forEach((r) => items.push({ value: `run|${slug}|${r.id}`, sort: Date.parse(r.generated_at) / 1000 - 1e9,
        text: `${c.name.split(",")[0]} · single period ${r.start} → ${r.end} · ${r.resolution_m} m` }));
    });
    items.sort((a, b) => b.sort - a.sort);
    if (!items.length) sel.innerHTML = "<option value=''>Nothing saved yet</option>";
    items.forEach((it) => { const o = document.createElement("option"); o.value = it.value; o.textContent = it.text; sel.appendChild(o); });
    if (select) sel.value = select;
    return items;
  }
  $("saved").addEventListener("change", (e) => openSaved(e.target.value));
  async function openSaved(v) {
    const [kind, slug, id] = v.split("|");
    if (kind === "series") return showSeries(await getJSON(`/api/series/${slug}/${id}`));
    if (kind === "run") {
      const s = await getJSON(`/api/runs/${slug}/${id}`);
      return showSeries({ id, slug, city: s.city.query, window_days: null, resolution_m: s.resolution_m, changes: [],
        dates: [{ date: s.period.end, run_id: id, start: s.period.start, end: s.period.end }] });
    }
  }

  // ------------------------------------------------------------------ analyze
  $("analyze-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const dates = readDates();
    if (!dates.length) return;
    const body = { city: $("city").value.trim(), dates, window_days: Number($("window").value), resolution: Number($("res").value) };
    $("go").disabled = true;
    setProgress(0, "Starting analysis");
    try {
      let job = await fetch("/api/analyze", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body) }).then((r) => r.json());
      if (job.detail) throw new Error(typeof job.detail === "string" ? job.detail : JSON.stringify(job.detail));
      while (job.status === "running") {
        setProgress(job.progress, job.message);
        await new Promise((r) => setTimeout(r, 1500));
        job = await getJSON(`/api/jobs/${job.id}`).catch(() => job);
      }
      if (job.status === "error") throw new Error(job.error);
      setProgress(1, "Done");
      await loadSaved(`series|${job.result.slug}|${job.result.series_id}`);
      await showSeries(await getJSON(`/api/series/${job.result.slug}/${job.result.series_id}`));
      setTimeout(() => $("progress").classList.add("hidden"), 1500);
    } catch (err) {
      setProgress(0, `Analysis failed: ${err.message}`, true);
    } finally {
      $("go").disabled = false;
    }
  });
  function setProgress(frac, msg, error = false) {
    $("progress").classList.remove("hidden");
    $("progress").classList.toggle("error", error);
    $("progress-fill").style.width = `${Math.round(frac * 100)}%`;
    $("progress-msg").textContent = msg;
  }

  // ------------------------------------------------------------------ open a series
  async function showSeries(series) {
    clearOverlays();
    state.series = series;
    state.slug = series.slug;
    state.runs = await Promise.all(series.dates.map((d) => getJSON(`/api/runs/${series.slug}/${d.run_id}`)));
    state.changes = {};
    await Promise.all(series.changes.map(async (c) => { state.changes[c.id] = { ...c, data: await getJSON(`/api/changes/${series.slug}/${c.id}`) }; }));
    // default pair: first to last date
    const fl = series.changes.find((c) => c.from === series.dates[0].date && c.to === series.dates[series.dates.length - 1].date);
    state.pairId = fl ? fl.id : (series.changes[0] || {}).id || null;
    state.zoneData = {};
    state.selection = null;
    state.localities = await getJSON(`/api/localities/${series.slug}`).catch(() => []);
    $("city").value = series.city;
    setDates(series.dates.map((d) => d.date));
    if (series.window_days) $("window").value = String(series.window_days);

    const first = state.runs[0];
    const boundary = await getJSON(`/results/${series.slug}/${series.dates[0].run_id}/boundary.geojson`);
    if (state.boundary) map.removeLayer(state.boundary);
    state.boundary = L.geoJSON(boundary, { interactive: false, style: { color: "#111", weight: 2, fill: false, dashArray: "4 3" } }).addTo(map);
    map.fitBounds(state.boundary.getBounds(), { padding: [20, 20] });

    buildTimeline();
    buildLayerList();
    buildPairSelect();
    const zs = $("zone-set");
    zs.innerHTML = "";
    Object.entries(first.zone_sets).forEach(([k, v]) => {
      const o = document.createElement("option"); o.value = k; o.textContent = `${v.label} (${v.count})`; zs.appendChild(o);
    });
    state.zoneSet = zs.value;
    if (state.series.dates.length < 2) $("zone-metric").value = "pct_built";
    renderAbout();
    setT(state.runs.length - 1);
    renderChange();
    await loadZones(zs.value);
  }

  // ------------------------------------------------------------------ timeline + crossfade
  const curIndex = () => Math.round(state.t);
  function buildTimeline() {
    const n = state.series.dates.length;
    $("timeline").classList.toggle("hidden", n < 2);
    $("tl").max = String(Math.max(1, n - 1));
    $("tl-ticks").innerHTML = state.series.dates.map((d, i) =>
      `<button type="button" class="tick" style="left:${n > 1 ? (100 * i) / (n - 1) : 0}%;transform:translateX(${i === 0 ? "0" : i === n - 1 ? "-100%" : "-50%"})" data-i="${i}">${window.innerWidth < 600 ? colLabel(d.date) : d.date}</button>`).join("");
    $("tl-ticks").querySelectorAll(".tick").forEach((b) => b.addEventListener("click", () => { stopPlay(); setT(Number(b.dataset.i)); }));
  }
  $("tl").addEventListener("input", (e) => { stopPlay(); setT(Number(e.target.value)); });

  let lastIndex = null;
  function setT(t) {
    const n = state.series.dates.length;
    state.t = Math.max(0, Math.min(n - 1, t));
    $("tl").value = String(state.t);
    const i = Math.floor(state.t), f = state.t - i;
    const d = state.series.dates;
    $("tl-label").innerHTML = f < 0.01 || i >= n - 1
      ? `Showing <b>${d[Math.round(state.t)].date}</b>`
      : `Blending <b>${d[i].date}</b> → <b>${d[i + 1].date}</b> · ${Math.round(f * 100)}%`;
    $("tl-ticks").querySelectorAll(".tick").forEach((b) => b.classList.toggle("on", Number(b.dataset.i) === curIndex()));
    applyOverlayOpacity();
    if (curIndex() !== lastIndex) {
      lastIndex = curIndex();
      renderOverview(state.runs[lastIndex], d[lastIndex]);
      if (state.zoneSet) { ensureZoneData(state.zoneSet, lastIndex).then(() => { drawZones(); renderZoneTable(); renderZoneDetail(); }); }
    }
    drawTrendCursor();
  }

  let playTimer = null;
  function stopPlay() { if (playTimer) cancelAnimationFrame(playTimer); playTimer = null; $("play").textContent = "▶"; }
  $("play").addEventListener("click", () => {
    if (playTimer) return stopPlay();
    const n = state.series.dates.length;
    if (state.t >= n - 1) setT(0);
    $("play").textContent = "❚❚";
    let last = performance.now();
    const step = (now) => {
      const dt = (now - last) / 1000; last = now;
      // ~2.5 s per date step, with a short hold on each date
      const next = state.t + dt / 2.5;
      const held = Math.abs(next - Math.round(next)) < 0.02 ? Math.round(next) : next;
      setT(held);
      if (state.t >= n - 1) return stopPlay();
      playTimer = requestAnimationFrame(step);
    };
    playTimer = requestAnimationFrame(step);
  });

  // ------------------------------------------------------------------ layers
  const DATE_LAYERS = ["landcover", "truecolor", "ndvi", "ndbi", "bsi", "mndwi", "ndmi", "ndti", "ndci"];
  const LAYER_NAMES = { landcover: "Land cover classes", truecolor: "Sentinel-2 true colour", ndvi: "Vegetation (NDVI)",
    ndbi: "Built-up (NDBI)", bsi: "Bare soil (BSI)", mndwi: "Water (MNDWI)", ndmi: "Moisture (NDMI)",
    ndti: "Water turbidity", ndci: "Water chlorophyll", transitions: "Change: land-cover transitions",
    ndvi_change: "Change: vegetation gain / loss" };
  const SPARSE = new Set(["ndti", "ndci"]);   // mostly transparent layers fade both ways

  function buildLayerList() {
    const hasChange = !!state.pairId;
    const keys = DATE_LAYERS.filter((k) => state.runs[0].layers[k]);
    $("layer-list").innerHTML =
      `<div class="lg-head">Per date (follows the slider)</div>` +
      keys.map((k) => `<label><input type="radio" name="layer" value="${k}"> ${LAYER_NAMES[k]}</label>`).join("") +
      (hasChange ? `<div class="lg-head">Between two dates</div>` +
        ["transitions", "ndvi_change"].map((k) => `<label><input type="radio" name="layer" value="${k}"> ${LAYER_NAMES[k]}</label>`).join("") : "") +
      `<label><input type="radio" name="layer" value=""> None (basemap only)</label>`;
    document.querySelectorAll("input[name=layer]").forEach((r) => r.addEventListener("change", () => setLayer(r.value)));
    if (!hasChange && ["transitions", "ndvi_change"].includes(state.layerKey)) state.layerKey = "landcover";
    setLayer(state.layerKey);
  }
  function clearOverlays() {
    Object.values(state.overlays).forEach((o) => map.removeLayer(o));
    state.overlays = {};
    if (state.changeOverlay) map.removeLayer(state.changeOverlay);
    state.changeOverlay = null;
  }
  function setLayer(key) {
    clearOverlays();
    state.layerKey = key;
    const r = document.querySelector(`input[name=layer][value="${key}"]`);
    if (r) r.checked = true;
    $("legend").innerHTML = "";
    if (!key) return;
    if (key === "transitions" || key === "ndvi_change") {
      const ch = state.changes[state.pairId].data;
      const l = ch.layers[key];
      state.changeOverlay = L.imageOverlay(`/results/${state.slug}/changes/${state.pairId}/${l.file}`, l.bounds,
        { opacity: $("opacity").value / 100, className: "pixelated" }).addTo(map);
      renderLegend(l, `${ch.from.label} → ${ch.to.label}`);
      $("timeline").classList.add("dim");
    } else {
      $("timeline").classList.remove("dim");
      state.series.dates.forEach((d, i) => {
        const l = state.runs[i].layers[key];
        state.overlays[i] = L.imageOverlay(`/results/${state.slug}/${d.run_id}/${l.file}`, l.bounds,
          { opacity: 0, className: "pixelated fade" }).addTo(map);
      });
      renderLegend(state.runs[0].layers[key]);
      applyOverlayOpacity();
    }
    if (state.zoneLayer) state.zoneLayer.bringToFront();
  }
  function applyOverlayOpacity() {
    const op = $("opacity").value / 100;
    if (state.changeOverlay) state.changeOverlay.setOpacity(op);
    const ids = Object.keys(state.overlays).map(Number);
    if (!ids.length) return;
    const n = ids.length;
    const i = Math.min(Math.floor(state.t), n - 1), f = state.t - i;
    ids.forEach((k) => {
      let o = 0;
      if (k === i) o = SPARSE.has(state.layerKey) ? op * (1 - f) : op;
      else if (k === i + 1) o = op * f;
      state.overlays[k].setOpacity(o);
    });
    if (state.overlays[i + 1]) state.overlays[i + 1].bringToFront();
    if (state.zoneLayer) state.zoneLayer.bringToFront();
  }
  $("opacity").addEventListener("input", applyOverlayOpacity);
  function renderLegend(l, pairLabel) {
    const head = pairLabel ? `<div class="desc" style="margin:0 0 4px">${esc(pairLabel)}</div>` : "";
    if (l.kind === "classes") {
      $("legend").innerHTML = head + l.classes.map((c) => `<div class="cls"><span class="sw" style="background:${c.color}"></span>${esc(c.label)}</div>`).join("");
    } else if (l.kind === "ramp") {
      $("legend").innerHTML = head + `<div>${esc(l.title)}</div><div class="ramp" style="background:linear-gradient(90deg,${l.stops.join(",")})"></div>
        <div class="ends"><span>${l.min}</span><span>${l.max}</span></div><div class="desc">${esc(l.description)}</div>`;
    } else {
      $("legend").innerHTML = `<div class="desc">Cloud-masked median composite for each date.</div>`;
    }
  }

  // ------------------------------------------------------------------ overview (at slider date)
  function renderOverview(s, d) {
    $("empty").classList.add("hidden");
    $("overview").classList.remove("hidden");
    $("city-title").textContent = `${s.city.name.split(",")[0]} as of ${d.date}`;
    $("city-meta").textContent = `${fmt(s.city.area_km2, 0)} km² · imagery ${s.period.start} to ${s.period.end} · ${s.resolution_m} m pixels`;
    $("narrative").textContent = s.narrative;
    const lc = s.landcover, m = s.metrics;
    const prev = state.runs[0] !== s ? state.runs[0] : null;
    const delta = (a, b) => prev ? `<div class="dlt">${signed(a - b, 1, " pts")} since ${state.series.dates[0].date}</div>` : "";
    const tiles = [
      ["Green cover", pct(m.green_cover_pct), "#5fae4e", prev && delta(m.green_cover_pct, prev.metrics.green_cover_pct)],
      ["Tree cover", pct(m.tree_cover_pct), lc.dense_veg.color, prev && delta(m.tree_cover_pct, prev.metrics.tree_cover_pct)],
      ["Built-up", pct(lc.built.percent), lc.built.color, prev && delta(lc.built.percent, prev.landcover.built.percent)],
      ["Bare soil", pct(lc.bare.percent), lc.bare.color, prev && delta(lc.bare.percent, prev.landcover.bare.percent)],
      ["Water", pct(lc.water.percent), lc.water.color, prev && delta(lc.water.percent, prev.landcover.water.percent)],
      ["Mean NDVI", fmt(m.mean_ndvi, 2), null, prev ? `<div class="dlt">${signed(m.mean_ndvi - prev.metrics.mean_ndvi, 2)} since ${state.series.dates[0].date}</div>` : ""],
    ];
    $("tiles").innerHTML = tiles.map(([k, v, c, dl]) =>
      `<div class="tile"><div class="k">${c ? `<span class="dot" style="background:${c}"></span>` : ""}${k}</div><div class="v">${v}</div>${dl || ""}</div>`).join("");
    $("composition").innerHTML = CLASS_ORDER.map((k) =>
      `<div title="${esc(lc[k].label)}: ${pct(lc[k].percent)}" style="flex:${lc[k].percent};background:${lc[k].color}"></div>`).join("");
    $("composition").setAttribute("aria-label", CLASS_ORDER.map((k) => `${lc[k].label} ${pct(lc[k].percent)}`).join(", "));
    $("lc-table").innerHTML = CLASS_ORDER.map((k) => `<tr><td><span class="dot" style="background:${lc[k].color}"></span> ${esc(lc[k].label)}</td>
      <td class="num">${fmt(lc[k].area_km2, 1)} km²</td><td class="num">${pct(lc[k].percent)}</td></tr>`).join("");
    const wq = s.water_quality;
    $("wq").innerHTML = wq.ndti === null ? `<div class="big">${esc(wq.label)}</div>` :
      `<div class="big">${esc(wq.label)}</div>
       <dl class="kv"><dt>Turbidity (NDTI)</dt><dd>${fmt(wq.ndti, 3)} <span class="muted">(&lt; −0.1 clear, &gt; 0.05 very turbid)</span></dd>
       <dt>Chlorophyll (NDCI)</dt><dd>${fmt(wq.ndci, 3)} <span class="muted">(&gt; 0.15 high algae)</span></dd>
       <dt>Water area</dt><dd>${fmt(lc.water.area_km2, 1)} km²</dd></dl>`;
    const im = s.imagery;
    $("imagery").innerHTML = `<dl class="kv">
      <dt>Source</dt><dd>${esc(im.collection)} via ${esc(im.source)}</dd>
      <dt>Dates blended</dt><dd>${im.dates.map((x) => `${x.date} (${Math.round(x.clear_fraction * 100)}% clear)`).join("<br>")}</dd>
      <dt>Clear looks / pixel</dt><dd>${fmt(im.clear_obs_mean, 1)} on average; ${pct(im.aoi_coverage * 100)} of city covered</dd>
      ${d.window_days && state.series.window_days && d.window_days !== state.series.window_days ? `<dt>Window</dt><dd>widened to ${d.window_days} days (too cloudy)</dd>` : ""}
      <dt>Boundary</dt><dd>${esc(s.city.boundary_source)}</dd></dl>`;
  }

  // ------------------------------------------------------------------ change tab
  function buildPairSelect() {
    const sel = $("pair");
    sel.innerHTML = state.series.changes.map((c) => `<option value="${c.id}">${c.from} → ${c.to}</option>`).join("");
    if (state.pairId) sel.value = state.pairId;
  }
  $("pair").addEventListener("change", (e) => {
    state.pairId = e.target.value;
    renderChange();
    if (["transitions", "ndvi_change"].includes(state.layerKey)) setLayer(state.layerKey);
    drawZones(); renderZoneTable(); renderZoneDetail();
  });

  function renderChange() {
    const n = state.series.dates.length;
    $("change-empty").classList.toggle("hidden", n >= 2);
    $("change").classList.toggle("hidden", n < 2);
    if (n < 2) return;
    const d = state.series.dates, runs = state.runs;
    $("change-title").textContent = `${runs[0].city.name.split(",")[0]}, ${d[0].date} to ${d[n - 1].date}`;
    $("change-meta").textContent = `${n} dates · each a cloud-free composite of up to ${state.series.window_days || "?"} days before the date · ${state.series.resolution_m} m pixels`;
    renderTrendChart();
    // table: class rows × date columns + change first → last
    const lc = runs[0].landcover;
    $("trend-table").innerHTML = `<thead><tr><th>Class</th>${d.map((x) => `<th class="num">${colLabel(x.date)}</th>`).join("")}<th class="num">Change</th></tr></thead><tbody>` +
      CLASS_ORDER.map((k) => `<tr><td><span class="dot" style="background:${lc[k].color}"></span> ${esc(shortLabel(k))}</td>` +
        runs.map((r) => `<td class="num">${fmt(r.landcover[k].percent, 1)}</td>`).join("") +
        `<td class="num chg">${signed(runs[n - 1].landcover[k].percent - runs[0].landcover[k].percent, 1)}</td></tr>`).join("") +
      `<tr><td>Mean NDVI</td>${runs.map((r) => `<td class="num">${fmt(r.metrics.mean_ndvi, 2)}</td>`).join("")}<td class="num chg">${signed(runs[n - 1].metrics.mean_ndvi - runs[0].metrics.mean_ndvi, 2)}</td></tr>` +
      `<tr><td>Water turbidity</td>${runs.map((r) => `<td class="num">${fmt(r.water_quality.ndti, 2)}</td>`).join("")}<td class="num chg">${r2(runs[n - 1].water_quality.ndti, runs[0].water_quality.ndti)}</td></tr></tbody>`;
    renderPair();
  }
  const r2 = (a, b) => (a === null || b === null) ? "–" : signed(a - b, 2);
  const SHORT = { dense_veg: "Tree cover", moderate_veg: "Moderate veg.", sparse_veg: "Sparse veg.", built: "Built-up", bare: "Bare soil", water: "Water" };
  const shortLabel = (k) => SHORT[k];
  // column label for a date: the year when every date is in a different year, else YYYY-MM-DD
  function colLabel(date) {
    const ds = state.series.dates.map((d) => d.date);
    const years = new Set(ds.map((d) => d.slice(0, 4)));
    if (years.size === ds.length) return date.slice(0, 4);
    const months = new Set(ds.map((d) => d.slice(0, 7)));
    return months.size === ds.length ? date.slice(0, 7) : date.slice(5);
  }

  function renderPair() {
    const c = state.changes[state.pairId];
    if (!c) return;
    const ch = c.data;
    $("pair-summary").textContent = `${fmt(ch.changed_km2, 1)} km² of ${fmt(ch.compared_km2, 0)} km² changed class between ${ch.from.label} and ${ch.to.label}. Mean NDVI changed by ${signed(ch.mean_dndvi, 3)}.`;
    const tr = Object.values(ch.transitions);
    const max = Math.max(...tr.map((x) => x.area_km2), 0.001);
    $("transitions").innerHTML = tr.map((x) => `<div class="hbar"><div class="hb-label"><span class="sw" style="background:${x.color}"></span>${esc(x.label)}</div>
      <div class="hb-track"><div class="hb-fill" style="width:${(100 * x.area_km2) / max}%;background:${x.color}"></div></div>
      <div class="hb-val">${fmt(x.area_km2, 1)} km²</div></div>`).join("");
    // top localities from the first (finest named) zone set
    const setKey = Object.keys(state.runs[0].zone_sets)[0];
    ensureZoneData(setKey, 0).then(() => {
      const names = {};
      state.zoneData[setKey][0].features.forEach((f) => { names[f.properties.zid] = f.properties.name; });
      const rows = Object.entries(ch.zones[setKey] || {}).map(([zid, v]) => ({ zid, name: names[zid], ...v }));
      const top = (key) => rows.sort((a, b) => b[key] - a[key]).slice(0, 5).map((r) =>
        `<tr data-set="${setKey}" data-zid="${r.zid}"><td>${esc(r.name)}</td><td class="num">${fmt(r[key], 2)} km²</td></tr>`).join("");
      $("top-built").innerHTML = top("veg_to_built");
      $("top-green").innerHTML = top("to_veg");
      document.querySelectorAll("#top-built tr, #top-green tr").forEach((tr) => tr.addEventListener("click", async () => {
        $("zone-set").value = tr.dataset.set;
        await loadZones(tr.dataset.set);
        selectZones([Number(tr.dataset.zid)], null, true);
      }));
    });
  }

  // SVG line chart: share of each class over time, with a cursor at the slider position.
  function renderTrendChart() {
    const W = 380, H = 210, m = { l: 34, r: 12, t: 10, b: 24 };
    const d = state.series.dates.map((x) => Date.parse(x.date));
    const x0 = d[0], x1 = d[d.length - 1];
    const X = (t) => m.l + (W - m.l - m.r) * (x1 > x0 ? (t - x0) / (x1 - x0) : 0.5);
    const all = state.runs.flatMap((r) => CLASS_ORDER.map((k) => r.landcover[k].percent));
    const ymax = Math.ceil(Math.max(...all) / 10) * 10;
    const Y = (v) => m.t + (H - m.t - m.b) * (1 - v / ymax);
    const lc = state.runs[0].landcover;
    let svg = `<svg viewBox="0 0 ${W} ${H}" class="trend" role="img" aria-label="Share of each land-cover class at each date">`;
    for (let v = 0; v <= ymax; v += 10) svg += `<line class="grid" x1="${m.l}" x2="${W - m.r}" y1="${Y(v)}" y2="${Y(v)}"/><text class="ax" x="${m.l - 6}" y="${Y(v) + 4}" text-anchor="end">${v}%</text>`;
    let lastX = -1e9;
    state.series.dates.forEach((x, i) => {
      const px = X(d[i]);
      if (px - lastX < 34 && i !== state.series.dates.length - 1) return;
      if (px - lastX < 34) svg = svg.replace(/<text class="ax lastlbl"[^>]*>[^<]*<\/text>$/, "");
      svg += `<text class="ax lastlbl" x="${px}" y="${H - 6}" text-anchor="${i === 0 ? "start" : i === state.series.dates.length - 1 ? "end" : "middle"}">${colLabel(x.date)}</text>`;
      lastX = px;
    });
    CLASS_ORDER.forEach((k) => {
      const pts = state.runs.map((r, i) => `${X(d[i])},${Y(r.landcover[k].percent)}`);
      svg += `<polyline fill="none" stroke="${lc[k].color}" stroke-width="2" points="${pts.join(" ")}"/>`;
      state.runs.forEach((r, i) => { svg += `<circle cx="${X(d[i])}" cy="${Y(r.landcover[k].percent)}" r="4" fill="${lc[k].color}" stroke="var(--surface)" stroke-width="2"/>`; });
    });
    svg += `<line id="trend-cursor" class="cursor" y1="${m.t}" y2="${H - m.b}"/>`;
    state.series.dates.forEach((x, i) => { svg += `<rect class="hit" data-i="${i}" x="${X(d[i]) - 18}" y="${m.t}" width="36" height="${H - m.t - m.b}"/>`; });
    svg += `</svg><div class="chart-legend">${CLASS_ORDER.map((k) => `<span><span class="sw" style="background:${lc[k].color}"></span>${shortLabel(k)}</span>`).join("")}</div>`;
    $("trend-chart").innerHTML = svg;
    state.trendX = (t) => X(t);
    $("trend-chart").querySelectorAll(".hit").forEach((h) => {
      h.addEventListener("mousemove", (e) => {
        const r = state.runs[Number(h.dataset.i)];
        showTip(e, `<b>${state.series.dates[Number(h.dataset.i)].date}</b><br>` + CLASS_ORDER.map((k) =>
          `<span class="sw" style="background:${lc[k].color}"></span> ${shortLabel(k)} ${pct(r.landcover[k].percent)}`).join("<br>"));
      });
      h.addEventListener("mouseleave", hideTip);
      h.addEventListener("click", () => { stopPlay(); setT(Number(h.dataset.i)); });
    });
    drawTrendCursor();
  }
  function drawTrendCursor() {
    const c = document.getElementById("trend-cursor");
    if (!c || !state.trendX) return;
    const d = state.series.dates.map((x) => Date.parse(x.date));
    const i = Math.floor(state.t), f = state.t - i;
    const t = i >= d.length - 1 ? d[d.length - 1] : d[i] + f * (d[i + 1] - d[i]);
    c.setAttribute("x1", state.trendX(t)); c.setAttribute("x2", state.trendX(t));
  }
  function showTip(e, html) {
    const tip = $("tooltip");
    tip.innerHTML = html; tip.classList.remove("hidden");
    tip.style.left = `${e.clientX + 14}px`; tip.style.top = `${e.clientY + 14}px`;
  }
  function hideTip() { $("tooltip").classList.add("hidden"); }

  // ------------------------------------------------------------------ point query (pixel history)
  map.on("click", async (e) => {
    if (!state.series) return;
    const qs = await Promise.all(state.series.dates.map((d) =>
      getJSON(`/api/point?slug=${state.slug}&run_id=${d.run_id}&lat=${e.latlng.lat}&lon=${e.latlng.lng}`).catch(() => ({}))));
    if (!qs.some((q) => q.inside)) return;
    const rows = qs.map((q, i) => `<tr><td>${state.series.dates[i].date}</td><td>${q.inside ? `<span class="dot" style="background:${q.color}"></span> ${esc(q.label.split(" / ")[0])}` : "–"}</td><td class="num">${fmt(q.ndvi, 2)}</td><td class="num">${fmt(q.ndbi, 2)}</td></tr>`).join("");
    L.popup({ maxWidth: 360 }).setLatLng(e.latlng).setContent(
      `<table class="pop"><thead><tr><th>Date</th><th>Class</th><th>NDVI</th><th>NDBI</th></tr></thead><tbody>${rows}</tbody></table>
       <div class="muted">${e.latlng.lat.toFixed(5)}, ${e.latlng.lng.toFixed(5)} · one ${state.series.resolution_m} m pixel</div>`).openOn(map);
  });

  // ------------------------------------------------------------------ zones / localities
  async function ensureZoneData(set, i) {
    state.zoneData[set] = state.zoneData[set] || {};
    if (!state.zoneData[set][i]) {
      const file = state.runs[i].zone_sets[set].file;
      state.zoneData[set][i] = await getJSON(`/results/${state.slug}/${state.series.dates[i].run_id}/${file}`);
    }
    return state.zoneData[set][i];
  }
  async function loadZones(set) {
    state.zoneSet = set;
    await Promise.all(state.series.dates.map((_, i) => ensureZoneData(set, i)));
    if (state.selection && state.selection.set !== set) state.selection = null;
    drawZones(); renderZoneTable(); renderZoneDetail();
  }
  $("zone-set").addEventListener("change", (e) => loadZones(e.target.value));
  $("zone-metric").addEventListener("change", () => { drawZones(); renderZoneTable(); });
  $("show-zones").addEventListener("change", drawZones);
  $("zone-choro").addEventListener("change", drawZones);

  // props for a zone id at date index i
  function zprops(set, i, zid) {
    const fc = state.zoneData[set] && state.zoneData[set][i];
    if (!fc) return null;
    fc._byId = fc._byId || Object.fromEntries(fc.features.map((f) => [f.properties.zid, f.properties]));
    return fc._byId[zid] || null;
  }
  function pairIdx() {
    const c = state.changes[state.pairId];
    if (!c) return null;
    const ds = state.series.dates.map((d) => d.date);
    return [ds.indexOf(c.from), ds.indexOf(c.to)];
  }
  // value of the chosen metric for one zone (or a combined selection)
  function metricValue(metric, zids) {
    const set = state.zoneSet;
    if (metric.startsWith("t_")) {
      const ch = state.changes[state.pairId];
      if (!ch) return null;
      return zids.reduce((s, z) => s + ((ch.data.zones[set] || {})[z] || {})[metric.slice(2)] || 0, 0);
    }
    if (metric.startsWith("d_")) {
      const pi = pairIdx();
      if (!pi) return null;
      const a = combined(set, pi[0], zids), b = combined(set, pi[1], zids);
      const k = metric.slice(2);
      return a && b && a[k] !== null && b[k] !== null ? b[k] - a[k] : null;
    }
    const p = combined(set, curIndex(), zids);
    return p ? p[metric] : null;
  }
  // area-weighted combination of several zones' props (e.g. "Rohini" = 5 wards)
  function combined(set, i, zids) {
    const ps = zids.map((z) => zprops(set, i, z)).filter(Boolean);
    if (!ps.length) return null;
    if (ps.length === 1) return ps[0];
    const area = ps.reduce((s, p) => s + p.area_km2, 0);
    const out = { area_km2: area };
    Object.keys(ps[0]).forEach((k) => {
      if (k.startsWith("pct_") || k.startsWith("mean_")) {
        const vs = ps.filter((p) => p[k] !== null);
        const a = vs.reduce((s, p) => s + p.area_km2, 0);
        out[k] = a ? vs.reduce((s, p) => s + p[k] * p.area_km2, 0) / a : null;
      }
    });
    return out;
  }

  const SEQ = {
    green: ["#eef3e6", "#cfe2bd", "#a3cb8c", "#6fae5f", "#3f8a3c", "#1f6b2c"],
    red: ["#f8ece8", "#f0cbc1", "#e3a091", "#d47262", "#c04a3d", "#97302a"],
    tan: ["#f8f1e4", "#efdcb9", "#e3c38d", "#d6a862", "#b98540", "#8c5f28"],
    blue: ["#ebf1fa", "#c9daf2", "#9dbde6", "#6d9bd8", "#3f78c7", "#1f56a3"],
  };
  // Diverging: two hues around a neutral grey midpoint (no change)
  const DIV = {
    built: ["#2f6f80", "#7fa9b3", "#e6e4df", "#df9a8c", "#b8432f"],
    green: ["#8a5a2b", "#c9a77a", "#e6e4df", "#8cc58a", "#1f7a3a"],
    bare: ["#2f6f80", "#7fa9b3", "#e6e4df", "#dcb47c", "#a8661e"],
  };
  const METRIC_SCALE = {
    pct_built: ["seq", "red"], pct_green: ["seq", "green"], pct_dense_veg: ["seq", "green"], pct_bare: ["seq", "tan"],
    pct_water: ["seq", "blue"], mean_ndvi: ["seq", "green"],
    d_pct_built: ["div", "built"], d_pct_green: ["div", "green"], d_pct_bare: ["div", "bare"], d_mean_ndvi: ["div", "green"],
    t_veg_to_built: ["seq", "red"], t_to_veg: ["seq", "green"],
  };
  function colorFor(metric, v, lo, hi) {
    if (v === null || v === undefined) return "transparent";
    const [kind, hue] = METRIC_SCALE[metric];
    if (kind === "seq") {
      const r = SEQ[hue], t = hi > lo ? (v - lo) / (hi - lo) : 0;
      return r[Math.min(r.length - 1, Math.max(0, Math.floor(t * r.length)))];
    }
    const r = DIV[hue], m = Math.max(Math.abs(lo), Math.abs(hi)) || 1;
    const t = (v / m + 1) / 2;   // 0..1 with 0.5 = no change
    return r[Math.min(r.length - 1, Math.max(0, Math.round(t * (r.length - 1))))];
  }
  function metricLabel(m, v) {
    if (v === null || v === undefined) return "–";
    if (m.startsWith("t_")) return `${fmt(v, 2)} km²`;
    if (m === "d_mean_ndvi") return signed(v, 3);
    if (m.startsWith("d_")) return signed(v, 1, " pts");
    return m.startsWith("pct_") ? pct(v) : fmt(v, 2);
  }
  const metricName = () => $("zone-metric").selectedOptions[0].textContent;

  function drawZones() {
    if (state.zoneLayer) map.removeLayer(state.zoneLayer);
    state.zoneLayer = null;
    $("zone-legend").innerHTML = "";
    const fc = state.zoneData[state.zoneSet] && state.zoneData[state.zoneSet][0];
    if (!fc || !$("show-zones").checked) return;
    const metric = $("zone-metric").value;
    const vals = fc.features.map((f) => metricValue(metric, [f.properties.zid])).filter((v) => v !== null);
    const lo = Math.min(...vals), hi = Math.max(...vals);
    const choro = $("zone-choro").checked;
    const sel = new Set(state.selection ? state.selection.zids : []);
    if (choro) {
      const [kind, hue] = METRIC_SCALE[metric];
      const stops = kind === "seq" ? SEQ[hue] : DIV[hue];
      const m = Math.max(Math.abs(lo), Math.abs(hi));
      $("zone-legend").innerHTML = `<div>Sections: ${esc(metricName())}${metric.startsWith("d_") || metric.startsWith("t_") ? ` <span class="muted">(${esc(pairText())})</span>` : ""}</div>
        <div class="ramp" style="background:linear-gradient(90deg,${stops.join(",")})"></div>
        <div class="ends"><span>${metricLabel(metric, kind === "div" ? -m : lo)}</span>${kind === "div" ? "<span>no change</span>" : ""}<span>${metricLabel(metric, kind === "div" ? m : hi)}</span></div>`;
    }
    state.zoneLayer = L.geoJSON(fc, {
      pane: "zones",
      style: (f) => {
        const on = sel.has(f.properties.zid);
        return { color: on ? "#000" : "#333", weight: on ? 3.5 : 0.8, opacity: on ? 1 : 0.7,
          fillColor: choro ? colorFor(metric, metricValue(metric, [f.properties.zid]), lo, hi) : "#000",
          fillOpacity: choro ? 0.85 : 0 };
      },
      onEachFeature: (f, layer) => {
        layer.on("mouseover", (e) => showTip(e.originalEvent, `<b>${esc(f.properties.name)}</b><br>${esc(metricName())}: ${metricLabel(metric, metricValue(metric, [f.properties.zid]))}`));
        layer.on("mousemove", (e) => showTip(e.originalEvent, $("tooltip").innerHTML));
        layer.on("mouseout", hideTip);
        layer.on("click", (e) => { L.DomEvent.stopPropagation(e); selectZones([f.properties.zid], null, false); });
      },
    }).addTo(map);
    state.selLayer = null;
  }
  const pairText = () => { const c = state.changes[state.pairId]; return c ? `${c.from} → ${c.to}` : ""; };

  function renderZoneTable() {
    const fc = state.zoneData[state.zoneSet] && state.zoneData[state.zoneSet][0];
    if (!fc) return;
    const metric = $("zone-metric").value;
    const rows = fc.features.map((f) => ({ p: f.properties, v: metricValue(metric, [f.properties.zid]) }))
      .sort((a, b) => (b.v ?? -1e9) - (a.v ?? -1e9));
    const sel = new Set(state.selection ? state.selection.zids : []);
    const ctx = metric.startsWith("d_") || metric.startsWith("t_") ? pairText() : state.series.dates[curIndex()].date;
    $("zone-table").querySelector("thead").innerHTML =
      `<tr><th>Section</th><th class="num">${esc(metricName())}<div class="sub">${esc(ctx)}</div></th><th class="num">km²</th></tr>`;
    $("zone-table").querySelector("tbody").innerHTML = rows.map(({ p, v }) => `<tr data-zid="${p.zid}" class="${sel.has(p.zid) ? "sel" : ""}">
      <td>${esc(p.name)}</td><td class="num">${metricLabel(metric, v)}</td><td class="num">${fmt(p.area_km2, 1)}</td></tr>`).join("");
    $("zone-table").querySelectorAll("tbody tr").forEach((tr) => tr.addEventListener("click", () => selectZones([Number(tr.dataset.zid)], null, true)));
  }

  function selectZones(zids, label, zoom) {
    state.selection = { set: state.zoneSet, zids, label };
    showTab("areas");
    drawZones(); renderZoneTable(); renderZoneDetail();
    const fc = state.zoneData[state.zoneSet][0];
    const feats = fc.features.filter((f) => zids.includes(f.properties.zid));
    if (zoom && feats.length) map.fitBounds(L.geoJSON({ type: "FeatureCollection", features: feats }).getBounds(), { padding: [60, 60], maxZoom: 14 });
  }

  function renderZoneDetail() {
    const box = $("zone-detail");
    const s = state.selection;
    if (!s || s.set !== state.zoneSet || !state.zoneData[s.set]) { box.classList.add("hidden"); return; }
    const n = state.series.dates.length;
    const per = state.series.dates.map((_, i) => combined(s.set, i, s.zids));
    if (!per[0]) { box.classList.add("hidden"); return; }
    const names = s.zids.map((z) => (zprops(s.set, 0, z) || {}).name).filter(Boolean);
    const title = s.label || (names.length === 1 ? names[0] : `${names.length} sections combined`);
    const lc = state.runs[0].landcover;
    const cur = per[curIndex()];
    const dom = CLASS_ORDER.reduce((a, k) => cur["pct_" + k] > cur["pct_" + a] ? k : a, CLASS_ORDER[0]);
    let html = `<div class="big">${esc(title)}</div>
      <div class="muted" style="margin-bottom:8px">${fmt(cur.area_km2, 1)} km² · mostly ${esc(lc[dom].label.toLowerCase())} on ${state.series.dates[curIndex()].date}${names.length > 1 || s.label ? `<br>${esc(names.join(", "))}` : ""}</div>`;
    html += `<div class="stackbar">${CLASS_ORDER.map((k) => `<div title="${esc(lc[k].label)} ${pct(cur["pct_" + k])}" style="flex:${cur["pct_" + k]};background:${lc[k].color}"></div>`).join("")}</div>`;
    html += `<div class="table-scroll"><table class="trend-table"><thead><tr><th>Class</th>${state.series.dates.map((d) => `<th class="num">${colLabel(d.date)}</th>`).join("")}${n > 1 ? "<th class=num>Change</th>" : ""}</tr></thead><tbody>` +
      CLASS_ORDER.map((k) => `<tr><td><span class="dot" style="background:${lc[k].color}"></span> ${shortLabel(k)}</td>${per.map((p) => `<td class="num">${p ? fmt(p["pct_" + k], 1) : "–"}</td>`).join("")}${n > 1 ? `<td class="num chg">${signed(per[n - 1]["pct_" + k] - per[0]["pct_" + k], 1)}</td>` : ""}</tr>`).join("") +
      `<tr><td>Mean NDVI</td>${per.map((p) => `<td class="num">${p ? fmt(p.mean_ndvi, 2) : "–"}</td>`).join("")}${n > 1 ? `<td class="num chg">${signed(per[n - 1].mean_ndvi - per[0].mean_ndvi, 2)}</td>` : ""}</tr></tbody></table></div>`;
    const ch = state.changes[state.pairId];
    if (ch) {
      const zc = s.zids.map((z) => (ch.data.zones[s.set] || {})[z]).filter(Boolean);
      const tr = Object.entries(ch.data.transitions).map(([k, v]) => ({ ...v, km2: zc.reduce((a, r) => a + (r[k] || 0), 0) }));
      html += `<div class="sub-h">What changed, ${esc(pairText())}</div>` + tr.filter((x) => x.km2 > 0).map((x) =>
        `<div class="cls"><span class="sw" style="background:${x.color}"></span>${esc(x.label)}<span class="num-r">${fmt(x.km2, 2)} km²</span></div>`).join("");
    }
    box.innerHTML = html;
    box.classList.remove("hidden");
  }

  // ------------------------------------------------------------------ suggestion boxes
  // Arrow keys move through a suggestion list, Enter picks, Escape closes. Shared by the city box and locality search.
  function suggestKeys(input, box) {
    input.addEventListener("keydown", (e) => {
      const items = [...box.querySelectorAll("button")];
      if (box.classList.contains("hidden") || !items.length) return;
      let i = items.findIndex((b) => b.classList.contains("active"));
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        i = e.key === "ArrowDown" ? (i + 1) % items.length : (i <= 0 ? items.length - 1 : i - 1);
        items.forEach((b, k) => b.classList.toggle("active", k === i));
        items[i].scrollIntoView({ block: "nearest" });
      } else if (e.key === "Enter" && i >= 0) {
        e.preventDefault();
        items[i].click();
      } else if (e.key === "Escape") {
        box.classList.add("hidden");
      }
    });
    new MutationObserver(() => input.setAttribute("aria-expanded", String(!box.classList.contains("hidden"))))
      .observe(box, { attributes: true, attributeFilter: ["class"] });
  }

  // City box: cities, towns, districts and neighbourhoods only (the server filters out buildings, roads, POIs).
  let cityTimer = null, cityReq = 0;
  $("city").addEventListener("input", () => {
    clearTimeout(cityTimer);
    const q = $("city").value.trim(), box = $("city-suggest");
    if (q.length < 2) { box.classList.add("hidden"); return; }
    cityTimer = setTimeout(async () => {
      const my = ++cityReq;
      let list = [];
      try { list = await (await fetch(`/api/places?q=${encodeURIComponent(q)}`)).json(); } catch (e) { /* offline */ }
      if (my !== cityReq) return;
      if (!list.length) { box.classList.add("hidden"); return; }
      box.innerHTML = list.map((o, i) => `<button type="button" role="option" data-i="${i}">${esc(o.label)}<span class="kind">${esc(o.source === "saved boundary" ? "boundary saved" : o.kind || "")}</span></button>`).join("");
      box.classList.remove("hidden");
      box.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => {
        $("city").value = list[Number(b.dataset.i)].value;
        box.classList.add("hidden");
        $("city").focus();
      }));
    }, 250);
  });
  suggestKeys($("city"), $("city-suggest"));

  // ------------------------------------------------------------------ locality search
  // Two boxes share this: the one in the Localities tab and the one on the map. Both suggest only areas
  // (ward sections, districts, named localities), never landmarks or roads.
  const norm = (s) => s.toLowerCase().replace(/[^a-z0-9]/g, "");
  function localitySuggestions(text) {
    const q = norm(text);
    if (q.length < 2 || !state.zoneSet) return null;
    const set = Object.keys(state.runs[0].zone_sets)[0];   // localities = first (finest named) set
    const fc = state.zoneData[set] && state.zoneData[set][0];
    const out = [];
    if (fc) {
      const hits = fc.features.filter((f) => norm(f.properties.name).includes(q));
      const typed = text.trim().replace(/\b\w/g, (c) => c.toUpperCase());
      if (hits.length > 1) out.push({ label: `All sections matching “${typed}” (${hits.length})`, all: true, set, zids: hits.map((f) => f.properties.zid), name: typed });
      hits.slice(0, 8).forEach((f) => out.push({ label: f.properties.name, hint: "section", set, zids: [f.properties.zid] }));
      state.localities.filter((l) => norm(l.name).includes(q)).slice(0, 8).forEach((l) => {
        const f = fc.features.find((x) => x.properties.name === l.ward);
        if (f) out.push({ label: l.name, hint: `in section ${l.ward}`, set, zids: [f.properties.zid], name: l.name, point: [l.lat, l.lon] });
      });
    }
    // other zone sets (districts, sub-districts)
    Object.keys(state.zoneData).filter((k) => k !== set && k !== "grid").forEach((k) => {
      const z = state.zoneData[k][0];
      if (z) z.features.filter((f) => norm(f.properties.name).includes(q)).slice(0, 3)
        .forEach((f) => out.push({ label: f.properties.name, hint: state.runs[0].zone_sets[k].label, set: k, zids: [f.properties.zid] }));
    });
    // "All sections matching" first, then names that start with what was typed (Saket before Nangli Sakravati)
    const rank = (o) => (o.all ? 0 : norm(o.label).startsWith(q) ? 1 : 2);
    return out.sort((x, y) => rank(x) - rank(y)).slice(0, 14);
  }

  async function pickLocality(o, fromMap) {
    if (state.zoneSet !== o.set) { $("zone-set").value = o.set; await loadZones(o.set); }
    const label = o.zids.length > 1 ? `${o.name} (${o.zids.length} sections combined)`
      : (o.point ? `${o.name} (section: ${(zprops(o.set, 0, o.zids[0]) || {}).name})` : null);
    selectZones(o.zids, label, true);
    $("loc-search").value = $("map-search").value = o.label.replace(/^All sections matching “(.*)” \(\d+\)$/, "$1");
    if (fromMap) setTimeout(showSelectionPopup, 450);   // after the zoom settles, so the popup can pan into view
  }

  // A short summary on the map itself, so the result is visible without looking at the side panel.
  function showSelectionPopup() {
    const s = state.selection;
    if (!s) return;
    const fc = state.zoneData[s.set][0];
    const feats = fc.features.filter((f) => s.zids.includes(f.properties.zid));
    if (!feats.length) return;
    const n = state.series.dates.length, i = curIndex();
    const cur = combined(s.set, i, s.zids), first = combined(s.set, 0, s.zids), last = combined(s.set, n - 1, s.zids);
    const names = s.zids.map((z) => (zprops(s.set, 0, z) || {}).name).filter(Boolean);
    const row = (lbl, k) => `<tr><th>${lbl}</th><td>${pct(cur[k])}</td>${n > 1 ? `<td class="chg">${signed(last[k] - first[k], 1)} pts</td>` : ""}</tr>`;
    L.popup({ maxWidth: 300, className: "sel-popup", autoPanPaddingTopLeft: [20, 120], autoPanPaddingBottomRight: [20, 110] })
      .setLatLng(L.geoJSON({ type: "FeatureCollection", features: feats }).getBounds().getCenter())
      .setContent(`<div class="big">${esc(s.label || (names.length === 1 ? names[0] : `${names.length} sections combined`))}</div>
        <div class="muted">${fmt(cur.area_km2, 1)} km² · ${state.series.dates[i].date}</div>
        <table class="pop"><thead><tr><th></th><th>Now</th>${n > 1 ? `<th>${colLabel(state.series.dates[0].date)} → ${colLabel(state.series.dates[n - 1].date)}</th>` : ""}</tr></thead><tbody>
        ${row("Built-up", "pct_built")}${row("Green", "pct_green")}${row("Bare soil", "pct_bare")}${row("Water", "pct_water")}</tbody></table>
        <a href="#" class="open-detail">Full analysis in the Localities panel</a>`)
      .openOn(map);
    const a = document.querySelector(".sel-popup .open-detail");
    if (a) a.addEventListener("click", (e) => { e.preventDefault(); showTab("areas"); $("zone-detail").scrollIntoView({ behavior: "smooth", block: "start" }); });
  }

  function wireLocalitySearch(input, box, fromMap) {
    let out = [];
    input.addEventListener("input", () => {
      out = localitySuggestions(input.value);
      if (!out) { box.classList.add("hidden"); return; }
      box.innerHTML = out.length ? out.map((o, i) => `<button type="button" role="option" data-i="${i}">${esc(o.label)}${o.hint ? ` <span class="muted">${esc(o.hint)}</span>` : ""}</button>`).join("")
        : `<div class="muted" style="padding:8px">No area with that name. Try a nearby locality.</div>`;
      box.classList.remove("hidden");
      box.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => {
        box.classList.add("hidden");
        pickLocality(out[Number(b.dataset.i)], fromMap);
      }));
    });
    suggestKeys(input, box);
    // Enter without choosing from the list takes the best match ("rohini" + Enter selects all Rohini sections).
    input.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" || e.defaultPrevented) return;
      e.preventDefault();
      const list = localitySuggestions(input.value);
      if (list && list.length) { box.classList.add("hidden"); pickLocality(list[0], fromMap); }
    });
  }
  wireLocalitySearch($("loc-search"), $("loc-suggest"), false);
  wireLocalitySearch($("map-search"), $("map-suggest"), true);
  document.addEventListener("click", (e) => {
    const wrap = e.target.closest(".search-wrap");
    ["loc-suggest", "city-suggest", "map-suggest"].forEach((id) => { if (!wrap || !wrap.contains($(id))) $(id).classList.add("hidden"); });
  });

  // ------------------------------------------------------------------ about
  function renderAbout() {
    const s = state.runs[0];
    const r = s.classification_rules;
    $("about").innerHTML = `
      <h2>How it works</h2>
      <p>Each date gets its own composite: every Sentinel-2 Level-2A scene in the window before the date is checked for cloud
      over the city itself, and the clearest are blended into a per-pixel median after masking cloud, shadow, cirrus and snow.
      If a window is too cloudy it is widened automatically. All dates share one pixel grid, so change is measured pixel by pixel.</p>
      <p>The timeline slider cross-fades the selected layer from one date's image to the next, so changes appear as the slider moves.
      The change layers compare two dates directly: class transitions (for example vegetation to built-up) and NDVI difference.</p>
      <h2>Sections</h2>
      <p>${Object.values(s.zone_sets).map((z) => `${esc(z.label)} (${z.count})`).join(", ")}. The locality search also knows
      ${state.localities.length} named places from the GeoNames postal-code data (for example Saket or Badarpur) and opens the
      ward section that contains each one. Searching a name shared by several sections (Rohini) combines them.</p>
      <h2>Indices</h2>
      <dl class="kv">
        <dt>NDVI</dt><dd>(NIR − Red) / (NIR + Red): vegetation vigour</dd>
        <dt>NDBI</dt><dd>(SWIR1 − NIR) / (SWIR1 + NIR): built-up surfaces</dd>
        <dt>MNDWI</dt><dd>(Green − SWIR1) / (Green + SWIR1): open water</dd>
        <dt>BSI</dt><dd>((SWIR1+Red) − (NIR+Blue)) / sum: bare soil</dd>
        <dt>NDMI</dt><dd>(NIR − SWIR1) / (NIR + SWIR1): moisture</dd>
        <dt>NDTI</dt><dd>(Red − Green) / (Red + Green) on water: turbidity</dd>
        <dt>NDCI</dt><dd>(RedEdge1 − Red) / (RedEdge1 + Red) on water: chlorophyll-a</dd>
      </dl>
      <h2>Classification rules</h2>
      <p class="muted">Transparent thresholds, calibrated on Delhi. Not a trained model; small changes between dates can be
      noise (season, haze, a different mix of scenes). Compare the same season across years.</p>
      <dl class="kv">${Object.entries(r).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join("")}</dl>
      <h2>Downloads</h2>
      <ul>${state.series.dates.map((d) => `<li>${d.date}: <a href="/results/${state.slug}/${d.run_id}/summary.json" target="_blank">summary.json</a>,
        <a href="/results/${state.slug}/${d.run_id}/landcover.tif">landcover.tif</a>, <a href="/results/${state.slug}/${d.run_id}/indices.tif">indices.tif</a></li>`).join("")}
        ${state.series.changes.map((c) => `<li>Change ${c.from} → ${c.to}: <a href="/results/${state.slug}/changes/${c.id}/change.json" target="_blank">change.json</a></li>`).join("")}
      </ul>`;
  }

  // ------------------------------------------------------------------ boot
  loadSaved().then(async (items) => {
    const params = new URLSearchParams(location.search);
    if (params.get("open")) { $("saved").value = params.get("open"); return openSaved(params.get("open")); }
    const first = items.find((i) => i.value.startsWith("series|")) || items[0];
    if (first) { $("saved").value = first.value; return openSaved(first.value); }
  });
})();
