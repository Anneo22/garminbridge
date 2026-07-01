// GarminBridge Content Manager: UI logic.
// Talks to the Python engine only through the Rust `api` command; never re-implements deletes.
const { invoke } = window.__TAURI__.core;

async function engine(args) {
  const raw = await invoke("api", { args });
  let data;
  try { data = JSON.parse(raw); }
  catch (e) { throw new Error("engine returned non-JSON: " + raw.slice(0, 200)); }
  if (data && data.ok === false) {
    const err = new Error(data.message || data.error || "engine error"); err.payload = data; throw err;
  }
  return data;
}

const state = {
  snap: null,
  filters: { loc: "on-watch", kind: "all", sport: "", search: "" },
  selected: new Set(),
  staleDismissed: false,
  thumbs: {},              // course id -> {vb,d} filled lazily from Connect geoPoints
  thumbsTried: new Set(),  // ids we've already asked for (success or not); never refetch
  windWhen: "",            // "" = off, else a ride-time slot key; forecast wind is opt-in
  wind: {},                // route uid -> {deg,speed} for the chosen hour (deg = blows FROM)
  windTried: new Set(),    // uids fetched for the current slot; reset when the slot changes
};

// sport -> vendored Tabler line icon
const SPORT = {
  running: "run", treadmill_running: "run", trail_running: "run", track_running: "run",
  cycling: "bike", road_biking: "bike", mountain_biking: "bike", indoor_cycling: "bike", gravel_cycling: "bike",
  swimming: "swimming", lap_swimming: "swimming", open_water_swimming: "swimming",
  strength_training: "barbell", strength: "barbell",
  hiit: "flame", cardio: "heart-rate-monitor",
  hiking: "mountain", walking: "walk",
  mobility: "stretching", pilates: "stretching", yoga: "stretching",
};
const sportIcon = (s) => SPORT[s] || "activity";
const prettySport = (s) => (s ? s.replace(/_/g, " ") : "no sport");
const kindLabel = (k) => (k === "workout" ? "workout" : "route");

const $ = (id) => document.getElementById(id);

function setIcon(el, name) {
  const u = `url('assets/icons/${name}.svg')`;
  el.style.webkitMaskImage = u; el.style.maskImage = u;
}
function icon(name, cls) {
  const s = el("span", { class: "ico" + (cls ? " " + cls : "") });
  setIcon(s, name); return s;
}
function svgEl(markup) {
  const t = document.createElement("div"); t.innerHTML = markup.trim(); return t.firstElementChild;
}

