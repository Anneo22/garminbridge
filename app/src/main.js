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
  view: "content",
  snap: null,
  voice: { loaded: false, root: "", vault_configured: false, items: [], showArchived: false, selected: new Set() },
  settings: { loaded: false, data: null, installing: "", focus: "" },
  filters: { loc: "on-watch", kind: "all", sport: "", tag: "", folder: "", search: "" },
  selected: new Set(),
  staleDismissed: false,
  thumbs: {},              // course id -> {vb,d} filled lazily from Connect geoPoints
  thumbsTried: new Set(),  // ids we've already asked for (success or not); never refetch
  windWhen: "",            // "" = off, else a ride-time slot key; forecast wind is opt-in
  wind: {},                // route uid -> {deg,speed} for the chosen hour (deg = blows FROM)
  windTried: new Set(),    // uids fetched for the current slot; reset when the slot changes
  routeWind: { uid: "", key: "", startHours: 0, reverse: false, loading: false, data: null, error: "" },
  sort: "dist_asc",        // routes sort: dist_asc | dist_desc | name | nearest | recent
  anchor: null,            // area filter anchor { lat, lon, label, approximate? }
  radiusKm: 10,            // radius (km) around the anchor to keep routes within
  pendingAnchorIntent: null,
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
const TRANSCRIPTION_BACKENDS = [
  { id: "parakeet", label: "Parakeet", kind: "local", key: "" },
  { id: "whisper", label: "Whisper", kind: "local", key: "" },
  { id: "openai", label: "OpenAI", kind: "cloud", key: "GVE_OPENAI_KEY" },
  { id: "gemini", label: "Gemini", kind: "cloud", key: "GVE_GEMINI_KEY" },
  { id: "groq", label: "Groq", kind: "cloud", key: "GVE_GROQ_KEY" },
  { id: "deepgram", label: "Deepgram", kind: "cloud", key: "GVE_DEEPGRAM_KEY" },
];
const backendInfo = (id) => TRANSCRIPTION_BACKENDS.find((b) => b.id === id) || TRANSCRIPTION_BACKENDS[0];

const $ = (id) => document.getElementById(id);

function setListMode(mode) {
  const list = $("list");
  list.classList.toggle("list-settings", mode === "settings");
  list.classList.toggle("list-voice", mode === "voice");
}

function nextPaint() {
  return new Promise((resolve) => requestAnimationFrame(() => resolve()));
}

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

