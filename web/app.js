// Assioma Recorder window. Python owns the ride; this page polls /api/state 4 times a second
// (a snapshot plus the graph points that arrived since the last poll) and sends button presses
// to /api/action. Everything here is display only.
"use strict";

const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* private window */ } },
};
// Two themes: dark, and LCD (a rowing-machine monitor: grey-green reflective LCD, everything in one
// ink, series told apart by weight and fill instead of colour). The CSS side is [data-theme="lcd"].
const INK = "#1b1e19";
const PALETTES = {
  dark: { bg: "#0b0d10", panel: "#12151a", grid: "#1b1f25", fg: "#e6e8eb", dim: "#7c8490", faint: "#4a515a",
          power: "#5b9dff", hr: "#ff4d6d", hrDim: "#3a1b24", z2: "#132019", a1: "#c08cff", rr: "#2dd4bf",
          good: "#3ddc97", warn: "#ffb020", bad: "#ff5c5c" },
  lcd: { bg: "#383b3e", panel: "#a9b49f", grid: "#98a48e", fg: INK, dim: "#3f463a", faint: "#76806e",
         power: INK, hr: INK, hrDim: "#8e9985", z2: "#9ca991", a1: INK, rr: INK,
         good: INK, warn: INK, bad: INK },
};
let THEME = PALETTES[store.get("theme", "dark")] ? store.get("theme", "dark") : "dark";
const C = { ...PALETTES[THEME] };
document.documentElement.dataset.theme = THEME;
const ZONES = [[0.55, "Z1", "#9aa3ad"], [0.75, "Z2", "#5b9dff"], [0.90, "Z3", "#3ddc97"],
               [1.05, "Z4", "#ffd166"], [1.20, "Z5", "#ff9f43"], [99, "Z6", "#ff5c5c"]];   // Coggan, share of FTP
const CADENCE_BAND = [80, 100];     // rpm; the number dims outside it while pedalling
const POLL_MS = 250;

const $ = (id) => document.getElementById(id);
const fmt = (v, d = 0) => (v == null ? "--" : d ? v.toFixed(d) : String(Math.round(v)));
const hms = (s) => { s = Math.max(0, Math.round(s));
  return `${Math.floor(s / 3600)}:${String(Math.floor(s / 60) % 60).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`; };

function blend(c1, c2, k) {
  const a = [1, 3, 5].map((i) => parseInt(c1.slice(i, i + 2), 16));
  const b = [1, 3, 5].map((i) => parseInt(c2.slice(i, i + 2), 16));
  return "#" + a.map((x, i) => Math.round(x + (b[i] - x) * k).toString(16).padStart(2, "0")).join("");
}
function zone(watts, ftp) {
  if (!ftp || watts == null) return null;
  for (const [top, name, colour] of ZONES) if (watts <= top * ftp) return { name, colour };
  return null;
}
function alphaColour(a, art) {
  if (a == null) return C.dim;
  if (art != null && art > 5) return C.faint;          // too many artifacts to trust
  return a >= 0.75 ? C.good : a >= 0.5 ? C.warn : C.bad;
}

// --------------------------------------------------------------------------- state
const S = {
  snap: null, p: 0, h: 0, a: 0, b: 0, fails: 0,
  offset: null,                         // server clock (s since first data) minus performance.now()/1000
  span: store.get("span", 300),         // graph range in s; 0 = the whole ride
  dialogKey: null,
  power3s: store.get("power3s", false),  // power tile: 3 s average instead of instantaneous
};
const serverNow = () => (S.offset == null ? null : performance.now() / 1000 + S.offset);

// --------------------------------------------------------------------------- numbers that roll
const tweens = {};                      // id -> {from, to, t0}
const TWEEN_S = 0.18;
function tweenTo(id, v) {
  const el = $(id);
  if (v == null) { delete tweens[id]; el.textContent = "--"; return; }
  const tw = tweens[id];
  if (!tw) { tweens[id] = { from: v, to: v, t0: 0 }; el.textContent = fmt(v); return; }
  if (v !== tw.to) tweens[id] = { from: tweenAt(tw), to: v, t0: performance.now() / 1000 };
}
function tweenAt(tw) {
  const k = Math.min(1, (performance.now() / 1000 - tw.t0) / TWEEN_S);
  return tw.from + (tw.to - tw.from) * (1 - (1 - k) ** 3);
}
function stepTweens() {
  for (const [id, tw] of Object.entries(tweens)) $(id).textContent = fmt(tweenAt(tw));
}

