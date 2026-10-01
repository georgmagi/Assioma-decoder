// The pages around a ride. Home: the connections, the next ride (Start, its workout) and an overview
// (last ride, this week, fitness / fatigue / form, VT1). Settings: your numbers. The ride history
// (on Statistics) and the ride summary: one ride from its per-second CSV (ride_summary.py on the
// Python side), shown the same way right after the ride or months later. Uses C, $, fmt, hms,
// blend, act, THEME, ZONES, spread, floating, niceUp and LOW_AT/HIGH_AT from app.js.
"use strict";

const HOME = { rides: null, loadedFor: null, settingsKey: "", prog: null, progFor: null };
const esc = (t) => String(t).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
// how a sensor is connected: ANT+ or Bluetooth
const linkTag = (link) => link ? `<em class="link${link === "ANT+" ? " ant" : ""}">${esc(link)}</em>` : "";
const DAY = new Intl.DateTimeFormat("en-GB", { weekday: "short", day: "numeric", month: "short" });
const TIME = new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
const ovStat = (label, value, unit = "") =>
  `<div><dt>${label}</dt><dd><span class="num">${value}</span>${unit ? `<span class="unit">${unit}</span>` : ""}</dd></div>`;

// --------------------------------------------------------------------------- home: connections
// One card per device: what it is, how it's connected, its live value, and Connect / Disconnect
// (the pedals are always on: they're the recorder's reason to exist).
const DEVICES = [
  { key: "pedals", label: "Pedals", sensor: "Pedals", what: "Assioma power, cadence, pedalling dynamics", value: (h) => h.power_w == null ? "" : `${fmt(h.power_w)} W` },
  { key: "hr", label: "Heart-rate strap", sensor: "Strap", what: "heart rate and beat-to-beat intervals (ANT+ stick if plugged in)", value: (h) => h.hr == null ? "" : `${fmt(h.hr)} bpm` },
  { key: "trainer", label: "Smart trainer", sensor: "Trainer", what: "ERG targets for workouts (one app at a time)", value: (h) => h.trainer_w == null ? "" : `${fmt(h.trainer_w)} W` },
];
function renderConnections(s) {
  const h = s.home || {}, sens = s.sensors || [];
  const key = JSON.stringify([sens, h.power_w, h.hr, h.trainer_w, s.settings.hr, s.settings.trainer]);
  if (key === HOME.connKey) return;
  HOME.connKey = key;
  $("dev-list").innerHTML = DEVICES.map((d) => {
    const on = d.key === "pedals" || !!s.settings[d.key];
    const sn = sens.find((x) => x.label === d.sensor), status = on ? sn?.status || "starting…" : "not connected";
    const ok = on && status.startsWith("connected");
    const name = ok && status.includes(": ") ? status.split(": ")[1] : status;
    const btn = d.key === "pedals" ? "" : `<button class="btn${on ? "" : " primary"}" data-dev="${d.key}" data-on="${on ? 0 : 1}">${on ? "Disconnect" : "Connect"}</button>`;
    return `<div class="dev${ok ? " ok" : on ? " wait" : " off"}"><i class="dot"></i>` +
      `<div class="dev-main"><b>${d.label}</b>${on ? linkTag(sn?.link) : ""}<span class="dev-st">${esc(name)}</span><span class="dev-what">${d.what}</span></div>` +
      `<span class="val">${on ? d.value(h) : ""}</span>${btn}</div>`;
  }).join("");
  for (const b of $("dev-list").querySelectorAll("button[data-dev]"))
    b.addEventListener("click", () => act("connect", { device: b.dataset.dev, on: b.dataset.on === "1" }));
}

// --------------------------------------------------------------------------- home
// Top to bottom: how training is going (the last weeks and form), the last ride beside the next
// one, then the connections and your numbers.
function renderHome(s) {
  renderConnections(s);
  renderSettings(s);
  if (HOME.prog && HOME.efFtp !== s.config.ftp) { HOME.efFtp = s.config.ftp; drawEftp(HOME.prog, s); }   // FTP changed
  if (S.prevScreen !== "home" || HOME.progFor !== s.rides_version) {    // on arriving, and after a save / delete
    HOME.progFor = s.rides_version;
    loadOverview();
  }
}
async function loadOverview() {
  let rides, prog;
  try {
    [rides, prog] = await Promise.all([(await fetch("/api/rides")).json(), (await fetch("/api/progress")).json()]);
  } catch { return; }
  HOME.rides = rides;
  HOME.prog = prog;
  drawHero();
  vt1Home().draw(prog);
  drawEftp(prog, S.snap);
  // last ride
  const r = rides[0];
  $("ov-last-empty").hidden = !!r;
  $("b-last").hidden = !r;
  $("ov-last-when").textContent = r ? `${DAY.format(new Date(r.start_utc))}, ${TIME.format(new Date(r.start_utc))}${r.name ? ` · ${r.name}` : ""}` : "";
  $("ov-last-stats").innerHTML = r ? [
    ovStat("Time", hms(r.ride_s)), ovStat("Power", fmt(r.avg_power), "W"), ovStat("NP", fmt(r.np), "W"),
    ovStat("TSS", fmt(r.tss)), ovStat("Heart rate", fmt(r.avg_hr), "bpm"),
    ovStat("Pw:HR", r.decoupling_pct == null ? "--" : `${r.decoupling_pct > 0 ? "+" : ""}${r.decoupling_pct.toFixed(1)}`, r.decoupling_pct == null ? "" : "%"),
    ovStat('<span class="greek">α</span>1', fmt(r.avg_a1, 2)),
    ovStat("VT1", r.vt1_hr?.kind === "cross" ? fmt(r.vt1_hr.value) : r.vt1_hr?.kind === "above" ? `>${fmt(r.vt1_hr.value)}` : "--", r.vt1_hr ? "bpm" : ""),
  ].join("") : "";
  $("b-last").onclick = () => r && act("open", r.id);
}

