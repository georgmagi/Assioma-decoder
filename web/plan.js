// The training plan (plan.py), to look at: the blocks with their fitness / fatigue / form forecast
// and the calendar. The plan itself is written by the AI agent (plan.json), not by hand here.
// Uses $, act, fmt, hms, C, THEME, blend, dayOf, isoOf, SHORT_DAY, hm, laneChart, chartTips,
// niceRange, drawProfile, esc, formWord, store from the other files.
"use strict";

const PLAN = { data: null, version: null, rides: null, loading: false, editing: null, marks: [] };
const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

async function showPlan(s) {
  $("pl-msg").hidden = !s.plan_msg;
  $("pl-msg").textContent = s.plan_msg || "";
  // the status text for Claude includes the morning check: a new one reloads it too
  if (PLAN.loading || (PLAN.data && S.prevScreen === "plan" && PLAN.version === s.plan_version && PLAN.rides === s.data_key
      && PLAN.morning === s.morning_version)) return;
  PLAN.version = s.plan_version; PLAN.rides = s.data_key; PLAN.morning = s.morning_version;
  PLAN.loading = true;
  try { PLAN.data = await (await fetch("/api/plan")).json(); } catch { return; } finally { PLAN.loading = false; }
  renderPlan();
}

function renderPlan() {
  const d = PLAN.data;
  if (!d) return;
  const a = dayOf(d.start), b = dayOf(d.end);
  $("pl-range").textContent = `${SHORT_DAY.format(a)} – ${SHORT_DAY.format(b)}`;
  drawPlanChart(d);
  renderPlanGrid(d);
}