// --------------------------------------------------------------------------- tiles
function renderTiles(s) {
  const v = s.live, st = s.stats, h = s.hrv, ftp = s.config.ftp;
  // power: coloured by zone (the zone follows the real value, not the rolling number)
  const pw = S.power3s ? v.power_3s : v.power_w;
  const z = pw ? zone(pw, ftp) : null;
  tweenTo("power", pw);
  $("power").style.color = z && THEME !== "lcd" ? z.colour : C.fg;
  const badge = $("zone");
  badge.textContent = z ? z.name : "";
  if (z) { badge.style.color = z.colour; badge.style.background = blend(C.panel, z.colour, 0.18); }
  $("power-sub").textContent = `avg ${fmt(st.avg_power)} · NP ${fmt(st.np)}`;

  const cad = v.cadence_rpm;
  tweenTo("cadence", cad);
  $("cadence").classList.toggle("dim", !!cad && (cad < CADENCE_BAND[0] || cad > CADENCE_BAND[1]));
  $("cadence-sub").textContent = `avg ${fmt(st.avg_cadence)}`;

  const bal = v.balance_right_pct, ab = st.balance_right;
  $("bal-l").textContent = bal == null ? "--" : fmt(100 - bal);
  $("bal-r").textContent = bal == null ? "--" : fmt(bal);
  $("bal-l-sub").textContent = ab == null ? "" : `avg ${(100 - ab).toFixed(1)}`;
  $("bal-r-sub").textContent = ab == null ? "" : ab.toFixed(1);

  const bpm = v.heart_rate_bpm;
  tweenTo("hr", bpm);
  $("hr").style.color = bpm ? C.hr : C.fg;
  const hrSub = $("hr-sub");
  const warn = s.strap && s.strap.warn;
  // the Z2 guard: your 30-s heart rate against your own VT1 (recorder_app Engine._guard)
  const g = s.guard, gs = g && g.ref && g.state;
  hrSub.textContent = !s.strap ? "" : warn ? warn
    : gs === "above" ? (g.headroom != null && g.headroom < 0 ? `${fmt(-g.headroom)} above VT1 ${fmt(g.ref.hr)}` : `α1 says above VT1`)
    : gs ? `${fmt(g.headroom)} below VT1 ${fmt(g.ref.hr)}` : `RR ${fmt(h.rr)} ms`;
  hrSub.classList.toggle("warn", !!warn || gs === "near");
  hrSub.classList.toggle("bad", !warn && gs === "above");
  $("t-hr").classList.toggle("over", !warn && gs === "above");

  const a1 = h.alpha1, a1Sub = $("a1-sub");
  $("a1").textContent = fmt(a1, 2);
  $("a1").style.color = alphaColour(a1, h.art);
  const partial = (h.cov ?? 100) < 97;             // beats missing from the strap: read α1 with care
  const low = g ? g.a1_low_s : 0, aboveS = g && g.ref ? g.above_s : 0;
  a1Sub.textContent = a1 == null ? (h.note || "")
    : low >= 30 ? `under 0.75 for ${minsec(low)}`
    : partial ? `${fmt(h.cov)}% of beats · art ${fmt(h.art, 1)}%`
    : aboveS >= 60 ? `${minsec(aboveS)} above VT1 · art ${fmt(h.art, 1)}%`
    : `RMSSD ${fmt(h.rmssd, 1)} · art ${fmt(h.art, 1)}%`;
  a1Sub.classList.toggle("warn", a1 != null && (partial || low >= 30));

  const r = s.ride;
  $("clock").textContent = r.clock;
  $("clock").style.color = r.state === "recording" ? C.fg : C.dim;
  const cs = $("clock-sub");
  cs.textContent = { ready: "not started", paused: "paused", finished: "saved" }[r.state] || `elapsed ${r.elapsed}`;
  cs.classList.toggle("warn", r.state === "paused");
}

function showPowerMode() {
  $("power-mode").textContent = S.power3s ? "3 s" : "";
  $("t-power").title = S.power3s ? "3 s average. Click for instantaneous." : "Instantaneous. Click for the 3 s average.";
}
$("t-power").addEventListener("click", () => {
  S.power3s = !S.power3s; savePref("power3s", S.power3s);
  showPowerMode();
  if (S.snap) renderTiles(S.snap);
});
showPowerMode();

// --------------------------------------------------------------------------- heartbeats in the HR tile
// One faint full-height line per beat, sliding in at the right edge and drifting left over 2 s, the
// newest brighter; a dot in the corner pulses as each beat comes in. The right edge shows
// "now - 0.4 s": a beat is stamped when it arrives (ANT+: within ~0.25 s of the beat) and the page
// fetches every 0.25 s, so every beat still enters at the edge.
const STRIP = { span: 2.0, delay: 0.4, glow: 0.6, edge: null, pulseAt: -1 };
function drawBeats() {
  const cv = $("beats"), s = S.snap, now = serverNow();
  const dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth, hgt = cv.clientHeight;
  if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(hgt * dpr)) {
    cv.width = Math.round(w * dpr); cv.height = Math.round(hgt * dpr);
  }
  const ctx = cv.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, hgt);
  if (!s || !s.strap || now == null || s.ride.state === "finished") return;
  const warn = !!s.strap.warn;
  const rad = 12, xa = rad, xb = w - rad, speed = (xb - xa) / STRIP.span;
  const edge = now - STRIP.delay, tw = Math.max(1, hgt * 0.012);
  for (let i = s.beats.length - 1; i >= 0; i--) {
    const [t, art] = s.beats[i];
    const age = edge - t;
    if (age < 0) continue;                                        // not at the edge yet
    if (STRIP.edge == null || t > STRIP.edge) { STRIP.edge = t; STRIP.pulseAt = performance.now(); }
    const x = xb - age * speed;
    if (x < xa) break;
    const fresh = Math.max(0, 1 - age / STRIP.glow) ** 2;
    const k = 0.8 + 0.14 * Math.min(1, age / STRIP.span) - 0.45 * fresh;
    ctx.fillStyle = blend(art ? C.warn : C.hr, C.panel, k);
    ctx.fillRect(Math.round(x - tw), 1.5, 2 * tw, hgt - 3);
  }
  // the pulse dot, top right, level with the label
  const r = Math.max(3, hgt * 0.028), dx = w - Math.max(14, w * 0.08) - r, dy = Math.max(14, hgt * 0.14) + r * 0.6;
  const pulse = Math.max(0, 1 - (performance.now() - STRIP.pulseAt) / 350);
  if (pulse > 0 && !warn) {
    const kk = 1 - pulse, rr = r * (1.3 + 1.6 * kk);
    ctx.strokeStyle = blend(C.hr, C.panel, 0.35 + 0.65 * kk);
    ctx.lineWidth = Math.max(1, r * 0.25);
    ctx.beginPath(); ctx.arc(dx, dy, rr, 0, 2 * Math.PI); ctx.stroke();
  }
  ctx.fillStyle = warn ? C.warn : blend(C.hrDim, C.hr, Math.sqrt(pulse));
  ctx.beginPath(); ctx.arc(dx, dy, r, 0, 2 * Math.PI); ctx.fill();
}

