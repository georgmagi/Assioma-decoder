"""One strap over ANT+ and Bluetooth in the same ride: the comparison (linkcheck.py)."""
import random
from datetime import datetime, timedelta

import ant_hr
import assioma_decoder as dec
import linkcheck

T0 = datetime(2026, 10, 5, 18)


def _ble(rr_list):
    data = bytearray([0x10, 70])
    for x in rr_list:
        data += round(x * 1024 / 1000).to_bytes(2, "little")
    return bytes(data)


def test_same_beats_over_both_links_match_and_losses_are_counted(tmp_path):
    rnd = random.Random(5)
    rr = [800 + 40 * (-1) ** k + rnd.gauss(0, 12) for k in range(600)]
    rows, t, pend = [], T0, []
    for k, x in enumerate(rr):
        t += timedelta(milliseconds=x)
        q = round(round(x * 1024 / 1000) * 1000 / 1024, 1)            # both links: 1/1024 s
        rows.append((t + timedelta(milliseconds=120), dec.ANT_HR_CHAR, ant_hr.hr_notification(t, 70, [q]).data))
        if k == 300:
            continue                                                   # Bluetooth loses one beat
        pend.append(q)
        if k % 2:                                                      # Bluetooth sends every other beat, in twos
            rows.append((t + timedelta(milliseconds=40), dec.BLE_HR_SECOND, _ble(pend)))
            pend = []
    rows.sort(key=lambda r: r[0])
    path = tmp_path / "ride_raw.csv"
    path.write_text("time_utc,char,hex\n" + "".join(f"{a.isoformat()},{c},{d.hex()}\n" for a, c, d in rows))
    c = linkcheck.compare(str(path))
    assert c["matched"] > 550 and c["mean_abs_ms"] < 1.5 and c["within_3ms_pct"] > 95
    assert c["ant"]["beats"] == 600 and c["ble"]["beats"] >= 595
    assert abs(c["ant"]["rmssd"] - c["ble"]["rmssd"]) < 3


def test_one_link_only_is_no_comparison(tmp_path):
    path = tmp_path / "ride_raw.csv"
    path.write_text("time_utc,char,hex\n" + "".join(
        f"{(T0 + timedelta(seconds=k)).isoformat()},{dec.ANT_HR_CHAR},{ant_hr.hr_notification(T0, 60, [1000.0]).data.hex()}\n" for k in range(100)))
    assert linkcheck.compare(str(path)) is None