// A route's GPS shape as line art (the engine gives {vb, d, start, end, km, prof,
// dist_m, asc_m, desc_m}). The row shows a tiny glyph; hovering it opens a bigger
// map with direction, km markers, a metrics strip and an elevation profile.
const SVGNS = "http://www.w3.org/2000/svg";
function svgNode(tag, attrs) {
  const n = document.createElementNS(SVGNS, tag);
  for (const k in attrs) n.setAttribute(k, attrs[k]);
  return n;
}
const pathPoints = (d) => {
  const n = (d.match(/-?\d+(?:\.\d+)?/g) || []).map(Number), out = [];
  for (let i = 0; i + 1 < n.length; i += 2) out.push([n[i], n[i + 1]]);
  return out;
};
// Points at the given path-length fractions, each with the local travel angle,
// for placing a couple of small direction markers.
function directionArrows(pts, fracs) {
  if (pts.length < 2) return [];
  const cum = [0];
  for (let i = 1; i < pts.length; i++) cum.push(cum[i - 1] + Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]));
  const total = cum[cum.length - 1] || 1;
  return fracs.map((f) => {
    const target = total * f;
    let i = 1; while (i < cum.length - 1 && cum[i] < target) i++;
    const p0 = pts[i - 1], p1 = pts[i];
    return { x: p1[0], y: p1[1], angle: Math.atan2(p1[1] - p0[1], p1[0] - p0[0]) * 180 / Math.PI };
  });
}
// small map (row glyph): just the line + a start dot so it reads as oriented
function glyphSvg(thumb) {
  const svg = svgNode("svg", { viewBox: thumb.vb, class: "route-trace", "aria-hidden": "true" });
  svg.appendChild(svgNode("path", { d: thumb.d, class: "route-line" }));
  if (thumb.start) svg.appendChild(svgNode("circle", { cx: thumb.start[0], cy: thumb.start[1], r: 1.6, class: "route-start" }));
  return svg;
}
// big map (preview): line + two small direction arrowheads + km markers + start dot.
// Direction is carried mostly by the row's compass flag now, so the map stays calm:
// a small start dot and a couple of filled arrowheads pointing the way round.
function bigMapSvg(thumb) {
  const svg = svgNode("svg", { viewBox: thumb.vb, class: "route-trace-big", "aria-hidden": "true" });
  svg.appendChild(svgNode("path", { d: thumb.d, class: "route-line" }));
  for (const a of directionArrows(pathPoints(thumb.d), [0.42, 0.82]))
    svg.appendChild(svgNode("path", { d: "M0 0L-2 -1.15L-2 1.15Z", class: "route-arrow",
      transform: `translate(${a.x} ${a.y}) rotate(${a.angle.toFixed(0)})` }));
  for (const [x, y, km] of (thumb.km || [])) {
    svg.appendChild(svgNode("circle", { cx: x, cy: y, r: 0.7, class: "route-km-dot" }));
    const t = svgNode("text", { x: x, y: y - 1.7, "font-size": "2.7", class: "route-km-label" }); t.textContent = String(km);
    svg.appendChild(t);
  }
  if (thumb.start) svg.appendChild(svgNode("circle", { cx: thumb.start[0], cy: thumb.start[1], r: 0.95, class: "route-start" }));
  return svg;
}
function profSvg(prof) {
  const svg = svgNode("svg", { viewBox: prof.vb, class: "route-prof", preserveAspectRatio: "none", "aria-hidden": "true" });
  const pts = pathPoints(prof.d), ph = 24;
  if (pts.length) {
    const area = `M${pts[0][0]} ${ph}` + pts.map((p) => `L${p[0]} ${p[1]}`).join("") + `L${pts[pts.length - 1][0]} ${ph}Z`;
    svg.appendChild(svgNode("path", { d: area, class: "route-prof-area" }));
  }
  svg.appendChild(svgNode("path", { d: prof.d, class: "route-prof-line" }));
  return svg;
}
const fmtDist = (m) => (m >= 1000 ? (m / 1000).toFixed(m >= 100000 ? 0 : 1) + " km" : Math.round(m) + " m");
const fmtM = (m) => Math.round(m).toLocaleString("en-US") + " m";
// The route's outbound direction: an unambiguous compass arrow glyph + the label.
// (8-point compass -> the matching arrow; clearer at small size than a rotated shape.)
const COMPASS_ARROW = { N: "↑", NE: "↗", E: "→", SE: "↘", S: "↓", SW: "↙", W: "←", NW: "↖" };
function dirBadge(bearing, compass) {
  return el("span", { class: "route-dir", title: `Heads ${compass}` }, [
    el("span", { class: "dir-glyph", text: COMPASS_ARROW[compass] || "" }),
    document.createTextNode(compass),
  ]);
}
// ---- forecast wind (the thing cyclists check before a ride: is the wind on my
// back for the way home?). The route heads out toward `bearing`, so the ride back
// runs the opposite way; a tailwind home means the wind blows FROM the outbound
// direction. deg is the compass bearing the wind blows FROM (open-meteo convention).
const DEG_COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];
const degCompass = (d) => DEG_COMPASS[Math.round(d / 45) % 8];
const angDiff = (a, b) => { const x = Math.abs(a - b) % 360; return x > 180 ? 360 - x : x; };
function windVerdict(fromDeg, outBearing) {
  const d = angDiff(fromDeg, outBearing);        // 0 = wind straight from where you headed out
  return d <= 45 ? "tail" : d >= 135 ? "head" : "cross";
}
const VERDICT_WORD = { tail: "Tailwind home", head: "Headwind home", cross: "Crosswind home" };
// the wind at a route's start for the chosen slot, if fetched (keyed by row uid)
const rowWind = (r) => (state.windWhen && state.wind[r.uid]) || null;
// How to dress: map the WMO weather code to a line glyph + a short label.
function weatherIcon(code) {
  if (code == null) return "cloud";
  if (code <= 1) return "sun";           // clear / mainly clear
  if (code <= 3) return "cloud";         // partly cloudy / overcast
  if (code <= 48) return "cloud-fog";    // fog
  if (code <= 67) return "cloud-rain";   // drizzle + rain
  if (code <= 77) return "cloud-snow";   // snow
  if (code <= 82) return "cloud-rain";   // rain showers
  if (code <= 86) return "cloud-snow";   // snow showers
  return "cloud-storm";                  // thunderstorm
}
const WX_LABEL = [[1, "Clear"], [3, "Cloudy"], [48, "Fog"], [67, "Rain"], [77, "Snow"], [82, "Showers"], [86, "Snow"], [99, "Thunderstorm"]];
const weatherLabel = (code) => (code == null ? "" : (WX_LABEL.find(([m]) => code <= m) || [, ""])[1]);
const tempStr = (t) => (t == null ? "" : `${t}°`);
const pad2 = (n) => String(n).padStart(2, "0");
const fmtClock = (d) => `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
// Is it going to rain, and WHEN: the local clock time rain becomes likely for the chosen
// slot ("now" if already, null if it stays dry for the next few hours). rain_in is the
// engine's hours-from-slot count, so onset = the slot's time + that many hours.
function rainAt(w) {
  if (w.rain_in == null) return null;
  if (w.rain_in === 0) return "now";
  return fmtClock(new Date(windSlotDate(state.windWhen).getTime() + w.rain_in * 3600e3));
}
function rainToken(w) {
  const at = rainAt(w);
  if (at == null) return null;
  return el("span", { class: "rm-clip cond-rain", title: w.rain_prob != null ? `Rain likely, ${w.rain_prob}% chance` : "Rain likely" },
    [icon("droplet"), document.createTextNode(at === "now" ? "rain now" : `rain ${at}`)]);
}
// classify the extra conditions an athlete plans around (shown in the preview detail)
const uvLevel = (uv) => (uv < 3 ? "low" : uv < 6 ? "moderate" : uv < 8 ? "high" : uv < 11 ? "very high" : "extreme");
const aqiLevel = (a) => (a < 20 ? "good" : a < 40 ? "fair" : a < 60 ? "moderate" : a < 80 ? "poor" : a < 100 ? "very poor" : "hazardous");
const pollenLevel = (p) => (p < 20 ? "low" : p < 50 ? "moderate" : p < 100 ? "high" : "very high");
const rainWeight = (mm) => (mm < 2.5 ? "light" : mm < 7.6 ? "moderate" : "heavy");
// Will you have daylight, or need bike lights? sunset is a UTC ISO string; localise it and
// work out the light left from the chosen slot.
function daylightLabel(w) {
  if (!w.sunset) return null;
  const set = new Date(w.sunset + "Z");
  const left = (set.getTime() - windSlotDate(state.windWhen).getTime()) / 3600e3;
  const clock = fmtClock(set);
  if (left <= 0) return `Dark, set ${clock}`;
  if (left < 1) return `Sets ${clock}, under 1h light`;
  return `Sets ${clock}, ${Math.round(left)}h light`;
}

// The forecast conditions line (row line 2, present only when a slot is picked): a
// weather glyph + temp, feels-like, the wind's strength (km/h) and where it blows from,
// then the tailwind-home verdict. It answers "how do I dress, and is the wind on my back
// on the way home?". Teal is kept for the good case only (tailwind).
function conditionsLine(w, bearing) {
  const items = [];
  const wx = [icon(weatherIcon(w.code))];
  if (w.temp != null) wx.push(document.createTextNode(tempStr(w.temp)));
  items.push(el("span", { class: "rm-clip cond-wx", title: weatherLabel(w.code) }, wx));
  if (w.feels != null && w.temp != null && w.feels !== w.temp)
    items.push(el("span", { class: "cond-feels", text: `feels ${tempStr(w.feels)}` }));
  const rain = rainToken(w);
  if (rain) items.push(rain);
  items.push(el("span", { class: "rm-clip", title: `Wind from ${degCompass(w.deg)}` },
    [icon("wind"), document.createTextNode(`${w.speed} km/h`)]));
  const v = windVerdict(w.deg, bearing);
  items.push(el("span", { class: "route-wind wind-" + v, text: VERDICT_WORD[v] }));
  return el("div", { class: "row-meta row-metrics row-conditions" }, items);
}
const rowThumb = (r) => r.thumb || state.thumbs[r.id] || null;
// Line 2 for a route row, only when a forecast slot is picked and its wind is loaded.
function conditionsRow(r) {
  const t = rowThumb(r), w = rowWind(r);
  if (r.kind !== "course" || !w || !t || t.bearing == null) return null;
  return conditionsLine(w, t.bearing);
}

// The route row's data line (in the list, no hover needed): distance, ascent, descent,
// and the direction flag. (Forecast conditions live on a separate line below.)
function routeMetrics(thumb) {
  const items = [el("span", { text: fmtDist(thumb.dist_m) })];
  if (thumb.asc_m != null) items.push(el("span", { class: "rm-clip" }, [icon("arrow-up-right"), document.createTextNode(fmtM(thumb.asc_m))]));
  if (thumb.desc_m != null) items.push(el("span", { class: "rm-clip" }, [icon("arrow-down-right"), document.createTextNode(fmtM(thumb.desc_m))]));
  if (thumb.compass) items.push(dirBadge(thumb.bearing, thumb.compass));
  return el("div", { class: "row-meta row-metrics" }, items);
}
// A row's second line: route data when we have it, else the kind + sport.
function metaNode(r) {
  const t = rowThumb(r);
  if (r.kind === "course" && t && t.dist_m != null) return routeMetrics(t);
  return el("div", { class: "row-meta", text: kindLabel(r.kind) + "  ·  " + prettySport(r.sport) });
}

// Hover a route's little trace -> a larger preview card. One shared card,
// positioned next to the hovered glyph (flips left near the window edge).
let _preview = null, _previewTimer = null;
function windLine(w, bearing) {
  const v = windVerdict(w.deg, bearing);
  const label = weatherLabel(w.code);
  const feels = w.feels != null && w.temp != null && w.feels !== w.temp ? `, feels ${tempStr(w.feels)}` : "";
  const tempPart = w.temp != null ? `${tempStr(w.temp)}${feels}${label ? " · " + label : ""}` : label;
  const gustPart = w.gust != null && w.gust >= w.speed + 5 ? `, gusts ${w.gust}` : "";
  const at = rainAt(w);
  const rainPart = at == null ? "" : (at === "now" ? "Rain now" : `Rain from ${at}`)
    + (w.rain_prob != null ? `, ${w.rain_prob}%` : "") + (w.rain_mm != null ? ` · ${rainWeight(w.rain_mm)}` : "");
  const skyPart = [
    w.uv != null && w.uv >= 1 ? `UV ${w.uv} (${uvLevel(w.uv)})` : "",
    daylightLabel(w) || "",
  ].filter(Boolean).join(" · ");
  const airPart = [
    w.aqi != null ? `Air ${aqiLevel(w.aqi)}` : "",
    w.pollen != null ? `pollen ${pollenLevel(w.pollen)}` : "",
  ].filter(Boolean).join(" · ");
  const line = (t) => (t ? el("div", { class: "wind-detail", text: t }) : null);
  return el("div", { class: "route-wind-line wind-" + v }, [
    icon(weatherIcon(w.code)),
    el("div", { class: "wind-text" }, [
      line(tempPart),
      line(rainPart),
      line(`${w.deg}° · ${w.speed} km/h${gustPart}`),
      line(skyPart),
      line(airPart),
      el("div", { class: "wind-verdict", text: VERDICT_WORD[v] }),
    ]),
  ]);
}
function showPreview(slot, thumb, r) {
  if (!_preview) { _preview = el("div", { class: "route-preview" }); document.body.appendChild(_preview); }
  const kids = [bigMapSvg(thumb), el("div", { class: "route-preview-name", text: (r && r.name) || "Route" })];
  const w = r && rowWind(r);
  if (w && thumb.bearing != null) kids.push(windLine(w, thumb.bearing));
  if (thumb.prof) kids.push(profSvg(thumb.prof));
  _preview.replaceChildren(...kids);
  _preview.classList.add("show");
  const s = slot.getBoundingClientRect();
  const cw = _preview.offsetWidth, ch = _preview.offsetHeight;
  let left = s.right + 12;
  if (left + cw > window.innerWidth - 8) left = s.left - cw - 12;   // flip to the left near the edge
  let top = s.top + s.height / 2 - ch / 2;
  _preview.style.left = Math.max(8, left) + "px";
  _preview.style.top = Math.max(8, Math.min(top, window.innerHeight - ch - 8)) + "px";
}
function hidePreview() { clearTimeout(_previewTimer); if (_preview) _preview.classList.remove("show"); }

// The row identity cell: for routes, the sport icon AND the little trace glyph
// side by side (workouts: just the sport icon, so icons align across all rows).
// Routes with no trace yet get the mountain fallback (the async fill swaps it in).
function identityNode(r) {
  const sportSpan = el("span", { class: "sport-ico" }, [icon(sportIcon(r.sport))]);
  if (r.kind !== "course") return el("span", { class: "ident" }, [sportSpan]);
  const slot = el("span", { class: "route-slot" });
  if (r.id) slot.dataset.thumbId = String(r.id);
  const thumb = rowThumb(r);
  slot.appendChild(thumb ? glyphSvg(thumb) : icon("mountain"));
  // hover the trace -> enlarge it (reads the thumb live, so it works once the
  // async fill lands too); guarded so the mountain-fallback state does nothing
  slot.addEventListener("mouseenter", () => {
    clearTimeout(_previewTimer);
    const t = rowThumb(r);
    if (t) _previewTimer = setTimeout(() => showPreview(slot, t, r), 110);
  });
  slot.addEventListener("mouseleave", hidePreview);
  return el("span", { class: "ident" }, [sportSpan, slot]);
}

function el(tag, props = {}, kids = []) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") n.className = v;
    else if (k === "text") n.textContent = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (v === true) n.setAttribute(k, "");
    else if (v !== false && v != null) n.setAttribute(k, v);
  }
  for (const c of [].concat(kids)) if (c) n.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  return n;
}

function relTime(iso) {
  if (!iso) return "never";
  const d = (Date.now() - new Date(iso).getTime()) / 1000;
  if (d < 90) return "just now";
  if (d < 3600) return Math.round(d / 60) + " min ago";
  if (d < 86400) return Math.round(d / 3600) + " h ago";
  return Math.round(d / 86400) + " d ago";
}

// ---- line-art (Visualize Value style) ----
function dinkus() {
  return el("div", { class: "dinkus" }, [svgEl(`
    <svg viewBox="0 0 50 12" fill="none" stroke="currentColor" stroke-width="1">
      <line x1="2" y1="6" x2="19" y2="6" stroke-linecap="round"/>
      <rect class="node" x="22.4" y="3.4" width="5.2" height="5.2" transform="rotate(45 25 6)" stroke="none"/>
      <line x1="31" y1="6" x2="48" y2="6" stroke-linecap="round"/>
    </svg>`)]);
}
function illoEmpty() {
  // the brand bridge at illustration scale: watch and Mac anchors, a dashed span (nothing flowing),
  // the teal data-node at the apex.
  return svgEl(`
    <svg class="illo" viewBox="0 0 140 100" fill="none" stroke="currentColor" stroke-width="1.6">
      <g opacity=".7">
        <circle cx="28" cy="74" r="9"/>
        <rect x="96" y="63" width="20" height="17" rx="3"/>
      </g>
      <path d="M28 74 C28 33 44 21 70 21 C96 21 112 33 112 74" stroke-dasharray="3 6.5" stroke-linecap="round"/>
      <circle class="node" cx="70" cy="21" r="7"/>
    </svg>`);
}

// ---- load + render ----
async function load(refresh = false) {
  if (refresh) showScan("Scanning your Fenix over USB");
  else renderSkeleton();
  try {
    state.snap = await engine(refresh ? ["snapshot", "--refresh"] : ["snapshot"]);
    state.selected.clear();
    populateSports();
    render();
  } catch (e) {
    $("list").replaceChildren(emptyState("Could not reach the engine", e.message));
  } finally {
    hideScan();
  }
}

function renderSkeleton() {
  const list = $("list");
  list.replaceChildren();
  for (let i = 0; i < 9; i++) {
    list.appendChild(el("div", { class: "sk-row" }, [
      el("div", { class: "sk sk-ico" }),
      el("div", { class: "sk sk-line", style: `width:${40 + (i * 37) % 45}%` }),
      el("div", { class: "sk sk-chip" }),
    ]));
  }
}

function populateSports() {
  const sel = $("filter-sport");
  const cur = sel.value;
  sel.replaceChildren(el("option", { value: "", text: "All sports" }));
  for (const s of state.snap.sports) sel.appendChild(el("option", { value: s, text: prettySport(s) }));
  sel.value = cur && state.snap.sports.includes(cur) ? cur : "";
}

function visibleRows() {
  const f = state.filters;
  return state.snap.items.filter((r) => {
    if (f.kind !== "all" && r.kind !== f.kind) return false;
    if (f.sport && r.sport !== f.sport) return false;
    if (f.search && !(r.name || "").toLowerCase().includes(f.search.toLowerCase())) return false;
    if (f.loc === "on-watch") return r.on_watch;
    if (f.loc === "connect-only") return r.state === "connect-only";
    if (f.loc === "watch-only") return r.state === "watch-only";
    return true;
  });
}

function render() {
  hidePreview();
  renderWatchPill();
  renderStaleBanner();
  const rows = visibleRows();
  const list = $("list");
  list.replaceChildren();

  const groups = ["workout", "course"]
    .map((kind) => [kind, rows.filter((r) => r.kind === kind).sort((a, b) => (a.name || "").localeCompare(b.name || ""))])
    .filter(([, g]) => g.length);
  const places = placesVisible();

  if (!groups.length && !places.length) {
    list.appendChild(emptyState("Nothing here", "No workouts, routes or places match these filters."));
  } else {
    groups.forEach(([kind, group], gi) => {
      if (gi > 0) list.appendChild(dinkus());
      list.appendChild(el("div", { class: "group-head" }, [
        el("span", { class: "group-title", text: kind === "workout" ? "Workouts" : "Routes" }),
        el("span", { class: "group-count", text: String(group.length) }),
        el("span", { class: "rule" }),
      ]));
      for (const r of group) list.appendChild(rowNode(r));
    });
    if (places.length) {
      if (groups.length) list.appendChild(dinkus());
      list.appendChild(el("div", { class: "group-head" }, [
        el("span", { class: "group-title", text: "Places" }),
        el("span", { class: "group-count", text: String(places.length) }),
        el("span", { class: "rule" }),
      ]));
      for (const p of places) list.appendChild(placeRow(p));
    }
  }
  renderBulkBar();
  renderStatus(rows);
  fillThumbs(rows);
  fetchWind(rows);
}

// Fill route shapes + data the snapshot had no local trace for by fetching the
// Connect geoPoints once (batched, in the background), then re-rendering so both
// the glyph and the row's data line pick them up. Cached forever; snapshot stays fast.
async function fillThumbs(rows) {
  const want = rows.filter((r) => r.kind === "course" && r.id && !rowThumb(r)
    && !state.thumbsTried.has(String(r.id)));
  if (!want.length) return;
  const ids = [...new Set(want.map((r) => String(r.id)))];
  ids.forEach((id) => state.thumbsTried.add(id));
  let res;
  try { res = await engine(["thumbs", "--ids", ids.join(",")]); }
  catch { return; }  // best-effort: rows keep the mountain fallback
  const got = (res && res.thumbs) || {};
  if (!Object.keys(got).length) return;
  Object.assign(state.thumbs, got);
  render();  // thumbsTried now covers these ids, so this render won't refetch
}

// A ride-time slot -> the concrete local Date to forecast for (snapped to the hour).
function windSlotDate(slot) {
  const d = new Date();
  d.setMinutes(0, 0, 0);
  if (slot === "now") return d;
  if (slot === "in3h") { d.setHours(d.getHours() + 3); return d; }
  const at = (addDays, hour) => { const x = new Date(); x.setDate(x.getDate() + addDays); x.setHours(hour, 0, 0, 0); return x; };
  if (slot === "tomAM") return at(1, 8);
  if (slot === "tomPM") return at(1, 15);
  if (slot === "in2d") return at(2, 15);
  return d;
}
// The engine matches forecast hours in UTC, so send the target as a UTC hour string.
function utcHour(dt) {
  const p = (n) => String(n).padStart(2, "0");
  return `${dt.getUTCFullYear()}-${p(dt.getUTCMonth() + 1)}-${p(dt.getUTCDate())}T${p(dt.getUTCHours())}:00`;
}

// Fetch the forecast wind at each visible route's start for the chosen slot, in one
// batched engine call, then re-render so rows + preview show the tailwind verdict.
// Opt-in (state.windWhen) and never part of the snapshot: wind changes hourly.
async function fetchWind(rows) {
  if (!state.windWhen) return;
  const want = rows.filter((r) => {
    const t = rowThumb(r);
    return r.kind === "course" && t && t.slat != null && t.bearing != null && !state.windTried.has(r.uid);
  });
  if (!want.length) return;
  want.forEach((r) => state.windTried.add(r.uid));
  const q = want.map((r) => { const t = rowThumb(r); return `${r.uid},${t.slat},${t.slon}`; }).join(";");
  const hour = utcHour(windSlotDate(state.windWhen));
  let res;
  try { res = await engine(["wind", "--q", q, "--hour", hour]); }
  catch { return; }  // best-effort: rows just show no wind
  const got = (res && res.wind) || {};
  if (!Object.keys(got).length) return;
  Object.assign(state.wind, got);
  render();
}

function setWindSlot(slot) {
  if (slot === state.windWhen) return;
  state.windWhen = slot;
  state.wind = {};
  state.windTried = new Set();   // a new hour: forget what we fetched for the old one
  render();
}

function emptyState(title, sub) {
  return el("div", { class: "empty" }, [
    illoEmpty(), el("div", { class: "empty-title", text: title }), el("div", { class: "empty-sub", text: sub }),
  ]);
}

// ---- saved points ("Places"): the third content type. They live only on the watch + the
// Mac backup (never Connect), so no sport, no thumb, no Connect/Watch badges, just a name,
// coordinates, rename and delete. Rename/delete pull the LIVE file off the watch, patch it,
// verify, then write it back and sync the backup (the apply path enforces the watch is on USB).
function fmtCoord(lat, lon) {
  if (lat == null || lon == null) return "No coordinates";
  const la = `${Math.abs(lat).toFixed(4)}°${lat >= 0 ? "N" : "S"}`;
  const lo = `${Math.abs(lon).toFixed(4)}°${lon >= 0 ? "E" : "W"}`;
  return `${la}   ${lo}`;
}
function placesVisible() {
  const f = state.filters, loc = state.snap.locations;
  if (!loc || !loc.points || !loc.points.length) return [];
  if (f.kind !== "all" && f.kind !== "location") return [];
  // places aren't in Connect and have no sport; hide them where those filters are the point
  if (f.kind === "all" && (f.loc === "connect-only" || f.loc === "watch-only" || f.sport)) return [];
  const q = (f.search || "").toLowerCase();
  return loc.points.filter((p) => !q || (p.name || "").toLowerCase().includes(q));
}
function placeRow(p) {
  const rename = el("button", { class: "row-rename", title: "Rename", "aria-label": "Rename " + (p.name || "") }, [icon("pencil")]);
  rename.addEventListener("click", () => openRenamePlace(p));
  const del = el("button", { class: "row-del", title: "Delete saved point", "aria-label": "Delete " + (p.name || "") }, [icon("trash")]);
  del.addEventListener("click", () => deletePlace(p));
  return el("div", { class: "row place-row" }, [
    el("span", { class: "place-gap" }),
    el("span", { class: "ident" }, [el("span", { class: "sport-ico" }, [icon("map-pin")])]),
    el("div", { class: "row-main" }, [
      el("div", { class: "row-name", title: p.name || "", text: p.name || "Unnamed" }),
      el("div", { class: "row-meta place-coords", text: fmtCoord(p.lat, p.lon) }),
    ]),
    el("div", { class: "place-actions" }, [rename, del]),
  ]);
}

function rowNode(r) {
  const selected = state.selected.has(r.uid);
  const check = el("input", { type: "checkbox", class: "row-check" });
  check.checked = selected;
  if (!r.actions.can_rm_watch) check.disabled = true;
  check.addEventListener("change", () => {
    if (check.checked) state.selected.add(r.uid); else state.selected.delete(r.uid);
    row.classList.toggle("is-selected", check.checked);
    renderBulkBar();
  });

  const connectBadge = badge("plug-connected", "Connect", r.in_connect, r.in_connect || r.watch_known,
    r.actions.can_rm_connect, () => onConnectBadge(r));
  const watchBadge = badge("device-watch", "Watch", r.on_watch, r.watch_known,
    r.actions.can_rm_watch || r.actions.can_add_to_watch, () => onWatchBadge(r));

  // Rename is keyed by the Connect id (must be in Connect); scheduled plan files re-sync,
  // so renaming them is futile; leave it disabled there.
  const canRename = !!(r.in_connect && r.id && !r.scheduled);
  const renameBtn = el("button", { class: "row-rename", title: "Rename", "aria-label": "Rename " + (r.name || "") }, [icon("pencil")]);
  if (canRename) renameBtn.addEventListener("click", () => openRename(r));
  else renameBtn.disabled = true;

  const stale = r.stale === "orphan";
  const locClass = "locchip " + (stale ? "is-stale" : r.state === "synced" ? "is-synced" : "");
  const row = el("div", { class: "row" + (selected ? " is-selected" : "") }, [
    check,
    identityNode(r),
    el("div", { class: "row-main" }, [
      el("div", { class: "row-name", title: r.name || "", text: r.name || "Unnamed" }),
      metaNode(r),
      conditionsRow(r),
    ]),
    el("div", { class: "badges" }, [connectBadge, watchBadge]),
    el("span", { class: locClass, title: r.location_detail, text: stale ? "Stale route" : r.location_label }),
    renameBtn,
  ]);
  return row;
}

function badge(iconName, label, on, known, enabled, onClick) {
  const cls = "badge " + (on ? "on" : known ? "off" : "unknown");
  const b = el("button", { class: cls, title: label }, [icon(iconName), label]);
  if (!enabled) b.disabled = true; else b.addEventListener("click", onClick);
  return b;
}

function renderWatchPill() {
  const w = state.snap.watch;
  const pill = $("watch-pill"), txt = $("watch-pill-text");
  pill.classList.remove("is-live", "is-cache", "is-none");
  if (w.connected) {
    pill.classList.add("is-live");
    txt.textContent = w.source === "live" ? "Watch connected" : "Connected. Refresh for live";
  } else if (w.source === "cache" || w.source === "manifest-cache") {
    pill.classList.add("is-cache");
    txt.textContent = "Last seen " + relTime(w.captured_at);
  } else {
    pill.classList.add("is-none");
    txt.textContent = "Watch not scanned yet";
  }
}

function renderStaleBanner() {
  const s = state.snap.stale_routes;
  const banner = $("stale-banner");
  if (!s.orphan_count || state.staleDismissed) { banner.hidden = true; return; }
  banner.hidden = false;
  $("stale-text").replaceChildren(
    document.createTextNode(`${s.orphan_count} stale route${s.orphan_count > 1 ? "s" : ""} left on your Fenix by Garmin's `),
    el("em", { text: "one-way sync" }), document.createTextNode("."));
}