// ---- the hero: the rolling 7 days (a bar per day, today on the right) and form
const HERO = { metric: store.get("hwMetric", "time_s") };
const HW = { time_s: [(v) => v / 3600, (v) => hm(v), "h"], tss: [(v) => v, (v) => fmt(v), "TSS"] };
for (const b of $("hw-metric").querySelectorAll("button")) {
  b.classList.toggle("on", b.dataset.m === HERO.metric);
  b.addEventListener("click", () => {
    HERO.metric = b.dataset.m; store.set("hwMetric", HERO.metric);
    for (const o of $("hw-metric").querySelectorAll("button")) o.classList.toggle("on", o === b);
    drawHero();
  });
}
function drawHero() {
  const prog = HOME.prog;
  if (!prog) return;
  // the rolling 7 days: today and the 6 before it, each day's rides summed; and the 7 days before
  const today = dayOf(prog.today), DAYMS = 86400e3;
  const day = (k) => {
    const d = new Date(today.getTime() - k * DAYMS), rides = prog.days[isoOf(d)] || [];
    const zones = {};
    for (const r of rides) for (const [z, sec] of Object.entries(r.zones || {})) zones[z] = (zones[z] || 0) + sec;
    return { d, rides: rides.length, time_s: rides.reduce((a, r) => a + (r.ride_s || 0), 0),
             tss: rides.reduce((a, r) => a + (r.tss || 0), 0), kj: rides.reduce((a, r) => a + (r.work_kj || 0), 0), zones };
  };
  const last = [6, 5, 4, 3, 2, 1, 0].map(day), prev = [13, 12, 11, 10, 9, 8, 7].map(day);
  const sum = (arr, k) => arr.reduce((a, x) => a + x[k], 0);
  const t = sum(last, "time_s"), tss = sum(last, "tss"), n = sum(last, "rides"), kj = sum(last, "kj");
  const pt = sum(prev, "time_s"), ptss = sum(prev, "tss");
  // intensity over the 7 days: easy (Z1-2), moderate (Z3), hard (Z4+), from the power zones
  const zt = {};
  for (const x of last) for (const [z, sec] of Object.entries(x.zones)) zt[z] = (zt[z] || 0) + sec;
  const ztot = Object.values(zt).reduce((a, b) => a + b, 0);
  const share = (zs) => (ztot ? zs.reduce((a, z) => a + (zt[z] || 0), 0) / ztot : 0);
  const easy = share(["Z1", "Z2"]), mod = share(["Z3"]), hard = share(["Z4", "Z5", "Z6"]);
  // fitness gained over the 7 days (the ramp rate)
  const p = prog.pmc, m = p.days.length;
  const ramp = m > 7 ? p.ctl[m - 1] - p.ctl[m - 8] : m ? p.ctl[m - 1] : 0;

  $("hw-verdict").innerHTML = verdict({ t, tss, n, pt, ptss, easy, hard, ztot, tsb: m ? p.tsb[m - 1] : null });
  const signed = (v, f) => `${v >= 0 ? "+" : "−"}${f(Math.abs(v))}`;
  $("hw-now").innerHTML = [
    `<div class="big"><dt>Time</dt><dd><span class="num">${hm(t)}</span><span class="unit">${n} ride${n === 1 ? "" : "s"}</span></dd></div>`,
    `<div><dt>Load</dt><dd><span class="num">${fmt(tss)}</span><span class="unit">TSS</span></dd></div>`,
    `<div><dt>Work</dt><dd><span class="num">${fmt(kj)}</span><span class="unit">kJ</span></dd></div>`,
    `<div><dt>vs 7 days before</dt><dd><span class="num">${signed(t - pt, hm)}</span><span class="unit">${signed(tss - ptss, fmt)} TSS</span></dd></div>`,
    `<div><dt>Fitness</dt><dd><span class="num">${signed(ramp, (v) => v.toFixed(1))}</span><span class="unit">this week</span></dd></div>`].join("");
  // easy / moderate / hard
  const lcd = THEME === "lcd";
  const seg = (f, cls, name) => f > 0 ? `<i class="${cls}" style="flex:${f.toFixed(4)}" title="${name} ${Math.round(100 * f)}%"></i>` : "";
  $("hw-mix").innerHTML = ztot ? `<div class="mix-bar">${seg(easy, "easy", "easy")}${seg(mod, "mod", "moderate")}${seg(hard, "hard", "hard")}</div>` +
    `<div class="mix-key"><span><i class="easy"></i>easy (Z1–2) <b>${Math.round(100 * easy)}%</b></span>` +
    `<span><i class="mod"></i>moderate (Z3) <b>${Math.round(100 * mod)}%</b></span>` +
    `<span><i class="hard"></i>hard (Z4+) <b>${Math.round(100 * hard)}%</b></span></div>`
    : `<p class="ov-note">${t ? "Set your FTP to see how hard the riding was." : ""}</p>`;

  // a bar per day, today on the right; in Time, each bar stacked by power zone
  const cv = $("hw-chart"), dpr = devicePixelRatio || 1, wd = cv.clientWidth, ht = cv.clientHeight;
  if (!wd || !ht) return;
  cv.width = Math.round(wd * dpr); cv.height = Math.round(ht * dpr);
  const ctx = cv.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, wd, ht);
  const [conv, show] = HW[HERO.metric];
  const vals = last.map((x) => conv(x[HERO.metric]));
  const top = Math.max(HERO.metric === "tss" ? 60 : 1, ...vals) * 1.2;     // an empty week still has a scale
  const mb = 20, mt = 16, H = ht - mb - mt, slot = wd / 7, bw = Math.min(64, slot * 0.62);
  const WD = new Intl.DateTimeFormat("en-GB", { weekday: "short" });
  ctx.font = "11px Bahnschrift, 'Segoe UI', sans-serif"; ctx.textAlign = "center";
  ctx.strokeStyle = C.grid; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(0, mt + H + 0.5); ctx.lineTo(wd, mt + H + 0.5); ctx.stroke();
  last.forEach((x, i) => {
    const cx = slot * (i + 0.5), v = vals[i], y = mt + H - (v / top) * H, now = i === 6;
    if (v > 0) {
      ctx.save();
      ctx.beginPath(); ctx.roundRect(cx - bw / 2, y, bw, mt + H - y, [4, 4, 0, 0]); ctx.clip();
      const ztime = Object.values(x.zones).reduce((a, b) => a + b, 0);
      if (HERO.metric === "time_s" && ztime && !lcd) {             // stacked by zone, Z1 at the bottom
        let yy = mt + H;
        for (const [, z, colour] of ZONES) {
          const hgt = ((x.zones[z] || 0) / ztime) * (mt + H - y);
          ctx.fillStyle = now ? colour : blend(colour, C.panel, 0.25);
          ctx.fillRect(cx - bw / 2, yy - hgt, bw, hgt + 0.5);
          yy -= hgt;
        }
      } else {
        ctx.fillStyle = now ? (lcd ? C.fg : C.power) : lcd ? "rgba(27,30,25,0.45)" : blend(C.power, C.panel, 0.3);
        ctx.fillRect(cx - bw / 2, y, bw, mt + H - y);
      }
      ctx.restore();
      ctx.fillStyle = C.fg;
      ctx.fillText(show(x[HERO.metric]), cx, y - 4);
    }
    ctx.fillStyle = now ? C.fg : C.dim;
    ctx.fillText(now ? "today" : WD.format(x.d), cx, ht - 5);
  });
  drawForm(prog, ramp);
}

