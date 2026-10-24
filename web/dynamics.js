// Cycling Dynamics panel, laid out like Garmin's screen: two rings per leg on dark tracks (the whole
// revolution), the inner one the power phase, the outer (yellow) one the peak power phase, whose angles
// are written inside over a yellow line with the leg's watts and
// the peak phase angles inside, torque effectiveness and pedal smoothness, and the platform centre offset on a drawing of each pedal
// (red = now, blue = the ride's middle 80 %, dotted = centre). Angles are degrees clockwise from
// top dead centre. Uses C, $ and fmt from app.js.
"use strict";

const RING = { r: 45, rPeak: 53 };             // inner ring = power phase, outer ring = peak
const PP_KEYS = ["start", "peak_start", "peak_end", "end"];

function polar(a, r) {
  const rad = (a * Math.PI) / 180;
  return [r * Math.sin(rad), -r * Math.cos(rad)];
}
function arcPath(a0, a1, r) {
  const ext = (((a1 - a0) % 360) + 360) % 360;
  if (ext < 0.5) return "";
  if (ext > 359.5) return `M0 ${-r}A${r} ${r} 0 1 1 0 ${r}A${r} ${r} 0 1 1 0 ${-r}`;
  const [x0, y0] = polar(a0, r), [x1, y1] = polar(a0 + ext, r);
  return `M${x0.toFixed(2)} ${y0.toFixed(2)}A${r} ${r} 0 ${ext > 180 ? 1 : 0} 1 ${x1.toFixed(2)} ${y1.toFixed(2)}`;
}

const DYN = { built: false, pedals: false, shown: {} };

function buildDyn() {
  for (const leg of document.querySelectorAll(".leg")) {
    const side = leg.dataset.side, letter = side === "left" ? "L" : "R";
    leg.querySelector(".ring").innerHTML =
      `<circle class="track" r="${RING.r}"/><circle class="track" r="${RING.rPeak}"/>` +
      Array.from({ length: 36 }, (_, k) => k * 10).map((a) => {  // a dial inside: every 10°, longer every 30°
        const kind = a % 90 === 0 ? "main" : a % 30 === 0 ? "mid" : "";   // and 90° (TDC, 3, 6, 9 o'clock)
        const r0 = RING.r - 5, [x0, y0] = polar(a, r0), [x1, y1] = polar(a, r0 - ({ main: 5, mid: 3.2 }[kind] || 1.8));
        return `<line class="tick ${kind}" x1="${x0.toFixed(2)}" y1="${y0.toFixed(2)}" x2="${x1.toFixed(2)}" y2="${y1.toFixed(2)}"/>`;
      }).join("") +
      `<path class="phase"/><path class="peak"/>` +
      `<text class="w" y="5">--<tspan class="wu" dx="2">W</tspan></text><text class="pk" y="23"></text>` +
      `<line class="pk-line" y1="28" y2="28"/>` +
      `<text class="side" x="${side === "left" ? -60 : 60}" y="58">${letter}</text>`;
  }
  DYN.built = true;
  fetch("/api/pedal").then((r) => r.json()).then(drawPedals).catch(() => {});
}

function drawPedals(shape) {
  const path = (polys, inb) => polys.map((p) => "M" + p.map(([x, y]) => `${(inb * x).toFixed(4)} ${y}`).join("L") + "Z").join("");
  for (const box of document.querySelectorAll(".pco")) {
    const inb = box.dataset.side === "left" ? 1 : -1;      // the crank is on the right of the left pedal
    box.querySelector(".pedal").innerHTML =
      `<path class="sil" d="${path(shape.solid, inb)}"/><path class="hole" d="${path(shape.holes, inb)}"/>` +
      `<line class="range r0" y1="-0.05" y2="1.07"/><line class="range r1" y1="-0.05" y2="1.07"/>` +
      `<line class="now" y1="-0.05" y2="1.07"/><line class="centre" x1="0" x2="0" y1="-0.1" y2="1.07"/>` +
      `<text x="${-inb * 0.47}" y="0.02">+</text><text x="${inb * 0.44}" y="0.02">−</text>`;
  }
  DYN.pedals = true;
  DYN.shown = {};                                              // redraw the lines
}

// positive PCO = outboard (away from the crank); the platform is about 62 mm wide
const pcoX = (v, inb) => -inb * Math.max(-25, Math.min(25, v)) * (0.72 / 62);

function setText(el, text) { if (el.textContent !== text) el.textContent = text; }