function renderBulkBar() {
  const n = state.selected.size;
  $("bulkbar").hidden = n === 0;
  $("bulk-count").textContent = n === 1 ? "1 selected" : `${n} selected`;
}

function renderStatus(rows) {
  const c = state.snap.counts;
  const onW = (k) => c[k].synced + c[k]["watch-only"] + c[k].scheduled;
  const np = (state.snap.locations && state.snap.locations.count) || 0;
  const showing = rows.length + placesVisible().length;
  $("status-counts").textContent =
    `On watch: ${onW("workout")} workouts · ${onW("course")} routes · ${np} places · showing ${showing}`;
  $("status-updated").textContent =
    "Watch data: " + (state.snap.watch.source === "live" ? "live" : "from " + relTime(state.snap.watch.captured_at));
}

// ---- badge actions ----
function onConnectBadge(r) {
  if (r.in_connect && r.actions.can_rm_connect) confirmAction("rm-connect", r.kind, { id: r.id });
}
function onWatchBadge(r) {
  if (r.on_watch && r.actions.can_rm_watch) confirmAction("rm-watch", r.kind, { watchFile: r.watch_file });
  else if (!r.on_watch && r.actions.can_add_to_watch) confirmAction("add-to-watch", r.kind, { id: r.id });
}

function selArgs(s) {
  const a = [];
  if (s.id != null) a.push("--id", String(s.id));
  if (s.watchFile) a.push("--watch-file", s.watchFile);
  if (s.name) a.push("--name", s.name);
  if (s.state) a.push("--state", s.state);
  if (s.index != null) a.push("--index", String(s.index));
  if (s.expect != null) a.push("--expect", s.expect);
  return a;
}