function pathCum(pts) {
  const cum = [0];
  for (let i = 1; i < pts.length; i++) cum.push(cum[i - 1] + Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]));
  return cum;
}
function pointAtPath(pts, cum, target) {
  if (!pts.length) return null;
  if (target <= 0) return pts[0];
  const total = cum[cum.length - 1] || 1;
  if (target >= total) return pts[pts.length - 1];
  let i = 1; while (i < cum.length - 1 && cum[i] < target) i++;
  const a = pts[i - 1], b = pts[i], span = cum[i] - cum[i - 1] || 1;
  const f = (target - cum[i - 1]) / span;
  return [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f];
}
function pathSliceD(pts, cum, fromFrac, toFrac) {
  if (pts.length < 2 || toFrac <= fromFrac) return "";
  const total = cum[cum.length - 1] || 1;
  const a = Math.max(0, Math.min(total, fromFrac * total));
  const b = Math.max(0, Math.min(total, toFrac * total));
  const start = pointAtPath(pts, cum, a), end = pointAtPath(pts, cum, b);
  if (!start || !end) return "";
  const mid = [];
  for (let i = 1; i < pts.length - 1; i++) if (cum[i] > a && cum[i] < b) mid.push(pts[i]);
  return "M" + [start, ...mid, end].map((p) => `${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join("L");
}
function windMapSvg(thumb, wind, reverse = false) {
  const svg = svgNode("svg", { viewBox: thumb.vb, class: "route-trace-wind", "aria-hidden": "true" });
  svg.appendChild(svgNode("path", { d: thumb.d, class: "route-wind-base" }));
  const pts = pathPoints(thumb.d), cum = pathCum(pts);
  for (const band of (wind && wind.bands) || []) {
    const from = reverse ? 1 - band.to : band.from;
    const to = reverse ? 1 - band.from : band.to;
    const d = pathSliceD(pts, cum, from, to);
    if (d) svg.appendChild(svgNode("path", { d, class: "route-wind-band wind-" + band.kind }));
  }
  for (const a of directionArrows(pts, [0.55]))
    svg.appendChild(svgNode("path", { d: "M0 0L-2 -1.15L-2 1.15Z", class: "route-arrow",
      transform: `translate(${a.x} ${a.y}) rotate(${(a.angle + (reverse ? 180 : 0)).toFixed(0)})` }));
  const start = reverse ? thumb.end : thumb.start;
  if (start) svg.appendChild(svgNode("circle", { cx: start[0], cy: start[1], r: 1.05, class: "route-start" }));
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
    const d = anchorDistKm(r);   // when an area filter is active, show how far the start is
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

function highlightedText(text, query, className = "", title = text || "") {
  const value = text || "";
  const q = (query || "").trim();
  const props = {};
  if (className) props.class = className;
  if (title) props.title = title;
  const n = el("span", props);
  if (!q) {
    n.textContent = value;
    return n;
  }
  const lower = value.toLowerCase();
  const needle = q.toLowerCase();
  let pos = 0, idx = lower.indexOf(needle);
  if (idx < 0) {
    n.textContent = value;
    return n;
  }
  while (idx >= 0) {
    if (idx > pos) n.appendChild(document.createTextNode(value.slice(pos, idx)));
    n.appendChild(el("mark", { class: "match", text: value.slice(idx, idx + q.length) }));
    pos = idx + q.length;
    idx = lower.indexOf(needle, pos);
  }
  if (pos < value.length) n.appendChild(document.createTextNode(value.slice(pos)));
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
  else {
    showScan("Loading your library", { elapsed: true });
    renderSkeleton();
  }
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
  setListMode("content");
  list.replaceChildren();
  for (let i = 0; i < 9; i++) {
    list.appendChild(el("div", { class: "sk-row" }, [
      el("div", { class: "sk sk-ico" }),
      el("div", { class: "sk sk-line", style: `width:${40 + (i * 37) % 45}%` }),
      el("div", { class: "sk sk-chip" }),
    ]));
  }
}

function populateSelect(id, allText, values, label = (x) => x) {
  const sel = $(id);
  const cur = sel.value;
  const items = values || [];
  sel.replaceChildren(el("option", { value: "", text: allText }));
  for (const item of items) sel.appendChild(el("option", { value: item, text: label(item) }));
  sel.value = cur && items.includes(cur) ? cur : "";
  return sel.value;
}

function populateSports() {
  state.filters.sport = populateSelect("filter-sport", "All sports", state.snap.sports || [], prettySport);
  state.filters.tag = populateSelect("filter-tag", "All tags", state.snap.tags || []);
  state.filters.folder = populateSelect("filter-folder", "All folders", state.snap.folders || []);
  $("filter-tag").hidden = !(state.snap.tags || []).length;
  $("filter-folder").hidden = !(state.snap.folders || []).length;
}

function visibleRows() {
  const f = state.filters;
  return state.snap.items.filter((r) => {
    if (f.kind !== "all" && r.kind !== f.kind) return false;
    if (f.sport && r.sport !== f.sport) return false;
    if (f.tag && !(r.kind === "course" && (r.tags || []).includes(f.tag))) return false;
    if (f.folder && !(r.kind === "course" && r.route_folder === f.folder)) return false;
    if (f.search) {
      const q = f.search.toLowerCase();
      const tagHit = (r.tags || []).some((t) => t.toLowerCase().includes(q));
      if (!(r.name || "").toLowerCase().includes(q) && !tagHit) return false;
    }
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
  if (state.view === "settings") { renderSettings(); return; }
  if (state.view === "voice") { renderVoice(); return; }
  hidePreview();
  setListMode("content");
  renderWatchPill();
  renderStaleBanner();
  const rows = visibleRows();
  const list = $("list");
  list.replaceChildren();
  const windRoute = selectedWindRoute(rows);

  const groups = ["workout", "course"]
    .map((kind) => {
      const g = rows.filter((r) => r.kind === kind);
      return [kind, kind === "course" ? sortRoutes(g) : g.sort((a, b) => (a.name || "").localeCompare(b.name || ""))];
    })
    .filter(([, g]) => g.length);
  const places = placesVisible();
  renderRouteToolbar(rows);
  if (windRoute) list.appendChild(routeWindPanel(windRoute));

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
  fetchRouteWind(windRoute);
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

function routeWindStartDate() {
  const d = new Date();
  d.setMinutes(0, 0, 0);
  d.setHours(d.getHours() + Number(state.routeWind.startHours || 0));
  return d;
}
function routeWindStartLabel() {
  return routeWindStartDate().toLocaleString(undefined, { weekday: "short", hour: "2-digit", minute: "2-digit" });
}
function selectedWindRoute(rows = null) {
  const pool = rows || state.snap.items;
  const courses = pool.filter((r) => r.kind === "course" && state.selected.has(r.uid));
  return courses.length === 1 ? courses[0] : null;
}
function routeWindKey(r) {
  return [r.uid, routeWindStartDate().toISOString().slice(0, 13), state.routeWind.reverse ? "rev" : "fwd", r.id || "", r.watch_file || "", r.name || ""].join("|");
}
function routeWindArgs(r) {
  const args = ["route-wind", "--start-iso", routeWindStartDate().toISOString()];
  if (state.routeWind.reverse) args.push("--reverse");
  if (r.id != null) args.push("--id", String(r.id));
  if (r.watch_file) args.push("--watch-file", r.watch_file);
  if (r.name) args.push("--name", r.name);
  return args;
}
async function fetchRouteWind(r) {
  if (!r || r.kind !== "course") return;
  const key = routeWindKey(r);
  if (state.routeWind.loading && state.routeWind.key === key) return;
  if (state.routeWind.data && state.routeWind.key === key) return;
  state.routeWind = { ...state.routeWind, uid: r.uid, key, loading: true, data: null, error: "" };
  render();
  try {
    const res = await engine(routeWindArgs(r));
    if (state.routeWind.key !== key) return;
    state.routeWind = { ...state.routeWind, loading: false, data: res.wind, error: "" };
  } catch (e) {
    if (state.routeWind.key !== key) return;
    state.routeWind = { ...state.routeWind, loading: false, data: null, error: e.message };
  }
  render();
}
function setRouteWindHours(v) {
  state.routeWind = { ...state.routeWind, startHours: Number(v), key: "", data: null, error: "" };
  render();
}
function setRouteWindReverse(reverse) {
  if (state.routeWind.reverse === reverse) return;
  state.routeWind = { ...state.routeWind, reverse, key: "", data: null, error: "" };
  render();
}
function windValueText(v) {
  if (v == null) return "No wind";
  if (v > 0.5) return `${Math.abs(v).toFixed(1)} km/h tailwind`;
  if (v < -0.5) return `${Math.abs(v).toFixed(1)} km/h headwind`;
  return "neutral";
}
function signedMeters(v) {
  if (v == null) return "0 m";
  return `${v > 0 ? "+" : ""}${Math.round(v)} m`;
}
function pctText(p) {
  p = p || {};
  return `${p.head || 0}% head / ${p.tail || 0}% tail / ${p.cross || 0}% cross`;
}
function kmRange(s) {
  if (!s) return "";
  return `km ${s.from_km}-${s.to_km}`;
}
function weatherStrip(w) {
  const wx = (w && w.weather) || {};
  const rain = wx.rain_probability_max == null ? "rain n/a" : `${wx.rain_probability_max}% rain` + (wx.rain_at_km != null ? ` near km ${wx.rain_at_km}` : "");
  const gust = wx.gust_max_kmh == null ? "gusts n/a" : `gusts ${wx.gust_max_kmh} km/h`;
  const feels = wx.feels_min_c == null ? "feels n/a" : `feels ${wx.feels_min_c}-${wx.feels_max_c}°`;
  return el("div", { class: "route-wind-weather" }, [
    el("span", {}, [icon("droplet"), document.createTextNode(rain)]),
    el("span", {}, [icon("wind"), document.createTextNode(gust)]),
    el("span", {}, [icon("sun"), document.createTextNode(feels)]),
  ]);
}
function routeWindPanel(r) {
  const thumb = rowThumb(r);
  const key = routeWindKey(r);
  const current = state.routeWind.key === key ? state.routeWind : { ...state.routeWind, data: null, loading: false, error: "" };
  const wind = current.data;
  const range = el("input", { type: "range", class: "route-wind-range", min: "0", max: "72", step: "1",
    value: String(state.routeWind.startHours), "aria-label": "Ride start time", onchange: (e) => setRouteWindHours(e.target.value) });
  const dirBtn = (label, reverse) => el("button", {
    class: "seg-btn" + (state.routeWind.reverse === reverse ? " is-active" : ""),
    "aria-pressed": String(state.routeWind.reverse === reverse),
    onclick: () => setRouteWindReverse(reverse),
  }, label);
  const status = current.loading ? "Loading wind along route" : current.error || (!wind ? "Select a route with GPS points to compute wind" : "");
  const headline = wind ? el("div", { class: "route-wind-headline" }, [
    el("div", {}, [el("span", { class: "rw-k", text: "Net" }), el("strong", { text: windValueText(wind.net_effective_kmh) })]),
    el("div", {}, [el("span", { class: "rw-k", text: "Split" }), el("strong", { text: pctText(wind.pct) })]),
    el("div", {}, [el("span", { class: "rw-k", text: "Wind as climbing" }), el("strong", { text: signedMeters(wind.wind_climb_m) })]),
  ]) : el("div", { class: "route-wind-status", text: status });
  const head = wind && wind.worst_headwind;
  const gust = wind && wind.worst_gust_cross;
  const callouts = wind ? el("div", { class: "route-wind-callouts" }, [
    el("div", {}, [
      el("span", { class: "rw-k", text: "Hardest headwind" }),
      el("strong", { text: `${kmRange(head)} · ${head.avg_head_kmh || 0} km/h avg` }),
    ]),
    el("div", {}, [
      el("span", { class: "rw-k", text: "Gust/cross stretch" }),
      el("strong", { text: `${kmRange(gust)} · ${gust.avg_cross_kmh || 0} km/h cross` + (gust.max_gust_kmh ? ` · gusts ${gust.max_gust_kmh}` : "") }),
    ]),
  ]) : null;
  return el("section", { class: "route-wind-panel", "aria-label": "Selected route wind" }, [
    el("div", { class: "route-wind-top" }, [
      el("div", { class: "route-wind-title" }, [
        icon("wind"),
        el("div", {}, [
          el("div", { class: "route-wind-name", text: r.name || "Selected route" }),
          el("div", { class: "route-wind-note", text: "Open-Meteo grid wind, timed at about 22 km/h. Real shelter and exposure will vary." }),
        ]),
      ]),
      el("div", { class: "route-wind-controls" }, [
        el("label", { class: "route-wind-time" }, [
          el("span", { text: routeWindStartLabel() }),
          range,
        ]),
        el("div", { class: "seg", role: "group", "aria-label": "Route direction" }, [
          dirBtn("Forward", false),
          dirBtn("Reverse", true),
        ]),
      ]),
    ]),
    el("div", { class: "route-wind-body" }, [
      el("div", { class: "route-wind-mapwrap" }, [
        thumb && wind ? windMapSvg(thumb, wind, state.routeWind.reverse) : thumb ? bigMapSvg(thumb) : el("div", { class: "route-wind-map-empty", text: "Loading route shape" }),
      ]),
      el("div", { class: "route-wind-summary" }, [
        headline,
        wind ? weatherStrip(wind) : null,
        callouts,
      ]),
    ]),
  ]);
}

function emptyState(title, sub) {
  return el("div", { class: "empty" }, [
    illoEmpty(), el("div", { class: "empty-title", text: title }), el("div", { class: "empty-sub", text: sub }),
  ]);
}

// ===================================================================================
// Main view switch: Content Manager or Voice memos. The voice view reads local memo files
// through api.py and reuses the proven CLI for import + the existing Obsidian note writer.
// ===================================================================================
function resetSearchSilently() {
  const s = $("search");
  if (s) s.value = "";
  for (const id of ["filter-tag", "filter-folder"]) if ($(id)) $(id).value = "";
  state.filters.search = "";
  state.filters.tag = "";
  state.filters.folder = "";
}

function showView(view) {
  const loaded = (view === "voice" && state.voice.loaded) ||
    (view === "settings" && state.settings.loaded) ||
    (view === "content" && state.snap);
  if (state.view === view && loaded) return;
  if (state.view !== view) resetSearchSilently();
  state.view = view;
  for (const b of $("view-nav").querySelectorAll(".seg-btn")) {
    const on = b.dataset.view === view;
    b.classList.toggle("is-active", on);
    b.setAttribute("aria-pressed", String(on));
  }
  $("brand-title").textContent =
    view === "voice" ? "Voice memos" : view === "settings" ? "Settings" : "Content Manager";
  $("content-toolbar").hidden = view !== "content";
  $("voice-toolbar").hidden = view !== "voice";
  $("stale-banner").hidden = view !== "content" || $("stale-banner").hidden;
  $("route-toolbar").hidden = true;
  $("bulkbar").hidden = true;
  $("search").closest(".search-wrap").hidden = view === "settings";
  const refresh = $("refresh-btn");
  refresh.title = view === "voice"
    ? "Refresh the local voice memo list"
    : view === "settings"
      ? "Reload settings from local config"
      : "Re-scan the watch over USB (live)";
  if (view === "voice") {
    hidePreview();
    if (!state.voice.loaded) loadVoice(false);
    else renderVoice();
  } else if (view === "settings") {
    hidePreview();
    if (!state.settings.loaded) loadSettings(false);
    else renderSettings();
  } else if (state.snap) {
    render();
  } else {
    load(false);
  }
}

function openSettings(section = "") {
  state.settings.focus = section;
  showView("settings");
  setTimeout(focusSettingsTarget, 80);
  return true;
}

function focusSettingsTarget() {
  const target = state.settings.focus ? $("settings-" + state.settings.focus) : $("settings-pane");
  if (!target) return;
  target.scrollIntoView({ block: "start" });
  state.settings.focus = "";
}

async function loadSettings(refresh = false) {
  if (refresh) showScan("Reloading settings");
  else renderSettingsSkeleton();
  try {
    const d = await engine(["settings-get"]);
    setSettingsState(d);
    renderSettings();
  } catch (e) {
    $("list").replaceChildren(emptyState("Could not read settings", e.message));
  } finally {
    hideScan();
  }
}

function setSettingsState(d) {
  const focus = state.settings.focus || "";
  const installing = state.settings.installing || "";
  state.settings = { loaded: true, data: d, installing, focus };
}

function settingsData() {
  return state.settings.data || {};
}

function renderSettingsSkeleton() {
  const list = $("list");
  setListMode("settings");
  list.replaceChildren();
  for (let i = 0; i < 5; i++) {
    list.appendChild(el("div", { class: "sk-row settings-sk-row" }, [
      el("div", { class: "sk sk-ico" }),
      el("div", { class: "sk sk-line", style: `width:${62 + (i * 17) % 24}%` }),
      el("div", { class: "sk sk-chip" }),
    ]));
  }
}

function renderSettings() {
  $("content-toolbar").hidden = true;
  $("voice-toolbar").hidden = true;
  $("stale-banner").hidden = true;
  $("route-toolbar").hidden = true;
  $("bulkbar").hidden = true;
  const d = settingsData();
  renderSettingsPill(d);
  const list = $("list");
  setListMode("settings");
  list.replaceChildren(el("div", { id: "settings-pane", class: "settings-pane" }, [
    settingsTopbar(),
    settingsSection("transcription", "Transcription", "Voice memo text", "Turns audio memos into searchable transcript files. Local backends stay on this Mac and are free after install. Cloud backends send audio to the provider you choose and need your API key.", [
      renderTranscriptionSettings(d),
    ]),
    settingsSection("cleanup", "Transcript cleanup", "Optional LLM pass", "Fixes punctuation and removes filler after transcription. Keep raw when you want the untouched transcript beside the cleaned version.", [
      renderCleanupSettings(d),
    ]),
    settingsSection("watch-delete", "Delete from watch", "After import", "Controls when the original memo is removed from the Fenix. Use a stricter mode only when you trust the copy or transcript step.", [
      renderDeleteSettings(d),
    ]),
    settingsSection("audio-retention", "Local audio retention", "Mac storage", "Prunes heavy local .wav files after a transcript exists. It never deletes local audio that has no transcript.", [
      renderRetentionSettings(d),
    ]),
    settingsSection("archived-cleanup", "Archived audio cleanup", "Free Mac storage", "Deletes only archived .wav audio older than the age you choose. Transcripts stay, and active memos and notes are never touched.", [
      renderArchivedCleanupSettings(d),
    ]),
    settingsSection("auto-import", "Auto-import", "Free the watch", "Pause background imports when another app needs the watch's USB connection. Resume when GarminBridge should take over again.", [
      renderAutoImportSettings(d),
    ]),
  ]));
  renderSettingsStatus(d);
  setTimeout(focusSettingsTarget, 0);
}

function renderSettingsPill(d) {
  const pill = $("watch-pill"), txt = $("watch-pill-text");
  pill.classList.remove("is-live", "is-cache", "is-none");
  const paused = !!d.auto_import_paused;
  pill.classList.add(paused ? "is-cache" : "is-live");
  txt.textContent = paused ? "Auto-import paused" : "Auto-import active";
}

function renderSettingsStatus(d) {
  const t = d.transcription || {};
  const c = d.cleanup || {};
  $("status-counts").textContent =
    `Settings: transcription ${t.enabled ? "on" : "off"} · cleanup ${c.enabled ? "on" : "off"} · watch delete ${d.delete_mode || "keep"}`;
  $("status-updated").textContent = d.auto_import_paused ? "Watch is free for other apps" : "Auto-import is active";
}

function settingsTopbar() {
  return el("div", { class: "settings-topbar" }, [
    el("div", { class: "settings-top-copy" }, [
      el("div", { class: "settings-page-title", text: "Settings" }),
      el("div", { class: "settings-page-sub", text: "Voice memo import, transcription, cleanup, and storage." }),
    ]),
    el("button", { class: "btn btn-primary settings-done", onclick: () => showView("content") },
      [icon("check"), el("span", { text: "Done" })]),
  ]);
}

function settingsSection(id, title, kicker, lead, kids) {
  return el("section", { id: "settings-" + id, class: "settings-section" }, [
    el("div", { class: "settings-head" }, [
      el("div", { class: "settings-title-wrap" }, [
        el("div", { class: "settings-title", text: title }),
        el("div", { class: "settings-kicker", text: kicker }),
      ]),
      el("p", { class: "settings-lead", text: lead }),
    ]),
    el("div", { class: "settings-body" }, kids),
  ]);
}

function settingSwitch(label, help, checked, onChange) {
  const input = el("input", { type: "checkbox", class: "setting-switch-input" });
  input.checked = !!checked;
  input.addEventListener("change", () => onChange(input.checked));
  return el("label", { class: "setting-switch" }, [
    input,
    el("span", { class: "setting-switch-ui" }),
    el("span", { class: "setting-switch-copy" }, [
      el("span", { class: "setting-switch-label", text: label }),
      el("span", { class: "setting-help", text: help }),
    ]),
  ]);
}

function settingField(label, control, help = "") {
  return el("label", { class: "setting-field" }, [
    el("span", { class: "setting-label", text: label }),
    control,
    help ? el("span", { class: "setting-help", text: help }) : null,
  ]);
}

function settingNote(text, cls = "") {
  return el("div", { class: "setting-note" + (cls ? " " + cls : ""), text });
}

async function saveSetting(key, value, opts = {}) {
  showScan(opts.scan || "Saving settings");
  try {
    const args = ["settings-set", key];
    if (opts.unset) args.push("--unset");
    else args.push(String(value));
    const d = await engine(args);
    setSettingsState(d);
    hideScan();
    toast(opts.toast || d.message || "Settings saved.");
    renderSettings();
  } catch (e) {
    hideScan();
    toast(e.message || "Could not save settings.", true);
    renderSettings();
  }
}

function renderTranscriptionSettings(d) {
  const t = d.transcription || {};
  const backend = t.backend || "parakeet";
  const info = backendInfo(backend);
  const sel = el("select", { class: "sport-select setting-select", "aria-label": "Transcription backend",
    onchange: (e) => saveSetting("GVE_TRANSCRIBE_BACKEND", e.target.value) },
    TRANSCRIPTION_BACKENDS.map((b) => el("option", { value: b.id, text: b.label + (b.kind === "local" ? " (local)" : " (cloud)") })));
  sel.value = backend;
  const enabled = settingSwitch("Transcription", "Creates a .txt transcript for each memo so search and notes can use the words, not just the audio.", !!t.enabled,
    (on) => saveSetting("GVE_TRANSCRIBE", "1", { unset: !on }));
  const backendCopy = info.kind === "local"
    ? `${info.label} runs offline on this Mac. First use needs a one-time install, then transcription is local and free.`
    : `${info.label} is a cloud backend. It needs an API key and sends audio to ${info.label} for transcription.`;
  return el("div", { class: "settings-grid" }, [
    enabled,
    settingField("Backend", sel, backendCopy),
    info.kind === "local" ? renderLocalBackendInstall(info, t) : renderCloudBackendKey(info, t),
  ]);
}

function renderLocalBackendInstall(info, t) {
  const installed = !!(t.local_installed && t.local_installed[info.id]);
  const installing = state.settings.installing === info.id;
  const statusClass = installing ? "is-installing" : installed ? "is-installed" : "";
  const statusText = installing ? "Installing..." : installed ? "Installed" : "Not installed";
  const btn = el("button", { class: "btn btn-outline" + (installing ? " is-busy" : ""),
    disabled: installing, onclick: () => installTranscriptionBackend(info.id) },
    [icon(installing ? "refresh" : installed ? "refresh" : "microphone"), el("span", { text: installing ? "Installing..." : installed ? "Reinstall / Update" : `Install ${info.label}` })]);
  return el("div", { class: "setting-action-row local-backend-row" }, [
    el("span", { class: "setting-status " + statusClass }, [
      icon(installing ? "refresh" : installed ? "check" : "x"),
      el("span", { text: statusText }),
    ]),
    btn,
    settingNote(installed
      ? `${info.label} is installed. Updating is safe and keeps transcription on this backend.`
      : `The app installs ${info.label} in its isolated local environment and keeps you here while it runs.`),
  ]);
}

function renderCloudBackendKey(info, t) {
  const saved = !!(t.keys && t.keys[info.id]);
  const input = el("input", { class: "author-input setting-key-input", type: "password",
    autocomplete: "off", spellcheck: "false", placeholder: saved ? "Saved, paste a new key to replace it" : `${info.label} API key` });
  const btn = el("button", { class: "btn btn-outline", onclick: () => saveCloudKey(info, input) },
    [icon("check"), el("span", { text: saved ? "Replace key and use" : "Save key and use" })]);
  return el("div", { class: "setting-action-row setting-key-row" }, [
    input,
    btn,
    settingNote(saved
      ? `${info.label} key is saved locally. The field stays blank so the key is not echoed back.`
      : `Paste a ${info.label} key to use this cloud backend.`),
  ]);
}

async function saveCloudKey(info, input) {
  const key = (input.value || "").trim();
  if (!key) return toast("Paste the API key before saving.", true);
  showScan("Saving cloud key");
  try {
    await engine(["settings-set", info.key, key]);
    await engine(["settings-set", "GVE_TRANSCRIBE_BACKEND", info.id]);
    const d = await engine(["settings-set", "GVE_TRANSCRIBE", "1"]);
    setSettingsState(d);
    hideScan();
    toast(`${info.label} transcription is ready.`);
    renderSettings();
  } catch (e) {
    hideScan();
    toast(e.message || "Could not save that key.", true);
    renderSettings();
  }
}

async function installTranscriptionBackend(backend) {
  const label = backendInfo(backend).label;
  state.settings.installing = backend;
  renderSettings();
  showScan(`Installing ${label} transcription`);
  try {
    let d = await engine(["transcription-install", "--backend", backend]);
    if (!hasLocalBackendStatus(d)) d = await engine(["settings-get"]);
    setSettingsState(d);
    hideScan();
    const installed = !!(d.transcription && d.transcription.local_installed && d.transcription.local_installed[backend]);
    toast(installed ? `${label} transcription is installed.` : d.message || `${label} transcription is ready.`);
  } catch (e) {
    const installed = await refreshLocalBackendStatus(backend);
    hideScan();
    if (installed) toast(`${label} is already installed. You can use it now.`);
    else toast(installBackendError(label, e), true);
  } finally {
    state.settings.installing = "";
    renderSettings();
  }
}

function hasLocalBackendStatus(d) {
  return !!(d && d.transcription && d.transcription.local_installed);
}

async function refreshLocalBackendStatus(backend) {
  try {
    const d = await engine(["settings-get"]);
    setSettingsState(d);
    return !!(d.transcription && d.transcription.local_installed && d.transcription.local_installed[backend]);
  } catch {
    return false;
  }
}

function installBackendError(label, e) {
  const code = e.payload && e.payload.error;
  if (code === "installer_missing") return `The ${label} installer is missing in this build. Install the latest app build, then try again.`;
  if (code === "unsupported_backend") return `${label} does not use the local installer. Choose a local backend, then try again.`;
  return `${label} could not be installed. Check that the local installer is available, then try Reinstall / Update.`;
}

function renderCleanupSettings(d) {
  const c = d.cleanup || {};
  const provider = c.backend || "openai";
  const keySaved = !!(c.keys && c.keys[provider]);
  return el("div", { class: "settings-grid" }, [
    settingSwitch("Clean up transcripts", "Runs the raw transcript through an LLM to fix punctuation and remove obvious filler.", !!c.enabled,
      (on) => saveSetting("GVE_TRANSCRIPT_CLEANUP", "1", { unset: !on })),
    settingSwitch("Keep raw transcript", "Also saves the untouched transcript as a raw sidecar, useful when cleanup might change wording.", !!c.keep_raw,
      (on) => saveSetting("GVE_TRANSCRIPT_KEEP_RAW", "1", { unset: !on })),
    settingNote(keySaved
      ? `Cleanup uses ${provider}. Its key is saved locally.`
      : `Cleanup uses ${provider}. Save that provider's cloud key before relying on cleanup.`),
  ]);
}

