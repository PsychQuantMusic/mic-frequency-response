// #13 彩色曲線：顏色是 Level-0 可分特徵——targetColor 天然排除異色網格。
// #10 agent 實戰心得（e935 藍 / C414·NT1 紅曲線）的測試鎖。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { traceCurve } from '../trace.js';
import { makeTransform } from '../coords.js';

const calib = {
  freq: { p1: { px: 100, value: 20 }, p2: { px: 1100, value: 20000 } },
  db: { p1: { py: 50, value: 0 }, p2: { py: 450, value: -40 } },
};
const BLUE = [0, 60, 200, 255];
const BLACK = [0, 0, 0, 255];
const WHITE = [255, 255, 255, 255];
const t = makeTransform(calib);
const curveFn = (f) => -6 * Math.pow(Math.log10(f / 1000), 2);

// 藍曲線 + 黑網格（水平每 10dB + 垂直）——真實彩圖情境
function makeColorImage() {
  const blue = new Set();
  const black = new Set();
  for (const db of [10, 0, -10, -20, -30]) {
    const y = Math.round(t.toPixel(1000, db).py);
    for (let x = 100; x <= 1100; x++) black.add(`${x},${y}`);
  }
  for (const f of [50, 100, 1000, 10000]) {
    const x = Math.round(t.toPixel(f, 0).px);
    for (let y = 30; y < 470; y++) black.add(`${x},${y}`);
  }
  for (let x = 100; x <= 1100; x++) {
    const { freq_hz } = t.toData(x, 0);
    const yc = Math.round(t.toPixel(freq_hz, curveFn(freq_hz)).py);
    for (let dy = -1; dy <= 1; dy++) blue.add(`${x},${yc + dy}`);
  }
  return {
    width: 1200, height: 500,
    pixelAt: (x, y) => (blue.has(`${x},${y}`) ? BLUE : black.has(`${x},${y}`) ? BLACK : WHITE),
  };
}

function maxError(pts) {
  let m = 0;
  for (const p of pts) m = Math.max(m, Math.abs(p.level_db - curveFn(p.freq_hz)));
  return m;
}

for (const strategy of ['greedy', 'viterbi']) {
  test(`colored curve on black grid: targetColor excludes grid natively (${strategy})`, () => {
    const img = makeColorImage();
    const seed = {
      px: Math.round(t.toPixel(500, curveFn(500)).px),
      py: Math.round(t.toPixel(500, curveFn(500)).py),
    };
    const pts = traceCurve({
      pixelAt: img.pixelAt, width: 1200, height: 500,
      xStart: 100, xEnd: 1100, calib, targetColor: BLUE, tolerance: 60,
      seed, strategy,
    });
    // 黑網格 colorDistance ≈ 208 >> 60 → 完全不成候選；連垂直網格線欄都不是 gap
    assert.ok(pts.length >= 995, `彩色曲線應近乎全還原（got ${pts.length}）`);
    assert.ok(maxError(pts) < 0.5, `零網格污染（got ${maxError(pts).toFixed(2)} dB）`);
  });
}
