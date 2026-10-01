// Progress: long-term numbers from every ride (training.py on the Python side). Fitness, fatigue
// and form over time, a month calendar (click a ride to open it) and weekly totals. Uses C, $, fmt,
// blend, act, THEME, store and dither from app.js.
"use strict";

const PROG = { data: null, version: null, month: null, range: store.get("pmcRange", 90), metric: store.get("wkMetric", "time_s"), plots: {} };
const MONTH = new Intl.DateTimeFormat("en-GB", { month: "long", year: "numeric" });
const SHORT_DAY = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short" });
const hm = (s) => `${Math.floor(s / 3600)}:${String(Math.floor(s / 60) % 60).padStart(2, "0")}`;
const dayOf = (iso) => { const [y, m, d] = iso.split("-").map(Number); return new Date(y, m - 1, d); };
const isoOf = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;

async function showStats(s) {
  if (S.prevScreen !== "stats" || HOME.loadedFor !== s.rides_version) {     // the history
    HOME.loadedFor = s.rides_version;
    loadRides();
  }
  // (re)load on arriving here, and when a ride was saved or deleted meanwhile; one fetch at a time
  if (PROG.loading || (PROG.data && S.prevScreen === "stats" && PROG.version === s.rides_version)) return;
  PROG.version = s.rides_version;
  PROG.loading = true;
  try {
    PROG.data = await (await fetch("/api/progress")).json();
  } catch { return; } finally { PROG.loading = false; }
  if (!PROG.month) PROG.month = dayOf(PROG.data.today);
  renderProgress();
}
function redrawProgress() { if (PROG.data && !$("scr-stats").hidden) renderProgress(); }

function renderProgress() {
  const d = PROG.data, t = d.totals;
  $("prog-totals").innerHTML = [["Rides", t.rides], ["Time", hm(t.time_s)], ["TSS", fmt(t.tss)], ["Work", `${fmt(t.work_kj / 1000, 1)} MJ`]]
    .map(([k, v]) => `<div><dt>${k}</dt><dd>${v}</dd></div>`).join("");
  renderNow(d.pmc);
  makePmc(d.pmc);
  renderCalendar();
  makeWeekly(d.weeks);
  drawVt1Trend();
}

// ---- today's fitness, fatigue and form, and what the form means
function formWord(tsb) {
  if (tsb > 25) return "very fresh: fitness may be slipping";
  if (tsb > 5) return "fresh: good for a race or a test";
  if (tsb >= -10) return "neutral";
  if (tsb >= -30) return "productive training load";
  return "very high load: plan some rest";
}
function renderNow(p) {
  const n = p.days.length;
  if (!n) { $("pmc-now").innerHTML = `<p class="empty-note">No rides yet.</p>`; return; }
  const i = n - 1, ctl = p.ctl[i], atl = p.atl[i], tsb = ctl - atl;       // form going into tomorrow
  const since = p.days.length < 42 ? `<p class="note">Fitness builds over about six weeks of riding, so it reads low for now (${n} ${n === 1 ? "day" : "days"} of history).</p>` : "";
  $("pmc-now").innerHTML =
    `<div class="now"><span class="lab">Fitness</span><span class="num">${fmt(ctl)}</span></div>` +
    `<div class="now"><span class="lab">Fatigue</span><span class="num">${fmt(atl)}</span></div>` +
    `<div class="now"><span class="lab">Form</span><span class="num">${tsb > 0 ? "+" : ""}${fmt(tsb)}</span><span class="word">${formWord(tsb)}</span></div>` + since;
}

