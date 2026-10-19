#!/usr/bin/env python3
"""
Bike fit from a phone video: your joint angles through the pedal stroke, against the usual ranges.

    python bikefit.py side.mp4                      side view (film the side facing the camera)
    python bikefit.py front.mp4 --view front        front view: knee tracking, left vs right
    python bikefit.py side.mp4 --compare old/report.json     before / after a change
    options: --crank 172.5 (mm, sets the scale)  --seconds 25  --quality fast|balanced|best

Out (next to the video, <name>_fit/): report.md (numbers, ranges, a verdict), report.json, video.mp4
with the angles drawn on, and stills at the top, 3 o'clock and bottom of a typical stroke.

How it finds you: a pose model (RTMPose, halpe26 keypoints incl. heel and big toe; runs on the CPU)
in every frame. With --markers color (or white), tape on the bony points (hip: the bump on the side of the hip,
knee: outside of the knee joint, ankle: the bump on the outside of the ankle), each one seen near the
model's joint replaces it: tape is a few degrees more exact. Everything is averaged over every pedal
stroke, so one odd frame doesn't move the result.

Scale: the ankle goes round a circle about the size of the crank, so its radius (side view) or its
up-and-down travel (front view, 2 x crank) turns pixels into millimetres. Set --crank to yours.

Geometry (the pure functions at the top) is tested against a simulated rider (tests/test_bikefit.py).
Needs: pip install -r requirements-bikefit.txt (numpy, opencv-python-headless, onnxruntime, rtmlib).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from typing import Optional

import numpy as np

# halpe26 keypoints: (left, right)
KP = {"shoulder": (5, 6), "elbow": (7, 8), "wrist": (9, 10), "hip": (11, 12), "knee": (13, 14),
      "ankle": (15, 16), "toe": (20, 21), "heel": (24, 25)}
JOINTS = list(KP)

# the usual ranges (dynamic, from video; fitting guidelines, not laws)
RANGES = {
    "knee_flex_bottom": (30, 40, "knee bend at the bottom of the stroke", "°"),
    "knee_flex_top": (None, 115, "knee bend at the top", "°"),
    "hip_angle_min": (45, None, "hip angle at its most closed (shoulder-hip-knee)", "°"),
    "ankle_bottom": (90, 115, "ankle angle at the bottom (shin to foot)", "°"),
    "hip_rock_mm": (None, 20, "hip going up and down per stroke", "mm"),
    "torso_deg": (30, 60, "back angle to horizontal", "°"),
    "elbow_deg": (145, 170, "elbow angle", "°"),
    # from the knee joint's centre; the classic "knee over pedal" uses the front of the knee, 3-5 cm
    # further forward, so neutral reads about 1-7 cm behind
    "kops_mm": (-70, -10, "knee joint centre ahead (+) / behind (-) the pedal spindle at 3 o'clock (rough)", "mm"),
}
DEG_PER_CM = 4.0          # about 4° of knee bend per cm of saddle height (rule of thumb)


# --------------------------------------------------------------------------- geometry (pure)
def angle_at(a, b, c) -> np.ndarray:
    """Inner angle at b (degrees) of a-b-c, for arrays of points [..., 2]."""
    v1, v2 = np.asarray(a, float) - b, np.asarray(c, float) - b
    cross = v1[..., 0] * v2[..., 1] - v1[..., 1] * v2[..., 0]
    return np.degrees(np.arctan2(np.abs(cross), (v1 * v2).sum(-1)))


def fit_circle(pts: np.ndarray) -> tuple:
    """Least-squares circle (Kasa): (cx, cy, r)."""
    x, y = pts[:, 0], pts[:, 1]
    A = np.c_[2 * x, 2 * y, np.ones(len(x))]
    cx, cy, c = np.linalg.lstsq(A, x * x + y * y, rcond=None)[0]
    return cx, cy, math.sqrt(c + cx * cx + cy * cy)


def crank_angle(ankle: np.ndarray, centre: tuple, forward: int) -> np.ndarray:
    """Crank angle of each ankle point, unwrapped: 0 = top, 90 = forward (3 o'clock), 180 = bottom.
    Image y points down; forward = +1 if the rider faces +x."""
    dx = forward * (ankle[:, 0] - centre[0])
    up = -(ankle[:, 1] - centre[1])
    return np.degrees(np.unwrap(np.arctan2(dx, up)))


def smooth(a: np.ndarray, w: int) -> np.ndarray:
    """Centred moving average along axis 0 (edges padded), w odd."""
    if w <= 1:
        return a
    pad = w // 2
    p = np.concatenate([np.repeat(a[:1], pad, 0), a, np.repeat(a[-1:], pad, 0)])
    k = np.ones(w) / w
    return np.stack([np.convolve(p[:, i], k, "valid") for i in range(a.shape[1])], 1) if a.ndim == 2 \
        else np.convolve(p, k, "valid")


def fill_gaps(xy: np.ndarray, ok: np.ndarray) -> np.ndarray:
    """Linear interpolation over frames where ok is False."""
    out = xy.copy()
    idx = np.arange(len(xy))
    if ok.sum() < 2:
        return out
    for d in range(2):
        out[~ok, d] = np.interp(idx[~ok], idx[ok], xy[ok, d])
    return out


def steady_span(hip: np.ndarray, tol: float) -> tuple:
    """The longest stretch (start, end) where the hip stays inside a tol x tol box: on a trainer the
    hip barely moves, so a moving hip means the camera moved (handheld, zoom)."""
    best, i, n = (0, 0), 0, len(hip)
    for j in range(n):
        while i < j and (np.ptp(hip[i:j + 1, 0]) > tol or np.ptp(hip[i:j + 1, 1]) > tol):
            i += 1
        if j + 1 - i > best[1] - best[0]:
            best = (i, j + 1)
    return best


def at_phase(theta: np.ndarray, values: np.ndarray, phase: float) -> list:
    """values where the crank passes phase (degrees, within each turn), linearly interpolated."""
    out = []
    t = np.asarray(theta)
    for k in range(int(math.floor(t.min() / 360)), int(math.ceil(t.max() / 360)) + 1):
        target = k * 360 + phase
        i = np.where((t[:-1] <= target) & (t[1:] > target))[0]
        for j in i[:1]:
            f = (target - t[j]) / (t[j + 1] - t[j])
            out.append(values[j] + f * (values[j + 1] - values[j]))
    return out


def per_turn(theta: np.ndarray, values: np.ndarray, fn) -> list:
    """fn(values) over each whole crank turn (top to top)."""
    out = []
    turn = np.floor(theta / 360)
    for k in np.unique(turn)[1:-1]:                   # whole turns only
        m = turn == k
        if m.sum() >= 8:
            out.append(fn(values[m]))
    return out


def side_metrics(P: dict, crank_mm: float) -> dict:
    """The side-view numbers from smoothed joint tracks P[joint] = [n, 2] (pixels), near-side leg."""
    hip, knee, ankle = P["hip"], P["knee"], P["ankle"]
    if "toe" in P and "heel" in P:                    # the foot points the way you ride
        forward = 1 if np.median(P["toe"][:, 0] - P["heel"][:, 0]) > 0 else -1
    else:
        forward = 1 if np.median(P["shoulder"][:, 0] - hip[:, 0]) > 0 else -1
    foot = "toe" in P and "heel" in P
    path = P["heel"] + 0.7 * (P["toe"] - P["heel"]) if foot else ankle    # the pedal spindle, about
    cx, cy, r = fit_circle(path)
    mm = crank_mm / r                                 # mm per pixel: the spindle goes round the crank's circle
    theta = crank_angle(path, (cx, cy), forward)
    if theta[-1] < theta[0]:                          # the foot goes forward after the top: wrong way round
        forward = -forward
        theta = crank_angle(path, (cx, cy), forward)
    knee_ang = angle_at(hip, knee, ankle)
    flex = 180 - knee_ang
    hip_ang = angle_at(P["shoulder"], hip, knee)
    upper = P.get("_upper_ok", True)                  # shoulders in the picture
    out: dict = {"turns": len(per_turn(theta, flex, np.max)), "mm_per_px": mm, "crank_radius_px": r,
                 "facing": "right" if forward > 0 else "left"}

    def summary(vals):
        v = np.asarray([x for x in vals if x is not None and np.isfinite(x)], float)
        return None if len(v) == 0 else {"mean": float(v.mean()), "sd": float(v.std()), "n": int(len(v))}

    out["knee_flex_bottom"] = summary(per_turn(theta, flex, np.min))     # most straight in a turn
    out["knee_flex_top"] = summary(per_turn(theta, flex, np.max))
    out["knee_flex_at_bdc"] = summary(at_phase(theta, flex, 180))
    if upper:
        out["hip_angle_min"] = summary(per_turn(theta, hip_ang, np.min))
    if "toe" in P and "heel" in P:
        foot_dir = P["toe"] - P["heel"]
        ankle_ang = angle_at(knee, ankle, ankle + foot_dir)
        out["ankle_bottom"] = summary(at_phase(theta, ankle_ang, 180))
        out["ankle_range"] = summary(per_turn(theta, ankle_ang, np.ptp))
        spindle = P["heel"] + 0.7 * foot_dir          # under the ball of the foot, about
        kops = forward * (knee[:, 0] - spindle[:, 0]) * mm
        out["kops_mm"] = summary(at_phase(theta, kops, 90))
    out["hip_rock_mm"] = summary(per_turn(theta, hip[:, 1] * mm, np.ptp))
    sh = P["shoulder"] - hip
    if not upper:
        out["_theta"] = theta
        return out
    out["torso_deg"] = {**summary(np.degrees(np.arctan2(-sh[:, 1], np.abs(sh[:, 0])))), "n": None}   # every frame
    if "elbow" in P and "wrist" in P:
        out["elbow_deg"] = {**summary(angle_at(P["shoulder"], P["elbow"], P["wrist"])), "n": None}
    out["_theta"] = theta
    return out


def front_metrics(L: dict, R: dict, crank_mm: float) -> dict:
    """Front view, both legs: knee sideways travel per turn and its drift from the hip-ankle line."""
    out = {}
    for name, P in (("left", L), ("right", R)):
        ank = P["ankle"]
        travel = np.ptp(smooth(ank[:, 1], 5))         # up-down travel of the ankle ~ 2 x crank
        mm = 2 * crank_mm / max(travel, 1e-6)
        # crank phase from the ankle's height: a turn = one bottom to the next
        y = ank[:, 1] - ank[:, 1].mean()
        bottoms = [i for i in range(1, len(y) - 1) if y[i] >= y[i - 1] and y[i] > y[i + 1] and y[i] > 0.6 * y.max()]
        hip, knee = P["hip"], P["knee"]
        # sideways distance of the knee from the hip-ankle line (+ = outward from the bike's centre later)
        d = hip - ank
        t = ((knee - ank) * d).sum(1) / ((d * d).sum(1) + 1e-9)
        line = ank + t[:, None] * d
        dev = (knee[:, 0] - line[:, 0]) * mm
        travels = [float(np.ptp(knee[a:b, 0]) * mm) for a, b in zip(bottoms, bottoms[1:]) if b - a > 5]
        out[name] = {"knee_side_travel_mm": None if not travels else {"mean": float(np.mean(travels)),
                                                                      "sd": float(np.std(travels)), "n": len(travels)},
                     "knee_off_line_mm": {"mean": float(dev.mean()), "sd": float(dev.std()), "n": int(len(dev))},
                     "mm_per_px": mm}
    return out


def verdict_side(m: dict) -> list:
    """Plain-language findings from the side-view numbers."""
    out = []
    kb = m.get("knee_flex_bottom")
    if kb:
        v = kb["mean"]
        if v < 30:
            out.append(f"Saddle probably high: the knee straightens to {v:.0f}° of bend (usual 30-40°). "
                       f"Lowering it by about {(35 - v) / DEG_PER_CM:.1f} cm would bring it to the middle.")
        elif v > 40:
            out.append(f"Saddle probably low: the knee stays bent {v:.0f}° at the bottom (usual 30-40°). "
                       f"Raising it by about {(v - 35) / DEG_PER_CM:.1f} cm would bring it to the middle. "
                       "Go in steps of 3-5 mm a few rides apart.")
        else:
            out.append(f"Saddle height looks right: {v:.0f}° of knee bend at the bottom (usual 30-40°).")
    a = m.get("ankle_bottom")
    if a and kb and a["mean"] > 115 and kb["mean"] < 35:
        out.append("Toes point well down at the bottom: often a sign of reaching for the pedal (saddle a bit high).")
    hr = m.get("hip_rock_mm")
    if hr and hr["mean"] > 20:
        out.append(f"Hips move {hr['mean']:.0f} mm up and down per stroke: a sign of reaching (saddle high) "
                   "or a hard seat.")
    k = m.get("kops_mm")
    if k:
        v = k["mean"]
        where = "ahead of" if v > 0 else "behind"
        txt = f"At 3 o'clock the centre of the knee is {abs(v):.0f} mm {where} the pedal spindle"
        if v > -10:
            txt += (": the knee is well forward, more load on the front of the knee. Moving the saddle back "
                    "(5-10 mm at a time, re-checking the height after) usually helps.")
        elif v < -70:
            txt += ": the saddle is well back. Fine for some riders; it shifts work to the hips and hamstrings."
        else:
            txt += (", so the front of the knee is roughly over the pedal: a neutral fore-aft position "
                    "(a starting point, not a rule).")
        out.append(txt)
    h = m.get("hip_angle_min")
    if h and h["mean"] < 45:
        out.append(f"The hip closes to {h['mean']:.0f}° at the top: tight. A higher bar or shorter cranks open it.")
    e = m.get("elbow_deg")
    if e and e["mean"] > 170:
        out.append("Arms nearly locked: the reach is long or the bar low (a common cause of a sore back and neck).")
    t = m.get("torso_deg")
    if t:
        out.append(f"Back angle {t['mean']:.0f}° to horizontal "
                   + ("(upright)." if t["mean"] > 55 else "(aggressive: fine if the back is happy)." if t["mean"] < 32
                      else "(a normal road position)."))
    return out


def verdict_front(m: dict) -> list:
    out = []
    for side in ("left", "right"):
        s = m[side]
        tr = s.get("knee_side_travel_mm")
        if tr:
            msg = f"{side.capitalize()} knee moves {tr['mean']:.0f} mm sideways per stroke"
            msg += " (steady)." if tr["mean"] <= 25 else " (a lot: check cleat angle and saddle height)."
            out.append(msg)
    tl, tr = (m[s].get("knee_side_travel_mm") for s in ("left", "right"))
    if tl and tr and abs(tl["mean"] - tr["mean"]) > 10:
        out.append("The two knees track differently: often cleat position or a leg-length difference.")
    return out


# --------------------------------------------------------------------------- video and pose
def detect_markers(frame_bgr, near: np.ndarray, radius: float, kind: str = "color") -> Optional[np.ndarray]:
    """A small blob of tape near a joint: bright and strongly coloured (kind "color") or bright white
    ("white"), or None."""
    import cv2
    x, y = int(near[0]), int(near[1])
    r = int(max(6, radius))
    h, w = frame_bgr.shape[:2]
    x0, y0, x1, y1 = max(0, x - r), max(0, y - r), min(w, x + r), min(h, y + r)
    if x1 - x0 < 4 or y1 - y0 < 4:
        return None
    hsv = cv2.cvtColor(frame_bgr[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
    if kind == "white":
        mask = ((hsv[..., 1] < 60) & (hsv[..., 2] > 200)).astype(np.uint8)
    else:
        mask = ((hsv[..., 1] > 150) & (hsv[..., 2] > 140)).astype(np.uint8)
    n, lab, stats, cent = cv2.connectedComponentsWithStats(mask)
    best, bd = None, None
    for i in range(1, n):
        area = stats[i, cv2.CC_STAT_AREA]
        if not (12 <= area <= (0.5 * r) ** 2 * 3.2):        # tape-sized: not a shirt
            continue
        cxy = cent[i] + (x0, y0)
        d = np.hypot(*(cxy - near))
        if d < r and (bd is None or d < bd):
            best, bd = cxy, d
    return best


def read_frames(video: str, seconds: float, start: float = 0.0):
    """(fps, frames as BGR arrays, at most 1280 px). ffmpeg when it's there (every phone format, HEVC
    included, and the phone's rotation), else OpenCV."""
    import shutil
    import subprocess
    import cv2
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if ffmpeg and ffprobe:
        info = json.loads(subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                                          "stream=r_frame_rate,avg_frame_rate", "-of", "json", video],
                                         capture_output=True, text=True).stdout or "{}")
        st = (info.get("streams") or [{}])[0]
        num, _, den = (st.get("avg_frame_rate") or st.get("r_frame_rate") or "30/1").partition("/")
        fps = float(num) / float(den or 1) if float(den or 1) and float(num) else 30.0
        vf = "scale=w=1280:h=1280:force_original_aspect_ratio=decrease,scale=trunc(iw/2)*2:trunc(ih/2)*2"
        first = subprocess.run([ffmpeg, "-v", "error", "-i", video, "-vf", vf, "-frames:v", "1", "-f", "image2pipe",
                                "-vcodec", "png", "-"], capture_output=True).stdout
        img = cv2.imdecode(np.frombuffer(first, np.uint8), cv2.IMREAD_COLOR) if first else None
        if img is not None:
            h, w = img.shape[:2]
            proc = subprocess.Popen([ffmpeg, "-v", "error", "-ss", str(start), "-i", video, "-t", str(seconds), "-vf", vf,
                                     "-f", "rawvideo", "-pix_fmt", "bgr24", "-"], stdout=subprocess.PIPE)
            frames = []
            while True:
                buf = proc.stdout.read(w * h * 3)
                if len(buf) < w * h * 3:
                    break
                frames.append(np.frombuffer(buf, np.uint8).reshape(h, w, 3).copy())
            proc.wait()
            if frames:
                return fps, frames
    cap = cv2.VideoCapture(video)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    cap.set(cv2.CAP_PROP_POS_MSEC, start * 1000)
    frames = []
    while len(frames) < seconds * fps:
        ok, f = cap.read()
        if not ok:
            break
        if max(f.shape[:2]) > 1280:                    # phones film 4K: 720p-1280 is plenty
            s = 1280 / max(f.shape[:2])
            f = cv2.resize(f, (int(f.shape[1] * s), int(f.shape[0] * s)))
        frames.append(f)
    cap.release()
    if not frames:
        raise SystemExit(f"can't read {video} (install ffmpeg for phone videos)")
    return fps, frames


