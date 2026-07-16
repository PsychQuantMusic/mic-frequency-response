// #8 資料層 schema 一致性測試：守住 data/ 的兩種 CSV schema 與 meta 對應。
// 未來資料筆數成長後，schema drift 不能只靠人眼抓。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const DATA_DIR = join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'data');
const LEGACY_REPRODUCTION_FILE = join(DATA_DIR, 'legacy-overlay-verified.txt');
const LEGACY_REPRODUCTION_ENTRIES = readFileSync(LEGACY_REPRODUCTION_FILE, 'utf8')
  .split('\n')
  .map((line) => line.trim())
  .filter((line) => line && !line.startsWith('#'));
const LEGACY_REPRODUCTION_ALLOWLIST = new Set(LEGACY_REPRODUCTION_ENTRIES);

// 2026-07-15 一次性遷移基線。registry 可以移除／升級既有項目，但不能新增項目；
// 基線刻意放在測試程式中，避免同步修改 metadata + registry 就靜默擴張 legacy 例外。
const LEGACY_MIGRATION_BASELINE = new Set(`
akg-c214/frequency-response--typical.csv
akg-c414-xlii/frequency-response--cardioid.csv
akg-c451-b/frequency-response--typical.csv
akg-d112-mkii/frequency-response--typical.csv
akg-d112-mkii/frequency-response--proximity-10cm.csv
akg-d5/frequency-response--typical.csv
audio-technica-at2020/frequency-response--typical.csv
audio-technica-at2035/frequency-response--typical.csv
audio-technica-at4040/frequency-response--typical.csv
audix-d6/frequency-response--typical.csv
audix-i5/frequency-response--typical.csv
audix-om5/frequency-response--typical.csv
electro-voice-re20/frequency-response--typical.csv
electro-voice-re320/frequency-response--typical.csv
lewitt-lct-440-pure/frequency-response--typical.csv
rode-nt1/frequency-response--typical.csv
rode-nt5/frequency-response--typical.csv
rode-podmic/frequency-response--typical.csv
rode-procaster/frequency-response--typical.csv
se-electronics-se2200/frequency-response--typical.csv
sennheiser-e835/frequency-response--typical.csv
sennheiser-e845/frequency-response--typical.csv
sennheiser-e904/frequency-response--typical.csv
sennheiser-e906/frequency-response--typical.csv
sennheiser-e914/frequency-response--typical.csv
sennheiser-e935/frequency-response--typical.csv
sennheiser-md421-ii/frequency-response--typical.csv
sennheiser-md441-u/frequency-response--typical.csv
sennheiser-mkh416/frequency-response--typical.csv
shure-beta181-c/frequency-response--typical.csv
shure-beta52a/frequency-response--typical.csv
shure-beta52a/frequency-response--proximity-3mm.csv
shure-beta52a/frequency-response--proximity-25mm.csv
shure-beta52a/frequency-response--proximity-51mm.csv
shure-beta57a/frequency-response--typical.csv
shure-beta57a/frequency-response--proximity-3mm.csv
shure-beta57a/frequency-response--proximity-25mm.csv
shure-beta57a/frequency-response--proximity-51mm.csv
shure-beta58a/frequency-response--typical.csv
shure-beta58a/frequency-response--proximity-3mm.csv
shure-beta58a/frequency-response--proximity-25mm.csv
shure-beta58a/frequency-response--proximity-51mm.csv
shure-beta87a/frequency-response--typical.csv
shure-beta87a/frequency-response--proximity-1cm.csv
shure-beta91a/frequency-response--typical.csv
shure-ksm137/frequency-response--typical.csv
shure-ksm137/frequency-response--proximity-15cm.csv
shure-ksm8/frequency-response--typical.csv
shure-sm35/frequency-response--typical.csv
shure-sm35/frequency-response--proximity-1cm.csv
shure-sm57/frequency-response--typical.csv
shure-sm58/frequency-response--typical.csv
shure-sm81/frequency-response--typical.csv
`.trim().split('\n'));

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

const ALLOWED_REPRODUCTION_STATUS = {
  'manufacturer-values': new Set(['manufacturer-issued']),
  'vector-path-extraction': new Set(['vector-source-recorded']),
  'seeded-pixel-trace': new Set(['command-recorded', 'legacy-overlay-verified']),
};

