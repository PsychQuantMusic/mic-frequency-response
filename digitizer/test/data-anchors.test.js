// #18 anchor-point 回歸測試：守住 data/ 每條 FR 曲線的 dB「值」。
//
// data-schema.test.js 只驗格式（header/兩欄/遞增/finite），不驗任何值。本測試補上值的
// 回歸網：每條 frequency-response*.csv 有一份 data/<slug>/anchors.yaml，內含 3-5 個
// (freq_hz, expected_db, tol_db) 自動產生的回歸參考點（由 scripts/gen_anchors.py 從已通過
// overlay 驗證的 CSV 取 golden snapshot；它不是獨立原廠證據）。這裡對 CSV 做「與產生器逐字相同」的 log-f 內插，
// 斷言 |interp − expected| ≤ tol。CSV 值一被改壞（typo / 腳本 bug / CRLF / 換錯支資料），
// 內插值漂出容差即 fail —— 不需原廠圖、可上 CI。誠實邊界：spot-check 非全曲線，抓值崩壞/
// 特徵錯位，抓不到兩 anchor 間細微竄改（見 issue #18 診斷）。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync, statSync, existsSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const DATA_DIR = join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'data');

function micDirs() {
  return readdirSync(DATA_DIR).filter((d) => statSync(join(DATA_DIR, d)).isDirectory());
}

/**
 * 最小 anchors.yaml 解析器 —— 只針對本 repo 受控格式（gen_anchors.py 產出），非通用
 * YAML parser。與 data-schema.test.js 的 parseCurves 同套路：block 標頭 `- file:` 起新
 * 曲線，flow-style `{ freq_hz:.. expected_db:.. tol_db:.. }` 逐點以 regex 抽取。
 */
function parseAnchors(text) {
  const curves = [];
  let current = null;
  for (const line of text.split('\n')) {
    if (/^\s*#/.test(line)) continue;
    const fileM = /^\s*-\s*file:\s*"?([^"\s]+)"?/.exec(line);
    if (fileM) {
      if (current) curves.push(current);
      current = { file: fileM[1], points: [] };
      continue;
    }
    const ptM = /freq_hz:\s*(-?[\d.]+).*expected_db:\s*(-?[\d.]+).*tol_db:\s*(-?[\d.]+)/.exec(line);
    if (ptM && current) {
      current.points.push({
        freq: Number(ptM[1]),
        expected: Number(ptM[2]),
        tol: Number(ptM[3]),
      });
    }
  }
  if (current) curves.push(current);
  return curves;
}

function readCurve(path) {
  const lines = readFileSync(path, 'utf8').trim().split('\n');
  const pts = [];
  for (let i = 1; i < lines.length; i++) {
    const [a, b] = lines[i].split(',');
    pts.push([Number(a), Number(b)]);
  }
  return pts;
}

// log-frequency linear interpolation; clamps outside the measured range.
// MUST stay byte-for-byte equivalent to interp_logf in scripts/gen_anchors.py —
// the whole snapshot invariant (dev ~0 when CSV unchanged) depends on it.
function interpLogf(pts, freq) {
  if (freq <= pts[0][0]) return pts[0][1];
  const last = pts[pts.length - 1];
  if (freq >= last[0]) return last[1];
  const lf = Math.log10(freq);
  for (let i = 1; i < pts.length; i++) {
    const [f0, d0] = pts[i - 1];
    const [f1, d1] = pts[i];
    if (freq <= f1) {
      const t = (lf - Math.log10(f0)) / (Math.log10(f1) - Math.log10(f0));
      return d0 + t * (d1 - d0);
    }
  }
  return last[1];
}

for (const dir of micDirs()) {
  const dirPath = join(DATA_DIR, dir);
  const frCsvs = readdirSync(dirPath).filter(
    (f) => f.startsWith('frequency-response') && f.endsWith('.csv'),
  );
  const anchorsPath = join(dirPath, 'anchors.yaml');

  test(`${dir}: every FR curve has an anchors.yaml block (>=3 points)`, () => {
    if (frCsvs.length === 0) return; // polar-only mic → 無 FR 曲線可 anchor，不強制 anchors.yaml
    assert.ok(existsSync(anchorsPath), `${dir}: 缺 anchors.yaml（新增麥克風後跑 scripts/gen_anchors.py）`);
    const curves = parseAnchors(readFileSync(anchorsPath, 'utf8'));
    const anchored = new Set(curves.map((c) => c.file));
    for (const f of frCsvs) {
      assert.ok(anchored.has(f), `${f} 有 CSV 但 anchors.yaml 未涵蓋`);
    }
    for (const c of curves) {
      assert.ok(frCsvs.includes(c.file), `anchors.yaml 引用 ${c.file} 但 FR CSV 不存在`);
      assert.ok(c.points.length >= 3, `${c.file}: anchor 至少 3 點（got ${c.points.length}）`);
    }
  });

  if (!existsSync(anchorsPath)) continue;
  const curves = parseAnchors(readFileSync(anchorsPath, 'utf8'));

  for (const c of curves) {
    test(`${dir}/${c.file}: anchor 值守在容差內`, () => {
      const pts = readCurve(join(dirPath, c.file));
      for (const p of c.points) {
        const got = interpLogf(pts, p.freq);
        const dev = Math.abs(got - p.expected);
        assert.ok(
          dev <= p.tol,
          `${c.file} @ ${p.freq} Hz: 內插 ${got.toFixed(3)} dB 偏離 anchor ${p.expected} dB ` +
            `達 ${dev.toFixed(3)} dB（tol ${p.tol}）——曲線值可能被改壞`,
        );
      }
    });
  }
}
