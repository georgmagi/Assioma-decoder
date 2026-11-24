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
  // fill: the highest step reaches the top (the ride strip); otherwise a fixed 120 % scale
  const peak = Math.max(0, ...steps.map((s) => Math.max(s[1] ?? 0, s[2] ?? 0)));
  const top = opts.fill ? Math.max(0.5, peak) * 1.04 : Math.max(1.2, peak);
  const X = (t) => (t / total) * w, Y = (f) => h - (f / top) * (h - 2);
  // during a ride the part already ridden is greyed as it goes, also inside a long step (a 90-min
  // block used to stay bright to its end and then grey all at once): each step is filled bright, then
  // again faded, clipped to the left of "now"
  const xNow = opts.at != null ? X(opts.at) : null;
  let t = 0;
  for (const [dur, lo, hi] of steps) {
    const x0 = X(t), x1 = X(t + dur);
    const shape = () => {
      ctx.beginPath();
      if (lo == null) ctx.rect(x0, Y(0.3), Math.max(1, x1 - x0 - 1), h - Y(0.3));   // free ride: a low block
      else {
        ctx.moveTo(x0, h); ctx.lineTo(x0, Y(lo)); ctx.lineTo(Math.max(x0 + 1, x1 - 1), Y(hi)); ctx.lineTo(Math.max(x0 + 1, x1 - 1), h);
        ctx.closePath();
      }
    };
    const colour = lo == null ? C.dim : zoneColour((lo + hi) / 2);
    const fade = lo == null ? 0.7 : xNow == null ? 0.2 : 0.15;
    ctx.fillStyle = blend(colour, C.panel, fade);
    shape(); ctx.fill();
    if (xNow != null && xNow > x0) {                               // ridden: faded, up to now
      ctx.save();
      ctx.beginPath(); ctx.rect(x0, 0, Math.min(x1, xNow) - x0, h); ctx.clip();
      ctx.fillStyle = C.panel; shape(); ctx.fill();                // cover the bright fill first
      ctx.fillStyle = blend(colour, C.panel, lo == null ? 0.82 : 0.55);
      shape(); ctx.fill();
      ctx.restore();
    }
    t += dur;
  }
  if (opts.ftpLine) {                                              // 100 % FTP for scale
    ctx.strokeStyle = blend(C.fg, C.panel, 0.75); ctx.setLineDash([3, 3]); ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(0, Y(1)); ctx.lineTo(w, Y(1)); ctx.stroke(); ctx.setLineDash([]);
  }
  if (opts.watts) {                                                // the ride strip: watts on the work steps that fit
    ctx.font = "600 11px 'Barlow Semi Condensed', 'IBM Plex Sans', sans-serif";
    ctx.textAlign = "center"; ctx.textBaseline = "top";
    let t2 = 0;
    for (const [dur, lo, hi] of steps) {
      const x0 = X(t2), x1 = X(t2 + dur), txt = lo == null ? "" : `${Math.round(((lo + hi) / 2) * opts.watts)}`;
      if (lo != null && lo === hi && lo >= 0.75 && x1 - x0 >= 34 && h - Y(lo) >= 18) {
        ctx.fillStyle = THEME === "lcd" ? C.lcd || C.panel : C.panel;
        ctx.fillText(txt, (x0 + x1) / 2, Y(lo) + 3);
      }
      t2 += dur;
    }
  }
  if (opts.at != null) {                                           // where we are
    const x = Math.min(w - 1, X(opts.at));
    ctx.fillStyle = C.fg;
    ctx.fillRect(Math.round(x) - 1, 0, 2, h);
  }
}

