// Workouts (workout.py): the library (Workouts page), the next ride's workout (Home) and the strip on the ride screen that shows
// the current step, its target, the next step and the whole workout's profile, with the controls
// (intensity -/+, skip, ERG on/off, end). The trainer itself is driven by the Python side (ftms.py);
// this only shows and asks. Uses C, $, fmt, act, blend, THEME, ZONES, esc, minsec from the other files.
"use strict";

const WO = { list: null, loading: false, loadedAt: 0 };

// ---- profile drawing, shared by the picker thumbnails and the strip
function zoneColour(frac) {
  if (THEME === "lcd") return C.fg;
  const z = ZONES.find(([top]) => frac <= top) || ZONES[ZONES.length - 1];
  return z[2];
}
function drawProfile(cv, steps, opts = {}) {
  const dpr = devicePixelRatio || 1, w = cv.clientWidth, h = cv.clientHeight;
  if (!w || !h) return;
  cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
  const ctx = cv.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  const total = steps.reduce((a, s) => a + s[0], 0) || 1;
  const top = Math.max(1.2, ...steps.map((s) => Math.max(s[1] ?? 0, s[2] ?? 0)));
  const X = (t) => (t / total) * w, Y = (f) => h - (f / top) * (h - 2);
  let t = 0;
  for (const [dur, lo, hi] of steps) {
    const x0 = X(t), x1 = X(t + dur);
    const past = opts.at != null && t + dur <= opts.at;
    if (lo == null) {                                              // free ride: a low hatched block
      ctx.fillStyle = blend(C.dim, C.panel, 0.7);
      ctx.fillRect(x0, Y(0.3), Math.max(1, x1 - x0 - 1), h - Y(0.3));
    } else {
      ctx.fillStyle = blend(zoneColour((lo + hi) / 2), C.panel, opts.at != null && !past ? 0.15 : opts.at != null ? 0.55 : 0.2);
      ctx.beginPath();
      ctx.moveTo(x0, h); ctx.lineTo(x0, Y(lo)); ctx.lineTo(Math.max(x0 + 1, x1 - 1), Y(hi)); ctx.lineTo(Math.max(x0 + 1, x1 - 1), h);
      ctx.closePath(); ctx.fill();
    }
    t += dur;
  }
  if (opts.ftpLine) {                                              // 100 % FTP for scale
    ctx.strokeStyle = blend(C.fg, C.panel, 0.75); ctx.setLineDash([3, 3]); ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(0, Y(1)); ctx.lineTo(w, Y(1)); ctx.stroke(); ctx.setLineDash([]);
  }
  if (opts.at != null) {                                           // where we are
    const x = Math.min(w - 1, X(opts.at));
    ctx.fillStyle = C.fg;
    ctx.fillRect(Math.round(x) - 1, 0, 2, h);
  }
}

// ---- the library (Workouts page) and the next ride's workout (Home)
async function loadWorkouts(force) {
  if (WO.loading || (!force && WO.list && performance.now() - WO.loadedAt < 15000)) return;
  WO.loading = true;
  try { WO.list = await (await fetch("/api/workouts")).json(); WO.loadedAt = performance.now(); }
  catch { return; } finally { WO.loading = false; }
  WO.drawn = WO.homeDrawn = null;
}
const woMin = (secs) => `${Math.round(secs / 60)} min`;
const est = (w) => w.estimate ? `${fmt(w.estimate.tss)} TSS · IF ${w.estimate.if.toFixed(2)}` : "set FTP for TSS";

