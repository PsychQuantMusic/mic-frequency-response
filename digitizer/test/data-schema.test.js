// #8 資料層 schema 一致性測試：守住 data/ 的兩種 CSV schema 與 meta 對應。
// 未來資料筆數成長後，schema drift 不能只靠人眼抓。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const DATA_DIR = join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'data');

const SCHEMAS = {
  'frequency-response--': { header: 'freq_hz,level_db', firstCol: 'freq_hz' },
  'polar-pattern--': { header: 'angle_deg,level_db', firstCol: 'angle_deg' },
};

const ALLOWED_ORIGINS = new Set([
  'manufacturer-numeric',
  'official-published-curve',
]);

const ALLOWED_METHODS = {
  'manufacturer-numeric': new Set(['manufacturer-values']),
  'official-published-curve': new Set(['vector-path-extraction', 'seeded-pixel-trace']),
};

function micDirs() {
  return readdirSync(DATA_DIR).filter((d) => {
    const p = join(DATA_DIR, d);
    return statSync(p).isDirectory();
  });
}

/**
 * 最小 meta.yaml curves[] 解析器 —— 只針對本 repo 受控格式（block + flow style 的
 * file:/kind: 欄位），不是通用 YAML parser。格式漂移時測試會 fail 在「meta 未引用」
 * 或「檔案不存在」的斷言上；若確認是格式演進，請同步更新此解析器。
 */
function parseCurves(metaText) {
  const lines = metaText.split('\n');
  const curves = [];
  let inCurves = false;
  let current = null;
  for (const line of lines) {
    if (/^curves:\s*(#.*)?$/.test(line)) { inCurves = true; continue; } // 容許行尾註解
    if (inCurves && /^[A-Za-z_]/.test(line)) { inCurves = false; } // 下一個 top-level key
    if (!inCurves) continue;
    if (/^\s*#/.test(line)) continue; // 註解
    const entryStart = /^\s*-\s*(.*)$/.exec(line);
    if (entryStart) {
      if (current) curves.push(current);
      current = {};
      // flow style：- { file: x, kind: y, condition: "..." }
      const flow = entryStart[1];
      const fFile = /file:\s*"?([^,}"\s]+)"?/.exec(flow);
      const fKind = /kind:\s*"?([^,}"\s]+)"?/.exec(flow);
      const fOrigin = /data_origin:\s*"?([^,}"\s]+)"?/.exec(flow);
      const fMethod = /digitization_method:\s*"?([^,}"\s]+)"?/.exec(flow);
      if (fFile) current.file = fFile[1];
      if (fKind) current.kind = fKind[1];
      if (fOrigin) current.dataOrigin = fOrigin[1];
      if (fMethod) current.digitizationMethod = fMethod[1];
      continue;
    }
    if (current) {
      // block style 續行
      const bFile = /^\s+file:\s*"?([^"\s]+)"?/.exec(line);
      const bKind = /^\s+kind:\s*"?([^"\s]+)"?/.exec(line);
      const bOrigin = /^\s+data_origin:\s*"?([^"\s]+)"?/.exec(line);
      const bMethod = /^\s+digitization_method:\s*"?([^"\s]+)"?/.exec(line);
      if (bFile) current.file = bFile[1];
      if (bKind) current.kind = bKind[1];
      if (bOrigin) current.dataOrigin = bOrigin[1];
      if (bMethod) current.digitizationMethod = bMethod[1];
    }
  }
  if (current) curves.push(current);
  return curves.filter((c) => c.file);
}

