// 像素追蹤引擎（與 canvas 解耦）
//
// 沿 X 逐像素欄掃描，找該欄匹配 targetColor 的 y 像素（取質心），用 coords 反算成 (Hz, dB)。
// 關鍵：引擎吃抽象的 pixelAt(x, y) -> [r, g, b, a] accessor，不直接依賴 canvas。
//   - 瀏覽器：以 canvas ImageData 包成 pixelAt
//   - 測試：以程式生成的合成圖 pixel 包成 pixelAt（headless 回歸測試）

import { makeTransform } from './coords.js';

/** RGB 歐氏距離（忽略 alpha）。 */
export function colorDistance(c1, c2) {
  const dr = c1[0] - c2[0];
  const dg = c1[1] - c2[1];
  const db = c1[2] - c2[2];
  return Math.sqrt(dr * dr + dg * dg + db * db);
}

/**
 * 掃描一欄，把匹配 targetColor 的像素分組成連續 run。
 * @returns {{y0:number,y1:number,center:number,size:number}[]}
 */
function matchingRuns({ pixelAt, x, height, targetColor, tolerance }) {
  const runs = [];
  let start = -1;
  for (let y = 0; y < height; y++) {
    const c = pixelAt(x, y);
    const match = c[3] >= 128 && colorDistance(c, targetColor) <= tolerance;
    if (match && start < 0) start = y;
    if (!match && start >= 0) {
      runs.push({ y0: start, y1: y - 1, center: (start + y - 1) / 2, size: y - start });
      start = -1;
    }
  }
  if (start >= 0) runs.push({ y0: start, y1: height - 1, center: (start + height - 1) / 2, size: height - start });
  return runs;
}

/**
 * 追蹤一條曲線。
 * @param {object} opts
 * @param {(x:number,y:number)=>number[]} opts.pixelAt  (x,y) -> [r,g,b,a]
 * @param {number} opts.width
 * @param {number} opts.height
 * @param {number} [opts.xStart]  掃描起始欄（含），預設 0
 * @param {number} [opts.xEnd]    掃描結束欄（含），預設 width-1
 * @param {object} opts.calib     coords.makeTransform 的校準物件
 * @param {number[]} opts.targetColor  曲線顏色 [r,g,b,a]
 * @param {number} opts.tolerance  顏色距離容差
 * @param {{px:number,py:number}} [opts.seed]  種子點（取色點）。給定時走「連續性追蹤」：
 *   從種子逐欄向兩側走，每欄只取「中心最接近上一欄 y」的 run —— 同色網格線
 *   （水平線離曲線遠、垂直線巨型 run 被 maxRunSize 明確拒絕）（#3）。
 *   省略時退回全欄質心（乾淨/合成圖適用；真實網格圖會被污染）。
 * @param {number} [opts.maxJump]  連續性容差 px/欄（預設 12；陡峭曲線可加大）
 * @param {number} [opts.maxRunSize]  run 高度上限 px（預設 max(16, height/8) 自適應）。
 *   超過視為垂直網格線/色塊污染，**明確拒絕**（不能只靠 maxJump 距離——巨型 run 的
 *   中心可能恰好落在曲線附近；cluster verify HIGH finding）。固定小值會誤殺高解析圖
 *   陡降段的高 run（實測回歸），故依圖高自適應；特殊圖可顯式覆蓋。
 * @param {'greedy'|'viterbi'} [opts.strategy]  種子模式演算法（預設 greedy 向後相容）。
 *   'viterbi'（#9）：全域最優路徑 —— greedy 逐欄局部決策在「decoy 分岔」與「長遮擋後
 *   遠處恢復」會結構性跟丟；Viterbi 最小化整條路徑總成本，一次求全域最優。
 * @returns {{freq_hz:number, level_db:number}[]}  頻率遞增；無匹配的欄跳過（誠實 gap）。
 *   種子點不在任何合格 run 附近（±maxJump）→ 回空陣列（誠實拒絕，UI 應提示重新取色）。
 */
