"""ANT+ heart rate: RR intervals rebuilt exactly from the strap's beat count and beat times."""
import random
from datetime import datetime

import ant_hr
import assioma_decoder as dec


def _exact(rr_ms):
    """The intervals as the strap can report them: beat times rounded to 1/1024 s."""
    ticks, t = [], 0.0
    for x in rr_ms:
        t += x / 1000
        ticks.append(round(t * 1024))
    return [round((b - a) * 1000 / 1024, 1) for a, b in zip(ticks, ticks[1:])]


def _strap(rr_ms, slot=0.24605, start_count=200, start_time=60000):
    """What a strap broadcasts: one 8-byte page every slot (4.06 Hz), always page 4 here
    (page number, spare, previous beat time, last beat time, beat count, HR)."""
    beats, t = [], 0.0
    for x in rr_ms:
        t += x / 1000
        beats.append(t)
    pages, k, now = [], -1, 0.0
    while now < beats[-1]:
        while k + 1 < len(beats) and beats[k + 1] <= now:
            k += 1
        if k >= 1:
            last = (start_time + round(beats[k] * 1024)) % 65536
            prev = (start_time + round(beats[k - 1] * 1024)) % 65536
            pages.append(bytes([0x04, 0xFF, prev & 0xFF, prev >> 8, last & 0xFF, last >> 8,
                                (start_count + k) % 256, 75]))
        now += slot
    return pages


def _rr(n, seed=1):
    rnd = random.Random(seed)
    return [rnd.uniform(330, 1100) for _ in range(n)]          # 55-180 bpm


def _feed(pages):
    b, out = ant_hr.Beats(), []
    for p in pages:
        out += b.feed(p)
    return b, out


def test_every_interval_comes_back_exactly_across_wraps():
    rr = _rr(600)                                               # beat count and time both wrap
    b, out = _feed(_strap(rr))
    want = _exact(rr)
    assert len(out) >= len(want) - 2 and b.lost == 0
    i = want.index(out[0])                                      # the first beats arrive before any page
    assert out == want[i: i + len(out)]


def test_one_lost_message_in_a_row_costs_no_beats():
    rr = _rr(400, seed=2)
    pages = _strap(rr)
    rnd = random.Random(3)
    kept, dropped_last = [], False
    for p in pages:                                             # drop ~30 %, never two in a row
        if not dropped_last and rnd.random() < 0.3:
            dropped_last = True
            continue
        dropped_last = False
        kept.append(p)
    b, out = _feed(kept)
    _, full = _feed(pages)
    assert out == full and b.lost == 0


def test_a_long_gap_or_stale_data_never_makes_up_an_interval():
    rr = [800.0] * 40
    pages = _strap(rr)
    stale = bytes([0x04, 0xFF, 0x00, 0x10, 0x00, 0x14, 77, 80])   # left in the stick from last time
    b, out = _feed([stale] + pages[:20] + pages[40:])             # and ~5 s of nothing
    assert all(abs(x - 800) < 1.5 for x in out)
    assert b.lost > 0


def test_ant_notifications_decode_like_bluetooth_without_the_repeat_filter():
    d, t = dec.Decoder(), datetime(2026, 10, 1, 12)
    out = []
    for rr in ([781.2], [781.2], [781.2], [], [812.5, 790.0]):      # equal intervals are real beats
        for r in d.feed(ant_hr.hr_notification(t, 75, rr)):
            assert r["type"] == dec.HR_CHAR and r["heart_rate_bpm"] == 75
            out += r.get("rr_ms", [])
    assert out == [781.2, 781.2, 781.2, 812.5, 790.0]


def test_stick_messages_split_and_checksum():
    a, b = ant_hr.message(0x4E, [0, 1, 2, 3, 4, 5, 6, 7, 8]), ant_hr.message(0x40, [0, 1, 2])
    buf = bytearray(b"\x00\xa4" + a + b[:3])                       # junk, a, half of b
    assert ant_hr.split_messages(buf) == [(0x4E, bytes([0, 1, 2, 3, 4, 5, 6, 7, 8]))]
    buf += b[3:]
    assert ant_hr.split_messages(buf) == [(0x40, bytes([0, 1, 2]))] and not buf


def test_garmin_rides_lose_alpha1_a_polar_keeps_it():
    import ride_summary as rs
    assert rs.smoothed_rr({"hr_strap": {"name": "Garmin strap #17859", "link": "ANT+"}})
    assert rs.smoothed_rr({"hr_strap": {"name": "HRM200:83395", "link": "Bluetooth"}})
    assert not rs.smoothed_rr({"hr_strap": {"name": "Polar strap #4242", "link": "ANT+"}})
    assert not rs.smoothed_rr({"hr_strap": {"name": "Polar H10 A1B2C3D4", "link": "Bluetooth"}})
    assert not rs.smoothed_rr({})                                            # older rides: as before
