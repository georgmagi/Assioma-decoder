"""
Live window for the Assioma recorder (tkinter, everything drawn on canvases).

    top strip   power · cadence · balance · heart rate · DFA alpha 1 · time
    left        Cycling Dynamics, laid out like Garmin's screen:
                power-phase rings, torque effectiveness / smoothness,
                platform centre offset on a pedal drawing, power-phase angles
    right       two live graphs sharing a time axis:
                power + heart rate, and beat-to-beat (RR) intervals + DFA alpha 1

Click a graph to switch between the last 10 min, 30 min and the whole ride.
F11 toggles full screen.
"""
from __future__ import annotations

import math
import os
import queue
from bisect import bisect_left
from collections import deque
import sys
import time
from datetime import timedelta

import pedal_outline

BG, PANEL, BORDER, GRID = "#0b0d10", "#12151a", "#1d2128", "#1b1f25"
FG, DIM, FAINT = "#e6e8eb", "#7c8490", "#4a515a"
RING, PHASE, PEAK, NOW_LINE, PCO_RANGE = "#262b33", "#6cc4ff", "#1f7bff", "#ff5252", "#4a8df0"
BORDER_TOP, BORDER_BOTTOM = "#2a3039", "#15181d"      # panel edges: light from above
ZONES = [(0.55, "Z1", "#9aa3ad"), (0.75, "Z2", "#5b9dff"), (0.90, "Z3", "#3ddc97"),
         (1.05, "Z4", "#ffd166"), (1.20, "Z5", "#ff9f43"), (99, "Z6", "#ff5c5c")]   # Coggan, share of FTP
CADENCE_BAND = (80, 100)             # rpm; the number dims outside it while pedalling
POWER, Z2_FILL, HR, HR_DIM = "#5b9dff", "#132019", "#ff4d6d", "#3a1b24"
RR, A1, GOOD, WARN, BAD = "#2dd4bf", "#c08cff", "#3ddc97", "#ffb020", "#ff5c5c"
PEDAL = {"body": "#3b4048", "deep": "#31363d", "plate": "#c3c8ce", "red": "#b3413a",
         "chrome": "#a3a9b0", "chrome_dark": "#62686f", "panel": PANEL}
PEDAL_SIL = "#2c3139"                 # the pedal silhouette: quiet, so the PCO lines stand out
PEDAL_UNDER = {"body": "panel", "deep": "body", "plate": "body", "red": "body", "chrome": "panel",
               "chrome_dark": "chrome"}


def fmt(v, digits: int = 0, dash: str = "--") -> str:
    if v is None:
        return dash
    return f"{v:.{digits}f}" if digits else f"{round(v)}"


def track(text: str) -> str:
    """Letter-spaced caps for labels (hair spaces; Tk has no letter spacing). "α" and the
    metric name "Pw:HR" keep their case."""
    if "Pw:HR" in text:
        return " ".join(text)
    caps = "".join(ch if ch == "α" else ch.upper() for ch in text)
    return " ".join(caps).replace("   ", " ")


def alpha_colour(a, art) -> str:
    if a is None:
        return DIM
    if art is not None and art > 5:
        return FAINT                    # too many artifacts to trust
    return GOOD if a >= 0.75 else WARN if a >= 0.5 else BAD


def blend(c1: str, c2: str, k: float) -> str:
    """Mix colour c1 towards c2 by k (0..1)."""
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * k):02x}" for x, y in zip(a, b))


class Fonts:
    def __init__(self, families) -> None:
        fams = set(families)
        pick = lambda *names: next((n for n in names if n in fams), "TkDefaultFont")
        self._num = pick("Bahnschrift", "Segoe UI", "Helvetica")
        self._num_b = pick("Bahnschrift SemiBold", "Bahnschrift", "Segoe UI Semibold", "Helvetica")
        self._ui = pick("Segoe UI", "Helvetica")
        self._ui_b = pick("Segoe UI Semibold", "Segoe UI", "Helvetica")
        self._cache: dict = {}
        self._widths: dict = {}

    def num(self, px, bold: bool = False):
        return (self._num_b if bold else self._num, -max(7, int(px)))

    def ui(self, px, bold: bool = False):
        return (self._ui_b if bold else self._ui, -max(7, int(px)))

    def _font(self, spec):
        f = self._cache.get(spec)
        if f is None:
            import tkinter.font as tkfont
            f = self._cache[spec] = tkfont.Font(font=spec)
        return f

    def width(self, spec, text: str) -> int:
        k = (spec, text)
        w = self._widths.get(k)
        if w is None:
            if len(self._widths) > 4000:
                self._widths.clear()
            w = self._widths[k] = self._font(spec).measure(text)
        return w

    def height(self, spec) -> int:
        return self._font(spec).metrics("linespace")


def rrect(c, x0, y0, x1, y1, r, **kw):
    r = min(r, (x1 - x0) / 2, (y1 - y0) / 2)
    pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1,
           x1 - r, y1, x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
    return c.create_polygon(pts, smooth=True, **kw)


def panel(c, W, H, r) -> None:
    """The panel card as the lowest item of a canvas that is exactly the panel's size."""
    img = _panel_image(W, H, r)
    if img is None:
        rrect(c, 0.5, 0.5, W - 0.5, H - 0.5, r, fill=PANEL, outline=BORDER, width=1, tags="bg")
    else:
        c.create_image(0, 0, image=_photo(("panel", W, H, round(r, 1)), img), anchor="nw", tags="bg")


# --- anti-aliased images (Pillow), shared by panels, rings, pedals ---
_IMAGES: dict = {}                     # key -> PIL image
_PHOTOS = None                         # key -> Tk photo, an OrderedDict used as an LRU


def _photo(key, img):
    """A Tk photo for a PIL image, cached by key (least recently used dropped beyond 400)."""
    global _PHOTOS
    if _PHOTOS is None:
        from collections import OrderedDict
        _PHOTOS = OrderedDict()
    ph = _PHOTOS.get(key)
    if ph is None:
        from PIL import ImageTk
        ph = _PHOTOS[key] = ImageTk.PhotoImage(img)
        while len(_PHOTOS) > 400:
            _PHOTOS.popitem(last=False)
    else:
        _PHOTOS.move_to_end(key)
    return ph


def _panel_image(w: int, h: int, r: float):
    """A card on the window background: anti-aliased rounded corners, a border that is lighter at
    the top than at the bottom, and a faint highlight along the top edge. None without Pillow."""
    w, h = int(w), int(h)
    key = ("panel", w, h, round(r, 1))
    img = _IMAGES.get(key)
    if img is None:
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            return None
        ss = 3
        W_, H_, R_ = w * ss, h * ss, r * ss
        img = Image.new("RGB", (W_, H_), _rgb(BG))
        grad = Image.new("RGB", (1, H_))
        grad.putdata([_rgb(blend(BORDER_TOP, BORDER_BOTTOM, y / max(1, H_ - 1))) for y in range(H_)])
        mask = Image.new("L", (W_, H_), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, W_ - 1, H_ - 1], radius=R_, fill=255)
        img.paste(grad.resize((W_, H_)), (0, 0), mask)
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([ss, ss, W_ - 1 - ss, H_ - 1 - ss], radius=max(0, R_ - ss), fill=_rgb(PANEL))
        d.line([(R_, 2 * ss), (W_ - R_, 2 * ss)], fill=_rgb(blend(PANEL, FG, 0.07)), width=ss)
        img = _IMAGES[key] = img.reduce(ss)
    return img


class Retained:
    """Draw every frame with the usual create_* calls, but only touch the Tk items whose
    coordinates or options changed. Tk paints on the CPU and repaints whatever region changes,
    so deleting and recreating a whole panel (big numbers, pedal drawings) for one new value
    was what made the window heavy; now only the changed text or line is repainted."""

    def __init__(self, canvas, tag: str) -> None:
        self.c, self.tag = canvas, tag
        self.items: list = []           # (kind, coords, options, item id)
        self.ops: list = []

    def reset(self) -> None:
        """The canvas was cleared by someone else."""
        self.items = []

    def begin(self) -> None:
        self.ops = []

    def __getattr__(self, name):
        if not name.startswith("create_"):
            raise AttributeError(name)
        kind = name[7:]

        def record(*args, **kw):
            if len(args) == 1 and isinstance(args[0], (list, tuple)):
                args = args[0]
            self.ops.append((kind, tuple(args), kw))
        return record

    def end(self) -> bool:
        """Apply the frame; True if the items had to be rebuilt (new ones are on top)."""
        c = self.c
        if [o[0] for o in self.ops] != [i[0] for i in self.items]:      # different shapes: rebuild
            for item in self.items:
                c.delete(item[3])
            self.items = [(k, a, kw, getattr(c, "create_" + k)(*a, tags=self.tag, **kw)) for k, a, kw in self.ops]
            return True
        for n, ((k, a, kw), (_, a0, kw0, iid)) in enumerate(zip(self.ops, self.items)):
            if a != a0:
                c.coords(iid, *a)
            if kw != kw0:
                c.itemconfigure(iid, **kw)
            self.items[n] = (k, a, kw, iid)
        return False


def _retained(c, tag: str) -> Retained:
    rets = c.__dict__.setdefault("_rets", {})
    if tag not in rets:
        rets[tag] = Retained(c, tag)
    return rets[tag]


def text_width(c, text, font) -> float:
    t = c.create_text(0, -1000, text=text, font=font)
    b = c.bbox(t)
    c.delete(t)
    return b[2] - b[0]


def nice_step(span: float, target: int) -> float:
    raw = span / max(1, target)
    for s in (1, 2, 2.5, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000):
        if s >= raw:
            return s
    return 1000


# --------------------------------------------------------------------------- #
# Top strip
# --------------------------------------------------------------------------- #
STRIP_SPAN = 4.0          # seconds of heartbeats across the heart-rate tile
STRIP_FPS = 60


