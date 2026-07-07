import { test } from 'node:test';
import assert from 'node:assert/strict';
import { traceCurve, colorDistance } from '../trace.js';
import { makeTransform } from '../coords.js';

const calib = {
  freq: { p1: { px: 100, value: 20 }, p2: { px: 1100, value: 20000 } },
  db: { p1: { py: 50, value: 0 }, p2: { py: 450, value: -40 } },
};

const RED = [255, 0, 0, 255];
const WHITE = [255, 255, 255, 255];

// 生成一張「答案已知」的合成頻響圖：用已知 curveFn(freq) 反算每個 px 該塗色的 py。
function makeSyntheticImage({ width, height, calib, curveFn, color, lineWidth }) {
  const t = makeTransform(calib);
  const half = Math.floor(lineWidth / 2);
  const curvePixels = new Set();
  for (let x = 0; x < width; x++) {
    const { freq_hz } = t.toData(x, 0); // freq 只依 px
    const level = curveFn(freq_hz);
    const yc = Math.round(t.toPixel(freq_hz, level).py);
    for (let dy = -half; dy <= half; dy++) {
      const y = yc + dy;
      if (y >= 0 && y < height) curvePixels.add(`${x},${y}`);
    }
  }
  return {
    width,
    height,
    pixelAt: (x, y) => (curvePixels.has(`${x},${y}`) ? color : WHITE),
  };
}

test('colorDistance is euclidean over RGB', () => {
  assert.equal(colorDistance([0, 0, 0, 255], [0, 0, 0, 255]), 0);
  assert.ok(Math.abs(colorDistance(RED, [0, 0, 0, 255]) - 255) < 1e-9);
});

test('traceCurve takes the centroid of matching pixels in a column', () => {
  // x=500 的 y=100..104 為目標色 → 質心 102
  const pixelAt = (x, y) => (x === 500 && y >= 100 && y <= 104 ? RED : WHITE);
  const pts = traceCurve({
    pixelAt, width: 1200, height: 500,
    xStart: 500, xEnd: 500, calib, targetColor: RED, tolerance: 10,
  });
  assert.equal(pts.length, 1);
  const expected = makeTransform(calib).toData(500, 102).level_db;
  assert.ok(Math.abs(pts[0].level_db - expected) < 1e-9);
});

test('traceCurve skips columns with no matching pixel (honest gap, no output)', () => {
  const pixelAt = (x, y) => ((x === 300 || x === 305) && y === 200 ? RED : WHITE);
  const pts = traceCurve({
    pixelAt, width: 1200, height: 500,
    xStart: 200, xEnd: 400, calib, targetColor: RED, tolerance: 10,
  });
  assert.equal(pts.length, 2); // 只有 2 個非空欄，其餘跳過
});

test('regression: recovers a known parabolic curve within tolerance', () => {
  const width = 1200, height = 500;
  const curveFn = (f) => -6 * Math.pow(Math.log10(f / 1000), 2); // 0dB@1kHz, 拋物線, 全落在 [0,-18]
  const img = makeSyntheticImage({ width, height, calib, curveFn, color: RED, lineWidth: 3 });

  const pts = traceCurve({
    pixelAt: img.pixelAt, width, height,
    xStart: 100, xEnd: 1100, calib, targetColor: RED, tolerance: 60,
  });

  // 每欄都有曲線 → 應幾乎全數還原
  assert.ok(pts.length >= 995, `only recovered ${pts.length} points`);

  // 頻率遞增
  for (let i = 1; i < pts.length; i++) {
    assert.ok(pts[i].freq_hz > pts[i - 1].freq_hz, 'freq must be strictly increasing');
  }

  // 端點頻率接近校準錨點
  assert.ok(Math.abs(pts[0].freq_hz - 20) / 20 < 0.02, `first freq ${pts[0].freq_hz}`);
  assert.ok(Math.abs(pts[pts.length - 1].freq_hz - 20000) / 20000 < 0.02, `last freq ${pts.at(-1).freq_hz}`);

  // level 還原誤差 < 0.5dB（線寬 3 + round 的量化誤差）
  let maxErr = 0;
  for (const p of pts) {
    maxErr = Math.max(maxErr, Math.abs(p.level_db - curveFn(p.freq_hz)));
  }
  assert.ok(maxErr < 0.5, `max level error ${maxErr.toFixed(3)} dB exceeds tolerance`);
});
