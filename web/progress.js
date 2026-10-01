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
  if (PROG.data && HOME.efFtp !== s.config.ftp) { HOME.efFtp = s.config.ftp; drawEftp(PROG.data, s); }   // FTP changed
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
  drawChecks();
  drawEftp(d, S.snap);
  HOME.efFtp = S.snap?.config.ftp;
  drawEfficiency();
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

// ---- charts over time with lanes: one date axis, a lane per measure (each with its own scale and
// gridlines, stacked, never two scales on one plot), hover details and click-to-open on every mark
function laneChart(cv, height, lanes, t0, t1) {
  const dpr = devicePixelRatio || 1, wd = cv.parentElement.clientWidth;
  if (!wd) return null;
  cv.style.width = wd + "px"; cv.style.height = height + "px";
  cv.width = Math.round(wd * dpr); cv.height = Math.round(height * dpr);
  const ctx = cv.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, wd, height);
  const ml = 46, mr = 14, mt = 8, mb = 22, gap = 30, W = wd - ml - mr;
  const total = height - mt - mb - gap * (lanes.length - 1), wsum = lanes.reduce((a, l) => a + l.weight, 0);
  const X = (t) => ml + ((t - t0) / (t1 + 86400e3 - t0)) * W;
  ctx.font = "11px Bahnschrift, 'Segoe UI', sans-serif";
  let top = mt;
  for (const l of lanes) {
    const h = (total * l.weight) / wsum, y0 = l.y0, y1 = l.y1, base = top + h;
    l.Y = (v) => base - ((v - y0) / (y1 - y0)) * h;
    l.top = top; l.h = h;
    ctx.strokeStyle = C.grid; ctx.fillStyle = C.dim; ctx.lineWidth = 1; ctx.textAlign = "right";
    for (let v = Math.ceil(y0 / l.step) * l.step; v <= y1 + 1e-9; v += l.step) {
      ctx.beginPath(); ctx.moveTo(ml, l.Y(v)); ctx.lineTo(ml + W, l.Y(v)); ctx.stroke();
      ctx.fillText(l.fmt(v), ml - 6, l.Y(v) + 4);
    }
    ctx.textAlign = "left"; ctx.fillStyle = C.dim;                   // the lane's name, top left
    ctx.fillText(l.label, ml + 4, top + 11);
    top = base + gap;
  }
  ctx.textAlign = "center"; ctx.fillStyle = C.dim;                  // a date label every week or so
  const days = (t1 - t0) / 86400e3, every = days > 120 ? 28 : days > 50 ? 14 : 7;
  for (let t = t1; t >= t0; t -= every * 86400e3) ctx.fillText(SHORT_DAY.format(new Date(t)), X(t + 43200e3), height - 5);
  return { ctx, X, lanes, ml, W };
}
const niceRange = (vals, step, pad) => [Math.floor((Math.min(...vals) - pad) / step) * step, Math.ceil((Math.max(...vals) + pad) / step) * step];

// hover details and click-to-open; marks = [{x, y, html, id}] kept on the chart's own object
function chartTips(cv, getMarks) {
  const wrap = cv.parentElement;
  wrap.style.position = "relative";
  const tip = document.createElement("div");
  tip.className = "ctip"; tip.hidden = true;
  wrap.append(tip);
  const hit = (ev) => {
    const r = cv.getBoundingClientRect(), mx = ev.clientX - r.left, my = ev.clientY - r.top;
    let best = null, bd = 12;
    for (const m of getMarks()) { const d = Math.hypot(m.x - mx, m.y - my); if (d < bd) { bd = d; best = m; } }
    return best;
  };
  cv.addEventListener("mousemove", (ev) => {
    const m = hit(ev);
    cv.style.cursor = m && m.id ? "pointer" : "";
    tip.hidden = !m;
    if (!m) return;
    tip.innerHTML = m.html;
    const left = Math.min(Math.max(4, m.x + 12), wrap.clientWidth - tip.offsetWidth - 4);
    tip.style.left = left + "px"; tip.style.top = Math.max(0, m.y - tip.offsetHeight - 8) + "px";
  });
  cv.addEventListener("mouseleave", () => { tip.hidden = true; });
  cv.addEventListener("click", (ev) => { const m = hit(ev); if (m && m.id) act("open", m.id); });
}

