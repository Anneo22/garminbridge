// GarminBridge health chart renderer — plain SVG string, zero dependencies.
// Matches the existing route-prof SVG style in main.js (kebab classes, no inline styles).
//
// The .health-chart-* classes emitted here are styled in styles.css, which is the
// app's single stylesheet. index.html links that one file and nothing else, so a
// separate chart.css beside this module would never load.

const SVGNS = "http://www.w3.org/2000/svg";

const MARGIN_LEFT = 56;
const MARGIN_RIGHT = 12;
const X_AXIS_HEIGHT = 32;
const ROW_TOP = 18;
const ROW_BOTTOM = 10;
const LABEL_Y = 15;
const Y_TICK_X = 44;
const Y_TICK_COUNT = 3;

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function finiteNum(n) {
  if (n === null || n === undefined) return null;
  const v = Number(n);
  return Number.isFinite(v) ? v : null;
}

function fmtNum(n) {
  const v = finiteNum(n);
  if (v === null) return "";
  // Keep enough precision for crisp paths, but not so much that output bloats.
  return v.toFixed(2);
}

function padDomain(min, max) {
  if (min === null || max === null) return [0, 1];
  // Never pad below zero for a series that never goes below zero. Stress and
  // body battery are 0-100 scores, and an axis reading -3.90 states that a
  // negative score exists.
  const floor = (lo) => (min >= 0 && lo < 0 ? 0 : lo);
  if (min === max) return [floor(min - 1), max + 1];
  const range = max - min;
  const pad = range * 0.05;
  return [floor(min - pad), max + pad];
}

function yDomain(values) {
  let min = null, max = null;
  for (const v of values) {
    const n = finiteNum(v);
    if (n === null) continue;
    if (min === null || n < min) min = n;
    if (max === null || n > max) max = n;
  }
  return padDomain(min, max);
}

function downsample(points, valueKey, plotWidth) {
  if (points.length <= plotWidth) return points;
  const tMin = points._tMin;
  const tMax = points._tMax;
  if (tMin === null || tMax === null || tMin === tMax) return points;

  // Split into null-delimited runs so gaps survive downsampling.
  const runs = [];
  let current = [];
  for (const p of points) {
    const t = finiteNum(p.t);
    const v = finiteNum(p[valueKey]);
    if (t === null || v === null) {
      if (current.length) {
        runs.push(current);
        current = [];
      }
      continue;
    }
    current.push(p);
  }
  if (current.length) runs.push(current);
  if (runs.length === 0) return [];

  // Allocate pixel columns to each run proportionally by time span.
  const totalSpan = tMax - tMin;
  const runSpans = runs.map((run) => {
    const t0 = finiteNum(run[0].t);
    const t1 = finiteNum(run[run.length - 1].t);
    return Math.max(0, t1 - t0);
  });
  const spanSum = runSpans.reduce((a, b) => a + b, 0);
  let widths;
  if (spanSum === 0) {
    widths = runs.map(() => 1);
  } else {
    widths = runSpans.map((span) => Math.max(1, Math.floor(plotWidth * span / spanSum)));
  }
  // Keep the column budget within plotWidth when possible.
  let totalWidth = widths.reduce((a, b) => a + b, 0);
  while (totalWidth > plotWidth) {
    const maxIdx = widths.indexOf(Math.max(...widths));
    if (widths[maxIdx] <= 1) break;
    widths[maxIdx]--;
    totalWidth--;
  }

  // Downsample each run independently and rejoin with a null gap marker.
  const out = [];
  for (let i = 0; i < runs.length; i++) {
    const run = runs[i];
    const runTMin = finiteNum(run[0].t);
    const runTMax = finiteNum(run[run.length - 1].t);
    const runWidth = widths[i];
    if (runTMin === null || runTMax === null || runWidth <= 0) continue;
    if (i > 0) out.push({ t: null, [valueKey]: null });
    const sampled = downsampleRun(run, valueKey, runWidth, runTMin, runTMax);
    for (const p of sampled) out.push(p);
  }
  return out;
}

