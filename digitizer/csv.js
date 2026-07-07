// CSV 匯出：把追蹤點輸出成 data/ 的 source-of-truth 格式 `freq_hz,level_db`。
// 假設 points 已是頻率遞增（traceCurve 保證）。

function fmt(n, digits) {
  // 四捨五入到指定小數位，再去除多餘尾零（20.000 -> "20"，200.50 -> "200.5"）
  return Number(n.toFixed(digits)).toString();
}

/**
 * @param {{freq_hz:number, level_db:number}[]} points
 * @param {{freqDigits?:number, dbDigits?:number}} [opts]
 * @returns {string} CSV 文字（含 header，尾端換行）
 */
export function toCSV(points, opts = {}) {
  const freqDigits = opts.freqDigits ?? 3;
  const dbDigits = opts.dbDigits ?? 2;
  const lines = ['freq_hz,level_db'];
  for (const p of points) {
    lines.push(`${fmt(p.freq_hz, freqDigits)},${fmt(p.level_db, dbDigits)}`);
  }
  return lines.join('\n') + '\n';
}
