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
  sort: "name",            // routes sort: name | dist_asc | dist_desc | recent | nearest
  anchor: null,            // start-place filter anchor { lat, lon, label, approximate? }
  radiusKm: 10,            // radius (km) around the anchor to keep routes within
};

// Great-circle distance in km between two lat/lon points.
function haversineKm(aLat, aLon, bLat, bLon) {
  const R = 6371, toR = Math.PI / 180;
  const dLat = (bLat - aLat) * toR, dLon = (bLon - aLon) * toR;
  const s = Math.sin(dLat / 2) ** 2 +
    Math.cos(aLat * toR) * Math.cos(bLat * toR) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.min(1, Math.sqrt(s)));
}
// A route's start distance from the current anchor (km), or null if either point is unknown.
function anchorDistKm(r) {
  const t = rowThumb(r);
  if (!state.anchor || !t || t.slat == null || t.slon == null) return null;
  return haversineKm(state.anchor.lat, state.anchor.lon, t.slat, t.slon);
}

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
// on the way home?". Klein is kept for the good case only (tailwind).
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
    [icon("wind"), document.createTextNode(`${w.speed} km/h from ${degCompass(w.deg)} (${w.deg}°)`)]));
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
  if (r.kind === "course" && t && t.dist_m != null) {
    const node = routeMetrics(t);
    const d = anchorDistKm(r);   // when a start-place filter is active, show how far the start is
    if (d != null) node.appendChild(el("span", { class: "route-anchor-dist rm-clip",
      title: "Starts " + (d < 10 ? d.toFixed(1) : Math.round(d)) + " km from " + state.anchor.label },
      [icon("map-pin"), document.createTextNode(`${d < 10 ? d.toFixed(1) : Math.round(d)} km away`)]));
    return node;
  }
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
      line(`From ${degCompass(w.deg)} (${w.deg}°) · ${w.speed} km/h${gustPart}`),
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
    else if (k === "style") n.style.cssText = v; // CSSOM, so the CSP needs no unsafe-inline
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
  // the brand mark at illustration scale: the clean bridge arch (a quiet ink stroke) with the
  // single faded hot-coral node at the apex. Same language as the masthead logo, so an empty
  // screen still reads as GarminBridge. (The earlier literal watch+Mac rebus was dropped.)
  return svgEl(`
    <svg class="illo" viewBox="0 0 140 100" fill="none">
      <path d="M20 84 C20 40 43 26 70 26 C97 26 120 40 120 84" stroke="currentColor"
            stroke-width="1.8" stroke-linecap="round"/>
      <circle class="node-glow" cx="70" cy="26" r="15" fill="url(#nodeGlow)"/>
      <circle class="node" cx="70" cy="26" r="6.4"/>
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
    // Start-place filter (routes only): keep courses whose start is within the radius of the
    // anchor. A route with no known start yet (thumb still loading) is hidden while filtering,
    // since we can't confirm it's near — it reappears once its trace fills in.
    if (state.anchor && r.kind === "course") {
      const d = anchorDistKm(r);
      if (d == null || d > state.radiusKm) return false;
    }
    if (f.loc === "on-watch") return r.on_watch;
    if (f.loc === "connect-only") return r.state === "connect-only";
    if (f.loc === "watch-only") return r.state === "watch-only";
    return true;
  });
}

// Order the routes group by the chosen sort. Workouts always stay alphabetical — sort is a
// route concern (distance, recency, proximity); only "name" is meaningful for workouts.
function sortRoutes(group) {
  const byName = (a, b) => (a.name || "").localeCompare(b.name || "");
  const asc = (v) => (v == null ? Infinity : v);   // unknowns last when sorting ascending
  const desc = (v) => (v == null ? -Infinity : v); // unknowns last when sorting descending
  const dist = (r) => { const t = rowThumb(r); return t ? t.dist_m : null; };
  const created = (r) => { const t = rowThumb(r); return t ? t.created : null; };
  const g = group.slice();
  if (state.sort === "dist_asc") g.sort((a, b) => asc(dist(a)) - asc(dist(b)) || byName(a, b));
  else if (state.sort === "dist_desc") g.sort((a, b) => desc(dist(b)) - desc(dist(a)) || byName(a, b));
  else if (state.sort === "recent") g.sort((a, b) => desc(created(b)) - desc(created(a)) || byName(a, b));
  else if (state.sort === "nearest") g.sort((a, b) => asc(anchorDistKm(a)) - asc(anchorDistKm(b)) || byName(a, b));
  else g.sort(byName);
  return g;
}

function render() {
  hidePreview();
  renderWatchPill();
  renderStaleBanner();
  const rows = visibleRows();
  const list = $("list");
  list.replaceChildren();

  const groups = ["workout", "course"]
    .map((kind) => {
      const g = rows.filter((r) => r.kind === kind);
      return [kind, kind === "course" ? sortRoutes(g) : g.sort((a, b) => (a.name || "").localeCompare(b.name || ""))];
    })
    .filter(([, g]) => g.length);
  const places = placesVisible();
  renderRouteToolbar(rows);

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

// ===================================================================================
// Route sort + filter-by-start-place. All geo lookups (geocode, "use my location")
// go through the engine — the webview CSP blocks external HTTP — so this file only
// holds UI + the km math. Anchor = a point (searched place / saved place / approx
// present position); routes are kept when their START is within `radiusKm` of it.
// ===================================================================================
function routesInView() {
  const k = state.filters.kind;
  return k === "all" || k === "course";
}

function renderRouteToolbar() {
  const tb = $("route-toolbar");
  if (!routesInView()) { tb.hidden = true; closeAnchorPop(); return; }
  tb.hidden = false;
  $("route-sort").value = state.sort;
  renderAnchorChip();
}

function renderAnchorChip() {
  const chip = $("anchor-chip");
  if (!state.anchor) { chip.hidden = true; return; }
  chip.hidden = false;
  const approx = state.anchor.approximate ? " (approx)" : "";
  chip.replaceChildren(
    icon("map-pin"),
    el("span", { text: `${state.anchor.label}${approx} · within ${state.radiusKm} km` }),
    el("button", { class: "anchor-chip-x", title: "Clear start-place filter", onclick: clearAnchor }, "×"),
  );
}

function setAnchor(a) {
  state.anchor = a;
  state.sort = "nearest";          // proximity is the whole point once you filter by place
  closeAnchorPop();
  render();
}
function clearAnchor() {
  state.anchor = null;
  if (state.sort === "nearest") state.sort = "name";
  closeAnchorPop();
  render();
}

let _anchorPopOpen = false;
function toggleAnchorPop() { _anchorPopOpen ? closeAnchorPop() : openAnchorPop(); }
function closeAnchorPop() {
  _anchorPopOpen = false;
  const pop = $("anchor-pop");
  if (pop) { pop.hidden = true; pop.replaceChildren(); }
}
function openAnchorPop() {
  _anchorPopOpen = true;
  const pop = $("anchor-pop");
  pop.hidden = false;
  buildAnchorPop(pop);
  setTimeout(() => { const s = pop.querySelector('input[type="search"]'); if (s) s.focus(); }, 30);
}

function buildAnchorPop(pop) {
  // radius as a slider (1-100 km): more intuitive than fixed chips, and it can go wide.
  const radiusOut = el("span", { class: "anchor-radius-val tnum", text: `${state.radiusKm} km` });
  const radiusSlider = el("input", { type: "range", class: "anchor-slider",
    min: "1", max: "100", step: "1", value: String(state.radiusKm),
    "aria-label": "Radius in kilometres" });
  let rDeb;
  radiusSlider.addEventListener("input", () => {
    state.radiusKm = Number(radiusSlider.value);
    radiusOut.textContent = `${state.radiusKm} km`;
    if (state.anchor) { clearTimeout(rDeb); rDeb = setTimeout(render, 110); }  // live filter, debounced
  });
  const radius = el("div", { class: "anchor-radius" }, [
    el("span", { class: "anchor-sub", text: "Within" }), radiusSlider, radiusOut,
  ]);

  const results = el("div", { class: "anchor-results" });
  const searchInput = el("input", { class: "author-input", type: "search",
    placeholder: "Search a place (town, area)…" });
  let deb;
  searchInput.addEventListener("input", () => {
    clearTimeout(deb);
    const q = searchInput.value.trim();
    if (!q) { results.replaceChildren(); return; }
    deb = setTimeout(async () => {
      try {
        const d = await engine(["geocode", "--q", q]);
        const hits = (d.results || []).slice(0, 6);
        results.replaceChildren(...(hits.length ? hits.map((r) =>
          el("button", { class: "anchor-result",
            onclick: () => setAnchor({ lat: r.lat, lon: r.lon, label: r.label }) }, r.label))
          : [el("div", { class: "anchor-empty", text: "No places found." })]));
      } catch { results.replaceChildren(el("div", { class: "anchor-empty", text: "Search failed." })); }
    }, 300);
  });

  const hereReset = () => here.replaceChildren(
    icon("map-pin"), el("span", { text: "Use my location" }),
    el("span", { class: "anchor-here-tag", text: "approx" }));
  const here = el("button", { class: "btn btn-ghost anchor-here",
    title: "Approximate location from your network — not a GPS fix" });
  hereReset();
  here.addEventListener("click", async () => {
    here.disabled = true; here.replaceChildren(el("span", { text: "Locating…" }));
    try {
      const d = await engine(["here"]);
      if (d && d.lat != null) setAnchor({ lat: d.lat, lon: d.lon, label: d.label || "Near me", approximate: true });
      else { toast((d && d.message) || "Couldn't get your location.", true); here.disabled = false; hereReset(); }
    } catch (e) {
      toast(e.message, true); here.disabled = false; hereReset();
    }
  });
  // honesty note: the IP fix is city-level, not GPS. Precise anchors = a searched place / a saved place.
  const hereNote = el("div", { class: "anchor-note",
    text: "Network-based, so only roughly right. For a precise start, search a place or pick a saved place." });

  // the user's own saved Places are exact anchors (a "Home" point beats approximate IP)
  const savedPts = ((state.snap.locations && state.snap.locations.points) || [])
    .filter((p) => p.lat != null && p.lon != null);
  const saved = savedPts.length ? el("div", { class: "anchor-saved" },
    [el("div", { class: "anchor-sub", text: "Your saved places" })].concat(
      savedPts.slice(0, 8).map((p) => el("button", { class: "anchor-result",
        onclick: () => setAnchor({ lat: p.lat, lon: p.lon, label: p.name || "Saved place" }) },
        [icon("map-pin"), el("span", { text: p.name || "Saved place" })])))) : null;

  pop.replaceChildren(
    radius,
    el("div", { class: "anchor-sub anchor-sub-top", text: "Anchor" }),
    searchInput, results, here, hereNote, saved,
  );
}

// ===================================================================================
// Add route: import a GPX/FIT, build a Garmin course FIT, add it to the watch (and the
// Mac route library). The file is read here, saved to a temp path by Rust, then handed
// to the engine's import-route — first --dry to detect name/sport/distance for the
// confirm dialog, then the real add on confirm.
// ===================================================================================
function onRoutePicked(e) {
  const file = e.target.files && e.target.files[0];
  e.target.value = "";                 // let the same file be re-picked later
  if (!file) return;
  const ext = (file.name.split(".").pop() || "").toLowerCase();
  if (ext !== "gpx" && ext !== "fit") { toast("Pick a .gpx or .fit route file.", true); return; }
  const reader = new FileReader();
  reader.onload = async () => {
    const b64 = String(reader.result).split(",")[1] || "";
    let path;
    try { path = await invoke("save_temp_file", { b64, ext }); }
    catch (err) { return toast("Could not read the file: " + err, true); }
    openImportDialog(path, file.name.replace(/\.[^.]+$/, ""));
  };
  reader.readAsDataURL(file);
}

async function openImportDialog(path, fallbackName) {
  showScan("Reading route");
  let pv;
  try { pv = await engine(["import-route", "--file", path, "--dry"]); }
  catch (e) { hideScan(); return toast(e.message, true); }
  hideScan();

  const nameInput = el("input", { class: "rename-input", type: "text",
    value: pv.name || fallbackName || "Imported route", spellcheck: "false", "aria-label": "Route name" });
  const sportSel = el("select", { class: "sport-select" },
    ["cycling", "running", "walking", "hiking", "swimming"].map((s) =>
      el("option", { value: s }, s)));
  sportSel.value = pv.sport || "cycling";
  const meta = el("div", { class: "import-meta", text:
    `${fmtDist(pv.dist_m)}${pv.asc_m ? " · +" + fmtM(pv.asc_m) : ""} · ${pv.n_points} points` });
  const connectInput = el("input", { type: "checkbox" });
  connectInput.checked = true;
  const connected = state.snap.watch.connected;

  openModal({
    title: "Add route to your Fenix",
    lead: "Builds a Garmin course from your file.",
    list: [el("li", { class: "rename-li" }, [
      el("label", { class: "import-field" }, [el("span", { text: "Name" }), nameInput]),
      el("label", { class: "import-field" }, [el("span", { text: "Sport" }), sportSel]),
      el("label", { class: "import-check" }, [
        connectInput,
        el("span", { text: "Also add to Garmin Connect" }),
      ]),
      meta,
    ])],
    warn: connected
      ? "Adds to Garmin Connect, copies to your Fenix over USB, and saves to your Mac route library."
      : "Adds to Garmin Connect and saves to your Mac route library. Plug in your Fenix over USB to copy it to the watch.",
    warnClass: connected ? "reversible" : "permanent",
    warnIcon: connected ? "device-watch" : "alert-triangle",
    confirmLabel: connected ? "Add to watch" : "Save to library",
    confirmDanger: false, blocked: false,
    onConfirm: () => doImportRoute(path, nameInput.value.trim(), sportSel.value, connectInput.checked),
  });
}

async function doImportRoute(path, name, sport, addConnect) {
  closeModal();
  showScan("Adding route");
  const args = ["import-route", "--file", path, "--sport", sport, "--to-watch"];
  if (name) args.push("--name", name);
  if (addConnect) args.push("--to-connect");
  let res;
  try { res = await engine(args); }
  catch (e) { hideScan(); return toast(e.message, true); }
  hideScan();
  const msg = [res.connect_message, res.watch_message].filter(Boolean).join(" ");
  toast(msg || "Route imported.", !!res.connect_error || (!res.on_watch && !res.saved_to_library));
  await load(res.on_watch);   // live refresh when the watch changed, so the new route shows
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
  const titleLine = [el("span", { class: "row-name", title: r.name || "", text: r.name || "Unnamed" })];
  if (stale) titleLine.push(el("span", { class: "row-flag", title: r.location_detail, text: "Stale route" }));
  const row = el("div", { class: "row" + (selected ? " is-selected" : "") }, [
    check,
    identityNode(r),
    el("div", { class: "row-main" }, [
      el("div", { class: "row-title-line" }, titleLine),
      metaNode(r),
      conditionsRow(r),
    ]),
    el("div", { class: "badges" }, [connectBadge, watchBadge]),
    renameBtn,
  ]);
  return row;
}

function badge(iconName, label, on, known, enabled, onClick) {
  const cls = "badge " + (on ? "on" : known ? "off" : "unknown");
  const txt = on
    ? (label === "Watch" ? "On Watch" : "In Connect")
    : known
      ? (label === "Watch" ? "Not on Watch" : "No Connect")
      : `${label} ?`;
  const title = `${label}: ${on ? "present" : known ? "absent" : "unknown"}`;
  const b = el("button", { class: cls, title, "aria-label": title }, [icon(iconName), txt]);
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
    engine(["preview", "--action", "rename", "--kind", "course", "--id", String(r.id), "--to", oldName, "--on-watch", "1"])
      .then((pv) => { watchMax = pv.watch_name_max; update(); })
      .catch(() => {});
  }
}

async function doRename(r, newName) {
  if (!newName || newName === r.name) return;
  closeModal();
  const onWatch = r.on_watch && !r.scheduled;
  showScan("Renaming");
  try {
    // Pass the placement the UI already knows so the engine skips the inventory fetch + watch
    // scan (the old rename bottleneck, and what made a Connect-only rename fail when unplugged).
    await engine(["apply", "--action", "rename", "--kind", r.kind, "--id", String(r.id), "--to", newName,
      "--on-watch", onWatch ? "1" : "0",
      ...(r.watch_idx != null ? ["--watch-idx", String(r.watch_idx)] : []),
      ...(r.scheduled ? ["--scheduled"] : [])]);
    hideScan();
    toast("Renamed.");
  } catch (e) {
    hideScan();
    toast((e.payload && (e.payload.message || e.payload.output)) || e.message, true);
    return;
  }
  // A rename changes only the name, never where the item lives — patch the row in place and
  // re-render (instant) instead of a full re-snapshot (the slow part). An on-watch rename also
  // rewrote a watch file (new name + index), so reconcile just that one in the background.
  r.name = newName;
  const it = state.snap.items.find((x) => x.uid === r.uid);
  if (it && it !== r) it.name = newName;
  render();
  if (onWatch) load(true);
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
  for (const b of $(groupId).querySelectorAll(".seg-btn")) {
    b.classList.toggle("is-active", b === btn);
    b.setAttribute("aria-pressed", String(b === btn));
  }
}

window.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("[data-icon]").forEach((e) => setIcon(e, e.dataset.icon));
  $("refresh-btn").addEventListener("click", () => load(true));
  $("new-workout-btn").addEventListener("click", openAuthor);
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
  // route controls: sort + start-place filter + add route
  $("route-sort").addEventListener("change", (e) => {
    state.sort = e.target.value;
    if (state.sort === "nearest" && !state.anchor) openAnchorPop();  // needs an anchor to mean anything
    render();
  });
  $("anchor-btn").addEventListener("click", (e) => { e.stopPropagation(); toggleAnchorPop(); });
  document.addEventListener("click", (e) => {   // click outside the picker closes it
    if (_anchorPopOpen && !e.target.closest(".anchor-wrap")) closeAnchorPop();
  });
  $("add-route-btn").addEventListener("click", () => $("route-file").click());
  $("route-file").addEventListener("change", onRoutePicked);
  $("bulk-remove").addEventListener("click", bulkRemove);
  $("bulk-clear").addEventListener("click", () => { state.selected.clear(); render(); });
  $("stale-review").addEventListener("click", reviewStale);
  $("stale-dismiss").addEventListener("click", () => { state.staleDismissed = true; renderStaleBanner(); });
  $("modal-cancel").addEventListener("click", closeModal);
  $("modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeModal(); });
  window.addEventListener("scroll", hidePreview, true);  // don't leave a stale preview mid-scroll
  load(false);
});

// ===================================================================================
// Workout authoring — create a workout and push it to the watch.
// Two surfaces, ONE engine: a no-LLM structured builder, and a bring-your-own-key
// natural-language / image input. Both end at the shared Python engine (workout.py)
// via api.py's workout-* commands: validate -> build -> push. No Garmin logic here.
// ===================================================================================
const AUTHOR = {
  built: false,
  tab: "describe",
  spec: null,          // the current valid-or-not spec (from the form or the LLM)
  valid: false,
  image: null,         // { b64, ext, name } of a picked plan photo
  form: null,          // the structured-builder model
};

const CUSTOM_MODEL_VALUE = "__custom__";
const PROVIDER_LABEL = { anthropic: "Anthropic", openai: "OpenAI", gemini: "Gemini" };

const TARGETS = [
  { v: "none", label: "No target" },
  { v: "hr", label: "Heart rate (bpm)", kind: "pair" },
  { v: "pace_pct", label: "Pace (% threshold)", kind: "pair" },
  { v: "power_pct", label: "Power (% FTP, cycling)", kind: "pair" },
  { v: "hr_pct", label: "Heart rate (% threshold)", kind: "pair" },
  { v: "power_zone", label: "Power zone", kind: "zone" },
  { v: "hr_zone", label: "HR zone", kind: "zone" },
  { v: "pace_zone", label: "Pace zone", kind: "zone" },
];
const targetKind = (v) => (TARGETS.find((t) => t.v === v) || {}).kind;

const STEP_TYPES = [
  { k: "warmup", label: "Warm-up" },
  { k: "interval", label: "Work interval" },
  { k: "recovery", label: "Recovery" },
  { k: "rest", label: "Rest" },
  { k: "strength", label: "Strength move" },
  { k: "cooldown", label: "Cool-down" },
  { k: "repeat", label: "Repeat block" },
];
const LEAF_TYPES = STEP_TYPES.filter((t) => !["repeat", "warmup", "cooldown"].includes(t.k));

function stepDefaults(k) {
  if (k === "rest") return { kind: k, seconds: 60 };
  if (k === "strength") return { kind: k, exercise: "", reps: 10, weight: "", rest: "" };
  if (k === "repeat") return { kind: k, times: 3, do: [stepDefaults("interval")] };
  const secs = k === "warmup" ? 300 : k === "cooldown" ? 180 : 60;
  return { kind: k, seconds: secs, target: { type: "none", lo: "", hi: "", z: "" } };
}

// ---- open / close / tabs -------------------------------------------------------

function openAuthor() {
  if (!AUTHOR.built) buildAuthorUI();
  AUTHOR.form = { name: "", sport: "strength", steps: [stepDefaults("warmup")] };
  AUTHOR.spec = null; AUTHOR.valid = false; AUTHOR.image = null;
  $("author-text").value = "";
  $("author-date").value = todayStr();
  renderImageChip();
  renderForm();
  clearPreview();
  loadAuthorSettings();
  switchTab("describe");
  $("author-overlay").hidden = false;
}
function todayStr() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
function closeAuthor() { $("author-overlay").hidden = true; }

function switchTab(name) {
  AUTHOR.tab = name;
  $("author-describe").hidden = name !== "describe";
  $("author-build").hidden = name !== "build";
  for (const b of $("author-tabs").querySelectorAll(".seg-btn"))
    b.classList.toggle("is-active", b.dataset.tab === name);
  // when switching to Build, preview the current form
  if (name === "build") syncAndPreview();
  else clearPreview();
}

// ---- overlay construction (once) ----------------------------------------------

function buildAuthorUI() {
  const tabs = el("div", { id: "author-tabs", class: "seg" }, [
    el("button", { class: "seg-btn is-active", "data-tab": "describe",
      onclick: () => switchTab("describe") }, "Describe it"),
    el("button", { class: "seg-btn", "data-tab": "build",
      onclick: () => switchTab("build") }, "Build it"),
  ]);
  const head = el("div", { class: "author-head" }, [
    el("div", { class: "author-title" }, "New workout"),
    tabs,
    el("div", { class: "spacer" }),
    el("button", { class: "banner-x", title: "Close", onclick: closeAuthor }, icon("x")),
  ]);

  const body = el("div", { class: "author-body" }, [buildDescribePane(), buildFormPane()]);

  const foot = el("div", { class: "author-foot" }, [
    el("div", { id: "author-warnings", class: "author-warnings", hidden: true }),
    el("div", { id: "author-summary", class: "author-cards" }),
    el("div", { class: "author-foot-actions" }, [
      el("div", { id: "author-status", class: "author-status" }),
      el("label", { class: "author-date-wrap", title: "Scheduling it makes it sync to your watch automatically — no manual Send to device" }, [
        el("span", { text: "On watch for" }),
        el("input", { type: "date", id: "author-date", class: "author-input author-date" }),
      ]),
      el("button", { id: "author-push", class: "btn btn-primary", disabled: true,
        onclick: pushSpec }, "Push to watch"),
    ]),
  ]);

  const card = el("div", { class: "author-card" }, [head, body, foot]);
  const overlay = el("div", { id: "author-overlay", class: "author-overlay", hidden: true,
    onclick: (e) => { if (e.target.id === "author-overlay") closeAuthor(); } }, [card]);
  document.body.appendChild(overlay);
  AUTHOR.built = true;
}

function buildDescribePane() {
  const info = el("div", { class: "author-info" }, [
    el("div", { text: "Describe a workout in words or snap a photo of a plan, and it builds it on your watch." }),
    el("div", { text: "You can sign in with ChatGPT, or paste an API key for Anthropic, OpenAI, or Gemini." }),
    el("div", { text: "Sign-in uses your own ChatGPT account. It's an unofficial path and can stop working if OpenAI changes it, in which case it uses your API key instead." }),
  ]);

  const ta = el("textarea", { id: "author-text", class: "author-text", rows: "5",
    placeholder: "Describe your workout in plain English, e.g. “10 min easy, then 4x(3 min hard / 2 min jog), 10 min easy”. Or attach a photo of a training plan below." });

  const fileInput = el("input", { type: "file", accept: "image/*", id: "author-file",
    style: "display:none", onchange: onImagePicked });
  const imageRow = el("div", { class: "author-image-row" }, [
    el("button", { class: "btn btn-ghost", onclick: () => $("author-file").click() },
      [icon("map-pin"), el("span", { text: "Attach plan photo" })]),
    el("span", { id: "author-image-chip", class: "author-chip", hidden: true }),
    fileInput,
  ]);

  const settings = buildSettingsBlock();

  const buildBtn = el("button", { id: "author-llm-btn", class: "btn btn-primary author-build-btn",
    onclick: buildFromLLM }, "Build with my model");

  return el("div", { id: "author-describe", class: "author-pane" },
    [info, ta, imageRow, settings, buildBtn]);
}

function buildSettingsBlock() {
  const provider = el("select", { id: "author-provider", class: "sport-select",
    onchange: onProviderChange }, [
    el("option", { value: "anthropic" }, "Anthropic (Claude)"),
    el("option", { value: "openai" }, "OpenAI (ChatGPT)"),
    el("option", { value: "gemini" }, "Google (Gemini)"),
  ]);
  const model = el("select", { id: "author-model", class: "sport-select",
    onchange: onModelSelectChange });
  const customModel = el("input", { id: "author-model-custom", class: "author-input",
    placeholder: "custom model id", hidden: true });
  const modelField = el("div", { class: "author-model-field" }, [model, customModel]);
  const key = el("input", { id: "author-key", class: "author-input", type: "password",
    placeholder: "API key for selected provider" });
  const save = el("button", { class: "btn btn-ghost", onclick: saveAuthorSettings }, "Save");
  const status = el("span", { id: "author-key-status", class: "author-status" });

  const details = el("details", { class: "author-settings" }, [
    el("summary", {}, "Model settings"),
    el("div", { class: "author-settings-grid" }, [
      el("label", {}, "Provider"), provider,
      el("label", {}, "Model"), modelField,
      el("label", {}, "API key"), key,
      el("div", {}), el("div", { class: "author-settings-actions" }, [save, status]),
    ]),
    buildOauthBlock(),
  ]);
  return details;
}

// The "Sign in with ChatGPT" block. Only visible when the provider is OpenAI. It sits ALONGSIDE
// the API-key field: the key path is always the fallback if the subscription sign-in stops working.
function buildOauthBlock() {
  const signIn = el("button", { id: "author-oauth-signin", class: "btn btn-primary",
    onclick: oauthSignIn }, "Sign in with ChatGPT");
  const signOut = el("button", { id: "author-oauth-signout", class: "btn btn-ghost",
    onclick: oauthSignOut, hidden: true }, "Sign out");
  const status = el("span", { id: "author-oauth-status", class: "author-status" });
  return el("div", { id: "author-oauth", class: "author-oauth", hidden: true }, [
    el("div", { class: "author-oauth-row" }, [signIn, signOut, status]),
  ]);
}

function buildFormPane() {
  const name = el("input", { id: "author-name", class: "author-input", placeholder: "Workout name",
    oninput: (e) => { AUTHOR.form.name = e.target.value; syncAndPreview(); } });
  const sport = el("select", { id: "author-sport", class: "sport-select",
    onchange: (e) => { AUTHOR.form.sport = e.target.value; syncAndPreview(); } },
    ["strength", "running", "cycling", "swimming"].map((s) =>
      el("option", { value: s }, s)));

  const steps = el("div", { id: "author-steps", class: "author-steps" });
  const addMenu = el("div", { class: "author-addrow" },
    [el("span", { class: "author-addlabel", text: "Add step:" })].concat(
      STEP_TYPES.map((t) => el("button", { class: "chip-btn",
        onclick: () => { AUTHOR.form.steps.push(stepDefaults(t.k)); renderForm(); syncAndPreview(); } },
        t.label))));

  return el("div", { id: "author-build", class: "author-pane", hidden: true }, [
    el("div", { class: "author-form-head" }, [name, sport]),
    steps, addMenu,
  ]);
}

// ---- structured form: render + model -> spec ----------------------------------

function renderForm() {
  $("author-name").value = AUTHOR.form.name;
  $("author-sport").value = AUTHOR.form.sport;
  const host = $("author-steps");
  host.replaceChildren(...AUTHOR.form.steps.map((s, i) => renderStep(s, AUTHOR.form.steps, i)));
}

function renderStep(step, list, i) {
  const title = el("div", { class: "author-step-title" },
    [el("span", { text: (STEP_TYPES.find((t) => t.k === step.kind) || {}).label || step.kind })]);
  const remove = el("button", { class: "banner-x", title: "Remove",
    onclick: () => { list.splice(i, 1); renderForm(); syncAndPreview(); } }, icon("trash"));
  const head = el("div", { class: "author-step-head" }, [title, el("div", { class: "spacer" }), remove]);

  const body = el("div", { class: "author-step-body" });
  if (step.kind === "rest") {
    body.append(numField("Seconds", step.seconds, (v) => (step.seconds = v)));
  } else if (step.kind === "strength") {
    body.append(exerciseField(step),
      numField("Reps", step.reps, (v) => (step.reps = v)),
      numField("Weight (kg)", step.weight, (v) => (step.weight = v)),
      numField("Rest after (s)", step.rest, (v) => (step.rest = v)));
  } else if (step.kind === "interval") {
    body.append(numField("Seconds", step.seconds, (v) => (step.seconds = v)), targetField(step));
  } else if (step.kind === "repeat") {
    body.append(numField("Times", step.times, (v) => (step.times = v)));
    const inner = el("div", { class: "author-steps author-inner" },
      step.do.map((s, j) => renderStep(s, step.do, j)));
    const addInner = el("div", { class: "author-addrow" },
      [el("span", { class: "author-addlabel", text: "Add to block:" })].concat(
        LEAF_TYPES.map((t) => el("button", { class: "chip-btn",
          onclick: () => { step.do.push(stepDefaults(t.k)); renderForm(); syncAndPreview(); } },
          t.label))));
    body.append(inner, addInner);
  } else {
    // warmup / cooldown / recovery
    body.append(numField("Seconds", step.seconds, (v) => (step.seconds = v)), targetField(step));
  }
  return el("div", { class: "author-step" }, [head, body]);
}

function numField(label, val, set) {
  return el("label", { class: "author-num" }, [
    el("span", { text: label }),
    el("input", { type: "number", class: "author-input", value: val ?? "",
      oninput: (e) => { set(e.target.value === "" ? "" : Number(e.target.value)); syncAndPreview(); } }),
  ]);
}

function targetField(step) {
  const t = step.target || (step.target = { type: "none", lo: "", hi: "", z: "" });
  const sel = el("select", { class: "sport-select",
    onchange: (e) => { t.type = e.target.value; renderForm(); syncAndPreview(); } },
    TARGETS.map((o) => el("option", { value: o.v }, o.label)));
  sel.value = t.type;
  const kids = [el("span", { text: "Target" }), sel];
  const kind = targetKind(t.type);
  if (kind === "pair") {
    kids.push(el("input", { type: "number", class: "author-input author-mini", value: t.lo, placeholder: "low",
      oninput: (e) => { t.lo = e.target.value; syncAndPreview(); } }));
    kids.push(el("input", { type: "number", class: "author-input author-mini", value: t.hi, placeholder: "high",
      oninput: (e) => { t.hi = e.target.value; syncAndPreview(); } }));
  } else if (kind === "zone") {
    kids.push(el("input", { type: "number", class: "author-input author-mini", value: t.z, placeholder: "zone",
      oninput: (e) => { t.z = e.target.value; syncAndPreview(); } }));
  }
  return el("label", { class: "author-target" }, kids);
}

function exerciseField(step) {
  const input = el("input", { class: "author-input author-ex", value: step.exercise,
    placeholder: "CATEGORY/NAME, e.g. SQUAT/GOBLET_SQUAT",
    oninput: (e) => { step.exercise = e.target.value; syncAndPreview(); } });
  const results = el("div", { class: "author-ex-results" });
  const search = el("button", { class: "btn btn-ghost", onclick: async () => {
    const term = (step.exercise || "").split("/").pop() || step.exercise || "";
    if (!term) return;
    try {
      const d = await engine(["workout-catalog", "--search", term]);
      results.replaceChildren(...d.hits.slice(0, 8).map(([cat, name]) =>
        el("button", { class: "chip-btn", onclick: () => {
          step.exercise = `${cat}/${name}`; renderForm(); syncAndPreview();
        } }, `${cat}/${name}`)));
    } catch (e) { toast(e.message, true); }
  } }, "Find");
  return el("div", { class: "author-ex-wrap" },
    [el("span", { text: "Exercise" }), el("div", { class: "author-ex-row" }, [input, search]), results]);
}

function num(v) { return v === "" || v == null ? null : Number(v); }

function applyTarget(o, t) {
  if (!t || t.type === "none") return;
  const kind = targetKind(t.type);
  if (kind === "pair" && num(t.lo) != null && num(t.hi) != null) o[t.type] = [num(t.lo), num(t.hi)];
  else if (kind === "zone" && num(t.z) != null) o[t.type] = num(t.z);
}

function stepToSpec(s) {
  if (s.kind === "repeat")
    return { repeat: num(s.times) || 1, do: s.do.map(stepToSpec) };
  if (s.kind === "rest") return { rest: num(s.seconds) || 0 };
  if (s.kind === "strength") {
    const o = { exercise: s.exercise };
    if (num(s.reps) != null) o.reps = num(s.reps);
    if (num(s.weight) != null) o.weight_kg = num(s.weight);
    if (num(s.rest) != null) o.rest = num(s.rest);
    return o;
  }
  const o = {};
  const key = s.kind === "interval" ? "work" : s.kind; // warmup/cooldown/recovery keep name
  o[s.kind === "recovery" ? "recover" : key] = num(s.seconds) || 0;
  applyTarget(o, s.target);
  return o;
}

function formToSpec(f) {
  return {
    name: f.name || "Untitled workout",
    sport: f.sport || "strength",
    animate: f.sport === "strength",
    steps: f.steps.map(stepToSpec),
  };
}

// ---- preview + push (shared) ---------------------------------------------------

let previewTimer;
function syncAndPreview() {
  if (AUTHOR.tab !== "build") return;
  clearTimeout(previewTimer);
  previewTimer = setTimeout(() => previewSpec(formToSpec(AUTHOR.form)), 200);
}

async function previewSpec(spec) {
  try {
    const d = await engine(["workout-preview", "--spec", JSON.stringify(spec)]);
    AUTHOR.spec = spec; AUTHOR.valid = d.valid;
    renderAuthorPreview(d.summary, d.warnings, d.errors);
  } catch (e) { toast(e.message, true); }
}

function renderAuthorPreview(_summary, warnings, errors) {
  renderSpecCards(AUTHOR.spec, $("author-summary"));
  const w = $("author-warnings");
  const lines = [].concat((errors || []).map((x) => "✕ " + x))
    .concat((warnings || []).map((x) => "! " + x));
  if (lines.length) { w.hidden = false; w.textContent = lines.join("\n"); }
  else w.hidden = true;
  $("author-push").disabled = !AUTHOR.valid;
}

function clearPreview() {
  AUTHOR.spec = null; AUTHOR.valid = false;
  $("author-summary").replaceChildren();
  $("author-warnings").hidden = true;
  $("author-push").disabled = true;
}

async function pushSpec() {
  if (!AUTHOR.spec || !AUTHOR.valid) return;
  const btn = $("author-push");
  btn.disabled = true; $("author-status").textContent = "Pushing…";
  const date = $("author-date").value;
  const args = ["workout-push", "--spec", JSON.stringify(AUTHOR.spec)];
  if (date) args.push("--schedule", date);
  try {
    const d = await engine(args);
    if (d.pushed) {
      toast(d.scheduled
        ? `Pushed “${d.name}” — it will sync to your watch automatically.`
        : `Pushed “${d.name}” to Connect.`);
      closeAuthor();
    } else {
      renderAuthorPreview(null, d.warnings, d.errors);
      $("author-status").textContent = "Fix the errors above.";
    }
  } catch (e) {
    toast(e.message, true); $("author-status").textContent = "";
    btn.disabled = false;
  }
}

// ---- pretty preview cards (colored step rows, human units) ---------------------

const STEP_STYLE = {
  warmup: { label: "Warm Up", color: "warmup" },
  cooldown: { label: "Cool Down", color: "cooldown" },
  recover: { label: "Recover", color: "recover" },
  rest: { label: "Rest", color: "recover" },
  strength: { color: "strength" },
  work: { label: "Run", color: "work" },
};

function fmtDurJS(s) {
  s = Math.round(s);
  if (s < 60) return s + "s";
  const m = Math.floor(s / 60), sec = s % 60;
  return sec ? `${m}:${String(sec).padStart(2, "0")}` : `${m} min`;
}
function paceJS(mps) {
  const spk = 1000 / mps, m = Math.floor(spk / 60), sec = Math.round(spk % 60);
  return `${m}:${String(sec).padStart(2, "0")}`;
}
function targetTextJS(s) {
  if (s.pace) return `${paceJS(s.pace[1])}–${paceJS(s.pace[0])} /km`;
  if (s.power) return `${s.power[0]}–${s.power[1]} W`;
  if (s.hr) return `${s.hr[0]}–${s.hr[1]} bpm`;
  if (s.power_pct) return `${s.power_pct[0]}–${s.power_pct[1]}% FTP`;
  if (s.pace_pct) return `${s.pace_pct[0]}–${s.pace_pct[1]}% pace`;
  if (s.hr_pct) return `${s.hr_pct[0]}–${s.hr_pct[1]}% HR`;
  if (s.power_zone) return `power zone ${s.power_zone}`;
  if (s.hr_zone) return `HR zone ${s.hr_zone}`;
  if (s.pace_zone) return `pace zone ${s.pace_zone}`;
  return "";
}
function titleCase(name) {
  return String(name).split("_").map((w) => w ? w[0] + w.slice(1).toLowerCase() : w).join(" ");
}
function stepCard(step) {
  let kind = "work", label, detail, extra = "";
  if ("warmup" in step) kind = "warmup";
  else if ("cooldown" in step) kind = "cooldown";
  else if ("exercise" in step) kind = "strength";
  else if ("recover" in step || "recovery" in step) kind = "recover";
  else if ("rest" in step) kind = "rest";
  const sty = STEP_STYLE[kind] || STEP_STYLE.work;

  if (kind === "strength") {
    const ex = step.exercise;
    label = typeof ex === "string" ? titleCase(ex.split("/").pop() || ex) : titleCase(ex.name || "");
    detail = step.reps ? `${step.reps} reps` : step.seconds ? fmtDurJS(step.seconds) : "to lap";
    if (step.weight_kg) detail += ` · ${step.weight_kg} kg`;
    if (step.rest) extra = `rest ${fmtDurJS(step.rest)}`;
  } else if (kind === "rest") {
    label = "Rest"; detail = fmtDurJS(step.rest);
  } else {
    label = sty.label;
    const dur = step.warmup ?? step.cooldown ?? step.recover ?? step.recovery ?? step.work ?? step.run;
    detail = dur != null ? fmtDurJS(dur) : (step.meters ? `${step.meters} m` : "to lap");
    extra = targetTextJS(step);
    if (step.power_alert) extra += (extra ? " · " : "") + `cap ${step.power_alert[1]} W`;
  }
  return el("div", { class: "wo-step wo-" + sty.color }, [
    el("div", { class: "wo-step-main" }, [
      el("span", { class: "wo-step-label", text: label }),
      el("span", { class: "wo-step-detail", text: detail }),
    ]),
    extra ? el("div", { class: "wo-step-extra", text: extra }) : null,
  ]);
}
function renderSpecCards(spec, host) {
  host.replaceChildren();
  if (!spec) return;
  host.appendChild(el("div", { class: "wo-title", text: spec.name || "Untitled workout" }));
  for (const st of spec.steps || []) {
    if (st && st.repeat) {
      const grp = el("div", { class: "wo-group" },
        [el("div", { class: "wo-group-label", text: `${st.repeat} ×` })]);
      for (const sub of st.do || []) grp.appendChild(stepCard(sub));
      host.appendChild(grp);
    } else host.appendChild(stepCard(st));
  }
}

// ---- Describe: image + LLM -----------------------------------------------------

function onImagePicked(e) {
  const file = e.target.files && e.target.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => {
    const b64 = String(reader.result).split(",")[1] || "";
    const ext = (file.name.split(".").pop() || "png").toLowerCase();
    AUTHOR.image = { b64, ext, name: file.name };
    renderImageChip();
  };
  reader.readAsDataURL(file);
}

function renderImageChip() {
  const chip = $("author-image-chip");
  if (!chip) return;
  if (AUTHOR.image) {
    chip.hidden = false;
    chip.replaceChildren(
      el("span", { text: AUTHOR.image.name }),
      el("button", { class: "author-chip-x", title: "Remove",
        onclick: () => { AUTHOR.image = null; $("author-file").value = ""; renderImageChip(); } }, "×"));
  } else chip.hidden = true;
}

async function buildFromLLM() {
  const text = $("author-text").value.trim();
  if (!text && !AUTHOR.image) { toast("Describe a workout or attach a photo.", true); return; }
  const btn = $("author-llm-btn");
  const s = AUTHOR.settings || {};
  const usingChatgpt = s.provider === "openai" && s.openai_auth === "oauth" && s.openai_oauth_connected;
  btn.disabled = true;
  $("author-status").textContent = usingChatgpt ? "Asking ChatGPT…" : "Asking your model…";
  try {
    let imagePath = null;
    if (AUTHOR.image) {
      imagePath = await invoke("save_temp_image", { b64: AUTHOR.image.b64, ext: AUTHOR.image.ext });
    }
    const args = ["workout-llm", "--text", text];
    if (imagePath) args.push("--image", imagePath);
    const d = await engine(args);
    AUTHOR.spec = d.spec; AUTHOR.valid = !(d.errors && d.errors.length);
    renderAuthorPreview(d.summary, d.warnings, d.errors);
    if (d.notes && d.notes.length) toast(d.notes.join(" "), false);  // e.g. OAuth->key fallback
    $("author-status").textContent = AUTHOR.valid ? "Ready — review and push." : "The model's spec has errors.";
  } catch (e) {
    toast(e.message, true); $("author-status").textContent = "";
  } finally { btn.disabled = false; }
}

// ---- Describe: settings --------------------------------------------------------

function providerLabel(provider) {
  return PROVIDER_LABEL[provider] || provider || "provider";
}

function authModeForProvider(provider, s) {
  return provider === "openai" && s && s.openai_auth === "oauth" && s.openai_oauth_connected
    ? "oauth" : "key";
}

function curatedModelsFor(provider, auth, s) {
  if (auth === "oauth") {
    const models = s.openai_oauth_models || [s.openai_oauth_default_model, "gpt-5.4"];
    return [...new Set(models.filter(Boolean))];
  }
  const models = (s.curated_models && s.curated_models[provider]) || [];
  const def = s.default_model && s.default_model[provider];
  return [...new Set([def].concat(models).filter(Boolean))];
}

function modelValueFor(provider, s) {
  const auth = authModeForProvider(provider, s);
  if (auth === "oauth") return s.openai_oauth_model || s.openai_oauth_default_model || "";
  return provider === s.provider ? (s.model || "") : ((s.default_model || {})[provider] || "");
}

function setModelOptions(models, selected, fallback) {
  const sel = $("author-model");
  const custom = $("author-model-custom");
  const opts = [...new Set([fallback].concat(models || []).filter(Boolean))];
  sel.replaceChildren(...opts.map((m) => el("option", { value: m }, m)),
    el("option", { value: CUSTOM_MODEL_VALUE }, "Custom..."));
  const value = selected || fallback || opts[0] || "";
  if (opts.includes(value)) {
    sel.value = value;
    custom.value = "";
    custom.hidden = true;
  } else {
    sel.value = CUSTOM_MODEL_VALUE;
    custom.value = value;
    custom.hidden = false;
  }
}

function onModelSelectChange() {
  const custom = $("author-model-custom");
  const isCustom = $("author-model").value === CUSTOM_MODEL_VALUE;
  custom.hidden = !isCustom;
  if (!isCustom) custom.value = "";
}

function selectedAuthorModel() {
  const sel = $("author-model");
  if (sel.value === CUSTOM_MODEL_VALUE) return $("author-model-custom").value.trim();
  return sel.value;
}

function renderModelOptionsFromSettings(provider, s) {
  const auth = authModeForProvider(provider, s);
  const fallback = auth === "oauth" ? s.openai_oauth_default_model : ((s.default_model || {})[provider] || "");
  setModelOptions(curatedModelsFor(provider, auth, s), modelValueFor(provider, s), fallback);
}

async function refreshAuthorModelOptions(provider, s) {
  const auth = authModeForProvider(provider, s);
  try {
    const d = await engine(["workout-models", "--provider", provider, "--auth", auth]);
    if ($("author-provider").value !== provider) return;
    setModelOptions(d.models || [], modelValueFor(provider, s), d.default_model || "");
  } catch (e) {
    if ($("author-provider").value === provider) renderModelOptionsFromSettings(provider, s);
  }
}

function updateKeyStatus(s, provider) {
  const byProvider = s.has_key_by_provider || {};
  const hasOwn = Object.prototype.hasOwnProperty.call(byProvider, provider);
  const hasKey = hasOwn ? byProvider[provider] : (provider === s.provider && s.has_key);
  $("author-key").placeholder = `${providerLabel(provider)} API key, stored locally`;
  $("author-key-status").textContent = hasKey
    ? `${providerLabel(provider)} key saved ✓`
    : `No ${providerLabel(provider)} key set`;
}

async function loadAuthorSettings() {
  try {
    const s = await engine(["workout-settings-get"]);
    AUTHOR.settings = s;
    $("author-provider").value = s.provider;
    renderModelOptionsFromSettings(s.provider, s);
    $("author-key").value = "";
    updateKeyStatus(s, s.provider);
    applyOauthUI(s);
    refreshAuthorModelOptions(s.provider, s);
  } catch (e) { /* settings are optional for the form path */ }
}

// Show the Sign-in-with-ChatGPT block only for OpenAI, and reflect connected/plan state.
function applyOauthUI(s) {
  const box = $("author-oauth");
  if (!box) return;
  const isOpenai = (s.provider || $("author-provider").value) === "openai";
  box.hidden = !isOpenai;
  if (!isOpenai) return;
  const connected = !!s.openai_oauth_connected;
  $("author-oauth-signin").hidden = connected;
  $("author-oauth-signout").hidden = !connected;
  const st = $("author-oauth-status");
  if (connected) {
    const plan = s.openai_oauth_plan ? ` (${s.openai_oauth_plan})` : "";
    st.textContent = `Signed in to ChatGPT${plan} ✓`;
  } else {
    st.textContent = "Not signed in";
  }
}

function onProviderChange(e) {
  const p = e.target.value;
  const s = { ...(AUTHOR.settings || {}), provider: p };
  renderModelOptionsFromSettings(p, s);
  updateKeyStatus(s, p);
  applyOauthUI(s);
  refreshAuthorModelOptions(p, s);
}

async function saveAuthorSettings() {
  const provider = $("author-provider").value;
  const auth = authModeForProvider(provider, { ...(AUTHOR.settings || {}), provider });
  const args = ["workout-settings-set", "--provider", provider];
  const model = selectedAuthorModel();
  if (model) {
    if (provider === "openai" && auth === "oauth") args.push("--openai-oauth-model", model);
    else args.push("--model", model);
  }
  const key = $("author-key").value.trim();
  if (key) args.push("--key", key);
  // Persist the OpenAI auth mode: use the subscription automatically when it's connected.
  if (provider === "openai") {
    const connected = !!(AUTHOR.settings && AUTHOR.settings.openai_oauth_connected);
    args.push("--openai-auth", connected ? "oauth" : "key");
  }
  try {
    const s = await engine(args);
    AUTHOR.settings = s;
    $("author-key").value = "";
    renderModelOptionsFromSettings(provider, s);
    updateKeyStatus(s, provider);
    applyOauthUI(s);
    refreshAuthorModelOptions(provider, s);
    toast("Model settings saved.");
  } catch (e) { toast(e.message, true); }
}

// ---- Describe: Sign in with ChatGPT (OpenAI subscription) ----------------------

async function oauthSignIn() {
  const btn = $("author-oauth-signin");
  const st = $("author-oauth-status");
  btn.disabled = true;
  st.textContent = "Opening browser — complete sign-in there…";
  try {
    // This call BLOCKS while the browser login runs (up to ~3 min), then returns fresh settings.
    const s = await engine(["workout-oauth-login"]);
    const selectedProvider = $("author-provider").value;
    AUTHOR.settings = { ...s, provider: selectedProvider };
    applyOauthUI(AUTHOR.settings);
    // Signing in switches OpenAI to the subscription path; persist that as the auth mode.
    if (selectedProvider === "openai" && s.openai_oauth_connected) {
      const s2 = await engine(["workout-settings-set", "--provider", "openai", "--openai-auth", "oauth"]);
      AUTHOR.settings = s2; applyOauthUI(s2); renderModelOptionsFromSettings("openai", s2);
      refreshAuthorModelOptions("openai", s2);
    }
    toast("Signed in with ChatGPT.");
  } catch (e) {
    toast(e.message, true);
    st.textContent = "Not signed in";
  } finally { btn.disabled = false; }
}

async function oauthSignOut() {
  try {
    const s = await engine(["workout-oauth-logout"]);
    AUTHOR.settings = s;
    // Fall back to the API-key mode for OpenAI once the subscription is disconnected.
    const s2 = await engine(["workout-settings-set", "--provider", "openai", "--openai-auth", "key"]);
    AUTHOR.settings = s2;
    applyOauthUI(s2);
    renderModelOptionsFromSettings("openai", s2);
    refreshAuthorModelOptions("openai", s2);
    toast("Signed out of ChatGPT.");
  } catch (e) { toast(e.message, true); }
}