// --------------------------------------------------------------------------- graphs
// Two charts sharing the time axis and the cursor: power + heart rate, and beat-to-beat intervals
// + DFA alpha 1, each pair on overlapping scales placed apart (see RANGES). The scales fit what is
// visible.
//
// Power and heart rate are binned per second: power as the second's mean (a faint line, 5 min view
// only) and a centred 10 s average (the main line), heart rate as a 3 s mean (15 s when zoomed
// out). Short gaps are bridged; a longer one breaks the line.
const G = { x: [], pw: [], pa: [], hr: [], hz: [], ps: [], pn: [], hs: [], hn: [], lastP: -1, lastH: -1 };
function grow(i) {
  for (let k = G.x.length; k <= i; k++) {
    G.x.push(k); G.pw.push(null); G.pa.push(null); G.hr.push(null); G.hz.push(null);
    G.ps.push(0); G.pn.push(0); G.hs.push(0); G.hn.push(0);
  }
}
// Beats and alpha 1 have their own times, so the second chart has its own x: every beat and every
// alpha 1 point. A series without a value at an x gets `undefined` (uPlot joins over it); `null`
// breaks the line (strap dropout, alpha 1 missing for over 15 s).
const B = { x: [], rr: [], ra: [], ag: [], ab: [], lastBeat: null, lastA: null };
function resetGraph() {
  for (const k of ["x", "pw", "pa", "hr", "hz", "ps", "pn", "hs", "hn"]) G[k] = [];
  for (const k of ["x", "rr", "ra", "ag", "ab"]) B[k] = [];
  G.lastP = G.lastH = -1; B.lastBeat = B.lastA = null; S.p = S.h = S.a = S.b = 0;
  for (const k in HELD) delete HELD[k];                          // a new ride gets its own scales
}
const niceUp = (v, step) => step * Math.ceil(v / step);
function addPower(t, w) {
  const i = Math.floor(t);
  if (i < 0) return;
  grow(i);
  G.ps[i] += w; G.pn[i]++;
  G.pw[i] = G.ps[i] / G.pn[i];
  const L = G.lastP;
  if (L >= 0 && i - L > 1 && i - L <= 3) for (let j = L + 1; j < i; j++) G.pw[j] = G.pw[L];
  G.lastP = Math.max(G.lastP, i);
  for (let j = Math.max(0, i - 5); j <= i; j++) G.pa[j] = centred(j);
}
function centred(j) {        // 10 s average centred on j (the newest seconds use what exists), so the
  let sum = 0, n = 0;        // line sits on the effort instead of trailing it; the tile has the live value
  for (let k = Math.max(0, j - 5); k <= Math.min(G.lastP, j + 4); k++) if (G.pw[k] != null) { sum += G.pw[k]; n++; }
  return G.pw[j] == null || !n ? null : sum / n;
}
function addHr(t, bpm) {
  if (!bpm) return;
  const i = Math.floor(t);
  if (i < 0) return;
  grow(i);
  G.hs[i] += bpm; G.hn[i]++;
  const L = G.lastH;
  if (L >= 0 && i - L > 1 && i - L <= 5) for (let j = L + 1; j < i; j++) G.hr[j] = G.hr[L];
  G.lastH = Math.max(G.lastH, i);
  let sum = 0, n = 0;
  for (let j = i - 2; j <= i; j++) if (j >= 0 && G.hn[j]) { sum += G.hs[j] / G.hn[j]; n++; }
  G.hr[i] = sum / n;
  for (let j = Math.max(0, i - 7); j <= i; j++) {         // 15 s centred mean for the zoomed-out views
    let a = 0, m = 0;
    for (let k = Math.max(0, j - 7); k <= Math.min(G.lastH, j + 7); k++) if (G.hr[k] != null) { a += G.hr[k]; m++; }
    G.hz[j] = G.hr[j] == null || !m ? null : a / m;
  }
}
function lowerBound(arr, t) {
  let lo = 0, hi = arr.length;
  while (lo < hi) { const m = (lo + hi) >> 1; if (arr[m] < t) lo = m + 1; else hi = m; }
  return lo;
}
function bInsert(t, vals) {                                   // almost always an append
  const i = B.x.length && t < B.x[B.x.length - 1] ? lowerBound(B.x, t) : B.x.length;
  B.x.splice(i, 0, t);
  for (const k of ["rr", "ra", "ag", "ab"]) B[k].splice(i, 0, k in vals ? vals[k] : undefined);
}
function addBeat(t, rr, artifact) {
  if (B.lastBeat != null && t - B.lastBeat > 3) bInsert(B.lastBeat + 0.001, { rr: null });   // dropout
  bInsert(t, artifact ? { ra: rr } : { rr });                 // an artifact is a dot; the line joins over it
  B.lastBeat = t;
}
// alpha 1 every 5 s: good stretches in ag, those with over 5 % artifacts in ab (drawn faint); where
// the quality changes the line stays joined
function addA1(t, v, art) {
  const L = B.lastA;
  if (v == null) { if (L) bInsert(L.t + 0.001, { ag: null, ab: null }); B.lastA = null; return; }
  const good = art == null || art <= 5, k = good ? "ag" : "ab";
  if (L && t - L.t > 15) bInsert(L.t + 0.001, { ag: null, ab: null });
  else if (L && L.good !== good) bInsert(L.t + 0.0001, { [k]: L.v });
  bInsert(t, { [k]: v });
  B.lastA = { t, v, good };
}

