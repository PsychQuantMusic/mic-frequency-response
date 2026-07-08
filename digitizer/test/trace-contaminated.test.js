// #3 網格污染回歸測試：真實廠商圖 = 曲線與網格「同色」。
// 全欄質心必被網格交點污染；種子連續性追蹤必須不被污染。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { traceCurve } from '../trace.js';
import { makeTransform } from '../coords.js';

const calib = {
  freq: { p1: { px: 100, value: 20 }, p2: { px: 1100, value: 20000 } },
  db: { p1: { py: 50, value: 0 }, p2: { py: 450, value: -40 } },
};
const BLACK = [0, 0, 0, 255];
const WHITE = [255, 255, 255, 255];

// 合成「同色網格污染圖」：黑曲線 3px + 黑水平網格線（每 10dB）+ 黑垂直網格線
function makeContaminatedImage({ width, height, curveFn, lineWidth = 3 }) {
  const t = makeTransform(calib);
  const dark = new Set();
  // 水平網格線（同色！這就是污染源）
  for (const db of [10, 0, -10, -20, -30]) {
    const y = Math.round(t.toPixel(1000, db).py);
    for (let x = 100; x <= 1100; x++) dark.add(`${x},${y}`);
  }
  // 垂直網格線
  for (const f of [50, 100, 1000, 10000]) {
    const x = Math.round(t.toPixel(f, 0).px);
    for (let y = 30; y < height - 30; y++) dark.add(`${x},${y}`);
  }
  // 曲線（粗 lineWidth px）
  const half = Math.floor(lineWidth / 2);
  for (let x = 100; x <= 1100; x++) {
    const { freq_hz } = t.toData(x, 0);
    const yc = Math.round(t.toPixel(freq_hz, curveFn(freq_hz)).py);
    for (let dy = -half; dy <= half; dy++) dark.add(`${x},${yc + dy}`);
  }
  return { width, height, pixelAt: (x, y) => (dark.has(`${x},${y}`) ? BLACK : WHITE) };
}

const curveFn = (f) => -6 * Math.pow(Math.log10(f / 1000), 2); // 已知拋物線 0dB@1kHz

function maxError(pts) {
  let m = 0;
  for (const p of pts) m = Math.max(m, Math.abs(p.level_db - curveFn(p.freq_hz)));
  return m;
}

test('legacy full-column centroid IS polluted by same-color grid (documents the failure)', () => {
  const img = makeContaminatedImage({ width: 1200, height: 500, curveFn });
  const pts = traceCurve({
    pixelAt: img.pixelAt, width: 1200, height: 500,
    xStart: 100, xEnd: 1100, calib, targetColor: BLACK, tolerance: 60,
  });
  // 每欄都有水平網格線像素 → 質心被拉離曲線。這是 #3 的存在理由。
  assert.ok(maxError(pts) > 1.0, `legacy 應被污染 (got max err ${maxError(pts).toFixed(2)} dB)`);
});

test('seed continuity tracing recovers the curve despite same-color grid', () => {
  const img = makeContaminatedImage({ width: 1200, height: 500, curveFn });
  const t = makeTransform(calib);
  // 種子 = 使用者在曲線上點的取色點（此處取 500Hz 處曲線位置）
  const seed = {
    px: Math.round(t.toPixel(500, curveFn(500)).px),
    py: Math.round(t.toPixel(500, curveFn(500)).py),
  };
  const pts = traceCurve({
    pixelAt: img.pixelAt, width: 1200, height: 500,
    xStart: 100, xEnd: 1100, calib, targetColor: BLACK, tolerance: 60, seed,
  });
  // 垂直網格線欄（整欄暗 run 巨大、遠離前欄 y）應被誠實跳過 → 少數 gap 可接受
  assert.ok(pts.length >= 950, `應近乎全數還原 (got ${pts.length})`);
  assert.ok(maxError(pts) < 0.5, `種子追蹤不應被污染 (got max err ${maxError(pts).toFixed(2)} dB)`);
  for (let i = 1; i < pts.length; i++) {
    assert.ok(pts[i].freq_hz > pts[i - 1].freq_hz, 'freq 遞增');
  }
});

test('seed tracing on clean image matches legacy quality (no regression)', () => {
  // 無網格乾淨圖：種子模式品質不應輸給 legacy
  const t = makeTransform(calib);
  const dark = new Set();
  for (let x = 100; x <= 1100; x++) {
    const { freq_hz } = t.toData(x, 0);
    const yc = Math.round(t.toPixel(freq_hz, curveFn(freq_hz)).py);
    for (let dy = -1; dy <= 1; dy++) dark.add(`${x},${yc + dy}`);
  }
  const pixelAt = (x, y) => (dark.has(`${x},${y}`) ? BLACK : WHITE);
  const seed = {
    px: Math.round(t.toPixel(500, curveFn(500)).px),
    py: Math.round(t.toPixel(500, curveFn(500)).py),
  };
  const pts = traceCurve({
    pixelAt, width: 1200, height: 500,
    xStart: 100, xEnd: 1100, calib, targetColor: BLACK, tolerance: 60, seed,
  });
  assert.equal(pts.length, 1001);
  assert.ok(maxError(pts) < 0.5, `乾淨圖種子追蹤 (got ${maxError(pts).toFixed(2)} dB)`);
});