// what a step is, for the strip: its own label, else from where it sits and how hard it is
function stepNames(steps) {
  const mid = (s) => (s[1] == null ? null : (s[1] + s[2]) / 2);
  const work = steps.map((s) => mid(s) != null && mid(s) >= 0.85 && s[0] >= 30);
  const nWork = work.filter(Boolean).length;
  let k = 0;
  return steps.map((s, i) => {
    if (s[3]) return s[3][0].toUpperCase() + s[3].slice(1);
    if (work[i]) return nWork > 1 ? `Interval ${++k} of ${nWork}` : "Main block";
    if (s[1] == null) return "Free ride";
    if (i === 0) return "Warm-up";
    if (i === steps.length - 1) return "Cool-down";
    if (work[i - 1] && mid(s) < 0.65) return "Recovery";
    return "Steady";
  });
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
  const changed = WO.pv !== s.plan_version;                     // a workout saved / deleted, or the plan changed
  WO.pv = s.plan_version;
  if (!WO.list || S.prevScreen !== "workouts" || changed) loadWorkouts(S.prevScreen !== "workouts" || changed);
  if (!WO.list) return;
  const LIB = WO.list.filter((w) => !w.id.startsWith("plan:"));     // the plan's own days live on the Plan page
  if (WO.sel == null || !LIB.some((w) => w.id === WO.sel)) WO.sel = LIB.some((w) => w.id === s.workout_id) ? s.workout_id : LIB[0]?.id;
  const key = JSON.stringify([WO.sel, s.workout_id, WO.loadedAt, THEME, s.config.ftp]);
  if (WO.drawn === key) return;
  WO.drawn = key;
  $("wo-count").textContent = LIB.length;
  $("wo-rows").innerHTML = LIB.map((w) =>
    `<button class="wo-row${w.id === WO.sel ? " on" : ""}" data-id="${esc(w.id)}">` +
    `<span class="wr-name"><b>${esc(w.name)}</b>${w.id === s.workout_id ? `<em class="link ant">next ride</em>` : ""}</span>` +
    `<span class="wr-meta">${woMin(w.total)} · ${est(w)}${w.file ? " · file" : ""}</span><canvas></canvas></button>`).join("");
  $("wo-rows").querySelectorAll(".wo-row").forEach((b, i) => {
    drawProfile(b.querySelector("canvas"), LIB[i].steps);
    b.addEventListener("click", () => { WO.sel = b.dataset.id; WO.drawn = null; renderWorkoutsPage(S.snap); });
  });
  // the selected one in detail
  const w = LIB.find((x) => x.id === WO.sel), ftp = s.config.ftp;
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
  $("wd-del").hidden = !w.id.startsWith("zwo:");
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
    $("wo-step").textContent = "Workout done"; $("wo-target").textContent = "free ride"; $("wo-left").textContent = "";
    $("wo-next").textContent = ""; $("wo-name").textContent = ""; $("wo-total").textContent = ""; $("wo-ctl").hidden = true;
    drawProfile($("wo-profile"), [], {});
    return;
  }
  $("wo-ctl").hidden = false;
  const names = stepNames(w.steps), st = w.steps[w.index] || [];
  const tgt = w.target, pct = tgt != null && w.ftp ? Math.round((100 * tgt) / w.ftp) : null;
  const ramp = st[1] != null && st[1] !== st[2];                        // a ramp: where it's heading
  $("wo-step").innerHTML = `${esc(names[w.index] || `Step ${w.index + 1}`)} <span>· step ${w.index + 1} of ${w.steps.length}</span>`;
  $("wo-target").innerHTML = tgt == null ? `<span class="num">free</span>`
    : `<span class="num">${fmt(tgt)}</span><span class="unit">W</span><span class="sub">${ramp ? `→ ${fmt(st[2] * w.ftp * w.intensity)}` : `${pct}%`}</span>`;
  $("wo-left").innerHTML = `<span class="num">${minsec(Math.max(0, w.left))}</span>`;
  const n = w.next;
  $("wo-next").innerHTML = n ? `Next: ${esc(names[w.index + 1] || "")} · ${n.lo == null ? "free"
    : `<b>${fmt(n.lo * w.ftp * w.intensity)}${n.hi !== n.lo ? `–${fmt(n.hi * w.ftp * w.intensity)}` : ""} W</b>`} · ${minsec(n.dur)}`
    : "Last step";
  const rest = Math.max(0, w.total - w.t);
  $("wo-name").textContent = w.name || "";
  $("wo-total").textContent = `${minsec(rest)} left of ${minsec(w.total)}`;
  $("wo-int").textContent = `${Math.round(w.intensity * 100)}%`;
  const erg = $("wo-erg");
  erg.textContent = "ERG";
  erg.title = w.erg ? "ERG on: the trainer holds the target. Click to ride it freely." : "ERG off. Click to let the trainer hold the target.";
  const match = $("wo-match");
  match.classList.toggle("on", w.pm != null);
  match.title = w.pm != null ? "Power match on: ERG holds the target by your pedals. Click to use the trainer's own reading."
    : "Power match off: ERG holds the target by the trainer's own reading. Click to steer by your pedals.";
  erg.classList.toggle("on", w.erg);
  const ceil = $("wo-ceil");
  ceil.classList.toggle("on", !!w.ceiling);
  ceil.textContent = w.ceiling && w.cap < 1 ? `VT1 cap −${Math.round((1 - w.cap) * 100)}%` : "VT1 cap";
  const tr = w.trainer;
  $("wo-trainer").textContent = !s.settings.trainer ? "trainer not used"
    : !tr ? "" : tr.error ? tr.error
    : tr.controlled ? (w.erg && tgt != null ? (w.ceiling && w.cap < 1 ? `held under VT1: ${fmt(tgt * w.cap)} W` : "trainer holding")
                      + (w.pm != null && Math.abs(w.pm - 1) >= 0.005 ? ` · power match ${w.pm > 1 ? "+" : "−"}${(Math.abs(w.pm - 1) * 100).toFixed(1)}%` : w.pm != null ? " · power match" : "")
                      : "trainer flat")
    : "waiting for the trainer…";
  $("wo-trainer").classList.toggle("warn", !!(tr && tr.error) || (s.settings.trainer && tr && !tr.controlled));
  drawProfile($("wo-profile"), w.steps, { at: w.t, ftpLine: true, fill: true, watts: w.ftp * w.intensity });
}

