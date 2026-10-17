/* Kamailio NOC dashboard — vanilla JS, sin dependencias (CSP script-src 'self') */
const $ = id => document.getElementById(id);
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
function showErr(m) {
  let b = $('errbadge');
  if (!b) {
    b = document.createElement('div');
    b.id = 'errbadge';
    b.style.cssText = 'position:fixed;bottom:10px;right:10px;z-index:99;max-width:70vw;background:#2a0d0d;border:1px solid #d03b3b;color:#fecaca;font:12px system-ui;padding:8px 12px;border-radius:6px';
    document.body.appendChild(b);
  }
  b.textContent = 'JS: ' + m;
}
window.addEventListener('error', e => showErr(e.message || 'error desconocido'));
const api = p => fetch(p).then(r => {
  if (r.status === 401) { location.href = 'login'; throw new Error('login'); }
  if (!r.ok) throw new Error(r.status);
  return r.json();
});

// ---------------- formato ----------------
const nf0 = new Intl.NumberFormat('es-PE', { maximumFractionDigits: 0 });
const nf1 = new Intl.NumberFormat('es-PE', { minimumFractionDigits: 1, maximumFractionDigits: 1 });
const nf2 = new Intl.NumberFormat('es-PE', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const isNum = v => typeof v === 'number' && isFinite(v);
const f0 = v => isNum(v) ? nf0.format(v) : '—';
const f1 = v => isNum(v) ? nf1.format(v) : '—';
const f2 = v => isNum(v) ? nf2.format(v) : '—';
const pad = n => String(n).padStart(2, '0');
const hhmm = ts => { const d = new Date(ts * 1000); return pad(d.getHours()) + ':' + pad(d.getMinutes()); };
const hhmmss = ts => { const d = new Date(ts * 1000); return hhmm(ts) + ':' + pad(d.getSeconds()); };
function fmtDur(s) {
  if (!isNum(s)) return '—';
  s = Math.round(s);
  const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
  return d > 0 ? `${d}d ${h}h ${m}m` : h > 0 ? `${h}h ${m}m` : m > 0 ? `${m}m ${s % 60}s` : `${s}s`;
}
const fmtAcd = s => !isNum(s) ? '—' : s >= 60 ? `${Math.floor(s / 60)}:${pad(Math.round(s % 60))}` : `${Math.round(s)}s`;
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

// ---------------- estado / series ----------------
const SERIES = ['dialogs', 'early', 'registered', 'cps', 'asr', 'ner', 'acd', 'invite_ps',
  'inv_2xx_ps', 'inv_4xx_ps', 'inv_5xx_ps', 'total_ps', 'register_ps', 'options_ps',
  'auth_401_ps', 'auth_403_ps', 'drop_ps', 'pdd_out', 'pdd_out_p95', 'pdd_in', 'mos_act_avg', 'mos_act_min'];
const D = { t: [], hwt: [], cpu: [], mem: [], dsk: [] };
SERIES.forEach(k => D[k] = []);
let rangeMin = 60, lastSnap = null;
const alarmSeen = {};   // clave alarma -> primer ts visto

function snapToRow(s) {
  const R = s.rates || {}, K = s.kpi || {};
  return {
    dialogs: s.active_dialogs ?? 0, early: s.early_dialogs ?? 0, registered: s.registered || null,
    cps: K.cps ?? null, asr: K.asr ?? null, ner: K.ner ?? null, acd: K.acd ?? null,
    invite_ps: R.invite ?? 0, inv_2xx_ps: R.inv_2xx ?? 0, inv_4xx_ps: R.inv_4xx ?? 0,
    inv_5xx_ps: R.inv_5xx ?? 0, total_ps: R.total_req ?? 0, register_ps: R.register ?? 0,
    options_ps: R.options ?? 0, auth_401_ps: R.auth_401 ?? 0, auth_403_ps: R.auth_403 ?? 0,
    drop_ps: R.drop_req ?? 0,
    pdd_out: s.pdd?.out?.avg ?? null, pdd_out_p95: s.pdd?.out?.p95 ?? null, pdd_in: s.pdd?.in?.avg ?? null,
    mos_act_avg: s.mos?.active?.avg ?? null, mos_act_min: s.mos?.active?.min ?? null,
  };
}

async function loadHistory() {
  try {
    const h = await api(`api/history?minutes=${rangeMin}`);
    const ix = {}; h.cols.forEach((c, i) => ix[c] = i);
    D.t = h.rows.map(r => r[0]);
    SERIES.forEach(k => {
      D[k] = ix[k] === undefined ? h.rows.map(() => null) : h.rows.map(r => r[ix[k]]);
    });
    D.registered = D.registered.map(v => v || null);
  } catch (e) { /* sin historico aun */ }
  try {
    const s = await api(`api/syshistory?minutes=${rangeMin}`);
    const ix = {}; s.cols.forEach((c, i) => ix[c] = i);
    D.hwt = s.rows.map(r => r[0]);
    D.cpu = s.rows.map(r => r[ix.cpu_pct]);
    D.mem = s.rows.map(r => r[ix.mem_pct]);
    D.dsk = s.rows.map(r => r[ix.disk_pct]);
  } catch (e) { /* sin historico hw aun */ }
  paintCharts();
}

function trim(tsKey, keys, since) {
  let i = 0; const ts = D[tsKey];
  while (i < ts.length && ts[i] < since) i++;
  if (i) { D[tsKey] = ts.slice(i); keys.forEach(k => D[k] = D[k].slice(i)); }
}
function pushLive(s) {
  // resolucion ~600 puntos por rango para que 24h no acumule 43k muestras
  const step = Math.max(2, rangeMin * 60 / 600);
  const since = s.ts - rangeMin * 60;
  if (!D.t.length || s.ts - D.t[D.t.length - 1] >= step - 0.5) {
    const row = snapToRow(s);
    D.t.push(s.ts); SERIES.forEach(k => D[k].push(row[k]));
  }
  if (!D.hwt.length || s.ts - D.hwt[D.hwt.length - 1] >= step - 0.5) {
    const SYS = s.system || {};
    D.hwt.push(s.ts); D.cpu.push(SYS.cpu_pct ?? null);
    D.mem.push((SYS.mem || {}).pct ?? null); D.dsk.push(SYS.disk_max_pct ?? null);
  }
  trim('t', SERIES, since); trim('hwt', ['cpu', 'mem', 'dsk'], since);
  paintCharts();
}

// ---------------- motor de graficas (canvas, eje unico, hover) ----------------
const C1 = () => css('--s1'), C2 = () => css('--s2'), C3 = () => css('--s3');
const CHARTS = {
  calls: {
    title: 'Llamadas activas', desc: 'diálogos confirmados en conversación', ts: 't',
    series: [{ k: 'dialogs', name: 'Llamadas activas', c: C1 }], fmt: f0, unit: 'llamadas',
    refs: s => isNum(s?.kpi?.calls_24h) ? [{ v: s.kpi.calls_24h, label: `pico 24h ${f0(s.kpi.calls_24h)}` }] : [],
    now: s => [f0(s.active_dialogs), `+ ${f0(s.early_dialogs)} timbrando`],
  },
  regs: {
    title: 'Usuarios registrados', desc: 'contactos SIP en usrloc (escala ajustada para ver variaciones)', ts: 't',
    series: [{ k: 'registered', name: 'Registrados', c: C1 }], fmt: f0, unit: 'contactos', fit: true,
    now: s => [f0(s.registered), deltaTxt(s.kpi?.reg_delta_10m, 'en 10 min')],
  },
  cps: {
    title: 'Intentos de llamada (CPS)', desc: 'nuevos diálogos por segundo · promedio móvil 5 min', ts: 't',
    series: [{ k: 'cps', name: 'CPS', c: C1 }], fmt: f2, unit: 'llamadas/s',
    refs: s => isNum(s?.kpi?.cps_24h) ? [{ v: s.kpi.cps_24h, label: `pico 24h ${f2(s.kpi.cps_24h)}` }] : [],
    now: s => [f2(s.kpi?.cps), `${f0(s.kpi?.attempts)} intentos / 5 min`],
  },
  asr: {
    title: 'ASR y NER', desc: 'efectividad de llamadas · ventana 5 min', ts: 't',
    series: [{ k: 'asr', name: 'ASR %', c: C1 }, { k: 'ner', name: 'NER %', c: C2 }],
    fmt: f1, unit: '%', yMax: 100,
    refs: () => [{ v: 30, label: 'ASR mín 30%' }, { v: 95, label: 'NER mín 95%' }],
    now: s => [`${f1(s.kpi?.asr)}%`, `NER ${f1(s.kpi?.ner)}%`],
  },
  pdd: {
    title: 'Post-Dial Delay (PDD)', desc: 'INVITE → primer 180/183 · captura pasiva · ventana 5 min', ts: 't',
    series: [{ k: 'pdd_out', name: 'Saliente prom.', c: C1 }, { k: 'pdd_out_p95', name: 'Saliente p95', c: C2 },
      { k: 'pdd_in', name: 'Entrante prom.', c: C3 }], fmt: f2, unit: 's',
    refs: () => [{ v: 3, label: 'umbral prom. 3 s' }, { v: 5, label: 'umbral p95 5 s' }],
    now: s => [`${f2(s.pdd?.out?.avg)} s`, `saliente · p95 ${f2(s.pdd?.out?.p95)} s`],
  },
  mos: {
    title: 'Calidad de voz (MOS)', desc: 'llamadas activas · RTCP vía RTPEngine NG · peor pierna por llamada', ts: 't',
    series: [{ k: 'mos_act_avg', name: 'MOS promedio', c: C1 }, { k: 'mos_act_min', name: 'MOS peor llamada', c: C2 }],
    fmt: f2, unit: '', yMin: 1, yMax: 5,
    refs: s => [{ v: s?.mos?.bad_threshold ?? 3.6, label: `umbral ${f1(s?.mos?.bad_threshold ?? 3.6)}` }],
    now: s => [f2(s.mos?.active?.avg), `${f0(s.mos?.active?.bad)} activas bajo umbral`],
  },
  invrep: {
    title: 'Respuestas a INVITE', desc: 'respuestas finales recibidas por clase, por segundo', ts: 't',
    series: [{ k: 'inv_2xx_ps', name: '2xx contestada', c: C1 }, { k: 'inv_4xx_ps', name: '4xx cliente', c: C3 },
      { k: 'inv_5xx_ps', name: '5xx red/servidor', c: C2 }], fmt: f2, unit: '/s',
    now: s => [f2(s.rates?.inv_5xx), '5xx/s'],
  },
  sig: {
    title: 'Señalización SIP', desc: 'requests recibidos por segundo', ts: 't',
    series: [{ k: 'total_ps', name: 'Total', c: C1 }, { k: 'register_ps', name: 'REGISTER', c: C2 },
      { k: 'options_ps', name: 'OPTIONS', c: C3 }], fmt: f1, unit: 'req/s',
    now: s => [f1(s.rates?.total_req), 'req/s'],
  },
  sec: {
    title: 'Seguridad y autenticación', desc: 'challenges, rechazos y requests descartados (pike/antiflood)', ts: 't',
    series: [{ k: 'auth_401_ps', name: '401 challenge', c: C1 }, { k: 'auth_403_ps', name: '403 rechazado', c: C2 },
      { k: 'drop_ps', name: 'Descartados', c: C3 }], fmt: f1, unit: '/s',
    now: s => [f1(s.rates?.auth_403), '403/s'],
  },
  hw: {
    title: 'Hardware del servidor', desc: 'uso de CPU, RAM y disco', ts: 'hwt',
    series: [{ k: 'cpu', name: 'CPU %', c: C1 }, { k: 'mem', name: 'RAM %', c: C2 }, { k: 'dsk', name: 'Disco %', c: C3 }],
    fmt: f1, unit: '%', yMax: 100, refs: () => [{ v: 80, label: 'umbral 80%' }],
    now: s => [`${f1(s.system?.cpu_pct)}%`, 'CPU'],
  },
};
function deltaTxt(d, suf) {
  if (!isNum(d)) return '';
  const cls = d < 0 ? 'dn-bad' : 'up-good';
  return `<span class="${cls}">${d > 0 ? '+' : ''}${f0(d)}</span> ${suf}`;
}

function niceStep(span, n) {
  const raw = span / n, p = Math.pow(10, Math.floor(Math.log10(raw))), m = raw / p;
  return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 2.5 ? 2.5 : m <= 5 ? 5 : 10) * p;
}