class BeatStrip:
    """Heartbeats as the living background of the heart-rate tile: one faint full-height line
    per beat, sliding in at the right edge and drifting left over ~4 s, the newest one brighter;
    a small dot in the corner pulses as each beat comes in. Amber when the strap has gone quiet
    or sends no beats: check the skin contact.

    The right edge shows "now - DELAY": a strap may report a beat up to ~0.5 s late, so with
    the edge that far back every beat enters exactly at the edge instead of popping up inside.

    Smoothness: the speed is a whole number of pixels per frame, positions are snapped to whole
    pixels and derived from the clock (a late frame catches up instead of drifting), and a frame
    is a single Tk `move`. The lines are rebuilt only when a beat enters or their fade needs
    refreshing (4 Hz). They sit just above the tile's background, under its text."""

    DELAY = 0.55
    GLOW = 0.6                              # s a new line takes to settle to its normal brightness

    def _colour(self, age: float, artifact: bool) -> str:
        """Faint, fading slowly towards the left; a new line starts bright and settles."""
        fresh = max(0.0, 1 - age / self.GLOW) ** 2
        k = 0.8 + 0.14 * min(1.0, max(0.0, age / STRIP_SPAN)) - 0.45 * fresh
        return blend(WARN if artifact else HR, PANEL, k)

    def __init__(self, canvas) -> None:
        self.c = canvas
        self.key = None
        self.built = 0.0
        self.shift = 0                      # whole pixels moved since the last rebuild
        self.speed = 0.0
        self.edge = None                    # time of the newest beat already on screen
        self.pulse_at = -1.0
        self.pulsing = True
        self.glowing: list = []             # (item, beat time, artifact) of lines still fading in

    def frame(self, geom, beats, now_dt, warn: bool) -> None:
        """geom from draw_top (tile rect, corner radius, dot position, id of the tile's background);
        beats = the HrvTracker's beat list; now_dt = the current (UTC) time."""
        c = self.c
        if not geom:
            c.delete("strip")
            self.key = None
            return
        x0, y0, x1, y1 = geom["rect"]
        rad = geom["rad"]
        xa, xb = x0 + rad, x1 - rad                 # stay clear of the rounded corners
        now = time.monotonic()
        # has a beat reached the right edge since the last rebuild? (only the newest few can have)
        entering = None
        for bt in reversed(beats[-4:]):
            if self.edge is not None and bt.time <= self.edge:
                break
            if (now_dt - bt.time).total_seconds() >= self.DELAY:
                entering = bt.time if entering is None else entering
        key = (geom["rect"], geom["panel"], warn)
        if key != self.key or entering or now - self.built >= 0.25:
            if entering:
                self.edge, self.pulse_at = entering, now
            self.key, self.built, self.shift = key, now, 0
            self.speed = max(1, round((xb - xa) / STRIP_SPAN / STRIP_FPS)) * STRIP_FPS
            c.delete("strip")
            self.glowing = []
            tw = max(1.0, (y1 - y0) * 0.012)
            for bt in reversed(beats[-40:]):
                age = (now_dt - bt.time).total_seconds() - self.DELAY
                x = round(xb - age * self.speed)
                if x < xa:
                    break
                if x <= xb:
                    item = c.create_rectangle(x - tw, y0 + 1.5, x + tw, y1 - 1.5, outline="",
                                              fill=self._colour(age, bt.artifact), tags=("strip", "tick"))
                    if age < self.GLOW:
                        self.glowing.append((item, bt.time, bt.artifact))
            c.tag_raise("strip", geom["panel"])      # above the tile's background, under its text
            self.pulsing = True
        else:
            shift = round((now - self.built) * self.speed)
            if shift != self.shift:
                c.move("tick", self.shift - shift, 0)
                self.shift = shift
            # the newest line's glow fades every frame (a rebuild only happens 4 times a second)
            for item, t, bad in self.glowing:
                c.itemconfigure(item, fill=self._colour((now_dt - t).total_seconds() - self.DELAY, bad))
        pulse = max(0.0, 1 - (now - self.pulse_at) / 0.35)
        if pulse > 0 or self.pulsing:
            c.delete("dot")
            (dx, dy), r = geom["dot"], geom["dot_r"]
            if pulse > 0 and not warn:
                k = 1 - pulse                      # expanding, fading ring
                rr = r * (1.3 + 1.6 * k)
                c.create_oval(dx - rr, dy - rr, dx + rr, dy + rr, outline=blend(HR, PANEL, 0.35 + 0.65 * k),
                              width=max(1, r * 0.25), tags=("strip", "dot"))
            fill = WARN if warn else blend(HR_DIM, HR, pulse ** 0.5)
            c.create_oval(dx - r, dy - r, dx + r, dy + r, fill=fill, outline="", tags=("strip", "dot"))
            self.pulsing = pulse > 0


def _balance(R, F, x, w, H, pad, value, sub) -> None:
    """Left and right leg as two numbers set symmetrically about a thin divider (left leg on
    the left), with their ride averages underneath in the same alignment."""
    left, right = value
    gap = H * 0.09                            # between each number and the divider
    size = min(H * 0.4, (w / 2 - pad - gap) / (0.55 * 2))
    vf, ym, mid = F.num(size), H * 0.535, x + w / 2
    R.create_line(mid, H * 0.34, mid, H - pad + H * 0.01, fill=GRID, width=1)
    R.create_text(mid - gap, ym, anchor="e", text=left, fill=FG, font=vf)
    R.create_text(mid + gap, ym, anchor="w", text=right, fill=FG, font=vf)
    if sub:
        sf, ys = F.ui(H * 0.085), H - pad + H * 0.015
        R.create_text(mid - gap, ys, anchor="se", text=sub[0], fill=DIM, font=sf)
        R.create_text(mid + gap, ys, anchor="sw", text=sub[1], fill=DIM, font=sf)


class Tween:
    """Numbers roll to a new value (ease-out over 180 ms) instead of jumping."""
    DUR = 0.18

    def __init__(self) -> None:
        self.st: dict = {}                  # name -> (from, to, start time)

    def _at(self, st, now):
        a, b, t0 = st
        k = min(1.0, (now - t0) / self.DUR)
        return a + (b - a) * (1 - (1 - k) ** 3)

    def value(self, name: str, target):
        if target is None:
            self.st.pop(name, None)
            return None
        now = time.monotonic()
        st = self.st.get(name)
        if st is None:
            self.st[name] = (target, target, now)
            return target
        if target != st[1]:
            self.st[name] = st = (self._at(st, now), target, now)
        return self._at(st, now)

    def active(self) -> bool:
        now = time.monotonic()
        return any(a != b and now - t0 < self.DUR for a, b, t0 in self.st.values())


def power_zone(watts, ftp):
    """(name, colour) of the Coggan zone for a power, or None without an FTP."""
    if not ftp or watts is None:
        return None
    for top, name, colour in ZONES:
        if watts <= top * ftp:
            return name, colour
    return None


def draw_top(c, F, v, st, h, secs, ride, strap=None, ftp=None):
    """strap: None without a heart-rate strap, else {"warn": text or ""}.
    Returns where the beat strip goes (drawn separately by draw_strip).
    Redraws only when something shown has changed (Tk paints on the CPU)."""
    W, H = c.winfo_width(), c.winfo_height()
    strip = None
    if W < 50 or H < 30:
        return strip
    bal = v.get("balance_right_pct")
    a1 = h.get("alpha1")
    zone = power_zone(v.get("power_target", v.get("power_w")), ftp)
    cad = v.get("cadence_rpm")
    tiles = [  # label, value, unit, sub, colour, width weight
        ("Power", fmt(v.get("power_w")), "W", f"avg {fmt(st['avg_power'])}   NP {fmt(st['np'])}",
         zone[1] if zone and v.get("power_w") else FG, 1.2),
        ("Cadence", fmt(v.get("cadence_rpm")), "rpm", f"avg {fmt(st['avg_cadence'])}",
         DIM if cad and not CADENCE_BAND[0] <= cad <= CADENCE_BAND[1] else FG, 1),
        ("Balance", ("--", "--") if bal is None else (f"{round(100 - bal)}", f"{round(bal)}"), "",
         "" if st["balance_right"] is None
         else (f"avg {100 - st['balance_right']:.1f}", f"{st['balance_right']:.1f}"), FG, 1.1),
        ("Heart rate", fmt(v.get("heart_rate_bpm")), "bpm", f"RR {fmt(h.get('rr'))} ms" if strap else "",
         HR if v.get("heart_rate_bpm") else FG, 1.2),
        ("DFA α1", fmt(a1, 2), "", (f"{h['cov']:.0f}% of beats   art {fmt(h.get('art'), 1)}%" if h.get("cov", 100) < 97
                                    else f"RMSSD {fmt(h.get('rmssd'), 1)}   art {fmt(h.get('art'), 1)}%"
                                    if a1 is not None else h.get("note", "")), alpha_colour(a1, h.get("art")), 1.1),
        ("Ride time", f"{secs // 3600}:{secs // 60 % 60:02d}:{secs % 60:02d}", "",
         {"ready": "not started", "paused": "paused"}.get(ride["state"], ride["elapsed"]),
         FG if ride["state"] == "recording" else DIM, 1.1),
    ]
    key = (W, H, tuple(tiles), strap and strap["warn"], strap is None)
    if getattr(c, "_key", None) == key:
        return c._strip
    R = _retained(c, "tiles")
    R.begin()
    gap = max(10.0, H * 0.07)
    total = sum(t[5] for t in tiles)
    pad = max(12.0, H * 0.14)
    x = 0.0
    for label, value, unit, sub, colour, weight in tiles:
        w = (W - gap * (len(tiles) - 1)) * weight / total
        if label == "Heart rate":
            hr_panel = len(R.ops)                 # op index of this tile's background
        img = _panel_image(round(x + w) - round(x), H, H * 0.08)
        if img is None:
            rrect(R, x + 0.5, 0.5, x + w - 0.5, H - 0.5, H * 0.08, fill=PANEL, outline=BORDER, width=1)
        else:
            R.create_image(round(x), 0, image=_photo(("panel", img.size, round(H * 0.08, 1)), img), anchor="nw")
        R.create_text(x + pad, pad, anchor="nw", text=track(label), fill=DIM, font=F.ui(H * 0.08, True))
        if label == "Power" and zone and v.get("power_w"):          # zone badge, top right
            zf = F.ui(H * 0.075, True)
            zw, zh = F.width(zf, zone[0]), F.height(zf)
            zx, zy = x + w - pad, pad + zh / 2
            rrect(R, zx - zw - H * 0.05, zy - zh / 2 - H * 0.012, zx + H * 0.05, zy + zh / 2 + H * 0.012, H * 0.05,
                  fill=blend(PANEL, zone[1], 0.18), outline="")
            R.create_text(zx, zy, anchor="e", text=zone[0], fill=zone[1], font=zf)
        if isinstance(value, tuple):          # balance: left leg on the left, right leg on the right
            _balance(R, F, x, w, H, pad, value, sub)
            x += w + gap
            continue
        unit_w = F.width(F.ui(H * 0.1), unit) + H * 0.05 if unit else 0
        size = min(H * 0.4, (w - 2 * pad - unit_w) / (0.55 * max(2, len(value))))
        vx, vf = x + pad - size * 0.05, F.num(size)
        R.create_text(vx, H * 0.535, anchor="w", text=value, fill=colour, font=vf)
        if unit:
            right, bottom = vx + F.width(vf, value), H * 0.535 + F.height(vf) / 2
            R.create_text(right + H * 0.035, bottom - size * 0.22, anchor="sw", text=unit, fill=DIM, font=F.ui(H * 0.1))
        sub_colour = WARN if label == "Ride time" and ride["state"] == "paused" else DIM
        if label == "DFA α1" and h.get("cov", 100) < 97:
            sub_colour = WARN                     # beats missing from the strap: read α1 with care
        if label == "Heart rate" and strap:
            strip = {"rect": (x, 0, x + w, H), "rad": H * 0.08, "panel": hr_panel,
                     "dot": (x + w - pad - H * 0.025, pad + H * 0.045), "dot_r": H * 0.025}
            if strap["warn"]:
                sub, sub_colour = strap["warn"], WARN
        if sub:
            R.create_text(x + pad, H - pad + H * 0.015, anchor="sw", text=sub, fill=sub_colour, font=F.ui(H * 0.085))
        x += w + gap
    R.end()
    if strip:
        strip["panel"] = R.items[strip["panel"]][3]      # op index -> Tk item id
        c.tag_raise("strip", strip["panel"])
    c._key, c._strip = key, strip
    return strip


# --------------------------------------------------------------------------- #
# Cycling Dynamics panel
# --------------------------------------------------------------------------- #
def _arc(c, x, y, r, a0, a1, colour, width) -> None:
    """Arc clockwise from a0 to a1, angles in degrees from top dead centre."""
    ext = (a1 - a0) % 360
    if ext < 0.5:
        return
    c.create_arc(x - r, y - r, x + r, y + r, start=90 - a0, extent=-ext, style="arc", outline=colour, width=width)