// One plain sentence on the week: how much against the week before, how hard, and what form says.
function verdict({ t, tss, n, pt, ptss, easy, hard, ztot, tsb }) {
  if (!n) return `<b>Nothing in the last 7 days.</b> ${tsb != null && tsb > 5 ? "You're fresh: a good time to start again." : "Start with an easy ride."}`;
  const r = ptss > 0 ? tss / ptss : null;
  const head = r == null ? (pt ? "Back on the bike." : "A first week in the books.")
    : r > 1.5 ? "A big step up." : r > 1.15 ? "A step up." : r >= 0.85 ? "A steady week." : r >= 0.5 ? "A lighter week." : "A much lighter week.";
  const mix = !ztot ? "" : easy >= 0.8 ? ", mostly easy" : hard >= 0.2 ? `, with a lot of intensity (${Math.round(100 * hard)}% hard)` : "";
  const form = tsb == null ? "" : tsb < -30 ? ` Form ${fmt(tsb)}: deep fatigue. Take a rest day.`
    : tsb < -10 ? ` Form ${fmt(tsb)}: you're absorbing the load. Keep the next ride easy.`
    : tsb <= 5 ? ` Form ${tsb > 0 ? "+" : ""}${fmt(tsb)}: balanced. Fine for a quality session.`
    : tsb <= 25 ? ` Form +${fmt(tsb)}: fresh. A good day for something hard or a test.`
    : ` Form +${fmt(tsb)}: very fresh. Fitness is slipping, time to build.`;
  return `<b>${head}</b> ${hm(t)} over ${n} ride${n === 1 ? "" : "s"}${mix}.${form}`;
}

// form on a scale: where -12 sits between overreaching and detraining
const FORM_BANDS = [[-40, -30, "over", "overload"], [-30, -10, "prod", "productive"], [-10, 5, "neutral", "neutral"],
                    [5, 25, "fresh", "fresh"], [25, 40, "detrain", "detraining"]];
function formGauge(tsb) {
  const lo = -40, hi = 40, pos = (v) => (100 * (Math.max(lo, Math.min(hi, v)) - lo)) / (hi - lo);
  const band = FORM_BANDS.find(([a, b]) => tsb >= a && tsb < b) || FORM_BANDS[tsb < -30 ? 0 : 4];
  return `<div class="g-bar">${FORM_BANDS.map(([a, b, cls, name]) =>
      `<i class="${cls}${cls === band[2] ? " on" : ""}" style="flex:${b - a}" title="${name}"></i>`).join("")}` +
    `<b class="g-mark" style="left:${pos(tsb).toFixed(1)}%"></b></div>` +
    `<div class="g-key">${FORM_BANDS.map(([a, b, cls, name]) => `<span style="flex:${b - a}" class="${cls === band[2] ? "on" : ""}">${name}</span>`).join("")}</div>`;
}
function drawForm(prog, ramp = 0) {
  const p = prog.pmc, n = p.days.length;
  if (!n) {
    $("hf-num").textContent = "--"; $("hf-word").textContent = "Fitness and form build up from your rides.";
    $("hf-stats").innerHTML = ""; $("hf-gauge").innerHTML = "";
    return;
  }
  const tsb = p.tsb[n - 1];
  $("hf-num").textContent = `${tsb > 0 ? "+" : ""}${fmt(tsb)}`;
  $("hf-word").textContent = formWord(tsb);
  $("hf-gauge").innerHTML = formGauge(tsb);
  const cross = (prog.vt1 || []).filter((v) => v.hr?.kind === "cross"), vt = cross[cross.length - 1];
  $("hf-stats").innerHTML = [ovStat("Fitness", fmt(p.ctl[n - 1]), `${ramp >= 0 ? "+" : "−"}${Math.abs(ramp).toFixed(1)} /wk`),
    ovStat("Fatigue", fmt(p.atl[n - 1])),
    ovStat("VT1", vt ? fmt(vt.hr.value) : "--", vt ? "bpm" : "")].join("");
  // form over the last 6 weeks, around zero
  const cv = $("hf-spark"), dpr = devicePixelRatio || 1, wd = cv.clientWidth, ht = cv.clientHeight;
  if (!wd || !ht) return;
  cv.width = Math.round(wd * dpr); cv.height = Math.round(ht * dpr);
  const ctx = cv.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, wd, ht);
  const v = p.tsb.slice(-42), lim = Math.max(10, ...v.map(Math.abs)) * 1.1, lcd = THEME === "lcd";
  const X = (i) => (i / Math.max(1, v.length - 1)) * (wd - 6) + 3, Y = (t) => ht / 2 - (t / lim) * (ht / 2 - 3);
  ctx.strokeStyle = C.grid; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(0, Y(0)); ctx.lineTo(wd, Y(0)); ctx.stroke();
  ctx.strokeStyle = lcd ? C.fg : tsb >= 0 ? C.good : C.warn; ctx.lineWidth = 2;
  ctx.beginPath(); v.forEach((t, i) => (i ? ctx.lineTo(X(i), Y(t)) : ctx.moveTo(X(i), Y(t)))); ctx.stroke();
  ctx.fillStyle = ctx.strokeStyle;
  ctx.beginPath(); ctx.arc(X(v.length - 1), Y(v[v.length - 1]), 3.5, 0, 2 * Math.PI); ctx.fill();
}
window.addEventListener("resize", () => { if (!$("scr-home").hidden) drawHero(); });
$("b-start").addEventListener("click", (e) => { act("start"); e.currentTarget.blur(); });
let VT1_HOME = null;                              // made on first use: vt1Trend lives in progress.js, loaded later
const vt1Home = () => (VT1_HOME ||= vt1Trend({ chart: "hv-chart", readout: "hv-readout", note: "hv-note", seg: "hv-by" }, 190));
window.addEventListener("resize", () => { if (!$("scr-home").hidden && HOME.prog) { vt1Home().draw(HOME.prog); drawEftp(HOME.prog, S.snap); } });

// ---- the top bar's Start: the next ride's workout beside it, and the countdown when you pedal
function renderNavStart(s) {
  if (!WO.list) loadWorkouts();
  const h = s.home || {}, w = WO.list && s.workout_id ? WO.list.find((x) => x.id === s.workout_id) : null;
  $("nav-wo").textContent = w ? `${w.name} · ${Math.round(w.total / 60)} min` : "Free ride";
  $("nav-wo").title = w ? "The next ride's workout: click to change" : "No workout: click to pick one";
  const b = $("b-start");
  b.textContent = h.pedalling_s != null ? `Starting in ${Math.max(0, Math.ceil(3 - h.pedalling_s))} s…` : "Start ride";
  b.title = h.armed === false ? "Auto-start comes back once you stop pedalling for a moment" : "Or just start pedalling";
}
$("nav-wo").addEventListener("click", (e) => { act("nav", "workouts"); e.currentTarget.blur(); });