function downsampleRun(points, valueKey, plotWidth, tMin, tMax) {
  if (points.length <= plotWidth) return points;
  const buckets = new Array(plotWidth);
  for (let i = 0; i < plotWidth; i++) buckets[i] = [];
  const span = tMax - tMin;
  if (span === 0) {
    buckets[0] = points.slice();
  } else {
    for (const p of points) {
      const t = finiteNum(p.t);
      const v = finiteNum(p[valueKey]);
      if (t === null || v === null) continue;
      const bx = Math.floor((t - tMin) / span * (plotWidth - 1));
      const b = Math.max(0, Math.min(plotWidth - 1, bx));
      buckets[b].push(p);
    }
  }
  const out = [];
  for (const b of buckets) {
    if (!b.length) continue;
    if (b.length === 1) {
      out.push(b[0]);
      continue;
    }
    let minPt = b[0], maxPt = b[0];
    for (const p of b) {
      if (p[valueKey] < minPt[valueKey]) minPt = p;
      if (p[valueKey] > maxPt[valueKey]) maxPt = p;
    }
    if (minPt === maxPt) {
      out.push(minPt);
    } else {
      // Preserve both extremes in chronological order; this keeps spikes while
      // bounding output to at most two points per pixel column.
      if (minPt.t <= maxPt.t) {
        out.push(minPt, maxPt);
      } else {
        out.push(maxPt, minPt);
      }
    }
  }
  return out;
}

function makeXScale(tMin, tMax, plotWidth) {
  const span = tMax - tMin;
  if (span === 0) {
    return (t) => MARGIN_LEFT + plotWidth / 2;
  }
  return (t) => MARGIN_LEFT + ((t - tMin) / span) * plotWidth;
}

function makeYScale(yMin, yMax, plotTop, plotBottom) {
  const span = yMax - yMin;
  if (span === 0) {
    const mid = (plotTop + plotBottom) / 2;
    return (v) => mid;
  }
  return (v) => plotBottom - ((v - yMin) / span) * (plotBottom - plotTop);
}

function pathSegments(points, xKey, yKey, xScale, yScale) {
  const segments = [];
  let current = [];
  for (const p of points) {
    const t = finiteNum(p[xKey]);
    const v = finiteNum(p[yKey]);
    if (t === null || v === null) {
      if (current.length) {
        segments.push(current);
        current = [];
      }
      continue;
    }
    current.push({ x: xScale(t), y: yScale(v) });
  }
  if (current.length) segments.push(current);
  return segments;
}

function dForSegments(segments) {
  if (!segments.length) return "";
  return segments
    .map((seg) => "M" + seg.map((p) => `${fmtNum(p.x)} ${fmtNum(p.y)}`).join("L"))
    .join("");
}

function areaPath(segments, yBase) {
  if (!segments.length) return "";
  const parts = [];
  for (const seg of segments) {
    const first = seg[0];
    const last = seg[seg.length - 1];
    const top = seg.map((p) => `${fmtNum(p.x)} ${fmtNum(p.y)}`).join("L");
    parts.push(`M${fmtNum(first.x)} ${fmtNum(yBase)}L${top}L${fmtNum(last.x)} ${fmtNum(yBase)}Z`);
  }
  return parts.join("");
}

function bandPath(segments, yBase) {
  if (!segments.length) return "";
  const parts = [];
  for (const seg of segments) {
    const top = seg.map((p) => `${fmtNum(p.x)} ${fmtNum(p.yTop)}`).join("L");
    const bottom = seg.map((p) => `${fmtNum(p.x)} ${fmtNum(p.yBottom)}`).join("L");
    const first = seg[0];
    const last = seg[seg.length - 1];
    parts.push(
      `M${fmtNum(first.x)} ${fmtNum(first.yBottom)}L${top}L${fmtNum(last.x)} ${fmtNum(last.yTop)}L${bottom}Z`
    );
  }
  return parts.join("");
}