def _ring_image(r: float, pp):
    """The ring as an anti-aliased image (None without Pillow); pp angles rounded to 1 degree."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    ss, t = 4, r * 0.13
    ro = r + t / 2                                   # outer edge; Pillow's arc width grows inwards
    size = int(2 * ro + 4)
    img = Image.new("RGB", (size * ss, size * ss), _rgb(PANEL))
    d = ImageDraw.Draw(img)
    c0 = size * ss / 2
    box = [c0 - ro * ss, c0 - ro * ss, c0 + ro * ss, c0 + ro * ss]

    def arc(a0, a1, colour, width):                  # clockwise from a0 to a1, 0 = top dead centre
        ext = (a1 - a0) % 360
        if ext >= 0.5:
            d.arc(box, a0 - 90, a0 - 90 + ext, fill=_rgb(colour), width=max(1, round(width * ss)))
    if pp:
        start, pk_s, pk_e, end = pp
        if (start - end) % 360 > 9:
            arc(end + 4, start - 4, RING, t)
        arc(start, end, PHASE, t)
        arc(pk_s, pk_e, PEAK, t * 1.8)
    else:
        d.ellipse(box, outline=_rgb(RING), width=round(t * ss))
    return img.reduce(ss)


def _ring(c, F, x, y, r, pp, leg_w, pct, letter) -> None:
    """Garmin's power-phase ring: grey rest of the stroke, light arc = power phase,
    thicker dark arc (growing inwards) = peak power phase, small gaps between."""
    t = r * 0.13
    key = ("ring", round(r, 1), None if pp is None else tuple(round(a) for a in pp))
    img = _IMAGES.get(key) or _ring_image(r, key[2])
    if img is not None:
        _IMAGES[key] = img
        if len(_IMAGES) > 600:                       # ring images: keep the cache bounded
            for k in [k for k in _IMAGES if k[0] == "ring"][:300]:
                _IMAGES.pop(k)
        c.create_image(x, y, image=_photo(key, img))
    elif pp:
        start, pk_s, pk_e, end = pp
        gap = 4
        if (start - end) % 360 > 2 * gap + 1:
            _arc(c, x, y, r, end + gap, start - gap, RING, t)
        _arc(c, x, y, r, start, end, PHASE, t)
        _arc(c, x, y, r - t * 0.4, pk_s, pk_e, PEAK, t * 1.8)
    else:
        c.create_oval(x - r, y - r, x + r, y + r, outline=RING, width=t)
    c.create_text(x, y - r * 0.06, text=fmt(leg_w), fill=FG, font=F.num(r * 0.5))
    c.create_text(x, y + r * 0.36, text="W" if pct is None else f"W · {pct:.0f}%", fill=DIM, font=F.ui(r * 0.18))
    lx = x - r * 1.12 if letter == "L" else x + r * 1.12
    c.create_text(lx, y + r * 0.95, text=letter, fill=DIM, font=F.ui(r * 0.22, True))


def _silhouette():
    """The pedal as one flat shape: every layer's outline filled in one grey, with only the
    see-through holes of the body (the hook, the cleat window) left open. -> [(polygon, fill?)]"""
    out = []
    for name, outer, holes in pedal_outline.LAYERS:
        out += [(poly, True) for poly in outer]
        if name == "body":
            out += [(poly, False) for poly in holes]
    # holes last, so a hole punched in the body isn't filled again by a later layer
    return [x for x in out if x[1]] + [x for x in out if not x[1]]


def _pedal_body(c, cx, top, h, side) -> None:
    """The pedal as a quiet grey silhouette behind the PCO lines (static: drawn once per window
    size). h = pedal length in pixels. Anti-aliased with Pillow, Tk polygons otherwise."""
    inboard = 1 if side == "left" else -1          # the crank is on the right of the left pedal
    xs = [x for _, outer, _ in pedal_outline.LAYERS for poly in outer for x, _ in poly]
    x_min, x_max = min(xs), max(xs)
    try:
        from PIL import Image, ImageDraw
        ss = 3
        w_img, h_img = int((x_max - x_min) * h) + 4, int(h) + 4
        key = ("pedal_sil", side, round(h, 1))
        img = _IMAGES.get(key)
        if img is None:
            img = Image.new("RGB", (w_img * ss, h_img * ss), _rgb(PANEL))
            d = ImageDraw.Draw(img)
            ox = (x_max if inboard < 0 else -x_min) * h + 2      # image x of the pedal centre line
            Q = lambda pts: [((ox + inboard * x * h) * ss, (2 + y * h) * ss) for x, y in pts]
            for poly, solid in _silhouette():
                d.polygon(Q(poly), fill=_rgb(PEDAL_SIL if solid else PANEL))
            img = _IMAGES[key] = img.reduce(ss)
        ox = (x_max if inboard < 0 else -x_min) * h + 2
        c.create_image(round(cx - ox), round(top - 2), image=_photo(key, img), anchor="nw")
        return
    except ImportError:
        pass
    P = lambda pts: [v for x, y in pts for v in (cx + inboard * x * h, top + y * h)]
    for poly, solid in _silhouette():
        c.create_polygon(P(poly), fill=PEDAL_SIL if solid else PANEL, outline="")


def _pedal_lines(c, F, cx, top, h, side, pco, rng) -> None:
    """Garmin-style PCO lines over the drawing: red = now, blue = the ride's range, dotted = centre."""
    inboard = 1 if side == "left" else -1
    # positive PCO = outboard (away from the crank); the platform is about 62 mm wide
    mm = h * 0.72 / 62
    xpos = lambda v: cx - inboard * max(-25, min(25, v)) * mm
    y0, y1 = top - h * 0.05, top + h * 1.07
    if rng is not None:
        for v in rng:
            c.create_line(xpos(v), y0, xpos(v), y1, fill=PCO_RANGE, width=max(2, h * 0.022))
    if pco is not None:
        c.create_line(xpos(pco), y0, xpos(pco), y1, fill=NOW_LINE, width=max(2, h * 0.02))
    c.create_line(cx, top - h * 0.1, cx, y1, fill=FG, dash=(1, 3), width=1)
    c.create_text(cx - inboard * h * 0.47, top - h * 0.02, text="+", fill=DIM, font=F.ui(h * 0.13, True))
    c.create_text(cx + inboard * h * 0.44, top - h * 0.02, text="−", fill=DIM, font=F.ui(h * 0.13, True))


def _stat(c, F, x, y, label, value, s) -> None:
    c.create_text(x, y, anchor="s", text=track(label), fill=DIM, font=F.ui(11.5 * s, True))
    c.create_text(x, y + 5 * s, anchor="n", text=value, fill=FG, font=F.num(24 * s))


def _section(c, F, W, y, title, s, pad) -> None:
    t = c.create_text(W / 2, y, text=track(title), fill=DIM, font=F.ui(12.5 * s, True))
    b = c.bbox(t)
    c.create_line(pad, y, b[0] - 12 * s, y, fill=GRID, width=1)
    c.create_line(b[2] + 12 * s, y, W - pad, y, fill=GRID, width=1)


def _dyn_key(v, st) -> tuple:
    """What the dynamics panel shows, rounded as displayed."""
    r = lambda x, d=0: None if x is None else round(x, d)
    shown = [v.get("rider_position"), r(v.get("balance_right_pct"))]
    for side in ("left", "right"):
        shown += [r(v.get(f"power_{side}_w")), r(v.get(f"te_{side}_pct")), r(v.get(f"ps_{side}_pct")),
                  v.get(f"pco_{side}_mm"), r(st.get(f"pco_{side}"), 1), st.get(f"pco_{side}_range")]
        shown += [r(v.get(f"pp_{side}_{k}_deg")) for k in ("start", "peak_start", "peak_end", "end")]
    return tuple(shown)


def draw_dynamics(c, F, v, st, part=None) -> None:
    """Static parts (panel, headings, pedal drawings) are drawn once per window size. The values
    come in three parts (1 rings, 2 PCO, 3 power-phase angles), each its own Retained layer: the
    GUI draws them on separate frames, so a change repaints only that part of the panel."""
    W, H = c.winfo_width(), c.winfo_height()
    if W < 80 or H < 80:
        return
    parts = (1, 2, 3) if part is None else (part,)
    key = (W, H, part, _dyn_key(v, st))
    if c.__dict__.setdefault("_keys", {}).get(part) == key:
        return
    c._keys[part] = key
    s = min(W / 560, H / 760)
    pad = 26 * s
    xl, xr = W * 0.27, W * 0.73
    ph = min(H * 0.17, W * 0.2)
    pedals = (("left", xl - W * 0.04), ("right", xr + W * 0.04))
    if getattr(c, "_static", None) != (W, H):
        c.delete("all")
        for k in (1, 2, 3):
            _retained(c, f"d{k}").reset()
        c._keys = {part: key}
        panel(c, W, H, 12 * s)
        c.create_text(pad, pad, anchor="nw", text=track("Cycling dynamics"), fill=FG, font=F.ui(13.5 * s, True))
        _section(c, F, W, H * 0.105, "Power phase", s, pad)
        _section(c, F, W, H * 0.5, "Platform center offset", s, pad)
        for side, x in pedals:
            _pedal_body(c, x, H * 0.555, ph, side)
        items = (("now", NOW_LINE), ("ride range p10–p90", PCO_RANGE))
        widths = [text_width(c, t, F.ui(12 * s)) for t, _ in items]
        x, ly = W / 2 - (sum(widths) + 2 * 20 * s + 18 * s) / 2, H * 0.86
        for (text, col), tw in zip(items, widths):
            c.create_line(x, ly, x + 14 * s, ly, fill=col, width=max(2, 2.5 * s))
            c.create_text(x + 20 * s, ly, anchor="w", text=text, fill=DIM, font=F.ui(12 * s))
            x += 20 * s + tw + 18 * s
        c.create_line(pad, H * 0.885, W - pad, H * 0.885, fill=GRID, width=1)
        c.create_line(W / 2, H * 0.905, W / 2, H - pad * 0.8, fill=GRID, width=1)
        for side, x in (("left", W * 0.25), ("right", W * 0.75)):
            c.create_text(x, H * 0.915, text=track(f"{side} PP"), fill=DIM, font=F.ui(11.5 * s, True))
        c.addtag_all("static")
        c._static = (W, H)
        parts = (1, 2, 3)

    if 1 in parts:
        R = _retained(c, "d1")
        R.begin()
        _dyn_rings(R, F, v, W, H, s, pad, xl, xr)
        R.end()
    if 2 in parts:
        R = _retained(c, "d2")
        R.begin()
        _dyn_pco(R, F, v, st, H, s, pedals, ph)
        R.end()
    if 3 in parts:
        R = _retained(c, "d3")
        R.begin()
        _dyn_pp(R, F, v, W, H, s)
        R.end()


