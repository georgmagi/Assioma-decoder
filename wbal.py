"""
The anaerobic reserve, W' balance: how much of the work you can do above critical power is left.

The critical power model splits your power in two. CP (in W) is the highest power you can hold for a
long time on aerobic metabolism; FTP is a close stand-in for it. W' (in J) is a fixed reserve of
work you can only do above CP: every second at P > CP spends P - CP joules of it, and at zero you
can't hold anything above CP any more. Below CP it refills, fast when it is low and when you ride
easy, slowly as it gets near full (Skiba et al. 2015, the "differential" model used by Golden
Cheetah and WKO):

    above CP:   W'bal -= P - CP
    below CP:   W'bal += (W' - W'bal) * (CP - P) / W'

W' comes from your newest ramp test, with CP = your FTP: a ramp to exhaustion spends the whole
reserve, so the work above CP over its last climb is W'. Two cautions: if the ramp ended before you
were truly done, the reserve is underestimated (it reads as emptier than it is), and the model is
good to perhaps 10-20 %: its use is comparing sessions with each other, not the exact joule.
Standard library only.
"""
from __future__ import annotations

from typing import Optional

MIN_W_PRIME, MAX_W_PRIME = 3000, 40000     # J: outside this the ramp wasn't a real one to exhaustion
SMOOTH_S = 30                              # the ramp's last climb starts where 30-s power rose over CP


def ramp_w_prime(power: list, cp: float) -> Optional[float]:
    """W' from a ramp test: the work above CP from where the 30-s power last rose over CP to where
    pedalling stopped. None when the ramp never got clearly over CP."""
    p = [v or 0 for v in power]
    while p and p[-1] < 50:                    # the cool-down / standing still after the end
        p.pop()
    if len(p) < SMOOTH_S or not cp:
        return None
    start = None
    acc = sum(p[:SMOOTH_S])
    for i in range(SMOOTH_S, len(p) + 1):
        above = acc / SMOOTH_S > cp
        if above and start is None:
            start = i - SMOOTH_S
        elif not above:
            start = None
        if i < len(p):
            acc += p[i] - p[i - SMOOTH_S]
    if start is None:
        return None
    w = sum(max(0.0, v - cp) for v in p[start:])
    return w if MIN_W_PRIME <= w <= MAX_W_PRIME else None


class Balance:
    """W'bal second by second, fed one power value (W, None = not pedalling) at a time."""

    def __init__(self, cp: float, w_prime: float) -> None:
        self.cp, self.w_prime, self.w = cp, w_prime, w_prime

    def add(self, p: Optional[float]) -> float:
        p = p or 0.0
        if p > self.cp:
            self.w -= p - self.cp
        else:
            self.w += (self.w_prime - self.w) * (self.cp - p) / self.w_prime
        return self.w


def series(power: list, cp: float, w_prime: float) -> list:
    b = Balance(cp, w_prime)
    return [b.add(p) for p in power]


def efforts(power: list, bal: list, cp: float, w_prime: float, min_s: int = 30) -> list:
    """Each stretch above CP (30-s power over CP for at least min_s; dips under 30 s are bridged):
    [{start, end (s), power (W, mean), spent (J), low (J, W'bal at its end)}]."""
    n = len(power)
    if n < SMOOTH_S:
        return []
    p = [v or 0 for v in power]
    above, acc = [False] * n, sum(p[:SMOOTH_S])
    for i in range(SMOOTH_S, n + 1):
        if acc / SMOOTH_S > cp:
            for k in range(i - SMOOTH_S, i):
                above[k] = True
        if i < n:
            acc += p[i] - p[i - SMOOTH_S]
    out, i = [], 0
    while i < n:
        if not above[i]:
            i += 1
            continue
        j = i
        while j < n and above[j]:
            j += 1
        # trim the 30-s smoothing's edges back to the seconds really over CP
        a, b = i, j
        while a < b and p[a] <= cp:
            a += 1
        while b > a and p[b - 1] <= cp:
            b -= 1
        if b - a >= min_s:
            out.append({"start": a, "end": b, "power": sum(p[a:b]) / (b - a),
                        "spent": (bal[a - 1] if a else w_prime) - bal[b - 1],
                        "low": bal[b - 1]})
        i = j
    return out


def summary(power: list, cp: Optional[float], w_prime: Optional[float]) -> Optional[dict]:
    """The ride's reserve: {cp, w_prime, series (J, every second), low, low_s, efforts, above_cp_s,
    above_cp_kj}. None without CP and W'."""
    if not cp or not w_prime or not power:
        return None
    bal = series(power, cp, w_prime)
    low = min(bal)
    return {"cp": cp, "w_prime": w_prime, "series": [round(x) for x in bal], "low": low,
            "low_s": bal.index(low), "efforts": efforts(power, bal, cp, w_prime),
            "above_cp_s": sum(1 for p in power if p and p > cp),
            "above_cp_kj": sum(p - cp for p in power if p and p > cp) / 1000}
