// The training plan (plan.py): a block of days with its fitness / fatigue / form forecast, the
// calendar (click a day to change it), a pasted plan from Claude, and the status to give Claude.
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
  if (!$("pl-start").value) {                                    // next Monday, for a new block
    const t = new Date(), add = (8 - t.getDay()) % 7 || 7;
    $("pl-start").value = isoOf(new Date(t.getFullYear(), t.getMonth(), t.getDate() + add));
  }
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
  $("pl-readout").innerHTML = end ? `end of block (${SHORT_DAY.format(dayOf(end.day))}): fitness <b>${fmt(end.ctl)}</b> · fatigue <b>${fmt(end.atl)}</b> · form <b>${end.tsb > 0 ? "+" : ""}${fmt(end.tsb)}</b>` : "";
  const today = A.days.length ? A.ctl[A.ctl.length - 1] : 0;
  $("pl-note").textContent = end ? `Fitness ${end.ctl >= today ? "+" : ""}${fmt(end.ctl - today)} over the block. ${formWord(end.tsb)[0].toUpperCase() + formWord(end.tsb).slice(1)} at the end. ` +
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
  ctx.fillStyle = lcd ? "rgba(27,30,25,0.06)" : blend(C.power, C.panel, 0.93);
  ctx.fillRect(x(d.start), L1.top, x(d.end) - x(d.start) + 1, L2.top + L2.h - L1.top);
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
    return { x: x(dd), y: L1.Y(ctl), html: `<b>${SHORT_DAY.format(dayOf(dd))}</b>${planned ? " (plan)" : ""}<br>fitness ${fmt(ctl)} · fatigue ${fmt(atl)}<br>form ${tsb > 0 ? "+" : ""}${fmt(tsb)}` +
      (planned ? `<br>${fmt(fc.tss[i - A.days.length])} TSS planned` : "") };
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
  const cells = WEEKDAYS.map((w) => `<div class="pl-h">${w}</div>`).concat(`<div class="pl-h">Week</div>`);
  d.days.forEach((x, i) => {
    const dd = dayOf(x.day), isToday = x.day === today, past = x.day < today;
    const st = { done: `done ${fmt(x.done_tss)}`, missed: "missed", extra: `extra ${fmt(x.done_tss)}`, today: "today" }[x.status] || "";
    cells.push(`<button class="pl-d${isToday ? " today" : ""}${past ? " past" : ""}" data-day="${x.day}">` +
      `<span class="dn">${dd.getDate()} ${SHORT_DAY.format(dd).split(" ")[1] || ""}</span>` +
      `<span class="nm${x.entry && x.entry.rest ? " rest" : ""}">${x.name ? esc(x.name) : x.entry && x.entry.rest ? "rest" : ""}</span>` +
      `${x.steps ? "<canvas></canvas>" : "<span></span>"}` +
      `<span class="mt"><span>${x.minutes ? `${x.minutes} min` : ""}${x.tss != null ? ` · ${fmt(x.tss)}` : ""}</span><span class="st ${x.status}">${st}</span></span></button>`);
    if (i % 7 === 6) {
      const w = d.weeks[Math.floor(i / 7)], tot = w.split.easy + w.split.moderate + w.split.hard;
      const seg = (k, cls) => (w.split[k] ? `<i class="${cls}" style="flex:${w.split[k]}"></i>` : "");
      cells.push(`<div class="pl-w"><span><b>${hm(w.minutes * 60)}</b></span><span><b>${fmt(w.tss)}</b> TSS${w.done_tss ? ` · done ${fmt(w.done_tss)}` : ""}</span>` +
        (tot ? `<div class="mix-bar">${seg("easy", "easy")}${seg("moderate", "mod")}${seg("hard", "hard")}</div>` +
               `<span>${Math.round((100 * w.split.easy) / tot)}% easy · ${Math.round((100 * w.split.hard) / tot)}% hard</span>` : "") + `</div>`);
    }
  });
  const grid = $("pl-grid");
  grid.innerHTML = cells.join("");
  grid.querySelectorAll(".pl-d").forEach((b) => {
    const x = d.days.find((y) => y.day === b.dataset.day);
    const cv = b.querySelector("canvas");
    if (cv && x.steps) drawProfile(cv, x.steps);
    b.addEventListener("click", () => editDay(b, x));
  });
}
function editDay(cell, x) {
  if (cell.querySelector("input")) return;
  if (!WO.list) loadWorkouts();
  const input = document.createElement("input");
  input.value = entryText(x.entry);
  input.setAttribute("list", "pl-names");
  input.placeholder = "60m Z2 · rest · a workout";
  cell.querySelector(".nm").replaceWith(input);
  let list = $("pl-names");
  if (!list) { list = document.createElement("datalist"); list.id = "pl-names"; document.body.append(list); }
  list.innerHTML = (WO.list || []).filter((w) => !w.id.startsWith("plan:")).map((w) => `<option value="${esc(w.name)}">`).join("") + `<option value="rest">`;
  input.focus(); input.select();
  let done = false;
  const commit = (save) => {
    if (done) return; done = true;
    if (save && input.value.trim() !== entryText(x.entry)) act("plan_set", { date: x.day, text: input.value.trim() });
    else renderPlanGrid(PLAN.data);
  };
  input.addEventListener("keydown", (e) => { e.stopPropagation(); if (e.key === "Enter") commit(true); if (e.key === "Escape") commit(false); });
  input.addEventListener("blur", () => commit(true));
  input.addEventListener("click", (e) => e.stopPropagation());
}

// ---- new block, paste a plan, copy the status
$("pl-make").addEventListener("click", (e) => {
  e.preventDefault(); e.currentTarget.blur();
  act("plan_new", { start: $("pl-start").value, weeks: +$("pl-weeks").value });
});
$("pl-paste").addEventListener("click", (e) => { e.currentTarget.blur(); $("pl-dialog").hidden = false; $("pl-text").focus(); });
$("pl-cancel").addEventListener("click", () => { $("pl-dialog").hidden = true; });
$("pl-import").addEventListener("click", () => {
  act("plan_import", { text: $("pl-text").value });
  $("pl-dialog").hidden = true; $("pl-text").value = "";
});
$("pl-text").addEventListener("keydown", (e) => e.stopPropagation());
$("pl-copy").addEventListener("click", async (e) => {
  e.currentTarget.blur();
  let text = PLAN.data && PLAN.data.status_text;
  try { text = (await (await fetch("/api/plan")).json()).status_text; } catch { /* the one we have */ }
  $("pl-status").value = text || "";
  let ok = false;
  try { await navigator.clipboard.writeText(text); ok = true; } catch { /* select it instead */ }
  $("pl-copied").textContent = ok ? "Copied: paste it into Claude when you ask for the next block." : "Select all and copy it (Ctrl+A, Ctrl+C), then paste it into Claude.";
  $("pl-copybox").hidden = false;
  $("pl-status").focus(); $("pl-status").select();
});
$("pl-close").addEventListener("click", () => { $("pl-copybox").hidden = true; });
window.addEventListener("resize", () => { if (!$("scr-plan").hidden) renderPlan(); });
