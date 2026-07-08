// #9 Viterbi 全域最優路徑：greedy 逐欄局部決策無法回頭的兩個結構性跟丟場景。
// 理論框架見 orthogonal-projects academic/curve-extraction-priors（Level 1 全域架構）。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { traceCurve } from '../trace.js';
import { makeTransform } from '../coords.js';

const calib = {
  freq: { p1: { px: 100, value: 20 }, p2: { px: 1100, value: 20000 } },
  db: { p1: { py: 50, value: 0 }, p2: { py: 450, value: -40 } }, // 10 px/dB
};
const BLACK = [0, 0, 0, 255];
const WHITE = [255, 255, 255, 255];
const t = makeTransform(calib);

/** 以像素空間定義 truth：回傳 x 欄的曲線中心 y。 */
function makePixelCurveImage({ truthY, decoy = null, occlude = null, width = 1200, height = 500 }) {
  const dark = new Set();
  for (let x = 100; x <= 1100; x++) {
    if (occlude && x >= occlude[0] && x <= occlude[1]) continue; // 遮擋帶：曲線缺席
    const yc = Math.round(truthY(x));
    for (let dy = -1; dy <= 1; dy++) dark.add(`${x},${yc + dy}`);
  }
  if (decoy) {
    // decoy：同色、同粗細的水平線段（分岔誘餌），在 x∈[x0,x1]、y=yd，之後 dead-end
    const [x0, x1, yd] = decoy;
    for (let x = x0; x <= x1; x++) for (let dy = -1; dy <= 1; dy++) dark.add(`${x},${yd + dy}`);
  }
  return { width, height, pixelAt: (x, y) => (dark.has(`${x},${y}`) ? BLACK : WHITE) };
}

/** 對照 truth 的最大誤差（dB）與 tail 覆蓋。 */
function analyze(points, truthY, tailFromX) {
  let maxErrDb = 0;
  let tailCount = 0;
  for (const p of points) {
    const { px, py } = t.toPixel(p.freq_hz, p.level_db);
    const err = Math.abs(py - truthY(px)) / 10; // 10 px/dB
    if (err > maxErrDb) maxErrDb = err;
    if (px >= tailFromX) tailCount++;
  }
  return { maxErrDb, tailCount };
}

// ── 場景 1：decoy 分岔 ──
// 真曲線 x<500 平走 y=150（-10dB），x∈[500,530] 陡降 8px/欄至 y=390（-34dB），之後平走。
// decoy：y=150 的水平線從 x=500 延伸到 x=800 後 dead-end。
const forkTruth = (x) => (x < 500 ? 150 : x <= 530 ? 150 + (x - 500) * 8 : 390);
const forkDecoy = [500, 800, 150];

test('greedy locks onto the decoy at a fork (documents the structural failure)', () => {
  const img = makePixelCurveImage({ truthY: forkTruth, decoy: forkDecoy });
  const pts = traceCurve({
    pixelAt: img.pixelAt, width: 1200, height: 500,
    xStart: 100, xEnd: 1100, calib, targetColor: BLACK, tolerance: 60,
    seed: { px: 300, py: 150 },
  });
  const { maxErrDb } = analyze(pts, forkTruth, 810);
  // 分岔當下 decoy（dist 0）勝過陡降的真曲線（dist 8）→ 鎖 decoy 騎到 x=800
  assert.ok(maxErrDb > 5, `greedy 應鎖錯 decoy（got maxErr ${maxErrDb.toFixed(2)} dB）`);
});

test('viterbi recovers the true curve past the decoy (global optimum)', () => {
  const img = makePixelCurveImage({ truthY: forkTruth, decoy: forkDecoy });
  const pts = traceCurve({
    pixelAt: img.pixelAt, width: 1200, height: 500,
    xStart: 100, xEnd: 1100, calib, targetColor: BLACK, tolerance: 60,
    seed: { px: 300, py: 150 }, strategy: 'viterbi',
  });
  const { maxErrDb, tailCount } = analyze(pts, forkTruth, 810);
  // decoy 在 x=800 dead-end：之後 Δy=240 >> maxJump → 該支路徑無法延續 → 全域最優走真曲線
  assert.ok(maxErrDb < 0.5, `viterbi 應貼真曲線（got maxErr ${maxErrDb.toFixed(2)} dB）`);
  assert.ok(tailCount >= 280, `tail 應被覆蓋（got ${tailCount}）`);
  assert.ok(pts.length >= 950, `近乎全數還原（got ${pts.length}）`);
});

// ── 場景 2：遮擋帶 + 曲線在遮擋中持續下降 ──
// 曲線在 [550,700] 以 2px/欄下降（y 150→450，全程在畫布內）；x∈[600,660] 被遮擋（61 欄）
// → 恢復點比遮擋前低 ~124px（>> maxJump=12）。
const occTruth = (x) => (x < 550 ? 150 : x <= 700 ? 150 + (x - 550) * 2 : 450);
const occBand = [600, 660];

