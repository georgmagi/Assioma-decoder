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
| `assioma_recorder.py` | Windows/macOS/Linux recorder: live window plus FIT output (needs `bleak`) |
| `dashboard.py` | The live window: Garmin-style Cycling Dynamics, power/HR graph, RR + DFA α1 graph |
| `hrv.py` | RR artifact filter, RMSSD and DFA α1 (2-min window, standard library only) |
| `fit_writer.py` | Minimal FIT activity writer with Cycling Dynamics and heart-rate variability (HRV) |
| `swift/AssiomaParser.swift` | Swift port for iOS apps (**not compiled yet**) |
| `tests/` | pytest suite: real test vectors, FIT round trip and a simulated Bluetooth stack |
| `samples/` | nRF Connect logs: ride, seated/standing, idle, HRM 200 beat-to-beat intervals |

## Recorder (Windows)
1. Install Python 3.12 from python.org and tick "Add python.exe to PATH".
2. Remove the pedals from your phone's apps, so the laptop can connect.
3. Double-click `start_recorder.bat`, or `start_recorder_hr.bat` to include a heart-rate strap. Set your planned ride length and FTP at the top of `start_recorder_hr.bat` (`PLAN`, `FTP`).
4. The dashboard shows live data as soon as the sensors connect; recording starts when you press **Start** (or Space), or by itself after 3 s of pedalling. **Space** pauses and resumes (paused time is left out of the ride and marked in the FIT). **Stop** pauses and asks: *Save ride*, *Resume* or *Discard*. The ride lands in `rides\*.fit`, with a summary.

The window shows power, cadence, balance, heart rate and DFA α1 on top (every heartbeat drifts across the heart-rate tile, so a poor strap contact shows at once); Cycling Dynamics (power-phase rings, TE/PS, PCO on a pedal drawing, power-phase angles) on the left; on the right, scrolling graphs of the last 5 minutes (power + heart rate, beat-to-beat intervals + DFA α1 with 0.75 ≈ VT1 and 0.5 ≈ VT2 marked) and a whole-ride overview on your planned duration (`--plan 90`). `--ftp 250` shades your Z2 band; F11 is full screen. The per-second CSV also gets `dfa_a1`, `rmssd_ms` and `rr_artifact_pct` columns.

Other modes:
```
python assioma_recorder.py --from-nrf LOG.csv --date 2026-09-28    nRF log -> FIT
python assioma_recorder.py --rebuild rides\ride_..._raw.csv          raw log -> FIT (after a crash)
python assioma_recorder.py --replay LOG.csv --date 2026-09-26        test the window
```

## Development
```
pip install bleak pytest garmin-fit-sdk
python -m pytest tests
```

Read-only by design: the code only subscribes to notifications and never writes to the pedals.
Not affiliated with or endorsed by Favero Electronics. Assioma is a trademark of Favero Electronics.

MIT licence, see [`LICENSE`](LICENSE).