def _dyn_rings(R, F, v, W, H, s, pad, xl, xr) -> None:
    pos = v.get("rider_position")
    if pos:
        col = GOOD if pos == "seated" else WARN
        txt, fnt = track(pos.replace("to_", "→ ")), F.ui(11.5 * s, True)
        xe, ye = W - pad - 10 * s, pad + 8 * s
        tw, th = F.width(fnt, txt), F.height(fnt)
        R.create_text(xe, ye, anchor="e", text=txt, fill=col, font=fnt)
        rrect(R, xe - tw - 10 * s, ye - th / 2 - 4 * s, xe + 10 * s, ye + th / 2 + 4 * s, 10 * s,
              fill="", outline=col, width=1)

    # --- power phase rings
    r = min(H * 0.088, W * 0.13)
    yr = H * 0.24
    for side, x, letter in (("left", xl, "L"), ("right", xr, "R")):
        pp = None
        if v.get(f"pp_{side}_start_deg") is not None:
            pp = tuple(v[f"pp_{side}_{k}_deg"] for k in ("start", "peak_start", "peak_end", "end"))
        bal = v.get("balance_right_pct")
        pct = None if bal is None else (100 - bal if side == "left" else bal)
        _ring(R, F, x, yr, r, pp, v.get(f"power_{side}_w"), pct, letter)
        _stat(R, F, x - W * 0.085, H * 0.395, "Torque eff", fmt(v.get(f"te_{side}_pct")) + "%", s)
        _stat(R, F, x + W * 0.085, H * 0.395, "Smoothness", fmt(v.get(f"ps_{side}_pct")) + "%", s)



def _dyn_pco(R, F, v, st, H, s, pedals, ph) -> None:
    for side, x in pedals:
        pco = v.get(f"pco_{side}_mm")
        _pedal_lines(R, F, x, H * 0.555, ph, side, pco, st.get(f"pco_{side}_range"))
        txt, fnt = "--" if pco is None else f"{pco:+d}", F.num(30 * s)
        R.create_text(x, H * 0.775, text=txt, fill=FG, font=fnt)
        R.create_text(x + F.width(fnt, txt) / 2 + 4 * s, H * 0.775 + F.height(fnt) / 2 - 7 * s, anchor="sw",
                      text="mm", fill=DIM, font=F.ui(12.5 * s))
        a = st.get(f"pco_{side}")
        R.create_text(x, H * 0.805, anchor="n", text="" if a is None else f"avg {a:+.1f}", fill=DIM, font=F.ui(12.5 * s))



def _dyn_pp(R, F, v, W, H, s) -> None:
    for side, x in (("left", W * 0.25), ("right", W * 0.75)):
        st_, en = v.get(f"pp_{side}_start_deg"), v.get(f"pp_{side}_end_deg")
        pk_s, pk_e = v.get(f"pp_{side}_peak_start_deg"), v.get(f"pp_{side}_peak_end_deg")
        R.create_text(x, H * 0.945, text="--" if st_ is None else f"{round(st_)}°–{round(en)}°",
                      fill=FG, font=F.num(21 * s))
        R.create_text(x, H * 0.974, text="" if pk_s is None else f"peak {round(pk_s)}°–{round(pk_e)}°",
                      fill=PHASE, font=F.ui(12 * s))