const ALLOWED_VERIFICATION_RELATIONS = new Set([
  'current-csv-directly-verified',
  'retained-unmodified-subset-of-verified-trace',
]);

function micDirs() {
  return readdirSync(DATA_DIR).filter((d) => {
    const p = join(DATA_DIR, d);
    return statSync(p).isDirectory();
  });
}

function stripYamlInlineComment(value) {
  const raw = String(value ?? '');
  let quote = null;
  let escaped = false;
  for (let i = 0; i < raw.length; i++) {
    const char = raw[i];
    if (escaped) {
      escaped = false;
      continue;
    }
    if (quote === '"' && char === '\\') {
      escaped = true;
      continue;
    }
    if (quote) {
      if (char === quote) quote = null;
      continue;
    }
    if (char === '"' || char === "'") {
      quote = char;
      continue;
    }
    if (char === '#' && (i === 0 || /\s/.test(raw[i - 1]))) {
      return raw.slice(0, i).trimEnd();
    }
  }
  return raw;
}

function unquoteYamlScalar(value) {
  const trimmed = stripYamlInlineComment(value).trim();
  if (/^(?:~|null)$/i.test(trimmed)) return '';
  if (
    trimmed.length >= 2
    && ((trimmed.startsWith('"') && trimmed.endsWith('"'))
      || (trimmed.startsWith("'") && trimmed.endsWith("'")))
  ) {
    return trimmed.slice(1, -1);
  }
  return trimmed;
}

function parseFlowList(value) {
  if (Array.isArray(value)) return value.map((item) => unquoteYamlScalar(item)).filter(Boolean);
  const match = /^\[([\s\S]*)\]$/.exec(String(value ?? '').trim());
  if (!match) return [];
  return match[1]
    .split(',')
    .map((item) => unquoteYamlScalar(item))
    .filter(Boolean);
}

function commandHasOptionValue(command, option) {
  return new RegExp(`(?:^|\\s)${option}(?:=\\S+|\\s+(?!--)\\S+)`).test(String(command ?? ''));
}

function setCurveField(curve, key, value) {
  const normalized = unquoteYamlScalar(value);
  const fieldMap = {
    file: 'file',
    kind: 'kind',
    data_origin: 'dataOrigin',
    digitization_method: 'digitizationMethod',
    reproduction_status: 'reproductionStatus',
    formal_point_count: 'formalPointCount',
    formal_frequency_span_hz: 'formalFrequencySpanHz',
    formal_angle_span_deg: 'formalAngleSpanDeg',
    verification_relation: 'verificationRelation',
    condition: 'condition',
    trace_command: 'traceCommand',
  };
  const target = fieldMap[key];
  if (target) curve[target] = normalized;
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
  let pendingBlockField = null;

  const finishCurrent = () => {
    if (!current) return;
    current.hasCondition = Boolean(current.condition?.trim());
    current.hasTraceCommand = Boolean(current.traceCommand?.trim());
    curves.push(current);
    current = null;
    pendingBlockField = null;
  };

  for (const line of lines) {
    if (/^curves:\s*(#.*)?$/.test(line)) { inCurves = true; continue; } // 容許行尾註解
    if (inCurves && /^[A-Za-z_]/.test(line)) {
      finishCurrent();
      inCurves = false;
    } // 下一個 top-level key
    if (!inCurves) continue;
    if (/^\s*#/.test(line)) continue; // 註解
    const entryStart = /^ {2}-\s*(.*)$/.exec(line);
    if (entryStart) {
      finishCurrent();
      current = {};
      // flow style：- { file: x, kind: y, condition: "..." }
      const flow = entryStart[1];
      const fFile = /file:\s*"?([^,}"\s]+)"?/.exec(flow);
      const fKind = /kind:\s*"?([^,}"\s]+)"?/.exec(flow);
      const fOrigin = /data_origin:\s*"?([^,}"\s]+)"?/.exec(flow);
      const fMethod = /digitization_method:\s*"?([^,}"\s]+)"?/.exec(flow);
      const fReproduction = /reproduction_status:\s*"?([^,}"\s]+)"?/.exec(flow);
      const fCondition = /condition:\s*([^,}]+)/.exec(flow);
      const fTraceCommand = /trace_command:\s*([^,}]+)/.exec(flow);
      if (fFile) setCurveField(current, 'file', fFile[1]);
      if (fKind) setCurveField(current, 'kind', fKind[1]);
      if (fOrigin) setCurveField(current, 'data_origin', fOrigin[1]);
      if (fMethod) setCurveField(current, 'digitization_method', fMethod[1]);
      if (fReproduction) setCurveField(current, 'reproduction_status', fReproduction[1]);
      if (fCondition) setCurveField(current, 'condition', fCondition[1]);
      if (fTraceCommand) setCurveField(current, 'trace_command', fTraceCommand[1]);
      continue;
    }
    if (current) {
      // block scalar 的 `>` / `|` 只是 YAML 指示符，不算實際內容。
      if (pendingBlockField) {
        const continuation = /^ {6,}(\S.*)$/.exec(line);
        if (continuation) {
          const previous = current[pendingBlockField] ?? '';
          current[pendingBlockField] = `${previous}${previous ? ' ' : ''}${continuation[1].trim()}`;
          continue;
        }
        if (/^\s*$/.test(line)) continue;
        pendingBlockField = null;
      }

      const blockField = /^ {4}([A-Za-z_]+):\s*(.*)$/.exec(line);
      if (blockField) {
        const [, key, rawValue] = blockField;
        if (
          (key === 'condition' || key === 'trace_command')
          && /^[>|][+-]?\s*(?:#.*)?$/.test(rawValue.trim())
        ) {
          const target = key === 'condition' ? 'condition' : 'traceCommand';
          current[target] = '';
          pendingBlockField = target;
        } else {
          setCurveField(current, key, rawValue);
        }
      }
    }
  }
  finishCurrent();
  return curves.filter((c) => c.file);
}