function buildCharts() {
  document.querySelectorAll('[data-chart]').forEach(el => {
    const id = el.dataset.chart, c = CHARTS[id];
    el.innerHTML = `<div class="ch"><div><h3>${c.title}</h3><div class="desc">${c.desc}</div></div>
      <div class="now"><b id="now-${id}">—</b><span id="nows-${id}"></span></div></div>
      ${c.series.length > 1 || c.refs ? `<div class="legend">${c.series.map(s => `<span><i style="background:${s.c()}"></i>${s.name}</span>`).join('')}${c.refs ? '<span><i class="dash"></i>referencia</span>' : ''}</div>` : ''}
      <canvas id="cv-${id}" aria-label="${c.title}"></canvas>
      <div class="stats" id="st-${id}"></div>`;
    const cv = $('cv-' + id);
    c.cv = cv; c.hover = null;
    cv.addEventListener('mousemove', e => { c.hoverX = e.offsetX; c.mouse = [e.clientX, e.clientY]; drawChart(id); });
    cv.addEventListener('mouseleave', () => { c.hoverX = null; $('tip').hidden = true; drawChart(id); });
  });
}

function drawChart(id) {
  const c = CHARTS[id], cv = c.cv;
  if (!cv) return;
  const ctx = cv.getContext('2d');
  const W = cv.clientWidth || 600, H = cv.clientHeight || 200;
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  if (cv.width !== Math.round(W * dpr) || cv.height !== Math.round(H * dpr)) {
    cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr);
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);
  const ts = D[c.ts];
  const now = lastSnap?.ts || (ts.length ? ts[ts.length - 1] : Date.now() / 1000);
  const x0 = now - rangeMin * 60, x1 = now;
  const L = 52, R = 10, T = 10, B = 22, pw = W - L - R, ph = H - T - B;
  const X = t => L + pw * (t - x0) / (x1 - x0);

  // dominio Y
  const vals = [];
  c.series.forEach(s => D[s.k].forEach((v, i) => { if (isNum(v) && ts[i] >= x0) vals.push(v); }));
  const refs = c.refs ? c.refs(lastSnap) : [];
  let lo = c.yMin || 0, hi = c.yMax || 1;
  if (vals.length) {
    const vmin = Math.min(...vals), vmax = Math.max(...vals);
    if (c.fit) {
      const padv = Math.max((vmax - vmin) * 0.25, Math.max(vmax * 0.002, 5));
      lo = Math.max(0, vmin - padv); hi = vmax + padv;
    } else if (!c.yMax) {
      hi = Math.max(vmax, ...refs.map(r => r.v)) * 1.12 || 1;
    }
  }
  const step = niceStep(hi - lo, 4);
  lo = Math.floor(lo / step) * step; hi = Math.ceil(hi / step) * step;
  if (hi <= lo) hi = lo + step;
  const Y = v => T + ph * (1 - (v - lo) / (hi - lo));

  // grid + ejes (recesivos)
  ctx.font = '11px system-ui'; ctx.textBaseline = 'middle';
  ctx.strokeStyle = css('--grid'); ctx.fillStyle = css('--mut'); ctx.lineWidth = 1;
  for (let v = lo; v <= hi + step / 2; v += step) {
    const y = Math.round(Y(v)) + 0.5;
    ctx.beginPath(); ctx.moveTo(L, y); ctx.lineTo(W - R, y); ctx.stroke();
    ctx.textAlign = 'right';
    ctx.fillText(v >= 10000 ? nf0.format(v) : step < 1 ? v.toFixed(step < 0.1 ? 2 : 1) : nf0.format(v), L - 6, y);
  }
  ctx.strokeStyle = css('--axis');
  ctx.beginPath(); ctx.moveTo(L, T + ph + 0.5); ctx.lineTo(W - R, T + ph + 0.5); ctx.stroke();
  // eje X por tiempo
  ctx.textAlign = 'center'; ctx.textBaseline = 'alphabetic';
  const tickS = [60, 300, 600, 900, 1800, 3600, 7200, 10800, 21600].find(s => (x1 - x0) / s <= 6) || 21600;
  const off = new Date().getTimezoneOffset() * 60;
  for (let t = Math.ceil((x0 - off) / tickS) * tickS + off; t <= x1; t += tickS) {
    const x = X(t);
    if (x < L + 16 || x > W - R - 16) continue;
    ctx.fillText(hhmm(t), x, H - 6);
  }

  // lineas de referencia (umbrales / picos)
  refs.forEach(r => {
    if (r.v < lo || r.v > hi) return;
    const y = Math.round(Y(r.v)) + 0.5;
    ctx.setLineDash([4, 4]); ctx.strokeStyle = css('--mut');
    ctx.beginPath(); ctx.moveTo(L, y); ctx.lineTo(W - R, y); ctx.stroke(); ctx.setLineDash([]);
    ctx.textAlign = 'right'; ctx.fillStyle = css('--txt2'); ctx.textBaseline = 'bottom';
    ctx.fillText(r.label, W - R - 2, y - 2);
  });

  // series (2px; huecos donde no hay dato o salto > 3 pasos)
  const gap = Math.max(30, rangeMin * 60 / 600 * 4);
  let any = false;
  c.series.forEach(s => {
    const d = D[s.k]; ctx.strokeStyle = s.c(); ctx.lineWidth = 2; ctx.lineJoin = 'round';
    ctx.beginPath(); let pen = false, pt = null;
    for (let i = 0; i < d.length; i++) {
      if (ts[i] < x0 || !isNum(d[i])) { pen = false; continue; }
      if (pen && ts[i] - pt > gap) pen = false;
      const x = X(ts[i]), y = Y(Math.min(Math.max(d[i], lo), hi));
      pen ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
      pen = true; pt = ts[i]; any = true;
    }
    ctx.stroke();
  });
  if (c.series.length === 1 && !c.fit && any) { // area suave solo en serie unica con base cero
    const s = c.series[0], d = D[s.k];
    const g = ctx.createLinearGradient(0, T, 0, T + ph);
    g.addColorStop(0, s.c() + '40'); g.addColorStop(1, s.c() + '00');
    ctx.fillStyle = g; ctx.beginPath(); let started = false, lx = 0;
    for (let i = 0; i < d.length; i++) {
      if (ts[i] < x0 || !isNum(d[i])) continue;
      const x = X(ts[i]), y = Y(Math.min(Math.max(d[i], lo), hi));
      if (!started) { ctx.moveTo(x, T + ph); started = true; }
      ctx.lineTo(x, y); lx = x;
    }
    ctx.lineTo(lx, T + ph); ctx.closePath(); ctx.fill();
  }
  if (!any) {
    ctx.fillStyle = css('--mut'); ctx.font = '12px system-ui'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.fillText('Sin datos en el rango — acumulando muestras…', L + pw / 2, T + ph / 2);
  }

  // hover: crosshair + puntos + tooltip
  const tip = $('tip');
  if (c.hoverX != null && any && c.hoverX >= L) {
    const tH = x0 + (c.hoverX - L) / pw * (x1 - x0);
    let bi = -1, bd = Infinity;
    for (let i = 0; i < ts.length; i++) { const dd = Math.abs(ts[i] - tH); if (dd < bd) { bd = dd; bi = i; } }
    if (bi >= 0) {
      const x = Math.round(X(ts[bi])) + 0.5;
      ctx.strokeStyle = css('--txt2'); ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(x, T); ctx.lineTo(x, T + ph); ctx.stroke();
      c.series.forEach(s => {
        const v = D[s.k][bi]; if (!isNum(v)) return;
        ctx.beginPath(); ctx.arc(x, Y(Math.min(Math.max(v, lo), hi)), 4, 0, Math.PI * 2);
        ctx.fillStyle = s.c(); ctx.fill(); ctx.lineWidth = 2; ctx.strokeStyle = css('--surface'); ctx.stroke();
      });
      tip.innerHTML = `<div class="t">${new Date(ts[bi] * 1000).toLocaleDateString()} ${hhmmss(ts[bi])}</div>` +
        c.series.map(s => `<div class="r"><span><i style="background:${s.c()}"></i>${s.name}</span><b>${c.fmt(D[s.k][bi])} ${c.unit}</b></div>`).join('');
      tip.hidden = false;
      const [mx, my] = c.mouse, tw = tip.offsetWidth;
      tip.style.left = (mx + 16 + tw > window.innerWidth ? mx - tw - 16 : mx + 16) + 'px';
      tip.style.top = (my - 10) + 'px';
    }
  }

  // estadisticas del rango (primera serie)
  const s0 = c.series[0], v0 = D[s0.k].filter((v, i) => isNum(v) && ts[i] >= x0);
  const st = $('st-' + id);
  if (st) st.innerHTML = v0.length
    ? `<span>${s0.name}:</span><span>mín <b>${c.fmt(Math.min(...v0))}</b></span><span>prom <b>${c.fmt(v0.reduce((a, b) => a + b, 0) / v0.length)}</b></span><span>máx <b>${c.fmt(Math.max(...v0))}</b></span><span>${v0.length} muestras</span>`
    : '';
  if (lastSnap && c.now) {
    const [a, b] = c.now(lastSnap);
    $('now-' + id).textContent = a; $('nows-' + id).innerHTML = b;
  }
}
function paintCharts() { Object.keys(CHARTS).forEach(drawChart); }