for (const dir of micDirs()) {
  const dirPath = join(DATA_DIR, dir);
  const csvFiles = readdirSync(dirPath).filter((f) => f.endsWith('.csv'));
  const metaText = readFileSync(join(dirPath, 'meta.yaml'), 'utf8');
  const curves = parseCurves(metaText);
  const rangeMatch = /^frequency_range_hz:\s*\[\s*([\d.]+)\s*,\s*([\d.]+)\s*\]/m.exec(metaText);

  test(`${dir}: every CSV has a known schema prefix and valid content`, () => {
    assert.ok(csvFiles.length > 0, '資料夾至少要有一個 CSV');
    for (const f of csvFiles) {
      const prefix = Object.keys(SCHEMAS).find((p) => f.startsWith(p));
      assert.ok(prefix, `${f}: 檔名前綴必須是 frequency-response-- 或 polar-pattern--`);
      const { header } = SCHEMAS[prefix];
      const text = readFileSync(join(dirPath, f), 'utf8').trim();
      const lines = text.split('\n');
      assert.equal(lines[0], header, `${f}: header 應為 ${header}`);
      let prev = -Infinity;
      for (let i = 1; i < lines.length; i++) {
        const parts = lines[i].split(',');
        assert.equal(parts.length, 2, `${f}:${i + 1} 應為兩欄`);
        // Number('') === 0 —— 空欄位會靜默變 0（cluster verify MEDIUM finding），先擋
        assert.ok(parts[0].trim() !== '' && parts[1].trim() !== '', `${f}:${i + 1} 欄位不得為空`);
        const a = Number(parts[0]);
        const b = Number(parts[1]);
        assert.ok(Number.isFinite(a) && Number.isFinite(b), `${f}:${i + 1} 需為 finite 數值`);
        assert.ok(a > prev, `${f}:${i + 1} 第一欄需嚴格遞增 (${a} ≤ ${prev})`);
        if (f.startsWith('frequency-response--')) {
          assert.ok(rangeMatch, 'frequency-response 資料必須宣告 frequency_range_hz');
          const minHz = Number(rangeMatch[1]);
          const maxHz = Number(rangeMatch[2]);
          assert.ok(
            a >= minHz && a <= maxHz,
            `${f}:${i + 1} 的 ${a} Hz 超出原廠標示範圍 ${minHz}–${maxHz} Hz`,
          );
        }
        prev = a;
      }
      assert.ok(lines.length >= 2, `${f}: 至少一個資料點`);
    }
  });

  test(`${dir}: meta.yaml curves[] ↔ CSV files correspond bidirectionally`, () => {
    const metaFiles = curves.map((c) => c.file);
    for (const f of csvFiles) {
      assert.ok(metaFiles.includes(f), `${f} 存在但 meta.yaml curves[] 未引用`);
    }
    for (const mf of metaFiles) {
      assert.ok(csvFiles.includes(mf), `meta.yaml 引用 ${mf} 但檔案不存在`);
    }
  });

  test(`${dir}: curves[].kind consistent with filename prefix`, () => {
    for (const c of curves) {
      if (c.file.startsWith('polar-pattern--')) {
        assert.equal(c.kind, 'polar-pattern', `${c.file}: polar 檔必須標 kind: polar-pattern`);
      } else if (c.file.startsWith('frequency-response--')) {
        assert.ok(c.kind === undefined || c.kind === 'frequency-response',
          `${c.file}: FR 檔 kind 應缺省或 frequency-response (got ${c.kind})`);
      }
    }
  });

  test(`${dir}: every published curve has approved provenance`, () => {
    assert.match(metaText, /^source:\s*$/m, 'meta.yaml 必須有 source');
    assert.match(metaText, /^\s+url:\s*https?:\/\//m, 'source.url 必須指向官方 HTTP(S) 來源');
    assert.match(metaText, /^\s+retrieved:\s*\d{4}-\d{2}-\d{2}/m, 'source.retrieved 必須是日期');

    for (const c of curves) {
      assert.ok(c.dataOrigin, `${c.file}: 缺 data_origin`);
      assert.ok(ALLOWED_ORIGINS.has(c.dataOrigin), `${c.file}: 不允許的 data_origin ${c.dataOrigin}`);
      assert.ok(c.digitizationMethod, `${c.file}: 缺 digitization_method`);
      assert.ok(
        ALLOWED_METHODS[c.dataOrigin]?.has(c.digitizationMethod),
        `${c.file}: ${c.dataOrigin} 不允許 digitization_method ${c.digitizationMethod}`,
      );

      if (c.dataOrigin === 'official-published-curve') {
        const verificationKey = c.kind === 'polar-pattern' ? 'polar_verification' : 'verification';
        assert.match(
          metaText,
          new RegExp(`^${verificationKey}:\\s*$`, 'm'),
          `${c.file}: 官方圖表數位化資料必須有 ${verificationKey}`,
        );
      }
    }
  });
}
