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
 * @returns {{freq_hz:number, level_db:number}[]}  頻率遞增；無匹配的欄跳過（誠實 gap）
 */
export function traceCurve({ pixelAt, width, height, xStart, xEnd, calib, targetColor, tolerance }) {
  const t = makeTransform(calib);
  const x0 = xStart ?? 0;
  const x1 = xEnd ?? width - 1;
  const points = [];

  for (let x = x0; x <= x1; x++) {
    // 該欄所有匹配 targetColor 的 y 取質心（曲線有粗細 / 抗鋸齒時取中心，比取最上/最下穩健）
    let sumY = 0;
    let count = 0;
    for (let y = 0; y < height; y++) {
      if (colorDistance(pixelAt(x, y), targetColor) <= tolerance) {
        sumY += y;
        count++;
      }
    }
    if (count === 0) continue; // 斷點：該欄無目標色 → 跳過不輸出，不強行內插
    const d = t.toData(x, sumY / count);
    points.push({ freq_hz: d.freq_hz, level_db: d.level_db });
  }

  return points;
}
