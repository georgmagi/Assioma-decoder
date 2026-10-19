"""bikefit.py's geometry against a simulated rider whose angles are known exactly."""
import math

import pytest

np = pytest.importorskip("numpy")
import bikefit as bf  # noqa: E402


def rider(saddle_up_px=0.0, saddle_fwd_px=0.0, facing=1, turns=6, fps=60, cadence=90, noise=0.0, seed=1):
    """A 2D rider on a bike, in image pixels (y down): crank 172.5 mm = 100 px. Returns (P, truth)."""
    rng = np.random.default_rng(seed)
    n = int(turns * 60 / cadence * fps)
    t = np.arange(n) / fps
    crank_px, thigh, shin = 100.0, 250.0, 245.0
    bb = np.array([600.0, 700.0])
    hip = np.array([600.0 - facing * (140 - saddle_fwd_px), 700.0 - 410 - saddle_up_px])
    phase = 2 * math.pi * cadence / 60 * t                       # 0 = top, increasing: foot goes forward first
    spindle = np.c_[bb[0] + facing * crank_px * np.sin(phase), bb[1] - crank_px * np.cos(phase)]
    # the foot: ankle 60 px up and 45 px back from the spindle, a little ankling with the stroke
    tilt = np.radians(15 + 10 * np.sin(phase))
    foot = np.c_[facing * np.cos(tilt), np.sin(tilt)]           # heel->toe direction
    heel = spindle - 0.7 * 140 * foot
    toe = heel + 140 * foot
    ankle = heel + 0.25 * 140 * foot + np.c_[np.zeros(n), np.full(n, -60.0)]
    # knee: two-link chain hip->ankle, knee forward
    d = ankle - hip
    L = np.linalg.norm(d, axis=1)
    a = (thigh ** 2 - shin ** 2 + L ** 2) / (2 * L)
    h = np.sqrt(np.maximum(thigh ** 2 - a ** 2, 0))
    u = d / L[:, None]
    perp = np.c_[-u[:, 1], u[:, 0]]
    k1, k2 = hip + a[:, None] * u + h[:, None] * perp, hip + a[:, None] * u - h[:, None] * perp
    knee = np.where(((k1[:, 0] - k2[:, 0]) * facing > 0)[:, None], k1, k2)   # the knee points forward
    shoulder = np.tile(hip + [facing * 330, -330], (n, 1))      # 45° back
    elbow = shoulder + [facing * 120, 150]
    wrist = elbow + [facing * 150, 40]
    P = {"hip": np.tile(hip, (n, 1)), "knee": knee, "ankle": ankle, "toe": toe, "heel": heel,
         "shoulder": shoulder, "elbow": elbow, "wrist": wrist}
    if noise:
        P = {k: v + rng.normal(0, noise, v.shape) for k, v in P.items()}
    flex = 180 - bf.angle_at(P["hip"], knee, ankle)
    return P, {"flex_min": flex.min(), "flex_max": flex.max(), "mm_per_px": 1.725,
               "hip_angle_min": bf.angle_at(P["shoulder"], P["hip"], knee).min()}


def test_angle_and_circle():
    assert bf.angle_at([1, 0], [0, 0], [0, 1]) == pytest.approx(90)
    assert bf.angle_at([1, 0], [0, 0], [-1, 0]) == pytest.approx(180)
    th = np.linspace(0, 6, 50)
    cx, cy, r = bf.fit_circle(np.c_[3 + 2 * np.cos(th), -1 + 2 * np.sin(th)])
    assert (cx, cy, r) == pytest.approx((3, -1, 2), abs=1e-6)


@pytest.mark.parametrize("facing", [1, -1])
def test_side_metrics_recover_the_simulated_rider(facing):
    P, truth = rider(facing=facing)
    m = bf.side_metrics(P, 172.5)
    assert m["facing"] == ("right" if facing > 0 else "left")
    assert m["turns"] >= 4
    assert m["knee_flex_bottom"]["mean"] == pytest.approx(truth["flex_min"], abs=0.5)
    assert m["knee_flex_top"]["mean"] == pytest.approx(truth["flex_max"], abs=0.5)
    assert m["hip_angle_min"]["mean"] == pytest.approx(truth["hip_angle_min"], abs=0.5)
    assert m["torso_deg"]["mean"] == pytest.approx(45, abs=0.5)
    assert m["hip_rock_mm"]["mean"] == pytest.approx(0, abs=0.5)
    # the ankle's circle is the crank's: the scale comes out right within a few %
    assert m["mm_per_px"] == pytest.approx(truth["mm_per_px"], rel=0.05)