function buildLineRow(row, xScale, yScale, plotBottom, plotWidth) {
  const points = row.points || [];
  points._tMin = row.tMin;
  points._tMax = row.tMax;
  const sampled = downsample(points, "v", plotWidth);
  const segments = pathSegments(sampled, "t", "v", xScale, yScale);
  const lineD = dForSegments(segments);
  const areaD = areaPath(segments, plotBottom);
  let paths = "";
  if (areaD) paths += `<path d="${areaD}" class="health-chart-area"/>`;
  if (lineD) paths += `<path d="${lineD}" class="health-chart-line"/>`;
  return paths;
}

function buildBandRow(row, xScale, yScale, plotWidth) {
  const bands = row.bands || [];
  const segments = [];
  let current = [];
  for (const b of bands) {
    const t = finiteNum(b.t);
    const min = finiteNum(b.min);
    const max = finiteNum(b.max);
    if (t === null || min === null || max === null) {
      if (current.length) {
        segments.push(current);
        current = [];
      }
      continue;
    }
    current.push({ x: xScale(t), yTop: yScale(max), yBottom: yScale(min) });
  }
  if (current.length) segments.push(current);

  const bandD = bandPath(segments);
  let paths = "";
  if (bandD) paths += `<path d="${bandD}" class="health-chart-band"/>`;

  // Avg line uses the same null-gap logic as a regular line.
  const avgPoints = (row.bands || []).map((b) => ({ t: b.t, v: b.avg }));
  avgPoints._tMin = row.tMin;
  avgPoints._tMax = row.tMax;
  const sampledAvg = downsample(avgPoints, "v", plotWidth);
  const avgSegments = pathSegments(sampledAvg, "t", "v", xScale, yScale);
  const avgD = dForSegments(avgSegments);
  if (avgD) paths += `<path d="${avgD}" class="health-chart-avg"/>`;
  return paths;
}

function yTicks(yMin, yMax, plotTop, plotBottom) {
  const ticks = [];
  for (let i = 0; i < Y_TICK_COUNT; i++) {
    const f = i / (Y_TICK_COUNT - 1);
    const v = yMax - f * (yMax - yMin);
    const y = plotTop + f * (plotBottom - plotTop);
    ticks.push({ v, y });
  }
  return ticks;
}

function formatClockLabel(ms, tzOffsetMinutes) {
  const d = new Date(ms + (tzOffsetMinutes || 0) * 60000);
  const h = String(d.getUTCHours()).padStart(2, "0");
  const m = String(d.getUTCMinutes()).padStart(2, "0");
  return `${h}:${m}`;
}

function formatDateLabel(ms, tzOffsetMinutes) {
  const d = new Date(ms + (tzOffsetMinutes || 0) * 60000);
  const mon = d.getUTCMonth() + 1;
  const day = d.getUTCDate();
  return `${mon}/${day}`;
}

function xAxisLabels(tMin, tMax, plotWidth, tzOffsetMinutes) {
  const span = tMax - tMin;
  const count = 5;
  const labels = [];
  for (let i = 0; i < count; i++) {
    const f = i / (count - 1);
    const t = tMin + f * span;
    const x = MARGIN_LEFT + f * plotWidth;
    const clock = formatClockLabel(t, tzOffsetMinutes);
    const date = span > 86400000 ? formatDateLabel(t, tzOffsetMinutes) : "";
    labels.push({ x, t, clock, date });
  }
  return labels;
}