// ---- thresholds over time (vt1.py): VT1 (alpha 1 = 0.75) and VT2 (0.5, near FTP) of every ride, a
// heart-rate lane over a power lane. Dots / squares with 80 % intervals where a ride crossed; open
// (VT1) or filled (VT2) triangles where it only gave a bound, pointing the way the threshold lies;
// a dashed bar for "somewhere in this gap". On Statistics and on Home.
const VT1T = { all: [] };
function vt1Trend(ids, height) {
  const w = { ids, height, marks: [], data: null };
  chartTips($(ids.chart), () => w.marks);
  w.draw = (data) => { w.data = data; drawVt1Into(w); };
  VT1T.all.push(w);
  return w;
}
function thrText(e, unit) {
  if (!e) return "--";
  if (e.kind === "cross") return `${fmt(e.value)} ${unit}${e.lo != null ? ` (${fmt(e.lo)}–${fmt(e.hi)})` : ""}`;
  if (e.kind === "between") return `between ${fmt(e.value)} and ${fmt(e.value2)} ${unit}`;
  return `${e.kind} ${fmt(e.value)} ${unit}`;
}
function drawVt1Into(w) {
  const d = w.data, lcd = THEME === "lcd", rows = d.vt1 || [];
  const S2 = [{ name: "VT1", ink: lcd ? C.fg : C.a1, get: (r, by) => r[by] },
              { name: "VT2", ink: lcd ? C.fg : C.hr, square: true, get: (r, by) => r.vt2 && r.vt2[by] }];
  const latest = (sr, by, unit) => {                               // the newest ride's result
    const got = rows.map((r) => sr.get(r, by)).filter(Boolean), e = got[got.length - 1];
    if (!e) return "--";
    return e.kind === "cross" ? `<b>${fmt(e.value)}</b> ${unit}` : e.kind === "between"
      ? `<b>${fmt(e.value)}–${fmt(e.value2)}</b> ${unit}` : `${e.kind} <b>${fmt(e.value)}</b> ${unit}`;
  };
  $(w.ids.readout).innerHTML = rows.length ? S2.map((sr) =>
    `<span class="tkey"><i class="${sr.square ? "sq" : "dot"}" style="background:${sr.ink}"></i>${sr.name} ${latest(sr, "hr", "bpm")} · ${latest(sr, "power", "W")}</span>`).join("") : "";
  const nCross = rows.filter((r) => r.hr && r.hr.kind === "cross").length;
  $(w.ids.note).textContent = !rows.length
    ? "Each ride with a heart-rate strap and α1 adds a point. VT1 is where α1 crosses 0.75, VT2 where it reaches 0.5 (near FTP): a steady block hard enough shows them."
    : `VT1 (α1 0.75) and VT2 (α1 0.5, close to FTP). ${nCross} of ${rows.length} rides crossed VT1; triangles are rides that stayed below, pointing to where the threshold lies. Hover for details, click to open the ride.`;
  w.marks = [];
  if (!rows.length) { laneChart($(w.ids.chart), w.height, [], 0, 1); return; }
  const lane = (by, step, pad, unit) => {
    const v = rows.flatMap((r) => S2.map((sr) => sr.get(r, by)).filter(Boolean).flatMap((e) => [e.value, e.value2, e.lo, e.hi].filter((x) => x != null)));
    const [y0, y1] = v.length ? niceRange(v, step, pad) : [0, step];
    return { by, unit, label: by === "hr" ? "heart rate, bpm" : "power, W", weight: 1, y0, y1,
             step: (y1 - y0) / step > 4 ? step * 2 : step, fmt: (x) => `${x}` };
  };
  const first = Math.min(...rows.map((r) => dayOf(r.day).getTime()));
  const t1 = dayOf(d.today).getTime(), t0 = Math.min(first, t1 - 28 * 86400e3) - 2 * 86400e3;
  const ch = laneChart($(w.ids.chart), w.height, [lane("hr", 5, 3, "bpm"), lane("power", 25, 10, "W")], t0, t1);
  if (!ch) return;
  const { ctx, X } = ch;
  for (const l of ch.lanes) {
    S2.forEach((sr, si) => {
      for (const r of rows) {
        const e = sr.get(r, l.by);
        if (!e) continue;
        const x = X(dayOf(r.day).getTime() + 43200e3) + (si ? 4 : -4), Y = l.Y;
        ctx.strokeStyle = ctx.fillStyle = sr.ink; ctx.lineWidth = 1.5;
        if (e.kind === "cross") {
          if (e.lo != null) { ctx.beginPath(); ctx.moveTo(x, Y(e.lo)); ctx.lineTo(x, Y(e.hi)); ctx.stroke(); }
          ctx.beginPath();
          sr.square ? ctx.rect(x - 4, Y(e.value) - 4, 8, 8) : ctx.arc(x, Y(e.value), 4.5, 0, 2 * Math.PI);
          ctx.fill();
        } else if (e.kind === "between") {
          ctx.setLineDash([2, 3]); ctx.beginPath(); ctx.moveTo(x, Y(e.value)); ctx.lineTo(x, Y(e.value2)); ctx.stroke(); ctx.setLineDash([]);
          for (const v of [e.value, e.value2]) { ctx.beginPath(); ctx.moveTo(x - 4, Y(v)); ctx.lineTo(x + 4, Y(v)); ctx.stroke(); }
        } else {
          const y = Y(e.value), k = e.kind === "above" ? -1 : 1;
          ctx.beginPath(); ctx.moveTo(x, y + k * 6); ctx.lineTo(x - 5, y - k * 3); ctx.lineTo(x + 5, y - k * 3); ctx.closePath();
          sr.square ? ctx.fill() : ctx.stroke();
        }
        w.marks.push({ x, y: Y(e.value), id: r.id,
          html: `<b>${sr.name}</b> · ${SHORT_DAY.format(dayOf(r.day))}<br>${thrText(sr.get(r, "hr"), "bpm")}<br>${thrText(sr.get(r, "power"), "W")}` +
                (r.beats_pct != null && r.beats_pct < 95 ? `<br><span class="dim">${fmt(r.beats_pct)}% of beats: α1 reads high</span>` : "") });
      }
    });
  }
}
const VT1_STATS = vt1Trend({ chart: "vt1t-chart", readout: "vt1t-readout", note: "vt1t-note" }, 300);
function drawVt1Trend() { if (PROG.data) VT1_STATS.draw(PROG.data); }