def track(video: str, seconds: float, quality: str, log=print, start: float = 0.0) -> dict:
    """Keypoints for every frame: {fps, size, kp [n,26,2], score [n,26], markers {joint: [n,2] or nan}}."""
    import cv2
    from rtmlib import RTMPose, YOLOX
    modes = {"fast": ("yolox_tiny_8xb8-300e_humanart-6f3252f9", (416, 416),
                      "rtmpose-s_simcc-body7_pt-body7-halpe26_700e-256x192-7f134165_20230605", (192, 256)),
             "balanced": ("yolox_m_8xb8-300e_humanart-c2c7a14a", (640, 640),
                          "rtmpose-m_simcc-body7_pt-body7-halpe26_700e-256x192-4d3e73dd_20230605", (192, 256)),
             "best": ("yolox_x_8xb8-300e_humanart-a39d44ed", (640, 640),
                      "rtmpose-x_simcc-body7_pt-body7-halpe26_700e-384x288-7fb6e239_20230606", (288, 384))}
    det_name, det_size, pose_name, pose_size = modes[quality]
    base = "https://download.openmmlab.com/mmpose/v1/projects/rtmposev1/onnx_sdk/"
    det = YOLOX(base + det_name + ".zip", model_input_size=det_size, backend="onnxruntime", device="cpu")
    pose = RTMPose(base + pose_name + ".zip", model_input_size=pose_size, backend="onnxruntime", device="cpu")
    fps, raw = read_frames(video, seconds, start)
    log(f"  {len(raw)} frames at {fps:.0f} fps, {raw[0].shape[1]}x{raw[0].shape[0]}")
    frames, kps, scs, box = [], [], [], None
    for i, f in enumerate(raw):
        if box is None or i % int(fps * 2) == 0:      # the rider stays put: find them every 2 s
            b = det(f)
            if len(b):
                areas = (b[:, 2] - b[:, 0]) * (b[:, 3] - b[:, 1])
                bb = b[int(np.argmax(areas))]
                w, h = bb[2] - bb[0], bb[3] - bb[1]
                box = np.array([bb[0] - 0.08 * w, bb[1] - 0.08 * h, bb[2] + 0.08 * w, bb[3] + 0.08 * h])
        if box is None:
            continue
        k, s = pose(f, bboxes=[box.tolist()])
        kps.append(k[0])
        scs.append(s[0])
        frames.append(f)
        if (i + 1) % 150 == 0:
            log(f"  {i + 1}/{len(raw)} frames")
    if len(kps) < fps * 3:
        raise SystemExit("couldn't find a rider in the video (is the whole body in the picture?)")
    kp, sc = np.array(kps, float), np.array(scs, float)
    return {"fps": fps, "frames": frames, "kp": kp, "score": sc}