// sparkline para los KPIs principales
function spark(cv, data, color) {
  if (!cv) return;
  const ctx = cv.getContext('2d'), W = cv.clientWidth || 180, H = cv.clientHeight || 34;
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const v = data.filter(isNum);
  if (v.length < 2) return;
  const mn = Math.min(...v), mx = Math.max(...v), sp = (mx - mn) || 1;
  ctx.strokeStyle = color; ctx.lineWidth = 1.5; ctx.beginPath();
  let k = 0;
  data.forEach(d => {
    if (!isNum(d)) return;
    const x = 1 + (W - 2) * k / (v.length - 1), y = 2 + (H - 4) * (1 - (d - mn) / sp);
    k++ === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  });
  ctx.stroke();
}

// ---------------- umbrales de estado ----------------
// devuelve [clase, etiqueta]; estado siempre acompanado de icono+texto
const ST = { good: 'OK', warning: 'AVISO', serious: 'MAYOR', critical: 'CRÍTICO', info: 'INFO' };
const lvl = (v, warn, crit, higherIsBetter = true) => {
  if (!isNum(v)) return 'info';
  return higherIsBetter ? (v < crit ? 'critical' : v < warn ? 'warning' : 'good')
    : (v > crit ? 'critical' : v > warn ? 'warning' : 'good');
};
// bandas ITU-T G.107: >=4.0 satisfecho, 3.6 algunos, 3.1 muchos insatisfechos
const mosLvl = v => !isNum(v) ? 'info' : v >= 4.0 ? 'good' : v >= 3.6 ? 'warning' : v >= 3.1 ? 'serious' : 'critical';
const badge = (cls, txt) => `<span class="st ${cls}">${txt ?? ST[cls]}</span>`;