// ---- aerobic efficiency ride by ride (ride_summary._efficiency): a lane of power per heartbeat in
// steady Z2 (with the mean of the last 5 rides as the trend) over a lane of Pw:HR decoupling bars
// (durability; 5 % and 10 % marked). Hover a ride for its numbers, click to open it.
const EFF = { marks: [] };
chartTips($("eff-chart"), () => EFF.marks);
function drawEfficiency() {
  const d = PROG.data;
  if (!d) return;
  const lcd = THEME === "lcd", all = d.efficiency || [];
  const pts = all.filter((r) => r.ef != null), decs = all.filter((r) => r.decoupling != null);
  const last5 = pts.slice(-5), mean = last5.length ? last5.reduce((a, r) => a + r.ef, 0) / last5.length : null;
  const d5 = decs.slice(-5), dmean = d5.length ? d5.reduce((a, r) => a + r.decoupling, 0) / d5.length : null;
  $("eff-readout").innerHTML = (mean != null ? `last 5: <b>${mean.toFixed(2)}</b> W per bpm at ~${fmt(last5.reduce((a, r) => a + r.power, 0) / last5.length)} W` : "") +
    (dmean != null ? ` · decoupling <b>${dmean > 0 ? "+" : ""}${dmean.toFixed(1)}</b>%` : "");
  $("eff-note").textContent = "Top: watts per heartbeat in steady Z2 (56–75 % of FTP, after a 10-min warm-up); rising means your aerobic fitness is, while heat, fatigue and dehydration pull a ride down. " +
    "Bottom: how much heart rate drifted against power from the first half of the ride to the second; under 5 % is durable, over 10 % went past your aerobic base. Hover a ride for its numbers.";
  EFF.marks = [];
  if (!all.length) { laneChart($("eff-chart"), 300, [], 0, 1); return; }
  const efv = pts.map((r) => r.ef), dv = decs.map((r) => r.decoupling);
  const [e0, e1] = efv.length ? niceRange(efv, 0.05, 0.03) : [0.9, 1.1];
  const dmax = Math.max(12, ...dv.map((v) => Math.ceil(v / 2) * 2 + 2)), dmin = Math.min(0, ...dv.map((v) => Math.floor(v / 2) * 2));
  const first = Math.min(...all.map((r) => dayOf(r.day).getTime()));
  const t1 = dayOf(d.today).getTime(), t0 = Math.min(first, t1 - 28 * 86400e3) - 2 * 86400e3;
  const ch = laneChart($("eff-chart"), 300, [
    { label: "power per heartbeat, W/bpm", weight: 1.5, y0: e0, y1: e1, step: e1 - e0 > 0.3 ? 0.1 : 0.05, fmt: (v) => v.toFixed(2) },
    { label: "decoupling, %", weight: 1, y0: dmin, y1: dmax, step: dmax - dmin > 16 ? 5 : 2, fmt: (v) => `${Math.round(v)}` },
  ], t0, t1);
  if (!ch) return;
  const { ctx, X } = ch, [L1, L2] = ch.lanes;
  const tip = (r) => {
    const hr = r.ef && r.power ? r.power / r.ef : null;
    return `<b>${SHORT_DAY.format(dayOf(r.day))}</b>` +
      (r.ef != null ? `<br>${r.ef.toFixed(2)} W per bpm: ${fmt(r.power)} W at ${fmt(hr)} bpm<br>${fmt(r.minutes)} min of steady Z2` : "<br>no steady Z2 to compare") +
      (r.decoupling != null ? `<br>decoupling ${r.decoupling > 0 ? "+" : ""}${r.decoupling.toFixed(1)}%` : "");
  };
  // lane 2: the 5 % and 10 % lines, then a bar per ride
  ctx.setLineDash([4, 4]); ctx.lineWidth = 1;
  for (const [v, c] of [[5, lcd ? C.fg : C.warn], [10, lcd ? C.fg : C.bad]]) {
    ctx.strokeStyle = blend(c, C.panel, 0.3);
    ctx.beginPath(); ctx.moveTo(ch.ml, L2.Y(v)); ctx.lineTo(ch.ml + ch.W, L2.Y(v)); ctx.stroke();
  }
  ctx.setLineDash([]);
  const bw = Math.max(4, Math.min(14, ch.W / ((t1 - t0) / 86400e3) * 0.6));
  for (const r of decs) {
    const x = X(dayOf(r.day).getTime() + 43200e3), v = r.decoupling, y = L2.Y(Math.max(0, v)), y0 = L2.Y(Math.min(0, v));
    ctx.fillStyle = lcd ? C.fg : blend(C.fg, C.panel, v >= 10 ? 0.1 : v >= 5 ? 0.3 : 0.55);
    ctx.fillRect(x - bw / 2, y, bw, Math.max(1, y0 - y));
    EFF.marks.push({ x, y, id: r.id, html: tip(r) });
  }
  // lane 1: the trend, then a dot per ride
  ctx.strokeStyle = lcd ? C.fg : blend(C.good, C.panel, 0.3); ctx.lineWidth = 2; ctx.beginPath();
  pts.forEach((r, i) => {
    const win = pts.slice(Math.max(0, i - 4), i + 1), m = win.reduce((a, x) => a + x.ef, 0) / win.length;
    const x = X(dayOf(r.day).getTime() + 43200e3);
    i ? ctx.lineTo(x, L1.Y(m)) : ctx.moveTo(x, L1.Y(m));
  });
  ctx.stroke();
  for (const r of pts) {
    const x = X(dayOf(r.day).getTime() + 43200e3), y = L1.Y(r.ef);
    ctx.beginPath(); ctx.arc(x, y, 4.5, 0, 2 * Math.PI);
    ctx.fillStyle = lcd ? C.fg : C.good; ctx.fill();
    EFF.marks.push({ x, y, id: r.id, html: tip(r) });
  }
}