async function confirmAction(action, kind, selectors) {
  let pv;
  try { pv = await engine(["preview", "--action", action, "--kind", kind, ...selArgs(selectors)]); }
  catch (e) { return toast(e.message, true); }
  if (!pv.count) return toast("Nothing to change.");
  openConfirm(pv, [["apply", action, kind, selectors]]);
}

function openConfirm(pv, jobs) {
  const lead = `${pv.verb}. ${pv.count === 1 ? "1 item" : pv.count + " items"}:`;
  const list = pv.changes.map((c) => el("li", {}, [
    el("span", { class: "nm", text: c.name || "Unnamed" }),
    el("span", { class: "eff", text: c.effect }),
  ]));
  const blocked = pv.needs_live_watch && !pv.watch_connected;
  let warn = null, warnClass = "reversible";
  if (blocked) { warn = pv.kind === "location" ? "Plug your Fenix in over USB to change its saved points." : "Plug your Fenix in over USB to change what is on the watch."; warnClass = "permanent"; }
  else if (pv.kind === "location" && pv.permanent) { warn = "This permanently removes the saved point from your Fenix and the Mac backup. There is no undo."; warnClass = "permanent"; }
  else if (pv.permanent) { warn = "This permanently deletes from Garmin Connect. There is no trash, and it does not remove the copy on your watch."; warnClass = "permanent"; }
  else if (pv.action === "rm-watch" || pv.action === "clean-watch") warn = "Removed from the watch only. It stays in Garmin Connect, so you can re-add it.";
  else if (pv.action === "add-to-watch") warn = "Copied onto the watch. It stays in Garmin Connect too.";

  openModal({
    title: pv.verb, lead, list,
    warn, warnClass, warnIcon: warnClass === "permanent" ? "alert-triangle" : "device-watch",
    confirmLabel: pv.permanent ? "Delete permanently" : pv.verb,
    confirmDanger: pv.permanent || pv.action === "rm-watch" || pv.action === "clean-watch",
    blocked, onConfirm: () => runJobs(jobs, pv.needs_live_watch),
  });
}