function parseTopLevelMapping(metaText, key) {
  const lines = metaText.split('\n');
  const fields = {};
  let inBlock = false;
  let pendingField = null;
  let pendingSequenceField = null;
  for (const line of lines) {
    if (new RegExp(`^${key}:\\s*(#.*)?$`).test(line)) {
      inBlock = true;
      continue;
    }
    if (inBlock && /^[A-Za-z_]/.test(line)) break;
    if (!inBlock) continue;

    if (/^\s*#/.test(line)) continue;

    if (pendingSequenceField) {
      const item = /^ {4}-\s*(\S.*)$/.exec(line);
      if (item) {
        fields[pendingSequenceField].push(unquoteYamlScalar(item[1]));
        continue;
      }
      if (/^\s*$/.test(line)) continue;
      pendingSequenceField = null;
    }

    if (pendingField) {
      const continuation = /^ {4,}(\S.*)$/.exec(line);
      if (continuation) {
        const previous = fields[pendingField] ?? '';
        fields[pendingField] = `${previous}${previous ? ' ' : ''}${continuation[1].trim()}`;
        continue;
      }
      if (/^\s*$/.test(line)) continue;
      pendingField = null;
    }

    const field = /^ {2}([A-Za-z_]+):\s*(.*)$/.exec(line);
    if (!field) continue;
    const [, fieldName, rawValue] = field;
    if (fieldName === 'curves' && rawValue.trim() === '') {
      fields[fieldName] = [];
      pendingSequenceField = fieldName;
    } else if (/^[>|][+-]?\s*(?:#.*)?$/.test(rawValue.trim())) {
      fields[fieldName] = '';
      pendingField = fieldName;
    } else {
      fields[fieldName] = unquoteYamlScalar(rawValue);
    }
  }
  return fields;
}

test('YAML block indicators do not count as provenance evidence', () => {
  const synthetic = `curves:
  - file: frequency-response--typical.csv
    data_origin: official-published-curve
    digitization_method: seeded-pixel-trace
    reproduction_status: command-recorded
    condition: # 行尾註解不是證據
    trace_command: > # YAML 指示符也不是證據
      # block 中只有註解仍不得通過
verification:
  method: # 同樣不能把行尾註解算成內容
  result: |
    # block 中只有註解仍不得通過
`;
  const [curve] = parseCurves(synthetic);
  const verification = parseTopLevelMapping(synthetic, 'verification');
  assert.equal(curve.hasCondition, false);
  assert.equal(curve.hasTraceCommand, false);
  assert.equal(verification.method, '');
  assert.equal(verification.result, '');
});

test('YAML block scalar content is collected without treating nested text as fields', () => {
  const synthetic = `curves:
  - file: frequency-response--typical.csv
    condition: >
      measured prefix only
    trace_command: >
      node digitizer/cli.js --bin chart.bin --size 100x100
      --cal-x 0,20 99,20000 --cal-y 0,10 99,-10 --seed 50,50 --x-range 0,99
verification:
  curves:
    - frequency-response--typical.csv
  method: >
    interpolated overlay
  result: >
    100 columns measured
`;
  const [curve] = parseCurves(synthetic);
  const verification = parseTopLevelMapping(synthetic, 'verification');
  assert.equal(curve.hasCondition, true);
  assert.match(curve.traceCommand, /--x-range 0,99/);
  assert.equal(verification.method, 'interpolated overlay');
  assert.equal(verification.result, '100 columns measured');
  assert.deepEqual(parseFlowList(verification.curves), ['frequency-response--typical.csv']);
});

test('recorded trace options require an actual value', () => {
  assert.equal(commandHasOptionValue('trace --bin chart.bin --size 100x100', '--bin'), true);
  assert.equal(commandHasOptionValue('trace --bin=chart.bin --size=100x100', '--size'), true);
  assert.equal(commandHasOptionValue('trace --bin --size 100x100', '--bin'), false);
  assert.equal(commandHasOptionValue('trace --bin=', '--bin'), false);
});

test('YAML inline comment stripping preserves quoted hashes and URL fragments', () => {
  assert.equal(unquoteYamlScalar('"verified # section" # reviewer note'), 'verified # section');
  assert.equal(
    unquoteYamlScalar('https://manufacturer.example/chart#frequency-response'),
    'https://manufacturer.example/chart#frequency-response',
  );
  assert.equal(unquoteYamlScalar('# comment only'), '');
  for (const yamlNull of ['~', 'null', 'Null', 'NULL']) {
    assert.equal(unquoteYamlScalar(yamlNull), '', `${yamlNull} 是 YAML null，不是文字證據`);
  }
  assert.equal(unquoteYamlScalar('"null"'), 'null', '明確引號包住的文字仍是文字');
});

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
    const source = parseTopLevelMapping(metaText, 'source');
    assert.match(source.url ?? '', /^https?:\/\//, 'source.url 必須指向官方 HTTP(S) 來源');
    assert.ok(source.chart, 'source.chart 必須辨識正式資料對應的圖表或數值表');
    assert.match(source.retrieved ?? '', /^\d{4}-\d{2}-\d{2}/, 'source.retrieved 必須是日期');

    for (const c of curves) {
      assert.ok(c.hasCondition, `${c.file}: 缺少非空白 condition`);
      assert.ok(c.dataOrigin, `${c.file}: 缺 data_origin`);
      assert.ok(ALLOWED_ORIGINS.has(c.dataOrigin), `${c.file}: 不允許的 data_origin ${c.dataOrigin}`);
      assert.ok(c.digitizationMethod, `${c.file}: 缺 digitization_method`);
      assert.ok(
        ALLOWED_METHODS[c.dataOrigin]?.has(c.digitizationMethod),
        `${c.file}: ${c.dataOrigin} 不允許 digitization_method ${c.digitizationMethod}`,
      );
      assert.ok(c.reproductionStatus, `${c.file}: 缺 reproduction_status`);
      assert.ok(
        ALLOWED_REPRODUCTION_STATUS[c.digitizationMethod]?.has(c.reproductionStatus),
        `${c.file}: ${c.digitizationMethod} 不允許 reproduction_status ${c.reproductionStatus}`,
      );
      assert.ok(
        ALLOWED_VERIFICATION_RELATIONS.has(c.verificationRelation),
        `${c.file}: 缺少或不允許的 verification_relation ${c.verificationRelation}`,
      );

      const formalRows = readFileSync(join(dirPath, c.file), 'utf8').trim().split('\n').slice(1);
      const formalFrequencies = formalRows.map((row) => Number(row.split(',', 1)[0]));
      assert.equal(
        Number(c.formalPointCount),
        formalRows.length,
        `${c.file}: formal_point_count 必須等於目前正式 CSV 的實際點數`,
      );
      const formalDomainName = c.kind === 'polar-pattern'
        ? 'formal_angle_span_deg'
        : 'formal_frequency_span_hz';
      const formalDomainValue = c.kind === 'polar-pattern'
        ? c.formalAngleSpanDeg
        : c.formalFrequencySpanHz;
      const formalSpan = parseFlowList(formalDomainValue).map(Number);
      assert.deepEqual(
        formalSpan,
        [formalFrequencies[0], formalFrequencies.at(-1)],
        `${c.file}: ${formalDomainName} 必須等於目前正式 CSV 第一欄的首末值`,
      );
      if (c.reproductionStatus === 'legacy-overlay-verified') {
        assert.ok(
          LEGACY_REPRODUCTION_ALLOWLIST.has(`${dir}/${c.file}`),
          `${c.file}: legacy-overlay-verified 只能用於凍結的遷移清單，新增資料必須保存完整重跑紀錄`,
        );
      }
      if (c.reproductionStatus === 'command-recorded') {
        assert.ok(c.hasTraceCommand, `${c.file}: command-recorded 必須保存 trace_command`);
        const requiredOptions = c.kind === 'polar-pattern'
          ? ['--bin', '--size', '--cal-center', '--cal-rings', '--seed', '--angle-range']
          : [
              '--bin', '--size', '--cal-x', '--cal-y', '--seed', '--x-range',
              '--strategy', '--tolerance', '--max-jump', '--target-color',
            ];
        for (const requiredOption of requiredOptions) {
          assert.ok(
            commandHasOptionValue(c.traceCommand, requiredOption),
            `${c.file}: trace_command 缺少可重跑的 ${requiredOption}`,
          );
        }
      }

      if (c.dataOrigin === 'official-published-curve') {
        const verificationKey = c.kind === 'polar-pattern' ? 'polar_verification' : 'verification';
        const verification = parseTopLevelMapping(metaText, verificationKey);
        assert.ok(verification.method, `${c.file}: ${verificationKey}.method 不得缺漏`);
        assert.ok(verification.result, `${c.file}: ${verificationKey}.result 不得缺漏`);
        assert.ok(
          parseFlowList(verification.curves).includes(c.file),
          `${c.file}: ${verificationKey}.curves 必須明列這條曲線，不能借用同檔其他曲線的驗證`,
        );
        if (c.verificationRelation === 'retained-unmodified-subset-of-verified-trace') {
          assert.ok(
            verification.scope_note,
            `${c.file}: 裁切後子集合必須以 ${verificationKey}.scope_note 區分全幅 trace 與正式 CSV`,
          );
        }
      }
    }
  });
}

test('legacy-overlay-verified registry exactly matches the frozen migration set', () => {
  assert.equal(
    LEGACY_REPRODUCTION_ENTRIES.length,
    LEGACY_REPRODUCTION_ALLOWLIST.size,
    'legacy 遷移清單不得有重複項目',
  );
  for (const entry of LEGACY_REPRODUCTION_ALLOWLIST) {
    assert.ok(
      LEGACY_MIGRATION_BASELINE.has(entry),
      `${entry}: legacy 遷移清單只能縮減，不能新增資料`,
    );
  }

  const actual = [];
  for (const dir of micDirs()) {
    const metaText = readFileSync(join(DATA_DIR, dir, 'meta.yaml'), 'utf8');
    for (const curve of parseCurves(metaText)) {
      if (curve.reproductionStatus === 'legacy-overlay-verified') {
        actual.push(`${dir}/${curve.file}`);
      }
    }
  }
  assert.deepEqual(
    actual.sort(),
    [...LEGACY_REPRODUCTION_ALLOWLIST].sort(),
    'legacy 遷移清單不得靜默增加或留下已升級／移除的項目',
  );
});