// the one-line dynamics of a workout's layout (layoutKind): per leg W, power phase, peak, TE, PS, PCO
function renderDynRow(s) {
  const row = $("dyn-row");
  if (row.hidden) return;
  const d = s.dyn || {}, st = s.stats || {};
  const n = (v, unit = "", plus = false) => v == null ? "--" : `${plus && v > 0 ? "+" : ""}${fmt(v)}${unit}`;
  const leg = (side, tag) => {
    const a = d[`pp_${side}_start_deg`], b = d[`pp_${side}_end_deg`];
    return `<span class="side"><span>${tag}</span><span><b>${n(d[`power_${side}_w`])}</b> W</span>` +
      `<span>phase <b>${a == null || b == null ? "--" : `${Math.round(a)}–${Math.round(b)}°`}</b></span>` +
      `<span>TE <b>${n(d[`te_${side}_pct`], "%")}</b></span><span>PS <b>${n(d[`ps_${side}_pct`], "%")}</b></span>` +
      `<span>PCO <b>${n(d[`pco_${side}_mm`], "", true)}</b> mm${st[`pco_${side}`] == null ? "" : ` (avg ${st[`pco_${side}`] > 0 ? "+" : ""}${st[`pco_${side}`].toFixed(1)})`}</span></span>`;
  };
  const pos = d.rider_position ? `<span class="sep"></span><span>${d.rider_position.replace("to_", "→ ")}</span>` : "";
  const html = leg("left", "L") + '<span class="sep"></span>' + leg("right", "R") + pos;
  if (row.innerHTML !== html) row.innerHTML = html;
}

function renderDyn(s) {
  renderDynRow(s);
  if (!DYN.built) buildDyn();
  const d = s.dyn || {}, st = s.stats || {};
  const pos = d.rider_position, badge = $("pos");
  badge.hidden = !pos;
  if (pos) {
    setText(badge, pos.replace("to_", "→ "));
    badge.style.color = pos === "seated" ? C.good : C.warn;
  }
  for (const leg of document.querySelectorAll(".leg")) {
    const side = leg.dataset.side;
    const pp = PP_KEYS.map((k) => d[`pp_${side}_${k}_deg`]);
    const has = pp[0] != null && pp[3] != null;
    const key = has ? pp.map(Math.round).join() : "";
    if (DYN.shown[side] !== key) {                            // only touch the arcs when they change
      DYN.shown[side] = key;
      const [start, pkS, pkE, end] = pp;
      const q = (c) => leg.querySelector(c);
      q(".phase").setAttribute("d", has ? arcPath(start, end, RING.r) : "");
      q(".peak").setAttribute("d", has && pkS != null ? arcPath(pkS, pkE, RING.rPeak) : "");
    }
    const w = leg.querySelector(".w").firstChild;                 // the number before the "W"
    if (w.nodeValue !== fmt(d[`power_${side}_w`])) w.nodeValue = fmt(d[`power_${side}_w`]);
    const pk = leg.querySelector(".pk"), pkText = has && pp[1] != null ? `${Math.round(pp[1])}–${Math.round(pp[2])}°` : "";
    if (pk.textContent !== pkText) {                              // the yellow line under it follows its width
      pk.textContent = pkText;
      const half = pkText ? pk.getComputedTextLength() / 2 : 0, ln = leg.querySelector(".pk-line");
      ln.setAttribute("x1", -half); ln.setAttribute("x2", half);
    }
    setText(leg.querySelector(".te"), d[`te_${side}_pct`] == null ? "--" : `${fmt(d[`te_${side}_pct`])}%`);
    setText(leg.querySelector(".ps"), d[`ps_${side}_pct`] == null ? "--" : `${fmt(d[`ps_${side}_pct`])}%`);
  }
  for (const box of document.querySelectorAll(".pco")) {
    const side = box.dataset.side, inb = side === "left" ? 1 : -1;
    const pco = d[`pco_${side}_mm`], rng = st[`pco_${side}_range`], avg = st[`pco_${side}`];
    setText(box.querySelector(".num"), pco == null ? "--" : pco > 0 ? `+${pco}` : String(pco));
    setText(box.querySelector(".sub"), avg == null ? "" : `avg ${avg > 0 ? "+" : ""}${avg.toFixed(1)}`);
    if (!DYN.pedals) continue;
    const key = `${pco}|${rng}`;
    if (DYN.shown["pco_" + side] === key) continue;
    DYN.shown["pco_" + side] = key;
    const line = (cls, v) => {
      const el = box.querySelector(cls);
      el.style.display = v == null ? "none" : "";
      if (v != null) { const x = pcoX(v, inb); el.setAttribute("x1", x); el.setAttribute("x2", x); }
    };
    line(".r0", rng ? rng[0] : null);
    line(".r1", rng ? rng[1] : null);
    line(".now", pco);
  }
}