async function runJobs(jobs, needsLive) {
  closeModal();
  showScan("Applying");
  let fail = 0, lastMsg = "";
  for (const [, action, kind, selectors] of jobs) {
    try {
      const res = await engine(["apply", "--action", action, "--kind", kind, ...selArgs(selectors)]);
      if (!res.ok) { fail++; lastMsg = res.message || res.output || "failed"; }
    } catch (e) { fail++; lastMsg = e.message; }
  }
  hideScan();
  toast(fail ? `Done with ${fail} issue(s): ${lastMsg}` : "Done.", fail > 0);
  await load(needsLive);
}

function bulkRemove() {
  const rows = state.snap.items.filter((r) => state.selected.has(r.uid) && r.actions.can_rm_watch);
  if (!rows.length) return;
  const byKind = {};
  for (const r of rows) (byKind[r.kind] ||= []).push(r);
  const connected = state.snap.watch.connected;

  openModal({
    title: "Remove from your Fenix",
    lead: `Remove ${rows.length === 1 ? "1 item" : rows.length + " items"} from the watch:`,
    list: rows.map((r) => el("li", {}, [
      el("span", { class: "nm", text: r.name || "Unnamed" }),
      el("span", { class: "eff", text: `deleted from ${r.folder} (stays in Garmin Connect)` }),
    ])),
    warn: connected ? "Removed from the watch only. They stay in Garmin Connect, so you can re-add them."
                    : "Plug your Fenix in over USB to change what is on the watch.",
    warnClass: connected ? "reversible" : "permanent",
    warnIcon: connected ? "device-watch" : "alert-triangle",
    confirmLabel: "Remove from watch", confirmDanger: true, blocked: !connected,
    onConfirm: () => runJobs(
      Object.entries(byKind).map(([kind, rs]) => ["apply", "rm-watch", kind, { watchFile: rs.map((r) => r.watch_file).join(",") }]), true),
  });
}