function heroTile(id, label, value, unit, cls, subL, subR, sparkKey, badgeTxt) {
  return `<div class="hk ${cls === 'critical' ? 'critical' : cls === 'serious' ? 'serious' : cls === 'warning' ? 'warning' : ''}">
    <div class="top"><span class="l">${label}</span>${badge(cls, badgeTxt)}</div>
    <div class="v">${value}${unit ? `<small>${unit}</small>` : ''}</div>
    <div class="sub"><span>${subL || ''}</span><span>${subR || ''}</span></div>
    ${sparkKey ? `<canvas id="sp-${id}"></canvas>` : ''}</div>`;
}

function renderHero(s) {
  const K = s.kpi || {}, P = s.pdd || {}, M = s.mos || {};
  const enough = (K.attempts || 0) >= 10;
  const tiles = [
    ['calls', 'Llamadas activas', f0(s.active_dialogs), '', 'info',
      `pico 24h <b>${f0(K.calls_24h)}</b>${K.calls_24h_ts ? ' · ' + hhmm(K.calls_24h_ts) : ''}`,
      `${f0(s.early_dialogs)} timbrando`, 'dialogs', 'EN CURSO'],
    ['regs', 'Usuarios registrados', f0(s.registered), '',
      K.reg_drop_pct >= 15 ? 'critical' : K.reg_drop_pct >= 5 ? 'serious' : s.registered > 0 ? 'good' : 'critical',
      deltaTxt(K.reg_delta_10m, '10 min'), `DB ${f0(s.registered_db)}`, 'registered'],
    ['cps', 'CPS', f2(K.cps), 'llam/s', 'info',
      `pico 24h <b>${f2(K.cps_24h)}</b>`, `${f0(K.attempts)} intentos/5m`, 'cps', 'TRÁFICO'],
    ['asr', 'ASR', f1(K.asr), '%', enough ? lvl(K.asr, 45, 30) : 'info',
      `${f0(K.answered)} / ${f0(K.attempts)} contestadas`, 'obj ≥ 45%', 'asr', enough ? undefined : 'MUESTRA BAJA'],
    ['ner', 'NER', f1(K.ner), '%', enough ? lvl(K.ner, 98, 95) : 'info',
      'sin fallos de red 5xx', 'obj ≥ 98%', 'ner', enough ? undefined : 'MUESTRA BAJA'],
    ['acd', 'ACD', fmtAcd(K.acd), 'min', isNum(K.acd) ? (K.acd < 30 ? 'warning' : 'good') : 'info',
      'duración media (est.)', 'mín 0:30', 'acd'],
    ['pdd', 'PDD saliente', f2(P.out?.avg), 's', (P.out?.n || 0) >= 5 ? lvl(P.out.p95, 3, 5, false) : 'info',
      `p95 <b>${f2(P.out?.p95)} s</b> · ${f0(P.out?.n)} llam.`, `entrante ${isNum(P.in?.avg) ? f0(P.in.avg * 1000) + ' ms' : '—'}`,
      'pdd_out', (P.out?.n || 0) >= 5 ? undefined : 'MUESTRA BAJA'],
    ['mos', 'MOS por llamada', f2(M.done?.avg ?? M.active?.avg), '/ 5', mosLvl(M.done?.avg ?? M.active?.avg),
      `<b>${isNum(M.done?.bad_pct) ? f1(M.done.bad_pct) + '%' : '—'}</b> bajo ${f1(M.bad_threshold)} · ${f0(M.done?.n_mos)} llam./15m`,
      `activas mín ${f1(M.active?.min)}`, 'mos_act_avg'],
    ['rsr', 'Registro exitoso', f2(K.reg_ok_pct), '%', lvl(K.reg_ok_pct, 99, 95),
      `${f1(s.rates?.register)} REGISTER/s`, 'obj ≥ 99%', null],
    ['avail', 'Disponibilidad 24h', isNum(K.avail_24h) ? f2(K.avail_24h) : '—', '%', lvl(K.avail_24h, 99.99, 99.9),
      K.avail_since ? `desde ${new Date(K.avail_since * 1000).toLocaleString([], { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })}` : 'sin muestras',
      'SLA 99,99%', null],
    ['rtt', 'Latencia SIP', f1(s.sip_rtt_max), 'ms', lvl(s.sip_rtt_max, 50, 200, false),
      'OPTIONS máx 4 listeners', `uptime ${fmtDur(s.uptime_s)}`, null],
  ];
  $('hero').innerHTML = tiles.map(t => heroTile(...t)).join('');
  tiles.forEach(t => { if (t[7]) spark($('sp-' + t[0]), D[t[7]].slice(-180), C1()); });
}

