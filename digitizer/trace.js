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
 *   （水平線離曲線遠、垂直線整欄融成巨大 run）自然被排除（#3）。
 *   省略時退回全欄質心（乾淨/合成圖適用；真實網格圖會被污染）。
 * @param {number} [opts.maxJump]  連續性容差 px/欄（預設 12；陡峭曲線可加大）
 * @returns {{freq_hz:number, level_db:number}[]}  頻率遞增；無匹配的欄跳過（誠實 gap）
 */
export function traceCurve({ pixelAt, width, height, xStart, xEnd, calib, targetColor, tolerance, seed, maxJump = 12 }) {
  if (seed) {
    return traceFromSeed({ pixelAt, width, height, xStart, xEnd, calib, targetColor, tolerance, seed, maxJump });
  }
  const t = makeTransform(calib);
  // 邊界防呆：clamp 到 [0, width-1] 並整數化（decoupled core 不假設 caller 已 clamp）
  const x0 = Math.max(0, Math.round(xStart ?? 0));
  const x1 = Math.min(width - 1, Math.round(xEnd ?? width - 1));
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
function traceFromSeed({ pixelAt, width, height, xStart, xEnd, calib, targetColor, tolerance, seed, maxJump }) {
  const t = makeTransform(calib);
  const x0 = Math.max(0, Math.round(xStart ?? 0));
  const x1 = Math.min(width - 1, Math.round(xEnd ?? width - 1));
  const seedX = Math.max(x0, Math.min(x1, Math.round(seed.px)));

  // 種子欄：取「包含或最接近種子 y」的 run 當錨點
  const seedRuns = matchingRuns({ pixelAt, x: seedX, height, targetColor, tolerance });
  if (seedRuns.length === 0) return [];
  const anchor = seedRuns.reduce((best, r) =>
    Math.abs(r.center - seed.py) < Math.abs(best.center - seed.py) ? r : best);

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