async function reviewStale() {
  let pv;
  try { pv = await engine(["preview", "--action", "clean-watch", "--kind", "course"]); }
  catch (e) { return toast(e.message, true); }
  if (!pv.count) return toast("No stale routes to clean right now.");
  openConfirm(pv, [["apply", "clean-watch", "course", {}]]);
}

// ---- rename (propagates to Connect + watch) ----
const utf8len = (s) => new TextEncoder().encode(s).length;

function openRename(r) {
  const oldName = r.name || "";
  const onWatch = r.on_watch && !r.scheduled;
  const needsCap = onWatch && r.kind === "course"; // routes have a real on-watch name-length ceiling
  let watchMax = null;                             // bytes; null = unknown / no backup FIT
  const input = el("input", { class: "rename-input", type: "text", value: oldName, spellcheck: "false", "aria-label": "New name" });
  const note = el("div", { class: "rename-note" });

  openModal({
    title: "Rename " + kindLabel(r.kind),
    lead: "Updates the name everywhere this " + kindLabel(r.kind) + " lives.",
    list: [el("li", { class: "rename-li" }, [input, note])],
    confirmLabel: "Rename", confirmDanger: false, blocked: true,
    onConfirm: () => doRename(r, input.value.trim()),
  });

  const cbtn = $("modal-confirm");
  function update() {
    const nv = input.value.trim();
    const changed = !!nv && nv !== oldName;
    cbtn.disabled = !changed;
    if (!nv) { note.textContent = "Enter a name."; note.className = "rename-note"; return; }
    if (!changed) { note.textContent = "Same as the current name."; note.className = "rename-note"; return; }
    let where = "Renames in Garmin Connect", warn = false;
    if (onWatch) {
      const fits = !needsCap || watchMax == null || utf8len(nv) <= watchMax;
      if (fits) where += " and on your Fenix";
      else { where = "Renames in Garmin Connect. On your Fenix the current name is kept: the new one is too long for the route file"; warn = true; }
    }
    note.textContent = where + ".";
    note.className = "rename-note" + (warn ? " warn" : "");
  }
  input.addEventListener("input", update);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !cbtn.disabled) doRename(r, input.value.trim()); });
  update();
  setTimeout(() => { input.focus(); input.select(); }, 30);

  // read the true watch ceiling (the on-watch route file's name-field size) in the
  // background; the modal is already usable, the note just sharpens when it arrives.
  if (needsCap) {
    engine(["preview", "--action", "rename", "--kind", "course", "--id", String(r.id), "--to", oldName])
      .then((pv) => { watchMax = pv.watch_name_max; update(); })
      .catch(() => {});
  }
}