function renderNE(s) {
  const H = s.health || {}, RTP = s.rtpengine || {}, HA = s.ha || {};
  const items = [];
  const ne = (name, cls, detail) => items.push(`<div class="nei ${cls}" title="${esc(name)} — ${esc(detail)}"><span class="ic"></span><div class="tx"><div class="n">${esc(name)}</div><div class="d">${badge(cls)} ${esc(detail)}</div></div></div>`);
  ne('Kamailio core', H.kamailio ? 'good' : 'critical', `/healthz HTTP ${s.codes?.healthz ?? '?'}`);
  ne('Base de datos', H.db ? 'good' : 'critical', `/readyz ${s.codes?.readyz ?? '?'}`);
  Object.entries(s.sip_ping || {}).forEach(([k, v]) =>
    ne(`SIP ${k}`, v.ok ? (v.ms > 50 ? 'warning' : 'good') : 'serious', v.ok ? `${v.ms} ms` : 'sin respuesta'));
  if (RTP.status !== 'n/a') ne('RTPEngine', RTP.status === 'active' ? (RTP.kernel_mode ? 'good' : 'warning') : 'critical',
    `${RTP.status || '?'} · ${RTP.kernel_mode ? 'kernel' : 'userspace'}`);
  (HA.peers || []).filter(p => !p.local).forEach(p =>
    ne(`DMQ ${p.host}`, p.status === 'active' ? 'good' : 'serious', p.status || '?'));
  if (HA.vip) ne('keepalived / VIP', HA.keepalived === 'active' ? 'good' : 'serious',
    `${HA.vip || '?'} ${HA.vip_local ? 'MASTER' : 'BACKUP'}`);
  ne('Dispatcher', s.dispatcher_up === s.dispatcher_total ? 'good' : s.dispatcher_up > 0 ? 'warning' : 'serious',
    `${s.dispatcher_up}/${s.dispatcher_total} destinos UP`);
  const P = s.pdd || {};
  if (P.status !== 'disabled') ne('Sonda PDD', P.status === 'running' ? 'good' : P.status === 'error' ? 'warning' : 'info',
    P.status === 'running' ? `${f0(P.pending)} INVITE en curso` : (P.error || P.status || '?'));
  const M = s.mos || {};
  if (M.status !== 'disabled') ne('Sonda MOS (NG)', M.status === 'running' ? 'good' : M.status === 'error' ? 'warning' : 'info',
    M.status === 'running' ? `${f0(M.active?.n_mos)}/${f0(M.active?.n)} llamadas con RTCP` : (M.error || M.status || '?'));
  const shm = s.shm?.pct ?? 0;
  ne('Memoria SHM', shm >= 85 ? 'serious' : shm >= 70 ? 'warning' : 'good', `${shm}% usado`);
  $('ne').innerHTML = items.join('');
}