// --------------------------------------------------------------------------- your numbers (on Home)
function renderSettings(s) {
  // fill the fields when they change on the Python side, never while you type in them
  const key = JSON.stringify(s.settings);
  if (key !== HOME.settingsKey && !document.activeElement?.closest?.("#set-form")) {
    HOME.settingsKey = key;
    $("set-ftp").value = s.settings.ftp ?? "";
    $("set-lthr").value = s.settings.lthr ?? "";
    $("set-vt1").value = s.settings.vt1_hr ?? "";
    $("set-plan").value = s.settings.plan_min ?? "";
  }
  // empty VT1 = your latest ride that crossed it (vt1.py): shown as the placeholder
  const auto = s.vt1 && s.vt1.source === "ride" ? s.vt1 : null;
  $("set-vt1").placeholder = auto ? `auto ${fmt(auto.hr)}` : "auto";
  $("set-vt1").title = auto ? `From your ride on ${auto.day}${auto.lo != null ? ` (${fmt(auto.lo)}–${fmt(auto.hi)})` : ""}. Type a value to override.`
    : "Found from a ride where α1 crosses 0.75. Type a value to set it yourself.";
}
function sendSettings() {
  const num = (id) => { const v = parseFloat($(id).value); return Number.isFinite(v) ? v : null; };
  const plan = num("set-plan");
  act("settings", { ftp: num("set-ftp"), lthr: num("set-lthr"), vt1_hr: num("set-vt1"), plan_min: plan ?? undefined });
}
for (const id of ["set-ftp", "set-lthr", "set-vt1", "set-plan"]) {
  $(id).addEventListener("change", sendSettings);
  $(id).addEventListener("keydown", (e) => { if (e.key === "Enter") e.target.blur(); e.stopPropagation(); });
}

// --------------------------------------------------------------------------- history (Statistics page)
async function loadRides() {
  if (!HOME.rides) {                              // the first read of new FIT files takes a moment
    $("rides-empty").textContent = "Loading your rides…";
    $("rides-empty").hidden = false;
  }
  try {
    HOME.rides = await (await fetch("/api/rides")).json();
  } catch { return; }
  $("rides-empty").textContent = "No rides yet. They'll show up here once you save one.";
  const body = $("ride-rows");
  $("rides-count").textContent = HOME.rides.length ? `${HOME.rides.length}` : "";
  $("rides-empty").hidden = HOME.rides.length > 0;
  body.replaceChildren(...HOME.rides.map((r) => {
    const d = new Date(r.start_utc), tr = document.createElement("tr");
    tr.tabIndex = 0;
    const name = (r.name ? `<span class="nm">${esc(r.name)}</span>` : "") + `<span class="src">${esc(r.source)}</span>`;
    tr.innerHTML = `<td>${DAY.format(d)}</td><td>${TIME.format(d)}</td><td class="wk">${name}</td><td class="n">${hms(r.ride_s)}</td>` +
      `<td class="n">${fmt(r.avg_power)}</td><td class="n">${fmt(r.np)}</td><td class="n">${fmt(r.avg_hr)}</td>` +
      `<td class="n">${fmt(r.avg_a1, 2)}</td><td class="n">${fmt(r.tss)}</td>` +
      `<td class="del"><button class="row-del" title="Delete this ride">Delete</button></td>`;
    tr.querySelector(".row-del").addEventListener("click", (e) => { e.stopPropagation(); act("delete", r.id); });
    const open = () => act("open", r.id);
    tr.addEventListener("click", open);
    tr.addEventListener("keydown", (e) => { if (e.key === "Enter") open(); });
    return tr;
  }));
}

// --------------------------------------------------------------------------- summary
const SUM = { id: null, data: null, plots: {} };
const LONG_DATE = new Intl.DateTimeFormat("en-GB", { weekday: "long", day: "numeric", month: "long", year: "numeric" });

async function showSummary(id) {
  if (SUM.id === id) return;
  SUM.id = id;
  $("sum-title").textContent = "Loading the ride…";
  for (const k of Object.keys(SUM.plots)) { SUM.plots[k].destroy(); delete SUM.plots[k]; }
  let d;
  try {
    d = await (await fetch("/api/ride?id=" + encodeURIComponent(id))).json();
  } catch {
    $("sum-title").textContent = "This ride couldn't be read.";
    return;
  }
  if (SUM.id !== id) return;
  SUM.data = d;
  renderSummary(d);
}
function redrawSummary() {                        // after a theme change: charts in the new ink
  if (!SUM.data) return;
  for (const k of Object.keys(SUM.plots)) { SUM.plots[k].destroy(); delete SUM.plots[k]; }
  renderSummary(SUM.data);
}

const minsec = (s) => (s >= 3600 ? hms(s) : `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, "0")}`);
function stat(label, value, unit = "", sub = "") {
  return `<div class="stat"><dt>${label}</dt><dd><span class="num">${value}</span>${unit ? `<span class="unit">${unit}</span>` : ""}</dd>` +
         `<p class="sub">${sub}</p></div>`;
}