async function doRename(r, newName) {
  if (!newName || newName === r.name) return;
  closeModal();
  showScan("Renaming");
  try {
    await engine(["apply", "--action", "rename", "--kind", r.kind, "--id", String(r.id), "--to", newName]);
    hideScan();
    toast("Renamed.");
  } catch (e) {
    hideScan();
    toast((e.payload && (e.payload.message || e.payload.output)) || e.message, true);
    return;
  }
  await load(r.on_watch && !r.scheduled); // live refresh only if the watch copy changed
}

// ---- saved-point rename + delete (both pull the live file off the watch) ----
function openRenamePlace(p) {
  const oldName = p.name || "";
  const input = el("input", { class: "rename-input", type: "text", value: oldName, spellcheck: "false", "aria-label": "New name" });
  const note = el("div", { class: "rename-note" });
  openModal({
    title: "Rename saved point",
    lead: "Renames this saved point on your Fenix and in the Mac backup.",
    list: [el("li", { class: "rename-li" }, [input, note])],
    confirmLabel: "Rename", confirmDanger: false, blocked: true,
    onConfirm: () => doRenamePlace(p, input.value.trim()),
  });
  const cbtn = $("modal-confirm");
  function update() {
    const nv = input.value.trim();
    const bytes = utf8len(nv);
    const changed = !!nv && nv !== oldName;
    const fits = bytes <= 32;      // saved-point name slot is a fixed 32 bytes
    cbtn.disabled = !changed || !fits;
    if (!nv) { note.textContent = "Enter a name."; note.className = "rename-note"; return; }
    if (!fits) { note.textContent = `Too long by ${bytes - 32}. Saved-point names fit about 32 characters.`; note.className = "rename-note warn"; return; }
    if (!changed) { note.textContent = "Same as the current name."; note.className = "rename-note"; return; }
    note.textContent = "Needs your Fenix on USB; updates it and the Mac backup.";
    note.className = "rename-note";
  }
  input.addEventListener("input", update);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !cbtn.disabled) doRenamePlace(p, input.value.trim()); });
  update();
  setTimeout(() => { input.focus(); input.select(); }, 30);
}