// ---- the Fitness checks (training.fitness_checks): heart rate at the same watts (lower = fitter;
// corrected to 20 C once the heat model has learned your bpm per degree), alpha 1 at the middle step
// (higher = fresher, more aerobic) and heart-rate recovery in 60 s (more = fitter / fresher)
const FC = { marks: [] };
chartTips($("fc-chart"), () => FC.marks);
function drawChecks() {
  const d = PROG.data, f = d && d.fitness_checks;
  if (!f) return;
  const cs = f.checks, lcd = THEME === "lcd", heat = f.heat;
  const last = cs[cs.length - 1], prev = cs[cs.length - 2];
  const hrOf = (c) => (c.hr_ref_20c ?? c.hr_ref);
  $("fc-readout").innerHTML = !last ? "" : `latest: <b>${fmt(hrOf(last))}</b> bpm at ${fmt(f.ref_w)} W${heat ? " (20 °C)" : ""}` +
    (prev ? ` · ${Math.abs(hrOf(last) - hrOf(prev)) < 0.5 ? "same as" : `${hrOf(last) < hrOf(prev) ? "−" : "+"}${fmt(Math.abs(hrOf(last) - hrOf(prev)))} bpm since`} the one before` : "");
  const n = cs.length, withT = cs.filter((c) => c.temp_c != null);
  $("fc-note").textContent = !n
    ? "Ride Threshold steps (Workouts page) once a week, rested, with the room temperature set: the same watts every time, so heart rate, α1 and recovery show your fitness and freshness, and the same ride gives VT1 and VT2."
    : `Heart rate at ${fmt(f.ref_w)} W from each check's steps up to 80 % FTP (lower = fitter), α1 at the same watts (higher = fresher), recovery in the first minute after the 100 % step (more = fitter). ` +
      (heat ? `Heat model: ${heat.bpm_per_c.toFixed(1)} bpm per °C from ${heat.n} checks (${heat.range_c[0]}–${heat.range_c[1]} °C); the line is corrected to 20 °C.`
            : `Heat model: learning (${withT.length} of 5 checks with a temperature, needs a 3 °C spread).`);
  FC.marks = [];
  if (!n) { laneChart($("fc-chart"), 320, [], 0, 1); return; }
  const hrs = cs.flatMap((c) => [c.hr_ref, c.hr_ref_20c].filter((v) => v != null));
  const a1s = cs.map((c) => c.a1_ref).filter((v) => v != null), hrr = cs.map((c) => c.hrr60).filter((v) => v != null);
  const [h0, h1] = niceRange(hrs, 5, 3);
  const [a0, a1] = a1s.length ? niceRange(a1s, 0.25, 0.1) : [0.5, 1.5];
  const [r0, r1] = hrr.length ? niceRange(hrr, 5, 3) : [0, 40];
  const first = dayOf(cs[0].day).getTime(), t1 = dayOf(d.today).getTime(), t0 = Math.min(first, t1 - 28 * 86400e3) - 2 * 86400e3;
  const ch = laneChart($("fc-chart"), 320, [
    { label: `heart rate at ${fmt(f.ref_w)} W, bpm`, weight: 1.4, y0: h0, y1: h1, step: (h1 - h0) / 5 > 4 ? 10 : 5, fmt: (v) => `${v}` },
    { label: `α1 at ${fmt(f.ref_w)} W`, weight: 1, y0: a0, y1: a1, step: 0.25, fmt: (v) => v.toFixed(2) },
    { label: "recovery, bpm in 60 s", weight: 1, y0: r0, y1: r1, step: (r1 - r0) / 5 > 4 ? 10 : 5, fmt: (v) => `${v}` },
  ], t0, t1);
  if (!ch) return;
  const { ctx, X } = ch, [L1, L2, L3] = ch.lanes, ink = lcd ? C.fg : C.hr;
  const tip = (c) => `<b>${SHORT_DAY.format(dayOf(c.day))}</b>${c.temp_c != null ? ` · ${c.temp_c} °C${c.rh_pct != null ? `, ${c.rh_pct}%` : ""}` : ""}` +
    `<br>${fmt(c.hr_ref)} bpm at ${fmt(f.ref_w)} W${c.hr_ref_20c != null ? ` (${fmt(c.hr_ref_20c)} at 20 °C)` : ""}` +
    `<br>steps ${c.steps.map((s) => `${fmt(s.power)} W ${fmt(s.hr)} bpm`).join(" · ")}` +
    `<br>α1 ${c.a1.map((v) => fmt(v, 2)).join(" / ")}${c.hrr60 != null ? ` · recovery ${fmt(c.hrr60)} bpm` : ""}`;
  const line = (lane, vals, colour) => {
    ctx.strokeStyle = colour; ctx.lineWidth = 2; ctx.beginPath();
    let started = false;
    cs.forEach((c, i) => { const v = vals(c); if (v == null) return;
      const x = X(dayOf(c.day).getTime() + 43200e3); started ? ctx.lineTo(x, lane.Y(v)) : ctx.moveTo(x, lane.Y(v)); started = true; });
    ctx.stroke();
  };
  if (heat) line(L1, (c) => c.hr_ref_20c, lcd ? C.fg : blend(C.hr, C.panel, 0.3));
  for (const c of cs) {
    const x = X(dayOf(c.day).getTime() + 43200e3);
    const dots = [[L1, c.hr_ref, ink], [L2, c.a1_ref, lcd ? C.fg : C.a1], [L3, c.hrr60, lcd ? C.fg : C.good]];
    for (const [lane, v, col] of dots) {
      if (v == null) continue;
      ctx.beginPath(); ctx.arc(x, lane.Y(v), 4.5, 0, 2 * Math.PI);
      if (lane === L1 && heat) { ctx.strokeStyle = col; ctx.lineWidth = 1.5; ctx.stroke(); }   // raw, beside the corrected line
      else { ctx.fillStyle = col; ctx.fill(); }
      FC.marks.push({ x, y: lane.Y(v), id: c.id, html: tip(c) });
    }
  }
}