function renderAlarms(s) {
  const now = s.ts, al = s.alerts || [];
  const keys = new Set();
  const order = { critical: 0, major: 1, minor: 2 };
  const rows = al.map(a => {
    const key = a.sev + '|' + a.msg.replace(/[\d.,]+/g, '#');
    keys.add(key);
    if (!alarmSeen[key]) alarmSeen[key] = now;
    return { ...a, since: alarmSeen[key] };
  }).sort((a, b) => order[a.sev] - order[b.sev] || a.since - b.since);
  Object.keys(alarmSeen).forEach(k => { if (!keys.has(k)) delete alarmSeen[k]; });
  const sevMap = { critical: 'critical', major: 'serious', minor: 'warning' };
  const sevTxt = { critical: 'CRÍTICA', major: 'MAYOR', minor: 'MENOR' };
  $('alarms').innerHTML = rows.length
    ? rows.map(a => `<tr class="${a.sev}"><td>${badge(sevMap[a.sev], sevTxt[a.sev])}</td><td>${hhmmss(a.since)}</td><td>${fmtDur(now - a.since)}</td><td>${esc(a.msg)}</td></tr>`).join('')
    : `<tr class="none"><td>${badge('good', 'LIMPIO')}</td><td>—</td><td>—</td><td>Sin alarmas activas</td></tr>`;
  const cnt = sev => al.filter(a => a.sev === sev).length;
  [['nCrit', 'critical', '.ac.crit'], ['nMaj', 'major', '.ac.maj'], ['nMin', 'minor', '.ac.min']].forEach(([id, sev, sel]) => {
    const n = cnt(sev); $(id).textContent = n;
    document.querySelector(sel).classList.toggle('on', n > 0);
  });
  $('alarmHint').textContent = `${al.length} activa(s) · duración medida desde que este navegador la detectó`;
}

function kpi(l, v, u = '', cls = '') {
  return `<div class="kpi ${cls}"><div class="l">${l}</div><div class="v">${v}</div><div class="u">${u}</div></div>`;
}