export function buildChartSVG(spec) {
  if (!spec || typeof spec !== "object") throw new Error("buildChartSVG expects a spec object");
  const width = finiteNum(spec.width);
  const rowHeight = finiteNum(spec.rowHeight);
  const xDomain = spec.xDomain || [];
  const tMin = finiteNum(xDomain[0]);
  const tMax = finiteNum(xDomain[1]);
  const tzOffsetMinutes = finiteNum(spec.tzOffsetMinutes) || 0;
  const series = spec.series || [];

  if (width === null || rowHeight === null || tMin === null || tMax === null) {
    throw new Error("buildChartSVG: width, rowHeight, and xDomain are required");
  }

  const plotWidth = Math.max(1, width - MARGIN_LEFT - MARGIN_RIGHT);
  const plotBottom = rowHeight - ROW_BOTTOM;
  const xScale = makeXScale(tMin, tMax, plotWidth);

  const rows = series.map((s) => {
    const key = s.key || "series";
    const label = s.label || key;
    const unit = s.unit || "";
    const kind = s.kind || "line";
    let values = [];
    if (kind === "line") {
      values = (s.points || []).map((p) => finiteNum(p.v)).filter((v) => v !== null);
    } else {
      for (const b of s.bands || []) {
        const min = finiteNum(b.min);
        const max = finiteNum(b.max);
        const avg = finiteNum(b.avg);
        if (min !== null) values.push(min);
        if (max !== null) values.push(max);
        if (avg !== null) values.push(avg);
      }
    }
    const [yMin, yMax] = yDomain(values);
    const yScale = makeYScale(yMin, yMax, ROW_TOP, plotBottom);
    return {
      key,
      label,
      unit,
      kind,
      yMin,
      yMax,
      yScale,
      points: s.points || [],
      bands: s.bands || [],
      tMin,
      tMax,
    };
  });

  const totalHeight = rows.length * rowHeight + X_AXIS_HEIGHT;
  let svg = `<svg xmlns="${SVGNS}" viewBox="0 0 ${width} ${totalHeight}" width="${width}" height="${totalHeight}" class="health-chart" role="img" aria-label="Health chart">`;

  for (let i = 0; i < rows.length; i++) {
    const row = rows[i];
    const yOffset = i * rowHeight;
    svg += `<g class="health-chart-row" transform="translate(0, ${yOffset})">`;

    // Row label and unit
    svg += `<text x="${MARGIN_LEFT}" y="${LABEL_Y}" class="health-chart-label">${escapeHtml(row.label)}${row.unit ? ` (${escapeHtml(row.unit)})` : ""}</text>`;

    // Y-axis ticks
    const ticks = yTicks(row.yMin, row.yMax, ROW_TOP, plotBottom);
    for (const tick of ticks) {
      const y = tick.y;
      const v = tick.v;
      svg += `<line x1="${MARGIN_LEFT}" y1="${fmtNum(y)}" x2="${width - MARGIN_RIGHT}" y2="${fmtNum(y)}" class="health-chart-grid"/>`;
      svg += `<text x="${Y_TICK_X}" y="${fmtNum(y + 3)}" class="health-chart-y-label">${fmtNum(v)}</text>`;
    }

    if (row.kind === "line") {
      svg += buildLineRow(row, xScale, row.yScale, plotBottom, plotWidth);
    } else {
      svg += buildBandRow(row, xScale, row.yScale, plotWidth);
    }

    svg += `</g>`;
  }

  // Shared X-axis
  const axisY = totalHeight - X_AXIS_HEIGHT + 6;
  svg += `<g class="health-chart-x-axis">`;
  svg += `<line x1="${MARGIN_LEFT}" y1="${axisY}" x2="${width - MARGIN_RIGHT}" y2="${axisY}" class="health-chart-x-tick"/>`;
  const labels = xAxisLabels(tMin, tMax, plotWidth, tzOffsetMinutes);
  for (const lbl of labels) {
    const text = lbl.date ? `${escapeHtml(lbl.date)} ${escapeHtml(lbl.clock)}` : escapeHtml(lbl.clock);
    svg += `<text x="${fmtNum(lbl.x)}" y="${axisY + 16}" class="health-chart-x-label">${text}</text>`;
  }
  svg += `</g>`;

  svg += `</svg>`;
  return svg;
}

export function renderChart(container, spec) {
  if (!container) return;
  container.innerHTML = buildChartSVG(spec);
}
