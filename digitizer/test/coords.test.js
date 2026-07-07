import { test } from 'node:test';
import assert from 'node:assert/strict';
import { makeTransform } from '../coords.js';

// 校準：20Hz@px100, 20000Hz@px1100（3 decades / 1000px）；0dB@py50, -40dB@py450（40dB / 400px）
const calib = {
  freq: { p1: { px: 100, value: 20 }, p2: { px: 1100, value: 20000 } },
  db: { p1: { py: 50, value: 0 }, p2: { py: 450, value: -40 } },
};

test('toData maps calibration anchor points back to known values', () => {
  const t = makeTransform(calib);
  const a = t.toData(100, 50);
  assert.ok(Math.abs(a.freq_hz - 20) < 1e-6, `freq ${a.freq_hz}`);
  assert.ok(Math.abs(a.level_db - 0) < 1e-9, `db ${a.level_db}`);
  const b = t.toData(1100, 450);
  assert.ok(Math.abs(b.freq_hz - 20000) < 1e-3, `freq ${b.freq_hz}`);
  assert.ok(Math.abs(b.level_db - -40) < 1e-9, `db ${b.level_db}`);
});

test('frequency axis is logarithmic (200Hz lands at expected px)', () => {
  const t = makeTransform(calib);
  // log10(200)=2.3010, log10(20)=1.3010; px = 100 + (2.3010-1.3010)/3*1000 = 433.33
  const r = t.toData(433.333, 50);
  assert.ok(Math.abs(r.freq_hz - 200) / 200 < 0.001, `got ${r.freq_hz}`);
});

test('dB axis is linear (py midpoint → -20dB)', () => {
  const t = makeTransform(calib);
  const r = t.toData(100, 250); // py halfway between 50 and 450
  assert.ok(Math.abs(r.level_db - -20) < 1e-9, `got ${r.level_db}`);
});

test('toPixel round-trips toData', () => {
  const t = makeTransform(calib);
  for (const [px, py] of [[300, 120], [800, 300], [1000, 400]]) {
    const d = t.toData(px, py);
    const p = t.toPixel(d.freq_hz, d.level_db);
    assert.ok(Math.abs(p.px - px) < 1e-6, `px ${p.px} vs ${px}`);
    assert.ok(Math.abs(p.py - py) < 1e-6, `py ${p.py} vs ${py}`);
  }
});

test('degenerate calibration (coincident points) throws', () => {
  assert.throws(() =>
    makeTransform({
      freq: { p1: { px: 100, value: 20 }, p2: { px: 100, value: 20000 } },
      db: { p1: { py: 50, value: 0 }, p2: { py: 450, value: -40 } },
    })
  );
});