function renderDeleteSettings(d) {
  const select = el("select", { class: "sport-select setting-select", "aria-label": "Delete from watch mode",
    onchange: (e) => {
      const v = e.target.value;
      if (v === "keep") saveSetting("GARMIN_VOICE_DELETE", "", { unset: true });
      else saveSetting("GARMIN_VOICE_DELETE", v);
    } }, [
    el("option", { value: "keep", text: "Keep on watch" }),
    el("option", { value: "now", text: "Delete after local copy" }),
    el("option", { value: "transcribed", text: "Delete after transcript" }),
  ]);
  select.value = d.delete_mode || "keep";
  return el("div", { class: "settings-grid" }, [
    settingField("Mode", select, "Keep is safest. Delete after local copy frees watch space sooner. Delete after transcript waits until searchable text exists."),
    el("div", { class: "setting-option-notes" }, [
      settingNote("Keep: import copies the memo to the Mac and leaves the original on the Fenix."),
      settingNote("Delete after local copy: removes the watch copy once the Mac copy is verified."),
      settingNote("Delete after transcript: removes the watch copy only after transcription succeeds."),
    ]),
  ]);
}

function renderRetentionSettings(d) {
  const current = d.audio_retention_days || "";
  const known = ["", "0", "30", "90"];
  const selected = known.includes(current) ? current : "custom";
  const custom = el("input", { class: "author-input setting-days-input", type: "number", min: "0", step: "1",
    value: selected === "custom" ? current : "30", "aria-label": "Days to keep local audio" });
  const customWrap = el("div", { class: "setting-custom-days" }, [
    settingField("Days", custom, "0 means delete the audio as soon as the transcript exists."),
    el("button", { class: "btn btn-outline", onclick: () => saveRetentionDays(custom.value) },
      [icon("check"), el("span", { text: "Save days" })]),
  ]);
  customWrap.hidden = selected !== "custom";
  const select = el("select", { class: "sport-select setting-select", "aria-label": "Local audio retention",
    onchange: (e) => {
      const v = e.target.value;
      customWrap.hidden = v !== "custom";
      if (v === "custom") return;
      if (v === "") saveSetting("GVE_AUDIO_RETENTION_DAYS", "", { unset: true });
      else saveSetting("GVE_AUDIO_RETENTION_DAYS", v);
    } }, [
    el("option", { value: "", text: "Keep audio forever" }),
    el("option", { value: "0", text: "Delete once transcribed" }),
    el("option", { value: "30", text: "Delete after 30 days" }),
    el("option", { value: "90", text: "Delete after 90 days" }),
    el("option", { value: "custom", text: "Custom days" }),
  ]);
  select.value = selected;
  return el("div", { class: "settings-grid" }, [
    settingField("Retention", select, "The transcript stays. This only prunes the local audio file after transcription is done."),
    customWrap,
  ]);
}

function saveRetentionDays(value) {
  const days = String(value || "").trim();
  if (!/^\d+$/.test(days)) return toast("Enter a whole number of days.", true);
  saveSetting("GVE_AUDIO_RETENTION_DAYS", days);
}

function positiveWholeDays(value) {
  const s = String(value || "").trim();
  if (!/^[1-9]\d*$/.test(s)) return 0;
  const n = Number(s);
  return Number.isSafeInteger(n) ? n : 0;
}

function humanBytes(n) {
  const units = ["B", "KB", "MB", "GB"];
  let value = Math.max(0, Number(n) || 0);
  let unit = units[0];
  for (const nextUnit of units) {
    unit = nextUnit;
    if (value < 1024 || unit === units[units.length - 1]) break;
    value /= 1024;
  }
  return unit === "B" ? `${Math.round(value)} B` : `${value.toFixed(1)} ${unit}`;
}

function renderArchivedCleanupSettings(d) {
  const retention = d.archived_retention || {};
  const current = retention.days || "";
  const days = positiveWholeDays(current);
  const known = ["", "30", "90", "180"];
  const selected = known.includes(current) ? current : "custom";
  const custom = el("input", { class: "author-input setting-days-input", type: "number", min: "1", step: "1",
    value: selected === "custom" ? current : "180", "aria-label": "Archived audio cleanup days" });
  const customWrap = el("div", { class: "setting-custom-days" }, [
    settingField("Days", custom, "Use 1 or more. 0 is not allowed for archived audio cleanup."),
    el("button", { class: "btn btn-outline", onclick: () => saveArchivedRetentionDays(custom.value) },
      [icon("check"), el("span", { text: "Save days" })]),
  ]);
  customWrap.hidden = selected !== "custom";
  const select = el("select", { class: "sport-select setting-select", "aria-label": "Archived audio cleanup",
    onchange: (e) => {
      const v = e.target.value;
      customWrap.hidden = v !== "custom";
      if (v === "custom") return;
      if (v === "") saveSetting("GVE_ARCHIVED_VOICE_RETENTION_DAYS", "", { unset: true });
      else saveSetting("GVE_ARCHIVED_VOICE_RETENTION_DAYS", v);
    } }, [
    el("option", { value: "", text: "Off" }),
    el("option", { value: "30", text: "30 days" }),
    el("option", { value: "90", text: "90 days" }),
    el("option", { value: "180", text: "180 days" }),
    el("option", { value: "custom", text: "Custom days" }),
  ]);
  select.value = selected;
  const btn = el("button", { class: "btn btn-outline", disabled: !days,
    onclick: () => previewArchivedCleanup(days) },
    [icon("trash"), el("span", { text: "Clean up now" })]);
  const stats = d.archived_audio || {};
  return el("div", { class: "settings-grid" }, [
    settingField("Age", select, "Off means archived audio is kept until you clean it manually."),
    customWrap,
    el("div", { class: "setting-action-row" }, [
      btn,
      settingNote(days ? "Preview runs first. Nothing is deleted until you confirm." : "Pick an age before cleaning archived audio."),
    ]),
    settingSwitch("Automatically clean up on app open", "When this is on and an age is set, GarminBridge cleans matching archived audio at startup.", !!retention.auto,
      (on) => saveSetting("GVE_ARCHIVED_VOICE_RETENTION_AUTO", "1", { unset: !on })),
    settingNote(`You have ${stats.count || 0} archived memos using ${humanBytes(stats.bytes || 0)}.`),
  ]);
}

function saveArchivedRetentionDays(value) {
  const days = String(value || "").trim();
  if (!/^[1-9]\d*$/.test(days)) return toast("Choose a whole number of days (1 or more).", true);
  saveSetting("GVE_ARCHIVED_VOICE_RETENTION_DAYS", days);
}

async function previewArchivedCleanup(days) {
  if (!positiveWholeDays(days)) return toast("Pick an archive age first.", true);
  showScan("Checking archived audio");
  try {
    const res = await engine(["voice-cleanup-archived", "--days", String(days)]);
    hideScan();
    const count = Number(res.count || 0);
    if (count === 0) return toast(`No archived memos older than ${days} days.`);
    openArchivedCleanupConfirm(res, days);
  } catch (e) {
    hideScan();
    toast(e.message || "Could not check archived audio.", true);
  }
}

function archivedCleanupRows(items) {
  const rows = (items || []).slice(0, 6).map((item) => el("li", {}, [
    el("div", { class: "nm", text: item.name || "Archived memo" }),
    el("span", { class: "eff", text: `${item.age_days || 0} days old · ${humanBytes(item.bytes || 0)}` }),
  ]));
  if ((items || []).length > rows.length) {
    rows.push(el("li", {}, [
      el("div", { class: "nm", text: `${items.length - rows.length} more archived memos` }),
      el("span", { class: "eff", text: "They are included in the same confirmed cleanup." }),
    ]));
  }
  return rows;
}

function openArchivedCleanupConfirm(res, days) {
  const count = Number(res.count || 0);
  const total = Number(res.total_bytes || 0);
  const memoWord = count === 1 ? "memo" : "memos";
  openModal({
    title: "Delete archived audio?",
    lead: `Delete ${count} archived ${memoWord} and free ~${humanBytes(total)}? The transcripts stay.`,
    list: archivedCleanupRows(res.items || []),
    warn: "Only archived .wav audio older than the selected age is deleted. Active memos, transcripts, and notes stay.",
    warnClass: "permanent",
    warnIcon: "trash",
    confirmLabel: "Delete archived audio",
    confirmDanger: true,
    blocked: false,
    onConfirm: () => applyArchivedCleanup(days),
  });
}

async function applyArchivedCleanup(days) {
  closeModal();
  showScan("Cleaning archived audio");
  try {
    const res = await engine(["voice-cleanup-archived", "--days", String(days), "--apply"]);
    try {
      setSettingsState(await engine(["settings-get"]));
    } catch (e) {
      // Cleanup already ran; keep the success toast even if the stats refresh fails.
    }
    hideScan();
    toast(res.message || "Archived audio cleanup finished.");
    if (state.view === "settings") renderSettings();
  } catch (e) {
    hideScan();
    toast(e.message || "Could not clean archived audio.", true);
    if (state.view === "settings") renderSettings();
  }
}

function renderAutoImportSettings(d) {
  return el("div", { class: "settings-grid" }, [
    settingSwitch("Pause auto-import", "Keeps GarminBridge from grabbing the watch automatically, so Garmin Express or an MTP app can use it.", !!d.auto_import_paused,
      (paused) => saveSetting("auto-import", paused ? "paused" : "active",
        { scan: paused ? "Pausing auto-import" : "Resuming auto-import",
          toast: paused ? "Auto-import paused. The watch is free for other apps." : "Auto-import resumed." })),
  ]);
}

function voiceDisplayTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

function voiceDurationText(seconds) {
  if (seconds == null) return "";
  const s = Math.max(0, Math.round(Number(seconds)));
  if (!Number.isFinite(s)) return "";
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
}

async function loadVoice(refresh = false) {
  if (refresh) showScan("Refreshing voice memos");
  else {
    renderVoiceSkeleton();
    await nextPaint();
  }
  try {
    const d = await engine(["voice-list"]);
    setVoiceState(d);
    renderVoice();
  } catch (e) {
    $("list").replaceChildren(emptyState("Could not read voice memos", e.message));
  } finally {
    hideScan();
  }
}

function setVoiceState(d) {
  const showArchived = !!state.voice.showArchived;
  const selected = state.voice.selected || new Set();
  const items = d.items || [];
  const valid = new Set(items.map((m) => m.audio_path));
  for (const path of [...selected]) if (!valid.has(path)) selected.delete(path);
  state.voice = { loaded: true, root: d.root || "", vault_configured: !!d.vault_configured,
    vault: d.vault || "", items, showArchived, selected };
}