for (const [id, name, arg] of [["wo-match", "wo_match"], ["wo-down", "wo_nudge", -1], ["wo-up", "wo_nudge", 1], ["wo-skip", "wo_skip"], ["wo-erg", "wo_erg"], ["wo-ceil", "wo_ceiling"]]) {
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

// ---- the builder: the shorthand (workout.parse_text) with a live preview, saved as a .zwo file
const WB = { timer: null, open: false, id: null };
function openBuilder(name, text, id = null) {
  WB.open = true; WB.id = id;
  $("wb-title").textContent = id ? "Edit workout" : "New workout";
  $("wo-detail").hidden = true; $("wo-build").hidden = false;
  $("wb-name").value = name || ""; $("wb-text").value = text || "";
  previewBuilder();
  $(text ? "wb-name" : "wb-text").focus();
}
function closeBuilder() { WB.open = false; $("wo-build").hidden = true; $("wo-detail").hidden = false; }
async function previewBuilder() {
  const text = $("wb-text").value.trim();
  if (!text) { $("wb-err").textContent = ""; $("wb-stats").innerHTML = ""; drawProfile($("wb-profile"), []); return; }
  let r;
  try { r = await (await fetch("/api/parse?text=" + encodeURIComponent(text))).json(); } catch { return; }
  $("wb-err").textContent = r.error || "";
  if (r.error) return;
  const e = r.estimate, tot = r.split.easy + r.split.moderate + r.split.hard;
  $("wb-stats").innerHTML = [ovStat("Time", hms(r.total)), ovStat("TSS", e ? fmt(e.tss) : "--"), ovStat("IF", e ? e.if.toFixed(2) : "--"),
    ovStat("NP", e ? fmt(e.np_w) : "--", e ? "W" : ""), ovStat("Easy", `${Math.round((100 * r.split.easy) / tot)}`, "%"),
    ovStat("Hard", `${Math.round((100 * r.split.hard) / tot)}`, "%")].join("");
  drawProfile($("wb-profile"), r.steps, { ftpLine: true });
}
$("wb-text").addEventListener("input", () => { clearTimeout(WB.timer); WB.timer = setTimeout(previewBuilder, 250); });
for (const id of ["wb-text", "wb-name"]) $(id).addEventListener("keydown", (e) => e.stopPropagation());
$("wo-new").addEventListener("click", (e) => { e.currentTarget.blur(); openBuilder("", ""); });
$("wb-cancel").addEventListener("click", closeBuilder);
$("wb-save").addEventListener("click", async () => {
  await act("wo_save", { id: WB.id || undefined, name: $("wb-name").value, text: $("wb-text").value });
  if ((S.snap?.plan_msg || "").startsWith("Saved")) { closeBuilder(); loadWorkouts(true); }
  else $("wb-err").textContent = S.snap?.plan_msg || "";
});
$("wd-edit").addEventListener("click", async (e) => {
  e.currentTarget.blur();
  const r = await (await fetch("/api/workout_text?id=" + encodeURIComponent(WO.sel))).json();
  if (!r.error) openBuilder(r.name, r.text, r.id);
});
$("wd-del").addEventListener("click", (e) => {                     // your own files only, and a second click to confirm
  const b = e.currentTarget;
  b.blur();
  if (b.classList.contains("confirm")) { b.classList.remove("confirm"); b.textContent = "Delete"; act("wo_delete", WO.sel); WO.sel = null; loadWorkouts(true); return; }
  b.classList.add("confirm"); b.textContent = "Click again to delete";
  setTimeout(() => { b.classList.remove("confirm"); b.textContent = "Delete"; }, 3000);
});

// ---- the 20-min test (pacing.py): what to do now in big type, the numbers that matter, and the
// test so far on one plot (power left, heart rate right, the heart-rate range a sustainable 20 min
// runs in shaded). You press - / + yourself; a new "+2 %" / "ease" / "everything" flashes once.
const PACE = { kind: null };
function mmss(s) { s = Math.max(0, Math.round(s)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; }
function renderPace(g) {
  const el = $("pace");
  el.hidden = !g;
  if (!g) { PACE.kind = null; return; }
  el.querySelector(".pace-body").toggleAttribute("data-pre", g.pre != null);
  if (g.pre != null) {
    $("pace-left").textContent = `starts in ${mmss(g.pre)}`;
    setCue({ kind: "pre", text: `Test in ${mmss(g.pre)}`, sub: "Fan on your face, a drink, settle your breathing. Start at the target and hold it for 3 minutes." });
    $("pace-stats").innerHTML = ""; drawPace(null);
    return;
  }
  $("pace-left").textContent = `${mmss(g.left)} left`;
  setCue(g.cue);
  const st = (k, v, u, sub) => `<div><dt>${k}</dt><dd><span class="num">${v ?? "--"}</span><span class="unit">${u}</span></dd>${sub ? `<dd class="s">${sub}</dd>` : ""}</div>`;
  const tr = g.trend == null ? "" : `${g.trend > 0 ? "+" : g.trend < 0 ? "−" : ""}${Math.abs(g.trend).toFixed(1)} /min`;
  $("pace-stats").innerHTML = st("Average so far", g.avg, "W") + st("Projected 20 min", g.proj, "W", g.target ? `at ${g.target} W from here` : "") +
    st("FTP", g.ftp, "W", "95 % of the projection") +
    st("Heart rate", g.hr, "bpm", [g.band ? `range ${Math.round(g.band[0])}–${Math.round(g.band[1])}` : "", tr].filter(Boolean).join(" · "));
  drawPace(g);
}
function setCue(c) {
  const box = $("pace-cue");
  if ($("pace-word").textContent !== c.text) $("pace-word").textContent = c.text;
  if ($("pace-sub").textContent !== c.sub) $("pace-sub").textContent = c.sub;
  box.dataset.kind = c.kind;
  const key = `${c.kind}|${c.text}`;
  if (PACE.kind !== null && key !== PACE.kind && ["up", "down", "go"].includes(c.kind)) {
    box.classList.remove("flash"); void box.offsetWidth; box.classList.add("flash");
  }
  PACE.kind = key;
}
function drawPace(g) {
  const cv = $("pace-cv"), dpr = devicePixelRatio || 1, wd = cv.parentElement.clientWidth, ht = Math.max(120, cv.parentElement.clientHeight - 22);
  if (!wd) return;
  cv.style.width = wd + "px"; cv.style.height = ht + "px";
  cv.width = Math.round(wd * dpr); cv.height = Math.round(ht * dpr);
  const ctx = cv.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, wd, ht);
  const MK = marksOf(cv);
  if (!g) return;
  const dur = g.dur, pts = g.chart, ml = 34, mr = 34, mt = 6, mb = 18, W = wd - ml - mr, H = ht - mt - mb;
  const X = (s) => ml + (s / dur) * W;
  const ps = pts.map((p) => p[1]).filter((v) => v != null), hs = pts.map((p) => p[2]).filter((v) => v != null);
  const tg = g.target || g.avg || 250;
  const pLo = Math.floor(Math.min(tg * 0.8, ...ps) / 20) * 20, pHi = Math.ceil(Math.max(tg * 1.15, ...ps) / 20) * 20;
  const hLo = Math.floor(Math.min(g.lt - 25, ...hs) / 10) * 10, hHi = Math.ceil(Math.max(g.lt + 30, ...hs) / 10) * 10;
  const P = (v) => mt + H - ((v - pLo) / (pHi - pLo)) * H, Hy = (v) => mt + H - ((v - hLo) / (hHi - hLo)) * H;
  const lcd = THEME === "lcd", hrCol = lcd ? C.fg : C.hr, pwCol = lcd ? C.fg : C.power;
  ctx.font = "11px 'Barlow Semi Condensed', 'IBM Plex Sans', sans-serif"; ctx.lineWidth = 1;
  ctx.textAlign = "right";
  for (let v = pLo; v <= pHi; v += 20) {
    ctx.strokeStyle = C.grid; ctx.beginPath(); ctx.moveTo(ml, P(v)); ctx.lineTo(ml + W, P(v)); ctx.stroke();
    ctx.fillStyle = C.dim; ctx.fillText(`${v}`, ml - 5, P(v) + 4);
  }
  ctx.textAlign = "left"; ctx.fillStyle = hrCol;
  for (let v = hLo; v <= hHi; v += 10) ctx.fillText(`${v}`, ml + W + 5, Hy(v) + 4);
  ctx.textAlign = "center"; ctx.fillStyle = C.dim;
  for (let m = 0; m <= dur / 60; m += 5) ctx.fillText(`${m}`, X(m * 60), ht - 4);
  // the heart-rate range a sustainable 20 min runs in, from 3 min
  const b = g.bands;
  if (b.length > 1) {
    ctx.fillStyle = lcd ? "rgba(27,30,25,0.10)" : blend(C.hr, C.panel, 0.86);
    ctx.beginPath(); b.forEach((r, i) => (i ? ctx.lineTo(X(r[0]), Hy(r[2])) : ctx.moveTo(X(r[0]), Hy(r[2]))));
    for (let i = b.length - 1; i >= 0; i--) ctx.lineTo(X(b[i][0]), Hy(b[i][1]));
    ctx.closePath(); ctx.fill();
  }
  if (g.target) {                                                   // the target from here
    ctx.strokeStyle = blend(C.fg, C.panel, 0.25); ctx.lineWidth = 1.5; ctx.setLineDash([4, 4]); ctx.beginPath();
    ctx.moveTo(X(g.t), P(g.target)); ctx.lineTo(ml + W, P(g.target)); ctx.stroke(); ctx.setLineDash([]);
  }
  const line = (k, Y, colour, width, dash) => {
    ctx.strokeStyle = colour; ctx.lineWidth = width; ctx.setLineDash(dash ? [5, 4] : []); ctx.beginPath();
    let on = false;
    for (const p of pts) { if (p[k] == null) { on = false; continue; } on ? ctx.lineTo(X(p[0]), Y(p[k])) : ctx.moveTo(X(p[0]), Y(p[k])); on = true; }
    ctx.stroke(); ctx.setLineDash([]);
  };
  line(2, Hy, hrCol, 1.75, lcd); line(1, P, pwCol, 2.25, false);
  $("pace-key-hr").style.color = "";
  for (const p of pts) {
    const r = b.find((x) => x[0] >= p[0]);
    MK.push({ x: X(p[0]), y: p[1] != null ? P(p[1]) : Hy(p[2] || hLo), html: tipHtml(mmss(p[0]), [
      ["power", p[1] ?? "--", "W", pwCol], ["heart rate", p[2] ?? "--", "bpm", hrCol],
      p[0] >= 180 && r && ["range", `${Math.round(r[1])}–${Math.round(r[2])}`, "bpm"]]) });
  }
}
window.addEventListener("resize", () => { if (!$("pace").hidden && S.snap) drawPace(S.snap.pacing && S.snap.pacing.pre == null ? S.snap.pacing : null); });
