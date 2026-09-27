# Favero Assioma: private BLE stream

These notes were reverse engineered from an **Assioma PRO RS-2** (firmware as of Sep 2026). The data came from four nRF Connect captures, checked against a Garmin fēnix 3 over ANT+ and against the Favero app's Live screen.
The work was read-only: we only subscribed to characteristics and never wrote to one.

## Where the data is

| Service / characteristic | What |
|---|---|
| `2A63` Cycling Power Measurement (notify) | Standard fields: power, balance and crank revolutions |
| `2701000A-489A-4A48-9DA8-80097E460D77` (notify) | **Private stream**: all the advanced metrics |
| `2A19` Battery Level (read) | Battery of the main pedal |

- **The left pedal is the main unit.** It streams data for both legs.
- The right pedal's `27010007` characteristic stays silent.
- Just enable notifications on `2701000A`. No handshake or write is needed.
- **Do not write** to `2A66` (Control Point), `6E400002` (UART RX), `27010006` or DFU.

## Framing

- The stream is **SLIP** framed: `C0 … C0`.
  - Escapes: `DB DC` = `C0` and `DB DD` = `DB`.
- One notification usually holds one frame, but decoders should stream-reassemble anyway.
- An unescaped frame looks like this:

```
[type:1][len:1][counter:1][payload: len-1 bytes]      len = frame_length - 2
```

- The counter is an 8-bit rolling counter, kept separately for each type.
- Multi-byte integers are little-endian.

## Message types

Each payload byte is listed as payload offset (the offset after the counter) → field.

### `10`: summary, about 0.1 Hz
| off | field | encoding |
|---|---|---|
| 0 | cadence | uint8 rpm |
| 1–2 | power | uint16 W |
| 3–4 | energy counter | uint16, ≈0.744 J/unit, rolls over (redundant) |
| 5 | right-leg balance | uint8 %, `FF` = invalid |

Example: `10 07 90 46 67 00 17 1D 2B` → 70 rpm, 103 W, energy 7447, right leg 43 %

### `13`: torque effectiveness and pedal smoothness, about 1 Hz
| off | field | encoding |
|---|---|---|
| 0 | TE left | uint8 ÷ 2 %, `FF` = invalid |
| 1 | TE right | same |
| 2 | PS left | same |
| 3 | PS right | same |

Raw values above 200 (over 100 %) show up very occasionally at near-zero power. Treat them as invalid.

Example: `13 05 8C 4E 38 18 10` → TE 39/28 %, PS 12/8 %

### `E0` (left) and `E1` (right): power phase
| off | field | encoding |
|---|---|---|
| 0 | phase start | uint8 × 360/256 ° |
| 1 | peak phase start | same |
| 2 | peak phase end | same |
| 3 | phase end | same |
| 4–5 | constant `20 1C` | unknown |

- `C0 C0 C0 C0` means invalid (not pedalling). It arrives SLIP-escaped as `DB DC …`.
- `E0` arrives at about 1 Hz. `E1` is sparse, roughly one frame every 4–5 s.

Example: `E0 07 7D F6 21 4F 83 20 1C` → left 345.9° → 184.2°, peak 46.4° → 111.1°

### `E2`: platform centre offset and rider position, about 2 Hz
| off | field | encoding |
|---|---|---|
| 0 | PCO left | int8 mm, `80` = invalid |
| 1 | PCO right | int8 mm, `80` = invalid |
| 2 | rider position | 0 seated · 3 → standing · 2 standing · 1 → seated |

Example: `E2 04 07 04 06 00` → PCO +4 / +6 mm, seated

### `52`: status, about every 30 s
| off | field | encoding |
|---|---|---|
| 0–1 | flags | constant `01 01` |
| 2 | battery left | uint8 % (matches `2A19`) |
| 3 | battery right | uint8 % |
| 4–5 | unknown | constant `26 26` |

Example: `52 07 0D 01 01 33 36 26 26` → battery 51 / 54 %

## Standard `2A63` (flags `0x23`)
- The fields are: power sint16 W, balance uint8 ½ % (referenced to the left leg), crank revolutions uint16, and last crank event time uint16 in 1/1024 s.
- Cadence is Δrevs·60·1024 / Δt, handling 16-bit wraparound.
  - Treat a gap longer than 3 s as a restart.
  - With no new crank event for more than 2 s, report 0 rpm.
- Supported features (`2A65` = `0x00111209`) include balance, crank revolutions and offset compensation. The pedals do **not** advertise extreme angles, dead spots or the vector fields.
  - That's why the advanced metrics only appear on the private stream.

## Validation (per second, 7-min ride vs Garmin ANT+)
| metric | result |
|---|---|
| power | MAE 2.3 W, r 0.998 |
| L/R balance | MAE 0.8 % |
| cadence | MAE 2.5 rpm, r 0.96 (about 1 rpm in steady state) |
| TE | MAE 6–7 % (mostly timing jitter at 1 Hz) |
| PS, PCO, power phase | match the Favero app Live screen |
| rider position | confirmed with stand/sit tests |

## Mapping to FIT (record message, global 20)
Most raw bytes already use FIT's units, so they can be copied straight across.

| FIT field (#) | Source | Conversion |
|---|---|---|
| `left_right_balance` (30) | `10` b5 or `2A63` | `0x80 \| right %` |
| `left/right_torque_effectiveness` (43/44) | `13` b0/b1 | none (both use ½ %) |
| `left/right_pedal_smoothness` (45/46) | `13` b2/b3 | none |
| `left/right_pco` (67/68) | `E2` b0/b1 | none (sint8 mm) |
| `left_power_phase` (69) | `E0` [b0, b3] | none (both use 360/256°) |
| `left_power_phase_peak` (70) | `E0` [b1, b2] | none |
| `right_power_phase` / `_peak` (71/72) | `E1` | none |
| rider position | `E2` b2 | event 44 `rider_position_change`: Favero 0/1/2/3 → FIT 0/2/1/3 |

TE and the balance encoding were checked against the Garmin FIT from the same ride. The PCO and power-phase fields follow the FIT profile; the fēnix 3 doesn't record them.

## Unknowns
- There are three constant pairs: `20 1C`, `01 01` and `26 26`.
  - They were identical in all 3,666 frames across four captures.
  - They carry no ride information.
- Every metric shown on the Favero app's Live screen is accounted for.