function renderPlatform(s) {
  const R = s.rates || {}, H = s.health || {}, RTP = s.rtpengine || {};
  const SYS = s.system || {}, MEM = SYS.mem || {};
  const shm = s.shm?.pct ?? 0;
  $('kpis').innerHTML =
    kpi('Uptime proxy', fmtDur(s.uptime_s), 'kamailio') +
    kpi('INVITE/s', f1(R.invite), 'incl. re-INVITE') +
    kpi('BYE/s', f1(R.bye), 'cuelgues/s') +
    kpi('CANCEL/s', f1(R.cancel), 'abandonos/s') +
    kpi('5xx/s', f2(R.rpl_5xx), 'errores servidor', R.rpl_5xx > 0.1 ? 'bad' : '') +
    kpi('Descartados/s', f1(R.drop_req), 'pike/antiflood', R.drop_req > 1 ? 'warn' : '') +
    kpi('SHM', shm + '%', 'memoria compartida', shm > 85 ? 'bad' : shm > 70 ? 'warn' : '') +
    kpi('CPU', f1(SYS.cpu_pct) + '%', `load ${f2((SYS.load || [])[0])}`, SYS.cpu_pct >= 90 ? 'bad' : SYS.cpu_pct >= 75 ? 'warn' : '') +
    kpi('RAM', f1(MEM.pct) + '%', `${f0(MEM.used_mb)}/${f0(MEM.total_mb)} MB`, MEM.pct >= 90 ? 'bad' : MEM.pct >= 80 ? 'warn' : '') +
    kpi('Disco', f0(SYS.disk_max_pct) + '%', 'punto más lleno', SYS.disk_max_pct >= 90 ? 'bad' : SYS.disk_max_pct >= 80 ? 'warn' : '') +
    kpi('Red', `${f0(SYS.net_rx)}↓ ${f0(SYS.net_tx)}↑`, 'KB/s total') +
    kpi('TCP resets/s', f2(R.tcp_reset), `timeouts ${f2(R.tcp_timeout)}/s`);
  $('platHint').textContent = `nodo ${s.hostname || '?'} · load ${(SYS.load || []).map(f2).join(' / ')}`;

  const row = (n, ok, extra = '') => `<tr><td>${n}</td><td>${badge(ok ? 'good' : 'critical', ok ? 'UP' : 'DOWN')}</td><td>${esc(extra)}</td></tr>`;
  $('rtt').textContent = Object.entries(s.sip_ping || {}).map(([k, v]) => `${k}: ${v.ok ? v.ms + ' ms' : 'FAIL'}`).join(' · ');
  $('health').innerHTML =
    row('Kamailio /healthz', H.kamailio, 'HTTP ' + (s.codes?.healthz ?? '?')) +
    row('Readiness + DB', H.db, '/readyz ' + (s.codes?.readyz ?? '?')) +
    row('SIP OPTIONS activo', H.sip_ping, '4 listeners UDP') +
    row('Dispatcher', H.dispatcher, `${s.dispatcher_up}/${s.dispatcher_total} UP`) +
    row('Métricas core', H.metrics, 'kamcmd stats') +
    row('SHM < 85%', H.shm_ok, shm + '% usado') +
    row('RTPEngine', RTP.status === 'active', 'kernel ' + (RTP.kernel_mode ? '✓' : '✗'));

  $('dsptable').querySelector('tbody').innerHTML = (s.dispatcher || []).map(d =>
    `<tr><td>${d.set ?? '—'}</td><td>${esc(d.uri)}</td><td>${esc(d.flags)}</td><td>${d.ignored ? badge('info', 'EXCLUIDO') : badge(d.up ? 'good' : 'serious', d.up ? 'UP' : 'DOWN')}</td></tr>`).join('')
    || '<tr><td colspan=4>sin destinos</td></tr>';

  const HA = s.ha || { peers: [] };
  $('hatable').querySelector('tbody').innerHTML = (HA.peers || []).map(p =>
    `<tr><td>${esc(p.host)}${p.local ? ' (local)' : ''}</td><td>${badge(p.status === 'active' ? 'good' : 'serious', (p.status || '?').toUpperCase())}</td><td>${p.local ? 'este nodo' : 'peer'}</td></tr>`).join('')
    || '<tr><td colspan=3>sin datos DMQ</td></tr>';
  $('hainfo').textContent = `VIP ${HA.vip || '?'} · aquí: ${HA.vip_local ? 'SÍ (MASTER)' : 'no (BACKUP)'} · keepalived ${HA.keepalived || '?'}`;

  const mosCls = lvl(RTP.mos_avg, 4.0, 3.5);
  $('rtptable').innerHTML =
    `<tr><td>Servicio</td><td>${badge(RTP.status === 'active' ? 'good' : 'critical', RTP.status === 'active' ? 'ACTIVE' : (RTP.status || '?'))}</td></tr>` +
    `<tr><td>Forwarding</td><td>${badge(RTP.kernel_mode ? 'good' : 'warning', RTP.kernel_mode ? 'KERNEL' : 'USERSPACE')}</td></tr>` +
    `<tr><td>Streams RTP</td><td>${f0(RTP.rtp_sessions)} en tabla kernel</td></tr>` +
    `<tr><td>MOS promedio</td><td>${badge(mosCls, isNum(RTP.mos_avg) ? f2(RTP.mos_avg) + ' / 5' : '—')}</td></tr>` +
    `<tr><td>Errores recientes</td><td>${badge((RTP.errors_recent || 0) > 0 ? 'warning' : 'good', f0(RTP.errors_recent ?? 0))}</td></tr>`;
  $('rtpinfo').textContent = 'kernel table /proc/rtpengine/0 · MOS = promedio últimas 20 llamadas';

  const sl = s.sl || {};
  $('codes').querySelector('tbody').innerHTML = Object.keys(sl).filter(k => /^\d{3}$/.test(k) && sl[k] > 0).sort()
    .map(k => `<tr><td>${k}</td><td>${f0(sl[k])}</td></tr>`).join('') || '<tr><td colspan=2>—</td></tr>';
  $('sysinfo').textContent = `diálogos fallidos acumulados ${f0(s.dialogs?.failed_dialogs ?? null)}`;

  const rows2 = [];
  (SYS.disks || []).forEach(d => rows2.push(`<tr><td>Disco ${esc(d.mount)}</td><td>${d.pct}% (${d.free_gb} GB libres)</td></tr>`));
  (SYS.net || []).forEach(n => rows2.push(`<tr><td>Red ${esc(n.if)}</td><td>↓ ${n.rx_kbs} ↑ ${n.tx_kbs} KB/s</td></tr>`));
  rows2.push(`<tr><td>Uptime sistema</td><td>${fmtDur(SYS.uptime_s)}</td></tr>`);
  rows2.push(`<tr><td>Swap usado</td><td>${SYS.swap_pct ?? 0}%</td></tr>`);
  rows2.push(`<tr><td>Kamailio</td><td>${SYS.kam_procs ?? 0} procs · ${SYS.kam_rss_mb ?? 0} MB RSS</td></tr>`);
  if (SYS.temp_max != null) rows2.push(`<tr><td>Temp máx</td><td>${SYS.temp_max} °C</td></tr>`);
  $('hwtable').querySelector('tbody').innerHTML = rows2.join('');
  $('hwinfo').textContent = `RAM ${f0(MEM.used_mb)}/${f0(MEM.total_mb)} MB`;
}

