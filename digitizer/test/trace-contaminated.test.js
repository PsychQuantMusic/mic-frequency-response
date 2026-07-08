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

test('giant vertical run is rejected even when its center is near the curve (HIGH fix)', () => {
  // 曲線走在圖中央高度（-20dB → y=250 = 垂直網格線 run 的中心附近）：
  // 巨型 run 的中心與 prevY 距離 ≈ 0，僅靠 maxJump 過濾會被接受 → 必須被 maxRunSize 明確拒絕
  const t = makeTransform(calib);
  const dark = new Set();
  const gridX = [300, 500, 700, 900];
  for (const x of gridX) for (let y = 30; y < 470; y++) dark.add(`${x},${y}`);
  const yc = Math.round(t.toPixel(1000, -20).py); // y=250，貼近垂直 run 中心
  for (let x = 100; x <= 1100; x++) for (let dy = -1; dy <= 1; dy++) dark.add(`${x},${yc + dy}`);
  const pixelAt = (x, y) => (dark.has(`${x},${y}`) ? BLACK : WHITE);

  const pts = traceCurve({
    pixelAt, width: 1200, height: 500,
    xStart: 100, xEnd: 1100, calib, targetColor: BLACK, tolerance: 60,
    seed: { px: 600, py: yc },
  });
  // 垂直線欄（曲線與線融合成巨型 run）誠實跳過；其餘全數還原且零污染
  for (const p of pts) {
    assert.ok(Math.abs(p.level_db - -20) < 0.3, `不得被巨型 run 拉偏 (got ${p.level_db})`);
  }
  const gridFreqs = gridX.map((x) => t.toData(x, 0).freq_hz);
  for (const gf of gridFreqs) {
    assert.ok(!pts.some((p) => Math.abs(Math.log10(p.freq_hz / gf)) < 1e-6), `網格欄應為 gap (${gf.toFixed(0)}Hz)`);
  }
  assert.ok(pts.length >= 990, `其餘欄應還原 (got ${pts.length})`);
});

test('seed on a vertical gridline column returns empty (honest refusal)', () => {
  const img = makeContaminatedImage({ width: 1200, height: 500, curveFn });
  const t = makeTransform(calib);
  const gridPx = Math.round(t.toPixel(1000, 0).px); // 1000Hz 垂直網格線欄
  const pts = traceCurve({
    pixelAt: img.pixelAt, width: 1200, height: 500,
    xStart: 100, xEnd: 1100, calib, targetColor: BLACK, tolerance: 60,
    seed: { px: gridPx, py: 250 }, // 點在垂直線上（該欄只有巨型 run）
  });
  assert.equal(pts.length, 0, '種子欄只有巨型 run → 誠實回空');
});

test('seed far from any run returns empty (off-curve pick refused)', () => {
  const img = makeContaminatedImage({ width: 1200, height: 500, curveFn });
  const pts = traceCurve({
    pixelAt: img.pixelAt, width: 1200, height: 500,
    xStart: 100, xEnd: 1100, calib, targetColor: BLACK, tolerance: 60,
    seed: { px: 600, py: 480 }, // 空白處（離曲線與網格線都遠 > maxJump）
  });
  assert.equal(pts.length, 0, '種子不在任何合格 run 附近 → 誠實回空');
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