// ---- the fitness / fatigue / form chart
function makePmc(p) {
  if (PROG.plots.pmc) { PROG.plots.pmc.destroy(); delete PROG.plots.pmc; }
  const el = $("pmc-chart");
  if (!p.days.length) return;
  const lcd = THEME === "lcd";
  const x = p.days.map((iso) => dayOf(iso).getTime() / 1000);
  const pos = p.tsb.map((v) => (v >= 0 ? v : null)), neg = p.tsb.map((v) => (v < 0 ? v : null));
  const axisFont = "12px Bahnschrift, 'Segoe UI', sans-serif";
  const bars = uPlot.paths.bars({ size: [0.7, 12] });
  // placed apart like the ride graphs: form's zero line at a quarter of the height (its bars
  // between the bottom and the middle), fitness and fatigue in the upper part
  const loadMax = Math.max(20, ...p.ctl, ...p.atl) * 1.08;
  const formAbs = Math.max(10, ...p.tsb.map(Math.abs)) * 1.1;
  const range = () => {
    const last = x[x.length - 1] + 86400 * 0.5;
    const first = PROG.range ? Math.max(x[0] - 86400 * 0.5, last - PROG.range * 86400) : x[0] - 86400 * 0.5;
    return [first, Math.max(last, first + 14 * 86400)];
  };
  const readout = (u) => {
    const i = u.cursor.idx, out = $("pmc-readout");
    if (i == null || u.cursor.left < 0) { out.textContent = ""; return; }
    out.innerHTML = `${SHORT_DAY.format(dayOf(p.days[i]))} &nbsp; fitness <b>${fmt(p.ctl[i])}</b> &nbsp; fatigue <b>${fmt(p.atl[i])}</b>` +
      ` &nbsp; form <b>${p.tsb[i] > 0 ? "+" : ""}${fmt(p.tsb[i])}</b> &nbsp; TSS <b>${fmt(p.tss[i])}</b>`;
  };
  const zero = (u) => {                                           // form's zero line
    const y = Math.round(u.valToPos(0, "form", true)) + 0.5;
    u.ctx.save(); u.ctx.strokeStyle = blend(C.fg, C.panel, 0.7); u.ctx.lineWidth = devicePixelRatio || 1;
    u.ctx.beginPath(); u.ctx.moveTo(u.bbox.left, y); u.ctx.lineTo(u.bbox.left + u.bbox.width, y); u.ctx.stroke(); u.ctx.restore();
  };
  PROG.plots.pmc = new uPlot({
    width: el.clientWidth, height: el.clientHeight, legend: { show: false }, padding: [24, 8, 0, 0],
    cursor: { drag: { x: false, y: false, setScale: false }, y: false, points: { show: false } },
    scales: { x: { time: true, range }, load: { range: () => [-0.55 * loadMax, loadMax] },
              form: { range: () => [-formAbs, 3 * formAbs] } },
    axes: [
      { stroke: C.dim, font: axisFont, grid: { stroke: C.grid, width: 1 }, ticks: { show: false }, space: 70,
        values: (u, v) => v.map((s) => SHORT_DAY.format(new Date(s * 1000))) },
      { scale: "load", stroke: blend(C.power, C.panel, 0.2), font: axisFont, size: 44, ticks: { show: false },
        grid: { stroke: C.grid, width: 1 }, filter: (u, v) => v.map((n) => (n >= 0 ? n : null)) },   // labels only where it lives
      { scale: "form", side: 1, stroke: C.dim, font: axisFont, size: 44, ticks: { show: false }, grid: { show: false },
        filter: (u, v) => v.map((n) => (n <= formAbs ? n : null)), values: (u, v) => v.map((n) => (n == null ? "" : (n > 0 ? "+" : "") + n)) },
    ],
    series: [{},
      { scale: "form", stroke: "transparent", width: 0, paths: bars, points: { show: false },
        fill: lcd ? dither : blend(C.good, C.panel, 0.45) },                                                    // fresh
      { scale: "form", stroke: "transparent", width: 0, paths: bars, points: { show: false },
        fill: lcd ? blend(C.fg, C.panel, 0.55) : blend(C.warn, C.panel, 0.45) },                                // tired
      { scale: "load", stroke: lcd ? C.fg : C.hr, width: lcd ? 1.25 : 1.75, points: { show: false },
        ...(lcd ? { dash: [4, 3] } : {}) },                                                                     // fatigue
      { scale: "load", stroke: C.power, width: lcd ? 3 : 2.75, points: { show: false } }],                     // fitness
    hooks: { drawClear: [zero], setCursor: [readout] },
  }, [x, pos, neg, p.atl, p.ctl], el);
  if (!el._observed) {
    el._observed = true;
    new ResizeObserver(() => PROG.plots.pmc?.setSize({ width: el.clientWidth, height: el.clientHeight })).observe(el);
  }
}
for (const b of $("pmc-range").querySelectorAll("button")) {
  b.classList.toggle("on", +b.dataset.days === PROG.range);
  b.addEventListener("click", () => {
    PROG.range = +b.dataset.days; store.set("pmcRange", PROG.range);
    for (const o of $("pmc-range").querySelectorAll("button")) o.classList.toggle("on", o === b);
    if (PROG.data) makePmc(PROG.data.pmc);
    b.blur();
  });
}