function renderVoiceSkeleton() {
  const list = $("list");
  setListMode("voice");
  list.replaceChildren();
  for (let i = 0; i < 6; i++) {
    list.appendChild(el("div", { class: "sk-row voice-sk-row" }, [
      el("div", { class: "sk sk-ico" }),
      el("div", { class: "sk sk-line", style: `width:${56 + (i * 19) % 28}%` }),
      el("div", { class: "sk sk-chip" }),
    ]));
  }
}

function renderVoice() {
  $("content-toolbar").hidden = true;
  $("voice-toolbar").hidden = false;
  $("stale-banner").hidden = true;
  $("route-toolbar").hidden = true;
  setListMode("voice");
  renderVoiceArchiveToggle();
  const root = $("voice-root");
  root.textContent = state.voice.root ? `Folder: ${state.voice.root}` : "";
  root.title = state.voice.root || "";

  const list = $("list");
  list.replaceChildren();
  const showArchived = !!state.voice.showArchived;
  const q = (state.filters.search || "").trim();
  const items = visibleVoiceMemos();
  if (!items.length) {
    list.appendChild(q
      ? emptyState("No voice memos match", "Search checks memo names and transcripts.")
      : (showArchived
        ? emptyState("No archived memos", "Archive moves handled memos out of the active list without deleting them.")
        : emptyState("No active voice memos", "Import copies new memos from your Fenix into this local folder.")));
  } else {
    list.appendChild(el("div", { class: "group-head voice-head" }, [
      el("span", { class: "group-title", text: showArchived ? "Archived" : "Active voice memos" }),
      el("span", { class: "group-count", text: String(items.length) }),
      el("span", { class: "rule" }),
    ]));
    for (const memo of items) list.appendChild(voiceRow(memo));
  }
  renderVoiceBulkBar();
  renderVoiceStatus();
}

function renderVoiceArchiveToggle() {
  const group = $("voice-archive-toggle");
  if (!group) return;
  for (const b of group.querySelectorAll(".seg-btn")) {
    const on = (b.dataset.archived === "1") === !!state.voice.showArchived;
    b.classList.toggle("is-active", on);
    b.setAttribute("aria-pressed", String(on));
  }
}

function renderVoiceStatus() {
  const items = state.voice.items || [];
  const active = items.filter((m) => !m.archived).length;
  const archived = items.filter((m) => m.archived).length;
  const transcribed = items.filter((m) => m.has_transcript).length;
  const notes = items.filter((m) => m.note_exists).length;
  $("status-counts").textContent =
    `Voice memos: ${active} active · ${archived} archived · transcripts: ${transcribed} · notes: ${notes}`;
  $("status-updated").textContent = state.voice.vault_configured ? "Obsidian configured" : "No Obsidian vault configured";
}

function voiceSelectedSet() {
  if (!state.voice.selected) state.voice.selected = new Set();
  return state.voice.selected;
}

function selectedVoiceMemos() {
  const selected = voiceSelectedSet();
  return (state.voice.items || []).filter((m) => selected.has(m.audio_path));
}

function visibleVoiceMemos() {
  const showArchived = !!state.voice.showArchived;
  const q = (state.filters.search || "").trim().toLowerCase();
  return (state.voice.items || [])
    .filter((m) => !!m.archived === showArchived)
    .filter((m) => !q ||
      (m.name || "").toLowerCase().includes(q) ||
      (m.transcript || "").toLowerCase().includes(q));
}

function voiceAction(iconName, label, onClick, extraClass = "") {
  return el("button", {
    class: `btn btn-ghost voice-action${extraClass ? " " + extraClass : ""}`,
    title: label,
    "aria-label": label,
    onclick: onClick,
  }, [icon(iconName)]);
}

function voiceMenuAction(iconName, label, onClick, extraClass = "") {
  return el("button", {
    class: `voice-menu-item${extraClass ? " " + extraClass : ""}`,
    onclick: (e) => {
      e.preventDefault();
      closeVoiceMenus();
      onClick();
    },
  }, [icon(iconName), el("span", { text: label })]);
}

function closeVoiceMenus() {
  document.querySelectorAll(".voice-more[open]").forEach((d) => { d.open = false; });
}

function voiceMoreMenu(kids) {
  const menu = el("details", { class: "voice-more" }, [
    el("summary", { class: "btn btn-ghost voice-action voice-action-more", title: "More actions", "aria-label": "More memo actions" }, [
      el("span", { class: "voice-more-dot", text: "..." }),
    ]),
    el("div", { class: "voice-menu" }, kids),
  ]);
  menu.addEventListener("toggle", () => {
    if (!menu.open) return;
    document.querySelectorAll(".voice-more[open]").forEach((d) => { if (d !== menu) d.open = false; });
  });
  return menu;
}

function voiceRow(memo) {
  const transcript = memo.has_transcript
    ? (memo.transcript || "(empty transcript)")
    : "No transcript yet. Transcription is optional; it adds searchable text when you want it.";
  const q = state.filters.search || "";
  const selected = voiceSelectedSet().has(memo.audio_path);
  const check = el("input", { type: "checkbox", class: "row-check voice-check",
    "aria-label": "Select " + (memo.name || "voice memo") });
  check.checked = selected;
  const play = voiceAction("player-play", "Play audio", () => playVoiceMemo(memo));
  const transcribe = memo.has_transcript
    ? voiceAction("check", "Already transcribed", () => {}, "voice-action-done")
    : voiceAction("microphone", "Transcribe memo", () => transcribeVoiceMemo(memo), "voice-action-outline");
  if (memo.has_transcript) transcribe.disabled = true;
  const note = voiceAction(memo.note_exists ? "check" : "file-text",
    memo.note_exists ? "Already in notes" : "Send to notes", () => sendVoiceToNotes(memo),
    memo.note_exists ? "voice-action-done" : "voice-action-outline");
  if (memo.note_exists) note.disabled = true;
  const archiveIcon = memo.archived ? "arrow-up-right" : "arrow-down-right";
  const del = voiceAction("trash", "Delete local memo", () => deleteVoiceMemo(memo), "voice-action-danger");
  const more = voiceMoreMenu([
    voiceMenuAction("folder-open", "Reveal in Finder", () => revealVoiceMemo(memo)),
    voiceMenuAction("pencil", "Rename", () => openRenameVoiceMemo(memo)),
    voiceMenuAction(archiveIcon, memo.archived ? "Unarchive" : "Archive", () => archiveVoiceMemo(memo)),
  ]);
  const meta = [voiceDurationText(memo.duration), voiceDisplayTime(memo.time)].filter(Boolean).join(" · ");
  const row = el("article", { class: "voice-row" + (selected ? " is-selected" : "") }, [
    check,
    el("div", { class: "voice-main" }, [
      el("div", { class: "voice-title-line" }, [
        highlightedText(memo.name || "Voice memo", q, "voice-title"),
      ]),
      el("div", { class: "voice-meta", text: meta }),
      highlightedText(transcript, memo.has_transcript ? q : "", "voice-transcript" + (memo.has_transcript ? "" : " is-missing"), ""),
    ]),
    el("div", { class: "voice-actions" }, [
      el("div", { class: "voice-action-group" }, [play, transcribe, note]),
      el("span", { class: "voice-action-sep" }),
      more,
      del,
    ]),
  ]);
  check.addEventListener("change", () => {
    if (check.checked) voiceSelectedSet().add(memo.audio_path);
    else voiceSelectedSet().delete(memo.audio_path);
    row.classList.toggle("is-selected", check.checked);
    renderVoiceBulkBar();
  });
  return row;
}

async function playVoiceMemo(memo) {
  try { await invoke("play_audio", { path: memo.audio_path }); }
  catch { toast("Could not start playback. Use Reveal to open the memo in Finder.", true); }
}

async function revealVoiceMemo(memo) {
  try { await invoke("open_path", { path: memo.audio_path, reveal: true }); }
  catch { toast("Could not reveal that memo in Finder.", true); }
}

function voiceSettingsPane() {
  return $("settings-pane");
}

function openVoiceSettingsPane() {
  return openSettings("transcription");
}

function openTranscriptionOptionalPopup() {
  openModal({
    title: "Transcription is optional",
    lead: "It turns a memo into searchable text. The audio, archive, delete, play, and notes actions still work without it.",
    list: [
      el("li", { class: "modal-bullet", text: "Open Settings to choose a local backend or save a cloud API key." }),
      el("li", { class: "modal-bullet", text: "Local backends are installed by the app and keep audio on this Mac." }),
    ],
    warn: "Open Settings to choose or repair the transcription backend.",
    warnClass: "reversible", warnIcon: "file-text",
    confirmLabel: "Open Settings", confirmDanger: false, blocked: false,
    onConfirm: () => { closeModal(); openVoiceSettingsPane(); },
  });
}

function openSendToNotesSetup(memo) {
  openModal({
    title: "Send to notes",
    lead: "Pick the folder before the note is written.",
    list: [
      el("li", { class: "modal-bullet", text: "Send to notes creates one Markdown note for this voice memo." }),
      el("li", { class: "modal-bullet", text: "The folder you pick is where future voice memo notes are written." }),
      el("li", { class: "modal-bullet", text: "The note includes the recording date, transcript when available, and a link to the local audio." }),
    ],
    confirmLabel: "Choose folder", confirmDanger: false, blocked: false,
    onConfirm: async () => {
      closeModal();
      const chosen = await setNotesFolder(false);
      if (chosen) await doSendVoiceToNotes(memo);
      else toast("Choose a notes folder before sending memos to notes.");
    },
  });
}

function sendVoiceToNotes(memo) {
  if (!state.voice.vault_configured) {
    openSendToNotesSetup(memo);
    return;
  }
  doSendVoiceToNotes(memo);
}

async function doSendVoiceToNotes(memo) {
  showScan("Writing note");
  try {
    const d = await engine(["voice-note", "--audio", memo.audio_path]);
    hideScan();
    toast(d.message || "Sent to notes.");
    await loadVoice(true);
  } catch (e) {
    hideScan();
    toast(e.message || "Could not write the note. Choose the notes folder again and try.", true);
  }
}

async function setNotesFolder(refresh = true) {
  try {
    const chosen = await invoke("set_notes_folder");
    if (!chosen) return "";
    state.voice.vault_configured = true;
    state.voice.vault = chosen;
    toast("Notes folder set.");
    if (refresh) await loadVoice(true);
    return chosen;
  } catch {
    toast("Could not open the folder picker.");
    return "";
  }
}

async function transcribeVoiceMemo(memo) {
  showScan("Transcribing...", { elapsed: true, detail: memo.name || "Voice memo" });
  try {
    const d = await engine(["voice-transcribe", "--audio", memo.audio_path]);
    hideScan();
    toast(d.message || "Transcribed.");
    await loadVoice(true);
  } catch (e) {
    hideScan();
    const unconfigured = e.payload && e.payload.error === "transcription_unconfigured";
    if (unconfigured) openTranscriptionOptionalPopup();
    else toast(e.message || "Transcription did not finish. Try again later.", true);
  }
}

function openRenameVoiceMemo(memo) {
  const oldName = memo.name || "";
  const input = el("input", { class: "rename-input", type: "text", value: oldName, spellcheck: "false", "aria-label": "New memo name" });
  const note = el("div", { class: "rename-note" });

  openModal({
    title: "Rename voice memo",
    lead: "Renames the audio file and transcript sidecars together.",
    list: [el("li", { class: "rename-li" }, [input, note])],
    confirmLabel: "Rename", confirmDanger: false, blocked: true,
    onConfirm: () => doRenameVoiceMemo(memo, input.value.trim()),
  });

  const cbtn = $("modal-confirm");
  function update() {
    const nv = input.value.trim();
    const changed = !!nv && nv !== oldName;
    cbtn.disabled = !changed || nv.includes("/");
    if (!nv) { note.textContent = "Enter a memo name."; note.className = "rename-note"; return; }
    if (nv.includes("/")) { note.textContent = "Memo names cannot contain slashes."; note.className = "rename-note warn"; return; }
    if (!changed) { note.textContent = "Same as the current name."; note.className = "rename-note"; return; }
    note.textContent = "Keeps the audio and transcript files together.";
    note.className = "rename-note";
  }
  input.addEventListener("input", update);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !cbtn.disabled) doRenameVoiceMemo(memo, input.value.trim()); });
  update();
  setTimeout(() => { input.focus(); input.select(); }, 30);
}

async function doRenameVoiceMemo(memo, newName) {
  if (!newName || newName === memo.name) return;
  closeModal();
  showScan("Renaming memo");
  try {
    const d = await engine(["voice-rename", "--audio", memo.audio_path, "--name", newName]);
    hideScan();
    voiceSelectedSet().delete(memo.audio_path);
    toast(d.message || "Renamed.");
    await loadVoice(true);
  } catch (e) {
    hideScan();
    toast(e.message || "Could not rename that memo.", true);
  }
}

async function importVoiceMemos() {
  const btn = $("voice-import-btn");
  const ico = btn.querySelector(".ico");
  const label = btn.querySelector("span:not(.ico)");
  const oldLabel = label ? label.textContent : "";
  btn.disabled = true;
  btn.classList.add("is-busy");
  if (ico) setIcon(ico, "refresh");
  if (label) label.textContent = "Importing...";
  showScan("Importing voice memos...");
  try {
    const d = await engine(["voice-import"]);
    setVoiceState(d);
    hideScan();
    toast(d.message || "Voice memo import finished.");
    renderVoice();
  } catch (e) {
    hideScan();
    toast(e.message || "Voice memo import did not finish. Check the cable and try again.", true);
  } finally {
    btn.disabled = false;
    btn.classList.remove("is-busy");
    if (ico) setIcon(ico, "microphone");
    if (label) label.textContent = oldLabel || "Import";
  }
}