function renderSummary(d) {
  const st = d.stats, start = new Date(d.start_utc);
  $("sum-title").innerHTML = (d.name ? `${esc(d.name)} <span class="sub-title">` : `<span>`) +
    `${LONG_DATE.format(start)}, ${TIME.format(start)}${d.source !== "Assioma" ? ` · ${esc(d.source)}` : ""}</span>`;
  $("sum-file").hidden = !d.fit;
  const bal = st.balance_right;
  const dec = st.decoupling_pct;
  $("sum-stats").innerHTML = [
    stat("Ride time", hms(st.ride_s), "", st.elapsed_s > st.ride_s + 5 ? `elapsed ${hms(st.elapsed_s)}` : ""),
    stat("Power", fmt(st.avg_power), "W", `NP ${fmt(st.np)} · max ${fmt(st.max_power)}`),
    stat("Load", st.tss == null ? "--" : fmt(st.tss), "TSS", st.if == null ? "set your FTP for IF and TSS" : `IF ${st.if.toFixed(2)} · FTP ${fmt(st.ftp)}`),
    stat("Heart rate", fmt(st.avg_hr), "bpm", `max ${fmt(st.max_hr)}`),
    stat('DFA <span class="greek">α</span>1', fmt(st.avg_a1, 2), "", st.rmssd == null ? "" : `RMSSD ${st.rmssd.toFixed(1)} ms · art ${fmt(st.artifact_pct, 1)}%`),
    stat("Pw:HR", dec == null ? "--" : `${dec > 0 ? "+" : ""}${dec.toFixed(1)}`, dec == null ? "" : "%",
         dec == null ? "needs 20 min" : `EF ${st.ef.toFixed(2)} · ${Math.abs(dec) < 5 ? "well coupled" : "drifting"}`),
    stat("Work", fmt(st.work_kj), "kJ", `${fmt(st.avg_cadence)} rpm avg`),
    stat("Balance", bal == null ? "--" : `${fmt(100 - bal)} · ${fmt(bal)}`, "", st.standing_pct == null ? "" : `standing ${fmt(st.standing_pct)}%`),
  ].join("");

  // best powers
  $("sum-best").innerHTML = [["5", "5 s"], ["60", "1 min"], ["300", "5 min"], ["1200", "20 min"]].map(([k, l]) =>
    `<div class="best"><span class="lab">${l}</span><span class="num">${fmt(d.best[k])}</span><span class="unit">W</span>` +
    (d.best[k] && st.ftp ? `<span class="pct">${fmt(100 * d.best[k] / st.ftp)}% FTP</span>` : "") + `</div>`).join("");

  // time in zones
  const zc = THEME === "lcd" ? null : Object.fromEntries(ZONES.map(([, n, c]) => [n, c]));
  const total = Object.values(d.zones).reduce((a, b) => a + b, 0);
  const bar = (label, secs, all, colour, extra = "") =>
    `<div class="zbar"><span class="zl">${label}</span><span class="track"><i style="width:${all ? (100 * secs / all).toFixed(1) : 0}%;` +
    `${colour ? `background:${colour}` : ""}"></i></span><span class="zt">${secs ? minsec(secs) : "–"}</span>` +
    `<span class="zp">${all && secs ? fmt(100 * secs / all) + "%" : ""}</span>${extra}</div>`;
  $("sum-zones").innerHTML = total
    ? Object.entries(d.zones).map(([z, sec]) => bar(z, sec, total, zc && zc[z])).join("")
    : `<p class="empty-note">Set your FTP on the home screen to see power zones.</p>`;
  const hz = d.hr_zones, htotal = Object.values(hz).reduce((a, b) => a + b, 0);
  const hc = THEME === "lcd" ? {} : { Z1: "#9aa3ad", Z2: "#5b9dff", Z3: "#3ddc97", Z4: "#ffd166", Z5: "#ff5c5c" };
  $("sum-hrzones").innerHTML = htotal
    ? Object.entries(hz).map(([z, sec]) => bar(z, sec, htotal, hc[z])).join("")
    : `<p class="empty-note">${d.stats.lthr ? "No heart rate in this ride." : "Set your threshold heart rate on the home screen to see heart-rate zones."}</p>`;
  const ab = d.a1_bands, aall = ab["above_0.75"] + ab["0.5_to_0.75"] + ab["below_0.5"];
  $("sum-a1bands").innerHTML = aall ? [
    bar("> 0.75", ab["above_0.75"], aall, THEME === "lcd" ? null : C.good),
    bar("0.5–0.75", ab["0.5_to_0.75"], aall, THEME === "lcd" ? null : C.warn),
    bar("< 0.5", ab["below_0.5"], aall, THEME === "lcd" ? null : C.bad)].join("")
    : `<p class="empty-note">No α1 for this ride: it needs a heart-rate strap and 2 minutes of beats.</p>`;

  // pedalling, left vs right
  const L = d.legs.left, R = d.legs.right, sgn = (v) => (v == null ? "--" : `${v > 0 ? "+" : ""}${v.toFixed(1)}`);
  const ang = (a, b) => (a == null || b == null ? "--" : `${Math.round(a)}–${Math.round(b)}°`);
  const rows = [
    ["Power", `${fmt(L.power_w)} W`, `${fmt(R.power_w)} W`],
    ["Balance", bal == null ? "--" : `${(100 - bal).toFixed(1)}%`, bal == null ? "--" : `${bal.toFixed(1)}%`],
    ["Torque eff.", L.te_pct == null ? "--" : `${fmt(L.te_pct)}%`, R.te_pct == null ? "--" : `${fmt(R.te_pct)}%`],
    ["Smoothness", L.ps_pct == null ? "--" : `${fmt(L.ps_pct)}%`, R.ps_pct == null ? "--" : `${fmt(R.ps_pct)}%`],
    ["PCO", `${sgn(L.pco_mm)} mm`, `${sgn(R.pco_mm)} mm`],
    ["PCO range", L.pco_p10 == null ? "--" : `${fmt(L.pco_p10)} to ${fmt(L.pco_p90)}`, R.pco_p10 == null ? "--" : `${fmt(R.pco_p10)} to ${fmt(R.pco_p90)}`],
    ["Power phase", ang(L.pp_start, L.pp_end), ang(R.pp_start, R.pp_end)],
    ["Peak phase", ang(L.peak_start, L.peak_end), ang(R.peak_start, R.peak_end)],
  ];
  $("sum-legs").innerHTML = `<tr><th></th><th>Left</th><th>Right</th></tr>` +
    rows.map(([k, a, b]) => `<tr><td>${k}</td><td class="n">${a}</td><td class="n">${b}</td></tr>`).join("");

  // cards only for data the ride has (another app's FIT file has no pedal dynamics or beats)
  $("card-legs").hidden = [L.te_pct, L.pco_mm, L.pp_start, R.te_pct].every((v) => v == null) && L.power_w == null;
  $("card-pc").hidden = !d.poincare.points.length;
  $("sum-c-beats").hidden = !d.beats.t.length && !d.series.a1.some((v) => v != null);
  drawPoincare(d.poincare);
  renderVt1(d);
  makeSummaryCharts(d);
}