// ---- the month calendar: rides per day (click one to open it), totals per week
function renderCalendar() {
  const d = PROG.data, m = PROG.month, today = d.today;
  $("cal-title").textContent = MONTH.format(m);
  const first = new Date(m.getFullYear(), m.getMonth(), 1);
  const start = new Date(first); start.setDate(1 - ((first.getDay() + 6) % 7));    // back to Monday
  const tssMax = Math.max(100, ...Object.values(d.days).map((rs) => rs.reduce((a, r) => a + (r.tss || 0), 0)));
  const cells = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun", "Week"].map((h) => `<div class="cal-h">${h}</div>`);
  const day = new Date(start);
  for (let w = 0; w < 6; w++) {
    let wkTime = 0, wkTss = 0;
    for (let k = 0; k < 7; k++) {
      const iso = isoOf(day), rides = d.days[iso] || [];
      const tss = rides.reduce((a, r) => a + (r.tss || 0), 0);
      wkTime += rides.reduce((a, r) => a + (r.ride_s || 0), 0); wkTss += tss;
      const out = day.getMonth() !== m.getMonth();
      const shade = rides.length ? Math.min(1, 0.25 + 0.75 * tss / tssMax) : 0;
      cells.push(`<div class="cal-d${out ? " out" : ""}${iso === today ? " today" : ""}${rides.length ? " has" : ""}" style="--load:${shade.toFixed(2)}">` +
        `<span class="dn">${day.getDate()}</span>` +
        rides.map((r) => `<button class="chip" data-id="${r.id}" title="Open this ride">${hm(r.ride_s)}${r.tss != null ? ` · ${fmt(r.tss)}` : ""}</button>`).join("") +
        `</div>`);
      day.setDate(day.getDate() + 1);
    }
    cells.push(`<div class="cal-w">${wkTime ? `<b>${hm(wkTime)}</b><span>${fmt(wkTss)} TSS</span>` : ""}</div>`);
    if (day.getMonth() !== m.getMonth() && w >= 3) break;          // no empty trailing week
  }
  const grid = $("cal-grid");
  grid.innerHTML = cells.join("");
  for (const c of grid.querySelectorAll(".chip")) c.addEventListener("click", () => act("open", c.dataset.id));
}
$("cal-prev").addEventListener("click", () => { PROG.month = new Date(PROG.month.getFullYear(), PROG.month.getMonth() - 1, 1); renderCalendar(); });
$("cal-next").addEventListener("click", () => { PROG.month = new Date(PROG.month.getFullYear(), PROG.month.getMonth() + 1, 1); renderCalendar(); });
$("cal-today").addEventListener("click", (e) => { PROG.month = dayOf(PROG.data.today); renderCalendar(); e.currentTarget.blur(); });