def test_saddle_height_moves_the_numbers_the_right_way():
    lo = bf.side_metrics(rider(saddle_up_px=-25)[0], 172.5)["knee_flex_bottom"]["mean"]
    mid = bf.side_metrics(rider()[0], 172.5)["knee_flex_bottom"]["mean"]
    hi = bf.side_metrics(rider(saddle_up_px=25)[0], 172.5)["knee_flex_bottom"]["mean"]
    assert lo > mid > hi                                   # higher saddle: straighter knee
    assert any("low" in v for v in bf.verdict_side({"knee_flex_bottom": {"mean": 46, "sd": 1, "n": 6}}))
    assert any("high" in v for v in bf.verdict_side({"knee_flex_bottom": {"mean": 24, "sd": 1, "n": 6}}))
    assert any("right" in v for v in bf.verdict_side({"knee_flex_bottom": {"mean": 35, "sd": 1, "n": 6}}))


def test_saddle_forward_moves_the_knee_ahead_of_the_spindle():
    back = bf.side_metrics(rider(saddle_fwd_px=-20)[0], 172.5)["kops_mm"]["mean"]
    fwd = bf.side_metrics(rider(saddle_fwd_px=30)[0], 172.5)["kops_mm"]["mean"]
    assert fwd > back + 30 * 1.725 * 0.6
    assert any("well forward" in v for v in bf.verdict_side({"kops_mm": {"mean": 5, "sd": 2, "n": 6}}))
    assert any("neutral" in v for v in bf.verdict_side({"kops_mm": {"mean": -40, "sd": 2, "n": 6}}))


def test_noise_is_averaged_out():
    P, truth = rider(noise=3.0, turns=10)
    m = bf.side_metrics({k: bf.smooth(v, 5) for k, v in P.items()}, 172.5)
    assert m["knee_flex_bottom"]["mean"] == pytest.approx(truth["flex_min"], abs=3)


def test_gaps_are_filled():
    xy = np.c_[np.arange(10.0), np.arange(10.0) * 2]
    ok = np.ones(10, bool)
    ok[3:6] = False
    bad = xy.copy()
    bad[3:6] = 0
    assert np.allclose(bf.fill_gaps(bad, ok), xy)


def test_markers_found_near_the_joint():
    cv2 = pytest.importorskip("cv2")
    img = np.full((200, 200, 3), 90, np.uint8)
    cv2.circle(img, (120, 80), 5, (0, 200, 255), -1)          # orange tape
    cv2.rectangle(img, (0, 150), (200, 200), (255, 0, 0), -1)   # a blue shirt: too big, ignored
    m = bf.detect_markers(img, np.array([110.0, 85.0]), 30)
    assert m is not None and np.hypot(*(m - [120, 80])) < 1.5
    assert bf.detect_markers(img, np.array([100.0, 170.0]), 30) is None


def test_steady_span_skips_camera_moves():
    hip = np.zeros((300, 2))
    hip[120:] += np.linspace(0, 400, 180)[:, None]          # the camera pans after 4 s
    i0, i1 = bf.steady_span(hip, 20)
    assert i0 == 0 and 118 <= i1 <= 130


def test_white_tape_and_a_moving_logo_is_rejected():
    cv2 = pytest.importorskip("cv2")
    img = np.full((200, 200, 3), 40, np.uint8)
    cv2.circle(img, (100, 100), 5, (250, 250, 250), -1)
    assert bf.detect_markers(img, np.array([95.0, 98.0]), 30, "white") is not None
    assert bf.detect_markers(img, np.array([95.0, 98.0]), 30, "color") is None
    # a coloured logo that wanders relative to the knee is not taken as tape
    n = 40
    frames, kp = [], np.zeros((n, 26, 2))
    for i in range(n):
        f = np.full((300, 300, 3), 40, np.uint8)
        cv2.circle(f, (150 + (i * 7) % 40 - 20, 150 + (i * 11) % 40 - 20), 5, (0, 220, 255), -1)
        frames.append(f)
    kp[:, :, :] = 150.0
    kp[:, 11] = [150.0, 50.0]                                  # hip above the knee: thigh 100 px
    t = {"kp": kp, "score": np.ones((n, 26)), "fps": 30.0, "frames": frames}
    _, share = bf.joint_tracks(t, "left", "color")
    assert share["knee"] < 0.6