// ---- the forecast: your real fitness / fatigue (top) and form (bottom) for the last 6 weeks, then
// the plan's, dashed, to the end of the block
const PLC = { marks: [] };
chartTips($("pl-chart"), () => PLC.marks);
function drawPlanChart(d) {
  const act = d.forecast.actual, fc = d.forecast.plan, lcd = THEME === "lcd";
  const from = Math.max(0, act.days.length - 42);
  const A = { days: act.days.slice(from), ctl: act.ctl.slice(from), atl: act.atl.slice(from), tsb: act.tsb.slice(from) };
  const all = (k) => A[k].concat(fc[k]);
  const days = A.days.concat(fc.days);
  if (!days.length) { laneChart($("pl-chart"), 280, [], 0, 1); return; }
  const lastF = fc.days.length - 1;
  const end = lastF >= 0 ? { ctl: fc.ctl[lastF], atl: fc.atl[lastF], tsb: fc.tsb[lastF], day: fc.days[lastF] } : null;
  $("pl-readout").innerHTML = end ? `end of ${d.blocks && d.blocks.length > 1 ? "the plan" : "block"} (${SHORT_DAY.format(dayOf(end.day))}): fitness <b>${fmt(end.ctl)}</b> · fatigue <b>${fmt(end.atl)}</b> · form <b>${end.tsb > 0 ? "+" : ""}${fmt(end.tsb)}</b>` : "";
  const today = A.days.length ? A.ctl[A.ctl.length - 1] : 0;
  $("pl-note").textContent = end ? `Fitness ${end.ctl >= today ? "+" : ""}${fmt(end.ctl - today)} by then. ${formWord(end.tsb)[0].toUpperCase() + formWord(end.tsb).slice(1)} at the end. ` +
    "Solid: what happened; dashed: the plan, if ridden as written (each workout's TSS at your current FTP)." : "Plan some days to see the forecast.";
  const [y0, y1] = niceRange(all("ctl").concat(all("atl")), 10, 3);
  const [f0, f1] = niceRange(all("tsb").concat([0]), 10, 3);
  const t0 = dayOf(days[0]).getTime(), t1 = dayOf(days[days.length - 1]).getTime();
  const ch = laneChart($("pl-chart"), 280, [
    { label: "fitness (42 d) and fatigue (7 d)", weight: 1.5, y0: Math.max(0, y0), y1, step: (y1 - y0) / 10 > 5 ? 20 : 10, fmt: (v) => `${v}` },
    { label: "form", weight: 1, y0: f0, y1: f1, step: (f1 - f0) / 10 > 5 ? 20 : 10, fmt: (v) => `${v}` },
  ], t0, t1 - 86400e3);
  if (!ch) return;
  const { ctx, X } = ch, [L1, L2] = ch.lanes;
  const x = (iso) => X(dayOf(iso).getTime());
  // the block, shaded; today as a line
  (d.blocks || [{ start: d.start, end: d.end }]).forEach((b, i) => {          // each block shaded, every other one lighter
    ctx.fillStyle = lcd ? `rgba(27,30,25,${i % 2 ? 0.03 : 0.07})` : blend(C.power, C.panel, i % 2 ? 0.96 : 0.92);
    ctx.fillRect(x(b.start), L1.top, x(b.end) - x(b.start) + 1, L2.top + L2.h - L1.top);
    ctx.fillStyle = C.dim; ctx.textAlign = "left"; ctx.font = "11px 'Barlow Semi Condensed', 'IBM Plex Sans', sans-serif";
    if (d.blocks && d.blocks.length > 1) ctx.fillText(`Block ${b.n}`, x(b.start) + 4, L1.top + 12);
  });
  if (A.days.length) {
    ctx.strokeStyle = C.dim; ctx.lineWidth = 1;
    const tx = x(A.days[A.days.length - 1]);
    ctx.beginPath(); ctx.moveTo(tx, L1.top); ctx.lineTo(tx, L2.top + L2.h); ctx.stroke();
  }
  ctx.strokeStyle = C.grid; ctx.beginPath(); ctx.moveTo(ch.ml, L2.Y(0)); ctx.lineTo(ch.ml + ch.W, L2.Y(0)); ctx.stroke();
  const line = (lane, ds, vs, colour, width, dash) => {
    if (!ds.length) return;
    ctx.strokeStyle = colour; ctx.lineWidth = width; ctx.setLineDash(dash || []);
    ctx.beginPath(); ds.forEach((dd, i) => (i ? ctx.lineTo(x(dd), lane.Y(vs[i])) : ctx.moveTo(x(dd), lane.Y(vs[i])))); ctx.stroke();
    ctx.setLineDash([]);
  };
  const join = (k) => (A.days.length ? [A.days[A.days.length - 1]].concat(fc.days) : fc.days);
  const joinV = (k) => (A.days.length ? [A[k][A[k].length - 1]].concat(fc[k]) : fc[k]);
  const cCtl = lcd ? C.fg : C.power, cAtl = lcd ? C.fg : C.hr, cTsb = lcd ? C.fg : C.good;
  line(L1, A.days, A.ctl, cCtl, 2.5); line(L1, A.days, A.atl, cAtl, 1.5);
  line(L2, A.days, A.tsb, cTsb, 2);
  line(L1, join(), joinV("ctl"), cCtl, 2.5, [6, 4]); line(L1, join(), joinV("atl"), cAtl, 1.5, [6, 4]);
  line(L2, join(), joinV("tsb"), cTsb, 2, [6, 4]);
  PLC.marks = days.map((dd, i) => {
    const ctl = all("ctl")[i], atl = all("atl")[i], tsb = all("tsb")[i], planned = i >= A.days.length;
    const pd = planned ? d.days.find((y) => y.day === dd) : null;
    return { x: x(dd), y: L1.Y(ctl), html: tipHtml(`${SHORT_DAY.format(dayOf(dd))}${planned ? ' <span class="dim">· plan</span>' : ""}`, [
      ["fitness", fmt(ctl), "", cCtl], ["fatigue", fmt(atl), "", cAtl], ["form", `${tsb > 0 ? "+" : ""}${fmt(tsb)}`, "", cTsb],
      planned && ["planned", pd && pd.name ? esc(pd.name) : fc.tss[i - A.days.length] ? "" : "rest", ""],
      planned && fc.tss[i - A.days.length] > 0 && ["", fmt(fc.tss[i - A.days.length]), "TSS"]]) };
  });
}