export function traceCurve({ pixelAt, width, height, xStart, xEnd, calib, targetColor, tolerance, seed, maxJump = 12, maxRunSize = null, strategy = 'greedy' }) {
  // maxRunSize 自適應預設：max(16, height/8)。固定 16 在真實高解析圖的「陡降段」誤殺
  // 曲線自己的 run（每欄 run 高 ≈ stroke/cosθ，400dpi 陡段可達 20–70px）——實測 SM58
  // 高頻段整段被拒、tracer 提前止步。垂直網格線 run 是「整欄高」（≈ chart height），
  // 與陡段 run 天差地遠，height/8 足以分開兩者。
  maxRunSize = maxRunSize ?? Math.max(16, Math.floor(height / 8));
  if (seed && strategy === 'viterbi') {
    return traceFromSeedViterbi({ pixelAt, width, height, xStart, xEnd, calib, targetColor, tolerance, seed, maxJump, maxRunSize });
  }
  if (seed) {
    return traceFromSeed({ pixelAt, width, height, xStart, xEnd, calib, targetColor, tolerance, seed, maxJump, maxRunSize });
  }
  const t = makeTransform(calib);
  // 邊界防呆：正規化（xStart > xEnd 時互換）+ clamp 到 [0, width-1] 並整數化
  const lo = Math.round(Math.min(xStart ?? 0, xEnd ?? width - 1));
  const hi = Math.round(Math.max(xStart ?? 0, xEnd ?? width - 1));
  const x0 = Math.max(0, lo);
  const x1 = Math.min(width - 1, hi);
  const points = [];

  for (let x = x0; x <= x1; x++) {
    // 該欄所有匹配 targetColor 的 y 取質心（曲線有粗細 / 抗鋸齒時取中心，比取最上/最下穩健）
    let sumY = 0;
    let count = 0;
    for (let y = 0; y < height; y++) {
      const c = pixelAt(x, y);
      if (c[3] < 128) continue; // 透明像素不計入（避免透明區誤 match 不透明曲線色）
      if (colorDistance(c, targetColor) <= tolerance) {
        sumY += y;
        count++;
      }
    }
    if (count === 0) continue; // 斷點：該欄無目標色 → 跳過不輸出，不強行內插
    const d = t.toData(x, sumY / count);
    points.push({ freq_hz: d.freq_hz, level_db: d.level_db });
  }

  // 保證頻率遞增（與 JSDoc / csv.js 的假設一致；即使 calib 左右相反也成立）
  points.sort((a, b) => a.freq_hz - b.freq_hz);
  return points;
}

/** 種子連續性追蹤（#3）：從種子欄向兩側走，每欄取最接近上一欄 y 的 run。 */
function traceFromSeed({ pixelAt, width, height, xStart, xEnd, calib, targetColor, tolerance, seed, maxJump, maxRunSize }) {
  const t = makeTransform(calib);
  const lo = Math.round(Math.min(xStart ?? 0, xEnd ?? width - 1));
  const hi = Math.round(Math.max(xStart ?? 0, xEnd ?? width - 1));
  const x0 = Math.max(0, lo);
  const x1 = Math.min(width - 1, hi);
  const seedX = Math.max(x0, Math.min(x1, Math.round(seed.px)));

  // 種子欄：只在「合格 run」（非巨型）中取最接近種子 y 者當錨點。
  // 距離門檻：種子若不在任何合格 run 的 ±maxJump 內（點在網格線/文字/空白處），
  // 誠實回空 —— 錨定錯誤的 run 會沿錯誤的線追到底。
  const seedRuns = matchingRuns({ pixelAt, x: seedX, height, targetColor, tolerance })
    .filter((r) => r.size <= maxRunSize);
  if (seedRuns.length === 0) return [];
  const anchor = seedRuns.reduce((best, r) =>
    Math.abs(r.center - seed.py) < Math.abs(best.center - seed.py) ? r : best);
  if (Math.abs(anchor.center - seed.py) > maxJump) return [];

  const points = [];
  const emit = (x, y) => {
    const d = t.toData(x, y);
    points.push({ freq_hz: d.freq_hz, level_db: d.level_db });
  };

  // 向一側逐欄走：每欄在 maxJump 內的 runs 中取「score = 距離 − 寬度加分」最小者。
  // 寬度加分（曲線 stroke 通常 ≥2px、網格線 ~1px）解決曲線與水平網格線相切分離時
  // 「線比曲線更近 prevY」的鎖線問題 —— 與 overlay_verify 的最寬暗 run 同一洞察。
  // 無候選（垂直網格線整欄融成巨 run 離 prevY 遠、或曲線斷點）→ 誠實跳過該欄，
  // prevY 凍結，曲線在 maxJump 內恢復時繼續接上。
  const walk = (from, to, step) => {
    let prevY = anchor.center;
    for (let x = from; step > 0 ? x <= to : x >= to; x += step) {
      const runs = matchingRuns({ pixelAt, x, height, targetColor, tolerance });
      let best = null;
      let bestScore = Infinity;
      for (const r of runs) {
        if (r.size > maxRunSize) continue; // 巨型 run（垂直網格線/色塊）明確拒絕，不看距離
        const dist = Math.abs(r.center - prevY);
        if (dist > maxJump) continue;
        const score = dist - Math.min(r.size, 6);
        if (score < bestScore) { bestScore = score; best = r; }
      }
      if (!best) continue; // 誠實 gap：不輸出、不內插
      emit(x, best.center);
      prevY = best.center;
    }
  };

  emit(seedX, anchor.center);
  walk(seedX + 1, x1, 1);
  walk(seedX - 1, x0, -1);

  points.sort((a, b) => a.freq_hz - b.freq_hz);
  return points;
}