// --------------------------------------------------------------------------- VT1 (vt1.py)
// Where alpha 1 crosses 0.75 in this ride: by heart rate (the main one) or power. The chart is the
// evidence: each dot a bin's median alpha 1 (size = how long), the line the decreasing fit, the
// band the 80 % bootstrap interval of the crossing.
const VT1 = { by: "hr" };
for (const b of $("vt1-by").querySelectorAll("button")) {
  b.addEventListener("click", () => {
    VT1.by = b.dataset.by;
    for (const o of $("vt1-by").querySelectorAll("button")) o.classList.toggle("on", o === b);
    if (SUM.data) renderVt1(SUM.data);
  });
}
function vt1Text(e, unit) {
  const u = `<span class="unit">${unit}</span>`;
  if (!e) return { big: "--", sub: "" };
  if (e.kind === "cross") return { big: `<span class="num">${fmt(e.value)}</span>${u}`,
    sub: e.lo == null ? "uncertain: too few resamples cross" : `${fmt(e.lo)}–${fmt(e.hi)} ${unit} (80 %) · crossed in ${fmt(100 * e.p_cross)}% of resamples` };
  if (e.kind === "between") return { big: `<span class="pre">between</span><span class="num">${fmt(e.value)}</span>–<span class="num">${fmt(e.value2)}</span>${u}`,
    sub: "it crosses where this ride had no steady riding" };
  if (e.kind === "above") return { big: `<span class="pre">above</span><span class="num">${fmt(e.value)}</span>${u}`,
    sub: "α1 stayed above 0.75: a harder steady block would pin it down" };
  return { big: `<span class="pre">below</span><span class="num">${fmt(e.value)}</span>${u}`, sub: "α1 was under 0.75 throughout" };
}
function renderVt1(d) {
  const v = d.vt1, card = $("card-vt1");
  const hasA1 = d.series.a1.some((x) => x != null);
  card.hidden = !v && !hasA1;
  if (card.hidden) return;
  const e = v && v[VT1.by], unit = VT1.by === "hr" ? "bpm" : "W";
  const t = v ? vt1Text(e, unit) : { big: "--", sub: "" };
  $("vt1-head").innerHTML = `<div class="vt1-big">${t.big}</div><p class="sub">${t.sub}</p>`;
  const notes = [];
  if (!v) notes.push("Needs 10 min of steady riding with α1 after the first 10 min.");
  else {
    notes.push(`From ${fmt(v.minutes)} min of steady α1.`);
    if (v.beats_pct != null && v.beats_pct < 95)
      notes.push(`Only ${fmt(v.beats_pct)}% of beats arrived (Bluetooth): α1 reads high near 0.75, so VT1 is likely lower.`);
  }
  $("vt1-note").textContent = notes.join(" ");
  drawVt1(e, unit);
}
function drawVt1(e, unit) {
  const cv = $("vt1-chart"), dpr = devicePixelRatio || 1;
  const wd = Math.min(cv.parentElement.clientWidth, 560), ht = Math.round(Math.min(260, wd * 0.62));
  cv.style.width = wd + "px"; cv.style.height = ht + "px";
  cv.width = Math.round(wd * dpr); cv.height = Math.round(ht * dpr);
  const ctx = cv.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, wd, ht);
  if (!e || !e.curve.length) return;
  const cur = e.curve, lcd = THEME === "lcd";
  const xsAll = cur.map((c) => c[0]).concat(e.lo ?? [], e.hi ?? []);
  const step = unit === "W" ? 25 : 10;
  const x0 = Math.floor((Math.min(...xsAll) - step / 2) / step) * step, x1 = Math.ceil((Math.max(...xsAll) + step / 2) / step) * step;
  const ys = cur.flatMap((c) => [c[1], c[2]]);
  const y0 = Math.min(0.3, Math.floor(Math.min(...ys) * 10) / 10), y1 = Math.max(1.6, Math.ceil(Math.max(...ys) * 10) / 10);
  const ml = 34, mr = 10, mt = 8, mb = 22, W = wd - ml - mr, H = ht - mt - mb;
  const X = (v) => ml + ((v - x0) / (x1 - x0)) * W, Y = (v) => mt + H - ((v - y0) / (y1 - y0)) * H;
  ctx.font = "11px Bahnschrift, 'Segoe UI', sans-serif";
  ctx.strokeStyle = C.grid; ctx.fillStyle = C.dim; ctx.lineWidth = 1;
  for (let v = Math.ceil(y0 / 0.25) * 0.25; v <= y1 + 1e-9; v += 0.25) {
    ctx.beginPath(); ctx.moveTo(ml, Y(v)); ctx.lineTo(ml + W, Y(v)); ctx.stroke();
    ctx.textAlign = "right"; ctx.fillText(v.toFixed(2).replace(/0$/, ""), ml - 5, Y(v) + 4);
  }
  ctx.textAlign = "center";
  for (let v = x0; v <= x1; v += step) ctx.fillText(v, X(v), mt + H + 15);
  // the 80 % interval of the crossing
  if (e.lo != null) {
    ctx.fillStyle = lcd ? "rgba(27,30,25,0.12)" : blend(C.a1, C.panel, 0.82);
    ctx.fillRect(X(e.lo), mt, Math.max(1, X(e.hi) - X(e.lo)), H);
  }
  // the 0.75 line
  ctx.strokeStyle = lcd ? C.fg : C.warn; ctx.setLineDash([4, 4]);
  ctx.beginPath(); ctx.moveTo(ml, Y(0.75)); ctx.lineTo(ml + W, Y(0.75)); ctx.stroke(); ctx.setLineDash([]);
  // bin medians (area ~ time in the bin) and the decreasing fit
  const nmax = Math.max(...cur.map((c) => c[3]));
  ctx.fillStyle = lcd ? "rgba(27,30,25,0.35)" : blend(C.a1, C.panel, 0.45);
  for (const [x, , m, n] of cur) {
    ctx.beginPath(); ctx.arc(X(x), Y(m), 2 + 5 * Math.sqrt(n / nmax), 0, 2 * Math.PI); ctx.fill();
  }
  ctx.strokeStyle = lcd ? C.fg : C.a1; ctx.lineWidth = 2;
  ctx.beginPath();
  cur.forEach(([x, f], i) => (i ? ctx.lineTo(X(x), Y(f)) : ctx.moveTo(X(x), Y(f))));
  ctx.stroke();
  // the estimate
  if (e.kind === "cross") {
    ctx.fillStyle = lcd ? C.fg : C.fg;
    ctx.beginPath(); ctx.arc(X(e.value), Y(0.75), 4.5, 0, 2 * Math.PI); ctx.fill();
  }
}

// Poincaré plot: each interval against the next. The cloud's width across the diagonal (SD1) is
// beat-to-beat variability, its length along it (SD2) the slower swings.
function drawPoincare(p) {
  const cv = $("poincare"), dpr = devicePixelRatio || 1;
  const size = Math.min(cv.parentElement.clientWidth, 340);
  cv.style.width = cv.style.height = size + "px";
  cv.width = cv.height = Math.round(size * dpr);
  const ctx = cv.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, size, size);
  $("pc-stats").innerHTML = p.sd1 == null ? "Not enough clean beats."
    : `SD1 <b>${p.sd1.toFixed(1)}</b> ms · SD2 <b>${p.sd2.toFixed(1)}</b> ms · SD1/SD2 <b>${(p.sd1 / p.sd2).toFixed(2)}</b>`;
  if (!p.points.length) return;
  const vals = p.points.flat().sort((a, b) => a - b);
  const q = (f) => vals[Math.floor(f * (vals.length - 1))];
  let lo = q(0.005), hi = q(0.995);
  const padv = Math.max(20, (hi - lo) * 0.1); lo -= padv; hi += padv;
  const m = 34, w = size - m - 8, X = (v) => m + ((v - lo) / (hi - lo)) * w, Y = (v) => 8 + w - ((v - lo) / (hi - lo)) * w;
  const ink = C.fg, grid = C.grid;
  ctx.font = "11px Bahnschrift, 'Segoe UI', sans-serif";
  ctx.fillStyle = C.dim; ctx.strokeStyle = grid; ctx.lineWidth = 1;
  const step = [10, 20, 25, 50, 100, 200].find((s) => (hi - lo) / s <= 5) || 250;
  for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) {
    ctx.beginPath(); ctx.moveTo(X(v), 8); ctx.lineTo(X(v), 8 + w); ctx.moveTo(m, Y(v)); ctx.lineTo(m + w, Y(v)); ctx.stroke();
    ctx.textAlign = "right"; ctx.fillText(v, m - 5, Y(v) + 4);
  }
  ctx.strokeStyle = blend(ink, C.panel, 0.6);                      // the identity line
  ctx.setLineDash([3, 3]); ctx.beginPath(); ctx.moveTo(X(lo), Y(lo)); ctx.lineTo(X(hi), Y(hi)); ctx.stroke(); ctx.setLineDash([]);
  ctx.fillStyle = THEME === "lcd" ? "rgba(27,30,25,0.22)" : "rgba(45,212,191,0.22)";
  for (const [a, b] of p.points) ctx.fillRect(X(a) - 1, Y(b) - 1, 2, 2);
  // the SD1/SD2 ellipse, centred on the mean interval, along the diagonal
  const cx = X(p.mean), cy = Y(p.mean), k = w / (hi - lo);
  ctx.save(); ctx.translate(cx, cy); ctx.rotate(-Math.PI / 4);
  ctx.strokeStyle = THEME === "lcd" ? ink : C.a1; ctx.lineWidth = 2;
  ctx.beginPath(); ctx.ellipse(0, 0, Math.max(1, 2 * p.sd2 * k), Math.max(1, 2 * p.sd1 * k), 0, 0, 2 * Math.PI); ctx.stroke();
  ctx.restore();
}