test('greedy loses the tail across a long occlusion (documents the failure)', () => {
  const img = makePixelCurveImage({ truthY: occTruth, occlude: occBand });
  const pts = traceCurve({
    pixelAt: img.pixelAt, width: 1200, height: 500,
    xStart: 100, xEnd: 1100, calib, targetColor: BLACK, tolerance: 60,
    seed: { px: 300, py: occTruth(300) },
  });
  const { tailCount } = analyze(pts, occTruth, 670);
  // 凍結的 prevY 與恢復點相差 ~98px > maxJump → 永遠接不回
  assert.ok(tailCount === 0, `greedy 遮擋後應接不回（got tail ${tailCount}）`);
});

test('viterbi reconnects across the occlusion (gap-spread transition)', () => {
  const img = makePixelCurveImage({ truthY: occTruth, occlude: occBand });
  const pts = traceCurve({
    pixelAt: img.pixelAt, width: 1200, height: 500,
    xStart: 100, xEnd: 1100, calib, targetColor: BLACK, tolerance: 60,
    seed: { px: 300, py: occTruth(300) }, strategy: 'viterbi',
  });
  const { maxErrDb, tailCount } = analyze(pts, occTruth, 670);
  assert.ok(tailCount >= 400, `viterbi 應跨遮擋接回 tail（got ${tailCount}）`);
  assert.ok(maxErrDb < 0.5, `全程貼曲線（got ${maxErrDb.toFixed(2)} dB）`);
});

// ── 回歸邊界：viterbi 在既有場景不得劣於 greedy 的語意 ──

// ── 回歸鎖：陡降段的高 run 不得被 maxRunSize 誤殺 ──
// 真實高解析圖踩過：固定 maxRunSize=16 誤殺陡段（每欄 run 高 ≈ stroke/cosθ 可達 20-70px），
// 兩種策略都提前止步、高頻段全缺。自適應預設 max(16, height/8) 後必須能追穿陡段。
// 注意（cross-model verify MED-2）：必須用「連續 rasterize」——逐欄獨立畫 3px 點的
// helper 產不出高 run，測試會假綠、鎖不住回歸。
function makeConnectedCurveImage({ truthY, width = 1200, height = 500 }) {
  const dark = new Set();
  for (let x = 100; x <= 1100; x++) {
    const a = truthY(x - 0.5);
    const b = truthY(x + 0.5);
    const yLo = Math.round(Math.min(a, b)) - 1;
    const yHi = Math.round(Math.max(a, b)) + 1;
    for (let y = yLo; y <= yHi; y++) dark.add(`${x},${y}`);
  }
  return { width, height, pixelAt: (x, y) => (dark.has(`${x},${y}`) ? BLACK : WHITE) };
}

test('steep segments with tall per-column runs are traced (adaptive maxRunSize)', () => {
  // 斜率 20px/欄 的陡降 → 連續 rasterize 後每欄 run 高 ~22px（> 舊固定值 16、< height/8=62）
  const steepTruth = (x) => (x < 500 ? 100 : x <= 515 ? 100 + (x - 500) * 20 : 400);
  const img = makeConnectedCurveImage({ truthY: steepTruth });
  for (const strategy of ['greedy', 'viterbi']) {
    const pts = traceCurve({
      pixelAt: img.pixelAt, width: 1200, height: 500,
      xStart: 100, xEnd: 1100, calib, targetColor: BLACK, tolerance: 60,
      seed: { px: 300, py: 100 }, strategy, maxJump: 25,
    });
    const { maxErrDb, tailCount } = analyze(pts, steepTruth, 520);
    assert.ok(tailCount >= 550, `${strategy}: 陡降後段應被覆蓋（got ${tailCount}）`);
    assert.ok(maxErrDb < 1.5, `${strategy}: 誤差（got ${maxErrDb.toFixed(2)} dB）`);
  }
  // 反向鎖：固定 maxRunSize=16 下 greedy 必須失敗（否則上面只是假綠）。
  // 用 greedy 而非 viterbi 當反向鎖：viterbi 的 lookback 能「橋過」被誤殺的 15 欄
  // 陡段直接恢復 tail（額外的 robustness 加分），greedy 無跳欄能力才會真的斷尾。
  const ptsFixed = traceCurve({
    pixelAt: img.pixelAt, width: 1200, height: 500,
    xStart: 100, xEnd: 1100, calib, targetColor: BLACK, tolerance: 60,
    seed: { px: 300, py: 100 }, strategy: 'greedy', maxJump: 25, maxRunSize: 16,
  });
  const fixed = analyze(ptsFixed, steepTruth, 520);
  assert.ok(fixed.tailCount < 550, `固定 16 下 greedy 應誤殺陡段（got tail ${fixed.tailCount}）——若通過表示回歸鎖失效`);
});

