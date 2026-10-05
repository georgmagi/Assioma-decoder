"""
The same strap over two links in one ride: ANT+ (the source) and a Bluetooth copy kept in the raw log
(assioma_decoder.BLE_HR_SECOND). Are they the same beats? Each beat gets its time (the notification's
arrival; earlier beats of the same notification counted back by their intervals), artifacts are
judged the way hrv.py does it (more than 20 % off the median of 5 beats either side), the two clocks
are lined up by the offset that matches the most beats, and the matched pairs are compared.

    compare(raw_log_path) -> None (one link only) or {ant, ble: {beats, artifacts, rmssd, alpha1},
        matched, mean_abs_ms, within_3ms_pct, only_ant, only_ble, offset_s}
"""
from __future__ import annotations

import statistics as st
from typing import Optional

import assioma_decoder as dec
import hrv


def _beats(notes: list, char: str) -> list:
    """[(time s, RR ms)] for one link's notifications."""
    out = []
    d = dec.Decoder()
    for n in notes:
        if n.char != char:
            continue
        m = n if char != dec.BLE_HR_SECOND else dec.Notification(n.time, dec.HR_CHAR, n.data)
        for r in d.feed(m):
            t = n.time.timestamp()
            for x in reversed(r.get("rr_ms", [])):
                out.append((t, x))
                t -= x / 1000
    return sorted(out)


def _judge(beats: list) -> list:
    v = [x for _, x in beats]
    out = []
    for i, (t, x) in enumerate(beats):
        w = v[max(0, i - 5): i] + v[i + 1: i + 6]
        m = st.median(w) if w else x
        out.append((t, x, abs(x - m) > 0.2 * m))
    return out


def _pairs(a: list, b: list, shift: float, tol: float = 0.12) -> list:
    out, j = [], 0
    for t, x, bad in a:
        tt = t + shift
        while j + 1 < len(b) and abs(b[j + 1][0] - tt) <= abs(b[j][0] - tt):
            j += 1
        if b and abs(b[j][0] - tt) < tol:
            out.append((x, b[j][1], bad or b[j][2]))
    return out


def _rmssd(xs: list) -> Optional[float]:
    return (st.mean([(q - p) ** 2 for p, q in zip(xs, xs[1:])])) ** 0.5 if len(xs) > 2 else None


def _alpha1(beats: list) -> Optional[float]:
    """The ride's mean alpha 1 from one link (hrv.py's own pipeline, artifacts and all)."""
    from datetime import datetime
    tr = hrv.analyse([(datetime.fromtimestamp(t), [x]) for t, x, _ in beats])
    vals = [v for _, v, *_ in tr.series if v is not None]
    return st.mean(vals) if vals else None


def compare(raw_path: str) -> Optional[dict]:
    from assioma_recorder import read_raw_log
    try:
        notes = list(read_raw_log(raw_path))
    except OSError:
        return None
    A, B = _judge(_beats(notes, dec.ANT_HR_CHAR)), _judge(_beats(notes, dec.BLE_HR_SECOND))
    if len(A) < 60 or len(B) < 60:
        return None
    # the clocks: coarse then fine, keeping the offset that matches the most beats within 15 ms
    def score(s):
        return sum(1 for p, q, bad in _pairs(A, B, s) if not bad and abs(p - q) < 15)
    best = max((k * 0.05 for k in range(-40, 41)), key=score)
    best = max((best + k * 0.005 for k in range(-10, 11)), key=score)
    pairs = [(p, q) for p, q, bad in _pairs(A, B, best) if not bad]
    if not pairs:
        return None
    diff = [p - q for p, q in pairs]
    side = lambda s: {"beats": len(s), "artifacts": sum(1 for *_, bad in s if bad),
                      "rmssd": _rmssd([x for _, x, bad in s if not bad]), "alpha1": _alpha1(s)}
    return {"ant": side(A), "ble": side(B), "matched": len(pairs), "offset_s": round(best, 3),
            "mean_abs_ms": st.mean(map(abs, diff)), "within_3ms_pct": 100 * sum(abs(x) <= 3 for x in diff) / len(diff),
            "only_ant": len(A) - len(_pairs(A, B, best)), "only_ble": len(B) - len(_pairs(B, A, -best))}