// whole-ride charts: the ride screen's pairing and placement (power + HR, RR + α1), static
function makeSummaryCharts(d) {
  const lcd = THEME === "lcd", ser = d.series, t = ser.t, beats = d.beats;
  const smooth = (arr, half) => arr.map((v, i) => {          // centred mean, for the whole-ride view
    let s = 0, n = 0;
    for (let k = Math.max(0, i - half); k <= Math.min(arr.length - 1, i + half); k++) if (arr[k] != null) { s += arr[k]; n++; }
    return v == null || !n ? null : s / n;
  });
  const p30 = smooth(ser.power, 15), hr15 = smooth(ser.hr, 7);
  const t0 = new Date(d.start_utc).getTime();
  const clock = (s) => TIME.format(new Date(t0 + s * 1000));
  const axisFont = "12px Bahnschrift, 'Segoe UI', sans-serif";
  const smoothPath = uPlot.paths.spline();
  const round = (r, step) => [Math.floor(r[0] / step) * step, Math.ceil(r[1] / step) * step];
  const pwSt = spread(p30.filter((v) => v != null)), hrSt = spread(hr15.filter((v) => v != null), 0.03);
  const pwR = pwSt ? [0, niceUp(Math.max(pwSt.max * 1.05, pwSt.med / LOW_AT, 100), 50)] : [0, 300];
  const hrR = hrSt ? round(floating(hrSt, HIGH_AT, 40), 10) : [100, 160];
  const clean = beats.rr.filter((_, i) => !beats.artifact[i]);
  const rrSt = spread(clean, 0.03), a1v = ser.a1.filter((v) => v != null), a1St = spread(a1v);
  const rrR = rrSt ? round(floating(rrSt, HIGH_AT, 150), 50) : [300, 700];
  const a1lo = a1St ? Math.min(0.7, a1St.min - 0.05) : 0.5;
  const a1R = a1St ? [Math.floor(a1lo / 0.25) * 0.25, Math.ceil(Math.max(a1lo + (a1St.med - a1lo) / LOW_AT, a1St.max + 0.05) / 0.25) * 0.25] : [0.5, 1.75];
  const common = (el) => ({
    width: el.clientWidth, height: el.clientHeight, legend: { show: false }, padding: [24, 8, 0, 0],
    cursor: { sync: { key: "sum" }, drag: { x: true, y: false, setScale: true }, y: false, points: { show: false } },
  });
  const xAxis = (labels) => ({
    stroke: C.dim, font: axisFont, grid: { stroke: C.grid, width: 1 }, ticks: { show: false }, space: 80,
    incrs: [60, 120, 300, 600, 900, 1800, 3600], values: labels ? (u, v) => v.map(clock) : (u, v) => v.map(() => ""), size: labels ? 30 : 4,
  });
  const yAxis = (scale, side, colour, incrs, grid, format) => ({
    scale, side, stroke: blend(colour, C.panel, 0.2), font: axisFont, size: 46, ticks: { show: false },
    grid: grid ? { stroke: C.grid, width: 1 } : { show: false }, incrs, ...(format ? { values: (u, v) => v.map(format) } : {}),
  });
  const drawZ2 = (u) => {
    const ftp = d.stats.ftp;
    if (!ftp) return;
    const y1 = u.valToPos(0.75 * ftp, "pw", true), y0 = u.valToPos(0.56 * ftp, "pw", true);
    u.ctx.fillStyle = C.z2; u.ctx.fillRect(u.bbox.left, y1, u.bbox.width, y0 - y1);
  };
  const thresholds = (u) => {
    for (const [v, col] of [[0.75, C.good], [0.5, C.bad]]) {
      const ctx = u.ctx, dpr = devicePixelRatio || 1, y = Math.round(u.valToPos(v, "a1", true)) + 0.5;
      if (y < u.bbox.top || y > u.bbox.top + u.bbox.height) continue;
      ctx.save(); ctx.strokeStyle = blend(col, C.panel, 0.45); ctx.lineWidth = dpr; ctx.setLineDash([4 * dpr, 4 * dpr]);
      ctx.beginPath(); ctx.moveTo(u.bbox.left, y); ctx.lineTo(u.bbox.left + u.bbox.width, y); ctx.stroke(); ctx.restore();
    }
  };
  const readout = (u) => {
    const i = u.cursor.idx, out = $("sum-readout");
    if (i == null || u.cursor.left < 0) { out.textContent = ""; return; }
    const a1 = ser.a1[i];
    out.innerHTML = `${TIME.format(new Date(t0 + t[i] * 1000))} &nbsp; <b>${fmt(p30[i])}</b> W &nbsp; <b>${fmt(hr15[i])}</b> bpm` +
      (a1 != null ? ` &nbsp; α1 <b>${a1.toFixed(2)}</b>` : "");
  };
  const zoomBoth = (u) => {                                          // drag to zoom, double-click to reset
    const { min, max } = u.scales.x;
    for (const o of Object.values(SUM.plots)) if (o !== u && (o.scales.x.min !== min || o.scales.x.max !== max)) o.setScale("x", { min, max });
  };
  const elP = $("sum-c-power"), elB = $("sum-c-beats");
  const rrs = beats.rr.map((v, i) => (beats.artifact[i] ? undefined : v)), ras = beats.rr.map((v, i) => (beats.artifact[i] ? v : undefined));
  SUM.plots.power = new uPlot({
    ...common(elP),
    scales: { x: { time: false }, pw: { range: () => pwR }, hr: { range: () => hrR } },
    axes: [xAxis(false), yAxis("pw", 3, C.power, [25, 50, 100, 200], true), yAxis("hr", 1, C.hr, [5, 10, 20, 25, 50], false)],
    series: [{}, { scale: "pw", stroke: C.power, width: 2, paths: smoothPath, points: { show: false }, ...(lcd ? { fill: dither } : {}) },
      { scale: "hr", stroke: C.hr, width: 2, paths: smoothPath, points: { show: false } }],
    hooks: { drawClear: [drawZ2], setCursor: [readout], setScale: [(u, k) => k === "x" && zoomBoth(u)] },
  }, [t, p30, hr15], elP);
  // beats have their own times: merge with the per-second α1 on one x
  const bx = [], brr = [], bra = [], ba1 = [];
  let i = 0, j = 0;
  while (i < beats.t.length || j < t.length) {
    if (j >= t.length || (i < beats.t.length && beats.t[i] <= t[j])) { bx.push(beats.t[i]); brr.push(rrs[i]); bra.push(ras[i]); ba1.push(undefined); i++; }
    else { bx.push(t[j]); brr.push(undefined); bra.push(undefined); ba1.push(ser.a1[j] ?? undefined); j++; }
  }
  SUM.plots.beats = new uPlot({
    ...common(elB),
    scales: { x: { time: false }, rr: { range: () => rrR }, a1: { range: () => a1R } },
    axes: [xAxis(true), yAxis("rr", 3, C.rr, [10, 25, 50, 100, 200], true), yAxis("a1", 1, C.a1, [0.1, 0.25, 0.5, 1], false, (v) => v.toFixed(2))],
    series: [{}, { scale: "rr", stroke: lcd ? blend(C.fg, C.panel, 0.55) : C.rr, width: 1, points: { show: false } },   // light: a whole ride of beats
      { scale: "rr", stroke: C.bad, width: 0, paths: () => null,
        points: lcd ? { show: true, size: 5, width: 1, fill: C.panel, stroke: C.fg } : { show: true, size: 4, width: 0, fill: C.bad, stroke: C.bad } },
      { scale: "a1", stroke: C.a1, width: lcd ? 2.75 : 2.25, points: { show: false } }],
    hooks: { drawClear: [thresholds], setScale: [(u, k) => k === "x" && zoomBoth(u)] },
  }, [bx, brr, bra, ba1], elB);
  for (const [k, el] of [["power", elP], ["beats", elB]]) {
    if (el._observed) continue;
    el._observed = true;
    new ResizeObserver(() => SUM.plots[k]?.setSize({ width: el.clientWidth, height: el.clientHeight })).observe(el);
  }
}
$("sum-home").addEventListener("click", () => act("home"));
$("sum-file").addEventListener("click", () => act("show_file"));
$("sum-delete").addEventListener("click", (e) => { act("delete"); e.currentTarget.blur(); });