async function doRenamePlace(p, newName) {
  if (!newName || newName === p.name) return;
  closeModal();
  showScan("Renaming saved point");
  try {
    await engine(["apply", "--action", "rename", "--kind", "location",
      "--index", String(p.index), "--to", newName, "--expect", p.name || ""]);
    hideScan();
    toast("Renamed.");
  } catch (e) {
    hideScan();
    toast((e.payload && (e.payload.message || e.payload.output)) || e.message, true);
    return;
  }
  await load(true);   // re-list live so the new name (and healed backup) show
}

function deletePlace(p) {
  confirmAction("rm-location", "location", { index: p.index, expect: p.name });
}

// ---- modal + toast + scan ----
function openModal({ title, lead, list, warn, warnClass, warnIcon, confirmLabel, confirmDanger, blocked, onConfirm }) {
  $("modal-title").textContent = title;
  $("modal-lead").textContent = lead || "";
  $("modal-list").replaceChildren(...(list || []));
  const w = $("modal-warn");
  if (warn) {
    w.hidden = false; w.className = "modal-warn " + (warnClass || "reversible");
    w.replaceChildren(icon(warnIcon || "device-watch"), el("span", { text: warn }));
  } else w.hidden = true;
  const cbtn = $("modal-confirm");
  cbtn.textContent = confirmLabel || "Confirm";
  cbtn.className = "btn " + (confirmDanger ? "btn-danger" : "btn-primary");
  cbtn.disabled = !!blocked;
  cbtn.onclick = onConfirm;
  $("modal").hidden = false;
}
function closeModal() { $("modal").hidden = true; }

let toastTimer;
function toast(msg, isErr = false) {
  const t = $("toast");
  t.textContent = msg; t.className = "toast" + (isErr ? " err" : ""); t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), 3800);
}
function showScan(text) {
  $("scan-text").textContent = text || "";
  $("refresh-btn").classList.add("refreshing");
  $("scan").hidden = false;
}
function hideScan() { $("scan").hidden = true; $("refresh-btn").classList.remove("refreshing"); }

function segActive(groupId, btn) {
  for (const b of $(groupId).querySelectorAll(".seg-btn")) b.classList.toggle("is-active", b === btn);
}

window.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("[data-icon]").forEach((e) => setIcon(e, e.dataset.icon));
  $("refresh-btn").addEventListener("click", () => load(true));
  $("search").addEventListener("input", (e) => { state.filters.search = e.target.value; render(); });
  $("filter-loc").addEventListener("click", (e) => {
    const b = e.target.closest(".seg-btn"); if (!b) return;
    state.filters.loc = b.dataset.loc; segActive("filter-loc", b); render();
  });
  $("filter-kind").addEventListener("click", (e) => {
    const b = e.target.closest(".seg-btn"); if (!b) return;
    state.filters.kind = b.dataset.kind; segActive("filter-kind", b); render();
  });
  $("filter-sport").addEventListener("change", (e) => { state.filters.sport = e.target.value; render(); });
  $("wind-when").addEventListener("change", (e) => setWindSlot(e.target.value));
  $("bulk-remove").addEventListener("click", bulkRemove);
  $("bulk-clear").addEventListener("click", () => { state.selected.clear(); render(); });
  $("stale-review").addEventListener("click", reviewStale);
  $("stale-dismiss").addEventListener("click", () => { state.staleDismissed = true; renderStaleBanner(); });
  $("modal-cancel").addEventListener("click", closeModal);
  $("modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeModal(); });
  window.addEventListener("scroll", hidePreview, true);  // don't leave a stale preview mid-scroll
  load(false);
});