const VIEWS = [300, 1800, 0];                   // 5 min, 30 min, the whole ride (s; 0 = ride)
if (!VIEWS.includes(S.span)) S.span = 300;
// The recent past ends DELAY s ago: by then every second is complete, so the right edge never
// wobbles while a second fills in, and the charts can scroll smoothly every frame (see frame()).
const DELAY = 2;
function xRange() {
  const now = serverNow() ?? 0;
  if (!S.span) return [0, Math.max(now, (S.snap?.config.plan_min || 60) * 60)];
  return [now - DELAY - S.span, now - DELAY];
}
let lastScroll = 0;
function scrollCharts() {                       // ~30 fps, only the rolling views
  const t = performance.now();
  if (!plots.power || !S.span || t - lastScroll < 33) return;
  lastScroll = t;
  const [min, max] = xRange();
  plots.power.setScale("x", { min, max });
  plots.beats.setScale("x", { min, max });
}
function visible(xs, arr) {                     // min and max of arr's values inside the x range
  const [lo, hi] = xRange();
  let mn = Infinity, mx = -Infinity;
  for (let i = lowerBound(xs, lo); i < xs.length && xs[i] <= hi; i++) {
    const v = arr[i];
    if (v != null) { if (v < mn) mn = v; if (v > mx) mx = v; }
  }
  return mn <= mx ? [mn, mx] : null;
}
const hrLine = () => (S.span === 300 ? G.hr : G.hz);

// Each chart's two series share the full height on their own scales, placed so they don't sit on
// top of each other: the typical level (median of what is visible) of power and alpha 1 at ~38 %
// of the height, of heart rate and beat intervals at ~70 %. At any steady intensity the lines stay
// apart; spikes may cross. Heart rate and intervals are fitted to their 3rd-97th percentile, so a rare
// spike runs off the top instead of squashing the line. Power starts at 0; alpha 1 always shows the
// 0.75 (VT1) line.
function visVals(xs, arr) {
  const [lo, hi] = xRange(), out = [];
  for (let i = lowerBound(xs, lo); i < xs.length && xs[i] <= hi; i++) if (arr[i] != null) out.push(arr[i]);
  return out;
}
function spread(vals, trim = 0) {              // trim: share cut from each end (a rare spike may leave the top)
  if (!vals.length) return null;
  const v = [...vals].sort((a, b) => a - b), n = v.length, k = Math.floor(trim * n);
  return { min: v[k], max: v[n - 1 - k], med: v[n >> 1] };
}
function floating(st, at, minSpan) {            // median at `at` of the height, everything on screen
  const span = Math.max(minSpan, (st.max - st.min) / 0.6);
  let lo = st.med - at * span;
  lo = Math.min(lo, st.min - 0.04 * span);
  lo = Math.max(lo, st.max + 0.04 * span - span);
  return [lo, lo + span];
}
const LOW_AT = 0.38, HIGH_AT = 0.70;
// The scales are round values that stay put: the ideal range (above) is snapped outward to the
// scale's step and then held while the line still fits and sits near its place; only then it moves
// to new round values (e.g. heart rate climbing to 180). A new view starts afresh.
const HELD = {};
function held(key, st, ideal, step, at) {
  const cur = HELD[key];
  if (cur && st.min >= cur[0] && st.max <= cur[1]) {
    const pos = (st.med - cur[0]) / (cur[1] - cur[0]);
    if (Math.abs(pos - at) < 0.2 && cur[1] - cur[0] < 2 * (ideal[1] - ideal[0])) return cur;
  }
  return (HELD[key] = [Math.floor(ideal[0] / step + 1e-9) * step, Math.ceil(ideal[1] / step - 1e-9) * step]);
}
const RANGES = {
  pw: () => {
    const ftp = S.snap?.config.ftp, st = spread(visVals(G.x, G.pa));
    if (!st) return [0, niceUp(ftp ? 1.5 * ftp : 300, 50)];
    const top = S.span === 300 ? (visible(G.x, G.pw) || [0, 0])[1] : st.max;     // the 1 s line too when shown
    const hi = Math.max(top * 1.05, st.med / LOW_AT, ftp ? 0.8 * ftp : 0, 100);
    return held("pw", { ...st, min: 0, max: top }, [0, hi], 50, LOW_AT);
  },
  hr: () => {
    const st = spread(visVals(G.x, hrLine()), 0.03);
    return st ? held("hr", st, floating(st, HIGH_AT, 40), 10, HIGH_AT) : [100, 160];
  },
  rr: () => {
    const st = spread(visVals(B.x, B.rr), 0.03);
    return st ? held("rr", st, floating(st, HIGH_AT, 150), 50, HIGH_AT) : [300, 700];
  },
  a1: () => {
    const st = spread([...visVals(B.x, B.ag), ...visVals(B.x, B.ab)]);
    if (!st) return [0.5, 1.75];
    const lo = Math.min(0.7, st.min - 0.05);                    // VT1 (and VT2 when near it) in view
    const hi = Math.max(lo + (st.med - lo) / LOW_AT, st.max + 0.05, lo + 0.8);
    return held("a1", st, [lo, hi], 0.25, LOW_AT);
  },
};