def joint_tracks(t: dict, side: str, markers: str = "off") -> tuple:
    """Smoothed tracks of one side's joints, with tape markers where seen. -> (P, marker_share)."""
    j = 0 if side == "left" else 1
    w = max(3, int(round(t["fps"] / 12)) | 1)
    P, share = {}, {}
    thigh = np.median(np.hypot(*(t["kp"][:, KP["hip"][j]] - t["kp"][:, KP["knee"][j]]).T))
    for name in JOINTS:
        i = KP[name][j]
        xy, ok = t["kp"][:, i].copy(), t["score"][:, i] > 0.3
        # white tape near the ankle can't be told from white shoes and socks: hip and knee only
        if markers != "off" and name in (("hip", "knee") if markers == "white" else ("hip", "knee", "ankle")):
            found = [detect_markers(f, xy[n], 0.15 * thigh, markers) for n, f in enumerate(t["frames"])]
            hits = [n for n, m in enumerate(found) if m is not None]
            if hits:                                       # tape stays put on the joint: a logo or a shoe doesn't
                off = np.array([found[n] - xy[n] for n in hits])
                med = np.median(off, 0)
                spread = np.median(np.hypot(*(off - med).T)) * 1.4826
                hits = [n for n, o in zip(hits, off) if np.hypot(*(o - med)) <= max(3 * spread, 0.03 * thigh)
                        ] if spread < 0.08 * thigh else []
            share[name] = len(hits) / len(found)
            if share[name] >= 0.6:                         # tape seen most of the time: use it
                for n in hits:
                    xy[n], ok[n] = found[n], True
                ok[[n for n in range(len(found)) if n not in set(hits)]] = False
        P[name] = smooth(fill_gaps(xy, ok), w)
    sh = KP["shoulder"][j]
    P["_upper_ok"] = bool(np.mean(t["score"][:, sh] > 0.5) > 0.7 and np.median(t["kp"][:, sh, 1]) > 0)
    return P, share