const shortId = id => id.length > 22 ? id.slice(0, 10) + '…' + id.slice(-8) : id;
const qrow = (c, cols) => `<tr>${cols.map(k => {
  if (k === 'mos') return `<td>${isNum(c.mos) ? badge(mosLvl(c.mos), f2(c.mos)) : badge('info', 'sin RTCP')}</td>`;
  if (k === 'call_id') return `<td title="${esc(c.call_id)}"><code>${esc(shortId(c.call_id))}</code></td>`;
  if (k === 'dur') return `<td>${fmtDur(c.dur)}</td>`;
  if (k === 'ended') return `<td>${hhmmss(c.ended)}</td>`;
  if (k === 'mos_low') return `<td>${f1(c.mos_low)}</td>`;
  if (k === 'jitter') return `<td>${isNum(c.jitter) ? f0(c.jitter) + ' ms' : '—'}</td>`;
  if (k === 'loss') return `<td>${isNum(c.loss) ? f1(c.loss) + '%' : '—'}</td>`;
  if (k === 'rtt') return `<td>${isNum(c.rtt) ? f0(c.rtt) + ' ms' : '—'}</td>`;
  return `<td>${esc(c[k] || '—')}</td>`;
}).join('')}</tr>`;
function renderWorstActive(s) {
  const W = s.mos?.worst_active || [];
  $('worstAct').innerHTML = W.length ? W.map(c => qrow(c, ['call_id', 'remote', 'codec', 'dur', 'mos', 'mos_low', 'jitter', 'loss', 'rtt'])).join('')
    : '<tr><td colspan=9>sin llamadas activas con RTCP</td></tr>';
  const A = s.mos?.active || {};
  $('worstHint').textContent = `${f0(A.n_mos)} de ${f0(A.n)} llamadas reportan MOS (requiere RTCP)`;
}
let callsMode = 'recent';
async function loadCalls() {
  try {
    const q = callsMode === 'bad' ? 'bad=1&order=worst' : callsMode === 'worst' ? 'order=worst' : 'order=recent';
    const r = await api(`api/calls?minutes=${Math.max(rangeMin, 60)}&${q}`);
    const ix = {}; r.cols.forEach((c, i) => ix[c] = i);
    const rows = r.rows.slice(0, 60).map(x => Object.fromEntries(r.cols.map(c => [c, x[ix[c]]])));
    $('doneCalls').innerHTML = rows.length ? rows.map(c => qrow(c, ['ended', 'call_id', 'remote', 'dur', 'mos', 'mos_low', 'jitter', 'loss', 'rtt'])).join('')
      : '<tr><td colspan=9>sin llamadas en el periodo</td></tr>';
    $('doneHint').textContent = `${f0(r.total)} finalizadas · ${f0(r.with_mos)} con MOS · prom ${f2(r.avg_mos)} · ${f0(r.bad)} bajo ${f1(r.bad_threshold)} · últimas ${Math.max(rangeMin, 60) / 60} h`;
  } catch (e) { /* reintenta en el proximo ciclo */ }
}
document.querySelectorAll('#callsMode button').forEach(b => b.addEventListener('click', () => {
  document.querySelectorAll('#callsMode button').forEach(x => x.classList.toggle('on', x === b));
  callsMode = b.dataset.v; loadCalls();
}));

async function refresh() {
  if (document.hidden) return; // pestaña oculta: no renderizar
  try {
    const s = await api('api/snapshot');
    lastSnap = s;
    const d = new Date(s.ts * 1000);
    $('clock').textContent = d.toLocaleDateString() + ' ' + hhmmss(s.ts);
    $('clockUtc').textContent = 'UTC ' + pad(d.getUTCHours()) + ':' + pad(d.getUTCMinutes()) + ':' + pad(d.getUTCSeconds());
    const st = s.status === 'ok' ? 'ok' : s.status;
    $('gstatus').textContent = st === 'ok' ? 'SERVICIO OPERATIVO' : st === 'critical' ? 'SERVICIO CRÍTICO' : 'SERVICIO DEGRADADO';
    $('gstatus').className = 'pill ' + st;
    $('gdot').className = 'sev-dot ' + st;
    $('node').textContent = s.hostname || 'proxy';
    $('harole').textContent = !s.ha?.vip ? 'nodo único' : s.ha.vip_local ? 'MASTER (VIP activa)' : 'BACKUP';
    $('site').textContent = s.site || s.domain || '';
    document.title = (s.alerts?.length ? `(${s.alerts.length}) ` : '') + 'Kamailio NOC';
    renderAlarms(s); renderNE(s); renderPlatform(s); renderWorstActive(s);
    pushLive(s);
    renderHero(s);
  } catch (e) { $('gstatus').textContent = 'SIN DATOS (' + e.message + ')'; $('gstatus').className = 'pill critical'; }
}

// ---------------- arranque ----------------
document.querySelectorAll('#range button').forEach(b => b.addEventListener('click', () => {
  document.querySelectorAll('#range button').forEach(x => x.classList.toggle('on', x === b));
  rangeMin = parseInt(b.dataset.m, 10);
  try { localStorage.setItem('noc_range', rangeMin); } catch (e) { /* sin storage */ }
  loadHistory(); loadCalls();
}));
try {
  const r = parseInt(localStorage.getItem('noc_range'), 10);
  if ([15, 60, 360, 1440].includes(r)) {
    rangeMin = r;
    document.querySelectorAll('#range button').forEach(x => x.classList.toggle('on', +x.dataset.m === r));
  }
} catch (e) { /* sin storage */ }
buildCharts();
(async () => { await loadHistory(); await refresh(); loadCalls(); setInterval(refresh, 2000); setInterval(() => { if (!document.hidden) loadCalls(); }, 15000); })();
let rsz = null;
window.addEventListener('resize', () => { clearTimeout(rsz); rsz = setTimeout(() => { paintCharts(); if (lastSnap) renderHero(lastSnap); }, 150); });
document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