async function archiveVoiceMemo(memo) {
  showScan(memo.archived ? "Moving memo back to active" : "Archiving memo");
  try {
    const args = ["voice-archive", "--audio", memo.audio_path];
    if (memo.archived) args.push("--undo");
    const d = await engine(args);
    hideScan();
    toast(d.message || (memo.archived ? "Moved back to active memos." : "Archived."));
    await loadVoice(true);
  } catch (e) {
    hideScan();
    toast(e.message || "Could not move that memo.", true);
  }
}

function deleteVoiceMemo(memo) {
  const extras = [memo.has_transcript ? "transcript" : "", memo.has_raw_transcript ? "raw transcript" : ""].filter(Boolean);
  const detail = extras.length
    ? `Deletes the local audio and ${extras.join(" + ")} from this Mac.`
    : "Deletes the local audio from this Mac.";
  openModal({
    title: "Delete local memo",
    lead: "The memo has already been copied off the watch. This removes only the local copy; it does not change Garmin Connect or your notes.",
    list: [el("li", {}, [
      el("span", { class: "nm", text: memo.name || "Voice memo" }),
      el("span", { class: "eff", text: detail }),
    ])],
    warn: "Permanent local delete. There is no undo from inside the app.",
    warnClass: "permanent", warnIcon: "alert-triangle",
    confirmLabel: "Delete local copy", confirmDanger: true, blocked: false,
    onConfirm: () => doDeleteVoiceMemo(memo),
  });
}

async function doDeleteVoiceMemo(memo) {
  closeModal();
  showScan("Deleting local memo");
  try {
    const d = await engine(["voice-delete", "--audio", memo.audio_path]);
    hideScan();
    voiceSelectedSet().delete(memo.audio_path);
    toast(d.message || "Deleted the local memo copy.");
    await loadVoice(true);
  } catch (e) {
    hideScan();
    toast(e.message || "Could not delete that local memo.", true);
  }
}

function renderVoiceBulkBar() {
  const rows = selectedVoiceMemos();
  const n = rows.length;
  $("bulkbar").hidden = n === 0;
  if (!n) return;
  renderBulkSelectVisible();
  const transcribeRows = rows.filter((m) => !m.has_transcript);
  const archiveRows = rows.filter((m) => !m.archived);
  const unarchiveRows = rows.filter((m) => m.archived);
  const parts = [];
  if (transcribeRows.length) parts.push(`${transcribeRows.length} to transcribe`);
  if (archiveRows.length) parts.push(`${archiveRows.length} to archive`);
  if (unarchiveRows.length) parts.push(`${unarchiveRows.length} to unarchive`);
  $("bulk-count").textContent = (n === 1 ? "1 memo selected" : `${n} memos selected`) + (parts.length ? ` · ${parts.join(" · ")}` : "");

  const transcribe = $("bulk-add-watch"), archive = $("bulk-remove");
  const del = $("bulk-delete-connect"), unarchive = $("bulk-voice-unarchive");
  transcribe.hidden = transcribeRows.length === 0;
  archive.hidden = archiveRows.length === 0;
  unarchive.hidden = unarchiveRows.length === 0;
  del.hidden = false;
  transcribe.replaceChildren(icon("microphone"), el("span", { text: transcribeRows.length === 1 ? "Transcribe 1" : `Transcribe ${transcribeRows.length}` }));
  archive.replaceChildren(icon("arrow-down-right"), el("span", { text: archiveRows.length === 1 ? "Archive 1" : `Archive ${archiveRows.length}` }));
  unarchive.replaceChildren(icon("arrow-up-right"), el("span", { text: unarchiveRows.length === 1 ? "Unarchive 1" : `Unarchive ${unarchiveRows.length}` }));
  del.replaceChildren(icon("trash"), el("span", { text: n === 1 ? "Delete 1" : `Delete ${n}` }));
}

async function runVoiceBulk(label, memos, argsFor, successText) {
  if (!memos.length) return toast("No selected memos need that action.");
  showScan(label, label.toLowerCase().startsWith("transcribing") ? { elapsed: true } : {});
  let done = 0, fail = 0, lastMsg = "";
  for (const memo of memos) {
    try {
      await engine(argsFor(memo));
      done++;
    } catch (e) {
      if (e.payload && e.payload.error === "transcription_unconfigured") {
        hideScan();
        openTranscriptionOptionalPopup();
        return;
      }
      fail++;
      lastMsg = e.message || "One memo could not be changed.";
    }
  }
  hideScan();
  voiceSelectedSet().clear();
  toast(fail ? `Finished ${done}, ${fail} not changed: ${lastMsg}` : successText(done), fail > 0);
  await loadVoice(true);
}

function bulkTranscribeVoice() {
  const rows = selectedVoiceMemos().filter((m) => !m.has_transcript);
  runVoiceBulk("Transcribing memos", rows,
    (m) => ["voice-transcribe", "--audio", m.audio_path],
    (n) => n === 1 ? "Transcribed 1 memo." : `Transcribed ${n} memos.`);
}

function bulkArchiveVoice() {
  const rows = selectedVoiceMemos().filter((m) => !m.archived);
  runVoiceBulk("Archiving memos", rows,
    (m) => ["voice-archive", "--audio", m.audio_path],
    (n) => n === 1 ? "Archived 1 memo." : `Archived ${n} memos.`);
}

function bulkUnarchiveVoice() {
  const rows = selectedVoiceMemos().filter((m) => m.archived);
  runVoiceBulk("Unarchiving memos", rows,
    (m) => ["voice-archive", "--audio", m.audio_path, "--undo"],
    (n) => n === 1 ? "Unarchived 1 memo." : `Unarchived ${n} memos.`);
}

function bulkDeleteVoice() {
  const rows = selectedVoiceMemos();
  if (!rows.length) return;
  openModal({
    title: "Delete selected memos",
    lead: "This removes the selected local audio and transcript files from this Mac.",
    list: rows.map((m) => el("li", {}, [
      el("span", { class: "nm", text: m.name || "Voice memo" }),
      el("span", { class: "eff", text: m.has_transcript ? "Deletes audio and transcript sidecars." : "Deletes audio." }),
    ])),
    warn: "Permanent local delete. There is no undo from inside the app.",
    warnClass: "permanent", warnIcon: "alert-triangle",
    confirmLabel: "Delete local copies", confirmDanger: true, blocked: false,
    onConfirm: () => {
      closeModal();
      runVoiceBulk("Deleting memos", rows,
        (m) => ["voice-delete", "--audio", m.audio_path],
        (n) => n === 1 ? "Deleted 1 memo." : `Deleted ${n} memos.`);
    },
  });
}

// ===================================================================================
// Route sort + area filtering. All geo lookups (geocode, "use my location")
// go through the engine — the webview CSP blocks external HTTP — so this file only
// holds UI + the km math. Anchor = a point (searched place / saved place / approx
// present position); routes are kept when their START is within `radiusKm` of it.
// ===================================================================================
function routesInView() {
  const k = state.filters.kind;
  return k === "all" || k === "course";
}

function watchCourseRows() {
  return state.snap.items.filter((r) =>
    r.kind === "course" && r.on_watch && r.watch_file && r.actions.can_rm_watch);
}

function selectedCourseRows() {
  return state.snap.items.filter((r) => r.kind === "course" && state.selected.has(r.uid));
}

function selectedRows() {
  return state.snap.items.filter((r) => state.selected.has(r.uid));
}

function selectableRow(r) {
  return !!(r.actions.can_rm_watch ||
    (r.kind === "course" && (r.actions.can_add_to_watch || r.actions.can_rm_connect || r.id || r.watch_file || r.name)));
}

function bulkAddRows(rows = selectedRows()) {
  return rows.filter((r) => r.kind === "course" && !r.on_watch && r.actions.can_add_to_watch);
}

function bulkRemoveRows(rows = selectedRows()) {
  return rows.filter((r) => r.actions.can_rm_watch);
}

function bulkConnectDeleteRows(rows = selectedRows()) {
  return rows.filter((r) => r.kind === "course" && r.in_connect && r.actions.can_rm_connect);
}

function readdableSource(r) {
  if (r.in_connect) return "Garmin Connect";
  if (r.imported) return "your Mac library";
  return "";
}

function watchRemovalEffect(r) {
  const src = readdableSource(r);
  if (src === "Garmin Connect") return `deleted from ${r.folder} (stays in Garmin Connect)`;
  if (src) return `deleted from ${r.folder} (re-addable from your Mac library)`;
  return `deleted from ${r.folder} (permanently removed: not in Connect or your Mac library)`;
}

function defaultTrimNearestCount() {
  const rows = watchCourseRows();
  if (!rows.length) return 10;
  if (!state.anchor) return Math.min(10, rows.length);
  const within = rows.filter((r) => {
    const d = anchorDistKm(r);
    return d != null && d <= state.radiusKm;
  }).length;
  return Math.min(rows.length, within || 10);
}

function sortedWatchCoursesByAnchor() {
  const asc = (v) => (v == null ? Infinity : v);
  const byName = (a, b) => (a.name || "").localeCompare(b.name || "");
  return watchCourseRows().slice().sort((a, b) => asc(anchorDistKm(a)) - asc(anchorDistKm(b)) || byName(a, b));
}

function renderRouteToolbar() {
  const tb = $("route-toolbar");
  if (!routesInView()) { tb.hidden = true; closeAnchorPop(); return; }
  tb.hidden = false;
  $("route-sort").value = state.sort;
  renderAnchorChip();
  renderCurateWatchButton();
}

function renderAnchorChip() {
  const chip = $("anchor-chip");
  if (!state.anchor) { chip.hidden = true; return; }
  chip.hidden = false;
  const approx = state.anchor.approximate ? " (approx)" : "";
  chip.replaceChildren(
    icon("map-pin"),
    el("span", { text: `${state.anchor.label}${approx} · within ${state.radiusKm} km` }),
    el("button", { class: "anchor-chip-x", title: "Clear area filter", onclick: clearAnchor }, "×"),
  );
}

function renderCurateWatchButton() {
  const btn = $("curate-watch-btn");
  const count = watchCourseRows().length;
  btn.disabled = count === 0;
  btn.title = count ? "Choose which routes stay on your watch" : "No routes on your watch yet";
}

function setAnchor(a) {
  const intent = state.pendingAnchorIntent;
  state.pendingAnchorIntent = null;
  state.anchor = a;
  state.sort = "nearest";          // proximity is the whole point once you filter by place
  closeAnchorPop();
  render();
  if (intent && intent.type === "trim-nearest") setTimeout(() => trimWatchToNearest(intent.count), 0);
}
function clearAnchor() {
  state.pendingAnchorIntent = null;
  state.anchor = null;
  if (state.sort === "nearest") state.sort = "name";
  closeAnchorPop();
  render();
}

let _anchorPopOpen = false;
function toggleAnchorPop() { _anchorPopOpen ? closeAnchorPop(true) : openAnchorPop(); }
function closeAnchorPop(clearPending = false) {
  if (clearPending) state.pendingAnchorIntent = null;
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
  if (f.kind === "all" && (f.loc === "connect-only" || f.loc === "watch-only" || f.sport || f.tag || f.folder)) return [];
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
      highlightedText(p.name || "Unnamed", state.filters.search, "row-name"),
      el("div", { class: "row-meta place-coords", text: fmtCoord(p.lat, p.lon) }),
    ]),
    el("div", { class: "place-actions" }, [rename, del]),
  ]);
}

function routeTagChips(r) {
  if (r.kind !== "course") return null;
  const chips = [];
  if (r.route_folder) chips.push(el("span", { class: "row-flag row-tag-chip row-folder-chip", title: "Route folder", text: r.route_folder }));
  for (const tag of r.tags || []) chips.push(el("span", { class: "row-flag row-tag-chip", title: "Route tag", text: tag }));
  return chips.length ? el("div", { class: "row-tags-line" }, chips) : null;
}

function routeTagsButton(r) {
  const b = el("button", { class: "row-tags-btn", title: "Edit route tags", "aria-label": "Edit tags for " + (r.name || "") },
    [el("span", { text: "Tags" })]);
  if (r.tag_key) b.addEventListener("click", () => openRouteTags(r));
  else {
    b.disabled = true;
    b.title = "Tags need a Mac route library FIT backup.";
  }
  return b;
}

