// UI 殼層：載圖 → 4 點校準 → 取色 → 追蹤 → 疊示 → 匯出 CSV。
// 所有數學在 coords/trace/csv（已單元測試）；此檔只做 canvas/DOM 膠水。

import { makeTransform } from './coords.js';
import { traceCurve } from './trace.js';
import { toCSV } from './csv.js';

const $ = (id) => document.getElementById(id);
const canvas = $('canvas');
const ctx = canvas.getContext('2d', { willReadFrequently: true });

let baseImageData = null; // 原圖像素，疊示前用來還原畫面
let mode = 'idle'; // idle | x1 | x2 | y1 | y2 | color
let targetColor = null; // [r,g,b,a]
let seed = null; // 取色點同時是連續性追蹤的種子（顏色 + 曲線位置，#3）
let traced = [];

// 校準點：x1/x2 存 {px}, y1/y2 存 {py}；value 從 input 讀
const calib = { x1: {}, x2: {}, y1: {}, y2: {} };

const setStatus = (m) => { $('status').textContent = m; };

// ---- pixel accessor（把 canvas ImageData 包成 trace 引擎吃的 pixelAt）----
function pixelAt(x, y) {
  const d = baseImageData.data;
  const i = (y * canvas.width + x) * 4;
  return [d[i], d[i + 1], d[i + 2], d[i + 3]];
}

function canvasXY(e) {
  const rect = canvas.getBoundingClientRect();
  const x = Math.floor((e.clientX - rect.left) * (canvas.width / rect.width));
  const y = Math.floor((e.clientY - rect.top) * (canvas.height / rect.height));
  return { x: Math.max(0, Math.min(canvas.width - 1, x)), y: Math.max(0, Math.min(canvas.height - 1, y)) };
}

// ---- 載入圖片 ----
$('file').addEventListener('change', (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const img = new Image();
  img.onload = () => {
    canvas.width = img.naturalWidth;
    canvas.height = img.naturalHeight;
    ctx.drawImage(img, 0, 0);
    baseImageData = ctx.getImageData(0, 0, canvas.width, canvas.height);
    traced = [];
    $('export').disabled = true;
    $('count').textContent = '尚未追蹤';
    setStatus(`已載入 ${canvas.width}×${canvas.height}。填入校準值後逐一「在圖上點」。`);
    URL.revokeObjectURL(img.src); // 已 decode 到 canvas，釋放 object URL
  };
  img.onerror = () => { URL.revokeObjectURL(img.src); setStatus('圖片載入失敗。'); };
  img.src = URL.createObjectURL(file);
});

// ---- 校準 / 取色的 pick 按鈕 ----
const pickButtons = { x1: 'x1pick', x2: 'x2pick', y1: 'y1pick', y2: 'y2pick', color: 'colorpick' };
for (const [m, btnId] of Object.entries(pickButtons)) {
  $(btnId).addEventListener('click', () => {
    if (!baseImageData) { setStatus('先載入圖片。'); return; }
    mode = m;
    for (const b of Object.values(pickButtons)) $(b).classList.remove('active');
    $(btnId).classList.add('active');
    setStatus(m === 'color' ? '在曲線上點一下取色。' : `在圖上點 ${m.toUpperCase()} 的位置。`);
  });
}

// ---- canvas 點擊：依 mode 派工 ----
canvas.addEventListener('click', (e) => {
  if (!baseImageData || mode === 'idle') return;
  const { x, y } = canvasXY(e);
  if (mode === 'x1' || mode === 'x2') {
    calib[mode].px = x;
    $(mode + 'dot').classList.add('ok');
    $(mode + 'dot').textContent = '✓';
  } else if (mode === 'y1' || mode === 'y2') {
    calib[mode].py = y;
    $(mode + 'dot').classList.add('ok');
    $(mode + 'dot').textContent = '✓';
  } else if (mode === 'color') {
    targetColor = pixelAt(x, y);
    seed = { px: x, py: y }; // 點在曲線上 → 這也是追蹤的起點
    $('swatch').style.background = `rgb(${targetColor[0]},${targetColor[1]},${targetColor[2]})`;
    setStatus(`取色 rgb(${targetColor.slice(0, 3).join(',')})（此點同時作為追蹤種子）`);
  }
  $(pickButtons[mode]).classList.remove('active');
  mode = 'idle';
});