/**
 * Viterbi 全域最優路徑（#9）：greedy 的結構性升級。
 *
 * greedy 逐欄局部決策、無法回頭 → 兩個跟丟場景：
 *   (a) decoy 分岔：分岔當下 decoy 更近 → 鎖錯線騎到 dead-end
 *   (b) 長遮擋：曲線在遮擋中持續移動，恢復點超出凍結 prevY 的 maxJump → 永遠接不回
 *
 * Viterbi 重構：把「追蹤」變成「在所有可能路徑中選整條總成本最小的」：
 *   - 狀態 = 有合格候選 run 的欄 × 該欄的 runs（沿用 maxRunSize 巨型 run 拒絕）
 *   - 觀測成本 = -min(size,6)×W_WIDTH（「曲線比網格粗」加分，同 greedy）
 *   - 轉移成本 = (Δy²/Δx)×W_SMOOTH（平滑先驗；跨 gap 以 Δx 攤平）
 *     + 硬上限 Δy ≤ maxJump×Δx（禁止瞬移 → decoy dead-end 後的大跳被禁，
 *       該支路徑無法延續，全域最優被迫走真曲線）
 *   - 無候選欄 = 天然 gap（垂直網格線欄照舊誠實跳過）；不可達欄（孤立墨點）誠實跳過
 *   - 從種子欄向左右各跑半程 DP（種子 = 必經錨點，語意同 greedy）
 *
 * 理論脈絡：orthogonal-projects academic/curve-extraction-priors —— Level 1
 * 「全域架構 + 手寫先驗（連續平滑）」；同色網格辨識/座標語義仍屬 Level 2（學出的先驗）。
 */
