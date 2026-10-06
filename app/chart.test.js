// Headless tests for app/src/chart.js — plain Node, no dependencies.
import { buildChartSVG } from "./src/chart.js";

let passed = 0;
let failed = 0;

function assert(cond, msg) {
  if (cond) {
    passed++;
  } else {
    failed++;
    console.error("FAIL:", msg);
  }
}

function makeSpec(series) {
  return {
    width: 400,
    rowHeight: 100,
    xDomain: [0, 10],
    tzOffsetMinutes: 0,
    series,
  };
}

function pathPairCount(d) {
  return (d.match(/-?\d+(?:\.\d+)? -?\d+(?:\.\d+)?/g) || []).length;
}

function segmentsIn(d) {
  return (d.match(/M/g) || []).length;
}

// 1. Normal line series produces a path with the expected number of coordinate pairs.
const normalPoints = [];
for (let i = 0; i <= 10; i++) normalPoints.push({ t: i, v: i * 10 });
const normalSpec = makeSpec([{ key: "hr", label: "Heart rate", unit: "bpm", kind: "line", points: normalPoints }]);
const normalSvg = buildChartSVG(normalSpec);
assert(normalSvg.includes("class=\"health-chart-line\""), "normal series should emit a line path");
assert(!normalSvg.includes("style="), "output must contain no inline style attributes");
assert(!normalSvg.includes("NaN"), "output must not contain NaN");
assert(!normalSvg.includes("undefined"), "output must not contain undefined");