// ---- weekly totals: the last 12 weeks
const WK = { time_s: ["h", (v) => v / 3600, (v) => hm(v * 3600)], tss: ["TSS", (v) => v, (v) => fmt(v)], work_kj: ["kJ", (v) => v, (v) => fmt(v)] };
function makeWeekly(weeks) {
  if (PROG.plots.wk) { PROG.plots.wk.destroy(); delete PROG.plots.wk; }
  const el = $("wk-chart"), lcd = THEME === "lcd", [unit, conv, show] = WK[PROG.metric];
  const x = weeks.map((w) => dayOf(w.start).getTime() / 1000), y = weeks.map((w) => conv(w[PROG.metric]));
  const axisFont = "12px Bahnschrift, 'Segoe UI', sans-serif";
  const readout = (u) => {
    const i = u.cursor.idx, out = $("wk-readout");
    if (i == null || u.cursor.left < 0) { out.textContent = ""; return; }
    const w = weeks[i];
    out.innerHTML = `week of ${SHORT_DAY.format(dayOf(w.start))} &nbsp; <b>${w.rides}</b> ${w.rides === 1 ? "ride" : "rides"} &nbsp; <b>${hm(w.time_s)}</b>` +
      ` &nbsp; <b>${fmt(w.tss)}</b> TSS &nbsp; <b>${fmt(w.work_kj)}</b> kJ`;
  };
  PROG.plots.wk = new uPlot({
    width: el.clientWidth, height: el.clientHeight, legend: { show: false }, padding: [12, 8, 0, 0],
    cursor: { drag: { x: false, y: false, setScale: false }, y: false, points: { show: false } },
    scales: { x: { time: true, range: () => [x[0] - 3.5 * 86400, x[x.length - 1] + 3.5 * 86400] },
              y: { range: () => [0, Math.max(unit === "h" ? 1 : 50, ...y) * 1.15] } },
    axes: [
      { stroke: C.dim, font: axisFont, grid: { show: false }, ticks: { show: false }, space: 60,
        values: (u, v) => v.map((s) => SHORT_DAY.format(new Date(s * 1000))) },
      { stroke: C.dim, font: axisFont, size: 44, ticks: { show: false }, grid: { stroke: C.grid, width: 1 },
        values: (u, v) => v.map((n) => (unit === "h" ? `${n}h` : n)) },
    ],
    series: [{}, { stroke: lcd ? C.fg : C.power, width: lcd ? 1 : 0, points: { show: false },
                   fill: lcd ? dither : blend(C.power, C.panel, 0.25), paths: uPlot.paths.bars({ size: [0.62, 40] }) }],
    hooks: { setCursor: [readout] },
  }, [x, y], el);
  if (!el._observed) {
    el._observed = true;
    new ResizeObserver(() => PROG.plots.wk?.setSize({ width: el.clientWidth, height: el.clientHeight })).observe(el);
  }
  const [last, prev] = [weeks[weeks.length - 1], weeks[weeks.length - 2]];
  $("wk-note").innerHTML = `This week <b>${show(conv(last[PROG.metric]))}</b> ${unit === "h" ? "" : unit}` +
    (prev ? ` &nbsp;·&nbsp; last week <b>${show(conv(prev[PROG.metric]))}</b> ${unit === "h" ? "" : unit}` : "");
}
for (const b of $("wk-metric").querySelectorAll("button")) {
  b.classList.toggle("on", b.dataset.m === PROG.metric);
  b.addEventListener("click", () => {
    PROG.metric = b.dataset.m; store.set("wkMetric", PROG.metric);
    for (const o of $("wk-metric").querySelectorAll("button")) o.classList.toggle("on", o === b);
    if (PROG.data) makeWeekly(PROG.data.weeks);
    b.blur();
  });
}