function rowNode(r) {
  const selected = state.selected.has(r.uid);
  const check = el("input", { type: "checkbox", class: "row-check" });
  check.checked = selected;
  if (!selectableRow(r)) check.disabled = true;
  check.addEventListener("change", () => {
    if (check.checked) state.selected.add(r.uid); else state.selected.delete(r.uid);
    render();
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
  const titleLine = [highlightedText(r.name || "Unnamed", state.filters.search, "row-name")];
  if (stale) titleLine.push(el("span", { class: "row-flag", title: r.location_detail, text: "Stale route" }));
  const actions = [];
  if (r.kind === "course") actions.push(routeTagsButton(r));
  actions.push(renameBtn);

  const row = el("div", { class: "row" + (selected ? " is-selected" : "") }, [
    check,
    identityNode(r),
    el("div", { class: "row-main" }, [
      el("div", { class: "row-title-line" }, titleLine),
      metaNode(r),
      routeTagChips(r),
      conditionsRow(r),
    ]),
    el("div", { class: "badges" }, [connectBadge, watchBadge]),
    el("div", { class: "row-actions" }, actions),
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
  if (state.view === "voice") { renderVoiceBulkBar(); return; }
  const rows = selectedRows();
  const n = rows.length;
  const addRows = bulkAddRows(rows);
  const removeRows = bulkRemoveRows(rows);
  const deleteRows = bulkConnectDeleteRows(rows);
  $("bulkbar").hidden = n === 0;
  if (n) renderBulkSelectVisible();
  const parts = [];
  if (addRows.length) parts.push(`${addRows.length} to add`);
  if (removeRows.length) parts.push(`${removeRows.length} on watch`);
  if (deleteRows.length) parts.push(`${deleteRows.length} in Connect`);
  $("bulk-count").textContent = (n === 1 ? "1 selected" : `${n} selected`) + (parts.length ? ` · ${parts.join(" · ")}` : "");
  const add = $("bulk-add-watch"), remove = $("bulk-remove"), del = $("bulk-delete-connect");
  $("bulk-voice-unarchive").hidden = true;
  add.hidden = addRows.length === 0;
  remove.hidden = removeRows.length === 0;
  del.hidden = deleteRows.length === 0;
  add.replaceChildren(icon("plus"), el("span", { text: addRows.length === 1 ? "Add 1 to watch" : `Add ${addRows.length} to watch` }));
  remove.replaceChildren(icon("device-watch"), el("span", { text: removeRows.length === 1 ? "Remove 1 from watch" : `Remove ${removeRows.length} from watch` }));
  del.replaceChildren(icon("trash"), el("span", { text: deleteRows.length === 1 ? "Delete 1 from Connect" : `Delete ${deleteRows.length} from Connect` }));
}

function visibleSelectionKeys() {
  if (state.view === "voice") return visibleVoiceMemos().map((m) => m.audio_path).filter(Boolean);
  return visibleRows().filter(selectableRow).map((r) => r.uid).filter(Boolean);
}

function currentSelectionSet() {
  return state.view === "voice" ? voiceSelectedSet() : state.selected;
}

function allVisibleSelected() {
  const keys = visibleSelectionKeys();
  const selected = currentSelectionSet();
  return keys.length > 0 && keys.every((k) => selected.has(k));
}

function renderBulkSelectVisible() {
  const btn = $("bulk-select-visible");
  const all = allVisibleSelected();
  btn.disabled = visibleSelectionKeys().length === 0;
  btn.replaceChildren(icon(all ? "x" : "check"), el("span", { text: all ? "Deselect all" : "Select all" }));
}

function toggleVisibleSelection() {
  const selected = currentSelectionSet();
  const keys = visibleSelectionKeys();
  if (keys.length === 0) return;
  if (keys.every((k) => selected.has(k))) selected.clear();
  else for (const k of keys) selected.add(k);
  if (state.view === "voice") renderVoice();
  else render();
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
  if (blocked) { warn = pv.kind === "location" ? "Plug your Fenix in over USB to change its saved points." : "Plug your Fenix in over USB to change what is on the watch."; }
  else if (pv.kind === "location" && pv.permanent) { warn = "This permanently removes the saved point from your Fenix and the Mac backup. There is no undo."; warnClass = "permanent"; }
  else if (pv.permanent) { warn = "This permanently deletes from Garmin Connect. There is no trash, and it does not remove the copy on your watch."; warnClass = "permanent"; }
  else if (pv.action === "rm-watch" || pv.action === "clean-watch") warn = "Removing from the watch doesn't delete it. Each item stays where it's saved (shown below), so you can re-add it any time.";
  else if (pv.action === "add-to-watch") warn = "Copied onto the watch. It stays in Garmin Connect too.";

  openModal({
    title: pv.verb, lead, list,
    warn, warnClass, warnIcon: warnClass === "permanent" ? "alert-triangle" : "device-watch",
    confirmLabel: pv.permanent ? "Delete permanently" : pv.verb,
    confirmDanger: pv.permanent,
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

function openTrimConfirm(title, keepSet) {
  const rows = watchCourseRows();
  if (!rows.length) return toast("No routes on the watch to trim.");
  const removeRows = rows.filter((r) => !keepSet.has(r.uid));
  if (!removeRows.length) return toast("Nothing to trim.");

  const keepCount = rows.length - removeRows.length;
  const readdable = removeRows.filter((r) => readdableSource(r));
  const permanent = removeRows.filter((r) => !readdableSource(r));
  const connected = state.snap.watch.connected;
  const opt = permanent.length ? el("input", { type: "checkbox", class: "trim-optin-check" }) : null;
  const list = readdable.map((r) => el("li", {}, [
    el("span", { class: "nm", text: r.name || "Unnamed" }),
    el("span", { class: "eff", text: watchRemovalEffect(r) }),
  ]));

  if (permanent.length) {
    list.push(el("li", { class: "trim-optin" }, [
      el("label", { class: "trim-optin-label" }, [
        opt,
        el("span", { text: `Also remove ${permanent.length === 1 ? "1 route" : permanent.length + " routes"} that cannot be re-added` }),
      ]),
      el("span", { class: "eff", text: "Off by default. Leave this unchecked to keep them on the watch." }),
    ]));
    permanent.forEach((r) => list.push(el("li", { class: "trim-permanent-row" }, [
      el("span", { class: "nm", text: r.name || "Unnamed" }),
      el("span", { class: "eff", text: watchRemovalEffect(r) }),
    ])));
  }

  const chosenRows = () => readdable.concat(opt && opt.checked ? permanent : []);
  const warnForState = () => {
    if (!connected) return ["Plug your Fenix in over USB to change what is on the watch.", "reversible", "alert-triangle"];
    if (permanent.length && opt && opt.checked) {
      return [`This permanently removes ${permanent.length === 1 ? "1 route" : permanent.length + " routes"} that are not in Garmin Connect or your Mac library. There is no undo.`, "permanent", "alert-triangle"];
    }
    if (permanent.length) {
      return [`${permanent.length === 1 ? "1 route" : permanent.length + " routes"} cannot be re-added, so this trim leaves them on the watch unless you opt in.`, "reversible", "device-watch"];
    }
    const subject = removeRows.length === 1 ? "a route" : "routes";
    const pronoun = removeRows.length === 1 ? "it" : "them";
    return [`Removing ${subject} from the watch doesn't delete ${pronoun}. Each stays where it's saved (shown below), so you can re-add ${pronoun} any time.`, "reversible", "device-watch"];
  };

  const [warn, warnClass, warnIcon] = warnForState();
  openModal({
    title,
    lead: `${keepCount === 1 ? "1 route" : keepCount + " routes"} chosen to stay on the watch. Review what can be removed:`,
    list,
    warn, warnClass, warnIcon,
    confirmLabel: "Trim watch", confirmDanger: false, blocked: !connected,
    onConfirm: () => {
      const chosen = chosenRows();
      if (!chosen.length) return toast("Nothing to trim.");
      return runJobs([["apply", "rm-watch", "course", { watchFile: chosen.map((r) => r.watch_file).join(",") }]], true);
    },
  });

  const cbtn = $("modal-confirm");
  const update = () => {
    const [nextWarn, nextWarnClass, nextWarnIcon] = warnForState();
    setModalWarn(nextWarn, nextWarnClass, nextWarnIcon);
    cbtn.className = "btn " + (nextWarnClass === "permanent" ? "btn-danger" : "btn-primary");
    cbtn.disabled = !connected || chosenRows().length === 0;
  };
  if (opt) opt.addEventListener("change", update);
  update();
}

function openCurateWatchChooser() {
  const rows = watchCourseRows();
  if (!rows.length) return toast("No routes on your watch yet.");

  const selectedRows = selectedCourseRows();
  const selectedOnWatch = rows.filter((r) => state.selected.has(r.uid)).length;
  const selectGuide = selectedOnWatch
    ? `${selectedOnWatch === 1 ? "1 on-watch route" : selectedOnWatch + " on-watch routes"} selected.`
    : (selectedRows.length
      ? "The selected routes are not on your watch. Tick on-watch routes to keep."
      : "Tick the on-watch routes you want to keep first.");
  const keepBtn = el("button", {
    class: "btn btn-outline",
    disabled: selectedOnWatch === 0,
    title: selectedOnWatch ? "Review what will be removed" : "Tick routes on your watch first",
    onclick: keepOnlySelectedOnWatch,
  }, "Review selected");

  const countInput = el("input", {
    class: "curate-count-input tnum",
    type: "number",
    min: "1",
    max: String(rows.length),
    step: "1",
    value: String(defaultTrimNearestCount()),
    disabled: !state.anchor,
    "aria-label": "Routes to keep on watch",
  });
  const anchorGuide = state.anchor
    ? `Routes near: ${state.anchor.label}.`
    : "Set an area first.";
  const nearestBtn = el("button", {
    class: "btn btn-outline",
    title: state.anchor ? "Review nearest route trim" : "Set an area first",
    onclick: state.anchor
      ? () => trimWatchToNearest(countInput.value)
      : (e) => {
        e.stopPropagation();
        state.pendingAnchorIntent = { type: "trim-nearest", count: countInput.value };
        closeModal();
        setTimeout(openAnchorPop, 0);
      },
  }, state.anchor ? "Review nearest" : "Set area");

  openModal({
    title: "Curate watch",
    lead: `${rows.length === 1 ? "1 route" : rows.length + " routes"} on your watch. Choose how to decide what stays.`,
    list: [
      el("li", { class: "curate-choice" }, [
        el("div", { class: "curate-choice-copy" }, [
          el("div", { class: "curate-choice-title", text: "Keep only the routes I've selected" }),
          el("div", { class: "curate-choice-desc", text: "Review every other on-watch route for removal." }),
          el("div", { class: "curate-choice-note" + (selectedOnWatch ? "" : " is-warn"), text: selectGuide }),
        ]),
        el("div", { class: "curate-choice-actions" }, keepBtn),
      ]),
      el("li", { class: "curate-choice" }, [
        el("div", { class: "curate-choice-copy" }, [
          el("div", { class: "curate-choice-title", text: "Keep the nearest N to an area" }),
          el("div", { class: "curate-choice-desc", text: "Pick how many nearby on-watch routes should stay." }),
          el("div", { class: "curate-choice-note" + (state.anchor ? "" : " is-warn"), text: anchorGuide }),
        ]),
        el("div", { class: "curate-choice-actions" }, [countInput, nearestBtn]),
      ]),
    ],
    hideConfirm: true,
  });
}

function keepOnlySelectedOnWatch() {
  const keepSet = new Set(selectedCourseRows().map((r) => r.uid));
  if (!keepSet.size) return toast("Select at least one route to keep.");
  // Guard the footgun: if none of the selected routes are actually on the watch, "keep only
  // these" would propose clearing everything on it. Make the user pick on-watch routes to keep.
  if (!watchCourseRows().some((r) => keepSet.has(r.uid)))
    return toast("None of the selected routes are on your watch. Select the on-watch routes to keep.");
  openTrimConfirm("Keep only these on watch", keepSet);
}

function trimWatchToNearest(count) {
  if (!state.anchor) return toast("Choose an area first.");
  const rows = sortedWatchCoursesByAnchor();
  if (!rows.length) return toast("No routes on the watch to trim.");
  const fallback = defaultTrimNearestCount();
  const requested = Number.parseInt(count, 10);
  const n = Math.max(1, Math.min(rows.length, Number.isFinite(requested) ? requested : fallback));
  openTrimConfirm("Trim watch to nearest routes", new Set(rows.slice(0, n).map((r) => r.uid)));
}

function bulkRemove() {
  const allRows = selectedRows();
  const rows = bulkRemoveRows(allRows);
  if (!rows.length) return;
  const byKind = {};
  for (const r of rows) (byKind[r.kind] ||= []).push(r);
  const connected = state.snap.watch.connected;
  const hasPermanent = rows.some((r) => r.kind === "course" && !readdableSource(r));
  const unchanged = allRows.length - rows.length;

  openModal({
    title: "Remove from your Fenix",
    lead: `Remove ${rows.length === 1 ? "1 on-watch item" : rows.length + " on-watch items"} from the watch.${unchanged ? ` ${unchanged} selected item${unchanged === 1 ? "" : "s"} will not change in this action.` : ""}`,
    list: rows.map((r) => el("li", {}, [
      el("span", { class: "nm", text: r.name || "Unnamed" }),
      el("span", { class: "eff", text: r.kind === "course" ? watchRemovalEffect(r) : `deleted from ${r.folder}` }),
    ])),
    warn: connected
      ? (hasPermanent
        ? "Routes not in Garmin Connect or your Mac library cannot be re-added after removal."
        : "Removing items from the watch doesn't delete them. Route sources are shown below, so you can re-add saved routes any time.")
      : "Plug your Fenix in over USB to change what is on the watch.",
    warnClass: "reversible",
    warnIcon: "device-watch",
    confirmLabel: "Remove from watch", confirmDanger: false, blocked: !connected,
    onConfirm: () => runJobs(
      Object.entries(byKind).map(([kind, rs]) => ["apply", "rm-watch", kind, { watchFile: rs.map((r) => r.watch_file).join(",") }]), true),
  });
  $("modal-confirm").className = "btn btn-blue";
}

function bulkAddToWatch() {
  const allRows = selectedRows();
  const rows = bulkAddRows(allRows);
  if (!rows.length) return;
  const connected = state.snap.watch.connected;
  const unchanged = allRows.length - rows.length;
  openModal({
    title: "Add selected routes to your Fenix",
    lead: `Add ${rows.length === 1 ? "1 off-watch route" : rows.length + " off-watch routes"} to the watch.${unchanged ? ` ${unchanged} selected item${unchanged === 1 ? "" : "s"} will not change in this action.` : ""}`,
    list: rows.map((r) => el("li", {}, [
      el("span", { class: "nm", text: r.name || "Unnamed" }),
      el("span", { class: "eff", text: `copied into ${r.folder} on your Fenix; stays in Garmin Connect` }),
    ])),
    warn: connected
      ? "Needs your Fenix on USB. If a route has no matching Mac backup, the app will leave that route unchanged and tell you."
      : "Plug your Fenix in over USB to add routes to the watch.",
    warnClass: "reversible", warnIcon: "device-watch",
    confirmLabel: "Add to watch", confirmDanger: false, blocked: !connected,
    onConfirm: () => runJobs(rows.map((r) => ["apply", "add-to-watch", "course", { id: r.id }]), true),
  });
  $("modal-confirm").className = "btn btn-blue";
}

function bulkDeleteConnect() {
  const allRows = selectedRows();
  const rows = bulkConnectDeleteRows(allRows);
  if (!rows.length) return;
  const unchanged = allRows.length - rows.length;
  openModal({
    title: "Delete from Garmin Connect",
    lead: `Delete ${rows.length === 1 ? "1 route" : rows.length + " routes"} from Garmin Connect.${unchanged ? ` ${unchanged} selected item${unchanged === 1 ? "" : "s"} will not change in this action.` : ""}`,
    list: rows.map((r) => el("li", {}, [
      el("span", { class: "nm", text: r.name || "Unnamed" }),
      el("span", { class: "eff", text: r.on_watch
        ? "permanently deleted from Garmin Connect; the watch copy stays on your Fenix"
        : "permanently deleted from Garmin Connect" }),
    ])),
    warn: "Permanent Connect delete. There is no undo, and it does not remove any copy already on your watch.",
    warnClass: "permanent", warnIcon: "alert-triangle",
    confirmLabel: "Delete from Connect", confirmDanger: true, blocked: false,
    onConfirm: () => runJobs(rows.map((r) => ["apply", "rm-connect", "course", { id: r.id }]), false),
  });
}

async function reviewStale() {
  let pv;
  try { pv = await engine(["preview", "--action", "clean-watch", "--kind", "course"]); }
  catch (e) { return toast(e.message, true); }
  if (!pv.count) return toast("No stale routes to clean right now.");
  openConfirm(pv, [["apply", "clean-watch", "course", {}]]);
}

// ---- route tags (Mac-side sidecar only) ----
function openRouteTags(r) {
  if (!r.tag_key) return;
  const tagsInput = el("input", { class: "rename-input", type: "text",
    value: (r.tags || []).join(", "), spellcheck: "false", "aria-label": "Tags" });
  const folderInput = el("input", { class: "rename-input", type: "text",
    value: r.route_folder || "", spellcheck: "false", "aria-label": "Folder" });
  const note = el("div", { class: "rename-note",
    text: "Saved on this Mac only. Does not change Garmin Connect or your Fenix." });
  openModal({
    title: "Tags for route",
    lead: "Stored against the Mac route library file.",
    list: [el("li", { class: "rename-li" }, [
      el("label", { class: "import-field" }, [el("span", { text: "Tags" }), tagsInput]),
      el("label", { class: "import-field" }, [el("span", { text: "Folder" }), folderInput]),
      note,
    ])],
    confirmLabel: "Save", confirmDanger: false, blocked: false,
    onConfirm: () => saveRouteTags(r, tagsInput.value, folderInput.value),
  });
  const onEnter = (e) => { if (e.key === "Enter") saveRouteTags(r, tagsInput.value, folderInput.value); };
  tagsInput.addEventListener("keydown", onEnter);
  folderInput.addEventListener("keydown", onEnter);
  setTimeout(() => { tagsInput.focus(); tagsInput.select(); }, 30);
}

async function saveRouteTags(r, tagsCsv, folder) {
  if (!r.tag_key) return;
  closeModal();
  showScan("Saving tags");
  try {
    await engine(["route-tags-set", "--key", r.tag_key, "--tags", tagsCsv, "--folder", folder]);
    state.snap = await engine(["snapshot"]);
    populateSports();
    render();
    hideScan();
    toast("Tags saved.");
  } catch (e) {
    hideScan();
    toast(e.message || "Could not save tags.", true);
  }
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
function setModalWarn(warn, warnClass, warnIcon) {
  const w = $("modal-warn");
  if (warn) {
    w.hidden = false; w.className = "modal-warn " + (warnClass || "reversible");
    w.replaceChildren(icon(warnIcon || "device-watch"), el("span", { text: warn }));
  } else w.hidden = true;
}

function openModal({ title, lead, list, warn, warnClass, warnIcon, confirmLabel, confirmDanger, blocked, onConfirm, hideConfirm }) {
  $("modal-title").textContent = title;
  $("modal-lead").textContent = lead || "";
  $("modal-list").replaceChildren(...(list || []));
  setModalWarn(warn, warnClass, warnIcon);
  const cbtn = $("modal-confirm");
  cbtn.hidden = !!hideConfirm;
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

let scanTimer;
function elapsedText(ms) {
  const total = Math.max(0, Math.floor(ms / 1000));
  const m = Math.floor(total / 60);
  const s = String(total % 60).padStart(2, "0");
  return `${m}:${s}`;
}
function showScan(text, opts = {}) {
  clearInterval(scanTimer);
  scanTimer = null;
  $("scan-text").textContent = text || "";
  const elapsed = $("scan-elapsed");
  elapsed.hidden = !opts.elapsed;
  elapsed.textContent = "0:00";
  const sub = $("scan-subtext");
  sub.hidden = !opts.detail;
  sub.textContent = opts.detail || "";
  $("scan").classList.toggle("is-timed", !!opts.elapsed);
  if (opts.elapsed) {
    const started = Date.now();
    scanTimer = setInterval(() => { elapsed.textContent = elapsedText(Date.now() - started); }, 1000);
  }
  $("refresh-btn").classList.add("refreshing");
  $("scan").hidden = false;
}
function hideScan() {
  clearInterval(scanTimer);
  scanTimer = null;
  $("scan").hidden = true;
  $("refresh-btn").classList.remove("refreshing");
}

function segActive(groupId, btn) {
  for (const b of $(groupId).querySelectorAll(".seg-btn")) {
    b.classList.toggle("is-active", b === btn);
    b.setAttribute("aria-pressed", String(b === btn));
  }
}

// ---- appearance: light / dark / system (persisted in localStorage) ----------------------------
function resolvedTheme(pref) {
  const dark = pref === "dark" || (pref === "system" && matchMedia("(prefers-color-scheme: dark)").matches);
  return dark ? "dark" : "light";
}
// The masthead icon mirrors the CHOSEN mode: sun = light, moon = dark, split circle = system.
function iconForPref(pref) { return pref === "light" ? "sun" : pref === "dark" ? "moon" : "theme-auto"; }
function applyThemePref(pref) {
  try { localStorage.setItem("gb-theme", pref); } catch (e) { /* private mode: run for this session only */ }
  document.documentElement.setAttribute("data-theme", resolvedTheme(pref));
  const ic = $("theme-icon"); if (ic) setIcon(ic, iconForPref(pref));
}
function initTheme() {
  let pref = "system";
  try { pref = localStorage.getItem("gb-theme") || "system"; } catch (e) { /* ignore */ }
  const sel = $("theme-select");
  if (sel) { sel.value = pref; sel.addEventListener("change", (e) => applyThemePref(e.target.value)); }
  applyThemePref(pref);
  // keep following the OS while the preference is "system"
  try {
    matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
      let p = "system"; try { p = localStorage.getItem("gb-theme") || "system"; } catch (e) { /* ignore */ }
      if (p === "system") document.documentElement.setAttribute("data-theme", resolvedTheme("system"));
    });
  } catch (e) { /* older webview: no live OS-change updates */ }
}

function focusSearch() {
  if (state.view === "settings") return;
  const s = $("search");
  s.focus();
  s.select();
}

function clearSearch() {
  const s = $("search");
  if (!s.value && !state.filters.search) return;
  s.value = "";
  state.filters.search = "";
  render();
}

function clearBulkSelection() {
  if (state.view === "voice") {
    voiceSelectedSet().clear();
    renderVoice();
  } else {
    state.selected.clear();
    render();
  }
}

function isTypingTarget(target) {
  if (!target) return false;
  const tag = target.tagName;
  return target.isContentEditable || tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
}

function clampDocumentScroll() {
  const doc = document.scrollingElement || document.documentElement;
  if (doc && doc.scrollTop) doc.scrollTop = 0;
}

async function runArchivedAudioAutoCleanup() {
  try {
    let d = settingsData();
    if (!state.settings.loaded) {
      d = await engine(["settings-get"]);
      setSettingsState(d);
    }
    const retention = d.archived_retention || {};
    const days = positiveWholeDays(retention.days);
    if (!retention.auto || !days) return;
    const res = await engine(["voice-cleanup-archived", "--days", String(days), "--apply"]);
    const deleted = Number(res.deleted_count || 0);
    const freed = Number(res.freed_bytes || 0);
    if (deleted > 0 || freed > 0) {
      toast(res.message || "Archived audio cleanup finished.");
      try {
        setSettingsState(await engine(["settings-get"]));
        if (state.view === "settings") renderSettings();
      } catch (e) {
        // The cleanup already succeeded; failing to refresh Settings stats is non-fatal.
      }
    }
  } catch (e) {
    // Startup cleanup is optional; never let it break first paint or navigation.
  }
}

function scheduleArchivedAudioAutoCleanup() {
  requestAnimationFrame(() => setTimeout(runArchivedAudioAutoCleanup, 0));
}

window.addEventListener("DOMContentLoaded", () => {
  // Top-nav focus can scroll the document and unpin the footer; app scrolling lives in `.scroll`.
  window.addEventListener("scroll", clampDocumentScroll, { capture: true, passive: true });
  clampDocumentScroll();
  document.querySelectorAll("[data-icon]").forEach((e) => setIcon(e, e.dataset.icon));
  initTheme();
  $("view-nav").addEventListener("click", (e) => {
    const b = e.target.closest(".seg-btn"); if (!b) return;
    showView(b.dataset.view);
  });
  $("refresh-btn").addEventListener("click", () => {
    if (state.view === "voice") loadVoice(true);
    else if (state.view === "settings") loadSettings(true);
    else load(true);
  });
  $("voice-import-btn").addEventListener("click", importVoiceMemos);
  $("voice-notes-folder-btn").addEventListener("click", () => setNotesFolder(true));
  $("voice-archive-toggle").addEventListener("click", (e) => {
    const b = e.target.closest(".seg-btn"); if (!b) return;
    state.voice.showArchived = b.dataset.archived === "1";
    voiceSelectedSet().clear();
    renderVoice();
  });
  $("new-workout-btn").addEventListener("click", openAuthor);
  $("search").addEventListener("input", (e) => { state.filters.search = e.target.value; render(); });
  $("search").addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      e.preventDefault();
      if (state.filters.search) clearSearch();
      else $("search").blur();
    }
  });
  document.addEventListener("keydown", (e) => {
    const key = e.key.toLowerCase();
    if ((e.metaKey || e.ctrlKey) && key === "f") {
      e.preventDefault();
      focusSearch();
      return;
    }
    if (e.key === "/" && !e.metaKey && !e.ctrlKey && !e.altKey && !isTypingTarget(e.target)) {
      e.preventDefault();
      focusSearch();
    }
  });
  $("filter-loc").addEventListener("click", (e) => {
    const b = e.target.closest(".seg-btn"); if (!b) return;
    state.filters.loc = b.dataset.loc; segActive("filter-loc", b); render();
  });
  $("filter-kind").addEventListener("click", (e) => {
    const b = e.target.closest(".seg-btn"); if (!b) return;
    state.filters.kind = b.dataset.kind; segActive("filter-kind", b); render();
  });
  $("filter-sport").addEventListener("change", (e) => { state.filters.sport = e.target.value; render(); });
  $("filter-tag").addEventListener("change", (e) => { state.filters.tag = e.target.value; render(); });
  $("filter-folder").addEventListener("change", (e) => { state.filters.folder = e.target.value; render(); });
  $("wind-when").addEventListener("change", (e) => setWindSlot(e.target.value));
  // route controls: sort + area filter + add route
  $("route-sort").addEventListener("change", (e) => {
    state.sort = e.target.value;
    if (state.sort === "nearest" && !state.anchor) openAnchorPop();  // needs an anchor to mean anything
    render();
  });
  $("anchor-btn").addEventListener("click", (e) => { e.stopPropagation(); toggleAnchorPop(); });
  document.addEventListener("click", (e) => {   // click outside the picker closes it
    if (_anchorPopOpen && !e.target.closest(".anchor-wrap")) closeAnchorPop(true);
    if (!e.target.closest(".voice-more")) closeVoiceMenus();
  });
  $("add-route-btn").addEventListener("click", () => $("route-file").click());
  $("route-file").addEventListener("change", onRoutePicked);
  $("curate-watch-btn").addEventListener("click", openCurateWatchChooser);
  $("bulk-add-watch").addEventListener("click", () => state.view === "voice" ? bulkTranscribeVoice() : bulkAddToWatch());
  $("bulk-remove").addEventListener("click", () => state.view === "voice" ? bulkArchiveVoice() : bulkRemove());
  $("bulk-voice-unarchive").addEventListener("click", bulkUnarchiveVoice);
  $("bulk-delete-connect").addEventListener("click", () => state.view === "voice" ? bulkDeleteVoice() : bulkDeleteConnect());
  $("bulk-select-visible").addEventListener("click", toggleVisibleSelection);
  $("bulk-clear").addEventListener("click", clearBulkSelection);
  $("stale-review").addEventListener("click", reviewStale);
  $("stale-dismiss").addEventListener("click", () => { state.staleDismissed = true; renderStaleBanner(); });
  $("modal-cancel").addEventListener("click", closeModal);
  $("modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeModal(); });
  window.addEventListener("scroll", hidePreview, true);  // don't leave a stale preview mid-scroll
  showView("content");
  scheduleArchivedAudioAutoCleanup();
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
  renderDateBtn();
  const dpop = $("author-date-pop"); if (dpop) dpop.hidden = true;
  renderImageChip();
  renderForm();
  clearPreview();
  loadAuthorSettings();
  switchTab("describe");
  $("author-overlay").hidden = false;
  // replay the entrance each open (re-adding a class only animates after a reflow)
  const card = $("author-overlay").firstElementChild;
  if (card) { card.classList.remove("is-in"); void card.offsetWidth; card.classList.add("is-in"); }
}
function todayStr() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
function closeAuthor() { $("author-overlay").hidden = true; }

// ---- schedule date: a compact custom calendar (native picker is dated + light-in-dark) --------
// A hidden #author-date input keeps holding the YYYY-MM-DD value the engine reads; the button +
// popover just drive it, so the push path is unchanged.
const DP_MONTHS = ["January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December"];
const DP_WEEKDAYS = ["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"];
let _dpMonth = null; // {y, m} currently shown in the popover

function scheduleControl() {
  const hidden = el("input", { type: "hidden", id: "author-date" });
  const btn = el("button", { id: "author-date-btn", class: "author-date-btn", type: "button",
    onclick: toggleDatePop }, "Today");
  const pop = el("div", { id: "author-date-pop", class: "date-pop", hidden: true });
  return el("div", { class: "author-date-wrap",
      title: "Scheduling it makes it sync to your watch automatically, no manual Send to device" }, [
    el("span", { class: "author-date-label", text: "Schedule for:" }),
    el("div", { class: "author-date-field" }, [btn, hidden, pop]),
  ]);
}
function dpParse(s) { const p = (s || "").split("-").map(Number); return (p[0] && p[1] && p[2]) ? { y: p[0], m: p[1] - 1, d: p[2] } : null; }
function dpISO(y, m, d) { return `${y}-${String(m + 1).padStart(2, "0")}-${String(d).padStart(2, "0")}`; }
function dpHuman(s) {
  const p = dpParse(s); if (!p) return "Pick a date";
  const t = dpParse(todayStr());
  if (p.y === t.y && p.m === t.m && p.d === t.d) return "Today";
  const tm = new Date(t.y, t.m, t.d + 1);
  if (p.y === tm.getFullYear() && p.m === tm.getMonth() && p.d === tm.getDate()) return "Tomorrow";
  return new Date(p.y, p.m, p.d).toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
}
function renderDateBtn() { const b = $("author-date-btn"); if (b) b.textContent = dpHuman($("author-date").value); }
function toggleDatePop(e) {
  if (e) e.stopPropagation();
  const pop = $("author-date-pop");
  if (pop.hidden) {
    const c = dpParse($("author-date").value) || dpParse(todayStr());
    _dpMonth = { y: c.y, m: c.m }; renderDatePop(); pop.hidden = false;
  } else pop.hidden = true;
}
function shiftMonth(delta) {
  let { y, m } = _dpMonth; m += delta;
  if (m < 0) { m = 11; y--; } if (m > 11) { m = 0; y++; }
  _dpMonth = { y, m }; renderDatePop();
}
function pickDate(iso) { $("author-date").value = iso; renderDateBtn(); $("author-date-pop").hidden = true; }
function renderDatePop() {
  const pop = $("author-date-pop"); if (!pop) return;
  const { y, m } = _dpMonth, today = dpParse(todayStr()), sel = dpParse($("author-date").value);
  const first = new Date(y, m, 1).getDay(), days = new Date(y, m + 1, 0).getDate();
  const head = el("div", { class: "date-pop-head" }, [
    el("button", { class: "date-nav", type: "button", title: "Previous month",
      onclick: (e) => { e.stopPropagation(); shiftMonth(-1); } }, "‹"),
    el("div", { class: "date-pop-title", text: `${DP_MONTHS[m]} ${y}` }),
    el("button", { class: "date-nav", type: "button", title: "Next month",
      onclick: (e) => { e.stopPropagation(); shiftMonth(1); } }, "›"),
  ]);
  const week = el("div", { class: "date-pop-week" }, DP_WEEKDAYS.map((d) => el("span", { text: d })));
  const grid = el("div", { class: "date-pop-grid" });
  for (let i = 0; i < first; i++) grid.append(el("span", { class: "date-cell is-blank" }));
  for (let d = 1; d <= days; d++) {
    const iso = dpISO(y, m, d);
    const isToday = today.y === y && today.m === m && today.d === d;
    const isSel = sel && sel.y === y && sel.m === m && sel.d === d;
    const past = new Date(y, m, d) < new Date(today.y, today.m, today.d);
    const cls = "date-cell" + (isToday ? " is-today" : "") + (isSel ? " is-selected" : "") + (past ? " is-past" : "");
    grid.append(el("button", { class: cls, type: "button", disabled: past,
      onclick: (e) => { e.stopPropagation(); pickDate(iso); } }, String(d)));
  }
  pop.replaceChildren(head, week, grid);
}

function switchTab(name) {
  AUTHOR.tab = name;
  $("author-describe").hidden = name !== "describe";
  $("author-build").hidden = name !== "build";
  for (const b of $("author-tabs").querySelectorAll(".seg-btn"))
    b.classList.toggle("is-active", b.dataset.tab === name);
  // when switching to Build, preview the current form
  if (name === "build") syncAndPreview();
  else clearPreview();
  // Reset the status line so a stale "Ready — review and push" from a previous build never
  // greets a fresh, unbuilt pane. A ready/review status is set only after a build succeeds.
  $("author-status").textContent = name === "describe" ? "Describe a workout to get started." : "";
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
      scheduleControl(),
      el("button", { id: "author-push", class: "btn btn-primary", disabled: true,
        onclick: pushSpec }, "Push to watch"),
    ]),
  ]);

  const card = el("div", { class: "author-card" }, [head, body, foot]);
  const overlay = el("div", { id: "author-overlay", class: "author-overlay", hidden: true,
    onclick: (e) => { if (e.target.id === "author-overlay") closeAuthor(); } }, [card]);
  document.body.appendChild(overlay);
  // Close the date popover on any click outside its field.
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".author-date-field")) {
      const p = $("author-date-pop"); if (p && !p.hidden) p.hidden = true;
    }
  });
  AUTHOR.built = true;
}

