// Polar pattern（指向性圖）座標轉換（#5）
//
// 與 coords.js（直角 log-X/linear-Y）平行的極座標版本：
//   資料座標 = (angle_deg, level_db)，angle 0° = on-axis
//   畫面座標 = (px, py)
// 校準 = 圓心 + 兩個已知 dB 的圈半徑（線性半徑刻度，廠商圖通常 5dB/格等距）
//   + 0° 的畫面方向（screen deg：0=上、90=右、180=下、270=左）+ 旋轉方向。

/**
 * @param {{center:{px:number,py:number},
 *          rings:[{r:number,db:number},{r:number,db:number}],
 *          zeroAngleScreenDeg:number, clockwise:boolean}} calib
 */
export function makePolarTransform(calib) {
  const { center, rings, zeroAngleScreenDeg, clockwise } = calib;
  const [a, b] = rings;
  if (a.r === b.r) throw new Error('calibration rings share the same radius');
  if (a.db === b.db) throw new Error('calibration rings share the same dB');
  const slope = (b.db - a.db) / (b.r - a.r); // dB per px（通常負：往圓心 dB 降）

  const toRad = (d) => (d * Math.PI) / 180;

  return {
    toPixel(angle_deg, level_db) {
      const r = a.r + (level_db - a.db) / slope;
      const screen = zeroAngleScreenDeg + (clockwise ? angle_deg : -angle_deg);
      const rad = toRad(screen);
      // screen deg: 0=上 → (sin, -cos) 得畫面向量（py 向下增）
      return { px: center.px + r * Math.sin(rad), py: center.py - r * Math.cos(rad) };
    },
    toData(px, py) {
      const dx = px - center.px;
      const dy = py - center.py;
      const r = Math.hypot(dx, dy);
      let screen = (Math.atan2(dx, -dy) * 180) / Math.PI; // 0=上、順時針
      let angle = clockwise ? screen - zeroAngleScreenDeg : zeroAngleScreenDeg - screen;
      angle = ((angle % 360) + 360) % 360;
      return { angle_deg: angle, level_db: a.db + (r - a.r) * slope };
    },
  };
}