// ---- the calendar
function entryText(e) {
  if (!e) return "";
  if (e.rest) return "rest";
  if (e.ref) { const w = WO.list && WO.list.find((x) => x.id === e.ref); return w ? w.name : e.ref; }
  return e.name && e.name !== e.text ? `${e.name} | ${e.text}` : e.text;
}
function renderPlanGrid(d) {
  const today = isoOf(new Date());
  const head = WEEKDAYS.map((w) => `<div class="pl-h">${w}</div>`).concat(`<div class="pl-h">Week</div>`).join("");
  const html = d.blocks.map((blk) => {
    const cells = [];
    d.days.slice(blk.first, blk.first + blk.count).forEach((x, j) => {
      const dd = dayOf(x.day), isToday = x.day === today, past = x.day < today;
      const st = { done: `done ${fmt(x.done_tss)}`, missed: "missed", extra: `extra ${fmt(x.done_tss)}`, today: "today" }[x.status] || "";
      cells.push(`<div class="pl-d${isToday ? " today" : ""}${past ? " past" : ""}${x.warn ? " warn" : ""}" data-day="${x.day}"${x.warn ? ` title="${esc(x.warn)}"` : ""}>` +
        `<span class="dn">${dd.getDate()} ${SHORT_DAY.format(dd).split(" ")[1] || ""}</span>` +
        `<span class="nm${x.entry && x.entry.rest ? " rest" : ""}">${x.name ? esc(x.name) : x.entry && x.entry.rest ? "rest" : ""}</span>` +
        `${x.steps ? "<canvas></canvas>" : "<span></span>"}` +
        `<span class="mt"><span>${x.minutes ? `${x.minutes} min` : ""}${x.tss != null ? ` · ${fmt(x.tss)}` : ""}</span><span class="st ${x.status}">${x.warn ? "⚠ " : ""}${st}</span></span></div>`);
      if (j % 7 === 6) {
        const w = blk.weeks[Math.floor(j / 7)], tot = w.split.easy + w.split.moderate + w.split.hard;
        const seg = (k, cls) => (w.split[k] ? `<i class="${cls}" style="flex:${w.split[k]}"></i>` : "");
        cells.push(`<div class="pl-w"><span><b>${hm(w.minutes * 60)}</b></span><span><b>${fmt(w.tss)}</b> TSS${w.done_tss ? ` · done ${fmt(w.done_tss)}` : ""}</span>` +
          (tot ? `<div class="mix-bar">${seg("easy", "easy")}${seg("moderate", "mod")}${seg("hard", "hard")}</div>` +
                 `<span>${Math.round((100 * w.split.easy) / tot)}% easy · ${Math.round((100 * w.split.hard) / tot)}% hard</span>` : "") + `</div>`);
      }
    });
    const now = blk.start <= today && today <= blk.end, last = blk.n === d.blocks.length;
    const bhead = `<div class="pl-bhead"><h2>Block ${blk.n}</h2>` +
      `<span class="sub">${SHORT_DAY.format(dayOf(blk.start))} – ${SHORT_DAY.format(dayOf(blk.end))} · ${blk.weeks_n} weeks${now ? " · now" : blk.start > today ? "" : " · done"}</span>` +
      `<span class="readout"><b>${hm(blk.minutes * 60)}</b> · <b>${fmt(blk.tss)}</b> TSS${blk.done_tss ? ` · done ${fmt(blk.done_tss)}` : ""}</span>` +
      `</div>`;
    return `<div class="pl-block">${bhead}<div class="pl-grid">${head}${cells.join("")}</div></div>`;
  }).join("");
  const box = $("pl-grid");
  box.innerHTML = html;
  box.querySelectorAll(".pl-d").forEach((b) => {
    const x = d.days.find((y) => y.day === b.dataset.day);
    const cv = b.querySelector("canvas");
    if (cv && x.steps) drawProfile(cv, x.steps);
  });

}

window.addEventListener("resize", () => { if (!$("scr-plan").hidden) renderPlan(); });