function buildDescribePane() {
  // A quiet one-line intro in the home page's serif voice. No filled callout.
  const intro = el("div", { class: "author-intro" }, [
    el("p", { class: "author-lede",
      text: "Describe a workout, or snap a photo of a plan. It gets built on your watch." }),
  ]);

  const ta = el("textarea", { id: "author-text", class: "author-text", rows: "5", oninput: updateLLMBtn,
    placeholder: "e.g. 10 min easy, then 4x(3 min hard / 2 min jog), 10 min easy. Or attach a photo of a training plan." });

  const fileInput = el("input", { type: "file", accept: "image/*", id: "author-file",
    style: "display:none", onchange: onImagePicked });
  const imageRow = el("div", { class: "author-image-row" }, [
    el("button", { class: "btn btn-ghost", onclick: () => $("author-file").click() },
      [icon("photo"), el("span", { text: "Attach a photo" })]),
    el("span", { id: "author-image-chip", class: "author-chip", hidden: true }),
    fileInput,
  ]);

  // The connect-your-AI step: the one-click ChatGPT sign-in is the recommended primary,
  // the API-key path (any provider) is a quiet reveal below it.
  const connect = el("div", { class: "author-connect" }, [buildOauthBlock(), buildSettingsBlock()]);

  const buildBtn = el("button", { id: "author-llm-btn", class: "btn btn-primary author-build-btn",
    disabled: true, onclick: buildFromLLM }, "Build workout");

  return el("div", { id: "author-describe", class: "author-pane" },
    [intro, ta, imageRow, connect, buildBtn]);
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
    placeholder: "Paste an API key" });
  const save = el("button", { class: "btn btn-ghost", onclick: saveAuthorSettings }, "Save");
  const status = el("span", { id: "author-key-status", class: "author-status" });

  const details = el("details", { class: "author-settings" }, [
    el("summary", {}, "Use an API key instead"),
    el("div", { class: "author-settings-grid" }, [
      el("label", {}, "Provider"), provider,
      el("label", {}, "Model"), modelField,
      el("label", {}, "API key"), key,
      el("div", {}), el("div", { class: "author-settings-actions" }, [save, status]),
    ]),
  ]);
  return details;
}