// time labels: "minutes ago" for the recent past, the time of day for the whole ride
const clockOf = (t, secs) => {
  const d = new Date((S.snap?.t0_ms ?? 0) + t * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getHours()}:${p(d.getMinutes())}` + (secs ? `:${p(d.getSeconds())}` : "");
};
function xSplits(u, _ax, min, max) {
  const out = [];
  if (S.span) {
    const step = S.span <= 300 ? 60 : 300;
    for (let t = max; t >= min - 1e-6; t -= step) out.unshift(t);
    return out;
  }
  const t0s = (S.snap?.t0_ms ?? 0) / 1000, w = u.bbox.width / (devicePixelRatio || 1);
  const step = [300, 600, 900, 1800, 3600].find((st) => (max - min) / st <= w / 90) || 7200;
  for (let c = Math.ceil((t0s + min) / step) * step; c <= t0s + max; c += step) out.push(c - t0s);
  return out;
}
function xValues(u, splits) {
  if (!S.span) return splits.map((t) => clockOf(t));
  const now = u.scales.x.max;
  return splits.map((t) => { const m = Math.round((now - t) / 60); return m ? `−${m} min` : "now"; });
}
function nearest(xs, arr, t, within) {          // the value of arr closest before t (for the readout)
  for (let i = Math.min(lowerBound(xs, t + 1e-6), xs.length) - 1; i >= 0 && t - xs[i] <= within; i--)
    if (arr[i] != null) return arr[i];
  return null;
}

const plots = {}, observed = new Set();
function dither(u) {                            // LCD fill: a 50 % checkerboard of ink pixels
  if (!u._dither) {
    const d = Math.max(1, Math.round(devicePixelRatio || 1)), c = document.createElement("canvas");
    c.width = c.height = 2 * d;
    const x = c.getContext("2d");
    x.fillStyle = "rgba(27,30,25,0.42)";
    x.fillRect(0, 0, d, d); x.fillRect(d, d, d, d);
    u._dither = u.ctx.createPattern(c, "repeat");
  }
  return u._dither;
}
function makeCharts() {
  const lcd = THEME === "lcd";
  const axisFont = "12px Bahnschrift, 'Segoe UI', sans-serif";
  const common = (el) => ({
    width: el.clientWidth, height: el.clientHeight,
    legend: { show: false },
    padding: [24, 8, 0, 0],
    cursor: { sync: { key: "ph" }, drag: { x: false, y: false, setScale: false }, y: false, points: { show: false } },
  });
  const xAxis = (labels) => ({
    stroke: C.dim, font: axisFont, grid: { stroke: C.grid, width: 1 }, ticks: { show: false },
    splits: xSplits, values: labels ? xValues : (u, v) => v.map(() => ""), size: labels ? 30 : 4,
  });
  const yAxis = (scale, side, colour, incrs, grid, format) => ({
    scale, side, stroke: blend(colour, C.panel, 0.2), font: axisFont, size: 46, ticks: { show: false },
    grid: grid ? { stroke: C.grid, width: 1 } : { show: false }, incrs,
    ...(format ? { values: (u, v) => v.map(format) } : {}),
  });
  const smooth = uPlot.paths.spline();
  const hline = (u, scale, v, colour, dash) => {
    const ctx = u.ctx, dpr = devicePixelRatio || 1, y = Math.round(u.valToPos(v, scale, true)) + 0.5;
    if (y < u.bbox.top || y > u.bbox.top + u.bbox.height) return;         // off this scale's range
    ctx.save();
    ctx.strokeStyle = colour; ctx.lineWidth = dpr;
    if (dash) ctx.setLineDash([4 * dpr, 4 * dpr]);
    ctx.beginPath(); ctx.moveTo(u.bbox.left, y); ctx.lineTo(u.bbox.left + u.bbox.width, y); ctx.stroke();
    ctx.restore();
  };
  const drawZ2 = (u) => {                                          // the Z2 band (56-75 % of FTP)
    const ftp = S.snap?.config.ftp;
    if (!ftp) return;
    const y1 = u.valToPos(0.75 * ftp, "pw", true), y0 = u.valToPos(0.56 * ftp, "pw", true);
    u.ctx.fillStyle = C.z2;
    u.ctx.fillRect(u.bbox.left, y1, u.bbox.width, y0 - y1);
  };
  const vt1Line = (u) => {                                         // your VT1 heart rate (the Z2 guard)
    const ref = S.snap?.guard?.ref;
    if (!ref) return;
    if (ref.lo != null) {
      const y1 = u.valToPos(ref.hi, "hr", true), y0 = u.valToPos(ref.lo, "hr", true);
      u.ctx.fillStyle = blend(C.hr, C.panel, 0.9);
      u.ctx.fillRect(u.bbox.left, y1, u.bbox.width, y0 - y1);
    }
    hline(u, "hr", ref.hr, blend(C.hr, C.panel, 0.4), true);
    const dpr = devicePixelRatio || 1;
    u.ctx.save();
    u.ctx.font = `${11 * dpr}px Bahnschrift, 'Segoe UI', sans-serif`;
    u.ctx.fillStyle = blend(C.hr, C.panel, 0.25); u.ctx.textAlign = "left";
    u.ctx.fillText(`VT1 ${Math.round(ref.hr)}`, u.bbox.left + 6 * dpr, u.valToPos(ref.hr, "hr", true) - 4 * dpr);
    u.ctx.restore();
  };
  const thresholds = (u) => {                                      // 0.75 ≈ VT1, 0.5 ≈ VT2
    hline(u, "a1", 0.75, blend(C.good, C.panel, 0.45), true);
    hline(u, "a1", 0.5, blend(C.bad, C.panel, 0.45), true);
  };
  const readout = (u) => {
    const i = u.cursor.idx, out = $("readout");
    if (i == null || u.cursor.left < 0) { out.textContent = ""; return; }
    const t = G.x[i], rr = nearest(B.x, B.rr, t, 3), a1 = nearest(B.x, B.ag, t, 8) ?? nearest(B.x, B.ab, t, 8);
    const hr = hrLine()[i];
    out.innerHTML = `${clockOf(t, true)} &nbsp; <b>${fmt(G.pa[i])}</b> W` +
      (hr != null ? ` &nbsp; <b>${fmt(hr)}</b> bpm` : "") + (rr != null ? ` &nbsp; RR <b>${fmt(rr)}</b> ms` : "") +
      (a1 != null ? ` &nbsp; α1 <b>${a1.toFixed(2)}</b>` : "");
  };
  const elP = $("c-power"), elB = $("c-beats");
  plots.power = new uPlot({
    ...common(elP),
    scales: { x: { time: false, range: xRange }, pw: { range: RANGES.pw }, hr: { range: RANGES.hr } },
    axes: [xAxis(false), yAxis("pw", 3, C.power, [25, 50, 100, 200], true),
      yAxis("hr", 1, C.hr, [5, 10, 20, 25, 50], false)],
    series: [{},
      { scale: "pw", stroke: blend(C.power, C.panel, 0.55), width: lcd ? 1 : 1.25, points: { show: false } },   // 1 s
      { scale: "pw", stroke: C.power, width: 2.25, paths: smooth, points: { show: false },                       // 10 s
        ...(lcd ? { fill: dither } : {}) },                   // LCD: a dithered area, told apart from HR by fill
      { scale: "hr", stroke: C.hr, width: lcd ? 2 : 2.25, paths: smooth, points: { show: false } }],
    hooks: { drawClear: [drawZ2, vt1Line], setCursor: [readout] },
  }, [G.x, G.pw, G.pa, hrLine()], elP);
  plots.beats = new uPlot({
    ...common(elB),
    scales: { x: { time: false, range: xRange }, rr: { range: RANGES.rr }, a1: { range: RANGES.a1 } },
    axes: [xAxis(true), yAxis("rr", 3, C.rr, [10, 25, 50, 100, 200], true),
      yAxis("a1", 1, C.a1, [0.1, 0.25, 0.5, 1], false, (v) => v.toFixed(2))],
    series: [{},
      { scale: "rr", stroke: C.rr, width: lcd ? 1.1 : 1.5, points: { show: false } },
      { scale: "rr", stroke: C.bad, width: 0, paths: () => null,                                           // artifacts
        points: lcd ? { show: true, size: 6, width: 1.25, fill: C.panel, stroke: C.fg }
                    : { show: true, size: 5, width: 0, fill: C.bad, stroke: C.bad } },
      { scale: "a1", stroke: C.a1, width: lcd ? 3 : 2.25, points: { show: false } },                        // LCD: thick α1, thin RR
      { scale: "a1", stroke: C.faint, width: lcd ? 3 : 2.25, points: { show: false } }],
    hooks: { drawClear: [thresholds] },
  }, [B.x, B.rr, B.ra, B.ag, B.ab], elB);
  for (const [k, el] of [["power", elP], ["beats", elB]]) {
    if (observed.has(k)) continue;
    observed.add(k);
    new ResizeObserver(() => plots[k]?.setSize({ width: el.clientWidth, height: el.clientHeight })).observe(el);
  }
}
function setTheme(name, save = true) {
  THEME = name;
  if (save) savePref("theme", name);
  document.documentElement.dataset.theme = name;
  Object.assign(C, PALETTES[name]);
  for (const k of ["power", "beats"]) if (plots[k]) { plots[k].destroy(); delete plots[k]; }   // rebuilt in the new ink
  for (const b of $("themes").querySelectorAll("button")) b.classList.toggle("on", b.dataset.theme === name);
  if (S.snap?.screen === "ride" && S.snap.ride) { renderTiles(S.snap); renderGraph(); }
  if (typeof redrawSummary === "function") redrawSummary();
  if (typeof redrawProgress === "function") redrawProgress();
}
for (const b of $("themes").querySelectorAll("button")) {
  b.classList.toggle("on", b.dataset.theme === THEME);
  b.addEventListener("click", () => { setTheme(b.dataset.theme); b.blur(); });
}
function renderGraph() {
  const has = G.x.length > 0 || B.x.length > 0;
  $("chart-empty").hidden = has;
  if (!has) return;
  if (!plots.power) makeCharts();
  const fine = S.span === 300;                      // the 1 s line only in the 5 min view: noise further out
  if (plots.power.series[1].show !== fine) plots.power.setSeries(1, { show: fine });
  $("key-1s").hidden = !fine;
  plots.power.setData([G.x, G.pw, G.pa, hrLine()]);
  plots.beats.setData([B.x, B.rr, B.ra, B.ag, B.ab]);
}
for (const b of $("views").querySelectorAll("button")) {
  b.classList.toggle("on", +b.dataset.span === S.span);
  b.addEventListener("click", () => {
    S.span = +b.dataset.span; savePref("span", S.span);
    for (const k in HELD) delete HELD[k];                        // a new view gets its own scales
    for (const o of $("views").querySelectorAll("button")) o.classList.toggle("on", o === b);
    renderGraph();
    b.blur();
  });
}

// --------------------------------------------------------------------------- bottom bar
function renderBar(s) {
  const box = $("sensors");
  box.hidden = s.screen !== "ride" && !s.error;     // the pages have the Connections pane on Home
  box.replaceChildren(...s.sensors.map(({ label, status, link }) => {
    const ok = status.startsWith("connected");
    const el = document.createElement("span");
    el.className = "sensor" + (ok ? " ok" : "");
    el.innerHTML = `<i class="dot"></i><b></b><span></span>${linkTag(link)}`;
    el.querySelector("b").textContent = label;
    el.querySelector("span").textContent = ok && status.includes(": ") ? status.split(": ")[1] : status;
    return el;
  }));
  if (s.battery) {
    const el = document.createElement("span");
    el.className = "sensor";
    el.textContent = `Battery L ${s.battery[0]} % · R ${s.battery[1]} %`;
    box.append(el);
  }
  if (s.error) {
    const el = document.createElement("span");
    el.className = "sensor err";
    el.textContent = s.error;
    box.append(el);
  }
  const r = s.ride, rec = $("rec"), main = $("b-main"), stop = $("b-stop");
  rec.hidden = !r;
  if (!r) { main.hidden = stop.hidden = true; return; }             // the ride buttons are for the ride screen
  rec.className = "rec " + r.state;
  if (r.state === "ready") rec.textContent = "start pedalling or press Space";
  else {
    const tag = { recording: "Rec", paused: "Paused", finished: "Saved" }[r.state];
    rec.innerHTML = `<span class="tag">${tag}</span><span class="clk">${r.clock}</span>`;
  }
  const btn = { ready: ["Start", "primary", "start"], recording: ["Pause", "", "pause"], paused: ["Resume", "go", "resume"] }[r.state];
  main.hidden = !btn;
  if (btn) {
    main.firstChild.textContent = btn[0];
    main.className = "btn " + btn[1];
    main.dataset.action = btn[2];
  }
  stop.hidden = !(r.state === "recording" || r.state === "paused");
}
$("b-main").addEventListener("click", (e) => { act(e.currentTarget.dataset.action); e.currentTarget.blur(); });
$("b-stop").addEventListener("click", (e) => { act("stop"); e.currentTarget.blur(); });

// --------------------------------------------------------------------------- dialogs (the ride's end)
function dialogText(s) {
  const clock = s.ride?.clock ?? "";
  return {
    delete: ["Delete this ride?", "Its FIT file, logs and summary will be deleted. This can't be undone.",
             [["Delete", "danger", "delete_confirm"], ["Back", "", "back"]]],
    finish: ["Finish ride?", `${clock} recorded. The ride is paused while you decide.`,
             [["Discard", "danger-text", "discard"], ["Resume", "", "resume"], ["Save ride", "primary", "save"]]],
    discard: ["Discard this ride?", `The ${clock} recording and its files will be deleted. This can't be undone.`,
              [["Discard", "danger", "discard_confirm"], ["Back", "", "back"]]],
  };
}
function renderDialog(s) {
  const kind = s.dialog, box = $("dialog");
  if (kind === S.dialogKey) {                                     // same dialog: only the clock moves
    if (kind === "finish" || kind === "discard") $("d-sub").textContent = dialogText(s)[kind][1];
    return;
  }
  S.dialogKey = kind;
  box.hidden = !kind;
  if (!kind) return;
  const d = dialogText(s)[kind];
  $("d-title").textContent = d[0];
  $("d-sub").textContent = d[1];
  $("d-buttons").replaceChildren(...d[2].map(([text, cls, action]) => {
    const b = document.createElement("button");
    b.className = "btn " + cls;
    b.textContent = text;
    b.addEventListener("click", () => act(action));
    return b;
  }));
  $("d-buttons").lastChild.focus();                                // Enter presses the rightmost
}