def near_side(t: dict) -> str:
    legs = [KP[k] for k in ("hip", "knee", "ankle", "toe", "heel")]
    left = np.mean([t["score"][:, a].mean() for a, _ in legs])
    right = np.mean([t["score"][:, b].mean() for _, b in legs])
    return "left" if left > right else "right"


# --------------------------------------------------------------------------- report
def fmt(s, unit="°") -> str:
    if not s:
        return "--"
    n = s.get("n")
    return f"{s['mean']:.0f}{unit} (±{s['sd']:.0f}" + (f", {n} strokes)" if n else ")")


def status(key: str, s) -> str:
    if not s or key not in RANGES:
        return ""
    lo, hi, _, _ = RANGES[key]
    v = s["mean"]
    if lo is not None and v < lo:
        return "low"
    if hi is not None and v > hi:
        return "high"
    return "ok"


def write_report(out_dir: str, video: str, view: str, m: dict, verdict: list, extra: dict,
                 compare: Optional[dict]) -> str:
    lines = [f"# Bike fit: {os.path.basename(video)}", "", f"View: {view}. {extra.get('note', '')}", ""]
    if view == "side":
        lines += ["| Measure | You | Usual | |", "|---|---|---|---|"]
        for key, (lo, hi, label, unit) in RANGES.items():
            s = m.get(key)
            usual = f"{lo}–{hi}{unit}" if lo is not None and hi is not None else f"≥{lo}{unit}" if lo is not None \
                else f"≤{hi}{unit}"
            row = f"| {label} | {fmt(s, unit)} | {usual} | {status(key, s)} |"
            if compare and compare.get("metrics", {}).get(key) and s:
                row = row[:-1] + f"was {compare['metrics'][key]['mean']:.0f}{unit} |"
            lines.append(row)
        lines += ["", f"Knee bend exactly at the bottom of the crank: {fmt(m.get('knee_flex_at_bdc'))}.",
                  f"Scale: crank {extra['crank']} mm = {m['crank_radius_px']:.0f} px. Strokes measured: {m['turns']}."]
    else:
        lines += ["| Leg | Knee sideways travel | Knee off the hip-ankle line |", "|---|---|---|"]
        for side in ("left", "right"):
            s = m[side]
            lines.append(f"| {side} | {fmt(s['knee_side_travel_mm'], ' mm')} | {fmt(s['knee_off_line_mm'], ' mm')} |")
    lines += ["", "## What it says", ""] + [f"- {v}" for v in verdict]
    lines += ["", "Ranges are fitting guidelines for road riding, measured from video. Change one thing at a time, "
              "in small steps, and ride a few times before judging. Pain that stays: see a physio or a fitter."]
    path = os.path.join(out_dir, "report.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


def draw(out_dir: str, t: dict, P: dict, m: dict, side: str) -> None:
    """The video with the near-side leg and angles drawn on, and stills at top / 3 o'clock / bottom."""
    import cv2
    theta = m["_theta"]
    knee_flex = 180 - angle_at(P["hip"], P["knee"], P["ankle"])
    hip_ang = angle_at(P["shoulder"], P["hip"], P["knee"])
    h, w = t["frames"][0].shape[:2]
    vw = cv2.VideoWriter(os.path.join(out_dir, "video.mp4"), cv2.VideoWriter_fourcc(*"mp4v"), t["fps"], (w, h))
    chain = ["shoulder", "hip", "knee", "ankle", "heel", "toe", "ankle"]
    arm = ["shoulder", "elbow", "wrist"]
    for n, f in enumerate(t["frames"]):
        img = f.copy()
        for seq, col in ((chain, (255, 157, 91)), (arm, (180, 180, 180))):
            for a, b in zip(seq, seq[1:]):
                cv2.line(img, tuple(int(v) for v in P[a][n]), tuple(int(v) for v in P[b][n]), col, 3, cv2.LINE_AA)
        for j in ("hip", "knee", "ankle"):
            cv2.circle(img, tuple(int(v) for v in P[j][n]), 6, (32, 176, 255), -1, cv2.LINE_AA)
        txt = [f"knee bend {knee_flex[n]:.0f} deg", f"hip {hip_ang[n]:.0f} deg", f"crank {theta[n] % 360:.0f} deg"]
        for k, s in enumerate(txt):
            cv2.putText(img, s, (20, 40 + 34 * k), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 0), 5, cv2.LINE_AA)
            cv2.putText(img, s, (20, 40 + 34 * k), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2, cv2.LINE_AA)
        vw.write(img)
        t["frames"][n] = img
    vw.release()
    turn = np.floor(theta / 360)
    ks = np.unique(turn)
    mid = ks[len(ks) // 2] if len(ks) else 0
    for name, ph in (("top", 0), ("3oclock", 90), ("bottom", 180)):
        target = mid * 360 + ph
        n = int(np.argmin(np.abs(theta - target)))
        cv2.imwrite(os.path.join(out_dir, f"{name}.png"), t["frames"][n])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video")
    ap.add_argument("--view", choices=("side", "front"), default="side")
    ap.add_argument("--crank", type=float, default=172.5, help="crank length in mm (sets the scale)")
    ap.add_argument("--seconds", type=float, default=25, help="analyse this much of the video")
    ap.add_argument("--start", type=float, default=0, help="skip this many seconds first (getting on the bike)")
    ap.add_argument("--quality", choices=("fast", "balanced", "best"), default="balanced")
    ap.add_argument("--markers", choices=("off", "color", "white"), default="off",
                    help="tape on hip/knee/ankle: bright coloured (best) or white (hip and knee only)")
    ap.add_argument("--side", choices=("auto", "left", "right"), default="auto", help="the leg facing the camera")
    ap.add_argument("--compare", help="an earlier report.json")
    ap.add_argument("--out", help="folder for the report (default: next to the video)")
    a = ap.parse_args()
    out_dir = a.out or os.path.splitext(a.video)[0] + "_fit"
    os.makedirs(out_dir, exist_ok=True)
    print(f"reading {a.video} ...")
    t = track(a.video, a.seconds, a.quality, start=a.start)
    np.savez_compressed(os.path.join(out_dir, "keypoints.npz"), kp=t["kp"], score=t["score"], fps=t["fps"])
    compare = json.load(open(a.compare)) if a.compare else None
    # a camera that moved: keep the longest steady stretch
    hip_mid = (t["kp"][:, 11] + t["kp"][:, 12]) / 2
    thigh = np.median(np.hypot(*(t["kp"][:, 11] - t["kp"][:, 13]).T))
    i0, i1 = steady_span(smooth(hip_mid, 5), 0.25 * thigh)
    steady_note = ""
    if i1 - i0 < len(hip_mid) - 1:
        secs = (i1 - i0) / t["fps"]
        steady_note = f" The camera moved: measured the steadiest {secs:.0f} s." + \
            (" That's short: put the phone on something solid and film again." if secs < 8 else "")
        t = {**t, "kp": t["kp"][i0:i1], "score": t["score"][i0:i1], "frames": t["frames"][i0:i1]}
    if a.view == "side":
        side = near_side(t) if a.side == "auto" else a.side
        P, share = joint_tracks(t, side, a.markers)
        m = side_metrics(P, a.crank)
        if m["turns"] < 3:
            raise SystemExit("fewer than 3 whole pedal strokes found: film 20-30 s of steady pedalling")
        verdict = verdict_side(m)
        used = [k for k, v in share.items() if v >= 0.6]
        note = f"Leg measured: the one nearest the camera; rider faces {m['facing']}. " + \
               (f"Tape markers used for: {', '.join(used)}. " if used else "Pose model only (about ±5°). ") + \
               ("" if P["_upper_ok"] else "Shoulders not in the picture: no back or hip angle (film from further away). ") + steady_note
        draw(out_dir, t, P, m, side)
    else:
        L, _ = joint_tracks(t, "left", a.markers)
        R, _ = joint_tracks(t, "right", a.markers)
        m = front_metrics(L, R, a.crank)
        verdict, note = verdict_front(m), "Left/right as seen by the rider." + steady_note
    path = write_report(out_dir, a.video, a.view, m, verdict, {"crank": a.crank, "note": note}, compare)
    clean = {k: v for k, v in m.items() if not k.startswith("_")}
    with open(os.path.join(out_dir, "report.json"), "w") as fh:
        json.dump({"video": a.video, "view": a.view, "crank_mm": a.crank, "metrics": clean, "verdict": verdict}, fh, indent=1)
    print(open(path, encoding="utf-8").read())
    print(f"-> {out_dir}")


if __name__ == "__main__":
    sys.exit(main())
