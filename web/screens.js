// Home and ride summary screens. Home: the sensors (with a live check), the settings, Start, and
// every past ride. Summary: one ride from its per-second CSV (ride_summary.py on the Python side),
// shown the same way right after the ride or months later. Uses C, $, fmt, hms, blend, act,
// THEME, ZONES, spread, floating, niceUp and LOW_AT/HIGH_AT from app.js.
"use strict";

const HOME = { rides: null, loadedFor: null, settingsKey: "" };

// --------------------------------------------------------------------------- home
function sensorRow(label, status, value) {
  const ok = (status || "").startsWith("connected");
  const text = ok && status.includes(": ") ? status.split(": ")[1] : status || "off";
  return `<div class="srow${ok ? " ok" : ""}"><i class="dot"></i><b>${label}</b>` +
         `<span class="st">${esc(text)}</span><span class="val">${value}</span></div>`;
}
const esc = (t) => String(t).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function renderHome(s) {
  const h = s.home || {}, sens = s.sensors || [];
  const pedals = sens.find((x) => x.label === "Pedals"), strap = sens.find((x) => x.label === "Strap");
  $("home-sensors").innerHTML =
    sensorRow("Pedals", pedals?.status, h.power_w == null ? "" : `${fmt(h.power_w)} W`) +
    (s.settings.hr ? sensorRow("Strap", strap?.status, h.hr == null ? "" : `${fmt(h.hr)} bpm`) : "");
  const hint = $("home-hint");
  hint.textContent = h.pedalling_s != null ? `Pedalling… the ride starts in ${Math.max(0, Math.ceil(3 - h.pedalling_s))} s`
    : h.armed === false ? "Auto-start comes back once you stop pedalling for a moment" : "or just start pedalling";
  // settings: fill the fields when they change on the Python side, never while you type in them
  const key = JSON.stringify(s.settings);
  if (key !== HOME.settingsKey && !document.activeElement?.closest?.("#home-settings")) {
    HOME.settingsKey = key;
    $("set-ftp").value = s.settings.ftp ?? "";
    $("set-lthr").value = s.settings.lthr ?? "";
    $("set-plan").value = s.settings.plan_min ?? "";
    $("set-hr").checked = !!s.settings.hr;
  }
  // the list reloads on arriving home and whenever a ride was saved or deleted
  if (S.prevScreen !== "home" || HOME.loadedFor !== s.rides_version) {
    HOME.loadedFor = s.rides_version;
    loadRides();
  }
}
function sendSettings() {
  const ftp = parseFloat($("set-ftp").value), plan = parseFloat($("set-plan").value);
  const lthr = parseFloat($("set-lthr").value);
  act("settings", { ftp: Number.isFinite(ftp) ? ftp : null, lthr: Number.isFinite(lthr) ? lthr : null,
                    plan_min: Number.isFinite(plan) ? plan : undefined, hr: $("set-hr").checked });
}
for (const id of ["set-ftp", "set-lthr", "set-plan"]) {
  $(id).addEventListener("change", sendSettings);
  $(id).addEventListener("keydown", (e) => { if (e.key === "Enter") e.target.blur(); e.stopPropagation(); });
}
$("set-hr").addEventListener("change", sendSettings);
$("b-start").addEventListener("click", (e) => { act("start"); e.currentTarget.blur(); });
$("b-progress").addEventListener("click", (e) => { act("progress"); e.currentTarget.blur(); });

// English dates and 24 h times, whatever the system language
const DAY = new Intl.DateTimeFormat("en-GB", { weekday: "short", day: "numeric", month: "short" });
const TIME = new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
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
  makeSummaryCharts(d);
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