// ── HIGH-1 回歸（cross-model verify）：乾淨的全跨平坦曲線不得被覆蓋率特徵誤旗 ──
test('clean full-span flat curve is NOT flagged as gridline (viterbi full coverage)', () => {
  const flatTruth = () => 250;
  const img = makePixelCurveImage({ truthY: flatTruth });
  const pts = traceCurve({
    pixelAt: img.pixelAt, width: 1200, height: 500,
    xStart: 100, xEnd: 1100, calib, targetColor: BLACK, tolerance: 60,
    seed: { px: 600, py: 250 }, strategy: 'viterbi',
  });
  const { maxErrDb } = analyze(pts, flatTruth, 1101);
  assert.equal(pts.length, 1001, `平坦曲線應全數還原（got ${pts.length}）——只回種子點 = 誤旗成網格線`);
  assert.ok(maxErrDb < 0.3, `誤差（got ${maxErrDb.toFixed(2)} dB）`);
});

// ── HIGH-2 回歸（cross-model verify）：遮擋帶內有同色水平網格線時，路徑要能「跳過」它 ──
test('occlusion containing a same-color gridline: viterbi skips it and reconnects', () => {
  // 真曲線 [550,700] 下降（同場景 2），遮擋帶 [600,660] 內曲線缺席，
  // 但有一條「全跨水平網格線」在 y=260 —— 對遮擋前的曲線（y≈248）reachable（dy≈12），
  // 僅接前一欄的 DP 會被它劫持，之後 dy>maxJump 永遠接不回。
  const dark = new Set();
  for (let x = 100; x <= 1100; x++) {
    if (x >= occBand[0] && x <= occBand[1]) continue;
    const yc = Math.round(occTruth(x));
    for (let dy = -1; dy <= 1; dy++) dark.add(`${x},${yc + dy}`);
  }
  for (let x = 100; x <= 1100; x++) dark.add(`${x},260`); // 全跨網格線（1px，同色）
  const pixelAt = (x, y) => (dark.has(`${x},${y}`) ? BLACK : WHITE);

  const pts = traceCurve({
    pixelAt, width: 1200, height: 500,
    xStart: 100, xEnd: 1100, calib, targetColor: BLACK, tolerance: 60,
    seed: { px: 300, py: occTruth(300) }, strategy: 'viterbi',
  });
  // 濾掉網格線本身的點再對曲線 truth 驗證
  const { tailCount } = analyze(pts, occTruth, 670);
  assert.ok(tailCount >= 400, `viterbi 應跳過帶內網格線、接回 tail（got ${tailCount}）`);
});

// ── LOW 回歸：未知 strategy 不得靜默退回 greedy ──
test('unknown strategy throws (no silent fallback)', () => {
  const img = makePixelCurveImage({ truthY: forkTruth });
  assert.throws(() => traceCurve({
    pixelAt: img.pixelAt, width: 1200, height: 500,
    xStart: 100, xEnd: 1100, calib, targetColor: BLACK, tolerance: 60,
    seed: { px: 300, py: 150 }, strategy: 'vitterbi', // 拼字錯誤
  }));
});

test('viterbi honest refusals match greedy (seed off-curve → empty)', () => {
  const img = makePixelCurveImage({ truthY: forkTruth });
  const pts = traceCurve({
    pixelAt: img.pixelAt, width: 1200, height: 500,
    xStart: 100, xEnd: 1100, calib, targetColor: BLACK, tolerance: 60,
    seed: { px: 600, py: 480 }, strategy: 'viterbi', // 空白處
  });
  assert.equal(pts.length, 0);
});

test('viterbi on clean image matches truth (no regression vs greedy quality)', () => {
  const curveFn = (x) => 250 + 100 * Math.sin((x - 100) / 150);
  const img = makePixelCurveImage({ truthY: curveFn });
  const pts = traceCurve({
    pixelAt: img.pixelAt, width: 1200, height: 500,
    xStart: 100, xEnd: 1100, calib, targetColor: BLACK, tolerance: 60,
    seed: { px: 600, py: Math.round(curveFn(600)) }, strategy: 'viterbi',
  });
  const { maxErrDb } = analyze(pts, curveFn, 1101);
  assert.equal(pts.length, 1001);
  assert.ok(maxErrDb < 0.4, `乾淨圖應精準（got ${maxErrDb.toFixed(2)} dB）`);
});

// ══ #11 terminal 語意：合法斜率永不淨正 ══