// --------------------------------------------------------------------------- talking to Python
async function act(name, arg) {
  try {
    const r = await fetch("/api/action", { method: "POST", headers: { "Content-Type": "application/json" },
                                           body: JSON.stringify({ name, arg }) });
    apply(await r.json());
  } catch { /* the next poll shows the state */ }
}
// Theme, graph span and the power tile's mode live in the app's settings.json (the page's own
// storage is lost: the app's port changes every run). The first snapshot brings them; after that
// the page is in charge and sends every change.
function savePref(key, value) {
  store.set(key, value);
  act("prefs", { [key]: value });
}
function syncPrefs(p) {
  if (S.prefsSynced || !p) return;
  S.prefsSynced = true;
  if (p.theme && p.theme !== THEME && PALETTES[p.theme]) setTheme(p.theme, false);
  if (p.span != null && p.span !== S.span && VIEWS.includes(p.span)) {
    S.span = p.span;
    for (const o of $("views").querySelectorAll("button")) o.classList.toggle("on", +o.dataset.span === S.span);
  }
  if (typeof p.power3s === "boolean" && p.power3s !== S.power3s) { S.power3s = p.power3s; showPowerMode(); }
}

// one screen at a time: home, ride or summary (the Python side decides)
function showScreen(name) {
  for (const n of ["home", "plan", "workouts", "stats", "ride", "summary"]) $("scr-" + n).hidden = n !== name;
  // the top bar on the pages; a ride (and a ride's summary) has the whole window
  const page = ["home", "plan", "workouts", "stats"].includes(name);
  $("topnav").hidden = !page && name !== "summary";
  for (const b of $("topnav").querySelectorAll("button[data-page]"))
    b.classList.toggle("on", b.dataset.page === (name === "summary" ? S.snap?.back_to : name));
}
for (const b of $("topnav").querySelectorAll("button[data-page]"))
  b.addEventListener("click", () => { act("nav", b.dataset.page); b.blur(); });
