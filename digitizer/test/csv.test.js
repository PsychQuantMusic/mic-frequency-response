import { test } from 'node:test';
import assert from 'node:assert/strict';
import { toCSV } from '../csv.js';

test('empty points -> header only', () => {
  assert.equal(toCSV([]), 'freq_hz,level_db\n');
});

test('formats rows as freq_hz,level_db', () => {
  const csv = toCSV([
    { freq_hz: 20, level_db: -3.2 },
    { freq_hz: 1000.5, level_db: 0 },
  ]);
  assert.equal(csv, 'freq_hz,level_db\n20,-3.2\n1000.5,0\n');
});

test('rounds to configured precision and trims trailing zeros', () => {
  const csv = toCSV([{ freq_hz: 20.00012, level_db: -3.2049 }], { freqDigits: 3, dbDigits: 2 });
  assert.equal(csv, 'freq_hz,level_db\n20,-3.2\n');
});