// ---- VT1 ride by ride (vt1.py): a dot with its 80 % interval where alpha 1 crossed 0.75, a
// triangle where the ride only gave a bound (pointing the way VT1 lies), a bar for "somewhere in
// this gap". Click a mark to open the ride. One widget, on Statistics and on Home; they share the
// heart rate / power choice.
const VT1T = { by: store.get("vt1By", "hr"), all: [] };
function vt1Trend(ids, height) {
  const w = { ids, height, marks: [], data: null };
  for (const b of $(ids.seg).querySelectorAll("button")) {
    b.classList.toggle("on", b.dataset.by === VT1T.by);
    b.addEventListener("click", () => {
      VT1T.by = b.dataset.by; store.set("vt1By", VT1T.by);
      for (const x of VT1T.all) {
        for (const o of $(x.ids.seg).querySelectorAll("button")) o.classList.toggle("on", o.dataset.by === VT1T.by);
        if (x.data) drawVt1Into(x);
      }
    });
  }
  const hitAt = (ev) => {
    const r = ev.currentTarget.getBoundingClientRect(), mx = ev.clientX - r.left, my = ev.clientY - r.top;
    return w.marks.find((m) => Math.abs(m.x - mx) < 9 && Math.abs(m.y - my) < 12);
  };
  $(ids.chart).addEventListener("click", (ev) => { const h = hitAt(ev); if (h && h.id) act("open", h.id); });
  $(ids.chart).addEventListener("mousemove", (ev) => { ev.currentTarget.style.cursor = hitAt(ev) ? "pointer" : ""; });
  w.draw = (data) => { w.data = data; drawVt1Into(w); };
  VT1T.all.push(w);
  return w;
}
function drawVt1Into(w) {
  const d = w.data, cv = $(w.ids.chart), dpr = devicePixelRatio || 1, lcd = THEME === "lcd";
  const by = VT1T.by, unit = by === "hr" ? "bpm" : "W";
  const pts = (d.vt1 || []).filter((r) => r[by]);
  const wd = cv.parentElement.clientWidth, ht = w.height;
  if (!wd) return;
  cv.style.width = wd + "px"; cv.style.height = ht + "px";
  cv.width = Math.round(wd * dpr); cv.height = Math.round(ht * dpr);
  const ctx = cv.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, wd, ht);
  w.marks = [];
  const crosses = pts.filter((r) => r[by].kind === "cross");
  const last = crosses[crosses.length - 1];
  $(w.ids.readout).innerHTML = last ? `latest <b>${fmt(last[by].value)}</b> ${unit} (${SHORT_DAY.format(dayOf(last.day))})` : "";
  $(w.ids.note).textContent = !pts.length
    ? "Each ride with a heart-rate strap and α1 adds a point here. A crossing needs a steady block hard enough to take α1 under 0.75."
    : `${crosses.length} of ${pts.length} rides crossed 0.75; the others only show which side of it they stayed.`;
  if (!pts.length) return;
  const vals = pts.flatMap((r) => [r[by].value, r[by].value2, r[by].lo, r[by].hi].filter((v) => v != null));
  const step = by === "hr" ? 5 : 25;
  const y0 = Math.floor((Math.min(...vals) - step) / step) * step, y1 = Math.ceil((Math.max(...vals) + step) / step) * step;
  const t1 = dayOf(d.today).getTime(), t0 = Math.min(dayOf(pts[0].day).getTime(), t1 - 28 * 86400e3) - 2 * 86400e3;
  const ml = 40, mr = 14, mt = 10, mb = 24, W = wd - ml - mr, H = ht - mt - mb;
  const X = (t) => ml + ((t - t0) / (t1 + 86400e3 - t0)) * W, Y = (v) => mt + H - ((v - y0) / (y1 - y0)) * H;
  ctx.font = "11px Bahnschrift, 'Segoe UI', sans-serif";
  ctx.strokeStyle = C.grid; ctx.fillStyle = C.dim; ctx.lineWidth = 1; ctx.textAlign = "right";
  const ystep = (y1 - y0) / step > 6 ? step * 2 : step;
  for (let v = y0; v <= y1; v += ystep) {
    ctx.beginPath(); ctx.moveTo(ml, Y(v)); ctx.lineTo(ml + W, Y(v)); ctx.stroke();
    ctx.fillText(v, ml - 6, Y(v) + 4);
  }
  ctx.textAlign = "center";                                          // a date label every week or so
  const days = (t1 - t0) / 86400e3, every = days > 120 ? 28 : days > 50 ? 14 : 7;
  for (let t = dayOf(d.today).getTime(); t >= t0; t -= every * 86400e3) ctx.fillText(SHORT_DAY.format(new Date(t)), X(t + 43200e3), mt + H + 16);
  const ink = lcd ? C.fg : C.a1;
  for (const r of pts) {
    const e = r[by], x = X(dayOf(r.day).getTime() + 43200e3);
    ctx.strokeStyle = ctx.fillStyle = ink; ctx.lineWidth = 1.5;
    if (e.kind === "cross") {
      if (e.lo != null) { ctx.beginPath(); ctx.moveTo(x, Y(e.lo)); ctx.lineTo(x, Y(e.hi)); ctx.stroke(); }
      ctx.beginPath(); ctx.arc(x, Y(e.value), 4.5, 0, 2 * Math.PI); ctx.fill();
    } else if (e.kind === "between") {
      ctx.setLineDash([2, 3]); ctx.beginPath(); ctx.moveTo(x, Y(e.value)); ctx.lineTo(x, Y(e.value2)); ctx.stroke(); ctx.setLineDash([]);
      for (const v of [e.value, e.value2]) { ctx.beginPath(); ctx.moveTo(x - 4, Y(v)); ctx.lineTo(x + 4, Y(v)); ctx.stroke(); }
    } else {
      const up = e.kind === "above", y = Y(e.value), k = up ? -1 : 1;  // a bound: an open triangle pointing the way VT1 lies
      ctx.beginPath(); ctx.moveTo(x, y + k * 6); ctx.lineTo(x - 5, y - k * 3); ctx.lineTo(x + 5, y - k * 3); ctx.closePath(); ctx.stroke();
    }
    w.marks.push({ x, y: Y(e.value), id: r.id });
  }
}
const VT1_STATS = vt1Trend({ chart: "vt1t-chart", readout: "vt1t-readout", note: "vt1t-note", seg: "vt1t-by" }, 240);
function drawVt1Trend() { if (PROG.data) VT1_STATS.draw(PROG.data); }
