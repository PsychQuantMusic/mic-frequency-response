#!/usr/bin/env node
// Headless trace CLI（#12）：真實圖 seeded trace 不再需要瀏覽器。
//
// 兩步流程：
//   1) python3 scripts/dump_pixels.py chart.png /tmp/chart.bin   # 印出 "W H"
//   2) node digitizer/cli.js --bin /tmp/chart.bin --size WxH \
//        --cal-x PX,HZ PX,HZ --cal-y PY,DB PY,DB --seed PX,PY > curve.csv
//
// 校準參數格式對齊 scripts/overlay_verify.py（--cal-x 兩點、--cal-y 兩點）。
// 引擎即 trace.js（與瀏覽器 UI 同一套），預設 strategy=viterbi（#9）。
// 產出的 CSV 仍須跑 overlay_verify.py 驗證（紀律同 skill）。

import { readFileSync } from 'node:fs';
import { traceCurve } from './trace.js';
import { toCSV } from './csv.js';

function fail(msg) {
  process.stderr.write(msg + '\n');
  process.exit(1);
}

function parsePair(s, what) {
  const parts = String(s).split(',').map(Number);
  if (parts.length !== 2 || parts.some((v) => !Number.isFinite(v))) fail(`${what} 格式錯誤（需 A,B）: ${s}`);
  return parts;
}

function parseArgs(argv) {
  const a = { tolerance: 60, maxJump: 12, strategy: 'viterbi', targetColor: [0, 0, 0, 255] };
  for (let i = 2; i < argv.length; i++) {
    const k = argv[i];
    const next = () => argv[++i];
    switch (k) {
      case '--bin': a.bin = next(); break;
      case '--size': {
        const m = /^(\d+)x(\d+)$/.exec(next());
        if (!m) fail('--size 格式錯誤（需 WxH）');
        a.width = Number(m[1]); a.height = Number(m[2]);
        break;
      }
      case '--cal-x': a.calX = [parsePair(next(), '--cal-x'), parsePair(next(), '--cal-x')]; break;
      case '--cal-y': a.calY = [parsePair(next(), '--cal-y'), parsePair(next(), '--cal-y')]; break;
      case '--seed': { const [px, py] = parsePair(next(), '--seed'); a.seed = { px, py }; break; }
      case '--x-range': { const [s, e] = parsePair(next(), '--x-range'); a.xStart = s; a.xEnd = e; break; }
      case '--tolerance': a.tolerance = Number(next()); break;
      case '--max-jump': a.maxJump = Number(next()); break;
      case '--max-run-size': a.maxRunSize = Number(next()); break;
      case '--strategy': a.strategy = next(); break;
      case '--target-color': {
        const c = String(next()).split(',').map(Number);
        if (c.length < 3 || c.some((v) => !Number.isFinite(v))) fail('--target-color 格式錯誤（需 R,G,B[,A]）');
        a.targetColor = [c[0], c[1], c[2], c[3] ?? 255];
        break;
      }
      default: fail(`未知參數: ${k}`);
    }
  }
  for (const req of ['bin', 'width', 'calX', 'calY', 'seed']) {
    if (a[req] === undefined) fail(`缺少必要參數（--bin --size --cal-x --cal-y --seed）`);
  }
  return a;
}

const a = parseArgs(process.argv);
const buf = readFileSync(a.bin);
if (buf.length !== a.width * a.height * 4) {
  fail(`bin 大小 ${buf.length} ≠ ${a.width}x${a.height}x4 —— --size 與 dump_pixels.py 輸出不符？`);
}
const pixelAt = (x, y) => {
  const i = (y * a.width + x) * 4;
  return [buf[i], buf[i + 1], buf[i + 2], buf[i + 3]];
};

const calib = {
  freq: { p1: { px: a.calX[0][0], value: a.calX[0][1] }, p2: { px: a.calX[1][0], value: a.calX[1][1] } },
  db: { p1: { py: a.calY[0][0], value: a.calY[0][1] }, p2: { py: a.calY[1][0], value: a.calY[1][1] } },
};

const points = traceCurve({
  pixelAt, width: a.width, height: a.height,
  xStart: a.xStart, xEnd: a.xEnd, calib,
  targetColor: a.targetColor, tolerance: a.tolerance,
  seed: a.seed, maxJump: a.maxJump, maxRunSize: a.maxRunSize, strategy: a.strategy,
});

if (points.length === 0) {
  fail('trace 回空（種子不在合格 run 附近？）—— 檢查 --seed 是否點在曲線上');
}
process.stderr.write(`traced ${points.length} points (strategy=${a.strategy})\n`);
process.stdout.write(toCSV(points));
