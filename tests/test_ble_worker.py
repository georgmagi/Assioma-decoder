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


TRAINER = r'''
import queue, time, os
import assioma_recorder as ar, bleak
ar.PEDAL_FILE = os.environ["PEDAL_FILE"]
q = queue.Queue(); w = ar.BleWorker(q, False, "", ""); w.want_trainer = True; w.start()
w.trainer.MIN_GAP_S = 0.1
for watts in (150, 180, 210, 240, 260):
    w.trainer.want = ("erg", watts); time.sleep(2.5)
w.trainer.want = ("sim", 0.0); time.sleep(2)
seen = w.trainer.power_w
w.stop(); w.join(8)
ops = [x[0] for x in bleak.STATE["trainer_writes"]]
targets = [int.from_bytes(x[1:3], "little", signed=True) for x in bleak.STATE["trainer_writes"] if x[0] == 5]
print(ops.count(0), targets[-1], ops[-2] == 0x11, ops[-1], seen, bleak.STATE["trainer_drops"], w.is_alive())
'''


def test_trainer_is_driven_reconnects_and_is_released(tmp_path):
    env = dict(os.environ, PEDAL_FILE=str(tmp_path / "pedal.txt"),
               PYTHONPATH=os.pathsep.join([os.path.join(HERE, "fake_bleak"), ROOT]))
    out = subprocess.run([sys.executable, "-c", TRAINER], env=env, capture_output=True, text=True, timeout=90)
    # control taken twice (after the drop), the last target held, flat at the end, Reset on the way out,
    # the trainer's own power read from Indoor Bike Data, the worker gone
    assert out.stdout.split() == ["2", "260", "True", "1", "200", "1", "False"], out.stdout + out.stderr