$('tol').addEventListener('input', (e) => { $('tolval').textContent = e.target.value; });

// ---- 追蹤 ----
$('trace').addEventListener('click', () => {
  if (!baseImageData) { setStatus('先載入圖片。'); return; }
  const x1v = parseFloat($('x1v').value), x2v = parseFloat($('x2v').value);
  const y1v = parseFloat($('y1v').value), y2v = parseFloat($('y2v').value);
  if ([calib.x1.px, calib.x2.px, calib.y1.py, calib.y2.py].some((v) => v == null)) {
    setStatus('4 個校準點都要在圖上點過。'); return;
  }
  if ([x1v, x2v, y1v, y2v].some((v) => !Number.isFinite(v))) {
    setStatus('4 個校準值（頻率 / dB）都要填有效數字。'); return;
  }
  if (x1v <= 0 || x2v <= 0) { setStatus('校準頻率必須大於 0。'); return; }
  if (x1v === x2v) { setStatus('兩個 X 校準頻率不能相同。'); return; }
  if (y1v === y2v) { setStatus('兩個 Y 校準 dB 不能相同。'); return; }
  if (!targetColor) { setStatus('先「點曲線取色」。'); return; }

  const transformCalib = {
    freq: { p1: { px: calib.x1.px, value: x1v }, p2: { px: calib.x2.px, value: x2v } },
    db: { p1: { py: calib.y1.py, value: y1v }, p2: { py: calib.y2.py, value: y2v } },
  };

  let points;
  try {
    // 只掃兩個 X 校準點之間（圖框內的頻率範圍），避免框外座標軸標籤同色被誤抓
    const xStart = Math.min(calib.x1.px, calib.x2.px);
    const xEnd = Math.max(calib.x1.px, calib.x2.px);
    points = traceCurve({
      pixelAt, width: canvas.width, height: canvas.height,
      xStart, xEnd, calib: transformCalib, targetColor,
      tolerance: parseFloat($('tol').value),
      seed, // 種子追蹤：同色網格圖也能追（#3）
      strategy: 'viterbi', // 全域最優路徑：decoy 分岔/長遮擋也不跟丟（#9）
    });
  } catch (err) {
    setStatus('追蹤失敗：' + err.message); return;
  }

  // 依頻率遞增排序（px 可能左右相反）
  points.sort((a, b) => a.freq_hz - b.freq_hz);
  traced = points;

  // 疊示：還原原圖 + 畫追蹤點
  ctx.putImageData(baseImageData, 0, 0);
  const t = makeTransform(transformCalib);
  ctx.fillStyle = 'rgba(0,180,90,.9)';
  for (const p of points) {
    const { px, py } = t.toPixel(p.freq_hz, p.level_db);
    ctx.fillRect(px - 1, py - 1, 2, 2);
  }

  $('export').disabled = points.length === 0;
  const span = points.length ? `（${points[0].freq_hz.toFixed(0)}–${points.at(-1).freq_hz.toFixed(0)} Hz）` : '';
  $('count').textContent = `${points.length} 點 ${span}`;
  setStatus(`追蹤完成：${points.length} 點。綠點為還原結果，檢查貼合度後匯出。`);
});

// ---- 匯出 CSV ----
$('export').addEventListener('click', () => {
  if (!traced.length) return;
  const blob = new Blob([toCSV(traced)], { type: 'text/csv' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'frequency-response--curve.csv';
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 0);
});
