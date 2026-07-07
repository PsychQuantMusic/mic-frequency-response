// 頻響圖座標轉換
// X 軸對數（頻率 Hz），Y 軸線性（dB）。畫面像素 px 向右增、py 向下增。
// 校準用兩個已知頻率點（x 軸）+ 兩個已知 dB 點（y 軸）定出斜率。

/**
 * @param {{freq:{p1:{px:number,value:number},p2:{px:number,value:number}},
 *          db:{p1:{py:number,value:number},p2:{py:number,value:number}}}} calib
 * @returns {{toData:(px:number,py:number)=>{freq_hz:number,level_db:number},
 *            toPixel:(freq_hz:number,level_db:number)=>{px:number,py:number}}}
 */
export function makeTransform(calib) {
  const { freq, db } = calib;

  const lf1 = Math.log10(freq.p1.value);
  const lf2 = Math.log10(freq.p2.value);
  const fpx1 = freq.p1.px;
  const fpx2 = freq.p2.px;

  const dv1 = db.p1.value;
  const dv2 = db.p2.value;
  const dpy1 = db.p1.py;
  const dpy2 = db.p2.py;

  // 防呆：每軸兩個校準點不能重合，否則斜率為 0/0。
  if (fpx2 === fpx1) throw new Error('frequency calibration points share the same px');
  if (dpy2 === dpy1) throw new Error('dB calibration points share the same py');

  const freqSlope = (lf2 - lf1) / (fpx2 - fpx1); // log10(freq) per px
  const dbSlope = (dv2 - dv1) / (dpy2 - dpy1); // dB per py

  return {
    toData(px, py) {
      const lf = lf1 + freqSlope * (px - fpx1);
      return {
        freq_hz: 10 ** lf,
        level_db: dv1 + dbSlope * (py - dpy1),
      };
    },
    toPixel(freq_hz, level_db) {
      return {
        px: fpx1 + (Math.log10(freq_hz) - lf1) / freqSlope,
        py: dpy1 + (level_db - dv1) / dbSlope,
      };
    },
  };
}