// --------------------------------------------------------------------------- eFTP (eftp.py) on Home
// The critical power of your best 3-20 min efforts over 42 days, beside your FTP setting; the chart
// is the evidence: your best power for each duration (dots, log time) and the model's curve.
function drawEftp(prog, s) {
  const e = prog.eftp || {}, f = e.current, ftp = s?.config?.ftp;
  const q = $("ef-quality"), use = $("b-useftp");
  $("ef-num").textContent = f ? fmt(f.cp) : "--";
  q.hidden = !f;
  if (f) { q.textContent = `${f.quality} fit`; q.className = `link${f.quality === "good" ? " ant" : ""}`; }
  const diff = f && ftp ? f.cp - ftp : null;
  $("ef-sub").innerHTML = f ? `W′ <b>${fmt(f.w_prime / 1000, 1)}</b> kJ · best 20 min ×0.95 <b>${fmt(f.best20_95)}</b> W` +
    (diff != null ? ` · your FTP <b>${fmt(ftp)}</b> (${diff >= 0 ? "+" : "−"}${fmt(Math.abs(diff))})` : "") : "";
  use.hidden = !f || (ftp && Math.abs(diff) < 3);
  use.textContent = f ? `Use ${fmt(f.cp)} W as FTP` : "";
  use.onclick = () => f && act("settings", { ftp: Math.round(f.cp) });
  $("ef-note").textContent = !f
    ? (e.best20 ? "Not enough hard efforts in the last 42 days: ride a few all-out 3–12 min efforts or a 20 min test." : "Builds from the power in your rides.")
    : f.quality === "rough" ? "Your longer bests look easier than your short ones: an all-out 20 min (or 8–12 min) effort will sharpen it."
    : f.quality === "fair" ? "From your best efforts of the last 42 days. More all-out efforts tighten it." : "From your best efforts of the last 42 days.";
  // the power-duration curve, 1 min to 60 min on a log axis, with CP + W'/t through it
  const cv = $("ef-chart"), dpr = devicePixelRatio || 1, wd = cv.clientWidth, ht = cv.clientHeight;
  if (!wd || !ht) return;
  cv.width = Math.round(wd * dpr); cv.height = Math.round(ht * dpr);
  const ctx = cv.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, wd, ht);
  const pts = Object.entries(e.curve || {}).map(([t, p]) => [+t, p]).filter(([t]) => t >= 60).sort((a, b) => a[0] - b[0]);
  if (!pts.length) return;
  const lcd = THEME === "lcd", ml = 34, mr = 8, mt = 8, mb = 18, W = wd - ml - mr, H = ht - mt - mb;
  const t0 = Math.log(60), t1 = Math.log(3600);
  const ps = pts.map(([, p]) => p).concat(f ? [f.cp, f.cp + f.w_prime / 150] : []);
  const step = 25, y0 = Math.floor((Math.min(...ps) - 10) / step) * step, y1 = Math.ceil((Math.max(...ps) + 10) / step) * step;
  const X = (t) => ml + ((Math.log(t) - t0) / (t1 - t0)) * W, Y = (p) => mt + H - ((p - y0) / (y1 - y0)) * H;
  ctx.font = "11px Bahnschrift, 'Segoe UI', sans-serif"; ctx.strokeStyle = C.grid; ctx.fillStyle = C.dim; ctx.lineWidth = 1;
  ctx.textAlign = "right";
  const ys = (y1 - y0) / step > 5 ? step * 2 : step;
  for (let p = y0; p <= y1; p += ys) { ctx.beginPath(); ctx.moveTo(ml, Y(p)); ctx.lineTo(ml + W, Y(p)); ctx.stroke(); ctx.fillText(p, ml - 5, Y(p) + 4); }
  ctx.textAlign = "center";
  for (const [t, l] of [[60, "1 min"], [300, "5"], [1200, "20"], [3600, "60 min"]]) ctx.fillText(l, Math.min(ml + W - 16, Math.max(ml + 14, X(t))), ht - 4);
  if (f) {                                                         // the model, and CP itself
    ctx.strokeStyle = lcd ? C.fg : C.warn; ctx.setLineDash([4, 4]);
    ctx.beginPath(); ctx.moveTo(ml, Y(f.cp)); ctx.lineTo(ml + W, Y(f.cp)); ctx.stroke(); ctx.setLineDash([]);
    ctx.strokeStyle = lcd ? C.fg : C.power; ctx.lineWidth = 2; ctx.beginPath();
    const m0 = Math.log(150);                                      // the 2-parameter model only holds from a few minutes
    for (let k = 0; k <= 50; k++) {
      const t = Math.exp(m0 + ((t1 - m0) * k) / 50), p = f.cp + f.w_prime / t;
      k ? ctx.lineTo(X(t), Y(p)) : ctx.moveTo(X(t), Y(p));
    }
    ctx.stroke();
  }
  for (const [t, p] of pts) {                                      // your bests; filled = used in the fit
    const used = f && f.durations.includes(t);
    ctx.beginPath(); ctx.arc(X(t), Y(p), 3.5, 0, 2 * Math.PI);
    ctx.fillStyle = C.fg; ctx.strokeStyle = C.fg; ctx.lineWidth = 1.5;
    used ? ctx.fill() : ctx.stroke();
  }
}