function apply(s) {
  if (!s || !s.screen) return;
  if (s.pn != null) {
    if (s.pn < S.p || s.hn < S.h || s.an < S.a || s.bn < S.b) resetGraph();   // a new session behind the page
    for (const [t, w] of s.power) addPower(t, w);
    for (const [t, b] of s.hr) addHr(t, b);
    for (const [t, rr, art] of s.beats_rr) addBeat(t, rr, art);
    for (const [t, v, art] of s.a1) addA1(t, v, art);
    S.p += s.power.length; S.h += s.hr.length; S.a += s.a1.length; S.b += s.beats_rr.length;
  }
  if (s.now != null) {                                             // smoothed server clock for the animations
    const cand = s.now - performance.now() / 1000;
    S.offset = S.offset == null || Math.abs(cand - S.offset) > 1 ? cand : S.offset + (cand - S.offset) * 0.1;
  }
  syncPrefs(s.settings);
  S.prevScreen = S.snap?.screen;
  S.snap = s;
  showScreen(s.screen);
  renderBar(s);
  renderDialog(s);
  if (!$("topnav").hidden) renderNavStart(s);
  if (s.screen === "ride" && s.ride) {
    renderTiles(s);
    renderWorkout(s);
    renderDyn(s);
    $("z2-key").hidden = !s.config.ftp;
    if (s.pn != null) renderGraph();
  } else if (s.screen === "home") {
    renderHome(s);
  } else if (s.screen === "summary") {
    showSummary(s.summary_id);
    $("sum-home").textContent = { stats: "Statistics", workouts: "Workouts", plan: "Plan" }[s.back_to] || "Home";
  } else if (s.screen === "stats") {
    showStats(s);
  } else if (s.screen === "workouts") {
    renderWorkoutsPage(s);
  } else if (s.screen === "plan") {
    showPlan(s);
  }
  if (s.closed) { $("offline").textContent = "Recorder closed. You can close this tab."; $("offline").hidden = false; }
}
async function poll() {
  try {
    const r = await fetch(`/api/state?p=${S.p}&h=${S.h}&a=${S.a}&b=${S.b}`);
    apply(await r.json());
    S.fails = 0;
    if (!S.snap.closed) $("offline").hidden = true;
  } catch {
    if (++S.fails >= 3 && !S.snap?.closed) $("offline").hidden = false;
  }
  if (!S.snap?.closed) setTimeout(poll, POLL_MS);
}

function frame() {
  stepTweens();
  scrollCharts();
  drawBeats();
  requestAnimationFrame(frame);
}

// --------------------------------------------------------------------------- keys
document.addEventListener("keydown", (e) => {
  if (e.target.closest?.("input, textarea")) return;               // typing in the settings
  if (e.code === "Space") {
    e.preventDefault();
    if (["home", "workouts"].includes(S.snap?.screen)) act("start");
    else if (S.snap?.screen === "ride" && !S.snap?.dialog) act("toggle");
  } else if (e.key === "Escape" && S.snap?.screen === "summary") {
    act("home");
  } else if (e.key === "F11") {
    e.preventDefault();
    if (S.snap?.app) act("fullscreen");
    else if (document.fullscreenElement) document.exitFullscreen();
    else document.documentElement.requestFullscreen?.().catch(() => {});
  }
});

poll();
requestAnimationFrame(frame);
