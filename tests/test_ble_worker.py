"""Bluetooth worker against the fake bleak: picks the left pedal, rejects the right, reconnects."""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

SCRIPT = r'''
import queue, time, collections, os
import assioma_recorder as ar, assioma_decoder as dec, bleak
ar.PEDAL_FILE = os.environ["PEDAL_FILE"]
q = queue.Queue(); w = ar.BleWorker(q, True, "", ""); w.start()
time.sleep(20); w.stop(); w.join(5)
notes = list(q.queue)
bogus = sum(1 for n in notes if n.char == "2A63" and dec.parse_cpm(n.data)["cpm_power_w"] == 999)
c = collections.Counter(n.char for n in notes)
print(w.is_alive(), bogus, c[dec.PRIVATE_CHAR] > 400, c["2A37"] > 5, open(ar.PEDAL_FILE).read(), bleak.STATE["left"])
'''


def test_worker(tmp_path):
    env = dict(os.environ, PEDAL_FILE=str(tmp_path / "pedal.txt"),
               PYTHONPATH=os.pathsep.join([os.path.join(HERE, "fake_bleak"), ROOT]))
    out = subprocess.run([sys.executable, "-c", SCRIPT], env=env, capture_output=True, text=True, timeout=60)
    assert out.stdout.split() == ["False", "0", "True", "True", "L", "2"], out.stdout + out.stderr