function traceFromSeedViterbi({ pixelAt, width, height, xStart, xEnd, calib, targetColor, tolerance, seed, maxJump, maxRunSize }) {
  const t = makeTransform(calib);
  const lo = Math.round(Math.min(xStart ?? 0, xEnd ?? width - 1));
  const hi = Math.round(Math.max(xStart ?? 0, xEnd ?? width - 1));
  const x0 = Math.max(0, lo);
  const x1 = Math.min(width - 1, hi);
  const seedX = Math.max(x0, Math.min(x1, Math.round(seed.px)));

  const qualifyingRuns = (x) =>
    matchingRuns({ pixelAt, x, height, targetColor, tolerance }).filter((r) => r.size <= maxRunSize);

  // 種子錨定（同 greedy 語意：off-curve 誠實回空）
  const seedRuns = qualifyingRuns(seedX);
  if (seedRuns.length === 0) return [];
  const anchor = seedRuns.reduce((best, r) =>
    Math.abs(r.center - seed.py) < Math.abs(best.center - seed.py) ? r : best);
  if (Math.abs(anchor.center - seed.py) > maxJump) return [];

  const W_SMOOTH = 0.03; // 平滑先驗強度：夾在「陡降段（~8px/欄）淨成本仍為負、可延伸」與「跨 gap 重接可負擔」之間；decoy 防護不靠它（靠 maxJump 硬限的 dead-end 剪枝）
  const W_WIDTH = 1;    // run 寬度加分權重
  const W_GRIDLINE = 8; // 「純網格線 run」每欄罰值：> 寬度獎勵上限(6) → 純線 run 淨成本必為正，杜絕「騎線多活幾欄」在全域最小下勝出

  // 水平網格線列偵測（一次預掃）—— Level-1.5 手寫特徵（curve-extraction-priors OQ-1）：
  // 同色網格與曲線在「暗度/寬度」上可能完全不可分（粗網格線 ≥ 曲線 stroke 時寬度
  // 獎勵飽和同價），但「橫向覆蓋率」可分：曲線只在少數欄經過某 y，網格線橫貫全圖。
  // 真實 SM58 圖實測踩到：曲線峰頂與 +5dB 粗網格線相切後，騎線到圖緣是全域最優
  // （零轉移成本 + 多活 56 欄）→ 整條尾段 11dB 錯。
  const gridRows = new Set();
  {
    const spanCount = Math.max(1, Math.floor((x1 - x0) / 2) + 1);
    for (let y = 0; y < height; y++) {
      let dark = 0;
      for (let x = x0; x <= x1; x += 2) {
        const c = pixelAt(x, y);
        if (c[3] >= 128 && colorDistance(c, targetColor) <= tolerance) dark++;
      }
      if (dark / spanCount > 0.85) gridRows.add(y); // 真網格線是精確直線（覆蓋≈100%）；真曲線只要有傾斜/起伏，單列覆蓋遠低於此（平坦段誤旗實測踩過：0.6 門檻連「平坦曲線+同高 decoy」都會誤殺）
    }
  }
  // 只罰「純網格線 run」（整段 y 都落在網格線列上）。曲線經過/貼合網格線時，
  // 合併 run 含網格線列以外的 y → 不罰（歧義處給無罪推定，交給平滑先驗裁決）。
  const isPureGridRun = (r) => {
    if (gridRows.size === 0) return false;
    for (let y = Math.round(r.y0); y <= Math.round(r.y1); y++) {
      if (!gridRows.has(y)) return false;
    }
    return true;
  };
  const emission = (r) => -Math.min(r.size, 6) * W_WIDTH + (isPureGridRun(r) ? W_GRIDLINE : 0);

  /** 半程 DP：種子欄往 step 方向到 xLimit，回傳最優路徑 [{x,y}]（含種子欄）。 */
  const half = (step, xLimit) => {
    let prev = { x: seedX, runs: [anchor], cost: [0], parent: [-1] };
    const chain = [prev];
    // 終點選「全域最小成本狀態」，不強制走到 xLimit ——
    // 掃描範圍常比曲線寬（曲線在 chart 邊緣前結束）；若強制以最遠可達欄當終點，
    // 唯一能到那裡的是水平網格線 → 整條尾段被迫騎網格線（真實 SM58 圖實測踩到：
    // 曲線峰頂與 +5dB 線相切，回溯被拉去騎線到右緣）。emission 為負（獎勵），
    // 曲線（寬 run 獎勵密）延伸就會繼續變便宜、細網格線划不來 → 全域最小自然
    // 停在「曲線真正結束」處，語意同 greedy 的誠實止步。
    let best = { k: 0, j: 0, cost: 0 };
    for (let x = seedX + step; step > 0 ? x <= xLimit : x >= xLimit; x += step) {
      const runs = qualifyingRuns(x);
      if (runs.length === 0) continue; // 無候選欄 = 天然 gap
      const dx = Math.abs(x - prev.x);
      const cost = new Array(runs.length).fill(Infinity);
      const parent = new Array(runs.length).fill(-1);
      for (let j = 0; j < runs.length; j++) {
        for (let i = 0; i < prev.runs.length; i++) {
          if (prev.cost[i] === Infinity) continue;
          const dy = Math.abs(runs[j].center - prev.runs[i].center);
          if (dy > maxJump * dx) continue; // 硬上限：gap 越長容許位移越大，但不許瞬移
          const c = prev.cost[i] + ((dy * dy) / dx) * W_SMOOTH + emission(runs[j]);
          if (c < cost[j]) { cost[j] = c; parent[j] = i; }
        }
      }
      if (cost.every((c) => c === Infinity)) continue; // 不可達欄（孤立墨點）→ 誠實跳過
      const node = { x, runs, cost, parent };
      chain.push(node);
      prev = node;
      for (let j = 0; j < cost.length; j++) {
        if (cost[j] < best.cost) best = { k: chain.length - 1, j, cost: cost[j] };
      }
    }
    // 回溯：從全域最小成本狀態（而非最後可達欄）
    const pts = [];
    let idx = best.j;
    for (let k = best.k; k >= 0; k--) {
      pts.push({ x: chain[k].x, y: chain[k].runs[idx].center });
      idx = chain[k].parent[idx];
    }
    return pts;
  };

  const points = [];
  const emitPt = ({ x, y }) => {
    const d = t.toData(x, y);
    points.push({ freq_hz: d.freq_hz, level_db: d.level_db });
  };
  half(1, x1).forEach(emitPt);
  half(-1, x0).filter((p) => p.x !== seedX).forEach(emitPt); // 種子欄去重

  points.sort((a, b) => a.freq_hz - b.freq_hz);
  return points;
}