// steep-at-edge：陡段在曲線「右緣」結束、無後續水平 tail 補償成本。
// 舊尺度（W_SMOOTH 固定 0.03）下 dy≈maxJump 的合法步進淨成本為正 → 全域最小截短邊緣段。
test('steep segment at the curve edge is fully traced (no tail to compensate)', () => {
  // 曲線只到 x=690：平走後在 [660,690] 以 10px/欄陡降（y 150→450，全程畫布內），
  // 然後結束（右邊空白、無補償 tail）。注意 truth 不得超出畫布（前一版 truth 到 x=700
  // 時 y=550 > height——引擎正確追到畫布邊緣被誤判為截短，測試設計錯）。
  const edgeTruth = (x) => (x < 660 ? 150 : 150 + (x - 660) * 10);
  const dark = new Set();
  for (let x = 100; x <= 690; x++) {
    const yc = Math.round(edgeTruth(x));
    for (let dy = -1; dy <= 1; dy++) dark.add(`${x},${yc + dy}`);
  }
  const pixelAt = (x, y) => (dark.has(`${x},${y}`) ? BLACK : WHITE);
  const pts = traceCurve({
    pixelAt, width: 1200, height: 500,
    xStart: 100, xEnd: 1100, calib, targetColor: BLACK, tolerance: 60,
    seed: { px: 300, py: 150 }, strategy: 'viterbi', maxJump: 12,
  });
  const { maxErrDb, tailCount } = analyze(pts, edgeTruth, 680);
  // 陡降最後 10 欄（x 680-690）必須被覆蓋——它們是曲線的真實終點
  assert.ok(tailCount >= 10, `邊緣陡段應被完整追到（got tail ${tailCount}）`);
  assert.ok(maxErrDb < 0.5, `誤差（got ${maxErrDb.toFixed(2)} dB）`);
});

// 長 decoy × 短 truth tail：decoy 平滑優勢大、真曲線 dead-end 後補償短。
// Codex 指出 dead-end 剪枝與成本尺度耦合——此測試顯式鎖住。
test('long decoy vs short truth tail: documented Level-1 ambiguity + seed-placement resolution', () => {
  // 真曲線：平走到 500 → [500,530] 陡降 8px/欄 → 平走只到 640（短 tail ~110 欄）
  const shortTruth = (x) => (x < 500 ? 150 : x <= 530 ? 150 + (x - 500) * 8 : 390);
  const dark = new Set();
  for (let x = 100; x <= 640; x++) {
    const yc = Math.round(shortTruth(x));
    for (let dy = -1; dy <= 1; dy++) dark.add(`${x},${yc + dy}`);
  }
  // decoy：y=150 水平線 500→1050（很長，551 欄）後 dead-end
  for (let x = 500; x <= 1050; x++) for (let dy = -1; dy <= 1; dy++) dark.add(`${x},${150 + dy}`);
  const pixelAt = (x, y) => (dark.has(`${x},${y}`) ? BLACK : WHITE);
  const pts = traceCurve({
    pixelAt, width: 1200, height: 500,
    xStart: 100, xEnd: 1100, calib, targetColor: BLACK, tolerance: 60,
    seed: { px: 300, py: 150 }, strategy: 'viterbi', maxJump: 12,
  });
  // Documented boundary（#11）：兩支同寬同色、都 dead-end——decoy 較長（551 欄）、
  // 真曲線較短（陡降 + 110 欄 tail）。Level-1 沒有任何手寫特徵能區分「哪支才是曲線」
  // （寬度同、顏色同、都平滑、都非全跨網格），選累積獎勵較多的長支是**合理**歧義判定，
  // 不是缺陷。破歧義屬 Level-2（線型/語義）或 user 種子位置（點在分岔後的目標支上）。
  const { maxErrDb } = analyze(pts, shortTruth, 540);
  assert.ok(maxErrDb > 5, `目前語意：長支勝出（documented；got ${maxErrDb.toFixed(2)} dB）`);
  // 注意：種子在目標支上「不」足以破歧義——種子只是必經點，管不住曲線終點後
  // lookback 跳上遠處長線（dy ≤ maxJump×dx 隨 dx 線性放寬）。實務破法：
  // (a) --x-range 限掃描範圍到曲線實際跨度（CLI 既有）；
  // (b) 真實圖中這類長直線 = 軸線/網格 → 已被橫向覆蓋率旗標懲罰，場景本身罕見。
  const pts2 = traceCurve({
    pixelAt, width: 1200, height: 500,
    xStart: 100, xEnd: 640, calib, targetColor: BLACK, tolerance: 60,
    seed: { px: 600, py: Math.round(shortTruth(600)) }, strategy: 'viterbi', maxJump: 12,
  });
  const r2 = analyze(pts2, shortTruth, 540);
  assert.ok(r2.maxErrDb < 0.5, `x-range 限界後正確（got ${r2.maxErrDb.toFixed(2)} dB）`);
});
