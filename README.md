# Assioma: the private Bluetooth stream, decoded

Favero Assioma pedals only advertise power, balance and cadence through the standard Cycling Power Service. Every advanced metric travels on a **private BLE characteristic**:

- torque effectiveness
- pedal smoothness
- platform centre offset (PCO)
- power phase
- seated/standing
- battery

This repo decodes that stream and includes a Windows recorder that shows the data live and saves FIT files with Cycling Dynamics.

| | |
|---|---|
| [`PROTOCOL.md`](PROTOCOL.md) | Byte-level spec, validation results and FIT field mapping |
| `assioma_decoder.py` | Reference decoder (standard library only). Turns an nRF Connect log into CSVs |
| `recorder_app.py`, `web/` | The recorder: a desktop app window (live display, ride summaries, progress) |
| `assioma_recorder.py` | Recording core (ride session, Bluetooth, FIT on save) and the log-to-FIT converter |
| `ant_hr.py` | Heart rate over an ANT+ USB stick, with every beat-to-beat interval exact |
| `hrv.py` | RR artifact filter, RMSSD and DFA α1 (2-min window, standard library only) |
| `fit_writer.py` | Minimal FIT activity writer with Cycling Dynamics and heart-rate variability (HRV) |
| `ride_summary.py`, `training.py` | Ride summaries and long-term fitness / fatigue / form |
| `vt1.py` | VT1 (aerobic threshold) from each ride's DFA α1, with a bootstrap interval |
| `ftms.py`, `workout.py` | Smart trainer control (Bluetooth FTMS, ERG) and structured workouts (built in or Zwift `.zwo`) |
| `tests/` | pytest suite: real test vectors, FIT round trip and a simulated Bluetooth stack |
| `samples/` | nRF Connect logs: a ride and HRM 200 beat-to-beat intervals |

## Recorder (Windows)
1. Install Python 3.12+ from python.org and tick "Add python.exe to PATH".
2. Remove the pedals from your phone's apps, so the laptop can connect.
3. Double-click `start_app.bat` (it installs what it needs the first time). Or build a standalone app with its own icon, for the taskbar and Start menu: `pip install pyinstaller pillow`, then `py build_exe.py` makes `Assioma Recorder.exe` in this folder (keep it here: rides and settings live next to it).

The home screen checks the sensors, holds your settings (FTP, threshold heart rate, planned ride length, heart-rate strap) and lists every ride. Recording starts with **Start** or after 3 s of pedalling; **Stop** pauses and asks to save, resume or discard. A ride shows power, cadence, balance, heart rate and DFA α1, Cycling Dynamics (power-phase rings, TE/PS, PCO on a pedal drawing) and live graphs of the last 5 min, 30 min or the whole ride. Saving writes `rides\*.fit` and opens a summary (IF/TSS, decoupling, time in power, heart-rate and α1 zones, best powers, pedalling left/right, a Poincaré plot) that any past ride also opens. A Progress page shows fitness, fatigue and form, a month calendar and weekly totals. FIT files from other apps put in `rides\` are listed too. Dark and LCD themes. Charts by [uPlot](https://github.com/leeoniya/uPlot) (MIT), window by [pywebview](https://pywebview.flowrl.com/).

**VT1 and the Z2 guard:** each ride with α1 gets a VT1 estimate (where α1 crosses 0.75, by heart rate and power, with an 80 % interval), tracked on the Progress page; while riding, the heart-rate tile shows how far you are below or above your own VT1.

**Workouts and ERG:** pick a workout on the home screen (built-in ones, or Zwift `.zwo` files put in a `workouts` folder next to the app) and turn on *Smart trainer*: the app takes control of an FTMS trainer and sets each step's target power. Intensity −/+, skip step, ERG on/off and end workout are on the ride screen. The trainer rides flat whenever the ride is paused or you stop pedalling, after the workout, and when the app closes (it hands control back). Close other apps first: most trainers allow one Bluetooth connection.

**Heart rate:** with an ANT+ USB stick plugged in, heart rate comes over ANT+, where every beat's interval is exact (Bluetooth straps can drop beats, which biases DFA α1). The stick needs the WinUSB driver once: in [Zadig](https://zadig.akeo.ie), Options → List All Devices, pick the stick, WinUSB, Install Driver. Without the stick the strap connects over Bluetooth. The screen marks each sensor ANT+ or Bluetooth.

Other modes:
```
python recorder_app.py --replay rides/ride_..._raw.csv --skip 20     replay a ride from minute 20
python assioma_recorder.py --from-nrf LOG.csv --date 2026-09-28     nRF log -> FIT
python assioma_recorder.py --rebuild rides/ride_..._raw.csv          raw log -> FIT (after a crash)
```

## Development
```
pip install bleak pywebview pytest garmin-fit-sdk pyusb libusb
python -m pytest tests
```

Read-only by design: the code only subscribes to notifications and never writes to the pedals; the ANT+ channel is receive-only.
Not affiliated with or endorsed by Favero Electronics. Assioma is a trademark of Favero Electronics.

MIT licence, see [`LICENSE`](LICENSE).
