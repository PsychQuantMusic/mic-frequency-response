import { test } from 'node:test';
import assert from 'node:assert/strict';
import { makePolarTransform } from '../polar-coords.js';

// 校準：圓心 (500,400)、0dB 外圈半徑 300px、-20dB 圈半徑 60px（線性 60px/5dB）
// 0°（on-axis）在畫面正下方（SM58 圖慣例：180° 在上、0 在下）、順時針
const calib = {
  center: { px: 500, py: 400 },
  rings: [{ r: 300, db: 0 }, { r: 60, db: -20 }],
  zeroAngleScreenDeg: 180, // 畫面方向：0=上、90=右、180=下、270=左
  clockwise: true,
};

test('known anchor points map correctly', () => {
  const t = makePolarTransform(calib);
  // 0°/0dB → 圓心正下方 300px
  const p = t.toPixel(0, 0);
  assert.ok(Math.abs(p.px - 500) < 1e-9 && Math.abs(p.py - 700) < 1e-9, `${p.px},${p.py}`);
  // 0°/-20dB → 正下方 60px
  const q = t.toPixel(0, -20);
  assert.ok(Math.abs(q.py - 460) < 1e-9, `${q.py}`);
  // 90°（順時針 → 畫面左方）/0dB → (200, 400)
  const r = t.toPixel(90, 0);
  assert.ok(Math.abs(r.px - 200) < 1e-6 && Math.abs(r.py - 400) < 1e-6, `${r.px},${r.py}`);
});

test('toData round-trips toPixel', () => {
  const t = makePolarTransform(calib);
  for (const [ang, db] of [[0, 0], [30, -3], [90, -6.5], [150, -15], [180, -10], [270, -5]]) {
    const p = t.toPixel(ang, db);
    const d = t.toData(p.px, p.py);
    assert.ok(Math.abs(d.angle_deg - ang) < 1e-6, `angle ${d.angle_deg} vs ${ang}`);
    assert.ok(Math.abs(d.level_db - db) < 1e-9, `db ${d.level_db} vs ${db}`);
  }
});

test('counter-clockwise chart convention works', () => {
  const t = makePolarTransform({ ...calib, clockwise: false });
  // ccw 90° → 畫面右方 (800, 400)
  const p = t.toPixel(90, 0);
  assert.ok(Math.abs(p.px - 800) < 1e-6, `${p.px}`);
});

test('degenerate calibration throws', () => {
  assert.throws(() => makePolarTransform({ ...calib, rings: [{ r: 300, db: 0 }, { r: 300, db: -20 }] }));
  assert.throws(() => makePolarTransform({ ...calib, rings: [{ r: 300, db: 0 }, { r: 60, db: 0 }] }));
  assert.throws(() => makePolarTransform({ ...calib, rings: [{ r: NaN, db: 0 }, { r: 60, db: -20 }] }));
  assert.throws(() => makePolarTransform({ ...calib, rings: [{ r: -10, db: 0 }, { r: 60, db: -20 }] }));
});

test('center point has undefined angle (NaN), not a platform-dependent value', () => {
  const t = makePolarTransform(calib);
  const d = t.toData(500, 400); // 正是圓心
  assert.ok(Number.isNaN(d.angle_deg), `angle 應為 NaN (got ${d.angle_deg})`);
  assert.ok(Number.isFinite(d.level_db), 'level_db 仍應有值（r=0 外插）');
});

test('angle wraps correctly at 0/360 boundary', () => {
  const t = makePolarTransform(calib);
  for (const ang of [359.5, 0.5, 720 % 360]) {
    const p = t.toPixel(ang, -5);
    const d = t.toData(p.px, p.py);
    const diff = Math.min(Math.abs(d.angle_deg - ang), 360 - Math.abs(d.angle_deg - ang));
    assert.ok(diff < 1e-6, `wrap 誤差 ${diff} @ ${ang}°`);
  }
});