# --------------------------------------------------------------------------- #
# Graphs
# --------------------------------------------------------------------------- #
class _Binner:
    """Incremental pixel-column binning of an append-only series.

    Columns sit on a grid fixed to ride time (column = t // seconds-per-pixel), so a finished
    column never changes: it is kept between redraws and only new points are processed.
    Each column is [col, sum, n, lo, hi, starts_segment]."""

    def __init__(self) -> None:
        self.key = None

    def update(self, rows, value, per_px: float, gap: float, upto=None) -> list:
        if (per_px, gap) != self.key:
            self.key, self.cols, self.i, self.last = (per_px, gap), [], 0, None
        cols, n = self.cols, len(rows) if upto is None else upto
        brk_after = max(gap, 2 * per_px)
        for k in range(self.i, n):
            t, v = value(rows[k])
            if v is None:
                continue
            brk = self.last is not None and t - self.last > brk_after
            col = int(t // per_px)
            if cols and cols[-1][0] == col and not brk:
                c = cols[-1]
                c[1] += v
                c[2] += 1
                if v < c[3]:
                    c[3] = v
                elif v > c[4]:
                    c[4] = v
            else:
                cols.append([col, v, 1, v, v, brk])
            self.last = t
        self.i = n
        return cols


def _visible(cols, t0, t1, per_px) -> list:
    """The columns inside [t0, t1] (plus one to the left, so lines reach the edge)."""
    a = max(0, bisect_left(cols, int(t0 // per_px), key=lambda c: c[0]) - 1)
    b = bisect_left(cols, int(t1 // per_px) + 1, key=lambda c: c[0])
    return cols[a:b]


def _segments(cols, X, per_px, stride: int = 1) -> list:
    """Visible columns -> line segments of (x, mean, lo, hi), broken at gaps. stride > 1 keeps
    every n-th column (plus each segment's ends): smooth signals don't need a vertex per pixel,
    and Windows paints long thick polylines slowly."""
    segs = []
    for i, c in enumerate(cols):
        if c[5] or not segs:
            segs.append([])
        elif stride > 1 and i % stride and i + 1 < len(cols) and not cols[i + 1][5]:
            continue
        segs[-1].append((X((c[0] + 0.5) * per_px), c[1] / c[2], c[3], c[4]))
    return segs


LIVE_WINDOW = 300.0          # seconds of recent data in the scrolling graphs
SS = 2                       # graphs are drawn 2x larger and scaled down: anti-aliased lines


def _nice_up(v: float, step: float) -> float:
    return step * math.ceil(v / step)


def _rgb(colour: str) -> tuple:
    return tuple(int(colour[i:i + 2], 16) for i in (1, 3, 5))


def _runs(points, gap: float) -> list:
    """[(t, x, y)] -> lists of (x, y), broken where the data has a gap."""
    runs, last = [], None
    for t, x, y in points:
        if last is None or t - last > gap:
            runs.append([])
        runs[-1].append((x, y))
        last = t
    return runs


class _Painter:
    """A supersampled Pillow canvas: draw in final pixels, get back an anti-aliased Tk image."""
    _cache: dict = {}

    def __init__(self, w: int, h: int, bg: str = PANEL, ss: int = SS) -> None:
        from PIL import Image, ImageDraw
        self.Image, self.w, self.h, self.ss, self.bg = Image, max(1, int(w)), max(1, int(h)), ss, bg
        self.img = Image.new("RGB", (self.w * ss, self.h * ss), _rgb(bg))
        self.d = ImageDraw.Draw(self.img)

    def _sc(self, pts):
        return [(x * self.ss, y * self.ss) for x, y in pts]

    def rect(self, x0, y0, x1, y1, colour) -> None:
        self.d.rectangle([x0 * self.ss, y0 * self.ss, x1 * self.ss, y1 * self.ss], fill=_rgb(colour))

    def hline(self, y, colour, dash: bool = False) -> None:
        y = round(y * self.ss)
        if not dash:
            self.d.line([(0, y), (self.w * self.ss, y)], fill=_rgb(colour), width=self.ss)
        else:
            for x in range(0, self.w * self.ss, 8 * self.ss):
                self.d.line([(x, y), (x + 3 * self.ss, y)], fill=_rgb(colour), width=self.ss)

    def vline(self, x, colour) -> None:
        x = round(x * self.ss)
        self.d.line([(x, 0), (x, self.h * self.ss)], fill=_rgb(colour), width=self.ss)

    def line(self, pts, colour, width: float) -> None:
        if len(pts) > 1:
            self.d.line(self._sc(pts), fill=_rgb(colour), width=max(1, round(width * self.ss)), joint="curve")

    def dot(self, x, y, r, colour) -> None:
        self.d.ellipse([(x - r) * self.ss, (y - r) * self.ss, (x + r) * self.ss, (y + r) * self.ss], fill=_rgb(colour))

    def area(self, runs, base: float, colour, top_alpha: float, bottom_alpha: float) -> None:
        """Fill under lines (a list of point runs) with a vertical fade: strong at the top of the
        plot, faint at the bottom. One mask for all runs."""
        runs = [r for r in runs if len(r) > 1]
        if not runs:
            return
        from PIL import ImageDraw
        size = self.img.size
        mask = self.Image.new("L", size, 0)
        md = ImageDraw.Draw(mask)
        for pts in runs:
            md.polygon(self._sc([(pts[0][0], base)] + pts + [(pts[-1][0], base)]), fill=255)
        # the fade, already blended onto the background, cached per size: one masked paste
        key = (size, colour, self.bg, top_alpha, bottom_alpha)
        grad = self._cache.get(key)
        if grad is None:
            col = self.Image.new("RGB", (1, size[1]))
            col.putdata([_rgb(blend(self.bg, colour, top_alpha + (bottom_alpha - top_alpha) * y / size[1]))
                         for y in range(size[1])])
            grad = self._cache[key] = col.resize(size)
        self.img.paste(grad, (0, 0), mask)

    def photo(self):
        from PIL import ImageTk
        return ImageTk.PhotoImage(self.img.reduce(self.ss))


class _TilePlot:
    """One scrolling live plot, made of image tiles a whole number of pixels wide (~20 s each)
    in ride-time pixel coordinates. A tile is rendered once and re-rendered only when new data
    falls inside it (in practice the newest tile, at most 4 times a second). Scrolling moves the tiles
    by whole pixels (a cheap blit for Tk). Y-axes are fixed and only ever widen."""

    def __init__(self, c, F, n: int, render) -> None:
        self.c, self.F, self.n, self.render = c, F, n, render
        self.tag = f"live{n}"
        self.tiles: dict = {}
        self.ranges: dict = {}
        self.off = 0
        self.next_minute = 0.0

    def setup(self, rect, s: float) -> None:
        xl, top, xr, bot = (round(v) for v in rect)
        self.rect, self.s = (xl, top, xr, bot), s
        self.pxs = (xr - xl) / LIVE_WINDOW
        self.tw = max(24, round(20 * self.pxs))
        self.h = bot - top

    def y(self, name: str, v: float) -> float:
        """Value -> y inside a tile (0 = top of the plot)."""
        lo, hi = self.ranges[name]
        return self.h - (min(max(v, lo), hi) - lo) / (hi - lo) * self.h

    def clear(self, t_now: float) -> None:
        self.c.delete(self.tag)
        self.tiles = {}
        self.off = math.floor(t_now * self.pxs) - self.rect[2]
        self.next_minute = 60 * math.floor((t_now - LIVE_WINDOW) / 60)

    def scroll(self, t_now: float) -> None:
        off = math.floor(t_now * self.pxs) - self.rect[2]
        if off != self.off:
            self.c.move(self.tag, self.off - off, 0)
            self.off = off
        if self.n != 2:
            return
        xl, top, xr, bot = self.rect
        while self.next_minute <= t_now:          # time labels under plot 2 scroll with it
            if self.next_minute > t_now - LIVE_WINDOW - 60:
                m = int(self.next_minute // 60)
                lab = self.c.create_text(self.next_minute * self.pxs - self.off, bot + 8 * self.s, anchor="n",
                                         text=f"{m // 60}:{m % 60:02d}", fill=DIM,
                                         font=self.F.num(15 * self.s), tags=(self.tag, "minute"))
                self.c.tag_lower(lab, "mask")
            self.next_minute += 60

    def refresh(self, t_now: float, dirty_from) -> None:
        """Render missing tiles, and re-render tiles reaching past `dirty_from` (the earliest ride
        time new data could have changed; None = nothing new). At most 2 renders per call,
        newest first, so a frame never waits long."""
        xl, top, xr, bot = self.rect
        k_min, k_max = (self.off + xl) // self.tw - 1, (self.off + xr) // self.tw
        for k in [k for k in self.tiles if k < k_min]:
            self.c.delete(self.tiles.pop(k)[0])
        if dirty_from is not None:
            for k, tile in self.tiles.items():
                if (k + 1) * self.tw / self.pxs >= dirty_from:
                    self.tiles[k] = (tile[0], tile[1], dirty_from if tile[2] is None else min(tile[2], dirty_from))
        done = 0
        for k in range(k_max, k_min - 1, -1):
            tile = self.tiles.get(k)
            ta = k * self.tw / self.pxs
            stale = tile is not None and tile[2] is not None and ta + self.tw / self.pxs >= tile[2]
            if tile is not None and not stale:
                continue
            photo = self.render(self, ta)
            if tile is None:
                item = self.c.create_image(k * self.tw - self.off, top, image=photo, anchor="nw", tags=self.tag)
                self.c.tag_lower(item, "mask")
            else:
                item = tile[0]
                self.c.itemconfigure(item, image=photo)
            self.tiles[k] = (item, photo, None)
            done += 1
            if done >= 2:
                break
        for item in self.c.find_withtag("minute"):   # labels that scrolled out
            b = self.c.bbox(item)
            if b and b[2] < xl - 100:
                self.c.delete(item)


class LivePlots:
    """The scrolling "last 5 minutes" graphs: power + heart rate, and beat-to-beat intervals +
    DFA alpha 1, anti-aliased (Pillow). The GUI calls update() every frame, one plot at a time."""

    def __init__(self, c, F, ftp) -> None:
        self.c, self.F, self.ftp = c, F, ftp
        self.size = None
        self.p1 = _TilePlot(c, F, 1, self._render1)
        self.p2 = _TilePlot(c, F, 2, self._render2)
        self.session = None
        self.checked = {"power": 0, "hr": 0, "beats": 0}
        self.seen: dict = {}                      # per plot: data counts already drawn
        self.rendered: dict = {}                  # per plot: when tiles were last re-rendered

    # ---- layout and the vector parts (axis labels, masks, titles)
    def _layout(self, W: int, H: int) -> None:
        c, F = self.c, self.F
        c.delete("all")
        s = min(W / 820, H / 560)
        self.s, self.W, self.H = s, W, H
        pad = 18 * s
        panel(c, W, H, 12 * s)
        xl, xr = pad + 46 * s, W - pad - 46 * s
        mid = H * 0.5
        self.p1.setup((xl, pad + 30 * s, xr, mid - 10 * s), s)
        self.p2.setup((xl, mid + 34 * s, xr, H - pad - 30 * s), s)
        # masks: Tk can't clip, so tiles scrolling past the plot edges disappear under these. They are
        # cut from the panel card itself, so its shaded border stays intact.
        card = _panel_image(W, H, 12 * s)
        self._masks = []
        for p, bot in ((self.p1, self.p1.rect[3] + 2), (self.p2, H)):
            top = p.rect[1] - 2
            for x0, x1 in ((0, p.rect[0]), (p.rect[2], W)):
                if card is None:
                    c.create_rectangle(x0 - 10, top, x1 + 10, bot, fill=PANEL, outline="", tags="mask")
                    continue
                from PIL import ImageTk
                ph = ImageTk.PhotoImage(card.crop((int(x0), int(top), int(x1), int(bot))))
                self._masks.append(ph)
                c.create_image(int(x0), int(top), image=ph, anchor="nw", tags="mask")

        def header(y, title, legend):
            c.create_text(pad, y, anchor="w", text=track(title), fill=FG, font=F.ui(14.5 * s, True))
            x = W - pad
            for text, colour in reversed(legend):
                c.create_text(x, y, anchor="e", text=text, fill=DIM, font=F.ui(14 * s))
                bx = x - F.width(F.ui(14 * s), text) - 7 * s
                c.create_line(bx - 14 * s, y, bx, y, fill=colour, width=max(2, 2.5 * s))
                x = bx - 14 * s - 20 * s
        header(pad + 8 * s, "Last 5 min · power & heart rate", [("power", POWER), ("heart rate", HR)])
        header(mid + 16 * s, "Last 5 min · beat intervals & DFA α1", [("RR ms", RR), ("α1", A1)])
        self.size = (W, H)

    def _labels(self, p) -> None:
        c, F, s = self.c, self.F, self.s
        c.delete(p.tag + "lab")
        xl, top, xr, bot = p.rect

        def lab(x, v, text, colour, anchor):
            c.create_text(x, top + v, text=text, fill=colour, anchor=anchor, font=F.num(15 * s), tags=p.tag + "lab")
        if p.n == 1:
            hi = p.ranges["power"][1]
            step = nice_step(hi, 4)
            v = 0.0
            while v <= hi + 1e-6:
                lab(xl - 7 * s, p.y("power", v), f"{v:g}", blend(POWER, PANEL, 0.2), "e")
                v += step
            if "hr" in p.ranges:
                lo, hi = p.ranges["hr"]
                for b in range(int(lo), int(hi) + 1, 10 if hi - lo <= 80 else 20):
                    lab(xr + 7 * s, p.y("hr", b), f"{b}", blend(HR, PANEL, 0.2), "w")
        else:
            if "rr" in p.ranges:
                lo, hi = p.ranges["rr"]
                step = nice_step(hi - lo, 4)
                v = step * math.ceil(lo / step)
                while v <= hi + 1e-6:
                    lab(xl - 7 * s, p.y("rr", v), f"{v:g}", blend(RR, PANEL, 0.2), "e")
                    v += step
            elif self.session is not None:
                c.create_text((xl + xr) / 2, (top + bot) / 2, fill=DIM, font=F.ui(12 * s), tags=p.tag + "lab",
                              text="no heart-rate strap: start with --hr (start_recorder_hr.bat)"
                              if not self.session.hrv.beats else "waiting for beats…")
            for a in (0.5, 0.75, 1.0, 1.25, 1.5):
                lab(xr + 7 * s, p.y("a1", a), f"{a:.2f}", blend(A1, PANEL, 0.2), "w")

    # ---- axis ranges: fixed, only ever widen
    def _widen(self) -> bool:
        """Check the data that arrived since the last check; True if an axis had to widen."""
        ses, p1, p2, ck = self.session, self.p1, self.p2, self.checked
        widened = False
        for t, w, _ in ses.live_power[ck["power"]:]:
            if w > p1.ranges["power"][1]:
                p1.ranges["power"] = (0, _nice_up(w * 1.1, 50))
                widened = True
        ck["power"] = len(ses.live_power)
        for t, bpm in ses.live_hr[ck["hr"]:]:
            if not bpm:
                continue
            lo, hi = p1.ranges.get("hr", (10 * math.floor((bpm - 20) / 10), 10 * math.ceil((bpm + 20) / 10)))
            new = (min(lo, 10 * math.floor((bpm - 5) / 10)), max(hi, 10 * math.ceil((bpm + 5) / 10)))
            if p1.ranges.get("hr") != new:
                p1.ranges["hr"], widened = new, True
        ck["hr"] = len(ses.live_hr)
        judged = ses.hrv._judged
        for b in ses.hrv.beats[ck["beats"]:judged]:
            if b.artifact:
                continue
            lo, hi = p2.ranges.get("rr", (25 * math.floor((b.rr - 50) / 25), 25 * math.ceil((b.rr + 50) / 25)))
            new = (min(lo, 25 * math.floor((b.rr - 20) / 25)), max(hi, 25 * math.ceil((b.rr + 20) / 25)))
            if p2.ranges.get("rr") != new:
                p2.ranges["rr"], widened = new, True
        ck["beats"] = judged
        return widened

    # ---- tile renderers
    def _render1(self, p, ta: float):
        ses, s = self.session, self.s
        tb = ta + p.tw / p.pxs
        P = _Painter(p.tw, p.h)
        X = lambda t: (t - ta) * p.pxs
        if self.ftp:
            P.rect(0, p.y("power", 0.75 * self.ftp), p.tw, p.y("power", 0.56 * self.ftp), Z2_FILL)
        hi = p.ranges["power"][1]
        step = nice_step(hi, 4)
        v = step
        while v < hi + 1e-6:
            P.hline(p.y("power", v), GRID)
            v += step
        for m in range(math.ceil(ta / 60), math.floor(tb / 60) + 1):
            P.vline(X(m * 60), GRID)
        lp = ses.live_power
        i0 = bisect_left(lp, ta - 4, key=lambda r: r[0])
        i1 = bisect_left(lp, tb + 4, key=lambda r: r[0])
        rows = lp[i0:i1]
        P.area(_runs([(t, X(t), p.y("power", w)) for t, w, _ in rows], 3), p.h, POWER, 0.42, 0.04)
        for run in _runs([(t, X(t), p.y("power", a)) for t, _, a in rows], 3):
            P.line(run, POWER, max(2.5, 2.9 * s))
        if "hr" in p.ranges:
            lh = ses.live_hr
            i0 = bisect_left(lh, ta - 8, key=lambda r: r[0])
            i1 = bisect_left(lh, tb + 4, key=lambda r: r[0])
            win, pts = deque(), []
            for t, bpm in lh[i0:i1]:
                if not bpm:
                    continue
                win.append((t, bpm))                  # 3 s mean: the strap's whole-bpm steps look jaggy
                while win[0][0] < t - 3:
                    win.popleft()
                if t >= ta - 4:
                    pts.append((t, X(t), p.y("hr", sum(b for _, b in win) / len(win))))
            for run in _runs(pts, 5):
                P.line(run, HR, max(2.5, 2.7 * s))
        return P.photo()

    def _render2(self, p, ta: float):
        ses, s = self.session, self.s
        tb = ta + p.tw / p.pxs
        P = _Painter(p.tw, p.h)
        X = lambda t: (t - ta) * p.pxs
        if "rr" in p.ranges:
            lo, hi = p.ranges["rr"]
            step = nice_step(hi - lo, 4)
            v = step * math.ceil(lo / step)
            while v <= hi + 1e-6:
                P.hline(p.y("rr", v), GRID)
                v += step
        for m in range(math.ceil(ta / 60), math.floor(tb / 60) + 1):
            P.vline(X(m * 60), GRID)
        P.hline(p.y("a1", 0.75), blend(GOOD, PANEL, 0.4), dash=True)
        P.hline(p.y("a1", 0.5), blend(BAD, PANEL, 0.4), dash=True)
        t0, hrv_ = ses.t0, ses.hrv
        beats = hrv_.beats
        k0 = bisect_left(beats, t0 + timedelta(seconds=ta - 4), hi=hrv_._judged, key=lambda b: b.time)
        k1 = bisect_left(beats, t0 + timedelta(seconds=tb + 4), hi=hrv_._judged, key=lambda b: b.time)
        if "rr" in p.ranges:
            pts, dots = [], []
            for b in beats[k0:k1]:
                t = (b.time - t0).total_seconds()
                (dots if b.artifact else pts).append((t, X(t), p.y("rr", b.rr)))
            for run in _runs(pts, 3):
                P.line(run, RR, max(1.8, 2.0 * s))
            r = max(2.5, 3.0 * s)
            for _, x, y in dots:                  # off-scale artifacts sit fully inside the plot's edge
                P.dot(x, min(max(y, r + 1), p.h - r - 1), r, BAD)
        ser = hrv_.series
        i0 = bisect_left(ser, t0 + timedelta(seconds=ta - 20), key=lambda r: r[0])
        i1 = bisect_left(ser, t0 + timedelta(seconds=tb + 20), key=lambda r: r[0])
        pts = [((ts - t0).total_seconds(), a, art) for ts, a, _, art in ser[i0:i1] if a is not None]
        run, q, last = [], None, None      # coloured by quality; a colour change keeps the line joined
        for t, a, art in pts:
            good = art <= 5
            if last is not None and t - last > 15:
                P.line(run, A1 if q else FAINT, max(3, 3.2 * s))
                run = []
            elif q is not None and good != q:
                P.line(run, A1 if q else FAINT, max(3, 3.2 * s))
                run = run[-1:]
            run.append((X(t), p.y("a1", a)))
            q, last = good, t
        P.line(run, A1 if q else FAINT, max(3, 3.2 * s))
        return P.photo()

    def update(self, session, now_dt, part: int) -> None:
        c = self.c
        W, H = c.winfo_width(), c.winfo_height()
        if W < 150 or H < 150:
            return
        self.session = session
        if session.t0 is None:
            if self.size != (W, H, "empty"):
                c.delete("all")
                c.create_text(W / 2, H / 2, text="waiting for data…", fill=DIM, font=self.F.ui(14))
                self.size = (W, H, "empty")
            return
        try:
            import PIL  # noqa: F401
        except ImportError:
            if self.size != (W, H, "nopil"):
                c.delete("all")
                c.create_text(W / 2, H / 2, fill=DIM, font=self.F.ui(14),
                              text="the graphs need Pillow:  py -3 -m pip install pillow")
                self.size = (W, H, "nopil")
            return
        t_now = (now_dt - session.t0).total_seconds()
        if self.size != (W, H):
            self._layout(W, H)
            self.p1.ranges = {"power": (0, _nice_up(1.5 * self.ftp if self.ftp else 300, 50))}
            self.p2.ranges = {"a1": (0.25, 1.5)}
            self.checked = {"power": 0, "hr": 0, "beats": 0}
            self._widen()
            for p in (self.p1, self.p2):
                p.clear(t_now)
                self._labels(p)
            self.seen = {}
        elif self._widen():                       # an axis widened: redraw both plots' tiles
            for p in (self.p1, self.p2):
                p.clear(t_now)
                self._labels(p)
        p = self.p1 if part == 1 else self.p2
        p.scroll(t_now)
        dirty = None
        if time.monotonic() - self.rendered.get(part, 0) >= 0.25:      # new data: <= 4 renders/s
            seen = self.seen.setdefault(part, {})
            t0 = session.t0
            if part == 1:
                news = [(session.live_power, "power", lambda r: r[0], 4), (session.live_hr, "hr", lambda r: r[0], 4)]
                ends = {"power": len(session.live_power), "hr": len(session.live_hr)}
            else:
                sec = lambda r: (r.time - t0).total_seconds()
                news = [(session.hrv.beats, "beats", sec, 4),
                        (session.hrv.series, "a1", lambda r: (r[0] - t0).total_seconds(), 20)]
                ends = {"beats": session.hrv._judged, "a1": len(session.hrv.series)}
            for rows, name, when, margin in news:
                i = seen.get(name, ends[name])
                if ends[name] > i:
                    t_new = when(rows[i]) - margin
                    dirty = t_new if dirty is None else min(dirty, t_new)
                seen[name] = ends[name]
            if dirty is not None:
                self.rendered[part] = time.monotonic()
        p.refresh(t_now, dirty)


# --------------------------------------------------------------------------- #
# Ride overview: the whole ride on a planned-duration axis
# --------------------------------------------------------------------------- #
OV_TILE = 80                  # px per overview tile


def draw_overview(c, F, session, now, plan_s: float) -> None:
    """Power, heart rate and alpha 1 over the planned ride (--plan), filling left to right; the
    axis extends in 15 min steps if the ride runs longer. One pixel column per span/width
    seconds on a fixed grid, drawn as anti-aliased image tiles: only the tile being filled is
    re-rendered. Scales only ever widen (then every tile is redrawn, a few per call)."""
    W, H = c.winfo_width(), c.winfo_height()
    if W < 150 or H < 80:
        return
    s = min(W / 820, H / 240)
    pad = 18 * s
    origin = session.started                  # the overview is the ride: from Start on
    t_end = 0.0 if origin is None else max(0.0, (now - origin).total_seconds())
    span = plan_s
    while t_end > span:
        span += 900
    xl, xr = round(pad + 44 * s), round(W - pad - 12 * s)
    top, bot = round(pad + 26 * s), round(H - pad - 20 * s)
    w, h = xr - xl, bot - top
    gap = 8 * s
    lanes = [("W", "power"), ("bpm", "hr"), ("α1", "a1")]
    lh = (h - gap * (len(lanes) - 1)) / len(lanes)
    lane_y = [(i * (lh + gap), i * (lh + gap) + lh) for i in range(len(lanes))]
    st = c.__dict__.get("_ov")
    if st is None or st["key"] != (W, H, span):
        c.delete("all")
        c.__dict__.pop("_rets", None)
        panel(c, W, H, 12 * min(W / 820, H / 300))
        st = c._ov = {"key": (W, H, span), "tiles": {}, "bins": {}, "scale": {}}
    xn = t_end / span * w

    # vector parts: title, labels, time axis, the part still to ride, "now"
    R = _retained(c, "ov")
    R.begin()
    elapsed = f"{int(t_end // 3600)}:{int(t_end // 60 % 60):02d}"
    total = f"{int(span // 3600)}:{int(span // 60 % 60):02d}"
    R.create_text(pad, pad + 5 * s, anchor="w", text=track("Ride"), fill=FG, font=F.ui(14.5 * s, True))
    R.create_text(pad + F.width(F.ui(14.5 * s, True), track("Ride")) + 14 * s, pad + 5 * s, anchor="w",
                  text=f"{elapsed} of {total}", fill=DIM, font=F.num(15 * s))
    # aerobic decoupling, right-aligned in the header
    st_ = session.stats()
    x, y, f = W - pad, pad + 5 * s, F.num(15 * s)
    if st_["decoupling"] is None:
        at = st_["decoupling_at"]
        R.create_text(x, y, anchor="e", text=f"Pw:HR decoupling from {at // 3600}:{at // 60 % 60:02d}",
                      fill=FAINT, font=F.ui(13 * s))
    else:
        d = st_["decoupling"]
        col = GOOD if d < 5 else WARN if d < 10 else BAD
        R.create_text(x, y, anchor="e", text=f"{d:+.1f} %", fill=col, font=f)
        x -= F.width(f, f"{d:+.1f} %") + 8 * s
        R.create_text(x, y, anchor="e", text="Pw:HR", fill=DIM, font=F.ui(13 * s))   # the metric's usual spelling
        x -= F.width(F.ui(13 * s), "Pw:HR") + 22 * s
        R.create_text(x, y, anchor="e", text=f"{st_['ef']:.2f}", fill=FG, font=f)
        x -= F.width(f, f"{st_['ef']:.2f}") + 8 * s
        R.create_text(x, y, anchor="e", text=track("EF"), fill=DIM, font=F.ui(12 * s, True))
    R.create_rectangle(xl + xn, top, xr, bot, fill=blend(PANEL, BG, 0.45), outline="")
    for (unit, _), (y0, y1) in zip(lanes, lane_y):
        R.create_text(xl - 8 * s, top + (y0 + y1) / 2, anchor="e", text=unit, fill=DIM, font=F.ui(14 * s))
        R.create_line(xl + xn, top + y1, xr, top + y1, fill=GRID)
    R.create_line(xl + xn, top - 3 * s, xl + xn, bot, fill=FG)
    step = 900 if span <= 7200 else 1800
    for k in range(0, int(span) + 1, step):
        R.create_text(xl + k / span * w, bot + 4 * s, anchor="n", text=f"{k // 3600}:{k // 60 % 60:02d}",
                      fill=DIM, font=F.num(14 * s))
    R.end()
    if origin is None or session.t0 is None:
        return
    try:
        import PIL  # noqa: F401
    except ImportError:
        return

    # data columns on a fixed pixel grid (incremental)
    binw = span / w
    t0 = session.t0
    off = (origin - t0).total_seconds()      # live series count from the first notification
    bins = st["bins"]
    b = lambda name: bins.setdefault(name, _Binner())
    ride = lambda t, v: (t - off, v if t >= off else None)
    cols = {"power": b("power").update(session.live_power, lambda r: ride(r[0], r[1]), binw, 3),
            "hr": b("hr").update(session.live_hr, lambda r: ride(r[0], r[1] or None), binw, 5),
            "a1": b("a1").update(session.hrv.series,
                                 lambda r: ride((r[0] - t0).total_seconds(), r[1]), binw, 15)}
    scale = dict(st["scale"])
    if cols["power"]:
        scale["power"] = max(scale.get("power", 200.0),
                             _nice_up(max(cc[1] / cc[2] for cc in cols["power"]) * 1.1, 50))
    if cols["hr"]:
        lo = 10 * math.floor((min(cc[1] / cc[2] for cc in cols["hr"]) - 5) / 10)
        hi = 10 * math.ceil((max(cc[1] / cc[2] for cc in cols["hr"]) + 5) / 10)
        old = scale.get("hr", (lo, hi))
        scale["hr"] = (min(lo, old[0]), max(hi, old[1]))
    if scale != st["scale"]:                          # a scale widened: redraw every tile
        for item, _ in st["tiles"].values():
            c.delete(item)
        st["tiles"], st["scale"] = {}, scale
    sizes = {name: len(cc) for name, cc in cols.items()}

    def render(k):
        c0 = k * OV_TILE
        P = _Painter(OV_TILE, h)
        for name, (y0, y1) in zip((n for _, n in lanes), lane_y):
            P.hline(y1, GRID)
            cc = cols[name]
            a = bisect_left(cc, c0 - 2, key=lambda x: x[0])
            z = bisect_left(cc, c0 + OV_TILE + 2, key=lambda x: x[0])
            segs = _segments(cc[a:z], lambda t: t / binw - c0, binw)
            if name == "power":
                Y = lambda v, y1=y1: y1 - min(v, scale["power"]) / scale["power"] * lh
                runs = [[(x, Y(m)) for x, m, _, _ in sg] for sg in segs]
                P.area(runs, y1, POWER, 0.45, 0.45)
                for r in runs:
                    P.line(r, POWER, max(1.8, 1.9 * s))
            elif name == "hr" and "hr" in scale:
                lo, hi = scale["hr"]
                for sg in segs:
                    P.line([(x, y1 - (m - lo) / max(1, hi - lo) * lh) for x, m, _, _ in sg], HR, max(2, 2.3 * s))
            elif name == "a1":
                Y = lambda v, y1=y1: y1 - (min(max(v, 0.25), 1.5) - 0.25) / 1.25 * lh
                P.hline(Y(0.75), blend(GOOD, PANEL, 0.4), dash=True)
                for sg in segs:
                    P.line([(x, Y(m)) for x, m, _, _ in sg], A1, max(2, 2.3 * s))
        return P.photo()

    k_now = int(xn // OV_TILE)
    changed_from = None if st.get("sizes") == sizes else max(0, k_now - 1)
    st["sizes"] = sizes
    done = 0
    for k in range(k_now, -1, -1):                    # newest first; at most 3 renders per call
        tile = st["tiles"].get(k)
        if tile is not None and (changed_from is None or k < changed_from):
            continue
        photo = render(k)
        if tile is None:
            item = c.create_image(xl + k * OV_TILE, top, image=photo, anchor="nw")
            c.tag_lower(item, "ov")                  # under the vector parts ("now", the part still to ride)
        else:
            item = tile[0]
            c.itemconfigure(item, image=photo)
        st["tiles"][k] = (item, photo)
        done += 1
        if done >= 3:
            break


# --------------------------------------------------------------------------- #
# Controls: the bottom bar and dialogs
# --------------------------------------------------------------------------- #
BTN_PRIMARY, BTN_GO, BTN_NEUTRAL, BTN_DANGER = "#1f6feb", "#1f9d63", "#2a3039", "#e5484d"


def _pill_image(w: int, h: int, fill: str, outline=None, bg: str = BG):
    """An anti-aliased rounded button face (cached)."""
    key = ("pill", int(w), int(h), fill, outline, bg)
    img = _IMAGES.get(key)
    if img is None:
        from PIL import Image, ImageDraw
        ss = 3
        img = Image.new("RGB", (int(w) * ss, int(h) * ss), _rgb(bg))
        ImageDraw.Draw(img).rounded_rectangle([0, 0, int(w) * ss - 1, int(h) * ss - 1], radius=h * ss * 0.28,
                                              fill=_rgb(fill), outline=None if outline is None else _rgb(outline),
                                              width=ss if outline else 0)
        img = _IMAGES[key] = img.reduce(ss)
    return _photo(key, img)


def canvas_button(c, F, x_right, y, text, hint, colour, tag, on_click, s, bg=BG, outline=None, text_colour="#ffffff"):
    """A button drawn on a canvas (right-aligned at x_right, centred on y): AA face, hover state,
    and a small keyboard hint inside. Returns its left x."""
    f, hf = F.ui(14 * s, True), F.ui(11 * s)
    tw = F.width(f, text) + (F.width(hf, hint) + 10 * s if hint else 0)
    w, h = tw + 36 * s, 38 * s
    x0 = x_right - w
    hover = blend(colour, "#ffffff", 0.12)
    normal_img = _pill_image(w, h, colour, outline, bg)
    hover_img = _pill_image(w, h, hover, outline, bg)
    c.create_image(x0, y - h / 2, image=normal_img, anchor="nw", tags=(tag, tag + "_face", "ctl"))
    c.create_text(x0 + 18 * s, y, anchor="w", text=text, fill=text_colour, font=f, tags=(tag, "ctl"))
    if hint:
        c.create_text(x_right - 18 * s, y, anchor="e", text=hint, fill=blend(text_colour, colour, 0.45),
                      font=hf, tags=(tag, "ctl"))
    # bindings are registered once per tag (every tag_bind would add a Tcl command); they look up
    # the button's current images and action, which change as the bar is redrawn
    handlers = c.__dict__.setdefault("_buttons", {})
    new = tag not in handlers
    handlers[tag] = (normal_img, hover_img, on_click)
    if new:
        c.tag_bind(tag, "<Enter>", lambda e: (c.itemconfigure(tag + "_face", image=handlers[tag][1]),
                                              c.configure(cursor="hand2")))
        c.tag_bind(tag, "<Leave>", lambda e: (c.itemconfigure(tag + "_face", image=handlers[tag][0]),
                                              c.configure(cursor="")))
        c.tag_bind(tag, "<Button-1>", lambda e: handlers[tag][2]())
    return x0


class ControlBar:
    """The bottom bar: sensor status chips on the left; the recording state and the Start /
    Pause / Resume and Stop buttons on the right. Redrawn only when something on it changes."""

    def __init__(self, c, F, actions) -> None:
        self.c, self.F, self.actions = c, F, actions
        self.key = None

    def draw(self, state: str, clock: str, sensors: list, battery, error: str = "") -> None:
        c, F = self.c, self.F
        W, H = c.winfo_width(), c.winfo_height()
        blink = state == "recording" and int(time.monotonic() * 1.25) % 2 == 0
        key = (W, H, state, clock, tuple(sensors), battery, error, blink)
        if key == self.key or W < 100:
            return
        self.key = key
        c.delete("all")
        s = H / 56
        y = H / 2
        # sensors
        x = 4 * s
        for label, detail, ok in sensors:
            col = GOOD if ok else WARN
            c.create_oval(x, y - 4 * s, x + 8 * s, y + 4 * s, fill=col, outline="")
            t = c.create_text(x + 16 * s, y, anchor="w", text=track(label), fill=DIM, font=F.ui(11 * s, True))
            x = c.bbox(t)[2] + 8 * s
            t = c.create_text(x, y, anchor="w", text=detail, fill=FG if ok else WARN, font=F.ui(13 * s))
            x = c.bbox(t)[2] + 28 * s
        if battery:
            t = c.create_text(x, y, anchor="w", text=track("Battery"), fill=DIM, font=F.ui(11 * s, True))
            x = c.bbox(t)[2] + 8 * s
            low = min(battery) <= 15
            t = c.create_text(x, y, anchor="w", text=f"{battery[0]} · {battery[1]} %", fill=WARN if low else FG,
                              font=F.ui(13 * s))
            x = c.bbox(t)[2] + 28 * s
        if error:
            c.create_text(x, y, anchor="w", text=error, fill=BAD, font=F.ui(12 * s))
        # buttons (right to left)
        xr = W - 2
        a = self.actions
        if state in ("recording", "paused"):
            xr = canvas_button(c, F, xr, y, "Stop", "", BTN_NEUTRAL, "b_stop", a["stop"], s,
                               text_colour="#ff8a8d") - 10 * s
        if state == "ready":
            xr = canvas_button(c, F, xr, y, "Start", "Space", BTN_PRIMARY, "b_main", a["start"], s) - 22 * s
        elif state == "recording":
            xr = canvas_button(c, F, xr, y, "Pause", "Space", BTN_NEUTRAL, "b_main", a["pause"], s) - 22 * s
        elif state == "paused":
            xr = canvas_button(c, F, xr, y, "Resume", "Space", BTN_GO, "b_main", a["resume"], s) - 22 * s
        # recording state
        f = F.num(18 * s)
        if state == "recording":
            c.create_text(xr, y, anchor="e", text=clock, fill=FG, font=f)
            xr -= F.width(f, clock) + 12 * s
            c.create_text(xr, y, anchor="e", text=track("Rec"), fill=BAD, font=F.ui(12 * s, True))
            xr -= F.width(F.ui(12 * s, True), track("Rec")) + 10 * s
            c.create_oval(xr - 10 * s, y - 5 * s, xr, y + 5 * s, fill=BAD if blink else blend(BAD, BG, 0.6), outline="")
        elif state == "paused":
            c.create_text(xr, y, anchor="e", text=clock, fill=DIM, font=f)
            xr -= F.width(f, clock) + 12 * s
            c.create_text(xr, y, anchor="e", text=track("Paused"), fill=WARN, font=F.ui(12 * s, True))
        elif state == "ready":
            c.create_text(xr, y, anchor="e", text="start pedalling or press Space", fill=DIM, font=F.ui(13 * s))
        elif state == "finished":
            c.create_text(xr, y, anchor="e", text=clock, fill=DIM, font=f)
            xr -= F.width(f, clock) + 12 * s
            c.create_text(xr, y, anchor="e", text=track("Saved"), fill=GOOD, font=F.ui(12 * s, True))


class Dialog:
    """An in-window dialog card (instead of a system message box): a title, lines or a grid of
    stats, and buttons. Enter presses the rightmost (default) button."""

    def __init__(self, root, F, dpi: float) -> None:
        self.root, self.F, self.dpi = root, F, dpi
        self.c = None
        self.buttons: list = []

    @property
    def open(self) -> bool:
        return self.c is not None

    def close(self) -> None:
        if self.c is not None:
            self.c.destroy()
            self.c = None
            self.root.unbind("<Return>")
            self.root.unbind("<KP_Enter>")

    def show(self, title: str, subtitle: str, buttons: list, stats=None, width: float = 560) -> None:
        """buttons: [(text, colour, action)] left to right; the rightmost is the default."""
        import tkinter as tk
        self.close()
        F, s = self.F, self.dpi
        rows = 0 if not stats else (len(stats) + 3) // 4
        W, H = int(width * s), int((170 + rows * 74) * s)
        c = self.c = tk.Canvas(self.root, bg=BG, highlightthickness=0, width=W, height=H)
        c.place(relx=0.5, rely=0.45, anchor="center")
        tk.Misc.lift(c)                           # the widget (Canvas.lift would raise items)
        card = _panel_image(W, H, 16 * s)
        if card is not None:
            c._card = _photo(("dialog", W, H), card)
            c.create_image(0, 0, image=c._card, anchor="nw")
        else:
            rrect(c, 0.5, 0.5, W - 0.5, H - 0.5, 16 * s, fill=PANEL, outline=BORDER)
        pad = 28 * s
        c.create_text(pad, pad, anchor="nw", text=title, fill=FG, font=F.ui(20 * s, True))
        c.create_text(pad, pad + 36 * s, anchor="nw", text=subtitle, fill=DIM, font=F.ui(13 * s), width=W - 2 * pad)
        y = pad + 80 * s
        if stats:
            cw = (W - 2 * pad) / 4
            for i, (label, value) in enumerate(stats):
                x, yy = pad + (i % 4) * cw, y + (i // 4) * 74 * s
                c.create_text(x, yy, anchor="nw", text=track(label), fill=DIM, font=F.ui(10.5 * s, True))
                c.create_text(x, yy + 20 * s, anchor="nw", text=value, fill=FG, font=F.num(24 * s))
        xr = W - pad
        by = H - pad - 19 * s
        for i, (text, colour, action) in reversed(list(enumerate(buttons))):
            tc = "#ff8a8d" if colour == BTN_DANGER + "_text" else "#ffffff"
            col = BTN_NEUTRAL if colour.endswith("_text") else colour
            xr = canvas_button(c, F, xr, by, text, "", col, f"d{i}", action, s, bg=PANEL, text_colour=tc) - 10 * s
        default = buttons[-1][2]
        self.root.bind("<Return>", lambda e: default())
        self.root.bind("<KP_Enter>", lambda e: default())


# --------------------------------------------------------------------------- #
def run_gui(session, worker, q: queue.Queue, utcnow, ftp=None, plan_min: float = 60) -> None:
    import tkinter as tk
    import tkinter.font as tkfont

    # Sharp text on Windows: without this, Windows stretches the whole window
    # on scaled displays (125 %, 150 %), which makes it look blurry.
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
            ctypes.windll.winmm.timeBeginPeriod(1)    # 1 ms timers for even frame pacing (Windows resets it at exit)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass

    # Tk objects (images, fonts) must only be finalised on this thread. Python's cyclic garbage
    # collector runs in whichever thread happens to allocate, which includes the Bluetooth
    # thread, so it runs here instead (see slow_tick). Suspected cause of a Tcl
    # "alloc: invalid block" crash with both sensors going silent.
    import gc
    gc.disable()
    root = tk.Tk()
    root.title("Assioma Recorder")
    root.configure(bg=BG)
    dpi = max(1.0, root.winfo_fpixels("1i") / 96)
    root.geometry(f"{min(int(1360 * dpi), root.winfo_screenwidth() - 40)}x"
                  f"{min(int(840 * dpi), root.winfo_screenheight() - 80)}")
    root.minsize(int(900 * dpi), int(560 * dpi))
    F = Fonts(tkfont.families())

    mk = lambda parent: tk.Canvas(parent, bg=BG, highlightthickness=0, width=1, height=1)
    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=19)
    root.rowconfigure(1, weight=81)
    m, g = int(20 * dpi), int(14 * dpi)             # outer margin, gap between panels
    top = mk(root)
    top.grid(row=0, column=0, sticky="nsew", padx=m, pady=(m, g // 2))
    body = tk.Frame(root, bg=BG)
    body.grid(row=1, column=0, sticky="nsew", padx=m, pady=g // 2)
    body.rowconfigure(0, weight=1)
    body.columnconfigure(0, weight=33, uniform="b")
    body.columnconfigure(1, weight=67, uniform="b")
    dyn = mk(body)
    dyn.grid(row=0, column=0, sticky="nsew", padx=(0, g // 2))
    right = tk.Frame(body, bg=BG)
    right.grid(row=0, column=1, sticky="nsew", padx=(g // 2, 0))
    right.columnconfigure(0, weight=1)
    right.rowconfigure(0, weight=73)
    right.rowconfigure(1, weight=27)
    gr, ov = mk(right), mk(right)
    gr.grid(row=0, column=0, sticky="nsew", pady=(0, g // 2))
    ov.grid(row=1, column=0, sticky="nsew", pady=(g // 2, 0))
    strip = BeatStrip(top)
    tween = Tween()
    plots = LivePlots(gr, F, ftp)

    barc = tk.Canvas(root, bg=BG, highlightthickness=0, height=int(52 * dpi))
    barc.grid(row=2, column=0, sticky="ew", padx=m, pady=(g // 2, m - 4))
    dialog = Dialog(root, F, dpi)

    state = {"finished": False, "strip": None, "warn": False, "dirty": True, "live_part": 0,
             "plot": 1, "last_live": 0.0, "last_slow": 0.0, "last_overview": 0.0}
    fullscreen = [False]

    def toggle_fs(_e=None):
        fullscreen[0] = not fullscreen[0]
        root.attributes("-fullscreen", fullscreen[0])

    root.bind("<F11>", toggle_fs)
    root.bind("<Escape>", lambda e: fullscreen[0] and toggle_fs())

    def hrv_now(now) -> dict:
        h: dict = {}
        beats = session.hrv.beats
        if beats and (now - beats[-1].time).total_seconds() < 5:
            h["rr"] = beats[-1].rr
        row = session.hrv.latest(now)
        if row and row[1] is not None:
            h.update(alpha1=row[1], rmssd=row[2], art=row[3], cov=session.hrv.coverage[-1])
        elif beats:
            h["note"] = "needs 2 min of beats"
        return h

    def strap_state(now):
        if session.hr_last is None:
            return None
        quiet = (now - session.hr_last).total_seconds()
        beats = session.hrv.beats
        no_beat = (now - beats[-1].time).total_seconds() if beats else quiet
        warn = f"no data {quiet:.0f} s" if quiet > 3 else f"no beats {no_beat:.0f} s" if no_beat > 4 else ""
        return {"warn": warn}

    def redraw_top():
        now = utcnow()
        strap_ = strap_state(now)
        state["warn"] = bool(strap_ and strap_["warn"])
        v = dict(session.live(now))
        v["power_target"] = v.get("power_w")             # the zone follows the real value, not the roll
        for k in ("power_w", "cadence_rpm", "heart_rate_bpm"):
            v[k] = tween.value(k, v.get(k))
        ride = {"state": session.state, "elapsed": ""}
        if session.started is not None:
            el = int((now - session.started).total_seconds())
            ride["elapsed"] = f"elapsed {el // 3600}:{el // 60 % 60:02d}:{el % 60:02d}"
        state["strip"] = draw_top(top, F, v, session.stats(), hrv_now(now),
                                  len(session.seconds), ride, strap_, ftp)

    def redraw_dyn(part=None):
        draw_dynamics(dyn, F, session.live(utcnow()), session.stats(), part)

    def redraw_live():
        redraw_top()
        redraw_dyn()

    def redraw_overview():
        draw_overview(ov, F, session, utcnow(), plan_min * 60)

    def animate_strip():
        strip.frame(state["strip"], session.hrv.beats, utcnow(), state["warn"])

    for cv, fn in ((top, redraw_live), (dyn, redraw_live), (ov, redraw_overview)):
        cv.bind("<Configure>", lambda e, fn=fn: fn())

    def guarded(fn):
        try:
            fn()
        except Exception as e:      # never let a display glitch stop the recording
            state["error"] = f"display error: {e!r}"[:120]

    def drain():
        """Take in the notifications (cheap, every frame, so numbers and beats are instant)."""
        try:
            while True:
                session.add(q.get_nowait())
                state["dirty"] = True
        except queue.Empty:
            pass

    def slow_tick():
        """Per-second rows, alpha 1 (every 5 s) and the status line: 4 Hz is plenty."""
        session.tick(utcnow())
        t = time.monotonic()                     # garbage collection, on this thread only:
        if t - state.get("last_gc", 0) > 10:     # young objects every 10 s (a few ms),
            import gc                            # everything every 5 min
            full = t - state.get("last_full_gc", 0) > 300
            state["last_gc"] = t
            if full:
                state["last_full_gc"] = t
            gc.collect(2 if full else 1)
        if worker.pedal_name:
            session.pedal_name = worker.pedal_name
        now = utcnow()
        # auto-start: pedalling for 3 s while ready starts the ride
        if session.state == "ready" and not dialog.open:
            if (session.live(now).get("power_w") or 0) > 0:
                state["pedal_since"] = state.get("pedal_since") or now
                if (now - state["pedal_since"]).total_seconds() >= 3:
                    act_start()
            else:
                state["pedal_since"] = None
        draw_bar()

    def ride_clock() -> str:
        n = len(session.seconds)
        return f"{n // 3600}:{n // 60 % 60:02d}:{n % 60:02d}"

    def draw_bar():
        def sensor(label, status):
            ok = status.startswith("connected")
            return label, status.split(": ", 1)[1] if ok and ": " in status else status, ok
        sensors = [sensor("Pedals", worker.status["pedal"])]
        if worker.status.get("hr"):
            sensors.append(sensor("Strap", worker.status["hr"]))
        bar.draw(session.state, ride_clock(), sensors, session.battery, state.get("error", ""))

    # ---- start / pause / resume / stop
    def act_start():
        if session.state == "ready":
            session.start(utcnow())
            draw_bar()

    def act_pause():
        session.pause(utcnow())
        draw_bar()

    def act_resume():
        dialog.close()
        session.resume(utcnow())
        draw_bar()

    def act_toggle(_e=None):
        if dialog.open or state["finished"]:
            return
        {"ready": act_start, "recording": act_pause, "paused": act_resume}.get(session.state, lambda: None)()

    def act_stop():
        if state["finished"]:
            return
        if session.state == "ready":             # nothing recorded: just close
            finish_and_close(save=False)
            return
        session.pause(utcnow())                  # the clock stops while you decide
        draw_bar()
        dialog.show("Finish ride?", f"{ride_clock()} recorded. The ride is paused while you decide.",
                    [("Discard", BTN_DANGER + "_text", confirm_discard), ("Resume", BTN_NEUTRAL, act_resume),
                     ("Save ride", BTN_PRIMARY, save_ride)])

    def confirm_discard():
        dialog.show("Discard this ride?", f"The {ride_clock()} recording and its files will be deleted. "
                    "This can't be undone.",
                    [("Discard", BTN_DANGER, lambda: finish_and_close(save=False, discard=True)),
                     ("Back", BTN_NEUTRAL, act_stop)])

    def finish_and_close(save: bool, discard: bool = False):
        state["finished"] = True
        dialog.close()
        worker.stop()
        drain_rest()
        if discard:
            session.discard()
        else:
            session.finish()                     # ready (never started): removes the empty files
        root.destroy()

    def drain_rest():
        for _ in range(200):                     # take in anything still queued
            try:
                session.add(q.get_nowait())
            except queue.Empty:
                break

    def save_ride():
        state["finished"] = True
        dialog.close()
        worker.stop()
        drain_rest()
        st_ = session.stats()
        result = session.finish()
        if not result:
            root.destroy()
            return
        path, s = result
        draw_bar()                               # "saved", no more ride buttons
        redraw_top()
        secs = session.seconds
        n = len(secs)
        el = int((secs[-1]["time"] - secs[0]["time"]).total_seconds()) + 1
        hms = lambda v: f"{v // 3600}:{v // 60 % 60:02d}:{v % 60:02d}"
        a1 = [r["dfa_a1"] for r in secs if r.get("dfa_a1") is not None]
        bal = s["balance_right"]
        stats = [("Ride time", hms(n)), ("Elapsed", hms(el)),
                 ("Avg power", f"{fmt(s['avg_power'])} W"), ("NP", f"{fmt(s['np'])} W"),
                 ("Avg HR", f"{fmt(s['avg_hr'])} bpm"),
                 ("Balance", "--" if bal is None else f"{100 - bal:.0f} · {bal:.0f}"),
                 ("Pw:HR", "--" if st_["decoupling"] is None else f"{st_['decoupling']:+.1f} %"),
                 ("DFA α1 avg", "--" if not a1 else f"{sum(a1) / len(a1):.2f}")]

        def open_folder():
            import subprocess
            if sys.platform == "win32":
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        dialog.show("Ride saved", os.path.basename(path) + "  ·  upload it to intervals.icu or Garmin Connect",
                    [("Show file", BTN_NEUTRAL, open_folder), ("Close", BTN_PRIMARY, root.destroy)],
                    stats=stats, width=640)

    def one_job():
        """At most one piece of heavier work per frame (each a few ms), so the frame rate holds."""
        t = time.monotonic()
        if t - state["last_slow"] >= 0.25:
            state["last_slow"] = t
            state["dirty"] = True
            slow_tick()
        elif state["dirty"] and t - state["last_live"] >= 0.1:
            k = state["live_part"]                       # tiles, then the three dynamics parts, one per
            if k == 0:                                   # frame; each skips itself if nothing shown changed
                redraw_top()
            else:
                redraw_dyn(k)
            if k == 3:
                state["last_live"], state["dirty"] = t, False
            state["live_part"] = (k + 1) % 4
        elif t - state["last_overview"] >= 3:            # the overview gains ~1 px per 4 s
            state["last_overview"] = t
            redraw_overview()

    def live_plots():
        """The scrolling graphs, one plot per frame: each scrolls a pixel when due and appends
        whatever arrived; finished line pieces are never touched again."""
        plots.update(session, utcnow(), state["plot"])
        state["plot"] = 3 - state["plot"]

    def animate():
        """The frame loop, 60 fps against a deadline so frames stay evenly spaced."""
        if state["finished"]:
            return
        guarded(drain)
        if tween.active():                                # numbers rolling to a new value
            guarded(redraw_top)
        guarded(animate_strip)
        guarded(live_plots)
        guarded(one_job)
        state["next_frame"] = max(state["next_frame"] + 1 / STRIP_FPS, time.monotonic())
        root.after(max(1, int((state["next_frame"] - time.monotonic()) * 1000)), animate)

    bar = ControlBar(barc, F, {"start": act_start, "pause": act_pause, "resume": act_resume, "stop": act_stop})
    barc.bind("<Configure>", lambda e: draw_bar())
    root.bind("<space>", act_toggle)
    root.protocol("WM_DELETE_WINDOW", act_stop)
    state["next_frame"] = time.monotonic()
    root.after(16, animate)
    root.mainloop()