// 2. Nulls break the line into multiple segments and no coordinate maps to null timestamps.
const gapPoints = [
  { t: 0, v: 10 }, { t: 1, v: 20 }, { t: 2, v: null }, { t: 3, v: null }, { t: 4, v: 30 }, { t: 5, v: 40 },
];
const gapSpec = makeSpec([{ key: "hr", label: "Heart rate", unit: "bpm", kind: "line", points: gapPoints }]);
const gapSvg = buildChartSVG(gapSpec);
const lineMatch = gapSvg.match(/<path[^>]*class=\"health-chart-line\"[^>]*d=\"([^\"]*)\"/) || gapSvg.match(/<path[^>]*d=\"([^\"]*)\"[^>]*class=\"health-chart-line\"[^>]*\/>/);
assert(lineMatch, "gap series should emit a line path");
const gapD = lineMatch[1];
assert(segmentsIn(gapD) > 1, "nulls must create more than one path segment");
assert(!gapD.includes("NaN"), "gap path must not contain NaN");
const nullTs = [2, 3];
for (const nt of nullTs) {
  // x for t=2 or 3 should not appear as a coordinate in the line path.
  const approxX = (56 + (nt / 10) * (400 - 56 - 12)).toFixed(2);
  assert(!gapD.includes(approxX), `line path should not contain x coordinate for null timestamp ${nt}`);
}

// 3. All-null series does not throw and emits no line path.
let allNullSvg = "";
try {
  allNullSvg = buildChartSVG(makeSpec([{ key: "resp", label: "Respiration", unit: "brpm", kind: "line", points: [{ t: 0, v: null }, { t: 1, v: null }] }]));
} catch (e) {
  assert(false, "all-null series threw: " + e.message);
}
assert(!allNullSvg.includes("class=\"health-chart-line\""), "all-null series must not emit a line path");
assert(!allNullSvg.includes("class=\"health-chart-area\""), "all-null series must not emit an area path");
assert(!allNullSvg.includes('d=""'), "no empty path d attributes");

// 4. Single-point series does not throw.
try {
  buildChartSVG(makeSpec([{ key: "spo2", label: "SpO2", unit: "%", kind: "line", points: [{ t: 5, v: 98 }] }]));
  assert(true, "single-point series did not throw");
} catch (e) {
  assert(false, "single-point series threw: " + e.message);
}

// 5. Zero-range series does not throw and produces finite coordinates.
let zeroRangeSvg = "";
try {
  zeroRangeSvg = buildChartSVG(makeSpec([{ key: "hr", label: "HR", unit: "bpm", kind: "line", points: [{ t: 0, v: 70 }, { t: 1, v: 70 }, { t: 2, v: 70 }] }]));
} catch (e) {
  assert(false, "zero-range series threw: " + e.message);
}
assert(!zeroRangeSvg.includes("NaN"), "zero-range output must not contain NaN");
assert(!zeroRangeSvg.includes("Infinity"), "zero-range output must not contain Infinity");
assert(zeroRangeSvg.includes("class=\"health-chart-line\""), "zero-range series should emit a line path");

// 6. Empty series does not throw.
try {
  buildChartSVG(makeSpec([]));
  assert(true, "empty series did not throw");
} catch (e) {
  assert(false, "empty series threw: " + e.message);
}

// 7. No style= anywhere.
const noStyleSpec = makeSpec([{ key: "hr", label: "HR", unit: "bpm", kind: "line", points: normalPoints }]);
const noStyleSvg = buildChartSVG(noStyleSpec);
assert(!noStyleSvg.includes("style="), "output must contain no style= substring");

// 8. Band series renders both a band area and an avg line.
const bandSpec = makeSpec([{
  key: "stress",
  label: "Stress",
  unit: "score",
  kind: "band",
  bands: [
    { t: 0, min: 10, max: 30, avg: 20 },
    { t: 1, min: 15, max: 35, avg: 25 },
    { t: 2, min: 12, max: 28, avg: 18 },
  ],
}]);
const bandSvg = buildChartSVG(bandSpec);
assert(bandSvg.includes("class=\"health-chart-band\""), "band series should emit a band area path");
assert(bandSvg.includes("class=\"health-chart-avg\""), "band series should emit an avg line path");

// 9. Downsampling: a dense series is bounded to a small multiple of the pixel width.
// Each run keeps one point per pixel column plus both min and max per column to preserve
// spikes. The cap is therefore 2*plotWidth plus two null gap markers per gap; this still
// keeps output far below the raw point count and is the fidelity-first threshold.
const densePlotWidth = 400 - 56 - 12;
const densePoints = [];
for (let i = 0; i < 5000; i++) densePoints.push({ t: i / 500, v: Math.sin(i / 100) * 50 + 100 });
const denseSpec = makeSpec([{ key: "hr", label: "HR", unit: "bpm", kind: "line", points: densePoints }]);
const denseSvg = buildChartSVG(denseSpec);
const denseMatch = denseSvg.match(/<path[^>]*class=\"health-chart-line\"[^>]*d=\"([^\"]*)\"/) || denseSvg.match(/<path[^>]*d=\"([^\"]*)\"[^>]*class=\"health-chart-line\"[^>]*\/>/);
assert(denseMatch, "dense series should emit a line path");
const denseD = denseMatch[1];
const pairCount = pathPairCount(denseD);
assert(pairCount > 0, "downsampled path should have coordinates");
assert(pairCount <= 2 * densePlotWidth, `downsampled path pair count ${pairCount} should be <= ${2 * densePlotWidth}`);

// 10. renderChart wrapper does not need to be tested headlessly (uses DOM).

// 11. Dense line series with a long null run still produces two line subpaths after downsampling.
const denseGapPoints = [];
for (let i = 0; i < 1000; i++) {
  const t = i / 100;
  if (t >= 4 && t <= 6) {
    denseGapPoints.push({ t, v: null });
  } else {
    denseGapPoints.push({ t, v: Math.sin(i / 20) * 50 + 100 });
  }
}
const denseGapSpec = makeSpec([{ key: "hr", label: "HR", unit: "bpm", kind: "line", points: denseGapPoints }]);
const denseGapSvg = buildChartSVG(denseGapSpec);
const denseGapMatch = denseGapSvg.match(/<path[^>]*class=\"health-chart-line\"[^>]*d=\"([^\"]*)\"/) || denseGapSvg.match(/<path[^>]*d=\"([^\"]*)\"[^>]*class=\"health-chart-line\"[^>]*\/>/);
assert(denseGapMatch, "dense gap series should emit a line path");
const denseGapD = denseGapMatch[1];
assert(segmentsIn(denseGapD) > 1, "dense line series null run must still create multiple subpaths after downsampling");
assert(!denseGapD.includes("NaN"), "dense gap line path must not contain NaN");

// 12. Dense band series with a long null avg run still produces two avg-line subpaths after downsampling.
const denseGapBands = [];
for (let i = 0; i < 1000; i++) {
  const t = i / 100;
  if (t >= 4 && t <= 6) {
    denseGapBands.push({ t, min: 10, max: 20, avg: null });
  } else {
    denseGapBands.push({ t, min: 10, max: 20, avg: Math.sin(i / 20) * 5 + 15 });
  }
}
const denseGapBandSpec = makeSpec([{ key: "stress", label: "Stress", unit: "score", kind: "band", bands: denseGapBands }]);
const denseGapBandSvg = buildChartSVG(denseGapBandSpec);
const denseAvgMatch = denseGapBandSvg.match(/<path[^>]*class=\"health-chart-avg\"[^>]*d=\"([^\"]*)\"/) || denseGapBandSvg.match(/<path[^>]*d=\"([^\"]*)\"[^>]*class=\"health-chart-avg\"[^>]*\/>/);
assert(denseAvgMatch, "dense gap band series should emit an avg line path");
const denseAvgD = denseAvgMatch[1];
assert(segmentsIn(denseAvgD) > 1, "dense band series null avg run must still create multiple avg-line subpaths after downsampling");
assert(!denseAvgD.includes("NaN"), "dense gap avg line path must not contain NaN");

// 13. Dense all-null line series does not emit a line path.
const denseAllNullPoints = [];
for (let i = 0; i < 1000; i++) denseAllNullPoints.push({ t: i / 100, v: null });
const denseAllNullSvg = buildChartSVG(makeSpec([{ key: "resp", label: "Respiration", unit: "brpm", kind: "line", points: denseAllNullPoints }]));
assert(!denseAllNullSvg.includes("class=\"health-chart-line\""), "dense all-null series must not emit a line path");
assert(!denseAllNullSvg.includes("class=\"health-chart-area\""), "dense all-null series must not emit an area path");
assert(!denseAllNullSvg.includes('d=""'), "dense all-null series must not contain empty path d attributes");

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);