function renderWorkoutsPage(s) {
  if (!WO.list || S.prevScreen !== "workouts") loadWorkouts(S.prevScreen !== "workouts");
  if (!WO.list) return;
  if (WO.sel == null || !WO.list.some((w) => w.id === WO.sel)) WO.sel = s.workout_id || WO.list[0]?.id;
  const key = JSON.stringify([WO.sel, s.workout_id, WO.loadedAt, THEME, s.config.ftp]);
  if (WO.drawn === key) return;
  WO.drawn = key;
  $("wo-count").textContent = WO.list.length;
  $("wo-rows").innerHTML = WO.list.map((w) =>
    `<button class="wo-row${w.id === WO.sel ? " on" : ""}" data-id="${esc(w.id)}">` +
    `<span class="wr-name"><b>${esc(w.name)}</b>${w.id === s.workout_id ? `<em class="link ant">next ride</em>` : ""}</span>` +
    `<span class="wr-meta">${woMin(w.total)} · ${est(w)}${w.file ? " · file" : ""}</span><canvas></canvas></button>`).join("");
  $("wo-rows").querySelectorAll(".wo-row").forEach((b, i) => {
    drawProfile(b.querySelector("canvas"), WO.list[i].steps);
    b.addEventListener("click", () => { WO.sel = b.dataset.id; WO.drawn = null; renderWorkoutsPage(S.snap); });
  });
  // the selected one in detail
  const w = WO.list.find((x) => x.id === WO.sel), ftp = s.config.ftp;
  $("wd-name").textContent = w.name;
  $("wd-desc").textContent = w.description || "";
  const e = w.estimate;
  $("wd-stats").innerHTML = [ovStat("Time", hms(w.total)), ovStat("TSS", e ? fmt(e.tss) : "--"), ovStat("IF", e ? e.if.toFixed(2) : "--"),
    ovStat("NP", e ? fmt(e.np_w) : "--", e ? "W" : ""), ovStat("Avg", e ? fmt(e.avg_w) : "--", e ? "W" : ""),
    ovStat("Work", e ? fmt(e.kj) : "--", e ? "kJ" : "")].join("");
  const pw = (f) => ftp ? fmt(f * ftp) : "--";
  $("wd-steps").innerHTML = w.steps.map(([dur, lo, hi, label], i) =>
    `<tr><td>${i + 1}</td><td>${esc(label || (lo == null ? "free ride" : ""))}</td><td class="n">${minsec(dur)}</td>` +
    `<td class="n">${lo == null ? "free" : lo === hi ? `${pw(lo)} W` : `${pw(lo)}–${pw(hi)} W`}</td>` +
    `<td class="n">${lo == null ? "" : lo === hi ? `${Math.round(lo * 100)}%` : `${Math.round(lo * 100)}–${Math.round(hi * 100)}%`}</td></tr>`).join("");
  const chosen = w.id === s.workout_id;
  $("wd-select").textContent = chosen ? "Next ride ✓" : "Use for next ride";
  $("wd-select").disabled = chosen;
  requestAnimationFrame(() => drawProfile($("wd-profile"), w.steps, { ftpLine: true }));
}
$("wd-select").addEventListener("click", (e) => { act("workout", WO.sel); e.currentTarget.blur(); });
$("wd-ride").addEventListener("click", async (e) => { e.currentTarget.blur(); await act("workout", WO.sel); act("start"); });

// ---- ride: the strip
function renderWorkout(s) {
  const w = s.workout, strip = $("wo-strip");
  strip.hidden = !w;
  if (!w) return;
  if (w.done && !w.name) {
    $("wo-now").innerHTML = `<span class="wo-step">Workout done</span><span class="wo-target">free ride</span>`;
    $("wo-next").textContent = ""; $("wo-ctl").hidden = true;
    drawProfile($("wo-profile"), [], {});
    return;
  }
  $("wo-ctl").hidden = false;
  const tgt = w.target, pct = tgt != null && w.ftp ? Math.round((100 * tgt) / w.ftp) : null;
  $("wo-now").innerHTML =
    `<span class="wo-step">${esc(w.label || `Step ${w.index + 1}`)} · ${minsec(Math.max(0, w.left))} left</span>` +
    `<span class="wo-target">${tgt == null ? "free ride" : `<span class="num">${fmt(tgt)}</span><span class="unit">W</span>` +
      `<span class="pct">${pct}%</span>`}</span>`;
  const n = w.next;
  $("wo-next").innerHTML = n ? `Next <b>${minsec(n.dur)}</b> ${n.lo == null ? "free ride"
    : `at <b>${fmt(n.lo * w.ftp * w.intensity)}${n.hi !== n.lo ? `–${fmt(n.hi * w.ftp * w.intensity)}` : ""}</b> W`}${n.label ? ` · ${esc(n.label)}` : ""}`
    : "Last step";
  $("wo-int").textContent = `${Math.round(w.intensity * 100)}%`;
  const erg = $("wo-erg");
  erg.textContent = w.erg ? "ERG on" : "ERG off";
  erg.classList.toggle("on", w.erg);
  const tr = w.trainer;
  $("wo-trainer").textContent = !s.settings.trainer ? "trainer not used"
    : !tr ? "" : tr.error ? tr.error : tr.controlled ? (w.erg && tgt != null ? "trainer holding target" : "trainer riding flat") : "waiting for the trainer…";
  $("wo-trainer").classList.toggle("warn", !!(tr && tr.error) || (s.settings.trainer && tr && !tr.controlled));
  drawProfile($("wo-profile"), w.steps, { at: w.t, ftpLine: true });
}

for (const [id, name, arg] of [["wo-down5", "wo_nudge", -5], ["wo-down", "wo_nudge", -1], ["wo-up", "wo_nudge", 1],
                               ["wo-up5", "wo_nudge", 5], ["wo-skip", "wo_skip"], ["wo-erg", "wo_erg"]]) {
  $(id).addEventListener("click", (e) => { act(name, arg); e.currentTarget.blur(); });
}
// ending the workout takes a second click within 3 s (a stray click mid-interval shouldn't end it)
$("wo-end").addEventListener("click", (e) => {
  const b = e.currentTarget;
  b.blur();
  if (b.classList.contains("confirm")) { b.classList.remove("confirm"); b.textContent = "End workout"; act("wo_end"); return; }
  b.classList.add("confirm"); b.textContent = "Click again to end";
  setTimeout(() => { b.classList.remove("confirm"); b.textContent = "End workout"; }, 3000);
});