// The recommended one-click path: "Sign in with ChatGPT" uses the user's own ChatGPT account.
// It is the primary connect option (always shown); the API-key reveal below is the alternative.
function buildOauthBlock() {
  const signIn = el("button", { id: "author-oauth-signin", class: "btn btn-primary",
    onclick: oauthSignIn }, "Sign in with ChatGPT");
  const signOut = el("button", { id: "author-oauth-signout", class: "btn btn-ghost",
    onclick: oauthSignOut, hidden: true }, "Sign out");
  const status = el("span", { id: "author-oauth-status", class: "author-status" });
  const note = el("p", { id: "author-oauth-note", class: "author-note",
    text: "Uses your own ChatGPT account. This sign-in is unofficial and could stop working if OpenAI changes it." });
  // Order [signIn, status, signOut]: not-connected reads "Sign in with ChatGPT | No API key needed";
  // connected hides signIn so it reads "Signed in to ChatGPT ✓ | Sign out".
  return el("div", { id: "author-oauth", class: "author-oauth" }, [
    el("div", { class: "author-oauth-row" }, [signIn, status, signOut]),
    note,
  ]);
}

function buildFormPane() {
  // A serif lede for parity with the Describe pane, so this tab isn't a bare form.
  const intro = el("div", { class: "author-intro" }, [
    el("p", { class: "author-lede", text: "Build a workout step by step, then push it to your watch." }),
  ]);
  const name = el("input", { id: "author-name", class: "author-input", placeholder: "Workout name",
    oninput: (e) => { AUTHOR.form.name = e.target.value; syncAndPreview(); } });
  const sport = el("select", { id: "author-sport", class: "sport-select",
    onchange: (e) => { AUTHOR.form.sport = e.target.value; syncAndPreview(); } },
    ["strength", "running", "cycling", "swimming"].map((s) =>
      el("option", { value: s }, s[0].toUpperCase() + s.slice(1))));

  const steps = el("div", { id: "author-steps", class: "author-steps" });
  const addMenu = el("div", { class: "author-addrow" },
    [el("span", { class: "author-addlabel", text: "Add step:" })].concat(
      STEP_TYPES.map((t) => el("button", { class: "chip-btn",
        onclick: () => { AUTHOR.form.steps.push(stepDefaults(t.k)); renderForm(); syncAndPreview(); } },
        t.label))));

  return el("div", { id: "author-build", class: "author-pane", hidden: true }, [
    intro,
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

// colour the form step cards by type, matching the preview cards' left-border language
const STEP_COLOR = { warmup: "wo-warmup", interval: "wo-work", rest: "wo-recover",
  recovery: "wo-recover", strength: "wo-strength", cooldown: "wo-cooldown" };
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
  return el("div", { class: "author-step " + (STEP_COLOR[step.kind] || "") }, [head, body]);
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
  updateLLMBtn();
}

// "Build workout" is only actionable once there is something to build from.
function updateLLMBtn() {
  const b = $("author-llm-btn");
  if (b) b.disabled = !($("author-text").value.trim() || AUTHOR.image);
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
    $("author-status").textContent = AUTHOR.valid ? "Ready to review and push." : "The model's spec has errors.";
  } catch (e) {
    toast(e.message, true); $("author-status").textContent = "";
  } finally { updateLLMBtn(); }
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

// The ChatGPT sign-in is the recommended primary path, so it is always shown (not provider-gated).
// Reflect connected/plan state; otherwise nudge that it needs no key.
function applyOauthUI(s) {
  const box = $("author-oauth");
  if (!box) return;
  box.hidden = false;
  const connected = !!s.openai_oauth_connected;
  $("author-oauth-signin").hidden = connected;
  $("author-oauth-signout").hidden = !connected;
  // The unofficial-path caveat informs the CHOICE; once connected it's just clutter, so hide it.
  const note = $("author-oauth-note");
  if (note) note.hidden = connected;
  const st = $("author-oauth-status");
  // Blue = connected, echoing the app's Klein-blue "live" convention (watch pill, route data).
  st.className = "author-status" + (connected ? " is-connected" : "");
  if (connected) {
    const plan = s.openai_oauth_plan ? ` (${s.openai_oauth_plan})` : "";
    st.textContent = `Signed in to ChatGPT${plan} ✓`;
  } else {
    st.textContent = "No API key needed";
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
  st.textContent = "Opening your browser, finish sign-in there…";
  try {
    // This call BLOCKS while the browser login runs (up to ~3 min), then returns fresh settings.
    const s = await engine(["workout-oauth-login"]);
    if (s.openai_oauth_connected) {
      // Signing in with ChatGPT makes OpenAI (subscription) the active provider, so the build
      // uses it regardless of what the advanced provider dropdown was set to.
      const s2 = await engine(["workout-settings-set", "--provider", "openai", "--openai-auth", "oauth"]);
      AUTHOR.settings = s2;
      $("author-provider").value = "openai";
      applyOauthUI(s2);
      updateKeyStatus(s2, "openai");
      renderModelOptionsFromSettings("openai", s2);
      refreshAuthorModelOptions("openai", s2);
      toast("Signed in with ChatGPT.");
    } else {
      AUTHOR.settings = s;
      applyOauthUI(s);
      toast("Signed in with ChatGPT.");
    }
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
