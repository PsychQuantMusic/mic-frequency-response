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
      if (fFile) current.file = fFile[1];
      if (fKind) current.kind = fKind[1];
      continue;
    }
    if (current) {
      // block style 續行
      const bFile = /^\s+file:\s*"?([^"\s]+)"?/.exec(line);
      const bKind = /^\s+kind:\s*"?([^"\s]+)"?/.exec(line);
      if (bFile) current.file = bFile[1];
      if (bKind) current.kind = bKind[1];
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
}
